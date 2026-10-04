"""Private HTTP and decoder lifecycle checks use fake IPC and real spawned peers."""
import asyncio
import base64
import json
import multiprocessing
import os
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from starlette.requests import ClientDisconnect, Request

from shiri.tts.models import DEFAULT_MODEL_ID, QWEN_MODEL_ID, GenerationRequest, get_model
from shiri.tts.worker import ModelWorker, _child, create_worker_app, read_worker_token


TOKEN = "private-generation-worker-token-12345678901234567890"
PCM = b"\x12\x00" * 960


def gated_http_child(connection, cancel_event, eos_gate):
    """Use the production child loop, with EOS controlled by the HTTP observer."""
    import shiri.tts.backend as backend_module

    class Backend:
        last_metrics = {"synthetic": True, "complete": False}

        def prewarm(self):
            return {"synthetic": True}

        def generate(self, _request):
            yield PCM * 2
            if not eos_gate.wait(5):
                raise TimeoutError("HTTP test never released EOS")
            yield b"\x45\x00" * 480
            self.last_metrics = {"synthetic": True, "complete": True}

    backend_module.load_backend = lambda *args, **kwargs: Backend()
    _child(connection, get_model(DEFAULT_MODEL_ID), None, False, cancel_event)


async def test_real_http_delivers_incremental_child_pcm_before_eos_is_released():
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    cancellation, eos_gate = context.Event(), context.Event()
    process = context.Process(target=gated_http_child, args=(child, cancellation, eos_gate), daemon=True)
    process.start()
    child.close()
    worker = ModelWorker()
    worker.process, worker.connection, worker.cancel_event = process, parent, cancellation
    server_task = listener = server = None
    try:
        ready = await worker._receive(5)
        assert ready["type"] == "ready"
        worker.state, worker.model_id, worker.warmup = "ready", DEFAULT_MODEL_ID, ready["warmup"]
        app = create_worker_app(token=TOKEN, worker=worker)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        listener.setblocking(False)
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        listening = asyncio.Event()

        class ObservedServer(uvicorn.Server):
            async def startup(self, sockets=None):
                await super().startup(sockets=sockets)
                listening.set()

        server = ObservedServer(uvicorn.Config(app, log_level="error", lifespan="off"))
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        await asyncio.wait_for(listening.wait(), 5)
        received, kinds = bytearray(), []
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + TOKEN}, trust_env=False,
                                     timeout=2) as client:
            async with client.stream("POST", url + "/v1/generate", json=payload()) as response:
                assert response.status_code == 200
                async for line in response.aiter_lines():
                    event = json.loads(line)
                    kinds.append(event["type"])
                    if event["type"] == "pcm":
                        block = base64.b64decode(event["pcm_base64"], validate=True)
                        assert 0 < len(block) <= 1920
                        received.extend(block)
                        if len(received) <= len(PCM * 2):
                            assert not eos_gate.is_set() and worker.state == "busy"
                            assert bytes(received) == (PCM * 2)[:len(received)]
                            if len(received) == len(PCM * 2):
                                eos_gate.set()
                    if event["type"] == "end":
                        assert eos_gate.is_set() and event["metrics"]["complete"]
                assert kinds == ["format", "pcm", "pcm", "pcm", "end"]
                assert bytes(received) == PCM * 2 + b"\x45\x00" * 480
    finally:
        eos_gate.set()
        if server is not None:
            server.should_exit = True
        if server_task is not None:
            await asyncio.wait_for(server_task, 3)
        if listener is not None:
            listener.close()
        await worker.close()


class Connection:
    def __init__(self, messages=()):
        self.messages, self.sent, self.closed = list(messages), [], False

    def poll(self, timeout):
        assert timeout >= 0
        return bool(self.messages)

    def recv(self):
        return self.messages.pop(0)

    def send(self, message):
        self.sent.append(message)

    def close(self):
        self.closed = True


class Process:
    def __init__(self):
        self.alive, self.closed, self.operations = True, False, []

    def start(self):
        self.operations.append("start")

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.operations.append("terminate")
        self.alive = False

    def kill(self):
        self.operations.append("kill")
        self.alive = False

    def join(self, timeout):
        self.operations.append(("join", timeout))

    def close(self):
        self.closed = True


def ready_worker(messages=None):
    worker = ModelWorker()
    worker.model_id, worker.state = DEFAULT_MODEL_ID, "ready"
    worker.connection = Connection(messages if messages is not None else [
        {"type": "pcm", "pcm": PCM}, {"type": "end", "metrics": {"first_pcm_ms": 10}}])
    worker.process = Process()
    return worker


