"""Streaming generation crosses real room admission without touching house audio."""
import asyncio
import base64
import io
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
import wave

import httpx
import pytest

from shiri.api import create_app
from shiri.domain import Conflict, DomainError, NotFound, RoomCreate, RoomPatch, SpeakerRef
from shiri.service import RoomService
from shiri.settings import Settings
from shiri.store import Store
from shiri.rpc import RpcError
from shiri.runtime.pcm_speech import PcmSpeech
from shiri.tts.coordinator import TextSpeechCoordinator, TextSpeechRequest
from shiri.tts.models import DEFAULT_MODEL_ID
from shiri.tts.worker import ModelWorker


FORMAT = {"type": "format", "format": "s16le", "sample_rate": 48000, "channels": 1}
END = {"type": "end", "metrics": {"first_pcm_ms": 12, "first_nonquiet_pcm_ms": 16,
                                    "leading_silence_ms": 4, "rtf": .2,
                                    "generated_audio_seconds": .03, "generation_ms": 6}}


def pcm(samples=960, value=1):
    return {"type": "pcm", "pcm_base64": base64.b64encode(value.to_bytes(2, "little", signed=True) * samples).decode()}


class Records(httpx.AsyncByteStream):
    def __init__(self, events, *, gate=None, failure=None, pause_after=None, pause_gate=None, pause_entered=None,
                 on_close=None):
        self.events, self.gate, self.failure = events, gate, failure
        self.pause_after, self.pause_gate, self.pause_entered = pause_after, pause_gate, pause_entered
        self.closed = False
        self.on_close = on_close

    async def __aiter__(self):
        if self.gate is not None:
            await self.gate.wait()
        for index, event in enumerate(self.events):
            yield ((json.dumps(event) if not isinstance(event, str) else event) + "\n").encode()
            if index == self.pause_after:
                self.pause_entered.set()
                await self.pause_gate.wait()
        if self.failure:
            raise self.failure

    async def aclose(self):
        self.closed = True
        if self.on_close:
            self.on_close()


class Runtime:
    def __init__(self):
        self.calls, self.streams = [], set()
        self.receivers = {}
        self.admitted_pcm = []
        self.now_ns = time.monotonic_ns
        self.prepare_entered = asyncio.Event()
        self.prepare_gate = None
        self.refuse = None

    async def call(self, operation, payload=None):
        if operation != "speech":
            return {"ready": True, "simulation": False, "rooms": []}
        self.calls.append(dict(payload))
        action = payload["action"]
        if action == "prepare-pcm":
            self.prepare_entered.set()
            if self.prepare_gate is not None:
                await self.prepare_gate.wait()
            stream_id = "stream-" + payload["session_id"]
            self.streams.add(stream_id)
            receiver = PcmSpeech(now_ns=self.now_ns)
            receiver.stream_id = stream_id
            self.receivers[stream_id] = receiver
            return receiver.prepared(payload["session_id"], payload["request_id"])
        if self.refuse == action:
            return {"ok": False, "error": "Injected room refusal"}
        assert payload["stream_id"] in self.streams
        receiver = self.receivers[payload["stream_id"]]
        if action == "pcm":
            data, samples, now, next_ns, audible = receiver.decode(payload)
            self.admitted_pcm.append((payload["stream_id"], payload["sequence"], data, now))
            return receiver.admitted(samples, now, next_ns, audible)
        if action in {"finish", "close"}:
            if action == "finish":
                receiver.finish(payload)
            self.streams.remove(payload["stream_id"])
            del self.receivers[payload["stream_id"]]
            return {"ok": True, "finished": action == "finish"}
        raise AssertionError(action)


@pytest.fixture
async def rig(tmp_path):
    store, runtime = Store(tmp_path / "state.sqlite3"), Runtime()
    room = store.create_room(RoomCreate(name="Kitchen", interface="eth0", nobly_room_id="Kitchen/exact"))
    room = store.update_room(room.id, RoomPatch(enabled=True, duck_gain=.19), room.revision)
    room = store.assign_speakers(room.id, [SpeakerRef(id="101", name="Kitchen speaker", protocol="airplay2")], room.revision)
    other = store.create_room(RoomCreate(name="Other", interface="eth0"))
    result = SimpleNamespace(store=store, runtime=runtime, service=RoomService(store, runtime), room=room,
                             other=other, events=[FORMAT, pcm(), pcm(480), END], status=200,
                             streams=[], requests=[], generate_gate=None, failure=None, coordinators=[],
                             pause_after=None, pause_gate=None, pause_entered=None,
                             worker_state="ready", on_stream_close=None)

    async def transport(request):
        result.requests.append(request)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"worker": {"state": result.worker_state, "model_id": DEFAULT_MODEL_ID}, "models": []})
        if request.url.path == "/v1/load":
            return httpx.Response(202, json={"state": "loading"})
        assert request.url.path == "/v1/generate"
        if result.status != 200:
            return httpx.Response(result.status, json={"error": "Injected worker refusal"})
        stream = Records(result.events, gate=result.generate_gate, failure=result.failure,
                         pause_after=result.pause_after, pause_gate=result.pause_gate, pause_entered=result.pause_entered,
                         on_close=result.on_stream_close)
        result.streams.append(stream)
        return httpx.Response(200, stream=stream)

    def coordinator():
        client = httpx.AsyncClient(transport=httpx.MockTransport(transport),
                                   headers={"Authorization": "Bearer private-worker-token"})
        value = TextSpeechCoordinator(result.service, worker_url="http://worker.test", client=client)
        result.coordinators.append(value)
        return value

    result.coordinator = coordinator
    yield result
    for value in result.coordinators:
        await value.close()
    store.close()


async def complete(coordinator, admitted):
    job = coordinator.get(admitted["id"])
    await asyncio.wait_for(job.task, 2)
    return job


def request():
    return TextSpeechRequest(text="The house is ready.", model_id=DEFAULT_MODEL_ID)


async def test_optional_worker_disabled_never_touches_house_audio(rig):
    coordinator = TextSpeechCoordinator(rig.service)
    try:
        assert await coordinator.catalog() == {"enabled": False, "worker": {"state": "stopped"}, "models": []}
        with pytest.raises(DomainError, match="optional"):
            await coordinator.admit(request(), room_id=rig.room.id)
        assert rig.runtime.calls == []
    finally:
        await coordinator.close()


