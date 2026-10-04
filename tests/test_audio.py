import asyncio
from fractions import Fraction
import math
import struct
import time
from types import SimpleNamespace

import pytest

from shiri.rpc import RpcError
from shiri.runtime.audio import AudioWorker, SpeechSession


class RecordingMixer:
    def __init__(self):
        self.frames = []
        self.ticks = []
        self.closed = False
        self.received = asyncio.Event()

    def push_speech(self, data, samples):
        self.frames.append((data, samples))
        self.received.set()

    def tick(self, **kwargs):
        self.ticks.append(kwargs)

    def close(self):
        self.closed = True

    def health(self):
        return {"ready": not self.closed}


class WaitingTrack:
    kind = "audio"

    async def recv(self):
        await asyncio.Future()


class FakePeer:
    def __init__(self, *, blocked=False, emit_track=False):
        self.connectionState = "new"
        self.handlers = {}
        self.blocked = blocked
        self.emit_track = emit_track
        self.entered = asyncio.Event()
        self.proceed = asyncio.Event()
        self.close_calls = 0
        self.localDescription = SimpleNamespace(sdp="answer-sdp", type="answer")

    def on(self, name):
        def register(callback):
            self.handlers[name] = callback
            return callback
        return register

    async def setRemoteDescription(self, _description):
        if self.emit_track:
            self.handlers["track"](WaitingTrack())
        self.entered.set()
        if self.blocked:
            await self.proceed.wait()

    async def createAnswer(self):
        return self.localDescription

    async def setLocalDescription(self, _description):
        return

    async def close(self):
        self.close_calls += 1
        self.connectionState = "closed"
        # Synchronous re-entry is deliberately stronger than aiortc's scheduled
        # event delivery, and exercises ownership removal before peer teardown.
        if "connectionstatechange" in self.handlers:
            await self.handlers["connectionstatechange"]()


@pytest.fixture
async def valid_sdp():
    from aiortc import RTCConfiguration, RTCPeerConnection
    peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    peer.addTransceiver("audio", direction="sendonly")
    try:
        return (await peer.createOffer()).sdp
    finally:
        await peer.close()


def request(sdp=None, *, session="speaker-owner", request_id="utterance-1", action="offer", **kwargs):
    return {"session_id": session, "request_id": request_id, "action": action,
            **({"sdp": sdp, "type": "offer"} if sdp is not None else {}), **kwargs}


async def test_duplicate_offer_is_idempotent_but_other_producer_cannot_take_over(valid_sdp):
    peer = FakePeer()
    worker = AudioWorker(RecordingMixer(), peer_factory=lambda: peer)
    try:
        response = await worker.dispatch("speech", request(valid_sdp))
        assert await worker.dispatch("speech", request(valid_sdp)) == response
        for payload in (request(valid_sdp, session="different-producer"),
                        request(action="close", session="different-producer")):
            with pytest.raises(RpcError) as failure:
                await worker.dispatch("speech", payload)
            assert failure.value.code == "session_conflict"
        assert worker.session.session_id == "speaker-owner"
        assert peer.close_calls == 0
    finally:
        await worker.close()
    assert peer.close_calls == 1


async def test_close_during_negotiation_and_media_releases_every_task_without_deadlock(valid_sdp):
    peer = FakePeer(blocked=True, emit_track=True)
    worker = AudioWorker(RecordingMixer(), peer_factory=lambda: peer)
    negotiation = asyncio.create_task(worker.dispatch("speech", request(valid_sdp)))
    await asyncio.wait_for(peer.entered.wait(), 1)
    session = worker.session
    await asyncio.sleep(0)
    await asyncio.wait_for(worker.dispatch("speech", request(action="close")), 1)
    results = await asyncio.wait_for(asyncio.gather(negotiation, return_exceptions=True), 1)
    assert isinstance(results[0], asyncio.CancelledError)
    assert worker.session is None and peer.close_calls == 1
    assert session.receiver.done() and session.negotiation.done()
    await worker.close()


async def test_rpc_timeout_cancels_offer_and_does_not_leave_a_room_owned(valid_sdp):
    peer = FakePeer(blocked=True, emit_track=True)
    worker = AudioWorker(RecordingMixer(), peer_factory=lambda: peer)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(worker.dispatch("speech", request(valid_sdp)), timeout=0.03)
    assert worker.session is None
    # The canceled RPC does not own teardown. Join the worker's retained task
    # before asserting actual resource completion rather than assuming it is instant.
    await asyncio.wait_for(worker.close(), 1)
    assert worker.session is None and peer.connectionState == "closed"
    assert peer.close_calls == 1


async def test_simultaneous_worker_shutdown_and_client_close_is_idempotent(valid_sdp):
    peer = FakePeer(blocked=True, emit_track=True)
    mixer = RecordingMixer()
    worker = AudioWorker(mixer, peer_factory=lambda: peer)
    negotiating = asyncio.create_task(worker.dispatch("speech", request(valid_sdp)))
    await asyncio.wait_for(peer.entered.wait(), 1)
    closing = [worker.close(), worker.dispatch("speech", request(action="close"))]
    await asyncio.wait_for(asyncio.gather(*closing), 1)
    await asyncio.wait_for(asyncio.gather(negotiating, return_exceptions=True), 1)
    assert worker.session is None and mixer.closed and peer.close_calls == 1


async def test_late_cleanup_of_old_peer_does_not_release_the_new_owner(valid_sdp):
    peers = []
    def factory():
        peer = FakePeer()
        peers.append(peer)
        return peer
    worker = AudioWorker(RecordingMixer(), peer_factory=factory)
    await worker.dispatch("speech", request(valid_sdp))
    old = worker.session
    await worker.dispatch("speech", request(action="close"))
    await worker.dispatch("speech", request(valid_sdp, session="new-owner", request_id="new-request"))
    await worker._release(old)
    assert worker.session.session_id == "new-owner"
    await worker.close()
    assert all(peer.close_calls == 1 for peer in peers)


