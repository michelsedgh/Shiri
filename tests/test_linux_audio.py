"""Opt-in tests of the actual Linux audio path, without ALSA or speakers.

Run with SHIRI_LINUX_AUDIO_TESTS=1 using a Python environment that can import
the system GI bindings, GStreamer audio plugins, aiortc, and PyAV. Each test
uses a private temporary FIFO and Unix socket. Nothing touches live rooms.
"""
from array import array
import asyncio
from contextlib import asynccontextmanager
from fractions import Fraction
import math
import os
from pathlib import Path
import struct
import sys
import time

import pytest

from shiri.rpc import call_rpc, serve_rpc
from shiri.runtime.audio import AudioWorker, GstMixer, RATE
from shiri.runtime.local_output import GstLocalOutput, HOST_BUS

PROJECT_ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("SHIRI_LINUX_AUDIO_TESTS") != "1",
    reason="Opt in to actual Linux GStreamer integration with SHIRI_LINUX_AUDIO_TESTS=1",
)


class AudioHarness:
    def __init__(self, directory, *, reader=True):
        self.fifo = directory / "audio.pipe"
        self.socket = directory / "audio.sock"
        os.mkfifo(self.fifo, mode=0o600)
        self.reader = os.open(self.fifo, os.O_RDONLY | os.O_NONBLOCK) if reader else None
        self.mixer = GstMixer("unused-test-device", self.fifo, test_source=True)
        self.worker = AudioWorker(self.mixer)
        self.server = None
        self.ticker = None

    async def start(self):
        self.server = await serve_rpc(self.socket, self.worker.dispatch, mode=0o600,
                                      allowed_uids={os.getuid()})
        self.ticker = asyncio.create_task(self._tick(), name="linux-audio-test-ticker")

    async def _tick(self):
        previous = time.monotonic()
        while True:
            now = time.monotonic()
            await self.worker.tick(now - previous)
            previous = now
            await asyncio.sleep(0.01)

    def check_ticker(self):
        if self.ticker and self.ticker.done():
            self.ticker.result()
            raise AssertionError("Audio ticker exited unexpectedly")

    def open_reader(self):
        assert self.reader is None
        self.reader = os.open(self.fifo, os.O_RDONLY | os.O_NONBLOCK)

    def drain(self):
        assert self.reader is not None
        result = bytearray()
        while True:
            try:
                block = os.read(self.reader, 65536)
            except BlockingIOError:
                break
            if not block:
                break
            result.extend(block)
        return bytes(result)

    async def collect(self, duration=0.5, *, discard=True):
        if discard:
            self.drain()
        start = time.monotonic()
        data = bytearray()
        while time.monotonic() - start < duration:
            self.check_ticker()
            data.extend(self.drain())
            await asyncio.sleep(0.002)
        data.extend(self.drain())
        return bytes(data), time.monotonic() - start

    async def settle(self, duration):
        # Keep consuming throughout warm-up so FIFO pressure cannot skew timing.
        await self.collect(duration)

    async def health(self):
        self.check_ticker()
        return await call_rpc(self.socket, "health", timeout=1)

    async def eof(self):
        async def wait():
            while True:
                try:
                    if os.read(self.reader, 65536) == b"":
                        return
                except BlockingIOError:
                    pass
                await asyncio.sleep(0.005)
        await asyncio.wait_for(wait(), timeout=1)

    async def close(self):
        if self.server:
            self.server.close()
            await asyncio.wait_for(self.server.wait_closed(), 2)
        if self.ticker:
            self.ticker.cancel()
            await asyncio.gather(self.ticker, return_exceptions=True)
        await asyncio.wait_for(self.worker.close(), 2)
        if self.reader is not None:
            os.close(self.reader)
            self.reader = None
        self.socket.unlink(missing_ok=True)


@asynccontextmanager
async def audio_harness(directory, *, reader=True):
    harness = AudioHarness(directory, reader=reader)
    try:
        await harness.start()
        yield harness
    finally:
        await harness.close()


