"""Owned speech teardown survives canceled RPCs without touching the program."""
import asyncio
from fractions import Fraction
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import time

import pytest

from shiri.rpc import RpcError, call_rpc, serve_rpc
from shiri.runtime.audio import AudioWorker, SpeechSession
from test_audio import FakePeer, RecordingMixer, request
from test_native_audio import LateSpeech, begin, controller, pcm
from test_speech_startup import native_controller
from shiri.runtime.native import NativeHandle


@pytest.fixture
async def valid_sdp():
    from aiortc import RTCConfiguration, RTCPeerConnection
    peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    peer.addTransceiver("audio", direction="sendonly")
    try:
        yield (await peer.createOffer()).sdp
    finally:
        await peer.close()


@pytest.fixture
def rpc_socket():
    # macOS has a shorter AF_UNIX path limit than pytest's descriptive directory.
    with TemporaryDirectory(prefix="shiri-speech-", dir="/tmp") as directory:
        yield Path(directory) / "audio.sock"


class YieldingPeer(FakePeer):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.close_entered = asyncio.Event()
        self.close_proceed = asyncio.Event()

    async def close(self):
        self.close_calls += 1
        self.close_entered.set()
        await self.close_proceed.wait()
        self.connectionState = "closed"
        callback = self.handlers.get("connectionstatechange")
        if callback:
            await callback()  # Strong synchronous re-entry, not a scheduled mock.


async def native_worker(peer, **kwargs):
    native, writer, client = controller()
    await native.initialize()
    handle = NativeHandle(native)
    grant = await native.begin(begin(), handle)
    await native.message(pcm(grant), handle)
    worker = AudioWorker(native.mixer, native=native, peer_factory=lambda: peer, **kwargs)
    return worker, native, writer, client, handle, grant


def unchanged_program(native, client, handle, token):
    assert native.actor.owns(token) and not handle.closed
    source_requests = [item for item in client.requests if item[1] != "/api/player/shiri-speech-ready"]
    assert len(source_requests) == 2, "Speech must not invoke any source/player/flush command"


async def test_canceled_close_joins_receiver_and_negotiation_finally_without_a_cycle(valid_sdp):
    peer = YieldingPeer(blocked=True, emit_track=True)
    worker, native, _writer, client, handle, _grant = await native_worker(peer)
    negotiation = asyncio.create_task(worker.dispatch("speech", request(valid_sdp)))
    try:
        await asyncio.wait_for(peer.entered.wait(), 1)
        session = worker.session
        await asyncio.sleep(0)
        closing = asyncio.create_task(worker.dispatch("speech", request(action="close")))
        await asyncio.wait_for(peer.close_entered.wait(), 1)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        result = await asyncio.wait_for(asyncio.gather(negotiation, return_exceptions=True), 1)
        assert isinstance(result[0], asyncio.CancelledError)
        assert session.receiver.done() and not session.disposal.done() and not session.disposed
        assert worker.session is None and session.disposal in worker._disposals
        unchanged_program(native, client, handle, handle.token)
        peer.close_proceed.set()
        await asyncio.wait_for(worker._dispose(session), 1)
        assert session.disposed and peer.close_calls == 1 and not worker._disposals
        unchanged_program(native, client, handle, handle.token)
    finally:
        peer.close_proceed.set()
        await worker.close()
        await asyncio.gather(negotiation, return_exceptions=True)


async def test_offer_cancellation_returns_original_cancel_while_cleanup_remains_owned(valid_sdp):
    peer = YieldingPeer(blocked=True, emit_track=True)
    worker, native, _writer, client, handle, _grant = await native_worker(peer)
    negotiating = asyncio.create_task(worker.dispatch("speech", request(valid_sdp)))
    try:
        await asyncio.wait_for(peer.entered.wait(), 1)
        session = worker.session
        negotiating.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(negotiating, .2)
        await asyncio.wait_for(peer.close_entered.wait(), 1)
        assert worker.session is None and not session.disposed and not session.disposal.cancelled()
        unchanged_program(native, client, handle, handle.token)
        peer.close_proceed.set()
        await asyncio.wait_for(worker._dispose(session), 1)
        assert session.receiver.done() and session.negotiation.done() and peer.close_calls == 1
    finally:
        peer.close_proceed.set()
        await worker.close()