def payload(**changes):
    return {"model_id": DEFAULT_MODEL_ID, "text": "The house is ready.", **changes}


async def test_authenticated_http_stream_uses_validated_ipc_and_retains_warm_decoder():
    worker = ready_worker()
    original_connection, original_process = worker.connection, worker.process
    app = create_worker_app(token=TOKEN, worker=worker)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker.test") as client:
            assert (await client.get("/v1/models")).status_code == 401
            assert (await client.post("/v1/generate", json=payload())).status_code == 401
            client.headers["Authorization"] = "Bearer " + TOKEN
            catalog = await client.get("/v1/models")
            assert catalog.status_code == 200 and catalog.json()["worker"]["state"] == "ready"
            assert DEFAULT_MODEL_ID in {model["id"] for model in catalog.json()["models"]}
            generated = await client.post("/v1/generate", json=payload())
            assert generated.status_code == 200
            assert generated.headers["content-type"].startswith("application/x-ndjson")
            records = [json.loads(line) for line in generated.text.splitlines()]
            assert records[0] == {"type": "format", "format": "s16le", "sample_rate": 48000, "channels": 1}
            assert base64.b64decode(records[1]["pcm_base64"]) == PCM
            assert records[-1]["type"] == "end"
            assert len(original_connection.sent) == 1 and isinstance(original_connection.sent[0], GenerationRequest)
            assert original_connection.sent[0].text == payload()["text"]
        assert worker.state == "ready" and not worker.lock.locked()
        assert worker.connection is original_connection and worker.process is original_process
        assert not original_connection.closed and original_process.operations == []
    finally:
        await worker.close()


@pytest.mark.parametrize("authorization", [b"Bearer incorrect", b"Bearer \xe9", b"", b"bearer " + TOKEN.encode()])
async def test_malformed_worker_credential_returns_unauthorized_without_decoder_access(authorization):
    worker = ready_worker()
    app = create_worker_app(token=TOKEN, worker=worker)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                     base_url="http://worker.test") as client:
            response = await client.get("/v1/models", headers=[(b"authorization", authorization)])
            assert response.status_code == 401
        assert worker.state == "ready" and worker.connection.sent == []
    finally:
        await worker.close()


@pytest.mark.parametrize("value", [[], None, "hello", 5, {"model_id": "unknown", "text": "hi"},
                                 payload(voice="not-a-voice"), payload(speed=3), payload(untrusted_repo="https://evil.test"),
                                 payload(speed=10**400), payload(streaming_interval=10**400),
                                 payload(model_id=QWEN_MODEL_ID, temperature=10**400)])
async def test_bad_generation_input_is_refused_before_ipc_or_reservation(value):
    worker = ready_worker()
    app = create_worker_app(token=TOKEN, worker=worker)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                     base_url="http://worker.test", headers={"Authorization": "Bearer " + TOKEN}) as client:
            response = await client.post("/v1/generate", content=json.dumps(value), headers={"Content-Type": "application/json"})
            assert response.status_code == 409
        assert worker.state == "ready" and not worker.lock.locked() and worker.connection.sent == []
    finally:
        await worker.close()


async def test_private_body_limit_and_busy_policy_do_not_displace_active_decoder():
    worker = ready_worker()
    app = create_worker_app(token=TOKEN, worker=worker)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker.test",
                                     headers={"Authorization": "Bearer " + TOKEN}) as client:
            oversized = await client.post("/v1/generate", content=b"x" * 16385)
            assert oversized.status_code == 413 and worker.connection.sent == []
            request = await worker.reserve(payload())
            assert worker.state == "busy" and worker.lock.locked()
            assert (await client.post("/v1/generate", json=payload())).status_code == 409
            assert (await client.post("/v1/load", json={"model_id": DEFAULT_MODEL_ID})).status_code == 409
            records = [json.loads(line) async for line in worker.stream(request)]
            assert records[-1]["type"] == "end" and worker.state == "ready"
    finally:
        await worker.close()


async def test_qwen_voice_and_language_are_explicit_when_default_model_is_kokoro():
    worker = ready_worker()
    worker.model_id = QWEN_MODEL_ID
    try:
        prepared = await worker.reserve({"model_id": QWEN_MODEL_ID, "text": "hello", "voice": "ryan", "language": "English"})
        assert prepared.voice == "ryan" and prepared.language == "English"
        records = [json.loads(line) async for line in worker.stream(prepared)]
        assert records[-1]["type"] == "end"
        with pytest.raises(ValueError, match="speaking-rate"):
            await worker.reserve({"model_id": QWEN_MODEL_ID, "text": "hello", "speed": 1.2})
        assert worker.state == "ready" and not worker.lock.locked()
    finally:
        await worker.close()