def channel(data):
    assert data and len(data) % 4 == 0, "FIFO PCM must contain complete S16 stereo frames"
    values = array("h", data)
    assert sys.byteorder == "little", "The test host must decode the advertised S16LE format"
    left, right = values[0::2], values[1::2]
    # Both source tracks are mono material converted to stereo; any dither can
    # differ by one sample but channels must not drift or become misaligned.
    assert max(abs(a - b) for a, b in zip(left, right, strict=True)) <= 2
    return left


def rms(samples):
    return math.sqrt(sum(value * value for value in samples) / len(samples))


def amplitude(samples, frequency):
    # A half-second window contains whole periods of both 440 and 880 Hz, so
    # this measurement separates music from speech in the actual mixed stream.
    size = RATE // 2
    assert len(samples) >= size
    samples = samples[-size:]
    step = 2 * math.pi * frequency / RATE
    real = sum(value * math.cos(index * step) for index, value in enumerate(samples))
    imaginary = sum(value * math.sin(index * step) for index, value in enumerate(samples))
    return 2 * math.hypot(real, imaginary) / size


async def wait_until(predicate, *, timeout=8):
    async def wait():
        while not predicate():  # noqa: ASYNC110 - GStreamer and aiortc expose observed state, not an event.
            await asyncio.sleep(0.01)
    await asyncio.wait_for(wait(), timeout)


def require_unused_loopback_slot(slot):
    # Opting into tests must not borrow an active room's loopback substream.
    for direction in ("pcm0p", "pcm0c", "pcm1p", "pcm1c"):
        status = Path(f"/proc/asound/Loopback/{direction}/sub{slot}/status")
        try:
            state = status.read_text().strip()
        except FileNotFoundError:
            pytest.skip(f"ALSA Loopback slot {slot} is unavailable; the host bridge test requires snd-aloop")
        if state != "closed":
            pytest.skip(f"Loopback slot {slot} is in use; leave live audio undisturbed")


async def test_gstreamer_fifo_is_clocked_stereo_pcm_and_idles_to_eof(tmp_path):
    async with audio_harness(tmp_path) as audio:
        await call_rpc(audio.socket, "music", {"active": True}, timeout=1)
        await audio.settle(0.4)
        data, elapsed = await audio.collect(1.0)
        samples = channel(data)
        rate = len(samples) / elapsed
        assert RATE * 0.8 < rate < RATE * 1.2
        assert 1800 < rms(samples) < 3000
        assert 2800 < amplitude(samples, 440) < 3700
        caps = audio.mixer.pipeline.get_by_name("output").get_static_pad("sink").get_current_caps()
        structure = caps.get_structure(0)
        assert structure.get_value("format") == "S16LE"
        assert structure.get_value("rate") == RATE
        assert structure.get_value("channels") == 2
        health = await audio.health()
        assert health["ready"] and health["fifo_reader"] and health["written_bytes"] > 0
        print(f"PCM rate={rate:.1f} frames/s RMS={rms(samples):.1f}, FIFO bytes={len(data)}")
        await call_rpc(audio.socket, "music", {"active": False}, timeout=1)
        await wait_until(lambda: audio.mixer.writer.fd is None, timeout=1)
        await audio.eof()
        assert not (await audio.health())["audio_active"]
        # The same FIFO and pipeline can resume after EOF; there is no old queue.
        await call_rpc(audio.socket, "music", {"active": True}, timeout=1)
        resumed, _elapsed = await audio.collect(0.3)
        assert len(channel(resumed)) >= RATE // 8
    assert audio.mixer.pipeline.get_state(0).state == audio.mixer.Gst.State.NULL
    assert audio.mixer.writer.fd is None and not audio.socket.exists()