async def test_nobly_binding_is_exact_and_continuations_keep_admitted_uuid(rig):
    coordinator = rig.coordinator()
    with pytest.raises(NotFound):
        await coordinator.admit(request(), external_id="kitchen/exact")
    admitted = await coordinator.admit(request(), external_id="Kitchen/exact")
    old = rig.store.get_room(rig.room.id)
    rig.store.update_room(old.id, RoomPatch(nobly_room_id="previous"), old.revision)
    other = rig.store.get_room(rig.other.id)
    rig.store.update_room(other.id, RoomPatch(nobly_room_id="Kitchen/exact"), other.revision)
    job = await complete(coordinator, admitted)
    assert job.state == "completed" and job.room_id == rig.room.id
    calls = rig.runtime.calls
    assert [call["action"] for call in calls] == ["prepare-pcm", "pcm", "pcm", "finish"]
    assert all(call["room_id"] == rig.room.id and call["duck_gain"] == .19 for call in calls)
    assert len({call["session_id"] for call in calls}) == len({call["request_id"] for call in calls}) == 1
    assert [(call["sequence"], call["frame_index"]) for call in calls if call["action"] == "pcm"] == [(1, 0), (2, 960)]
    assert calls[-1]["final_sequence"] == 2 and calls[-1]["final_frame_index"] == 1440
    assert not rig.runtime.streams and all(stream.closed for stream in rig.streams)
    assert coordinator.active is None


async def test_request_id_replay_keeps_original_uuid_after_binding_moves_without_regenerating(rig):
    rig.generate_gate = asyncio.Event()
    coordinator = rig.coordinator()
    payload = request()
    admitted = await coordinator.admit(payload, external_id="Kitchen/exact")
    await asyncio.wait_for(rig.runtime.prepare_entered.wait(), 1)
    original = rig.store.get_room(rig.room.id)
    rig.store.update_room(original.id, RoomPatch(nobly_room_id="previous"), original.revision)
    other = rig.store.get_room(rig.other.id)
    rig.store.update_room(other.id, RoomPatch(nobly_room_id="Kitchen/exact"), other.revision)
    replay = await coordinator.admit(payload, external_id="Kitchen/exact")
    assert replay["id"] == admitted["id"] and replay["room_id"] == rig.room.id
    with pytest.raises(Conflict, match="different speech or routing intent"):
        await coordinator.admit(payload.model_copy(update={"text": "Different sentence"}), external_id="Kitchen/exact")
    with pytest.raises(Conflict, match="different speech or routing intent"):
        await coordinator.admit(payload, room_id=rig.other.id)
    rig.generate_gate.set()
    job = await complete(coordinator, admitted)
    assert job.state == "completed"
    completed_replay = await coordinator.admit(payload, external_id="Kitchen/exact")
    assert completed_replay["state"] == "completed" and completed_replay["room_id"] == rig.room.id
    generate_requests = [value for value in rig.requests if value.url.path == "/v1/generate"]
    assert len(generate_requests) == 1
    assert "request_id" not in json.loads(generate_requests[0].content)
    assert [call["action"] for call in rig.runtime.calls].count("prepare-pcm") == 1
    assert all(call["room_id"] == rig.room.id for call in rig.runtime.calls)


async def test_pcm_is_paced_by_exact_sample_count_before_room_admission(rig, monkeypatch):
    import shiri.tts.coordinator as module
    clock, sleeps = [100.0], []

    async def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    rig.runtime.now_ns = lambda: int(clock[0] * 1e9)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0], monotonic_ns=rig.runtime.now_ns))
    monkeypatch.setattr(module, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": sleep}))
    rig.events = [FORMAT, pcm(960), pcm(480), pcm(120), END]
    coordinator = rig.coordinator()
    job = await complete(coordinator, await coordinator.admit(request(), room_id=rig.room.id))
    assert job.state == "completed"
    assert sleeps == pytest.approx([.02, .01])
    assert [(call["sequence"], call["frame_index"]) for call in rig.runtime.calls if call["action"] == "pcm"] == [(1, 0), (2, 960), (3, 1440)]
    assert job.metrics["delivered_audio_s"] == 1560 / 48000


@pytest.mark.parametrize("delayed_reply", [False, True])
async def test_initial_pcm_uses_actual_receiver_clock_and_preserves_post_admission_deadline(rig, monkeypatch,
                                                                                        delayed_reply):
    import shiri.tts.coordinator as module
    clock, sleeps = [100.0], []
    rig.runtime.now_ns = lambda: int(clock[0] * 1e9)

    async def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0], monotonic_ns=rig.runtime.now_ns))
    monkeypatch.setattr(module, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": sleep}))
    original_call = rig.runtime.call

    async def call(operation, payload=None):
        first_pcm = operation == "speech" and payload["action"] == "pcm" and payload["sequence"] == 1
        if first_pcm and not delayed_reply:
            clock[0] += .18
        receipt = await original_call(operation, payload)
        if first_pcm and delayed_reply:
            clock[0] += .18
        return receipt

    monkeypatch.setattr(rig.runtime, "call", call)
    rig.events = [FORMAT, pcm(960, 123), pcm(480, 456), pcm(120, 789), END]
    coordinator = rig.coordinator()
    job = await complete(coordinator, await coordinator.admit(request(), room_id=rig.room.id))
    assert job.metrics["first_pcm_dispatch_ms"] == pytest.approx(0)
    assert job.metrics["first_pcm_rpc_ms"] == pytest.approx(180)
    assert not rig.runtime.streams and job.metrics["worker_cleanup_confirmed"] is True
    assert len([r for r in rig.requests if r.url.path == "/v1/generate"]) == 1
    if delayed_reply:
        assert job.state == "failed" and job.error == "PCM delivery missed its live playback deadline"
        assert job.metrics["room_admission_ms"] == pytest.approx(0)
        assert job.metrics["delivered_audio_s"] == .02
        assert len(rig.runtime.admitted_pcm) == 1
        assert rig.runtime.calls[-1]["action"] == "close"
    else:
        assert job.state == "completed" and job.metrics["room_admission_ms"] == pytest.approx(180)
        assert sleeps == pytest.approx([.02, .01])
        assert job.metrics["delivered_audio_s"] == 1560 / 48000
        assert [item[1] for item in rig.runtime.admitted_pcm] == [1, 2, 3]
        assert b"".join(item[2] for item in rig.runtime.admitted_pcm) == b"".join(
            base64.b64decode(event["pcm_base64"]) for event in rig.events if event["type"] == "pcm")
        assert rig.runtime.calls[-1]["action"] == "finish"


@pytest.mark.parametrize("proof", [
    {"first_pcm_admitted_monotonic_ns": None}, {"first_pcm_admitted_monotonic_ns": True},
    {"first_pcm_admitted_monotonic_ns": -1}, {"first_pcm_admitted_monotonic_ns": 100_000_000_001},
    {"first_pcm_admitted_monotonic_ns": 99_999_999_999}, {"pcm_clock": None},
    {"pcm_clock": "mac_generation_worker_monotonic"},
    {"pcm_clock": "room_audio_worker_wall_clock; admission_not_acoustic"},
])
async def test_first_pcm_rejects_untruthful_admission_clock_before_accepting_later_audio(rig, monkeypatch, proof):
    import shiri.tts.coordinator as module
    rig.runtime.now_ns = lambda: 100_000_000_000
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: 100.0, monotonic_ns=rig.runtime.now_ns))
    original_call = rig.runtime.call

    async def call(operation, payload=None):
        receipt = await original_call(operation, payload)
        if operation == "speech" and payload["action"] == "pcm":
            receipt.update(proof)
        return receipt

    monkeypatch.setattr(rig.runtime, "call", call)
    coordinator = rig.coordinator()
    job = await complete(coordinator, await coordinator.admit(request(), room_id=rig.room.id))
    assert job.state == "failed" and job.error == "Room PCM admission clock could not be verified"
    assert len(rig.runtime.admitted_pcm) == 1 and not rig.runtime.streams
    assert rig.runtime.calls[-1]["action"] == "close"
    assert rig.runtime.calls[-1]["stream_id"] == "stream-tts-" + job.id
    assert len([r for r in rig.requests if r.url.path == "/v1/generate"]) == 1
    assert job.metrics["worker_cleanup_confirmed"] is True


