"""Direct generated speech retains the authenticated native voice lifecycle."""

import asyncio
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import time

import pytest

from shiri.rpc import RpcError, serve_rpc
from shiri.speech_stream import open_speech
from shiri.runtime.audio import AudioWorker, SpeechSession
from shiri.runtime.pcm_speech import MAX_LEAD_NS, MAX_STALL_NS, PcmSpeech
from test_audio import FakePeer, request
from test_native_audio import begin, pcm
from test_speech_startup import native_controller
from test_speech_owner import owned_worker


def opening(session="generated-voice", request_id="text-1"):
    return {"session_id": session, "request_id": request_id, "duck_gain": 0.17}


def media(prepared, *, sequence=1, frame_index=0, samples=960, value=1000):
    return {"action": "pcm", "session_id": prepared["session_id"], "request_id": prepared["request_id"],
            "stream_id": prepared["stream_id"], "sequence": sequence, "frame_index": frame_index,
            "data": struct.pack("<h", value) * samples}


async def deliver(worker, payload):
    """Inject raw samples at the worker admission boundary for focused fault tests."""
    return await worker.direct_pcm(payload, data=payload["data"])


def ending(prepared, *, sequence=1, frames=960, action="finish"):
    return {"action": action, "session_id": prepared["session_id"], "request_id": prepared["request_id"],
            "stream_id": prepared["stream_id"],
            **({"final_sequence": sequence, "final_frame_index": frames} if action == "finish" else {})}


@pytest.fixture
async def ready_worker():
    native, writer, client, overlay = await native_controller()
    client.connect()
    client.first_mix()
    worker = AudioWorker(native.mixer, native=native)
    try:
        yield worker, native, writer, client, overlay
    finally:
        await worker.close()


async def test_preparation_gates_first_pcm_on_exact_output_readiness_without_ducking():
    native, _writer, client, overlay = await native_controller()
    worker = AudioWorker(native.mixer, native=native)
    preparing = asyncio.create_task(worker.prepare_pcm(opening(), 0.17))
    try:
        await asyncio.wait_for(client.prepare_entered.wait(), 1)
        assert not preparing.done() and not overlay.sent and not overlay.controls
        early = media({**opening(), "stream_id": worker.session.pcm.stream_id})
        with pytest.raises(RpcError, match="not ready"):
            await deliver(worker, early)
        client.connect()
        client.first_mix()
        prepared = await asyncio.wait_for(preparing, 1)
        assert prepared["format"] == "s16le" and prepared["max_samples"] == 960
        assert not overlay.sent and not overlay.controls
        with pytest.raises(RpcError, match="Another speech producer"):
            await worker.prepare_pcm(opening(), 0.17)
        ack = await deliver(worker, media(prepared, samples=1))
        assert ack["admitted_frames"] == 1
        assert overlay.sent[0][:3] == (struct.pack("<h", 1000), 1, 0.17)
        assert ack["first_pcm_admitted_monotonic_ns"] > 0
        assert ack["pcm_clock"].endswith("admission_not_acoustic")
    finally:
        await worker.close()
        await asyncio.gather(preparing, return_exceptions=True)


async def test_pcm_during_music_does_not_replace_source_calendar_or_phone_owner(ready_worker):
    from shiri.runtime.native import NativeHandle
    worker, native, writer, client, overlay = ready_worker
    handle = NativeHandle(native)
    grant = await native.begin(begin(), handle)
    now = time.monotonic_ns()
    await native.message(pcm(grant, monotonic_before_ns=now - 200, monotonic_after_ns=now), handle)
    owner, route, operations = native.actor.snapshot()["owner"], native.mixer.route, native.operation_generation
    source_calls = [entry for entry in client.requests if entry[1] == "/api/player/shiri-source"]
    prepared = await worker.prepare_pcm(opening(), 0.17)
    for index in range(3):
        await deliver(worker, media(prepared, sequence=index + 1, frame_index=index * 960))
        await worker.tick(0.01)
    await worker._release(worker.session)
    assert native.actor.snapshot()["owner"] == owner and native.actor.owns(handle.token)
    assert native.mixer.route == route and native.operation_generation == operations
    assert [entry for entry in client.requests if entry[1] == "/api/player/shiri-source"] == source_calls
    assert len(overlay.sent) == 3 and writer.packets


async def test_quiet_pcm_never_acquires_audible_lease(ready_worker):
    worker, _native, _writer, _client, _overlay = ready_worker
    prepared = await worker.prepare_pcm(opening(), 0.17)
    ack = await deliver(worker, media(prepared, value=0))
    await worker.tick(0.01)
    assert ack["first_audible_pcm_admitted_monotonic_ns"] is None
    assert worker.session.last_audible == 0