@pytest.mark.parametrize("message", [{"type": "error", "error": "decoder lost"},
                                    {"type": "unknown"}, {"type": "end", "metrics": {"rtf": float("nan")}}])
async def test_uncertain_generation_failure_discards_child_before_reuse(message):
    worker = ready_worker([message])
    connection, process = worker.connection, worker.process
    try:
        request = await worker.reserve(payload())
        records = [json.loads(line) async for line in worker.stream(request)]
        assert records[-1]["type"] == "error"
        assert worker.state == "failed" and not worker.lock.locked()
        assert worker.process is None and worker.connection is None
        assert connection.closed and process.closed and "terminate" in process.operations
    finally:
        await worker.close()


async def test_cancellation_without_cooperative_channel_terminates_exact_child_and_releases_reservation(monkeypatch):
    worker = ready_worker()
    connection, process = worker.connection, worker.process
    receiving = asyncio.Event()

    async def blocked_receive(timeout):
        receiving.set()
        await asyncio.Future()

    monkeypatch.setattr(worker, "_receive", blocked_receive)
    request = await worker.reserve(payload())

    async def consume():
        return [line async for line in worker.stream(request)]

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(receiving.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert worker.state == "stopped" and not worker.lock.locked()
        assert worker.process is None and worker.connection is None
        assert connection.closed and process.closed
        assert connection.sent == [request]
    finally:
        await worker.close()


@pytest.mark.parametrize("cooperative", [False, True])
async def test_http_disconnect_before_first_response_body_releases_primed_reservation(cooperative):
    worker = ready_worker()
    if cooperative:
        worker.cancel_event = threading.Event()
    connection, process = worker.connection, worker.process
    app = create_worker_app(token=TOKEN, worker=worker)
    encoded = json.dumps(payload()).encode()

    async def receive():
        return {"type": "http.request", "body": encoded, "more_body": False}

    async def send(message):
        assert message["type"] == "http.response.start"
        raise OSError("peer disconnected before accepting any audio")

    scope = {"type": "http", "method": "POST", "path": "/v1/generate", "headers": [],
             "asgi": {"version": "3.0", "spec_version": "2.4"}}
    route = next(route for route in app.routes if route.path == "/v1/generate")
    try:
        response = await route.endpoint(Request(scope, receive))
        assert worker.lock.locked() and worker.state == "busy"
        with pytest.raises(ClientDisconnect):
            await response(scope, receive, send)
        assert not worker.lock.locked()
        if cooperative:
            assert worker.state == "ready" and worker.connection is connection and worker.process is process
            assert not connection.closed and not process.closed and connection.messages == []
        else:
            assert worker.state == "stopped" and worker.connection is None and worker.process is None
            assert connection.closed and process.closed
    finally:
        await worker.close()


@pytest.mark.parametrize("ready", [True, False])
async def test_load_uses_spawn_and_admits_only_explicit_ready_response(monkeypatch, ready):
    import shiri.tts.worker as module
    parent = Connection([{"type": "ready", "warmup": {"first_pcm_ms": 23}}] if ready
                        else [{"type": "error", "error": "missing checkpoint"}])
    child, process, targets = Connection(), Process(), []

    class Context:
        def Pipe(self):
            return parent, child

        def Event(self):
            return threading.Event()

        def Process(self, *, target, args, daemon):
            targets.append((target, args, daemon))
            return process

    methods = []

    def context(method):
        methods.append(method)
        return Context()

    monkeypatch.setattr(module.multiprocessing, "get_context", context)
    worker = ModelWorker(cache_dir="private-test-cache", allow_download=False)
    try:
        await worker.load(DEFAULT_MODEL_ID)
        assert worker.state == "loading"
        await asyncio.wait_for(worker.load_task, 1)
        assert methods == ["spawn"] and len(targets) == 1 and targets[0][0] is module._child
        assert targets[0][1][0] is child and targets[0][1][1].id == DEFAULT_MODEL_ID
        assert targets[0][1][2:4] == ("private-test-cache", False) and targets[0][2] is True
        assert isinstance(targets[0][1][4], threading.Event)
        assert child.closed and not worker.lock.locked()
        if ready:
            assert targets[0][1][4] is worker.cancel_event
            assert worker.state == "ready" and worker.warmup == {"first_pcm_ms": 23}
            await worker.load(DEFAULT_MODEL_ID)
            assert len(targets) == 1
        else:
            assert worker.state == "failed" and "missing checkpoint" in worker.error
            assert parent.closed and process.closed and worker.connection is None
    finally:
        await worker.close()


def test_worker_credentials_are_required_independently_of_public_api_access_mode():
    with pytest.raises(ValueError, match="private worker token"):
        create_worker_app(token="short")


def test_worker_token_reader_accepts_only_private_regular_credential_file(tmp_path):
    path = tmp_path / "worker-token"
    original = (TOKEN + "\n").encode()
    path.write_bytes(original)
    path.chmod(0o600)
    assert read_worker_token(path) == TOKEN
    assert path.read_bytes() == original and path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory", "oversize", "public", "short", "whitespace"])
def test_worker_token_reader_refuses_unsupported_or_unbounded_credentials(tmp_path, kind):
    path = tmp_path / "worker-token"
    if kind == "symlink":
        target = tmp_path / "target"
        target.write_text(TOKEN)
        target.chmod(0o600)
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    elif kind == "directory":
        path.mkdir(mode=0o700)
    else:
        content = {"oversize": "x" * 4097, "public": TOKEN, "short": "short",
                   "whitespace": TOKEN + " embedded"}[kind]
        path.write_text(content)
        path.chmod(0o644 if kind == "public" else 0o600)
    with pytest.raises((ValueError, OSError)):
        read_worker_token(path)


def test_model_child_prewarm_is_quiet_and_large_generated_chunks_are_bounded_at_ipc(monkeypatch):
    import shiri.tts.backend as backend_module
    text = GenerationRequest(text="hello")
    connection, calls = Connection([text, None]), []
    generated = PCM + PCM + b"\x33\x00" * 7

    class Backend:
        last_metrics = {"first_pcm_ms": 17}

        def prewarm(self):
            calls.append("prewarm")
            return {"first_pcm_ms": 20}

        def generate(self, request):
            calls.append(request)
            yield generated

    def load(spec, *, cache_dir, allow_download):
        calls.append((spec.id, cache_dir, allow_download))
        return Backend()

    monkeypatch.setattr(backend_module, "load_backend", load)
    _child(connection, get_model(DEFAULT_MODEL_ID), "private-cache", False)
    assert calls == [(DEFAULT_MODEL_ID, "private-cache", False), "prewarm", text]
    assert connection.sent[0] == {"type": "ready", "warmup": {"first_pcm_ms": 20}}
    frames = [message["pcm"] for message in connection.sent if message["type"] == "pcm"]
    assert [len(frame) for frame in frames] == [1920, 1920, 14]
    assert b"".join(frames) == generated
    assert connection.sent[-1] == {"type": "end", "metrics": Backend.last_metrics}
    assert connection.closed


@pytest.mark.parametrize("invalid_pcm", [b"x", "wrong format"])
def test_child_decoder_error_refuses_reuse_and_preserves_failure_record(monkeypatch, invalid_pcm):
    import shiri.tts.backend as backend_module
    first, successor = GenerationRequest(text="first"), GenerationRequest(text="successor")
    connection, requests = Connection([first, successor]), []

    class Backend:
        def prewarm(self):
            return {}

        def generate(self, request):
            requests.append(request)
            yield invalid_pcm

    monkeypatch.setattr(backend_module, "load_backend", lambda *args, **kwargs: Backend())
    _child(connection, get_model(DEFAULT_MODEL_ID), None, False)
    assert requests == [first] and connection.messages == [successor]
    assert [message["type"] for message in connection.sent] == ["ready", "error"]
    assert "invalid signed 16-bit PCM" in connection.sent[-1]["error"]
    assert connection.closed


@pytest.mark.parametrize("acknowledgment", [{"type": "cancelled"}, {"type": "end", "metrics": {"first_pcm_ms": 10}}])
async def test_cooperative_cancel_drains_old_pcm_and_retains_one_warm_decoder(acknowledgment):
    worker = ready_worker([{"type": "pcm", "pcm": PCM}, {"type": "pcm", "pcm": PCM}, acknowledgment])
    worker.cancel_event = threading.Event()
    connection, process = worker.connection, worker.process
    iterator = worker.stream(await worker.reserve(payload()))
    try:
        assert json.loads(await iterator.__anext__())["type"] == "format"
        await asyncio.wait_for(iterator.aclose(), 1)
        assert not worker.cancel_event.is_set()
        assert worker.state == "ready" and not worker.lock.locked()
        assert worker.connection is connection and worker.process is process
        assert not connection.closed and process.operations == [] and connection.messages == []
        successor_pcm = b"\x45\x00" * 480
        connection.messages.extend([{"type": "pcm", "pcm": successor_pcm}, {"type": "end", "metrics": {}}])
        successor = await worker.reserve(payload(text="successor"))
        assert not worker.cancel_event.is_set()
        records = [json.loads(line) async for line in worker.stream(successor)]
        assert base64.b64decode(records[1]["pcm_base64"]) == successor_pcm
        assert len(records) == 3 and records[-1]["type"] == "end"
        assert len(connection.sent) == 2 and worker.process is process and worker.state == "ready"
    finally:
        await iterator.aclose()
        await worker.close()


@pytest.mark.parametrize("acknowledgment", [{"type": "error", "error": "decoder reset failed"},
                                          {"type": "unknown"}, {"type": "end", "metrics": {"rtf": float("nan")}}, None])
async def test_missing_or_uncertain_cooperative_cancel_ack_discards_decoder(acknowledgment):
    worker = ready_worker([acknowledgment] if acknowledgment is not None else [])
    worker.cancel_event = threading.Event()
    connection, process = worker.connection, worker.process
    iterator = worker.stream(await worker.reserve(payload()))
    try:
        await iterator.__anext__()
        await asyncio.wait_for(iterator.aclose(), 2.5)
        assert worker.state != "ready" and not worker.lock.locked()
        assert worker.connection is None and worker.process is None
        assert connection.closed and process.closed and "terminate" in process.operations
    finally:
        await iterator.aclose()
        await worker.close()


async def test_cooperative_cancel_keeps_single_flight_until_old_queue_is_retired(monkeypatch):
    worker = ready_worker()
    worker.cancel_event = threading.Event()
    entered, release = asyncio.Event(), asyncio.Event()

    async def gated_ack(timeout):
        entered.set()
        await release.wait()
        return {"type": "cancelled"}

    monkeypatch.setattr(worker, "_receive", gated_ack)
    iterator = worker.stream(await worker.reserve(payload()))
    try:
        await iterator.__anext__()
        cancelling = asyncio.create_task(iterator.aclose())
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(ValueError, match="one generation"):
            await worker.reserve(payload(text="too soon"))
        assert worker.lock.locked() and worker.state == "busy"
        release.set()
        await asyncio.wait_for(cancelling, 1)
        assert worker.state == "ready" and not worker.lock.locked()
    finally:
        release.set()
        await iterator.aclose()
        await worker.close()


def cooperative_decoder(connection, cancel_event, mode):
    """Real spawned IPC peer with no model imports, clocks or playback devices."""
    try:
        connection.send({"type": "ready", "warmup": {"loads": 1}})
        while True:
            request = connection.recv()
            if request is None:
                return
            if request.text == "successor":
                if cancel_event.is_set():
                    connection.send({"type": "error", "error": "stale cancellation leaked into successor"})
                    return
                connection.send({"type": "pcm", "pcm": b"\x52\x00" * 480})
                connection.send({"type": "end", "metrics": {"generation_ms": 1}})
                continue
            if mode not in {"before-first", "hung-before-first"}:
                connection.send({"type": "pcm", "pcm": PCM})
                connection.send({"type": "pcm", "pcm": PCM})
            if mode in {"hung", "hung-before-first"}:
                while True:
                    time.sleep(.1)
            if mode == "natural-end":
                connection.send({"type": "end", "metrics": {}})
            elif cancel_event.wait(5):
                connection.send({"type": "cancelled"})
            else:
                connection.send({"type": "error", "error": "test peer never received cancellation"})
    except (EOFError, BrokenPipeError, OSError):
        pass
    finally:
        connection.close()


async def spawn_cooperative_worker(mode):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    event = context.Event()
    process = context.Process(target=cooperative_decoder, args=(child, event, mode), daemon=True)
    process.start()
    child.close()
    worker = ModelWorker()
    worker.process, worker.connection, worker.cancel_event = process, parent, event
    worker.model_id = DEFAULT_MODEL_ID
    try:
        message = await worker._receive(5)
        assert message["type"] == "ready"
        worker.state, worker.warmup = "ready", message["warmup"]
        return worker
    except BaseException:
        await worker.close()
        raise


def reloaded_decoder(connection, _spec, _cache_dir, _allow_download, cancel_event):
    cooperative_decoder(connection, cancel_event, "natural-end")


@pytest.mark.parametrize("observation", ["status", "warm", "reserve", "load"])
async def test_idle_child_exit_is_not_ready_and_same_model_can_be_reloaded(monkeypatch, observation):
    import shiri.tts.worker as module
    worker = await spawn_cooperative_worker("natural-end")
    previous_process, previous_connection = worker.process, worker.connection
    monkeypatch.setattr(module, "_child", reloaded_decoder)
    try:
        # Only the isolated test decoder exits; no HTTP operation is active to
        # notice its death and update the parent's last readiness receipt.
        previous_connection.send(None)
        await asyncio.to_thread(previous_process.join, 2)
        assert not previous_process.is_alive() and worker.state == "ready"
        if observation == "status":
            assert worker.status()["state"] == "failed"
            assert "exited" in worker.error
        elif observation == "warm":
            receipt = await worker.warm(DEFAULT_MODEL_ID, "a" * 32)
            assert not receipt["accepted"] and receipt["reason"] == "model_not_ready"
        elif observation == "reserve":
            with pytest.raises(ValueError, match="Load and warm"):
                await worker.reserve(payload(text="successor"))
            assert not worker.lock.locked()
        # Recovery must also work without a prior status read or failed speech.
        await worker.load(DEFAULT_MODEL_ID)
        assert worker.state == "loading"
        await asyncio.wait_for(worker.load_task, 5)
        assert worker.state == "ready" and worker.error is None
        assert worker.process is not previous_process and worker.process.is_alive()
        assert previous_connection.closed
        with pytest.raises(ValueError, match="closed"):
            previous_process.is_alive()
        prepared = await worker.reserve(payload(text="successor"))
        records = [json.loads(line) async for line in worker.stream(prepared)]
        assert [record["type"] for record in records] == ["format", "pcm", "end"]
        assert base64.b64decode(records[1]["pcm_base64"]) == b"\x52\x00" * 480
    finally:
        await worker.close()


@pytest.mark.parametrize("mode", ["before-first", "after-first", "natural-end"])
async def test_real_spawn_cancellation_keeps_owned_reader_and_next_generation_clean(mode):
    worker = await spawn_cooperative_worker(mode)
    original_process, original_connection = worker.process, worker.connection
    iterator = worker.stream(await worker.reserve(payload(text="interrupted")))
    waiting = None
    try:
        assert json.loads(await iterator.__anext__())["type"] == "format"
        if mode == "before-first":
            waiting = asyncio.create_task(iterator.__anext__())
            await asyncio.sleep(.03)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(waiting, 2.5)
        else:
            assert json.loads(await iterator.__anext__())["type"] == "pcm"
            await asyncio.wait_for(iterator.aclose(), 2.5)
        assert worker.state == "ready" and not worker.lock.locked()
        assert worker.process is original_process and worker.connection is original_connection
        assert original_process.is_alive() and worker.warmup == {"loads": 1}
        successor = await worker.reserve(payload(text="successor"))
        records = [json.loads(line) async for line in worker.stream(successor)]
        assert [record["type"] for record in records] == ["format", "pcm", "end"]
        assert base64.b64decode(records[1]["pcm_base64"]) == b"\x52\x00" * 480
        assert worker.process is original_process and worker.state == "ready"
    finally:
        if waiting is not None and not waiting.done():
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
        await iterator.aclose()
        await worker.close()


@pytest.mark.parametrize("mode", ["hung", "hung-before-first"])
async def test_real_spawn_hung_decoder_is_killed_with_bounded_cancel_fallback(mode):
    worker = await spawn_cooperative_worker(mode)
    iterator = worker.stream(await worker.reserve(payload(text="hung")))
    waiting = None
    try:
        await iterator.__anext__()
        if mode == "hung":
            assert json.loads(await iterator.__anext__())["type"] == "pcm"
            started = time.monotonic()
            await asyncio.wait_for(iterator.aclose(), 2.75)
        else:
            waiting = asyncio.create_task(iterator.__anext__())
            await asyncio.sleep(.03)
            started = time.monotonic()
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(waiting, 2.75)
        assert time.monotonic() - started < 2.75
        assert worker.state != "ready" and not worker.lock.locked()
        assert worker.process is None and worker.connection is None and worker._receive_task is None
    finally:
        if waiting is not None and not waiting.done():
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
        await iterator.aclose()
        await worker.close()


@pytest.mark.parametrize("reset_fails", [False, True])
def test_child_reset_finishes_before_cancel_ack_and_failure_refuses_warm_reuse(monkeypatch, reset_fails):
    import shiri.tts.backend as backend_module
    cancel_event, resets, generated = threading.Event(), [], []
    first, successor = GenerationRequest(text="first"), GenerationRequest(text="successor")

    class OwnedConnection(Connection):
        def recv(self):
            value = super().recv()
            cancel_event.clear()
            return value

        def send(self, message):
            if message["type"] == "cancelled":
                assert resets == ["first"]
            super().send(message)
            if message["type"] == "pcm" and len(generated) == 1:
                cancel_event.set()

    connection = OwnedConnection([first, successor, None])

    class Backend:
        last_metrics = {"complete": True}

        def prewarm(self):
            return {"loads": 1}

        def generate(self, request):
            generated.append(request.text)
            try:
                yield PCM * 2 if request.text == "first" else b"\x63\x00" * 480
            finally:
                resets.append(request.text)
                if reset_fails:
                    raise RuntimeError("decoder reset failed")

    monkeypatch.setattr(backend_module, "load_backend", lambda *args, **kwargs: Backend())
    _child(connection, get_model(DEFAULT_MODEL_ID), None, False, cancel_event)
    if reset_fails:
        assert generated == ["first"] and resets == ["first"]
        assert [message["type"] for message in connection.sent] == ["ready", "pcm", "error"]
        assert connection.messages == [successor, None]
    else:
        assert generated == ["first", "successor"] and resets == ["first", "successor"]
        assert [message["type"] for message in connection.sent] == ["ready", "pcm", "cancelled", "pcm", "end"]
        assert connection.sent[3]["pcm"] == b"\x63\x00" * 480
    assert connection.closed


async def test_level_triggered_http_disconnect_shields_cooperative_cleanup():
    worker = ready_worker([{"type": "pcm", "pcm": PCM}, {"type": "cancelled"}])
    worker.cancel_event = threading.Event()
    connection, process = worker.connection, worker.process
    app = create_worker_app(token=TOKEN, worker=worker)
    encoded, started = json.dumps(payload()).encode(), asyncio.Event()

    async def request_receive():
        return {"type": "http.request", "body": encoded, "more_body": False}

    async def disconnect_receive():
        await started.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        assert message["type"] == "http.response.start"
        started.set()
        await asyncio.Future()

    scope = {"type": "http", "method": "POST", "path": "/v1/generate", "headers": [],
             "asgi": {"version": "3.0", "spec_version": "2.0"}}
    route = next(route for route in app.routes if route.path == "/v1/generate")
    try:
        response = await route.endpoint(Request(scope, request_receive))
        await asyncio.wait_for(response(scope, disconnect_receive, send), 1)
        assert worker.state == "ready" and not worker.lock.locked()
        assert worker.connection is connection and worker.process is process
        assert not connection.closed and not process.closed and connection.messages == []
    finally:
        await worker.close()


async def wait_ready(worker, model_id, *, previous=None):
    async def observe():
        while worker.state != "ready" or worker.model_id != model_id or worker.process is previous:  # noqa: ASYNC110 - observe the bounded external process lifecycle.
            await asyncio.sleep(.01)
    await asyncio.wait_for(observe(), 5)


async def test_selected_model_persists_after_warmup_and_restores_at_next_start(tmp_path, monkeypatch):
    import shiri.tts.worker as module
    monkeypatch.setattr(module, "_child", reloaded_decoder)
    state_file = tmp_path / "selected-model.json"
    worker = ModelWorker(state_file=state_file)
    restored = None
    try:
        await worker.start(DEFAULT_MODEL_ID)
        await wait_ready(worker, DEFAULT_MODEL_ID)
        await worker.load(QWEN_MODEL_ID)
        await wait_ready(worker, QWEN_MODEL_ID)
        assert json.loads(state_file.read_text()) == {"version": 1, "model_id": QWEN_MODEL_ID}
        assert state_file.stat().st_mode & 0o777 == 0o600
        await worker.close()
        restored = ModelWorker(state_file=state_file)
        await restored.start(DEFAULT_MODEL_ID)
        await wait_ready(restored, QWEN_MODEL_ID)
        assert restored.status()["selected_model_id"] == QWEN_MODEL_ID
        assert restored.status()["automatic_recovery"]
        request = await restored.reserve({"model_id": QWEN_MODEL_ID, "text": "successor"})
        records = [json.loads(line) async for line in restored.stream(request)]
        assert [record["type"] for record in records] == ["format", "pcm", "end"]
    finally:
        await worker.close()
        if restored is not None:
            await restored.close()


async def test_resident_model_recovers_idle_exit_and_pending_request_without_periodic_inference(monkeypatch):
    import shiri.tts.worker as module
    monkeypatch.setattr(module, "_child", reloaded_decoder)
    monkeypatch.setattr(module, "RECOVERY_POLL_SECONDS", .01)
    worker = ModelWorker()
    try:
        await worker.start(DEFAULT_MODEL_ID)
        await wait_ready(worker, DEFAULT_MODEL_ID)
        previous = worker.process
        worker.connection.send(None)
        await asyncio.to_thread(previous.join, 2)
        assert not previous.is_alive()
        # The request waits for resident recovery; it never loads a model itself.
        prepared = await asyncio.wait_for(worker.reserve(payload(text="successor")), 5)
        assert worker.process is not previous and worker.recovery_attempts == 1
        records = [json.loads(line) async for line in worker.stream(prepared)]
        assert [record["type"] for record in records] == ["format", "pcm", "end"]
        resident = worker.process
        await asyncio.sleep(.05)
        assert worker.process is resident and worker.recovery_attempts == 1 and worker.state == "ready"
        assert not worker.lock.locked() and worker._receive_task is None
    finally:
        await worker.close()
    assert worker.state == "stopped" and worker.process is None


def selectively_unavailable_decoder(connection, spec, cache_dir, allow_download, cancel_event):
    if spec.id == QWEN_MODEL_ID:
        connection.send({"type": "error", "error": "Selected checkpoint is unavailable"})
        connection.close()
    else:
        reloaded_decoder(connection, spec, cache_dir, allow_download, cancel_event)


async def test_failed_model_change_preserves_last_successful_selection_and_recovers_it(tmp_path, monkeypatch):
    import shiri.tts.worker as module
    monkeypatch.setattr(module, "_child", selectively_unavailable_decoder)
    monkeypatch.setattr(module, "RECOVERY_POLL_SECONDS", .01)
    state_file = tmp_path / "selected-model.json"
    worker = ModelWorker(state_file=state_file)
    try:
        await worker.load(DEFAULT_MODEL_ID)
        await worker.load_task
        await worker.start()
        previous = worker.process
        await worker.load(QWEN_MODEL_ID)
        await worker.load_task
        assert worker.model_id in {QWEN_MODEL_ID, DEFAULT_MODEL_ID}
        assert json.loads(state_file.read_text())["model_id"] == DEFAULT_MODEL_ID
        await wait_ready(worker, DEFAULT_MODEL_ID, previous=previous)
        assert worker.selected_model == DEFAULT_MODEL_ID and worker.recovery_attempts == 1
    finally:
        await worker.close()


async def test_explicit_same_model_selection_is_saved_and_survives_new_preload_default(tmp_path, monkeypatch):
    import shiri.tts.worker as module
    monkeypatch.setattr(module, "_child", reloaded_decoder)
    state_file = tmp_path / "selected-model.json"
    worker = ModelWorker(state_file=state_file)
    restored = None
    try:
        await worker.start(DEFAULT_MODEL_ID)
        await wait_ready(worker, DEFAULT_MODEL_ID)
        resident = worker.process
        await worker.load(DEFAULT_MODEL_ID)
        assert worker.process is resident and json.loads(state_file.read_text())["model_id"] == DEFAULT_MODEL_ID
        await worker.close()
        restored = ModelWorker(state_file=state_file)
        await restored.start(QWEN_MODEL_ID)
        await wait_ready(restored, DEFAULT_MODEL_ID)
    finally:
        await worker.close()
        if restored is not None:
            await restored.close()


@pytest.mark.parametrize("new_model", [False, True])
async def test_cancelled_selection_write_cannot_escape_its_lock_and_overwrite_successor(tmp_path, monkeypatch, new_model):
    import shiri.tts.worker as module
    monkeypatch.setattr(module, "_child", reloaded_decoder)
    state_file = tmp_path / "selected-model.json"
    worker = ModelWorker(state_file=state_file)
    entered, release = threading.Event(), threading.Event()
    saving = None
    try:
        await worker.load(DEFAULT_MODEL_ID)
        await worker.load_task
        save = worker._save_selection

        def blocked_save(model_id):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("Selection-write test did not release its gate")
            save(model_id)

        monkeypatch.setattr(worker, "_save_selection", blocked_save)
        selected = QWEN_MODEL_ID if new_model else DEFAULT_MODEL_ID
        successor = DEFAULT_MODEL_ID if new_model else QWEN_MODEL_ID
        if new_model:
            await worker.load(selected)
            saving = worker.load_task
        else:
            saving = asyncio.create_task(worker.load(selected))
        assert await asyncio.to_thread(entered.wait, 1)
        saving.cancel()
        await asyncio.sleep(0)
        assert not saving.done() and worker.lock.locked()
        with pytest.raises(ValueError, match="operation is already active"):
            await worker.load(successor)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await saving
        assert worker.selected_model == selected
        assert json.loads(state_file.read_text())["model_id"] == selected
        await worker.load(successor)
        await worker.load_task
        assert worker.selected_model == successor
        assert json.loads(state_file.read_text())["model_id"] == successor
    finally:
        release.set()
        if saving is not None:
            await asyncio.gather(saving, return_exceptions=True)
        await worker.close()