async def test_later_receipt_cannot_reanchor_a_genuinely_late_stream(rig, monkeypatch):
    import shiri.tts.coordinator as module
    clock = [100.0]
    rig.runtime.now_ns = lambda: int(clock[0] * 1e9)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0], monotonic_ns=rig.runtime.now_ns))

    async def sleep(delay):
        clock[0] += delay

    monkeypatch.setattr(module, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": sleep}))
    original_call = rig.runtime.call

    async def call(operation, payload=None):
        receipt = await original_call(operation, payload)
        if operation == "speech" and payload["action"] == "pcm" and payload["sequence"] == 2:
            clock[0] += .18
            receipt["first_pcm_admitted_monotonic_ns"] = rig.runtime.now_ns()
        return receipt

    monkeypatch.setattr(rig.runtime, "call", call)
    rig.events = [FORMAT, pcm(), pcm(), pcm(), END]
    coordinator = rig.coordinator()
    job = await complete(coordinator, await coordinator.admit(request(), room_id=rig.room.id))
    assert job.state == "failed" and job.error == "PCM delivery missed its live playback deadline"
    assert job.metrics["room_admission_ms"] == pytest.approx(0)
    assert job.metrics["delivered_audio_s"] == .04 and len(rig.runtime.admitted_pcm) == 2
    assert not rig.runtime.streams and rig.runtime.calls[-1]["action"] == "close"


async def test_cancel_while_first_admission_reply_waits_closes_exact_stream_without_replaying_pcm(rig, monkeypatch):
    admitted, reply = asyncio.Event(), asyncio.Event()
    original_call = rig.runtime.call

    async def call(operation, payload=None):
        receipt = await original_call(operation, payload)
        if operation == "speech" and payload["action"] == "pcm" and payload["sequence"] == 1:
            admitted.set()
            await reply.wait()
        return receipt

    monkeypatch.setattr(rig.runtime, "call", call)
    coordinator = rig.coordinator()
    accepted = await coordinator.admit(request(), room_id=rig.room.id)
    await asyncio.wait_for(admitted.wait(), 1)
    result = await asyncio.wait_for(coordinator.cancel(accepted["id"]), 1)
    assert result["state"] == "cancelled" and result["metrics"]["worker_cleanup_confirmed"] is True
    assert len(rig.runtime.admitted_pcm) == 1 and not rig.runtime.streams and coordinator.active is None
    assert rig.runtime.calls[-1]["action"] == "close"
    assert rig.runtime.calls[-1]["stream_id"] == "stream-tts-" + accepted["id"]
    assert len([r for r in rig.requests if r.url.path == "/v1/generate"]) == 1


async def test_backend_readiness_metric_is_confirmed_before_delayed_first_generated_pcm(rig, monkeypatch):
    import shiri.tts.coordinator as module
    clock = [100.0]
    rig.runtime.now_ns = lambda: int(clock[0] * 1e9)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0], monotonic_ns=rig.runtime.now_ns))
    original_call = rig.runtime.call

    async def runtime_call(operation, payload=None):
        result = await original_call(operation, payload)
        if operation == "speech" and payload["action"] == "prepare-pcm":
            clock[0] += .007
        return result

    monkeypatch.setattr(rig.runtime, "call", runtime_call)
    rig.events = [FORMAT, pcm(120), END]
    rig.pause_after, rig.pause_gate, rig.pause_entered = 0, asyncio.Event(), asyncio.Event()
    coordinator = rig.coordinator()
    admitted = await coordinator.admit(request(), room_id=rig.room.id)
    await asyncio.wait_for(rig.pause_entered.wait(), 1)
    await asyncio.wait_for(rig.runtime.prepare_entered.wait(), 1)
    job = coordinator.get(admitted["id"])
    assert job.metrics["backend_ready_ms"] == pytest.approx(7)
    clock[0] += .5
    rig.pause_gate.set()
    job = await complete(coordinator, admitted)
    assert job.state == "completed"
    assert job.metrics["backend_ready_ms"] == pytest.approx(7)
    assert job.metrics["room_admission_ms"] == pytest.approx(507)


