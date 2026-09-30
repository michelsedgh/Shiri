import asyncio
from fractions import Fraction
import math
import os
import struct
import time
from types import SimpleNamespace

import pytest

from shiri.rpc import RpcError
from shiri.runtime.audio import AudioWorker, FifoWriter, GstMixer, SpeechSession


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
    assert worker.session is None and peer.connectionState == "closed"
    assert peer.close_calls == 1
    await worker.close()


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
    assert worker.session is None and peer.close_calls == 1
    assert mixer.ticks[-1]["music_active"] and not mixer.ticks[-1]["speech_active"]
    await worker.close()


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


async def test_continuous_silence_has_a_bounded_room_lease():
    mixer = RecordingMixer()
    worker = AudioWorker(mixer, idle_seconds=10)
    peer = FakePeer()
    worker.session = SpeechSession("owner", "request", peer, 0.1,
                                   created=time.monotonic() - 31, last_media=time.monotonic())
    await worker.tick(0.01)
    assert worker.session is None and peer.close_calls == 1
    await worker.close()


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


def test_fifo_reader_absence_never_blocks_or_builds_an_audio_backlog(tmp_path):
    path = tmp_path / "audio.pipe"
    os.mkfifo(path)
    writer = FifoWriter(path)
    before = time.monotonic()
    for _ in range(100):
        writer.write(bytes(3840))
    assert time.monotonic() - before < 0.2
    assert writer.written_bytes == 0 and writer.dropped_bytes == 384000
    assert writer.fd is None and not writer.reader_present


def test_full_fifo_drops_complete_frames_and_reconnects_after_reader_exit(tmp_path):
    path = tmp_path / "audio.pipe"
    os.mkfifo(path)
    reader = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    writer = FifoWriter(path)
    payload = bytes(3840)
    try:
        for _ in range(100):
            writer.write(payload)
        assert writer.dropped_bytes > 0
        data = os.read(reader, 1024 * 1024)
        assert len(data) % 4 == 0
        assert writer.written_bytes + writer.dropped_bytes == 100 * len(payload)
        os.close(reader)
        reader = None
        writer.write(payload)
        assert writer.fd is None and not writer.reader_present
        reader = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        writer.write(payload)
        assert os.read(reader, len(payload)) == payload
    finally:
        if reader is not None:
            os.close(reader)
        writer.close()


def test_fifo_writer_rejects_regular_files_and_symlinks(tmp_path):
    regular = tmp_path / "regular"
    regular.write_bytes(b"untouched")
    symlink = tmp_path / "link"
    symlink.symlink_to(regular)
    for path in (regular, symlink):
        with pytest.raises(RuntimeError):
            FifoWriter(path)
    assert regular.read_bytes() == b"untouched"


def test_ducking_is_bounded_and_recovers_after_speech_stops():
    gains = []
    mixer = GstMixer.__new__(GstMixer)
    mixer._gain, mixer.error = 1.0, None
    mixer.music = SimpleNamespace(set_property=lambda _key, value: gains.append(value))
    mixer.bus = SimpleNamespace(pop_filtered=lambda _types: None)
    mixer.Gst = SimpleNamespace(MessageType=SimpleNamespace(ERROR=1, EOS=2))
    for _ in range(10):
        mixer.tick(music_active=True, speech_active=True, duck_gain=0.28, elapsed=0.01)
    assert gains[-1] == pytest.approx(0.28)
    for _ in range(30):
        mixer.tick(music_active=True, speech_active=False, duck_gain=0.28, elapsed=0.01)
    assert gains[-1] == pytest.approx(1.0)
    assert all(0.28 <= gain <= 1.0 for gain in gains)


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