async def test_tiny_eof_accounts_for_tail_and_uses_exact_finish(ready_worker):
    worker, _native, _writer, client, overlay = ready_worker
    prepared = await worker.prepare_pcm(opening(), 0.17)
    old = worker.session
    await deliver(worker, media(prepared, samples=1))
    completed = await worker.direct_pcm(ending(prepared, frames=1))
    assert completed["finished"] and completed["admitted_frames"] == 1
    assert old.disposed and old.natural_eof and worker.session is None
    assert overlay.sent[0][0] == struct.pack("<h", 1000)
    action = client.requests[-1][2]
    assert action["action"] == "finish"
    assert action["speech_id"] == native_preparation_id(old)
    assert not worker._disposals


def native_preparation_id(session):
    return session.preparation.result()["speech_startup_identity"]["speech_id"]


async def test_stale_stream_or_wrong_request_cannot_cancel_or_inject_successor(ready_worker):
    worker, _native, _writer, _client, overlay = ready_worker
    first = await worker.prepare_pcm(opening(), 0.17)
    await worker._release(worker.session)
    second = await worker.prepare_pcm(opening(), 0.17)
    assert first["stream_id"] != second["stream_id"]
    for stale in (media(first), ending(first, sequence=0, frames=0),
                  {**media(second), "request_id": "other-text"}):
        with pytest.raises(RpcError) as error:
            await worker.direct_pcm(stale, data=stale.get("data"))
        assert error.value.code == "session_conflict"
    assert worker.session.pcm.stream_id == second["stream_id"] and not overlay.sent
    await deliver(worker, media(second))
    assert len(overlay.sent) == 1


@pytest.mark.parametrize("mutation", [
    {"sequence": 0}, {"sequence": True}, {"frame_index": 1}, {"frame_index": False},
    {"data": "not-bytes"}, {"data": b"\0"}, {"data": b"\0" * 1922},
])
async def test_malformed_pcm_refuses_atomically_without_advancing(ready_worker, mutation):
    worker, _native, _writer, _client, overlay = ready_worker
    prepared = await worker.prepare_pcm(opening(), 0.17)
    with pytest.raises(RpcError) as error:
        await deliver(worker, {**media(prepared), **mutation})
    assert error.value.code == "invalid_media"
    assert worker.session.pcm.frames == 0 and not overlay.sent
    await deliver(worker, media(prepared))
    with pytest.raises(RpcError):
        await deliver(worker, media(prepared))
    assert worker.session.pcm.frames == 960 and len(overlay.sent) == 1


async def test_wrong_eof_does_not_discard_accepted_audio(ready_worker):
    worker, _native, _writer, _client, _overlay = ready_worker
    prepared = await worker.prepare_pcm(opening(), 0.17)
    await deliver(worker, media(prepared))
    with pytest.raises(RpcError, match="every admitted"):
        await worker.direct_pcm(ending(prepared, frames=959))
    assert worker.session.pcm.frames == 960 and not worker.session.natural_eof


@pytest.mark.parametrize("fault", ["burst", "stall"])
async def test_delivery_overrun_or_stall_cancels_exact_voice_and_reports_failure(ready_worker, fault):
    worker, _native, _writer, client, overlay = ready_worker
    prepared = await worker.prepare_pcm(opening(), 0.17)
    current = worker.session
    clock = [time.monotonic_ns()]
    current.pcm.now_ns = lambda: clock[0]
    accepted = 1 if fault == "stall" else MAX_LEAD_NS // 20_000_000
    for index in range(accepted):
        await deliver(worker, media(prepared, sequence=index + 1, frame_index=index * 960))
    if fault == "stall":
        clock[0] = current.pcm.next_ns + MAX_STALL_NS + 1
    with pytest.raises(RpcError) as error:
        await deliver(worker, media(prepared, sequence=accepted + 1, frame_index=accepted * 960))
    assert error.value.code == ("media_underrun" if fault == "stall" else "media_overrun")
    await worker._dispose(current)
    assert worker.session is None and current.disposed and len(overlay.sent) == accepted
    assert client.requests[-1][2]["action"] == "cancel"


async def test_output_refusal_is_visible_and_cleans_up_without_success_ack(ready_worker):
    worker, _native, _writer, client, overlay = ready_worker
    prepared = await worker.prepare_pcm(opening(), 0.17)
    current = worker.session
    overlay.push = lambda *_args: False
    with pytest.raises(RpcError) as error:
        await deliver(worker, media(prepared))
    assert error.value.code == "audio_unavailable"
    await worker._dispose(current)
    assert current.pcm.frames == 0 and client.requests[-1][2]["action"] == "cancel"


async def test_source_generation_change_refuses_followup_pcm_and_preserves_new_owner(ready_worker):
    worker, native, _writer, client, overlay = ready_worker
    prepared = await worker.prepare_pcm(opening(), 0.17)
    current = worker.session
    await deliver(worker, media(prepared))
    native.operation_generation += 1
    route = native.mixer.route
    with pytest.raises(RpcError, match="source changed"):
        await deliver(worker, media(prepared, sequence=2, frame_index=960))
    await worker._dispose(current)
    assert len(overlay.sent) == 1 and native.mixer.route == route
    assert client.requests[-1][2]["action"] == "cancel"


