"""Actual player handshake consumers, bounded early PCM and exact RTC cleanup."""

import asyncio
from dataclasses import replace
from array import array
from pathlib import Path
import time
from uuid import uuid4

import pytest

from shiri.rpc import RpcError
from shiri.runtime.audio import AudioWorker
from shiri.runtime.audio import SpeechSession
from shiri.runtime.native import NativeController, NativeHandle, NativeMixer
from shiri.runtime.speech_startup import PREFIX_MAX_AGE_NS, PREFIX_MAX_FRAMES, SpeechPreparation
from test_audio import FakePeer, request
from test_native_audio import Writer, begin, pcm


async def test_setup_task_delayed_past_absolute_deadline_never_prearms_one_output():
    native, writer, client, overlay = await native_controller()
    clock = [time.monotonic_ns()]
    native.mixer.now_ns = lambda: clock[0]
    identity = object()
    prep = native.begin_speech(identity)
    clock[0] = prep.deadline_ns
    try:
        with pytest.raises(RpcError):
            await native.prepare_speech(identity, prep)
        assert client.requests[-1][1] == "/api/player/shiri-source"  # only prior initialize barrier
        assert not any(path == "/api/player/shiri-speech-ready" for _method, path, _body in client.requests)
        assert not writer.packets and not overlay.sent
        assert prep.error == "setup_deadline_before_request"
    finally:
        native.retire_speech(identity)
        await native.close()


@pytest.mark.parametrize("queued", [True, False])
def test_output_refusal_keeps_original_exception_and_first_error_after_exact_retirement(queued):
    clock = [1_000_000_000]
    prep = SpeechPreparation(object(), now_ns=lambda: clock[0])
    failure = RpcError("audio_unavailable", "Output admission refused")

    def refused(*_args):
        raise failure

    if queued:
        prep.push(b"12", 1, 0.2, refused)
        with pytest.raises(RpcError) as result:
            prep.ready({"prepared_monotonic_ns": clock[0], "mixed_monotonic_ns": clock[0]}, refused)
    else:
        prep.ready({"prepared_monotonic_ns": clock[0], "mixed_monotonic_ns": clock[0]}, refused)
        with pytest.raises(RpcError) as result:
            prep.push(b"12", 1, 0.2, refused)
    assert result.value is failure and prep.phase == "failed" and not prep.prefix
    prep.retire()
    assert prep.receipt()["speech_startup_error"] == "prepared_output_rejected"


class Overlay:
    def __init__(self):
        self.room = uuid4().bytes
        self.launch = uuid4().bytes
        self.sent = []
        self.gain = 1
        self.controls = []
        self.owner = None

    def begin(self, speech_id, *, attack_ms=0, release_ms=0):
        self.owner = speech_id
        self.envelope = (attack_ms, release_ms)

    def retire(self, speech_id):
        if self.owner == speech_id:
            self.owner = None

    def set_gain(self, gain):
        self.gain = gain

    def push(self, data, samples):
        self.sent.append((data, samples, self.gain, time.monotonic_ns()))
        return True

    def control(self, active, gain):
        self.controls.append((active, gain))
        return True

    def health(self):
        return {}

    def close(self):
        pass


class Client:
    def __init__(self):
        self.requests = []
        self.prepare_entered = asyncio.Event()
        self.connected = asyncio.Event()
        self.mixed = asyncio.Event()
        self.prepared_ns = 0
        self.first_mix_ns = 0
        self.corrupt = None

    async def request(self, method, path, *, json):
        self.requests.append((method, path, dict(json)))
        if path != "/api/player/shiri-speech-ready":
            return dict(json)
        if json["action"] in {"cancel", "finish"}:
            return {**json, "connected": False, "ready": False,
                    "prepared_monotonic_ns": 0, "mixed_monotonic_ns": 0, "output_count": 0}
        if json["action"] == "prepare":
            self.prepare_entered.set()
            await self.connected.wait()
            self.prepared_ns = time.monotonic_ns()
        reply = {
            **json,
            "connected": True,
            "ready": self.mixed.is_set(),
            "prepared_monotonic_ns": self.prepared_ns,
            "mixed_monotonic_ns": self.first_mix_ns,
            "output_count": 1,
        }
        if self.mixed.is_set():
            reply["mixed_monotonic_ns"] = time.monotonic_ns()
        if self.corrupt:
            self.corrupt(reply)
        return reply

    def connect(self):
        self.connected.set()

    def first_mix(self):
        self.first_mix_ns = time.monotonic_ns()
        self.mixed.set()