async def test_host_local_bridge_starts_before_owntone_and_releases_loopback_on_sigterm(tmp_path):
    """Exercise the actual reverse snd-aloop pair, discarding at a test sink.

    Slot 6 is reserved only for this opt-in test; broker integration uses slot 7.
    No paired Bluetooth device is available, so this does not certify BlueALSA.
    """
    await asyncio.to_thread(require_unused_loopback_slot, 6)
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    socket = tmp_path / "local-output.sock"
    capture = "hw:Loopback,0,6"
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "shiri.runtime.local_output", "--capture", capture,
        "--device", "unused-test-output", "--socket", str(socket), "--test-sink",
        env={**os.environ, "DBUS_SYSTEM_BUS_ADDRESS": "unix:path=/tmp/shiri-unreachable-test-bus"},
        cwd=str(PROJECT_ROOT),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    playback = None
    replacement = None
    try:
        # Readiness must not depend on OwnTone starting playback first.
        await wait_until(socket.exists, timeout=5)
        async def ready():
            while True:
                health = await call_rpc(socket, "health", timeout=1)
                if health["ready"]:
                    return health
                await asyncio.sleep(0.02)
        health = await asyncio.wait_for(ready(), timeout=3)
        assert health["system_bus"] == HOST_BUS
        assert health["capture"] == capture and health["device"] == "unused-test-output"
        playback = Gst.parse_launch(
            "audiotestsrc is-live=true wave=sine freq=550 volume=0.1 samplesperbuffer=960 ! "
            "audio/x-raw,format=S16LE,rate=48000,channels=2,layout=interleaved ! "
            "alsasink device=hw:Loopback,1,6 sync=true buffer-time=120000 latency-time=20000"
        )
        assert playback.set_state(Gst.State.PLAYING) != Gst.StateChangeReturn.FAILURE
        await asyncio.sleep(0.35)
        first = await call_rpc(socket, "health", timeout=1)
        before = time.monotonic()
        await asyncio.sleep(0.65)
        second = await call_rpc(socket, "health", timeout=1)
        elapsed = time.monotonic() - before
        assert second["ready"] and second["error"] is None
        assert (second["format"], second["rate"], second["channels"]) == ("S16LE", RATE, 2)
        measured = (second["frames_forwarded"] - first["frames_forwarded"]) / elapsed
        assert RATE * 0.7 < measured < RATE * 1.3
        assert second["queue_bytes"] <= 38400
        assert second["queue_time_ms"] <= 220
        assert second["last_output_at"] > first["last_output_at"]
        process.terminate()
        output, _stderr = await asyncio.wait_for(process.communicate(), timeout=3)
        assert process.returncode == 0, output.decode(errors="replace")
        assert not socket.exists()
        # Reopening the same actual capture verifies that the stopped worker
        # released its ALSA handle. Also inspect this pipeline's explicit NULL.
        replacement = GstLocalOutput(capture, "unused-test-output", test_sink=True)
        recorded = bytearray()
        def record(_pad, probe):
            buffer = probe.get_buffer()
            if buffer is not None and len(recorded) < RATE * 4:
                recorded.extend(buffer.extract_dup(0, buffer.get_size()))
            return Gst.PadProbeReturn.OK
        replacement.sink.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, record)
        await asyncio.sleep(0.65)
        replacement.poll()
        waveform = channel(bytes(recorded))
        assert 1800 < rms(waveform) < 3000
        assert 2800 < amplitude(waveform, 550) < 3700
        replacement.close()
        assert replacement.pipeline.get_state(0).state == Gst.State.NULL
        print(f"Host local bridge: {measured:.1f} frames/s, RMS={rms(waveform):.1f}, "
              "SIGTERM clean, Loopback6 reusable")
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.communicate(), timeout=3)
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
        if replacement:
            replacement.close()
        if playback:
            playback.set_state(Gst.State.NULL)


async def test_absent_or_full_fifo_reader_cannot_block_worker_or_rpc(tmp_path):
    async with audio_harness(tmp_path, reader=False) as audio:
        await call_rpc(audio.socket, "music", {"active": True}, timeout=1)
        await asyncio.sleep(0.25)
        before = time.monotonic()
        health = await audio.health()
        assert time.monotonic() - before < 0.5
        assert health["ready"] and not health["fifo_reader"]
        assert health["written_bytes"] == 0 and health["dropped_bytes"] > 0
        audio.open_reader()
        data, _elapsed = await audio.collect(0.3)
        assert len(channel(data)) >= RATE // 8
        written = (await audio.health())["written_bytes"]
        await asyncio.sleep(0.3)  # Reader exists but intentionally stops draining.
        health = await audio.health()
        assert health["ready"] and health["dropped_bytes"] > 0
        # A full Linux pipe is bounded; the audio writer must drop instead of
        # retaining a replay backlog or waiting for the consumer.
        assert health["written_bytes"] - written < 65536
        for _ in range(8):
            assert (await audio.health())["ready"]
        print(f"Absent/full reader: written={health['written_bytes']} dropped={health['dropped_bytes']}")