async def test_received_progress_precedes_room_readiness_and_engine_completion(rig, monkeypatch):
    import shiri.tts.coordinator as module
    clock = [100.0]
    rig.runtime.now_ns = lambda: int(clock[0] * 1e9)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0], monotonic_ns=rig.runtime.now_ns))

    async def sleep(delay):
        clock[0] += delay

    monkeypatch.setattr(module, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": sleep}))
    rig.runtime.prepare_gate = asyncio.Event()
    rig.pause_after, rig.pause_gate, rig.pause_entered = 1, asyncio.Event(), asyncio.Event()
    coordinator = rig.coordinator()
    admitted = await coordinator.admit(request(), room_id=rig.room.id)
    await asyncio.wait_for(rig.runtime.prepare_entered.wait(), 1)
    job = coordinator.get(admitted["id"])
    assert job.state == "generating" and not job.task.done()
    assert job.metrics["first_worker_pcm_received_ms"] == pytest.approx(0)
    assert job.metrics["received_audio_s"] == .02
    assert job.metrics["delivered_audio_s"] == 0
    assert "first_pcm_ms" not in job.metrics and "room_admission_ms" not in job.metrics

    clock[0] += .62
    rig.runtime.prepare_gate.set()
    await asyncio.wait_for(rig.pause_entered.wait(), 1)
    public = job.public()
    assert public["state"] == "playing" and not job.task.done()
    assert public["metrics"]["received_audio_s"] == .02
    assert public["metrics"]["delivered_audio_s"] == .02
    assert public["metrics"]["room_admission_ms"] == pytest.approx(620)
    assert "first_pcm_ms" not in public["metrics"]

    rig.pause_gate.set()
    job = await complete(coordinator, admitted)
    assert job.state == "completed"
    assert job.metrics["first_worker_pcm_received_ms"] == pytest.approx(0)
    assert job.metrics["received_audio_s"] == job.metrics["delivered_audio_s"] == .03
    assert job.metrics["first_pcm_ms"] == 12  # A separate engine clock, received only at EOS.


async def test_received_progress_does_not_claim_delivery_when_room_refuses_pcm(rig):
    rig.runtime.refuse = "pcm"
    coordinator = rig.coordinator()
    job = await complete(coordinator, await coordinator.admit(request(), room_id=rig.room.id))
    assert job.state == "failed" and job.error == "Injected room refusal"
    assert job.metrics["received_audio_s"] == .02 and job.metrics["delivered_audio_s"] == 0
    assert job.metrics["first_worker_pcm_received_ms"] >= 0
    assert "room_admission_ms" not in job.metrics


async def test_benchmark_collects_generation_metrics_without_room_resolution_or_audio(rig):
    coordinator = rig.coordinator()
    job = await complete(coordinator, await coordinator.admit(request(), benchmark=True))
    assert job.kind == "benchmark" and job.room_id is None and job.state == "completed"
    assert rig.runtime.calls == []
    assert job.metrics["first_pcm_ms"] == 12 and job.metrics["first_non_silent_pcm_ms"] == 16
    assert job.metrics["realtime_factor"] == .2
    assert "room_admission_ms" not in job.metrics
    assert all(stream.closed for stream in rig.streams)


async def test_cancel_waits_for_pending_preparation_then_closes_exact_admitted_stream(rig):
    rig.runtime.prepare_gate, rig.generate_gate = asyncio.Event(), asyncio.Event()
    coordinator = rig.coordinator()
    admitted = await coordinator.admit(request(), external_id="Kitchen/exact")
    await asyncio.wait_for(rig.runtime.prepare_entered.wait(), 1)
    cancelling = asyncio.create_task(coordinator.cancel(admitted["id"]))
    await asyncio.sleep(0)
    assert not cancelling.done()
    rig.runtime.prepare_gate.set()
    result = await asyncio.wait_for(cancelling, 1)
    assert result["state"] == "cancelled"
    assert [call["action"] for call in rig.runtime.calls] == ["prepare-pcm", "close"]
    assert all(call["room_id"] == rig.room.id for call in rig.runtime.calls)
    assert rig.runtime.calls[-1]["stream_id"] == "stream-tts-" + admitted["id"]
    assert not rig.runtime.streams and all(stream.closed for stream in rig.streams)
    assert coordinator.active is None


async def test_cancellation_before_job_starts_releases_admission_and_marks_terminal_state(rig):
    coordinator = rig.coordinator()
    admitted = await coordinator.admit(request(), room_id=rig.room.id)
    result = await coordinator.cancel(admitted["id"])
    assert result["state"] == "cancelled"
    assert coordinator.active is None and rig.runtime.calls == []
    successor = await coordinator.admit(request(), benchmark=True)
    assert (await complete(coordinator, successor)).state == "completed"


async def test_aborted_canceller_cannot_leave_unstarted_job_queued_or_block_successor(rig):
    coordinator = rig.coordinator()

    async def interrupted_caller():
        admitted = await coordinator.admit(request(), benchmark=True)
        asyncio.get_running_loop().call_soon(asyncio.current_task().cancel)
        with pytest.raises(asyncio.CancelledError):
            await coordinator.cancel(admitted["id"])
        return admitted

    admitted = await asyncio.create_task(interrupted_caller())
    await asyncio.sleep(0)
    job = coordinator.get(admitted["id"])
    assert job.task.done() and job.state == "cancelled" and coordinator.active is None
    assert (await coordinator.cancel(job.id))["state"] == "cancelled"
    assert (await complete(coordinator, await coordinator.admit(request(), benchmark=True))).state == "completed"
    assert rig.runtime.calls == []


async def test_repeated_cancellation_preserves_pending_exact_stream_cleanup(rig):
    rig.runtime.prepare_gate, rig.generate_gate = asyncio.Event(), asyncio.Event()
    coordinator = rig.coordinator()
    admitted = await coordinator.admit(request(), room_id=rig.room.id)
    await asyncio.wait_for(rig.runtime.prepare_entered.wait(), 1)
    first = asyncio.create_task(coordinator.cancel(admitted["id"]))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    second = asyncio.create_task(coordinator.cancel(admitted["id"]))
    await asyncio.sleep(0)
    rig.runtime.prepare_gate.set()
    results = await asyncio.wait_for(asyncio.gather(first, second), 1)
    assert all(result["state"] == "cancelled" for result in results)
    assert [call["action"] for call in rig.runtime.calls] == ["prepare-pcm", "close"]
    assert not rig.runtime.streams and coordinator.active is None


@pytest.mark.parametrize("acknowledgment", [{"type": "cancelled"}, {"type": "end", "metrics": {}}])
@pytest.mark.parametrize("caller_disconnects", [False, True])
async def test_http_cancel_waits_for_private_decoder_ack_before_retiring_slot(rig, monkeypatch,
                                                                            acknowledgment, caller_disconnects):
    # The remote HTTP producer runs independently. Closing the client socket
    # triggers its cancellation but returns before its decoder reset/ACK.
    worker = ModelWorker()
    sent = []
    connection = SimpleNamespace(send=sent.append, close=Mock())
    worker.connection, worker.cancel_event = connection, threading.Event()
    worker.model_id, worker.state = DEFAULT_MODEL_ID, "ready"
    reading, resetting, acknowledge, busy_observed = (asyncio.Event() for _ in range(4))
    server_tasks, requests = [], []
    successor_records = [{"type": "pcm", "pcm": b"\x05\x00" * 960}, {"type": "end", "metrics": {}}]

    async def receive(wait_seconds):
        if len(sent) > 1:
            assert not worker.cancel_event.is_set()
            return successor_records.pop(0)
        if worker.cancel_event.is_set():
            resetting.set()
            await acknowledge.wait()
            return acknowledgment
        reading.set()
        await asyncio.Future()

    monkeypatch.setattr(worker, "_receive", receive)

    class RemoteStream(httpx.AsyncByteStream):
        def __init__(self, prepared):
            self.queue = asyncio.Queue()
            self.closed = False
            self.server = asyncio.create_task(self.serve(prepared))
            server_tasks.append(self.server)

        async def serve(self, prepared):
            try:
                async for line in worker.stream(prepared):
                    await self.queue.put(line.encode())
            except asyncio.CancelledError:
                pass
            finally:
                await self.queue.put(None)

        async def __aiter__(self):
            while (line := await self.queue.get()) is not None:
                yield line

        async def aclose(self):
            self.closed = True
            if not self.server.done():
                self.server.cancel()

    async def transport(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer private-worker-token"
        if request.url.path == "/v1/models":
            if worker.state == "busy" and resetting.is_set():
                assert request.extensions["timeout"]["read"] == 1
                busy_observed.set()
            return httpx.Response(200, json={"worker": worker.status(), "models": []})
        assert request.url.path == "/v1/generate" and request.method == "POST"
        prepared = await worker.reserve(json.loads(request.content))
        return httpx.Response(200, stream=RemoteStream(prepared))

    client = httpx.AsyncClient(transport=httpx.MockTransport(transport),
                               headers={"Authorization": "Bearer private-worker-token"})
    coordinator = TextSpeechCoordinator(rig.service, worker_url="http://worker.test", client=client)
    cancelling = None
    try:
        admitted = await coordinator.admit(request(), room_id=rig.room.id)
        job = coordinator.get(admitted["id"])
        await asyncio.wait_for(reading.wait(), 1)
        await asyncio.wait_for(rig.runtime.prepare_entered.wait(), 1)
        cancelling = asyncio.create_task(coordinator.cancel(job.id))
        await asyncio.wait_for(busy_observed.wait(), 1)
        assert not cancelling.done() and coordinator.active == job.id
        assert worker.state == "busy" and worker.lock.locked()
        assert job.state not in {"completed", "cancelled", "failed"}
        assert not rig.runtime.streams
        assert rig.runtime.calls[-1]["action"] == "close"
        assert rig.runtime.calls[-1]["stream_id"] == "stream-tts-" + job.id
        with pytest.raises(Conflict, match="already active"):
            await coordinator.admit(request(), benchmark=True)
        with pytest.raises(Conflict, match="active speech job"):
            await coordinator.load(DEFAULT_MODEL_ID)
        if caller_disconnects:
            cancelling.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelling
            assert not job.task.done() and coordinator.active == job.id
        acknowledge.set()
        await asyncio.wait_for(job.task, 1)
        if not caller_disconnects:
            assert (await cancelling)["state"] == "cancelled"
        assert job.state == "cancelled" and job.metrics["worker_cleanup_confirmed"] is True
        assert coordinator.active is None and worker.state == "ready" and not worker.lock.locked()
        assert worker.connection is connection and not connection.close.called
        assert (await coordinator.cancel(job.id))["state"] == "cancelled"
        successor = await complete(coordinator, await coordinator.admit(request(), room_id=rig.room.id))
        assert successor.state == "completed" and successor.metrics["worker_cleanup_confirmed"] is True
        assert len(sent) == 2 and len([r for r in requests if r.url.path == "/v1/generate"]) == 2
        assert not rig.runtime.streams and worker.connection is connection
    finally:
        acknowledge.set()
        await coordinator.close()
        await asyncio.gather(*server_tasks, return_exceptions=True)
        await worker.close()


async def test_unconfirmed_remote_cleanup_has_finite_budget_without_retry_or_remote_cancel(rig, monkeypatch):
    monkeypatch.setattr("shiri.tts.coordinator.WORKER_SETTLE_SECONDS", .075)
    rig.generate_gate = asyncio.Event()
    rig.on_stream_close = lambda: setattr(rig, "worker_state", "busy")
    coordinator = rig.coordinator()
    admitted = await coordinator.admit(request(), room_id=rig.room.id)
    await asyncio.wait_for(rig.runtime.prepare_entered.wait(), 1)
    result = await asyncio.wait_for(coordinator.cancel(admitted["id"]), .5)
    assert result["state"] == "cancelled" and "worker cleanup could not be confirmed" in result["error"]
    assert result["metrics"]["worker_cleanup_confirmed"] is False
    assert coordinator.active is None and not rig.runtime.streams
    assert len([r for r in rig.requests if r.url.path == "/v1/generate"]) == 1
    assert all(r.method == "GET" for r in rig.requests if r.url.path != "/v1/generate")
    assert (await coordinator.cancel(admitted["id"])) == result


async def test_control_client_failure_during_retirement_cannot_leak_admission(rig, monkeypatch):
    rig.generate_gate = asyncio.Event()
    coordinator = rig.coordinator()
    original_catalog = coordinator.catalog

    async def catalog(*, wait_seconds=5):
        if wait_seconds == 1:
            raise RuntimeError("Control client closed during decoder retirement")
        return await original_catalog(wait_seconds=wait_seconds)

    monkeypatch.setattr(coordinator, "catalog", catalog)
    admitted = await coordinator.admit(request(), room_id=rig.room.id)
    await asyncio.wait_for(rig.runtime.prepare_entered.wait(), 1)
    result = await asyncio.wait_for(coordinator.cancel(admitted["id"]), .5)
    assert result["state"] == "cancelled" and result["metrics"]["worker_cleanup_confirmed"] is False
    assert "worker cleanup could not be confirmed" in result["error"]
    assert coordinator.active is None and coordinator.get(admitted["id"]).task.done()
    assert not rig.runtime.streams and all(stream.closed for stream in rig.streams)


@pytest.mark.parametrize("benchmark", [False, True])
@pytest.mark.parametrize("caller_disconnects", [False, True])
async def test_first_cancel_during_natural_finalization_joins_owned_retirement(rig, monkeypatch,
                                                                             benchmark, caller_disconnects):
    rig.on_stream_close = lambda: setattr(rig, "worker_state", "busy")
    coordinator = rig.coordinator()
    original_catalog = coordinator.catalog
    retiring = asyncio.Event()

    async def catalog(*, wait_seconds=5):
        result = await original_catalog(wait_seconds=wait_seconds)
        if wait_seconds == 1 and result["worker"]["state"] == "busy":
            retiring.set()
        return result

    monkeypatch.setattr(coordinator, "catalog", catalog)
    admitted = await coordinator.admit(request(), benchmark=benchmark,
                                        room_id=None if benchmark else rig.room.id)
    job = coordinator.get(admitted["id"])
    await asyncio.wait_for(retiring.wait(), 1)
    assert job.state not in {"completed", "cancelled", "failed"} and coordinator.active == job.id
    with pytest.raises(NotFound):
        coordinator.sample(job.id)
    cancelling = asyncio.create_task(coordinator.cancel(job.id))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    repeated = asyncio.create_task(coordinator.cancel(job.id))
    await asyncio.sleep(0)
    assert not cancelling.done() and not repeated.done() and not job.task.done()
    assert coordinator.active == job.id and not rig.runtime.streams
    with pytest.raises(Conflict, match="already active"):
        await coordinator.admit(request(), benchmark=True)
    if caller_disconnects:
        cancelling.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelling
        assert not job.task.done() and coordinator.active == job.id
    rig.worker_state = "ready"
    result = await asyncio.wait_for(repeated, 1)
    assert result["state"] == "cancelled" and result["metrics"]["worker_cleanup_confirmed"] is True
    if not caller_disconnects:
        assert (await cancelling) == result
    assert job.task.done() and coordinator.active is None and job.audio is None
    assert [call["action"] for call in rig.runtime.calls] == ([] if benchmark else ["prepare-pcm", "pcm", "pcm", "finish"])
    rig.on_stream_close = None
    successor = await complete(coordinator, await coordinator.admit(request(), benchmark=True))
    assert successor.state == "completed" and successor.metrics["worker_cleanup_confirmed"] is True


@pytest.mark.parametrize("events", [[pcm(), END], [FORMAT, {**FORMAT, "channels": 2}],
                                   [FORMAT, {"type": "pcm", "pcm_base64": "@@"}],
                                   [FORMAT, {"type": "pcm", "pcm_base64": "AA=="}],
                                   [FORMAT, pcm(961)], [FORMAT, pcm(), "{"],
                                   [FORMAT, pcm()], [FORMAT, END], [FORMAT, pcm(), {"type": "error", "error": "decoder lost"}],
                                   [FORMAT, pcm(), []]])
async def test_malformed_or_truncated_generation_closes_exact_room_stream(rig, events):
    rig.events = events
    coordinator = rig.coordinator()
    job = await complete(coordinator, await coordinator.admit(request(), room_id=rig.room.id))
    assert job.state == "failed" and job.error
    assert rig.runtime.calls[-1]["action"] == "close"
    assert not rig.runtime.streams and all(stream.closed for stream in rig.streams)
    assert coordinator.active is None


@pytest.mark.parametrize("refuse", ["generate", "pcm", "finish", "transport"])
async def test_worker_or_backend_failure_releases_speech_and_busy_state(rig, refuse):
    if refuse == "generate":
        rig.status = 409
    elif refuse == "transport":
        rig.events, rig.failure = [FORMAT, pcm()], httpx.ReadError("lost private worker")
    else:
        rig.runtime.refuse = refuse
    coordinator = rig.coordinator()
    job = await complete(coordinator, await coordinator.admit(request(), room_id=rig.room.id))
    assert job.state == "failed" and job.error
    assert rig.runtime.calls[-1]["action"] == "close"
    assert not rig.runtime.streams and coordinator.active is None


async def test_busy_policy_is_explicit_and_completed_history_is_bounded(rig):
    rig.generate_gate = asyncio.Event()
    coordinator = rig.coordinator()
    admitted = await coordinator.admit(request(), benchmark=True)
    with pytest.raises(Conflict, match="already active"):
        await coordinator.admit(request(), benchmark=True)
    with pytest.raises(Conflict, match="active speech job"):
        await coordinator.load(DEFAULT_MODEL_ID)
    rig.generate_gate.set()
    await complete(coordinator, admitted)
    for _ in range(32):
        await complete(coordinator, await coordinator.admit(request(), benchmark=True))
    assert len(coordinator.jobs) == 32
    with pytest.raises(NotFound):
        coordinator.get(admitted["id"])
    assert rig.runtime.calls == []


@pytest.mark.parametrize("url", ["file:///tmp/model", "http://user:secret@worker.test", "http://worker.test/base",
                                "http://worker.test?target=other", "http://worker.test#secret"])
def test_worker_address_belongs_to_installation_origin_configuration(tmp_path, url):
    with pytest.raises(ValueError, match="trusted installation"):
        Settings(tts_worker_url=url, tts_worker_token_file=tmp_path / "token")


async def test_optional_tts_api_uses_normal_authentication_and_same_origin_guard(tmp_path):
    token = "private-test-admin-token-12345678901234567890"
    app = create_app(Settings(state_dir=tmp_path), token=token)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shiri.test") as client:
            assert (await client.get("/api/v1/tts/models")).status_code == 401
            client.headers["Authorization"] = "Bearer " + token
            response = await client.get("/api/v1/tts/models")
            assert response.json()["enabled"] is False
            for path, body in (("/api/v1/tts/models/load", {"model_id": DEFAULT_MODEL_ID}),
                               ("/api/v1/tts/benchmark", {"text": "hello"})):
                assert (await client.post(path, json=body, headers={"Origin": "https://evil.test"})).status_code == 403
                assert (await client.post(path, json=body, headers={"Origin": "http://shiri.test"})).status_code == 400
    finally:
        await app.state.tts.close()
        app.state.service.store.close()


async def test_public_lan_api_preserves_separate_worker_credential_and_quiet_benchmark(tmp_path):
    worker_token = "private-worker-key-123456789012345678901234567890"
    token_file = tmp_path / "worker-token"
    token_file.write_text(worker_token)
    token_file.chmod(0o600)
    app = create_app(Settings(state_dir=tmp_path / "state", allow_unauthenticated=True,
                              tts_worker_url="http://worker.test", tts_worker_token_file=token_file))
    requests = []

    async def transport(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer " + worker_token
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"worker": {"state": "ready", "model_id": DEFAULT_MODEL_ID}, "models": []})
        assert request.url.path == "/v1/generate"
        return httpx.Response(200, stream=Records([FORMAT, pcm(), END]))

    await app.state.tts.client._transport.aclose()
    app.state.tts.client._transport = httpx.MockTransport(transport)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shiri.test") as client:
            assert (await client.get("/api/v1/tts/models")).status_code == 200
            forbidden = await client.post("/api/v1/tts/benchmark", json={"text": "hello"}, headers={"Origin": "https://evil.test"})
            assert forbidden.status_code == 403
            accepted = await client.post("/api/v1/tts/benchmark", json={"text": "hello"}, headers={"Origin": "http://shiri.test"})
            assert accepted.status_code == 202
            job = await complete(app.state.tts, accepted.json())
            assert job.state == "completed" and job.kind == "benchmark" and job.room_id is None
            public = await client.get("/api/v1/tts/jobs/" + job.id)
            assert worker_token not in public.text
        assert [request.url.path for request in requests] == [
            "/v1/models", "/v1/models", "/v1/generate", "/v1/models"]
    finally:
        await app.state.tts.close()
        app.state.service.store.close()


async def test_quiet_sample_is_complete_48khz_mono_wav_with_exact_prefix_and_tail(rig):
    rig.events = [FORMAT, pcm(7, 0), pcm(960, 1234), pcm(481, -32767), END]
    expected = b"".join(base64.b64decode(event["pcm_base64"]) for event in rig.events if event["type"] == "pcm")
    coordinator = rig.coordinator()
    job = await complete(coordinator, await coordinator.admit(request(), benchmark=True))
    assert job.public()["sample_available"] is True
    with wave.open(io.BytesIO(coordinator.sample(job.id)), "rb") as audio:
        assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getnframes()) == (1, 2, 48000, 1448)
        assert audio.getcomptype() == "NONE"
        assert audio.readframes(audio.getnframes()) == expected
        assert audio.readframes(1) == b""
    assert expected[:14] == b"\x00" * 14
    assert expected[-962:] == (-32767).to_bytes(2, "little", signed=True) * 481
    assert rig.runtime.calls == []