@pytest.mark.parametrize("gain", [float("nan"), float("inf"), -0.1, 1.1, True, "0.28"])
async def test_control_identity_and_gain_are_strict(gain):
    worker = AudioWorker(RecordingMixer())
    with pytest.raises(RpcError) as failure:
        await worker.dispatch("speech", request(action="control", duck_gain=gain))
    assert failure.value.code == "invalid_request"


async def test_media_less_peer_expires_and_music_returns_to_normal():
    mixer = RecordingMixer()
    worker = AudioWorker(mixer, idle_seconds=0.01)
    peer = FakePeer()
    worker.session = SpeechSession("owner", "request", peer, 0.1, created=time.monotonic() - 1)
    worker.music_active = True
    await worker.tick(0.01)
    assert worker.session is None
    assert mixer.ticks[-1]["music_active"] and not mixer.ticks[-1]["speech_active"]
    await worker.close()
    assert peer.close_calls == 1


async def test_silent_transport_frames_do_not_hold_music_ducked():
    from av import AudioFrame
    class SilentTrack:
        kind = "audio"
        async def recv(self):
            await asyncio.sleep(0)
            frame = AudioFrame(format="s16", layout="mono", samples=960)
            frame.planes[0].update(bytes(1920))
            frame.sample_rate = 48000
            return frame
    mixer = RecordingMixer()
    worker = AudioWorker(mixer)
    session = SpeechSession("owner", "request", FakePeer(), 0.1)
    worker.session = session
    session.receiver = asyncio.create_task(worker._receive(session, SilentTrack()))
    await asyncio.wait_for(mixer.received.wait(), 1)
    await worker.tick(0.01)
    assert session.last_media > 0
    assert not mixer.ticks[-1]["speech_active"]
    await asyncio.wait_for(worker.close(), 1)


@pytest.mark.parametrize("created", [1.0, 17.0, 100000.0])
async def test_continuous_silence_has_a_bounded_room_lease(monkeypatch, created):
    from shiri.runtime import audio

    # Create the session at a valid monotonic origin, then advance its clock.
    # Subtracting31s from the real host uptime invents a negative origin on a
    # fresh boot, where last_audible's zero sentinel masks that artificial age.
    clock = SimpleNamespace(now=created)
    monkeypatch.setattr(audio, "time", SimpleNamespace(monotonic=lambda: clock.now))
    mixer = RecordingMixer()
    worker = AudioWorker(mixer, idle_seconds=10)
    peer = FakePeer()
    session = SpeechSession("owner", "request", peer, 0.1, created=created, last_media=created)
    worker.session = session
    worker.music_active = True
    try:
        for age in (0.0, 29.0, 30.0):
            clock.now = created+age
            session.last_media = clock.now  # Fresh silence keeps transport alive.
            await worker.tick(0.01)
            assert worker.session is session and peer.close_calls == 0
            assert mixer.ticks[-1]["music_active"] and not mixer.ticks[-1]["speech_active"]
        clock.now = created+30.001
        session.last_media = clock.now
        await worker.tick(0.01)
        assert worker.session is None  # The30s audible lease, not media idle.
        assert mixer.ticks[-1]["music_active"] and not mixer.ticks[-1]["speech_active"]
    finally:
        await worker.close()
    assert peer.close_calls == 1


async def test_multiple_media_sections_and_rejected_audio_are_not_accepted(valid_sdp):
    worker = AudioWorker(RecordingMixer(), peer_factory=FakePeer)
    variants = [valid_sdp + valid_sdp[valid_sdp.index("m=audio"):],
                valid_sdp.replace("m=audio 9 ", "m=audio 0 "),
                valid_sdp.replace("m=audio", "m=video"),
                valid_sdp.replace("a=sendonly", "a=recvonly")]
    for sdp in variants:
        with pytest.raises(RpcError) as failure:
            await worker.dispatch("speech", request(sdp))
        assert failure.value.code == "invalid_request"
        assert worker.session is None
    await worker.close()


async def test_real_aiortc_local_speech_decodes_to_bounded_mono_pcm():
    from aiortc import AudioStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription
    from av import AudioFrame
    class Tone(AudioStreamTrack):
        def __init__(self):
            super().__init__()
            self.samples = 0
        async def recv(self):
            await asyncio.sleep(0.02)
            frame = AudioFrame(format="s16", layout="mono", samples=960)
            values = [int(3000 * math.sin(2 * math.pi * 440 * (self.samples + index) / 48000)) for index in range(960)]
            frame.planes[0].update(struct.pack("<960h", *values))
            frame.sample_rate = 48000
            frame.pts = self.samples
            frame.time_base = Fraction(1, 48000)
            self.samples += 960
            return frame
    sender = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    sender.addTrack(Tone())
    mixer = RecordingMixer()
    worker = AudioWorker(mixer, idle_seconds=2)
    try:
        await sender.setLocalDescription(await sender.createOffer())
        answer = await asyncio.wait_for(worker.dispatch("speech", request(sender.localDescription.sdp)), 5)
        await sender.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))
        await asyncio.wait_for(mixer.received.wait(), 5)
        assert mixer.frames and all(len(data) == count * 2 and 0 < count <= 9600 for data, count in mixer.frames)
        assert any(any(data) for data, _count in mixer.frames)
        await worker.tick(0.02)
        assert mixer.ticks[-1]["speech_active"]
        await asyncio.wait_for(worker.dispatch("speech", request(action="close")), 2)
        assert worker.session is None
    finally:
        await sender.close()
        await asyncio.wait_for(worker.close(), 2)