async def test_public_health_receipt_binds_exact_current_api_session_and_authenticated_ack():
    native, writer, client, overlay = await native_controller()
    peer = FakePeer()
    worker = AudioWorker(native.mixer, native=native, peer_factory=lambda: peer)
    current = SpeechSession("finite-session", "finite-request", peer, 0.1)
    worker.session = current
    prep = native.begin_speech(current)
    client.connect()
    client.first_mix()
    try:
        await native.prepare_speech(current, prep)
        health = await worker.dispatch("health", {})
        assert health["speech_session_id"] == current.session_id
        assert health["speech_startup_identity"] == {
            "speech_id": prep.speech_id,
            "session_id": current.session_id,
            "request_id": current.request_id,
        }
        ack = health["speech_startup_authenticated_ready_ack"]
        assert ack == prep.backend_reply
        assert ack["speech_id"] == prep.speech_id and ack["action"] == "begin"
        assert ack["room_id"] == overlay.room.hex() and ack["launch_generation"] == overlay.launch.hex()
        assert ack["mixed_monotonic_ns"] == health["speech_startup_first_mix_monotonic_ns"]
        assert ack["prepared_monotonic_ns"] == health["speech_startup_prepared_monotonic_ns"]
        ack["speech_id"] = "f" * 32  # callers cannot mutate retained authentication evidence
        assert (await worker.dispatch("health", {}))["speech_startup_authenticated_ready_ack"][
            "speech_id"
        ] == prep.speech_id
        await worker._release(current)
        retired = await worker.dispatch("health", {})
        assert retired["speech_session_id"] is None and retired["speech_startup_phase"] == "retired"
        successor = SpeechSession("successor", "new-request", FakePeer(), 0.7)
        worker.session = successor
        next_prep = native.begin_speech(successor)
        fresh = await worker.dispatch("health", {})
        assert fresh["speech_startup_identity"]["speech_id"] == next_prep.speech_id != prep.speech_id
        assert fresh["speech_startup_authenticated_ready_ack"] is None
    finally:
        await worker.close()


def test_retirement_keeps_first_failure_and_original_receive_evidence_without_stale_ready_claim():
    clock = [1_000_000_000]
    prep = SpeechPreparation(object(), now_ns=lambda: clock[0])
    prep.push(b"12", 1, 0.2, lambda *_args: None)
    clock[0] += PREFIX_MAX_AGE_NS
    with pytest.raises(RpcError):
        prep.ready({"prepared_monotonic_ns": clock[0], "mixed_monotonic_ns": clock[0]}, lambda *_args: None)
    prep.retire()
    receipt = prep.receipt()
    assert receipt["speech_startup_phase"] == "retired" and receipt["speech_startup_error"] == "prefix_age"
    assert receipt["speech_startup_original_first_received_monotonic_ns"] == 1_000_000_000
    assert (
        receipt["speech_startup_authenticated_ready_ack"] is None
        and receipt["speech_startup_pending_frames"] == 0
    )


async def native_controller(*, horizon=1_000_000_000, lead=500):
    overlay = Overlay()
    writer = Writer()
    client = Client()
    mixer = NativeMixer(
        Path("/unused"), writer=writer, speech_output=overlay, relay_delay_ns=horizon, output_buffer_ms=lead
    )
    native = NativeController(str(uuid4()), mixer, client)
    await native.initialize()
    return native, writer, client, overlay


@pytest.fixture
async def valid_sdp():
    from aiortc import RTCConfiguration, RTCPeerConnection

    peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    peer.addTransceiver("audio", direction="sendonly")
    try:
        yield (await peer.createOffer()).sdp
    finally:
        await peer.close()