async def test_unexpected_output_exception_retires_exact_voice(ready_worker):
    worker, _native, _writer, client, overlay = ready_worker
    prepared = await worker.prepare_pcm(opening(), 0.17)
    current = worker.session
    def failed(*_args):
        raise OSError("fixture lost its private channel")
    overlay.push = failed
    with pytest.raises(RpcError) as error:
        await deliver(worker, media(prepared))
    assert error.value.code == "audio_unavailable"
    await worker._dispose(current)
    assert current.pcm.frames == 0 and client.requests[-1][2]["action"] == "cancel"


@pytest.mark.parametrize("same_identity", [False, True])
async def test_rtc_and_pcm_share_one_speech_admission(ready_worker, same_identity):
    worker, _native, _writer, _client, _overlay = ready_worker
    rtc = SpeechSession("rtc-owner", "rtc-request", FakePeer(), 0.2)
    worker.session = rtc
    with pytest.raises(RpcError) as error:
        await worker.prepare_pcm(opening(), 0.17)
    assert error.value.code == "session_conflict" and worker.session is rtc
    worker.session = None
    await worker.prepare_pcm(opening(), 0.17)
    # RTC validation still happens before its shared session admission.
    from aiortc import RTCPeerConnection
    peer = RTCPeerConnection()
    try:
        peer.addTransceiver("audio", direction="sendonly")
        offer = await peer.createOffer()
        identity = ({"session": opening()["session_id"], "request_id": opening()["request_id"]}
                    if same_identity else {"session": "rtc-owner"})
        with pytest.raises(RpcError) as error:
            await worker.dispatch("speech", request(offer.sdp, **identity))
        assert error.value.code == "session_conflict" and worker.session.pcm is not None
    finally:
        await peer.close()


async def test_prepare_cancellation_retains_owned_backend_retirement():
    native, _writer, client, overlay = await native_controller()
    worker = AudioWorker(native.mixer, native=native)
    task = asyncio.create_task(worker.prepare_pcm(opening(), 0.17))
    try:
        await asyncio.wait_for(client.prepare_entered.wait(), 1)
        current = worker.session
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await worker._dispose(current)
        await asyncio.sleep(0)  # The retained disposal's completion callback releases its slot.
        assert current.disposed and worker.session is None and not overlay.sent
        assert not worker._disposals
    finally:
        await worker.close()


async def test_cancel_during_executed_begin_keeps_cleanup_owned_and_fences_successor():
    async with owned_worker() as (worker, native, _writer, client, overlay, gates, _peer, _handle):
        gates.begin_reply.clear()
        gates.cancel_reply.clear()
        preparing = asyncio.create_task(worker.prepare_pcm(opening(), 0.17))
        await asyncio.wait_for(gates.begin_executed.wait(), 1)
        current = worker.session
        try:
            preparing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await preparing
            assert not native.mixer.speech_preparation.begin_task.done()
            assert not gates.begin_dependency_cancelled and not overlay.sent
            with pytest.raises(RpcError, match="retirement"):
                await worker.prepare_pcm(opening("successor", "text-2"), 0.17)
            gates.begin_reply.set()
            await asyncio.wait_for(gates.cancel_executed.wait(), 1)
            assert not current.disposed and not overlay.sent
            gates.cancel_reply.set()
            await worker._dispose(current)
            await asyncio.sleep(0)
            assert current.disposed and client.requests[-1][2]["action"] == "cancel"
            fresh = await worker.prepare_pcm(opening("successor", "text-2"), 0.17)
            assert fresh["stream_id"] != current.pcm.stream_id
        finally:
            gates.unblock()
            await asyncio.gather(preparing, return_exceptions=True)


async def test_private_rpc_binary_pcm_roundtrip_and_cancel(ready_worker):
    worker, _native, _writer, _client, overlay = ready_worker
    with TemporaryDirectory(prefix="shiri-pcm-", dir="/tmp") as directory:
        path = Path(directory) / "audio.sock"
        server = await serve_rpc(path, worker.dispatch)
        try:
            channel = await open_speech(path, opening())
            ack = await channel.send_pcm(struct.pack("<h", 1000) * 960, 1, 0)
            assert ack["next_sequence"] == 2 and ack["next_frame_index"] == 960
            await channel.close()
            assert worker.session is None and len(overlay.sent) == 1
        finally:
            server.close()
            await server.wait_closed()


@pytest.mark.parametrize("action", ["prepare-pcm", "pcm", "finish"])
async def test_json_pcm_actions_are_not_an_audio_control_path(ready_worker, action):
    worker, *_ = ready_worker
    with pytest.raises(RpcError, match="Unknown speech action"):
        await worker.dispatch("speech", {**opening(), "action": action})
    assert worker.session is None


def test_zero_audio_tail_is_immediate_and_counter_receipt_is_not_acoustic():
    stream = PcmSpeech(now_ns=lambda: 100)
    stream.finish({"stream_id": stream.stream_id, "final_sequence": 0, "final_frame_index": 0})
    assert stream.tail_seconds() == 0 and stream.receipt()["admitted_frames"] == 0