@pytest.mark.parametrize("action", ["offer", "close"])
async def test_real_unix_rpc_disconnect_does_not_cancel_owned_teardown(rpc_socket, valid_sdp, action):
    peer = YieldingPeer(blocked=action == "offer", emit_track=True)
    worker, native, _writer, client, handle, _grant = await native_worker(peer)
    socket_path = rpc_socket
    server = await serve_rpc(socket_path, worker.dispatch)
    if action == "close":
        await worker.dispatch("speech", request(valid_sdp))
    rpc = asyncio.create_task(call_rpc(socket_path, "speech", request(valid_sdp) if action == "offer" else request(action="close")))
    try:
        await asyncio.wait_for((peer.entered if action == "offer" else peer.close_entered).wait(), 1)
        session = worker.session if action == "offer" else next(iter(worker._disposals.values()))
        rpc.cancel()
        with pytest.raises(asyncio.CancelledError):
            await rpc
        await asyncio.wait_for(peer.close_entered.wait(), 1)
        health = await call_rpc(socket_path, "health")
        assert health["speech_cleanup_pending"] == 1 and health["ready"] is True
        assert not health["speech_cleanup_error"] and not session.disposed
        unchanged_program(native, client, handle, handle.token)
        peer.close_proceed.set()
        await asyncio.wait_for(worker._dispose(session), 1)
        assert session.disposed and not worker._disposals
        unchanged_program(native, client, handle, handle.token)
    finally:
        peer.close_proceed.set()
        server.close()
        await server.wait_closed()
        await worker.close()
        await asyncio.gather(rpc, return_exceptions=True)


async def test_canceled_worker_close_has_one_owned_bounded_shutdown(valid_sdp):
    peer = YieldingPeer()
    mixer = RecordingMixer()
    worker = AudioWorker(mixer, peer_factory=lambda: peer)
    await worker.dispatch("speech", request(valid_sdp))
    session = worker.session
    closing = asyncio.create_task(worker.close())
    await asyncio.wait_for(peer.close_entered.wait(), 1)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert not worker._shutdown.cancelled() and not session.disposal.cancelled()
    peer.close_proceed.set()
    await asyncio.wait_for(worker.close(), 1)
    assert session.disposed and mixer.closed and peer.close_calls == 1


async def test_worker_shutdown_deadline_retains_pending_resources_and_never_claims_completion():
    peer = YieldingPeer()
    mixer = RecordingMixer()
    worker = AudioWorker(mixer, cleanup_timeout=.03)
    worker.session = session = SpeechSession("owner", "request", peer, .2)
    started = time.monotonic()
    with pytest.raises(RpcError, match="shutdown deadline") as failure:
        await worker.close()
    assert failure.value.code == "audio_unavailable" and time.monotonic() - started < .2
    assert mixer.closed and not session.disposed and not session.disposal.cancelled()
    assert session.disposal in worker._disposals
    health = await worker.dispatch("health", {})
    assert health["speech_ready"] is False and health["speech_cleanup_pending"] == 1
    peer.close_proceed.set()
    await asyncio.wait_for(worker._dispose(session), 1)
    assert session.disposed and not worker._disposals


@pytest.mark.parametrize("expiry", ["media_idle", "continuous_silence"])
async def test_expiry_never_waits_for_peer_close_or_holds_program_ducked(expiry):
    peer = YieldingPeer()
    worker, native, writer, client, handle, grant = await native_worker(peer)
    now = time.monotonic()
    worker.session = session = SpeechSession("owner", "request", peer, .2, created=now - 31,
                                             last_media=now if expiry == "continuous_silence" else 0)
    overlay = LateSpeech(accepted=True)
    native.mixer.speech_output = overlay
    native.mixer.tick(music_active=True, speech_active=True, duck_gain=.2, elapsed=.01)
    assert overlay.controls[-1] == (True, .2)
    try:
        await asyncio.wait_for(worker.tick(.01), .1)
        assert worker.session is None and native.mixer._target_gain == 1
        await asyncio.wait_for(peer.close_entered.wait(), 1)
        assert not session.disposal.done() and not session.disposed
        for sequence in range(1, 31):
            await native.message(pcm(grant, sequence=sequence, frame_index=sequence * 480), handle)
        assert overlay.controls[-1][0] is False
        assert writer.packets[-1].pcm == struct.pack("<960h", *([1000] * 960))
        unchanged_program(native, client, handle, handle.token)
        health = await worker.dispatch("health", {})
        assert health["music_active"] and health["speech_cleanup_pending"] == 1
        peer.close_proceed.set()
        await asyncio.wait_for(worker._dispose(session), 1)
    finally:
        peer.close_proceed.set()
        await worker.close()