async def wait_for(predicate):
    async def run():
        while not predicate():  # noqa: ASYNC110 - inspect real task lifecycle under a1sbound
            await asyncio.sleep(0)

    await asyncio.wait_for(run(), 1)


async def test_delayed_outputs_and_first_mix_gate_whole_prefix_and_original_receive_age():
    native, writer, client, overlay = await native_controller()
    identity = object()
    prep = native.begin_speech(identity)
    task = asyncio.create_task(native.prepare_speech(identity, prep))
    try:
        await client.prepare_entered.wait()
        await asyncio.sleep(0.025)  # connection establishment has no bed/data
        assert not overlay.sent and not writer.packets
        client.connect()
        await wait_for(lambda: prep.phase == "waiting_for_mix")
        native.mixer.tick(music_active=False, speech_active=False, duck_gain=0.2, elapsed=0.01)
        assert not writer.packets  # Exact C readiness creates its own output-only bed.
        voice = array("h", [900] * 960).tobytes()
        native.mixer.set_speech_gain(0.1)
        native.mixer.push_speech(voice, 960)
        assert not overlay.sent and prep.frames == 960
        await asyncio.sleep(0.025)
        assert not overlay.sent
        client.first_mix()
        receipt = await task
        assert len(overlay.sent) == 1 and overlay.sent[0][:3] == (voice, 960, 0.1)
        assert receipt["speech_startup_maximum_prefix_wait_ns"] >= 20_000_000
        assert (
            receipt["speech_startup_original_first_received_monotonic_ns"]
            < receipt["speech_startup_released_monotonic_ns"]
        )
        assert receipt["speech_startup_performance_qualified"] is False
    finally:
        native.retire_speech(identity)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await native.close()


@pytest.mark.parametrize("failure", ["capacity", "age"])
def test_unexpected_early_rtp_rejects_entire_prefix_before_one_sample_is_sent(failure):
    clock = [1_000_000_000]
    sent = []
    prep = SpeechPreparation(object(), now_ns=lambda: clock[0])
    prep.push(bytes(1920), 960, 0.1, lambda *args: sent.append(args))
    if failure == "capacity":
        for _ in range(9):
            prep.push(bytes(1920), 960, 0.1, lambda *args: sent.append(args))
        assert prep.frames == PREFIX_MAX_FRAMES
        with pytest.raises(RpcError):
            prep.push(bytes(2), 1, 0.1, lambda *args: sent.append(args))
    else:
        clock[0] += PREFIX_MAX_AGE_NS
        with pytest.raises(RpcError):
            prep.ready(
                {"prepared_monotonic_ns": clock[0], "mixed_monotonic_ns": clock[0]},
                lambda *args: sent.append(args),
            )
    assert not sent and not prep.prefix and prep.frames == 0 and prep.phase == "failed"


def test_prefix_keeps_each_gain_and_old_retirement_cannot_discard_successor():
    clock = [1_000_000_000]
    mixer = NativeMixer(Path("/unused"), writer=Writer(), speech_output=Overlay(), now_ns=lambda: clock[0])
    old = object()
    prep = SpeechPreparation(old, now_ns=lambda: clock[0])
    mixer.begin_speech_preparation(prep)
    prep.push(b"11", 1, 0.1, lambda *args: None)
    prep.push(b"22", 1, 0.7, lambda *args: None)
    sent = []
    prep.ready(
        {"prepared_monotonic_ns": clock[0], "mixed_monotonic_ns": clock[0]}, lambda *args: sent.append(args)
    )
    assert sent == [(b"11", 1, 0.1), (b"22", 1, 0.7)]
    mixer.retire_speech_preparation(old)
    new = object()
    successor = SpeechPreparation(new, now_ns=lambda: clock[0])
    mixer.begin_speech_preparation(successor)
    successor.push(b"33", 1, 0.3, lambda *args: None)
    mixer.retire_speech_preparation(old)
    assert mixer.speech_preparation is successor and successor.frames == 1 and not successor.retired