async def test_real_webrtc_speech_is_mixed_ducks_music_and_silence_releases_it(tmp_path):
    from aiortc import AudioStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription
    from av import AudioFrame

    class SpeechTone(AudioStreamTrack):
        def __init__(self):
            super().__init__()
            self.samples = 0
            self.silent = False
            self.started = None

        async def recv(self):
            if self.started is None:
                self.started = time.monotonic()
            target = self.started + self.samples / RATE
            await asyncio.sleep(max(0, target - time.monotonic()))
            frame = AudioFrame(format="s16", layout="mono", samples=960)
            values = ([0] * 960 if self.silent else
                      [int(600 * math.sin(2 * math.pi * 880 * (self.samples + index) / RATE))
                       for index in range(960)])
            frame.planes[0].update(struct.pack("<960h", *values))
            frame.sample_rate = RATE
            frame.pts = self.samples
            frame.time_base = Fraction(1, RATE)
            self.samples += 960
            return frame

    sender = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    tone = SpeechTone()
    sender.addTrack(tone)
    try:
        async with audio_harness(tmp_path) as audio:
            await call_rpc(audio.socket, "music", {"active": True}, timeout=1)
            await audio.settle(0.3)
            baseline_data, _elapsed = await audio.collect(0.65)
            baseline = channel(baseline_data)
            await sender.setLocalDescription(await sender.createOffer())
            identity = {"session_id": "linux-speech-test", "request_id": "offer-1", "duck_gain": 0.2}
            answer = await call_rpc(audio.socket, "speech", {
                **identity, "action": "offer", "sdp": sender.localDescription.sdp, "type": "offer",
            }, timeout=8)
            await sender.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))
            await wait_until(lambda: audio.worker.session is not None and audio.worker.session.last_audible > 0)
            session = audio.worker.session
            await audio.settle(0.5)
            mixed_data, _elapsed = await audio.collect(0.65)
            mixed = channel(mixed_data)
            assert session.peer.connectionState == "connected"
            assert amplitude(mixed, 880) > 150, "Decoded speech must reach the real FIFO"
            music_ratio = amplitude(mixed, 440) / amplitude(baseline, 440)
            assert 0.12 < music_ratio < 0.35
            assert rms(mixed) < rms(baseline) * 0.6
            assert (await audio.health())["music_gain"] == pytest.approx(0.2)
            # Send silence over the same connected WebRTC stream. This must
            # preserve the session while releasing music ducking promptly.
            tone.silent = True
            await audio.settle(0.85)
            silent_data, _elapsed = await audio.collect(0.65)
            silent = channel(silent_data)
            assert audio.worker.session is session and session.peer.connectionState == "connected"
            assert time.monotonic() - session.last_media < 0.3
            assert time.monotonic() - session.last_audible > 0.5
            assert amplitude(silent, 440) / amplitude(baseline, 440) == pytest.approx(1, rel=0.1)
            assert amplitude(silent, 880) < 60
            assert rms(silent) / rms(baseline) == pytest.approx(1, rel=0.1)
            assert (await audio.health())["music_gain"] == pytest.approx(1)
            print(f"WebRTC music ratio={music_ratio:.3f}, RMS baseline/mixed/silent="
                  f"{rms(baseline):.1f}/{rms(mixed):.1f}/{rms(silent):.1f}")
            await call_rpc(audio.socket, "speech", {**identity, "request_id": "close-1", "action": "close"}, timeout=2)
            assert audio.worker.session is None and session.receiver.done()
            assert session.peer.connectionState == "closed"
            await call_rpc(audio.socket, "music", {"active": False}, timeout=1)
            await wait_until(lambda: audio.mixer.writer.fd is None, timeout=1)
            await audio.eof()
    finally:
        tone.stop()
        await asyncio.wait_for(sender.close(), 2)
    assert audio.mixer.pipeline.get_state(0).state == audio.mixer.Gst.State.NULL
    assert audio.mixer.writer.fd is None and not audio.socket.exists()