async def test_partial_quiet_generation_is_unavailable_and_cancellation_discards_preview(rig):
    rig.pause_after, rig.pause_gate, rig.pause_entered = 1, asyncio.Event(), asyncio.Event()
    coordinator = rig.coordinator()
    admitted = await coordinator.admit(request(), benchmark=True)
    queued = coordinator.get(admitted["id"])
    assert queued.public()["sample_available"] is False
    with pytest.raises(NotFound, match="sample"):
        coordinator.sample(queued.id)
    await asyncio.wait_for(rig.pause_entered.wait(), 1)
    assert queued.state == "generating" and len(queued.audio) == 1920
    assert queued.public()["sample_available"] is False
    with pytest.raises(NotFound, match="sample"):
        coordinator.sample(queued.id)
    cancelled = await coordinator.cancel(queued.id)
    assert cancelled["state"] == "cancelled" and cancelled["sample_available"] is False
    assert queued.audio is None
    with pytest.raises(NotFound, match="sample"):
        coordinator.sample(queued.id)
    assert rig.runtime.calls == []


@pytest.mark.parametrize("failure", ["truncated", "transport", "decoder"])
async def test_failed_quiet_generation_discards_every_partial_sample(rig, failure):
    rig.events = [FORMAT, pcm(960, 2255)]
    if failure == "transport":
        rig.failure = httpx.ReadError("private worker disconnected")
    elif failure == "decoder":
        rig.events.append({"type": "error", "error": "generation failed"})
    coordinator = rig.coordinator()
    job = await complete(coordinator, await coordinator.admit(request(), benchmark=True))
    assert job.state == "failed" and job.audio is None and job.public()["sample_available"] is False
    with pytest.raises(NotFound, match="sample"):
        coordinator.sample(job.id)
    assert rig.runtime.calls == []