@pytest.mark.parametrize(
    "field,value",
    [
        ("speech_id", "f" * 32),
        ("generation", True),
        ("ready", 1),
        ("output_count", True),
        ("mixed_monotonic_ns", 1),
    ],
)
async def test_mismatched_or_old_first_mix_receipt_fails_before_pcm(field, value):
    native, writer, client, overlay = await native_controller()
    identity = object()
    prep = native.begin_speech(identity)
    client.connect()
    client.first_mix()
    client.corrupt = lambda reply: reply.__setitem__(field, value)
    try:
        with pytest.raises(RpcError):
            await native.prepare_speech(identity, prep)
        assert not overlay.sent and prep.phase == "failed"
    finally:
        native.retire_speech(identity)
        await native.close()


async def test_canceled_or_source_replaced_setup_retains_no_prefix_or_successor_side_effects():
    native, writer, client, overlay = await native_controller()
    identity = object()
    prep = native.begin_speech(identity)
    task = asyncio.create_task(native.prepare_speech(identity, prep))
    await client.prepare_entered.wait()
    native.mixer.push_speech(bytes(1920), 960)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert prep.retired and not prep.prefix and not overlay.sent
    successor = object()
    new = native.begin_speech(successor)
    client.connect()
    task = asyncio.create_task(native.prepare_speech(successor, new))
    await wait_for(lambda: new.phase == "waiting_for_mix")
    native.operation_generation += 1
    client.first_mix()
    with pytest.raises(RpcError):
        await task
    assert not overlay.sent and new.phase == "failed"
    await native.close()


async def test_bounded_setup_timeout_discards_prefix_without_timer_or_source_mutation(monkeypatch):
    import shiri.runtime.speech_startup as startup

    monkeypatch.setattr(startup, "SETUP_SECONDS", 0.03)
    native, writer, client, overlay = await native_controller()
    identity = object()
    prep = native.begin_speech(identity)
    began = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await native.prepare_speech(identity, prep)
    assert time.monotonic() - began < 0.3 and not writer.packets and not overlay.sent
    assert prep.phase == "failed" and prep.error == "setup_request_or_deadline"
    await native.close()


@pytest.mark.parametrize("horizon,lead", [(1_000_000_000, 500), (2_750_000_000, 2250)])
async def test_connected_same_peer_quiet_speech_never_fills_program_fifo_then_retires(
    horizon, lead
):
    native, writer, client, overlay = await native_controller(horizon=horizon, lead=lead)
    identity = object()
    prep = native.begin_speech(identity)
    client.connect()
    client.first_mix()
    await native.prepare_speech(identity, prep)
    base = time.monotonic_ns()
    clock = [base]
    native.mixer.now_ns = lambda: clock[0]
    for i in range(0, 400):
        clock[0] = base + i * 20_000_000
        native.mixer.push_speech(bytes(1920), 960)
        native.mixer.tick(music_active=False, speech_active=False, duck_gain=0.2, elapsed=0.02)
        assert not writer.closed
    assert not writer.packets and native.actor.snapshot()["owner"] is None
    assert len(overlay.sent) == 400 and sum(packet[1] for packet in overlay.sent) == 400 * 960
    native.retire_speech(identity)
    clock[0] += 10_000_000_000
    native.mixer.tick(music_active=False, speech_active=False, duck_gain=0.2, elapsed=0.02)
    assert not writer.packets and prep.retired
    await native.close()


async def test_recent_active_native_mix_requires_no_prepare_or_program_mutation():
    native, writer, client, overlay = await native_controller()
    handle = NativeHandle(native)
    grant = await native.begin(begin(), handle)
    now = time.monotonic_ns()
    original = replace(
        pcm(grant),
        clock_sample_ns=now - 5_000_000_000,
        presentation_ns=now - 4_850_000_000,
        monotonic_before_ns=now - 200,
        monotonic_after_ns=now,
    )
    await native.message(original, handle)
    initial = writer.packets[-1]
    token = handle.token
    identity = object()
    prep = native.begin_speech(identity)
    client.first_mix()
    await native.prepare_speech(identity, prep)
    assert not prep.idle and not prep.output_bed and not overlay.sent and len(writer.packets) == 1
    assert native.actor.owns(token) and writer.packets == [initial]
    assert [request[2]["action"] for request in client.requests[2:]] == ["begin"]
    assert sum(request[2]["action"] == "begin" for request in client.requests[2:]) == 1
    await native.close()


