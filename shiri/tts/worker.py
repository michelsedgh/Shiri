"""Optional Mac generation service; isolated, persistent and single-flight."""
from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hmac
import json
import multiprocessing
import os
from pathlib import Path
import re
import stat
import time

from anyio import CancelScope, fail_after
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from shiri.tts.models import DEFAULT_MODEL_ID, MAX_GENERATED_SECONDS, GenerationRequest, get_models, get_model, validate_request

WARM_FIRST_PCM_SECONDS = 2
WARM_RECEIPT_SECONDS = 600
WARM_RECEIPT_LIMIT = 128
RECOVERY_POLL_SECONDS = 1.0
RECOVERY_MAX_BACKOFF_SECONDS = 30.0


@dataclass
class ModelWarm:
    request_id: str
    model_id: str
    began: float
    began_unix_ms: float
    state: str = "preparing"
    accepted: bool = True
    reason: str | None = None
    error: str | None = None
    first_pcm_ms: float | None = None
    discarded_received_audio_ms: float = 0
    retirement_ms: float | None = None
    ended: float | None = None
    task: asyncio.Task | None = None
    iterator: object | None = None

    def public(self):
        return {"request_id": self.request_id, "model_id": self.model_id,
                "state": self.state, "accepted": self.accepted, "reason": self.reason, "error": self.error,
                "mode": "first_pcm_then_reset", "began_unix_ms": self.began_unix_ms,
                "first_pcm_ms": self.first_pcm_ms,
                "discarded_received_audio_ms": self.discarded_received_audio_ms,
                "retirement_ms": self.retirement_ms,
                "total_ms": (self.ended - self.began) * 1000 if self.ended is not None else None,
                "age_ms": max(0, time.monotonic() - self.ended) * 1000 if self.ended is not None else None,
                "room_audio_sent": False, "latency_guaranteed": False}


