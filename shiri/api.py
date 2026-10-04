"""Typed rootless HTTP API and local browser UI. Linux actions go through RPC."""
from collections import defaultdict, deque
from contextlib import asynccontextmanager
import asyncio
import logging
import sqlite3
from pathlib import Path
import time

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import Field, field_validator

from shiri.auth import Auth, COOKIE, SESSION_SECONDS
from shiri.domain import AirplayTiming, Conflict, DomainError, NotFound, Room, RoomCreate, RoomPatch, StrictModel
from shiri.rpc import RpcError
from shiri.runtime_port import SimulatedRuntime, SocketRuntime
from shiri.service import RoomService
from shiri.settings import Settings
from shiri.store import Store
from shiri.tts.coordinator import TextSpeechCoordinator, TextSpeechRequest
from shiri.readiness import RoomReadinessCoordinator, WarmRequest, WarmRenew

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent / "web"
MAX_BODY = 2 * 1024 * 1024


class PatchRequest(StrictModel):
    expected_revision: int = Field(ge=1)
    changes: RoomPatch


class AssignmentRequest(StrictModel):
    expected_revision: int = Field(ge=1)
    speaker_ids: list[str] = Field(max_length=128)


class OffsetRequest(StrictModel):
    expected_revision: int = Field(ge=1)
    offset_ms: int = Field(ge=-2000, le=2000)


class BalanceRequest(StrictModel):
    expected_revision: int = Field(ge=1)
    balance_percent: int = Field(ge=0, le=100)


class AirplayTimingRequest(StrictModel):
    expected_revision: int = Field(ge=1)
    airplay_timing: AirplayTiming


class LocalDeviceBinding(StrictModel):
    selection_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    binding: str
    conversion: bool = True

    @field_validator("binding")
    @classmethod
    def validate_binding(cls, value):
        if value not in {"serial", "port", "path", "loopback"}:
            raise ValueError("Choose a supported physical speaker binding")
        return value


class CalibrationCreate(StrictModel):
    expected_revision: int = Field(ge=1)
    target_id: str = Field(pattern=r"^(0|[1-9][0-9]{0,19})$")
    reference_id: str = Field(pattern=r"^(0|[1-9][0-9]{0,19})$")
    reference_room_id: str | None = None
    expected_reference_revision: int | None = Field(default=None, ge=1)
    playback_context: str | None = Field(default=None, min_length=1, max_length=512)
    capture_device: str = Field(min_length=1, max_length=256)
    geometry: str = Field(min_length=1, max_length=512)
    max_lag_ms: int = Field(default=500, ge=1, le=2000)
    geometry_correction_ms: float = Field(default=0.0, ge=-100, le=100, allow_inf_nan=False)

    @field_validator("reference_room_id")
    @classmethod
    def validate_reference_room(cls, value):
        return Room.validate_id(value) if value is not None else None

    @field_validator("capture_device", "geometry", "playback_context")
    @classmethod
    def validate_description(cls, value):
        if value is None:
            return value
        if value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Use a description without control characters or surrounding whitespace")
        return value


class CalibrationApply(StrictModel):
    expected_revision: int = Field(ge=1)
    expected_generation: int = Field(ge=1)
    expected_reference_revision: int | None = Field(default=None, ge=1)


class SessionRequest(StrictModel):
    token: str = Field(min_length=1, max_length=256)


class PlayerRequest(StrictModel):
    action: str

    @field_validator("action")
    @classmethod
    def validate_action(cls, value):
        if value not in {"play", "stop"}:
            raise ValueError("action must be play or stop")
        return value


class SpeechRequest(StrictModel):
    action: str = "offer"
    session_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    request_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    sdp: str | None = Field(default=None, max_length=1024 * 1024)
    type: str = "offer"

    @field_validator("action")
    @classmethod
    def validate_action(cls, value):
        if value not in {"offer", "control", "close"}:
            raise ValueError("action must be offer, control, or close")
        return value