@pytest.mark.parametrize("previous_pcm", [False, True])
async def test_paused_phone_prepares_output_bed_without_native_program_writes(previous_pcm):
    native, writer, client, overlay = await native_controller()
    handle = NativeHandle(native)
    grant = await native.begin(begin(), handle)
    if previous_pcm:
        now = time.monotonic_ns()
        original = replace(pcm(grant), clock_sample_ns=now - 5_000_000_000,
                           presentation_ns=now - 4_850_000_000,
                           monotonic_before_ns=now - 200, monotonic_after_ns=now)
        await native.message(original, handle)
    # Only the retained source is present: no fresh native music/mix proof.
    native.mixer._last_native_ns = time.monotonic_ns() - 31_000_000_000
    packets = list(writer.packets)
    token, route, operation = handle.token, native.mixer.route, native.operation_generation
    identity = object()
    prep = native.begin_speech(identity)
    task = asyncio.create_task(native.prepare_speech(identity, prep))
    await client.prepare_entered.wait()
    assert prep.output_bed and not prep.idle
    native.mixer.tick(music_active=True, speech_active=False, duck_gain=0.2, elapsed=0.02)
    assert writer.packets == packets
    client.connect()
    await wait_for(lambda: prep.phase == "waiting_for_mix")
    client.first_mix()  # Hardware edge: actual backend output-only mix receipt.
    await task
    assert prep.phase == "ready" and native.actor.owns(token)
    assert native.mixer.route == route and native.operation_generation == operation
    assert writer.packets == packets and not overlay.sent
    actions = [body["action"] for _method, path, body in client.requests if path == "/api/player/shiri-speech-ready"]
    assert actions[0] == "begin" and "prepare" in actions and actions[-1] == "begin"
    assert "observe" not in actions and "ready" not in actions
    assert all(body["session_id"] == grant.session.hex() for _method, path, body in client.requests
               if path == "/api/player/shiri-speech-ready")
    native.retire_speech(identity)
    await native.close()


async def test_offer_preparation_failure_and_cancellation_before_first_task_instruction_close_exact_peer(
    valid_sdp,
):
    for cancel in (False, True):
        native, writer, client, overlay = await native_controller()
        peer = FakePeer(blocked=True)
        worker = AudioWorker(native.mixer, native=native, peer_factory=lambda peer=peer: peer)
        if not cancel:
            native.begin_speech = lambda current, **kwargs: (_ for _ in ()).throw(
                RpcError("audio_unavailable", "injected")
            )
        task = asyncio.create_task(worker.dispatch("speech", request(valid_sdp)))
        try:
            if cancel:
                await peer.entered.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(RpcError):
                    await task
            await wait_for(lambda peer=peer: peer.close_calls == 1)
            assert worker.session is None and not overlay.sent
            await wait_for(lambda worker=worker: not worker._disposals)
        finally:
            await worker.close()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("natural", [True, False])