async def test_pending_cleanup_cap_refuses_new_peers_without_perturbing_program(valid_sdp):
    peer = YieldingPeer()
    worker, native, _writer, client, handle, _grant = await native_worker(peer, max_pending_cleanup=1)
    try:
        await worker.dispatch("speech", request(valid_sdp))
        session = worker.session
        closing = asyncio.create_task(worker.dispatch("speech", request(action="close")))
        await asyncio.wait_for(peer.close_entered.wait(), 1)
        closing.cancel()
        await asyncio.gather(closing, return_exceptions=True)
        health = await worker.dispatch("health", {})
        assert health["speech_ready"] is False and health["ready"] is True
        with pytest.raises(RpcError, match="still being released") as failure:
            await worker.dispatch("speech", request(valid_sdp, session="successor"))
        assert failure.value.code == "audio_unavailable" and worker.session is None
        unchanged_program(native, client, handle, handle.token)
        peer.close_proceed.set()
        await asyncio.wait_for(worker._dispose(session), 1)
        assert (await worker.dispatch("health", {}))["speech_ready"] is True
        worker.peer_factory = FakePeer
        await worker.dispatch("speech", request(valid_sdp, session="successor"))
        unchanged_program(native, client, handle, handle.token)
    finally:
        peer.close_proceed.set()
        await worker.close()


async def test_failed_cleanup_remains_bounded_and_is_a_speech_only_fault(valid_sdp):
    class FailedPeer(FakePeer):
        async def close(self):
            self.close_calls += 1
            raise OSError("RTC resources could not close")
    peer = FailedPeer()
    worker, native, _writer, client, handle, _grant = await native_worker(peer)
    await worker.dispatch("speech", request(valid_sdp))
    session = worker.session
    with pytest.raises(OSError):
        await worker.dispatch("speech", request(action="close"))
    assert not session.disposed and len(worker._disposals) == 1
    health = await worker.dispatch("health", {})
    assert health["ready"] is True and not health["error"] and health["speech_ready"] is False
    assert health["speech_cleanup_error"] and health["speech_cleanup_pending"] == 0
    with pytest.raises(RpcError) as failure:
        await worker.dispatch("speech", request(valid_sdp, session="successor"))
    assert failure.value.code == "audio_unavailable"
    unchanged_program(native, client, handle, handle.token)
    with pytest.raises(OSError):
        await worker.close()


async def test_real_aiortc_cancelled_close_finishes_ice_and_preserves_native_program():
    from aiortc import AudioStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription
    from av import AudioFrame
    class Tone(AudioStreamTrack):
        def __init__(self):
            super().__init__()
            self.count = 0
        async def recv(self):
            await asyncio.sleep(.02)
            frame = AudioFrame(format="s16", layout="mono", samples=960)
            frame.planes[0].update(struct.pack("<960h", *([1000] * 960)))
            frame.sample_rate, frame.pts, frame.time_base = 48000, self.count, Fraction(1, 48000)
            self.count += 960
            return frame
    native, writer, client, overlay = await native_controller()
    client.connect()
    client.first_mix()
    handle = NativeHandle(native)
    grant = await native.begin(begin(), handle)
    worker = AudioWorker(native.mixer, native=native)
    received = asyncio.Event()
    push = native.mixer.push_speech
    def observed_push(data, samples):
        push(data, samples)
        received.set()
    native.mixer.push_speech = observed_push
    sender = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    sender.addTrack(Tone())
    try:
        await sender.setLocalDescription(await sender.createOffer())
        answer = await worker.dispatch("speech", request(sender.localDescription.sdp, duck_gain=.2))
        await sender.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))
        session = worker.session
        await asyncio.wait_for(received.wait(), 3)
        assert session.last_audible
        await worker.tick(.02)
        token = handle.token
        close_entered = asyncio.Event()
        @session.peer.on("signalingstatechange")
        def signaling_changed():
            if session.peer.signalingState == "closed":
                close_entered.set()
        closing = asyncio.create_task(worker.dispatch("speech", request(action="close")))
        # Inspect the real aiortc closing Future at its actual asynchronous seam.
        await asyncio.wait_for(close_entered.wait(), 1)
        assert session.peer._RTCPeerConnection__isClosed is not None
        assert not closing.done(), "The actual RTC close must be interrupted while in flight"
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        await asyncio.wait_for(worker._dispose(session), 2)
        assert session.disposed and peer_closed(session.peer)
        assert session.receiver.done() and session.negotiation is None
        assert not worker._disposals
        unchanged_program(native, client, handle, token)
        await worker.tick(.02)
        for sequence in range(30):
            now = time.monotonic_ns()
            await native.message(pcm(grant, sequence=sequence, frame_index=sequence * 480,
                                     monotonic_before_ns=now - 200, monotonic_after_ns=now), handle)
        assert overlay.owner is None and overlay.controls[-1][0] is False
        assert writer.packets[-1].pcm == struct.pack("<960h", *([1000] * 960))
        unchanged_program(native, client, handle, token)
    finally:
        await sender.close()
        await worker.close()


def peer_closed(peer):
    return (peer.connectionState == "closed" and peer._RTCPeerConnection__isClosed.done()
            and all(transceiver.receiver.transport.state == "closed"
                    and transceiver.receiver.transport.transport.state == "closed"
                    for transceiver in peer.getTransceivers()))