def read_worker_token(path: Path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        held = os.fstat(stream.fileno())
        named = path.stat(follow_symlinks=False)
        if (not stat.S_ISREG(held.st_mode) or held.st_mode & 0o077 or held.st_uid not in {0, os.getuid()}
                or (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino)):
            raise ValueError("The worker token must be a private, owned regular file (mode 0600)")
        data = stream.read(4097)
    if len(data) > 4096:
        raise ValueError("Worker credential file is too large")
    token = data.decode("ascii").strip()
    if len(token) < 32 or any(not 33 <= ord(char) <= 126 for char in token):
        raise ValueError("Use a worker token of at least 32 characters without whitespace")
    return token


def _child(connection, spec, cache_dir, allow_download, cancel_event=None):
    # Spawn avoids inheriting HTTP threads or a Metal context. Cancellation kills
    # this child, so an interrupted decoder can never contaminate its successor.
    try:
        from shiri.tts.backend import load_backend
        backend = load_backend(spec, cache_dir=cache_dir, allow_download=allow_download)
        warm = backend.prewarm()
        connection.send({"type": "ready", "warmup": warm})
        while True:
            payload = connection.recv()
            if payload is None:
                break
            started = time.monotonic()
            frames = 0
            try:
                iterator = backend.generate(payload)
                cancelled = False
                try:
                    for pcm in iterator:
                        if cancel_event is not None and cancel_event.is_set():
                            cancelled = True
                            break
                        if not isinstance(pcm, bytes) or len(pcm) % 2:
                            raise ValueError("Model returned invalid signed 16-bit PCM")
                        frames += len(pcm) // 2
                        if frames > 48_000 * MAX_GENERATED_SECONDS or time.monotonic() - started > 180:
                            raise ValueError("Speech exceeds the generation or audio duration limit")
                        for offset in range(0, len(pcm), 960 * 2):
                            if cancel_event is not None and cancel_event.is_set():
                                cancelled = True
                                break
                            connection.send({"type": "pcm", "pcm": pcm[offset:offset + 1920]})
                        if cancelled:
                            break
                finally:
                    iterator.close()  # Resampler discard and decoder reset must finish before ACK.
                connection.send({"type": "cancelled"} if cancelled else {"type": "end", "metrics": backend.last_metrics})
            except Exception as exc:
                connection.send({"type": "error", "error": f"{type(exc).__name__}: {str(exc)[:512]}"})
                break  # Never reuse a decoder after an uncertain failure.
    except Exception as exc:
        try:
            connection.send({"type": "error", "error": f"{type(exc).__name__}: {str(exc)[:512]}"})
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()


class ModelWorker:
    def __init__(self, *, registry_file=None, cache_dir=None, allow_download=False, state_file=None):
        self.registry_file = registry_file
        self.cache_dir = cache_dir
        self.allow_download = allow_download
        self.state_file = state_file
        self.selected_model = None
        self._supervisor_task = None
        self._closing = False
        self.recovery_attempts = 0
        self.process = None
        self.connection = None
        self.cancel_event = None
        self._receive_task = None
        self.load_task = None
        self.state = "stopped"
        self.model_id = None
        self.error = None
        self.warmup = None
        self.lock = asyncio.Lock()
        self.operation = None
        self.warm_receipts = OrderedDict()
        self._active_warm = None
        self._latest_warm = None
        self._warm_cancel_lock = asyncio.Lock()

    def status(self):
        self._observe_ready_process()
        self._prune_warm_receipts()
        receipt = self.warm_receipts.get(self._latest_warm)
        return {"state": self.state, "model_id": self.model_id, "busy": self.state == "busy",
                "error": self.error, "warmup": self.warmup, "operation": self.operation,
                "selected_model_id": self.selected_model,
                "automatic_recovery": bool(self._supervisor_task is not None
                                           and not self._supervisor_task.done() and not self._closing),
                "recovery_attempts": self.recovery_attempts,
                "model_warm": receipt.public() if receipt else None}

    def _saved_selection(self):
        if self.state_file is None:
            return None
        try:
            descriptor = os.open(self.state_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.getuid()}
                    or info.st_mode & 0o022 or info.st_size > 4096):
                raise ValueError("The saved model selection must be a bounded, owned regular file")
            data = stream.read(4097)
        if len(data) > 4096:
            raise ValueError("The saved model selection is too large")
        value = json.loads(data)
        if not isinstance(value, dict) or set(value) != {"version", "model_id"} or type(value["version"]) is not int or value["version"] != 1:
            raise ValueError("Invalid saved model selection")
        return get_model(value["model_id"], self.registry_file).id

    def _save_selection(self, model_id):
        if self.state_file is not None:
            from shiri.runtime.system import atomic_json
            atomic_json(self.state_file, {"version": 1, "model_id": model_id})

    async def _persist_selection(self, model_id):
        # Cancelling to_thread does not stop its filesystem write. Keep the
        # selection lock until that exact write ends, so it cannot overwrite a
        # later model choice after its cancelled caller has returned.
        writing = asyncio.create_task(asyncio.to_thread(self._save_selection, model_id))
        interrupted = False
        with CancelScope(shield=True):
            while not writing.done():
                try:
                    await asyncio.shield(writing)
                except asyncio.CancelledError:
                    interrupted = True
            writing.result()
            # Once the durable write commits, recovery must follow that choice
            # even if the caller no longer waits for its acknowledgement.
            self.selected_model = model_id
        if interrupted:
            raise asyncio.CancelledError

    async def start(self, preload_model=None):
        """Restore one resident model; recovery never runs periodic inference."""
        if self._supervisor_task is not None:
            return
        self.selected_model = await asyncio.to_thread(self._saved_selection) or preload_model
        if self.selected_model is not None:
            await self.load(self.selected_model, persist=False)
        self._supervisor_task = asyncio.create_task(self._supervise(), name="tts-model-recovery")

    async def _supervise(self):
        backoff = RECOVERY_POLL_SECONDS
        while not self._closing:
            await asyncio.sleep(backoff)
            self._observe_ready_process()
            if self.state == "ready":
                backoff = RECOVERY_POLL_SECONDS
            elif self.selected_model and self.state in {"failed", "stopped"} and not self.lock.locked():
                self.recovery_attempts += 1
                await self.load(self.selected_model, persist=False)
                await asyncio.shield(self.load_task)
                backoff = min(RECOVERY_MAX_BACKOFF_SECONDS, backoff * 2) if self.state != "ready" else RECOVERY_POLL_SECONDS

    def _observe_ready_process(self):
        # An idle child can exit without an HTTP stream left to observe EOF.
        # Keep its handles for _load/close to reap, but stop advertising stale
        # readiness or treating an explicit same-model reload as a no-op.
        if self.state == "ready" and (self.process is None or not self.process.is_alive()):
            self.state = "failed"
            self.error = ("The model worker exited; automatic recovery is pending"
                          if self._supervisor_task is not None else "The model worker exited; load the model again")

    def _prune_warm_receipts(self):
        now = time.monotonic()
        for identity, receipt in list(self.warm_receipts.items()):
            if receipt is not self._active_warm and receipt.ended is not None and now - receipt.ended >= WARM_RECEIPT_SECONDS:
                del self.warm_receipts[identity]

    @staticmethod
    def _warm_id(request_id):
        if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id):
            raise ValueError("Model warm request_id requires 32 lowercase hexadecimal characters")

    def warm_status(self, request_id):
        self._warm_id(request_id)
        self._prune_warm_receipts()
        receipt = self.warm_receipts.get(request_id)
        return receipt.public() if receipt else None

    async def warm(self, model_id, request_id):
        """One explicit, quiet hint. Never load a model or create a warm loop."""
        self._warm_id(request_id)
        spec = get_model(model_id, self.registry_file)
        request = validate_request(spec, GenerationRequest(text="Hi.", max_tokens=128))
        self._observe_ready_process()
        self._prune_warm_receipts()
        old = self.warm_receipts.get(request_id)
        if old is not None:
            if old.model_id != model_id:
                raise ValueError("Model warm request_id already belongs to a different model")
            return old.public()
        if len(self.warm_receipts) >= WARM_RECEIPT_LIMIT:
            # A receipt is an idempotency fence for its entire retention, even
            # when terminal. Eviction would silently re-run a retried hint.
            raise ValueError("Model warm receipt capacity reached; wait for retained requests to expire")
        receipt = ModelWarm(request_id, model_id, time.monotonic(), time.time() * 1000)
        self.warm_receipts[request_id] = receipt
        self._latest_warm = request_id
        if self.lock.locked() or self.state != "ready" or self.model_id != model_id:
            receipt.state, receipt.accepted = "skipped", False
            receipt.reason = "busy" if self.lock.locked() or self.state == "busy" else (
                "model_mismatch" if self.model_id != model_id else "model_not_ready")
            receipt.ended = time.monotonic()
            return receipt.public()
        await self.lock.acquire()
        self.state, self.operation = "busy", "warming"
        iterator = self._events(request)
        receipt.iterator = iterator
        # Enter the iterator's cleanup owner before scheduling: cancelling a
        # task before its first turn otherwise never runs that task's finally.
        first = await iterator.__anext__()
        self._active_warm = receipt
        receipt.task = asyncio.create_task(self._run_warm(receipt, first))
        return receipt.public()

    async def _run_warm(self, receipt, first):
        iterator = receipt.iterator
        outcome = "failed"
        try:
            if first.get("type") != "format":
                raise ValueError("Model priming did not establish its PCM format")
            with fail_after(WARM_FIRST_PCM_SECONDS):
                event = await iterator.__anext__()
                if event.get("type") != "pcm":
                    raise ValueError("Model priming did not produce a first PCM chunk")
                pcm = event["pcm"]
                if not pcm or len(pcm) % 2 or len(pcm) > 1920:
                    raise ValueError("Model priming returned invalid PCM")
                receipt.first_pcm_ms = (time.monotonic() - receipt.began) * 1000
                receipt.discarded_received_audio_ms = len(pcm) / 96
            outcome = "completed"
        except asyncio.CancelledError:
            outcome = "cancelled"
            receipt.reason = receipt.reason or "cancelled"
            raise
        except TimeoutError:
            receipt.reason = "first_pcm_timeout"
            receipt.error = "Model priming first PCM deadline exceeded"
        except Exception as exc:
            receipt.error = str(exc)[:512]
        finally:
            retirement = time.monotonic()
            with CancelScope(shield=True):
                try:
                    # Raw Task.cancel() may arrive while reset is already in
                    # progress. Shield the same close owner and still join it;
                    # otherwise preemption turns a healthy reset into a kill.
                    cleanup = asyncio.create_task(iterator.aclose())
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        await asyncio.shield(cleanup)
                        raise
                finally:
                    receipt.retirement_ms = (time.monotonic() - retirement) * 1000
                    receipt.ended = time.monotonic()
                    receipt.iterator = receipt.task = None
                    if self.state != "ready" and outcome == "completed":
                        outcome = "failed"
                        receipt.error = "Model priming could not confirm a reusable decoder"
                    # PCM arrival alone never claims completed readiness. The
                    # reusable reset ACK/retirement is the terminal boundary.
                    receipt.state = outcome
                    if self._active_warm is receipt:
                        self._active_warm = None

    async def cancel_warm(self, request_id, *, reason="cancelled"):
        """Retire only this warm owner; stale cancellation cannot stop speech."""
        self._warm_id(request_id)
        async with self._warm_cancel_lock:
            receipt = self.warm_receipts.get(request_id)
            if receipt is None or receipt is not self._active_warm:
                return receipt.public() if receipt else None
            receipt.reason = reason
            task, iterator = receipt.task, receipt.iterator
            with CancelScope(shield=True):
                if task is not None:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                # Also covers cancellation before the task's first turn.
                if iterator is not None:
                    await iterator.aclose()
                if receipt.ended is None:
                    receipt.state = "cancelled"
                    receipt.ended = time.monotonic()
                receipt.iterator = receipt.task = None
                if self._active_warm is receipt:
                    self._active_warm = None
            return receipt.public()

    async def _receive(self, wait_seconds):
        connection = self.connection
        def receive():
            if connection is None or not connection.poll(wait_seconds):
                raise TimeoutError("Model worker deadline exceeded")
            return connection.recv()
        if self._receive_task is None:
            self._receive_task = asyncio.create_task(asyncio.to_thread(receive))
        task = self._receive_task
        try:
            message = await asyncio.shield(task)
        except asyncio.CancelledError:
            # Retain the sole reader, including a result racing cancellation.
            # The cancellation drain consumes it before starting another read.
            raise
        except Exception:
            if self._receive_task is task:
                self._receive_task = None
            raise
        else:
            if self._receive_task is task:
                self._receive_task = None
            return message

    async def _terminate(self):
        process, connection = self.process, self.connection
        self.process = self.connection = None
        self.cancel_event = None
        if process is not None:
            def stop():
                if process.is_alive():
                    process.terminate()
                process.join(2)
                if process.is_alive():
                    process.kill()
                    process.join(2)
                process.close()
            await asyncio.to_thread(stop)
        if connection is not None:
            connection.close()
        if self._receive_task is not None:
            await asyncio.gather(self._receive_task, return_exceptions=True)
            self._receive_task = None

    async def _cancel_generation(self):
        if self.cancel_event is None or self.connection is None:
            return False
        self.cancel_event.set()
        deadline = time.monotonic() + 2
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                message = await asyncio.wait_for(self._receive(remaining), remaining)
                if message.get("type") == "cancelled":
                    self.cancel_event.clear()
                    return True
                if message.get("type") == "end":
                    json.dumps(message, allow_nan=False)
                    self.cancel_event.clear()
                    return True
                if message.get("type") != "pcm":
                    return False
        except (Exception, asyncio.CancelledError):
            return False

    async def load(self, model_id, *, persist=True):
        spec = get_model(model_id, self.registry_file)
        self._observe_ready_process()
        if self._closing or self.lock.locked() or self.state == "loading":
            raise ValueError("A model operation is already active")
        if self.state == "ready" and self.model_id == model_id:
            if persist:
                async with self.lock:
                    await self._persist_selection(model_id)
                    self.selected_model = model_id
            return
        self.state, self.model_id, self.error = "loading", model_id, None
        self.load_task = asyncio.create_task(self._load(spec, persist=persist))

    async def _load(self, spec, *, persist=True):
        async with self.lock:
            try:
                await self._terminate()
                context = multiprocessing.get_context("spawn")
                parent, child = context.Pipe()
                self.cancel_event = context.Event()
                self.connection = parent
                self.process = context.Process(target=_child, args=(child, spec, self.cache_dir, self.allow_download, self.cancel_event),
                                               daemon=True)
                self.process.start()
                child.close()
                message = await self._receive(120)
                if message.get("type") != "ready":
                    raise RuntimeError(message.get("error", "Model did not become ready"))
                if persist and self.state_file is not None:
                    await self._persist_selection(spec.id)
                self.selected_model = spec.id
                self.warmup = message.get("warmup")
                self.state = "ready"
            except asyncio.CancelledError:
                await self._terminate()
                self.state = "stopped"
                raise
            except Exception as exc:
                await self._terminate()
                self.error = str(exc)[:512]
                self.state = "failed"

    async def reserve(self, payload):
        model_id = payload.get("model_id")
        spec = get_model(model_id, self.registry_file)
        request = validate_request(spec, {key: value for key, value in payload.items() if key != "model_id"})
        self._observe_ready_process()
        if self._supervisor_task is not None and self.selected_model == model_id:
            try:
                with fail_after(125):
                    while not self._closing and self.state in {"stopped", "failed", "loading"}:
                        if self.load_task is not None and not self.load_task.done():
                            await asyncio.shield(self.load_task)
                        else:
                            await asyncio.sleep(.05)
            except TimeoutError as exc:
                raise ValueError("The selected speech model did not recover within its loading budget") from exc
        # Validate first: malformed or wrong-model requests cannot displace a
        # useful prime. A real utterance joins that exact owner's reset before
        # acquiring the one reader/decoder, and never cancels another utterance.
        if self._active_warm is not None and self.model_id == model_id:
            await self.cancel_warm(self._active_warm.request_id, reason="speech_priority")
        if self.lock.locked() or self.state != "ready" or self.model_id != model_id:
            raise ValueError("Load and warm the selected model before generating; one generation is allowed at a time")
        await self.lock.acquire()
        self.state, self.operation = "busy", "speech"
        return request

    async def stream(self, request):
        """HTTP encoding is separate from the one native generation owner."""
        events = self._events(request)
        try:
            async for event in events:
                if event["type"] == "pcm":
                    event = {"type": "pcm", "pcm_base64": base64.b64encode(event["pcm"]).decode("ascii")}
                yield json.dumps(event, allow_nan=False) + "\n"
        finally:
            with CancelScope(shield=True):
                await events.aclose()

    async def _events(self, request):
        completed = False
        failed = False
        try:
            self.connection.send(request)
            deadline = time.monotonic() + 180
            yield {"type": "format", "format": "s16le", "sample_rate": 48000, "channels": 1}
            while True:
                message = await self._receive(max(0, deadline - time.monotonic()))
                kind = message.get("type")
                if kind == "pcm":
                    pcm = message["pcm"]
                    if not isinstance(pcm, bytes) or not pcm or len(pcm) % 2 or len(pcm) > 1920:
                        raise ValueError("Invalid native model PCM frame")
                elif kind == "end":
                    json.dumps(message, allow_nan=False)
                elif kind == "error":
                    failed = True
                    self.error = message.get("error")
                else:
                    raise ValueError("Unexpected model worker message")
                if kind == "end":
                    completed = True
                yield message
                if kind in {"end", "error"}:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failed = True
            self.error = str(exc)[:512]
            yield {"type": "error", "error": self.error}
        finally:
            # Starlette disconnect cancellation can be level-triggered. Keep
            # cleanup shielded and bounded while retaining the producer lock.
            with CancelScope(shield=True):
                if completed or not failed and await self._cancel_generation():
                    self.state = "ready"
                else:
                    await self._terminate()
                    self.state = "stopped" if not self.error else "failed"
                self.operation = None
                self.lock.release()

    async def close(self):
        self._closing = True
        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
            await asyncio.gather(self._supervisor_task, return_exceptions=True)
            self._supervisor_task = None
        if self._active_warm is not None:
            await self.cancel_warm(self._active_warm.request_id, reason="shutdown")
        if self.load_task is not None and not self.load_task.done():
            self.load_task.cancel()
            await asyncio.gather(self.load_task, return_exceptions=True)
        await self._terminate()
        self.state = "stopped"