async def test_successful_house_speech_does_not_retain_or_expose_a_quiet_preview(rig):
    coordinator = rig.coordinator()
    job = await complete(coordinator, await coordinator.admit(request(), room_id=rig.room.id))
    assert job.state == "completed" and job.kind == "speech" and job.audio is None
    assert job.public()["sample_available"] is False
    with pytest.raises(NotFound, match="sample"):
        coordinator.sample(job.id)


async def test_preview_retirement_bounds_memory_and_keeps_old_job_status(rig):
    # Three fully admitted60s streams exceed12MiB; the oldest sample must retire.
    rig.events = [FORMAT, *([pcm()] * 3000), END]
    coordinator = rig.coordinator()
    jobs = [await complete(coordinator, await coordinator.admit(request(), benchmark=True)) for _ in range(3)]
    assert all(job.state == "completed" and job.metrics["delivered_audio_s"] == 60 for job in jobs)
    assert sum(len(job.audio or b"") for job in coordinator.jobs.values()) <= 12 * 1024**2
    assert coordinator.get(jobs[0].id).public()["state"] == "completed"
    assert jobs[0].public()["sample_available"] is False and jobs[0].audio is None
    with pytest.raises(NotFound, match="sample"):
        coordinator.sample(jobs[0].id)
    assert all(job.public()["sample_available"] for job in jobs[1:])
    assert len(jobs[-1].audio) == 60 * 48000 * 2
    assert rig.runtime.calls == []


