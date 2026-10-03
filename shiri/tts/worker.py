"""Optional Mac generation service; isolated, persistent and single-flight."""
from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
import hmac
import json
import multiprocessing
import os
from pathlib import Path
import stat
import time

from anyio import CancelScope
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from shiri.tts.models import DEFAULT_MODEL_ID, MAX_GENERATED_SECONDS, get_models, get_model, validate_request


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
    def __init__(self, *, registry_file=None, cache_dir=None, allow_download=False):
        self.registry_file = registry_file
        self.cache_dir = cache_dir
        self.allow_download = allow_download
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

    def status(self):
        return {"state": self.state, "model_id": self.model_id, "busy": self.state == "busy",
                "error": self.error, "warmup": self.warmup}

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

    async def load(self, model_id):
        spec = get_model(model_id, self.registry_file)
        if self.lock.locked() or self.state == "loading":
            raise ValueError("A model operation is already active")
        if self.state == "ready" and self.model_id == model_id:
            return
        self.state, self.model_id, self.error = "loading", model_id, None
        self.load_task = asyncio.create_task(self._load(spec))

    async def _load(self, spec):
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
        if self.lock.locked() or self.state != "ready" or self.model_id != model_id:
            raise ValueError("Load and warm the selected model before generating; one generation is allowed at a time")
        await self.lock.acquire()
        self.state = "busy"
        return request

    async def stream(self, request):
        completed = False
        failed = False
        try:
            self.connection.send(request)
            deadline = time.monotonic() + 180
            yield json.dumps({"type": "format", "format": "s16le", "sample_rate": 48000, "channels": 1}) + "\n"
            while True:
                message = await self._receive(max(0, deadline - time.monotonic()))
                kind = message.get("type")
                if kind == "pcm":
                    message = {"type": "pcm", "pcm_base64": base64.b64encode(message["pcm"]).decode("ascii")}
                elif kind == "end":
                    pass
                elif kind == "error":
                    failed = True
                    self.error = message.get("error")
                else:
                    raise ValueError("Unexpected model worker message")
                encoded = json.dumps(message, allow_nan=False) + "\n"
                if kind == "end":
                    completed = True
                yield encoded
                if kind in {"end", "error"}:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failed = True
            self.error = str(exc)[:512]
            yield json.dumps({"type": "error", "error": self.error}) + "\n"
        finally:
            # Starlette disconnect cancellation can be level-triggered. Keep
            # cleanup shielded and bounded while retaining the producer lock.
            with CancelScope(shield=True):
                if completed or not failed and await self._cancel_generation():
                    self.state = "ready"
                else:
                    await self._terminate()
                    self.state = "stopped" if not self.error else "failed"
                self.lock.release()

    async def close(self):
        if self.load_task is not None and not self.load_task.done():
            self.load_task.cancel()
            await asyncio.gather(self.load_task, return_exceptions=True)
        await self._terminate()
        self.state = "stopped"


def create_worker_app(*, token: str, registry_file: Path | None = None, cache_dir: Path | None = None,
                      allow_download=False, preload_model: str | None = None, worker=None):
    if len(token) < 32:
        raise ValueError("Use a private worker token with at least 32 characters")
    worker = worker or ModelWorker(registry_file=registry_file, cache_dir=cache_dir, allow_download=allow_download)

    @asynccontextmanager
    async def lifespan(app):
        if preload_model:
            await worker.load(preload_model)
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
