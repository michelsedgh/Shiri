"""One room's PCM mixer and bounded WebRTC speech receiver.

OwnTone owns output delivery and synchronization. This process only combines a
48 kHz ALSA receiver stream with one room-addressed speech stream. It never
opens network speakers. An absent FIFO reader cannot block the mixer or RPC.
"""
from __future__ import annotations

import argparse
from array import array
import asyncio
import contextlib
from dataclasses import dataclass, field
import errno
import fcntl
import logging
import math
import os
from pathlib import Path
import re
import signal
import stat
import time

from shiri.rpc import RpcError, call_rpc, serve_rpc

log = logging.getLogger(__name__)
RATE = 48000
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class FifoWriter:
    """Atomic, nonblocking PCM writes. Drop live audio rather than build backlog."""
    def __init__(self, path: Path):
        self.path = path
        self.fd = None
        self.written_bytes = 0
        self.dropped_bytes = 0
        self.reader_present = False
        self.packet_bytes = os.pathconf(path, "PC_PIPE_BUF") // 4 * 4
        if self.packet_bytes < 4:
            raise RuntimeError("Audio FIFO cannot write atomic stereo frames")
        if not stat.S_ISFIFO(path.lstat().st_mode):
            raise RuntimeError("The audio output must be an existing FIFO")

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        self.reader_present = False

    def write(self, data: bytes):
        if self.fd is None:
            try:
                self.fd = os.open(self.path, os.O_WRONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            except OSError as exc:
                if exc.errno != errno.ENXIO:
                    raise
                self.dropped_bytes += len(data)
                return
        # PIPE_BUF-sized writes are atomic. Every packet contains whole stereo
        # frames, so a full pipe never leaves a partial sample in the stream.
        if len(data) % 4:
            raise ValueError("Output PCM must contain complete stereo frames")
        for start in range(0, len(data), self.packet_bytes):
            packet = data[start:start + self.packet_bytes]
            try:
                count = os.write(self.fd, packet)
                self.written_bytes += count
                self.reader_present = True
            except BlockingIOError:
                self.dropped_bytes += len(data) - start
                return
            except BrokenPipeError:
                self.close()
                self.dropped_bytes += len(data) - start
                return


class GstMixer:
    def __init__(self, capture: str, fifo: Path, *, test_source=False):
        # Linux system GI is loaded only in this worker, never in API imports.
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        Gst.init(None)
        self.Gst = Gst
        self.writer = FifoWriter(fifo)
        self.active = False
        self.error = None
        self._speech_end = 0
        self._gain = 1.0
        self.pipeline = Gst.Pipeline.new("room-mixer")
        def element(factory, name, **properties):
            value = Gst.ElementFactory.make(factory, name)
            if value is None:
                raise RuntimeError(f"Missing GStreamer element: {factory}")
            for key, setting in properties.items():
                value.set_property(key.replace("_", "-"), setting)
            self.pipeline.add(value)
            return value
        def caps(name, description):
            return element("capsfilter", name, caps=Gst.Caps.from_string(description))
        def link(*elements):
            for before, after in zip(elements, elements[1:], strict=False):
                if not before.link(after):
                    raise RuntimeError(f"Could not connect {before.name} to {after.name}")
        self.mix = element("audiomixer", "mix", ignore_inactive_pads=True, latency=40_000_000,
                           output_buffer_duration=20_000_000)
        bed = element("audiotestsrc", "clock-bed", is_live=True, wave=4, samplesperbuffer=960)
        link(bed, caps("bed-format", "audio/x-raw,format=F32LE,rate=48000,channels=2"), self.mix)
        source = (element("audiotestsrc", "music-source", is_live=True, wave=0, freq=440,
                          volume=0.1, samplesperbuffer=960) if test_source else
                  element("alsasrc", "music-source", device=capture, buffer_time=120000,
                          latency_time=20000, provide_clock=False))
        music_queue = element("queue", "music-queue", max_size_time=200_000_000,
                              max_size_bytes=0, max_size_buffers=0, leaky=2)
        self.music = element("volume", "music-volume")
        link(source, caps("music-capture-format", "audio/x-raw,format=S16LE,rate=48000,channels=2"),
             music_queue, element("audioconvert", "music-convert"),
             element("audioresample", "music-resample"),
             caps("music-format", "audio/x-raw,format=F32LE,rate=48000,channels=2"), self.music, self.mix)
        self.speech = element("appsrc", "speech-source", is_live=True, format=Gst.Format.TIME,
                              block=False, max_bytes=24000, leaky_type=2,
                              caps=Gst.Caps.from_string("audio/x-raw,format=S16LE,rate=48000,channels=1,layout=interleaved"))
        link(self.speech, element("queue", "speech-queue", max_size_time=250_000_000,
                                 max_size_bytes=0, max_size_buffers=0, leaky=2),
             element("audioconvert", "speech-convert"), element("audioresample", "speech-resample"),
             caps("speech-format", "audio/x-raw,format=F32LE,rate=48000,channels=2"), self.mix)
        sink = element("appsink", "output", emit_signals=True, sync=False, max_buffers=2, drop=True)
        link(self.mix, element("audioconvert", "output-convert"),
             caps("output-format", "audio/x-raw,format=S16LE,rate=48000,channels=2,layout=interleaved"), sink)
        sink.connect("new-sample", self._output)
        self.bus = self.pipeline.get_bus()
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("The room audio pipeline could not start")

    def _output(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return self.Gst.FlowReturn.EOS
        if self.active:
            buffer = sample.get_buffer()
            try:
                self.writer.write(buffer.extract_dup(0, buffer.get_size()))
            except OSError as exc:
                self.error = f"Audio FIFO write failed: {exc.strerror}"
                return self.Gst.FlowReturn.ERROR
        else:
            self.writer.close()
        return self.Gst.FlowReturn.OK

    def now_ns(self):
        clock = self.pipeline.get_clock()
        if clock is None:
            return 0
        return max(0, clock.get_time() - self.pipeline.get_base_time())

    def push_speech(self, data: bytes, samples: int):
        if samples <= 0 or samples > RATE // 5 or len(data) != samples * 2:
            raise RpcError("invalid_media", "Speech audio frame has an invalid size")
        now = self.now_ns()
        # These are PCM mixer timestamps, not speaker clock corrections.
        if self._speech_end < now or self._speech_end > now + 250_000_000:
            self._speech_end = now + 60_000_000
        buffer = self.Gst.Buffer.new_allocate(None, len(data), None)
        buffer.fill(0, data)
        buffer.pts = self._speech_end
        buffer.duration = samples * self.Gst.SECOND // RATE
        self._speech_end += buffer.duration
        result = self.speech.emit("push-buffer", buffer)
        if result != self.Gst.FlowReturn.OK:
            raise RpcError("audio_unavailable", "Speech audio pipeline rejected a frame")

    def tick(self, *, music_active: bool, speech_active: bool, duck_gain: float, elapsed: float):
        self.active = music_active or speech_active
        target = duck_gain if speech_active else 1.0
        duration = 0.04 if target < self._gain else 0.25
        change = elapsed / duration
        self._gain = min(target, self._gain + change) if target > self._gain else max(target, self._gain - change)
        self.music.set_property("volume", self._gain)
        while True:
            message = self.bus.pop_filtered(self.Gst.MessageType.ERROR | self.Gst.MessageType.EOS)
            if message is None:
                break
            if message.type == self.Gst.MessageType.ERROR:
                err, _debug = message.parse_error()
                self.error = str(err)
            else:
                self.error = "Audio pipeline ended unexpectedly"
        if self.error:
            raise RuntimeError(self.error)

    def health(self):
        return {"ready": self.error is None, "audio_active": self.active, "music_gain": self._gain,
                "fifo_reader": self.writer.reader_present, "written_bytes": self.writer.written_bytes,
                "dropped_bytes": self.writer.dropped_bytes, "error": self.error}

    def close(self):
        self.pipeline.set_state(self.Gst.State.NULL)
        self.writer.close()


@dataclass(eq=False)
class SpeechSession:
    session_id: str
    request_id: str
    peer: object
    duck_gain: float
    created: float = field(default_factory=time.monotonic)
    last_media: float = 0
    last_audible: float = 0
    answer: dict | None = None
    receiver: asyncio.Task | None = None
    negotiation: asyncio.Task | None = None
    disposed: bool = False


class AudioWorker:
    def __init__(self, mixer, *, peer_factory=None, idle_seconds=10.0):
        self.mixer = mixer
        self.peer_factory = peer_factory
        self.idle_seconds = idle_seconds
        self.session: SpeechSession | None = None
        self.music_active = False
        self._lock = asyncio.Lock()
        self._closing = False

    @staticmethod
    def identity(payload):
        for key in ("session_id", "request_id"):
            if not isinstance(payload.get(key), str) or not IDENTIFIER.fullmatch(payload[key]):
                raise RpcError("invalid_request", f"An explicit valid {key} is required")
        gain = payload.get("duck_gain", 0.28)
        if isinstance(gain, bool) or not isinstance(gain, (int, float)) or not math.isfinite(gain) or not 0 <= gain <= 1:
            raise RpcError("invalid_request", "duck_gain must be finite and between zero and one")
        return float(gain)

    async def dispatch(self, operation, payload):
        if operation == "health":
            return {**self.mixer.health(), "music_active": self.music_active,
                    "speech_session_id": self.session.session_id if self.session else None}
        if operation == "music":
            if type(payload.get("active")) is not bool:
                raise RpcError("invalid_request", "Music activity must be a boolean")
            self.music_active = payload["active"]
            return {"ok": True}
        if operation != "speech":
            raise RpcError("invalid_request", "Unknown audio operation")
        gain = self.identity(payload)
        action = payload.get("action", "offer")
        if action == "offer":
            return await self.offer(payload, gain)
        if action not in {"control", "close"}:
            raise RpcError("invalid_request", "Unknown speech action")
        async with self._lock:
            current = self.session
            if current is not None and current.session_id != payload["session_id"]:
                raise RpcError("session_conflict", "Another speech producer owns this room")
            if action == "control" and current is None:
                raise RpcError("not_found", "This speech session does not exist")
            if action == "control" and current:
                current.duck_gain = gain
                return {"ok": True, "session_id": current.session_id, "negotiated": current.answer is not None}
            if action == "close" and current:
                self.session = None
        if action == "close" and current:
            await self._dispose(current)
        return {"ok": True}

    async def offer(self, payload, gain):
        from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
        from aiortc.sdp import SessionDescription
        sdp = payload.get("sdp")
        if not isinstance(sdp, str) or not sdp or len(sdp) > 1024 * 1024 or payload.get("type", "offer") != "offer":
            raise RpcError("invalid_request", "An SDP offer is required")
        try:
            description = SessionDescription.parse(sdp)
        except (ValueError, AssertionError, IndexError) as exc:
            raise RpcError("invalid_request", "The SDP offer could not be parsed") from exc
        if len(description.media) != 1 or description.media[0].kind != "audio" or description.media[0].port == 0 or description.media[0].direction not in {"sendonly", "sendrecv"}:
            raise RpcError("invalid_request", "Speech offers must contain exactly one active audio track")
        async with self._lock:
            if self._closing:
                raise RpcError("audio_unavailable", "Audio worker is stopping")
            old = self.session
            if old:
                if old.session_id != payload["session_id"]:
                    raise RpcError("session_conflict", "Another speech producer owns this room")
                if old.request_id == payload["request_id"]:
                    if old.answer is not None:
                        return old.answer
                    raise RpcError("conflict", "This speech offer is still negotiating")
                raise RpcError("conflict", "Close the existing speech session before making a new offer")
            peer = (self.peer_factory() if self.peer_factory else RTCPeerConnection(RTCConfiguration(iceServers=[])))
            current = SpeechSession(payload["session_id"], payload["request_id"], peer, gain,
                                    negotiation=asyncio.current_task())
            self.session = current
        @peer.on("track")
        def on_track(track):
            if track.kind == "audio" and current.receiver is None:
                current.receiver = asyncio.create_task(self._receive(current, track))
            else:
                asyncio.create_task(self._release(current))
        @peer.on("connectionstatechange")
        async def state_change():
            if peer.connectionState in {"failed", "closed"}:
                await self._release(current)
        try:
            await peer.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
            await peer.setLocalDescription(await peer.createAnswer())
            async with self._lock:
                if self.session is not current or self._closing:
                    raise RpcError("session_conflict", "Speech session ended during negotiation")
                current.answer = {"sdp": peer.localDescription.sdp, "type": "answer", "session_id": current.session_id}
                current.negotiation = None
                return current.answer
        except BaseException:
            await self._release(current)
            raise

    async def _receive(self, session, track):
        from av import AudioResampler
        from aiortc.mediastreams import MediaStreamError
        resampler = AudioResampler(format="s16", layout="mono", rate=RATE)
        try:
            while self.session is session:
                frame = await asyncio.wait_for(track.recv(), timeout=self.idle_seconds)
                for pcm in resampler.resample(frame):
                    if self.session is not session:
                        break
                    data = bytes(pcm.planes[0])[:pcm.samples * 2]
                    self.mixer.push_speech(data, pcm.samples)
                    session.last_media = time.monotonic()
                    # Transport packets can contain silence between utterances.
                    # They keep the decoder alive but must not hold music ducked.
                    values = array("h", data)
                    if values and sum(value * value for value in values) / len(values) > 65 * 65:
                        session.last_audible = session.last_media
        except (MediaStreamError, asyncio.TimeoutError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Speech stream stopped because media decoding failed")
        finally:
            await self._release(session)

    async def _release(self, session):
        async with self._lock:
            if self.session is session:
                self.session = None
        await self._dispose(session)

    async def _dispose(self, session):
        if session.disposed:
            return
        session.disposed = True
        caller = asyncio.current_task()
        tasks = [task for task in (session.receiver, session.negotiation) if task is not None and task is not caller and not task.done()]
        for task in tasks:
            task.cancel()
        # aiortc's closed callback may re-enter; removing ownership precedes close.
        if session.peer.connectionState != "closed":
            await session.peer.close()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def tick(self, elapsed):
        current = self.session
        now = time.monotonic()
        if current and (now - max(current.last_media, current.created) > self.idle_seconds
                        or now - max(current.last_audible, current.created) > 30):
            await self._release(current)
            current = None
        recent = bool(current and current.last_audible and now - current.last_audible < 0.25)
        self.mixer.tick(music_active=self.music_active, speech_active=recent,
                        duck_gain=current.duck_gain if current else 0.28, elapsed=elapsed)

    async def close(self):
        self._closing = True
        async with self._lock:
            current, self.session = self.session, None
        if current:
            await self._dispose(current)
        self.mixer.close()


async def run(args):
    if args.signal:
        await call_rpc(args.socket, "music", {"active": args.signal == "music-start"}, timeout=3)
        return
    directory = Path(args.room_dir)
    fifo = directory / "pipes" / "audio.pipe"
    # Broker ownership prevents duplicate workers; this lock also protects the
    # standalone CLI from replacing a live worker's control socket.
    lock_fd = os.open(str(args.socket) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        raise RuntimeError("An audio worker already owns this room socket") from None
    try:
        mixer = GstMixer(args.capture, fifo, test_source=args.test_source)
    except BaseException:
        os.close(lock_fd)
        raise
    worker = AudioWorker(mixer)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(name, stop.set)
    server = None
    try:
        server = await serve_rpc(args.socket, worker.dispatch, mode=0o600, allowed_uids={os.getuid()})
        last = time.monotonic()
        while not stop.is_set():
            now = time.monotonic()
            await worker.tick(now - last)
            last = now
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.01)
            except asyncio.TimeoutError:
                pass
    finally:
        if server:
            server.close()
            await server.wait_closed()
        await worker.close()
        os.close(lock_fd)
        if server:
            with contextlib.suppress(FileNotFoundError):
                Path(args.socket).unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--room-dir")
    parser.add_argument("--capture")
    parser.add_argument("--signal", choices=["music-start", "music-stop"])
    parser.add_argument("--test-source", action="store_true", help="Use a test oscillator instead of ALSA")
    args = parser.parse_args()
    if not args.signal and (not args.room_dir or not args.capture):
        parser.error("--room-dir and --capture are required for a worker")
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