async def test_idempotent_quiet_request_does_not_duplicate_preview_or_generate_audio_again(rig):
    coordinator, payload = rig.coordinator(), request()
    job = await complete(coordinator, await coordinator.admit(payload, benchmark=True))
    retained, first_sample = job.audio, coordinator.sample(job.id)
    replay = await coordinator.admit(payload, benchmark=True)
    assert replay["id"] == job.id and replay["sample_available"] is True
    assert job.audio is retained and coordinator.sample(job.id) == first_sample
    assert len(coordinator.jobs) == 1
    assert len([value for value in rig.requests if value.url.path == "/v1/generate"]) == 1
    with pytest.raises(Conflict):
        await coordinator.admit(payload.model_copy(update={"text": "changed intent"}), benchmark=True)
    assert coordinator.sample(job.id) == first_sample
    assert rig.runtime.calls == []


async def test_preview_api_requires_normal_installation_session_and_never_exposes_worker_credentials(tmp_path):
    admin_token = "private-test-admin-token-12345678901234567890"
    worker_token = "private-worker-key-123456789012345678901234567890"
    token_file = tmp_path / "worker-token"
    token_file.write_text(worker_token)
    token_file.chmod(0o600)
    app = create_app(Settings(state_dir=tmp_path / "state", tts_worker_url="http://worker.test",
                              tts_worker_token_file=token_file), token=admin_token)
    generated = [FORMAT, pcm(11, 0), pcm(137, 1407), END]
    expected = b"".join(base64.b64decode(event["pcm_base64"]) for event in generated if event["type"] == "pcm")

    async def transport(request):
        assert request.headers["Authorization"] == "Bearer " + worker_token
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"worker": {"state": "ready", "model_id": DEFAULT_MODEL_ID}, "models": []})
        assert request.url.path == "/v1/generate"
        return httpx.Response(200, stream=Records(generated))

    await app.state.tts.client._transport.aclose()
    app.state.tts.client._transport = httpx.MockTransport(transport)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shiri.test") as client:
            assert (await client.get("/api/v1/tts/jobs/unknown/sample.wav")).status_code == 401
            login = await client.post("/api/v1/session", json={"token": admin_token}, headers={"Origin": "http://shiri.test"})
            assert login.status_code == 200 and "HttpOnly" in login.headers["set-cookie"]
            admitted = await client.post("/api/v1/tts/benchmark", json={"text": "hello"}, headers={"Origin": "http://shiri.test"})
            assert admitted.status_code == 202
            job = await complete(app.state.tts, admitted.json())
            assert job.public()["sample_available"] is True
            path = f"/api/v1/tts/jobs/{job.id}/sample.wav"
            response = await client.get(path)
            assert response.status_code == 200 and response.headers["content-type"] == "audio/wav"
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["content-disposition"] == 'inline; filename="shiri-voice-preview.wav"'
            with wave.open(io.BytesIO(response.content), "rb") as audio:
                assert audio.getframerate() == 48000 and audio.getnchannels() == 1 and audio.getsampwidth() == 2
                assert audio.readframes(audio.getnframes()) == expected
            assert worker_token not in str(response.headers)
            assert (await client.get("/api/v1/tts/jobs/unknown/sample.wav")).status_code == 404
            assert (await client.delete("/api/v1/session", headers={"Origin": "http://shiri.test"})).status_code == 200
            assert (await client.get(path)).status_code == 401
    finally:
        await app.state.tts.close()
        app.state.service.store.close()