class BodyLimit:
    """Bound streamed bodies before FastAPI's JSON parser allocates them."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        body = bytearray()
        deadline = asyncio.get_running_loop().time() + 15
        while True:
            try:
                message = await asyncio.wait_for(receive(), timeout=max(0, deadline - asyncio.get_running_loop().time()))
            except asyncio.TimeoutError:
                return await JSONResponse({"error": "Request body timed out", "code": "deadline_exceeded"}, status_code=408)(scope, receive, send)
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > MAX_BODY:
                return await JSONResponse({"error": "Request body exceeds 2 MiB", "code": "request_too_large"}, status_code=413)(scope, receive, send)
            if not message.get("more_body", False):
                break
        delivered = False
        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()
        await self.app(scope, bounded_receive, send)


def create_app(settings: Settings | None = None, *, store=None, runtime=None, token=None):
    settings = settings or Settings.from_env()
    store = store or Store(settings.database, max_rooms=settings.max_rooms)
    runtime = runtime or (SimulatedRuntime() if settings.simulation else SocketRuntime(settings.runtime_socket))
    auth = (None if settings.simulation or settings.allow_unauthenticated
            else Auth(token or settings.api_token_file.read_text().strip()))
    service = RoomService(store, runtime)
    tts = TextSpeechCoordinator(service, worker_url=settings.tts_worker_url, max_rooms=settings.max_rooms,
                               worker_token=(settings.tts_worker_token_file.read_text().strip()
                                             if settings.tts_worker_url else None))
    readiness = RoomReadinessCoordinator(service, tts, automatic=(
        settings.speaker_readiness if not settings.simulation else False))
    service.warm_coordinator = readiness
    stop = asyncio.Event()

    async def reconcile_loop():
        while not stop.is_set():
            try:
                reconciled = await service.reconcile()
                async with service._mutation:
                    rooms = await service._store("list_rooms")
                    await readiness.reconcile_locked(rooms)
                    await readiness.maintain_locked(reconciled.get("runtime"), rooms=rooms)
            except Exception:
                log.exception("Desired room configuration could not reconcile")
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(reconcile_loop())
        try:
            yield
        finally:
            stop.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await readiness.close()
            await tts.close()
            store.close()

    app = FastAPI(title="Shiri", version="2.0.0", lifespan=lifespan,
                  docs_url=None, openapi_url="/api/openapi.json", redoc_url=None)
    app.state.service = service
    app.state.settings = settings
    app.state.tts = tts
    app.state.readiness = readiness
    app.add_middleware(BodyLimit)
    login_attempts = defaultdict(deque)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/"):
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                origin = request.headers.get("origin")
                if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                    return JSONResponse({"error": "Cross-origin control is not allowed", "code": "forbidden"}, status_code=403)
            public = path in {"/api/v1/session", "/api/v1/health/live", "/api/v1/health/ready"}
            if auth is not None and not public:
                header = request.headers.get("authorization", "")
                bearer = header[7:] if header.startswith("Bearer ") else ""
                if not auth.token_valid(bearer) and not auth.session_valid(request.cookies.get(COOKIE, "")):
                    return JSONResponse({"error": "Sign in with this installation's admin token", "code": "unauthorized"}, status_code=401)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; media-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        status = 404 if isinstance(exc, NotFound) else 409 if isinstance(exc, Conflict) else 400
        return JSONResponse({"error": str(exc), "code": "not_found" if status == 404 else "conflict" if status == 409 else "invalid_request"}, status_code=status)

    @app.exception_handler(RpcError)
    async def runtime_error(request, exc):
        status = 409 if exc.code in {"conflict", "session_conflict"} else 404 if exc.code == "not_found" else 503
        return JSONResponse({"error": str(exc), "code": exc.code}, status_code=status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        errors = [{"field": ".".join(str(part) for part in item["loc"]), "message": item["msg"]} for item in exc.errors()]
        return JSONResponse({"error": "Request validation failed", "code": "invalid_request", "fields": errors}, status_code=422)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse({"error": str(exc.detail), "code": "invalid_request"}, status_code=exc.status_code)

    @app.exception_handler(sqlite3.Error)
    @app.exception_handler(OSError)
    async def io_error(request, exc):
        log.exception("Storage operation failed")
        return JSONResponse({"error": "Storage operation failed; inspect the API service logs", "code": "storage_error"}, status_code=503)

    @app.get("/api/v1/health/live")
    async def live():
        return {"status": "live", "version": "2.0.0"}

    @app.get("/api/v1/health/ready")
    async def ready():
        healthy = await service.readiness()
        return JSONResponse({"ready": healthy, "simulation": settings.simulation}, status_code=200 if healthy else 503)

    @app.post("/api/v1/session")
    async def login(body: SessionRequest, request: Request):
        address = request.client.host if request.client else "local"
        if len(login_attempts) > 1024:
            login_attempts.clear()
        attempts = login_attempts[address]
        now = time.monotonic()
        while attempts and now - attempts[0] > 60:
            attempts.popleft()
        if len(attempts) >= 10:
            return JSONResponse({"error": "Too many sign-in attempts; retry in a minute", "code": "rate_limited"}, status_code=429)
        attempts.append(now)
        if auth is not None and not auth.token_valid(body.token):
            return JSONResponse({"error": "Admin token is incorrect", "code": "unauthorized"}, status_code=401)
        response = JSONResponse({"ok": True})
        if auth is not None:
            response.set_cookie(COOKIE, auth.create_session(), max_age=SESSION_SECONDS, httponly=True,
                                secure=request.url.scheme == "https", samesite="strict")
        return response

    @app.delete("/api/v1/session")
    async def logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE)
        return response

    @app.get("/api/v1/state")
    async def state():
        return await service.state()

    @app.get("/api/v1/local-devices")
    async def local_devices():
        return await service.local_devices()

    @app.post("/api/v1/local-devices/bind", status_code=201)
    async def bind_local_device(body: LocalDeviceBinding):
        return await service.bind_local_device(body.selection_id, body.binding, body.conversion)

    @app.post("/api/v1/rooms", status_code=201)
    async def create_room(body: RoomCreate):
        return await service.create(body)

    @app.patch("/api/v1/rooms/{room_id}")
    async def update_room(room_id: str, body: PatchRequest):
        return await service.update(room_id, body.changes, body.expected_revision)

    @app.delete("/api/v1/rooms/{room_id}")
    async def delete_room(room_id: str, expected_revision: int = Query(ge=1)):
        return await service.delete(room_id, expected_revision)

    @app.get("/api/v1/rooms/{room_id}/speakers")
    async def speakers(room_id: str):
        return {"room_id": room_id, "outputs": await service.discover(room_id)}

    @app.put("/api/v1/rooms/{room_id}/speakers")
    async def assign_speakers(room_id: str, body: AssignmentRequest):
        return await service.assign(room_id, body.speaker_ids, body.expected_revision)

    @app.patch("/api/v1/rooms/{room_id}/speakers/{speaker_id}/offset")
    async def speaker_offset(room_id: str, speaker_id: str, body: OffsetRequest):
        return await service.offset(room_id, speaker_id, body.offset_ms, body.expected_revision)

    @app.patch("/api/v1/rooms/{room_id}/speakers/{speaker_id}/balance")
    async def speaker_balance(room_id: str, speaker_id: str, body: BalanceRequest):
        return await service.balance(room_id, speaker_id, body.balance_percent, body.expected_revision)

    @app.patch("/api/v1/rooms/{room_id}/speakers/{speaker_id}/airplay-timing")
    async def speaker_airplay_timing(room_id: str, speaker_id: str, body: AirplayTimingRequest):
        return await service.airplay_timing(room_id, speaker_id, body.airplay_timing, body.expected_revision)

    @app.post("/api/v1/rooms/{room_id}/calibration", status_code=201)
    async def calibration_create(room_id: str, body: CalibrationCreate):
        return await service.calibration_create(room_id, body)

    @app.get("/api/v1/rooms/{room_id}/calibration")
    async def calibration_list(room_id: str):
        return await service.calibration_list(room_id)

    @app.get("/api/v1/rooms/{room_id}/calibration/{session_id}")
    async def calibration_session(room_id: str, session_id: str):
        return await service.calibration_get(room_id, session_id)

    @app.get("/api/v1/rooms/{room_id}/calibration/{session_id}/probe.wav")
    async def calibration_probe(room_id: str, session_id: str):
        return Response(await service.calibration_probe(room_id, session_id), media_type="audio/wav",
                        headers={"Content-Disposition": 'attachment; filename="shiri-calibration-probe.wav"'})

    @app.post("/api/v1/rooms/{room_id}/calibration/{session_id}/recordings")
    async def calibration_recording(room_id: str, session_id: str, request: Request,
                                    verification: bool = Query(default=False)):
        if request.headers.get("content-type", "").split(";", 1)[0].lower() not in {"audio/wav", "audio/x-wav"}:
            raise HTTPException(415, "Upload the recording as audio/wav")
        return await service.calibration_recording(room_id, session_id, await request.body(), verification)

    @app.post("/api/v1/rooms/{room_id}/calibration/{session_id}/apply")
    async def calibration_apply(room_id: str, session_id: str, body: CalibrationApply):
        return await service.calibration_apply(room_id, session_id, body.expected_revision, expected_generation=body.expected_generation,
                                               expected_reference_revision=body.expected_reference_revision)

    @app.post("/api/v1/rooms/{room_id}/calibration/{session_id}/rollback")
    async def calibration_rollback(room_id: str, session_id: str, body: CalibrationApply):
        return await service.calibration_apply(room_id, session_id, body.expected_revision, rollback=True, expected_generation=body.expected_generation,
                                               expected_reference_revision=body.expected_reference_revision)

    @app.get("/api/v1/rooms/{room_id}/calibration/{session_id}/export")
    async def calibration_export(room_id: str, session_id: str):
        return JSONResponse(await service.calibration_get(room_id, session_id),
                            headers={"Content-Disposition": 'attachment; filename="shiri-calibration-evidence.json"'})

    @app.delete("/api/v1/rooms/{room_id}/calibration/{session_id}")
    async def calibration_delete(room_id: str, session_id: str):
        return await service.calibration_delete(room_id, session_id)

    @app.post("/api/v1/rooms/{room_id}/player")
    async def player(room_id: str, body: PlayerRequest):
        await service._store("get_room", room_id)
        return await runtime.call("player", {"room_id": room_id, "action": body.action})

    def validate_speech(body):
        if body.action == "offer" and (not body.sdp or body.type != "offer"):
            raise HTTPException(422, "An SDP offer is required")

    @app.post("/api/v1/rooms/{room_id}/speech")
    async def speech(room_id: str, body: SpeechRequest):
        validate_speech(body)
        return await service.speech(room_id, body.model_dump(exclude_none=True))

    @app.post("/api/v1/nobly/rooms/{external_id:path}/speech")
    async def nobly_speech(external_id: str, body: SpeechRequest):
        validate_speech(body)
        return await service.admit_nobly(external_id, body.model_dump(exclude_none=True))

    @app.get("/api/v1/events")
    async def events(limit: int = Query(default=100, ge=1, le=200)):
        entries = await service._store("list_events", limit)
        return {"events": [event.model_dump(mode="json") for event in entries]}

    @app.get("/api/v1/tts/models")
    async def tts_models():
        return await tts.catalog()

    class ModelLoad(StrictModel):
        model_id: str = Field(min_length=1, max_length=128)

    @app.post("/api/v1/tts/models/load", status_code=202)
    async def tts_load(body: ModelLoad):
        return await tts.load(body.model_id)

    @app.post("/api/v1/tts/benchmark", status_code=202)
    async def tts_benchmark(body: TextSpeechRequest):
        return await tts.admit(body, benchmark=True)

    @app.post("/api/v1/rooms/{room_id}/tts", status_code=202)
    async def room_tts(room_id: str, body: TextSpeechRequest):
        return await tts.admit(body, room_id=room_id)

    @app.post("/api/v1/nobly/rooms/{external_id:path}/tts", status_code=202)
    async def nobly_tts(external_id: str, body: TextSpeechRequest):
        return await tts.admit(body, external_id=external_id)

    @app.post("/api/v1/rooms/{room_id}/warm", status_code=202)
    async def room_warm(room_id: str, body: WarmRequest):
        return await readiness.acquire(body, room_id=room_id)

    @app.post("/api/v1/nobly/rooms/{external_id:path}/warm", status_code=202)
    async def nobly_warm(external_id: str, body: WarmRequest):
        return await readiness.acquire(body, external_id=external_id)

    @app.get("/api/v1/rooms/{room_id}/warm/{lease_id}")
    async def room_warm_status(room_id: str, lease_id: str):
        return await readiness.get(room_id, lease_id)

    @app.post("/api/v1/rooms/{room_id}/warm/{lease_id}", status_code=202)
    async def room_warm_renew(room_id: str, lease_id: str, body: WarmRenew):
        return await readiness.renew(room_id, lease_id, body)

    @app.delete("/api/v1/rooms/{room_id}/warm/{lease_id}")
    async def room_warm_release(room_id: str, lease_id: str):
        return await readiness.release(room_id, lease_id)

    @app.get("/api/v1/tts/jobs/{job_id}")
    async def tts_job(job_id: str):
        return tts.get(job_id).public()

    @app.delete("/api/v1/tts/jobs/{job_id}")
    async def cancel_tts(job_id: str):
        return await tts.cancel(job_id)

    @app.get("/api/v1/tts/jobs/{job_id}/sample.wav")
    async def tts_sample(job_id: str):
        return Response(tts.sample(job_id), media_type="audio/wav",
                        headers={"Content-Disposition": 'inline; filename="shiri-voice-preview.wav"'})

    @app.get("/api/v1/rooms/{room_id}/diagnostics")
    async def diagnostics(room_id: str):
        await service._store("get_room", room_id)
        return await runtime.call("diagnostics", {"room_id": room_id})

    @app.get("/")
    async def index():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return Response(status_code=204)

    app.mount("/assets", StaticFiles(directory=WEB_DIR, check_dir=False), name="assets")
    return app