async def test_actual_av_early_eof_waits_first_mix_but_explicit_close_discards_own_prefix(natural):
    import av
    from aiortc.mediastreams import MediaStreamError
    from shiri.runtime.audio import SpeechSession

    native, writer, client, overlay = await native_controller()
    peer = FakePeer()
    worker = AudioWorker(native.mixer, native=native, peer_factory=lambda peer=peer: peer)
    current = SpeechSession("early-track", "one", peer, 0.1)
    worker.session = current
    prep = native.begin_speech(current)
    current.preparation = asyncio.create_task(native.prepare_speech(current, prep))
    frame = av.AudioFrame(format="s16", layout="mono", samples=480)
    frame.sample_rate = 48000
    data = array("h", [1000] * 480).tobytes()
    frame.planes[0].update(data)

    class Track:
        calls = 0

        async def recv(self):
            self.calls += 1
            if self.calls == 1:
                return frame
            raise MediaStreamError

    track = Track()
    current.receiver = asyncio.create_task(worker._receive(current, track))
    try:
        await client.prepare_entered.wait()
        await wait_for(lambda: prep.frames == 480)
        await wait_for(lambda: track.calls == 2)
        assert not overlay.sent and not current.receiver.done()
        if natural:
            client.connect()
            await wait_for(lambda: prep.phase == "waiting_for_mix")
            client.first_mix()
            await asyncio.wait_for(current.receiver, 1)
            assert overlay.sent[0][:3] == (data, 480, 0.1)
            assert prep.original_first_received_ns < prep.released_ns and prep.retired
        else:
            await worker.dispatch("speech", request(action="close", session="early-track"))
            client.connect()
            client.first_mix()
            await asyncio.sleep(0)
            assert not overlay.sent and not prep.prefix and prep.retired
        assert worker.session is None
    finally:
        await worker.close()
        await asyncio.gather(current.receiver, current.preparation, return_exceptions=True)


@pytest.mark.parametrize("fail_negotiation", [True, False])
async def test_output_ready_while_sdp_pending_never_releases_a_failed_offer_prefix(
    valid_sdp, fail_negotiation
):
    native, writer, client, overlay = await native_controller()
    peer = FakePeer(blocked=True)
    worker = AudioWorker(native.mixer, native=native, peer_factory=lambda peer=peer: peer)
    task = asyncio.create_task(worker.dispatch("speech", request(valid_sdp)))
    try:
        await peer.entered.wait()
        client.connect()
        client.first_mix()
        prep = native.mixer.speech_preparation
        await wait_for(lambda: prep.phase == "waiting_for_mix")
        data = array("h", [700] * 480).tobytes()
        native.mixer.push_speech(data, 480)
        await asyncio.sleep(0.02)
        assert not overlay.sent and prep.frames == 480 and not task.done()
        if fail_negotiation:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await wait_for(lambda worker=worker: not worker._disposals)
            assert not overlay.sent and prep.retired and not prep.prefix
        else:
            peer.proceed.set()
            answer = await task
            assert answer["type"] == "answer" and overlay.sent[0][:3] == (data, 480, 0.28)
            assert prep.phase == "ready"
    finally:
        await worker.close()
        await asyncio.gather(task, return_exceptions=True)


# This fixture uses a genuine private Unix datagram endpoint and the production
# 80byte wire producer. No datagram may precede both exact readiness receipts.
from test_speech_output import endpoint  # noqa: E402,F401


async def test_actual_datagram_is_fresh_only_after_first_mix_and_original_wait_remains_visible(endpoint):  # noqa: F811 - shared real-socket fixture
    from shiri.runtime.speech_output import HEADER, HEADER_BYTES

    sender, server = endpoint
    sender.retire(sender.owner.hex())
    server.setblocking(False)
    sender.now_ns = time.monotonic_ns
    native, writer, client, _overlay = await native_controller()
    native.mixer.speech_output = sender
    identity = object()
    prep = native.begin_speech(identity)
    task = asyncio.create_task(native.prepare_speech(identity, prep))
    try:
        client.connect()
        await wait_for(lambda: prep.phase == "waiting_for_mix")
        voice = array("h", [333] * 960).tobytes()
        native.mixer.set_speech_gain(0.2)
        native.mixer.push_speech(voice, 960)
        with pytest.raises(BlockingIOError):
            server.recv(4096)
        await asyncio.sleep(0.01)
        client.first_mix()
        await task
        packet = server.recv(4096)
        header = HEADER.unpack(packet[:HEADER_BYTES])
        assert packet[HEADER_BYTES:] == voice and header[9] == 960 and header[10] == round(0.2 * 65536)
        assert prep.original_first_received_ns < header[8] and header[8] >= prep.first_mix_ns
        assert prep.maximum_prefix_wait_ns > 5_000_000 and header[5] == 1
    finally:
        native.retire_speech(identity)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await native.close()