@pytest.mark.parametrize("document", [[], None, {}, {"worker": [], "models": []},
                                     {"worker": {"state": []}, "models": []},
                                     {"worker": {"state": "unknown"}, "models": []},
                                     {"worker": {"state": "ready"}, "models": {}},
                                     {"worker": {"state": "ready"}, "models": ["wrong shape"]}])
async def test_incompatible_catalog_becomes_unavailable_without_crashing_or_room_audio(rig, document):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
        200, content=json.dumps(document), headers={"Content-Type": "application/json"})))
    coordinator = TextSpeechCoordinator(rig.service, worker_url="http://worker.test", client=client)
    try:
        catalog = await coordinator.catalog()
        assert catalog["enabled"] is True and catalog["worker"]["state"] == "unavailable" and catalog["models"] == []
        assert rig.runtime.calls == []
    finally:
        await coordinator.close()


@pytest.mark.parametrize("status, document", [(202, []), (202, None), (202, {}),
                                             (202, {"state": "failed"}), (202, {"state": []}),
                                             (409, {"error": []}), (502, [])])
async def test_incompatible_load_response_is_explicit_unavailability_without_reserving_job(rig, status, document):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
        status, content=json.dumps(document), headers={"Content-Type": "application/json"})))
    coordinator = TextSpeechCoordinator(rig.service, worker_url="http://worker.test", client=client)
    try:
        with pytest.raises(RpcError) as error:
            await coordinator.load(DEFAULT_MODEL_ID)
        assert error.value.code == "audio_unavailable"
        assert coordinator.active is None and not coordinator.jobs and rig.runtime.calls == []
    finally:
        await coordinator.close()


@pytest.mark.parametrize("operation", ["catalog", "load"])
async def test_model_control_has_five_second_http_budget_separate_from_generation(rig, operation):
    async def transport(request):
        assert set(request.extensions["timeout"].values()) == {5}
        raise httpx.ReadTimeout("control worker did not respond", request=request)

    client = httpx.AsyncClient(timeout=httpx.Timeout(180, connect=5), transport=httpx.MockTransport(transport))
    coordinator = TextSpeechCoordinator(rig.service, worker_url="http://worker.test", client=client)
    try:
        if operation == "catalog":
            assert (await coordinator.catalog())["worker"]["state"] == "unavailable"
        else:
            with pytest.raises(RpcError) as error:
                await coordinator.load(DEFAULT_MODEL_ID)
            assert error.value.code == "audio_unavailable"
        assert rig.runtime.calls == [] and coordinator.active is None
    finally:
        await coordinator.close()


async def test_valid_worker_load_refusal_remains_a_bounded_conflict(rig):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(409, json={"error": "x" * 2048})))
    coordinator = TextSpeechCoordinator(rig.service, worker_url="http://worker.test", client=client)
    try:
        with pytest.raises(Conflict) as error:
            await coordinator.load(DEFAULT_MODEL_ID)
        assert str(error.value) == "x" * 512
        assert coordinator.active is None and rig.runtime.calls == []
    finally:
        await coordinator.close()
