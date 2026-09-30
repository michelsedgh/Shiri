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
from shiri.domain import Conflict, DomainError, NotFound, RoomCreate, RoomPatch, StrictModel
from shiri.rpc import RpcError
from shiri.runtime_port import SimulatedRuntime, SocketRuntime
from shiri.service import RoomService
from shiri.settings import Settings
from shiri.store import Store

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
    auth = None if settings.simulation else Auth(token or settings.api_token_file.read_text().strip())
    service = RoomService(store, runtime)
    stop = asyncio.Event()

    async def reconcile_loop():
        while not stop.is_set():
            try:
                await service.reconcile()
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
            store.close()

    app = FastAPI(title="Shiri", version="2.0.0", lifespan=lifespan,
                  docs_url=None, openapi_url="/api/openapi.json", redoc_url=None)
    app.state.service = service
    app.state.settings = settings
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
        await service._store("get_room", room_id)
        return await runtime.call("outputs", {"room_id": room_id})

    @app.put("/api/v1/rooms/{room_id}/speakers")
    async def assign_speakers(room_id: str, body: AssignmentRequest):
        return await service.assign(room_id, body.speaker_ids, body.expected_revision)

    @app.patch("/api/v1/rooms/{room_id}/speakers/{speaker_id}/offset")
    async def speaker_offset(room_id: str, speaker_id: str, body: OffsetRequest):
        return await service.offset(room_id, speaker_id, body.offset_ms, body.expected_revision)

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
