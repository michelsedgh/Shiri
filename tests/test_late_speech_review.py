"""Observe the late speech producer through real decoding and Unix datagrams.

The receiver here does not stand in for OwnTone's mixing/presentation proof.
It inspects exactly what the actual AudioWorker sends before a playback tick.
"""

from array import array
import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import socket
import sys
import tempfile
from uuid import uuid4

import pytest

from shiri.runtime.audio import AudioWorker, SpeechSession
from shiri.runtime.native import NativeMixer
from shiri.runtime.speech_output import HEADER, HEADER_BYTES, MAGIC, PCM, SpeechOutput


@dataclass
class Clock:
    now: int = 17_000_000_000

    def read(self):
        return self.now


class Writer:
    reader_present = True
    written_bytes = 0
    dropped_bytes = 0

    def __init__(self):
        self.closed = False

    def write(self, _packet):
        pytest.fail("Speech reception must not write or reconfigure native music")

    def reset(self, _owner):
        pytest.fail("Speech reception must not change the native music owner")

    def close(self):
        self.closed = True


class Peer:
    def __init__(self):
        self.close_calls = 0

    async def close(self):
        self.close_calls += 1


class Track:
    kind = "audio"

    def __init__(self):
        self.queue = asyncio.Queue()
        self.waiting = asyncio.Event()

    async def recv(self):
        self.waiting.set()
        return await self.queue.get()

    def frame(self):
        from av import AudioFrame

        frame = AudioFrame(format="s16", layout="mono", samples=960)
        # AV frames use host-native samples; wire PCM is little endian.
        frame.planes[0].update(array("h", [1000] * 960).tobytes())
        frame.sample_rate = 48000
        self.queue.put_nowait(frame)


@pytest.fixture
async def path():
    temporary = tempfile.TemporaryDirectory(prefix="shiri-late-review-", dir="/tmp")
    directory = Path(temporary.name) / "overlay"
    directory.mkdir()
    # A root test process must not make UID0 the admitted output daemon. Keep
    # real filesystem ownership and the sender's actual primary-group check.
    output_uid = os.getuid() or 1001
    os.chown(directory, output_uid if os.getuid() == 0 else -1, os.getgid())
    directory.chmod(0o2710)
    endpoint = directory / "speech.sock"
    receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    receiver.bind(str(endpoint))
    os.chown(endpoint, output_uid if os.getuid() == 0 else -1, os.getgid())
    endpoint.chmod(0o660)
    receiver.setblocking(False)
    clock = Clock()
    output = SpeechOutput(endpoint, str(uuid4()), uuid4().hex, output_uid, now_ns=clock.read)
    output.begin(uuid4().hex)  # explicit admission for this isolated producer fixture
    writer = Writer()
    mixer = NativeMixer(Path(temporary.name) / "unused.pipe", writer=writer,
                        now_ns=clock.read, speech_output=output)
    worker = AudioWorker(mixer)
    try:
        yield worker, mixer, output, receiver, clock, writer
    finally:
        await asyncio.wait_for(worker.close(), timeout=1)
        receiver.close()
        temporary.cleanup()


async def start(worker, gain, *, identity="first"):
    session = SpeechSession(identity, f"{identity}-request", Peer(), gain)
    track = Track()
    worker.session = session
    session.receiver = asyncio.create_task(worker._receive(session, track))
    await asyncio.wait_for(track.waiting.wait(), timeout=1)
    return session, track


async def receive(receiver):
    packet = await asyncio.wait_for(asyncio.get_running_loop().sock_recv(receiver, 4096), timeout=1)
    return HEADER.unpack(packet[:HEADER_BYTES]), packet[HEADER_BYTES:]


def nothing_sent(receiver):
    with pytest.raises(BlockingIOError):
        receiver.recv(4096)


@pytest.mark.parametrize("gain", [0.0, 0.1, 0.28, 1.0])
async def test_first_decoded_pcm_has_exact_current_gain_without_early_control_or_music_write(path, gain):
    worker, mixer, output, receiver, _clock, writer = path
    session, track = await start(worker, gain)
    # Preparing/negotiating a session and waiting for media is not ducking.
    assert output.sequence == 0 and output.sent_controls == 0 and not output.active
    assert not mixer.health()["speech_input_active"]
    nothing_sent(receiver)
    track.frame()
    header, pcm = await receive(receiver)
    assert header[:6] == (MAGIC, 2, PCM, HEADER_BYTES, 1920, 1)
    assert header[6:8] == (output.room, output.launch)
    assert header[9:] == (960, round(gain * 65536), 1, 0, output.owner)
    values = array("h", [1000] * 960)
    if sys.byteorder != "little":
        values.byteswap()
    assert pcm == values.tobytes()
    assert session.last_audible > 0 and session.last_media == session.last_audible
    assert output.sent_controls == 0 and not output.active
    assert not writer.closed and mixer.token is None and mixer.route is None


async def test_successor_uses_its_own_first_frame_gain_after_old_peer_retirement(path):
    worker, _mixer, output, receiver, _clock, _writer = path
    previous, track = await start(worker, 0.0)
    track.frame()
    old_header, _ = await receive(receiver)
    assert old_header[10] == 0
    await asyncio.wait_for(worker.dispatch("speech", {
        "session_id": previous.session_id, "request_id": "close-old", "action": "close",
    }), timeout=1)
    assert previous.peer.close_calls == 1 and previous.receiver.done()
    successor, successor_track = await start(worker, 0.8, identity="successor")
    nothing_sent(receiver)
    successor_track.frame()
    new_header, _ = await receive(receiver)
    assert new_header[5] == old_header[5] + 1
    assert new_header[10] == round(successor.duck_gain * 65536)
    assert new_header[11] == 1
    assert output.sent_controls == 0 and worker.session is successor
    # Retired decoding cannot emit another packet under the successor's policy.
    track.frame()
    await worker._release(previous)
    nothing_sent(receiver)
    assert worker.session is successor


async def test_idle_health_reports_bounded_input_activity_without_claiming_backend_presentation(path):
    worker, mixer, output, receiver, clock, writer = path
    _session, track = await start(worker, 0.2)
    before = mixer.health()
    assert not before["audio_active"] and not before["speech_input_active"]
    track.frame()
    await receive(receiver)
    active = await worker.dispatch("health", {})
    assert active["speech_input_active"] and active["audio_active"]
    assert not active["music_active"] and active["music_gain"] is None
    assert active["speech_sent_frames"] == 960 and output.sent_controls == 0
    assert not writer.closed and mixer.token is None and mixer.route is None
    clock.now += 249_999_999
    assert mixer.health()["speech_input_active"]
    clock.now += 1
    expired = mixer.health()
    assert not expired["speech_input_active"] and not expired["audio_active"]
    assert expired["speech_sent_frames"] == 960
    assert expired["music_gain"] is None and expired["error"] is None