def create_worker_app(*, token: str, registry_file: Path | None = None, cache_dir: Path | None = None,
                      allow_download=False, preload_model: str | None = None, state_file: Path | None = None, worker=None):
    if len(token) < 32:
        raise ValueError("Use a private worker token with at least 32 characters")
    worker = worker or ModelWorker(registry_file=registry_file, cache_dir=cache_dir,
                                   allow_download=allow_download, state_file=state_file)

    @asynccontextmanager
    async def lifespan(app):
        await worker.start(preload_model)
        try:
            yield
        finally:
            await worker.close()

    app = FastAPI(title="Shiri TTS worker", docs_url=None, openapi_url=None, lifespan=lifespan)
    app.state.worker = worker

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if not hmac.compare_digest(request.headers.get("authorization", "").encode(), ("Bearer " + token).encode()):
            return JSONResponse({"error": "Worker credential required"}, status_code=401)
        if request.method == "POST":
            data = bytearray()
            try:
                async def read_body():
                    async for chunk in request.stream():
                        data.extend(chunk)
                        if len(data) > 16_384:
                            raise ValueError("Request exceeds 16 KiB")
                await asyncio.wait_for(read_body(), 5)
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=413)
            except asyncio.TimeoutError:
                return JSONResponse({"error": "Request deadline exceeded"}, status_code=408)
            request._body = bytes(data)
        return await call_next(request)

    @app.get("/v1/models")
    async def models():
        return {"worker": worker.status(), "default_model_id": DEFAULT_MODEL_ID,
                "models": [spec.to_dict() for spec in get_models(registry_file)]}

    @app.post("/v1/load", status_code=202)
    async def load(request: Request):
        try:
            payload = await request.json()
            if set(payload) != {"model_id"}:
                raise ValueError("Provide only model_id")
            await worker.load(payload["model_id"])
            return worker.status()
        except (ValueError, KeyError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @app.post("/v1/warm", status_code=202)
    async def warm(request: Request):
        try:
            payload = await request.json()
            if not isinstance(payload, dict) or set(payload) != {"model_id", "request_id"}:
                raise ValueError("Provide only model_id and request_id for a quiet model hint")
            result = await worker.warm(payload["model_id"], payload["request_id"])
            return {"warm": result, "worker": worker.status()}
        except (ValueError, KeyError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @app.get("/v1/warm/{request_id}")
    async def warm_status(request_id: str):
        try:
            result = worker.warm_status(request_id)
            if result is None:
                return JSONResponse({"error": "Model warm request was not found or expired"}, status_code=404)
            return {"warm": result, "worker": worker.status()}
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @app.post("/v1/warm/cancel")
    async def cancel_warm(request: Request):
        try:
            payload = await request.json()
            if not isinstance(payload, dict) or set(payload) != {"request_id"}:
                raise ValueError("Provide only the owned model warm request_id")
            result = await worker.cancel_warm(payload["request_id"])
            if result is None:
                return JSONResponse({"error": "Model warm request was not found or expired"}, status_code=404)
            return {"warm": result, "worker": worker.status()}
        except (ValueError, KeyError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @app.post("/v1/generate")
    async def generate(request: Request):
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("Speech generation requires a JSON object")
            prepared = await worker.reserve(payload)
        except (ValueError, KeyError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        iterator = worker.stream(prepared)
        # Prime the owned iterator before returning a response. Its finally
        # block then exists even if the socket disconnects before body iteration.
        first = await iterator.__anext__()
        async def body():
            yield first
            async for line in iterator:
                yield line
        class OwnedResponse(StreamingResponse):
            async def __call__(self, scope, receive, send):
                try:
                    await super().__call__(scope, receive, send)
                finally:
                    await asyncio.shield(iterator.aclose())
        return OwnedResponse(body(), media_type="application/x-ndjson")

    return app
