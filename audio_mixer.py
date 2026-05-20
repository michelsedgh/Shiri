#!/usr/bin/env python3
"""
audio_mixer.py - Shiri zone audio mixer.

OwnTone reads one raw PCM FIFO per zone. This process feeds that FIFO with a
single long-running GStreamer mixer pipeline:

  ALSA loopback capture -------------> \
  silence clock bed ------------------> audiomixer -> OwnTone FIFO
  persistent WebRTC TTS appsrc ------> /

TTS audio reaches this process as WebRTC audio. The Flask app only handles
zone-addressed SDP/control forwarding over a private Unix socket; this mixer
process owns the persistent WebRTC session, decodes received audio, feeds it
into GStreamer through appsrc, applies level detection, ducks music, and mixes
the zone output with audiomixer.
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import fcntl
import json
import logging
import os
import queue
import signal
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


OUTPUT_RATE = 48000
OUTPUT_CHANNELS = 2
OUTPUT_CAPS = (
    f"audio/x-raw,format=S16LE,layout=interleaved,"
    f"rate={OUTPUT_RATE},channels={OUTPUT_CHANNELS}"
)

TTS_APPSRC_RATE = 48000
TTS_APPSRC_CHANNELS = 1
TTS_APPSRC_CAPS = (
    f"audio/x-raw,format=S16LE,layout=interleaved,"
    f"rate={TTS_APPSRC_RATE},channels={TTS_APPSRC_CHANNELS}"
)
TTS_APP_QUEUE_NS = 2_000_000_000
TTS_AUDIBLE_RMS = 32.0

MIXER_ELEMENT = "audiomixer"
MIXER_BUFFER_MS = 20
MIXER_LATENCY_MS = 60

DEFAULT_DUCK_GAIN = 0.28
DUCK_ATTACK_SECONDS = 0.08
DUCK_RELEASE_SECONDS = 0.45
DUCK_UPDATE_SECONDS = 0.02
TTS_IDLE_RELEASE_SECONDS = 1.50
TTS_DUCK_TAIL_SECONDS = 0.50
TTS_ACTIVE_LEVEL_DB = -60.0

PIPE_RETRY_SECONDS = 0.4
PIPE_LOG_INTERVAL_SECONDS = 5.0
CONTROL_SOCKET_NAME = "tts_webrtc.sock"
CONTROL_MAX_BYTES = 2 * 1024 * 1024
CONTROL_THREAD_TIMEOUT_SECONDS = 16.0
WEBRTC_ICE_GATHER_TIMEOUT_SECONDS = 5.0
WEBRTC_NEGOTIATION_TIMEOUT_SECONDS = 12.0
WEBRTC_NO_MEDIA_TIMEOUT_SECONDS = 30.0


log = logging.getLogger("shiri.audio_mixer")


class PipelineRestart(RuntimeError):
    """Raised when the GStreamer pipeline must be rebuilt."""


@dataclass
class ControlRequest:
    payload: dict[str, Any]
    response_queue: queue.Queue


@dataclass
class TtsWebRtcSession:
    session_id: str
    peer: Any
    duck_gain: float
    created_at: float
    active_request_id: str = ""
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    connection_state: str = ""
    media_started: bool = False
    last_audio_at: float = 0.0
    terminal_at: float = 0.0
    closed: bool = False

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.peer.close()


class GstZoneMixer:
    def __init__(
        self,
        *,
        capture_dev: str,
        grp_dir: Path,
        tts_webrtc_socket: Path | None = None,
        tts_duck_gain: float = DEFAULT_DUCK_GAIN,
    ) -> None:
        self.capture_dev = capture_dev
        self.grp_dir = grp_dir
        self.pipe_path = grp_dir / "pipes" / "audio.pipe"
        self.control_socket_path = tts_webrtc_socket or (grp_dir / "state" / CONTROL_SOCKET_NAME)
        self.mixer_pid_path = grp_dir / "state" / "mixer.pid"

        self.Gst = None
        self.GLib = None
        self.pipeline = None
        self.bus = None
        self.mixer = None
        self.pipe_fd: int | None = None
        self.music_mixer_pad = None
        self.tts_appsrc = None
        self.tts_level_name = "tts_level"

        self._stop = False
        self._duck_level = 1.0
        self._duck_target = 1.0
        self._duck_hold_gain = 1.0
        self._duck_hold_until = 0.0
        self._last_duck_update = time.monotonic()
        self._last_pipe_wait_log = 0.0

        self._tts_duck_gain = clamp_float(tts_duck_gain, 0.0, 1.0, DEFAULT_DUCK_GAIN)
        self._tts_active = False
        self._tts_last_activity_at = 0.0
        self._tts_request_id = ""

        self._control_listener: socket.socket | None = None
        self._control_thread: threading.Thread | None = None
        self._control_requests: queue.Queue[ControlRequest] = queue.Queue()
        self._sessions: dict[str, TtsWebRtcSession] = {}
        self._active_session_id = ""
        self._sessions_lock = threading.RLock()

        self._webrtc_loop: asyncio.AbstractEventLoop | None = None
        self._webrtc_thread: threading.Thread | None = None

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_stop)
        signal.signal(signal.SIGINT, self._handle_stop)
        self.mixer_pid_path.write_text(str(os.getpid()))

        log.info(
            "Starting GStreamer %s mixer capture_dev=%s tts_webrtc_socket=%s grp_dir=%s",
            MIXER_ELEMENT,
            self.capture_dev,
            self.control_socket_path,
            self.grp_dir,
        )
        try:
            self.Gst, self.GLib = require_gstreamer()
            require_aiortc_runtime()
            self._validate_gstreamer_audio_stack()
            self._start_webrtc_runtime()
            self._start_control_socket()
            while not self._stop:
                try:
                    self._open_pipe_if_needed()
                    if self.pipe_fd is None:
                        time.sleep(PIPE_RETRY_SECONDS)
                        continue
                    self._start_pipeline()
                    self._run_pipeline_loop()
                except PipelineRestart as exc:
                    log.warning("Restarting audio pipeline: %s", exc)
                    self._stop_pipeline()
                    time.sleep(0.2)
                except Exception:
                    log.exception("Mixer runtime error")
                    self._stop_pipeline()
                    time.sleep(0.5)
        finally:
            self._stop = True
            self._stop_control_socket()
            self._stop_pipeline()
            self._stop_webrtc_runtime()
            safe_unlink(self.mixer_pid_path)
            log.info("GStreamer mixer stopped")

    def _handle_stop(self, _signum: int, _frame: object) -> None:
        self._stop = True

    def _validate_gstreamer_audio_stack(self) -> None:
        required = [
            "appsrc",
            "audiomixer",
            "audiotestsrc",
            "alsasrc",
            "audioconvert",
            "audioresample",
            "capsfilter",
            "fdsink",
            "level",
            "queue",
        ]
        missing = [name for name in required if self.Gst.ElementFactory.find(name) is None]
        if missing:
            raise RuntimeError(
                "Missing required GStreamer audio elements: "
                + ", ".join(missing)
                + ". Install the GStreamer base/good/bad plugin packages used by Shiri."
            )

    def _open_pipe_if_needed(self) -> None:
        if self.pipe_fd is not None:
            return
        try:
            self.pipe_fd = os.open(self.pipe_path, os.O_WRONLY | os.O_NONBLOCK)
            flags = fcntl.fcntl(self.pipe_fd, fcntl.F_GETFL)
            fcntl.fcntl(self.pipe_fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
            log.info("Opened OwnTone FIFO for mixed audio: %s", self.pipe_path)
        except OSError as exc:
            if exc.errno == errno.ENXIO:
                now = time.monotonic()
                if now - self._last_pipe_wait_log >= PIPE_LOG_INTERVAL_SECONDS:
                    log.info("Waiting for OwnTone to read audio FIFO: %s", self.pipe_path)
                    self._last_pipe_wait_log = now
                return
            raise

    def _start_webrtc_runtime(self) -> None:
        if self._webrtc_loop is not None and self._webrtc_thread and self._webrtc_thread.is_alive():
            return
        loop = asyncio.new_event_loop()
        ready = threading.Event()
        thread = threading.Thread(
            target=self._webrtc_loop_entry,
            args=(loop, ready),
            name="tts-webrtc-aiortc",
            daemon=True,
        )
        thread.start()
        ready.wait(timeout=5)
        self._webrtc_loop = loop
        self._webrtc_thread = thread
        log.info("TTS WebRTC aiortc runtime started")

    def _webrtc_loop_entry(self, loop: asyncio.AbstractEventLoop, ready: threading.Event) -> None:
        asyncio.set_event_loop(loop)
        ready.set()
        try:
            loop.run_forever()
        finally:
            pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    def _stop_webrtc_runtime(self) -> None:
        loop = self._webrtc_loop
        thread = self._webrtc_thread
        if loop is None:
            return
        try:
            self._run_webrtc_coro(self._close_all_webrtc_sessions(), timeout_s=5.0)
        except Exception as exc:
            log.warning("Could not close WebRTC sessions cleanly: %s", exc)
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5)
        self._webrtc_loop = None
        self._webrtc_thread = None

    def _run_webrtc_coro(self, coro, *, timeout_s: float):
        loop = self._webrtc_loop
        if loop is None:
            raise RuntimeError("TTS WebRTC runtime is not running")
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=timeout_s)

    async def _close_all_webrtc_sessions(self) -> None:
        with self._sessions_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._active_session_id = ""
        await asyncio.gather(*(session.close() for session in sessions), return_exceptions=True)

    def _start_control_socket(self) -> None:
        if self._control_thread and self._control_thread.is_alive():
            return
        self.control_socket_path.parent.mkdir(parents=True, exist_ok=True)
        safe_unlink(self.control_socket_path)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.control_socket_path))
        os.chmod(self.control_socket_path, 0o666)
        listener.listen(16)
        listener.settimeout(0.5)
        self._control_listener = listener
        self._control_thread = threading.Thread(
            target=self._control_socket_loop,
            name="tts-webrtc-control",
            daemon=True,
        )
        self._control_thread.start()
        log.info("TTS WebRTC mixer control socket listening: %s", self.control_socket_path)

    def _stop_control_socket(self) -> None:
        listener = self._control_listener
        self._control_listener = None
        if listener is not None:
            with suppress_oserror():
                listener.close()
        if self._control_thread is not None:
            self._control_thread.join(timeout=2)
            self._control_thread = None
        safe_unlink(self.control_socket_path)

    def _control_socket_loop(self) -> None:
        while not self._stop and self._control_listener is not None:
            try:
                conn, _addr = self._control_listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if not self._stop:
                    log.exception("TTS WebRTC control socket accept failed")
                break
            threading.Thread(
                target=self._handle_control_connection,
                args=(conn,),
                name="tts-webrtc-control-client",
                daemon=True,
            ).start()

    def _handle_control_connection(self, conn: socket.socket) -> None:
        response: dict[str, Any]
        try:
            conn.settimeout(CONTROL_THREAD_TIMEOUT_SECONDS)
            payload = self._read_control_payload(conn)
            response_queue: queue.Queue = queue.Queue(maxsize=1)
            self._control_requests.put(ControlRequest(payload=payload, response_queue=response_queue))
            response = response_queue.get(timeout=CONTROL_THREAD_TIMEOUT_SECONDS)
        except Exception as exc:
            log.warning("TTS WebRTC control request failed before negotiation: %s", exc)
            response = {"ok": False, "error": str(exc)}
        try:
            conn.sendall((json.dumps(response) + "\n").encode("utf-8"))
        except OSError:
            pass
        finally:
            with suppress_oserror():
                conn.close()

    def _read_control_payload(self, conn: socket.socket) -> dict[str, Any]:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > CONTROL_MAX_BYTES:
                raise ValueError("WebRTC offer payload is too large")
        raw = b"".join(chunks).strip()
        if not raw:
            raise ValueError("WebRTC offer payload is empty")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("WebRTC offer payload must be an object")
        return payload

    def _start_pipeline(self) -> None:
        if self.pipeline is not None:
            return
        Gst = self.Gst
        self.pipeline = Gst.Pipeline.new("shiri-zone-mixer")

        mixer = make_element(Gst, MIXER_ELEMENT, "mix")
        set_property_if_present(mixer, "ignore-inactive-pads", True)
        set_property_if_present(mixer, "latency", MIXER_LATENCY_MS * 1_000_000)
        set_property_if_present(mixer, "min-upstream-latency", MIXER_LATENCY_MS * 1_000_000)
        set_property_if_present(mixer, "output-buffer-duration", MIXER_BUFFER_MS * 1_000_000)
        self.pipeline.add(mixer)
        self.mixer = mixer

        self._add_silence_branch(mixer)
        self._add_music_branch(mixer)
        self._add_tts_appsrc_branch(mixer)
        self._add_output_branch(mixer)

        self.bus = self.pipeline.get_bus()
        result = self.pipeline.set_state(Gst.State.PLAYING)
        if result == Gst.StateChangeReturn.FAILURE:
            raise PipelineRestart("GStreamer refused PLAYING state")
        log.info("GStreamer pipeline started")

    def _add_silence_branch(self, mixer) -> None:
        Gst = self.Gst
        src = make_element(Gst, "audiotestsrc", "silence_src")
        src.set_property("wave", 4)
        src.set_property("is-live", True)
        src.set_property("do-timestamp", True)
        set_property_if_present(src, "samplesperbuffer", max(1, int(OUTPUT_RATE * MIXER_BUFFER_MS / 1000)))
        caps = make_element(Gst, "capsfilter", "silence_caps")
        caps.set_property("caps", Gst.Caps.from_string(OUTPUT_CAPS))
        queue_element = make_element(Gst, "queue", "silence_queue")
        self._add_and_link([src, caps, queue_element])
        self._link_to_mixer(queue_element, mixer)

    def _add_music_branch(self, mixer) -> None:
        Gst = self.Gst
        src = make_element(Gst, "alsasrc", "loopback_src")
        src.set_property("device", self.capture_dev)
        src.set_property("do-timestamp", True)
        set_property_if_present(src, "provide-clock", False)
        set_property_if_present(src, "latency-time", 10_000)
        set_property_if_present(src, "buffer-time", 50_000)

        convert = make_element(Gst, "audioconvert", "loopback_convert")
        resample = make_element(Gst, "audioresample", "loopback_resample")
        caps = make_element(Gst, "capsfilter", "loopback_caps")
        caps.set_property("caps", Gst.Caps.from_string(OUTPUT_CAPS))
        queue = make_element(Gst, "queue", "loopback_queue")
        set_property_if_present(queue, "leaky", 2)
        set_property_if_present(queue, "max-size-time", int(0.25 * 1_000_000_000))
        set_property_if_present(queue, "max-size-bytes", 0)
        set_property_if_present(queue, "max-size-buffers", 0)
        self._add_and_link([src, convert, resample, caps, queue])
        self.music_mixer_pad = self._link_to_mixer(queue, mixer)
        set_property_if_present(self.music_mixer_pad, "volume", 1.0)

    def _add_tts_appsrc_branch(self, mixer) -> None:
        Gst = self.Gst
        src = make_element(Gst, "appsrc", "tts_webrtc_appsrc")
        src.set_property("caps", Gst.Caps.from_string(TTS_APPSRC_CAPS))
        src.set_property("format", Gst.Format.TIME)
        src.set_property("is-live", True)
        set_property_if_present(src, "do-timestamp", True)
        set_property_if_present(src, "block", True)
        set_property_if_present(src, "max-bytes", int(TTS_APPSRC_RATE * TTS_APPSRC_CHANNELS * 2 * 2))

        input_queue = make_element(Gst, "queue", "tts_appsrc_queue")
        set_property_if_present(input_queue, "max-size-time", TTS_APP_QUEUE_NS)
        set_property_if_present(input_queue, "max-size-bytes", 0)
        set_property_if_present(input_queue, "max-size-buffers", 0)
        convert = make_element(Gst, "audioconvert", "tts_convert")
        resample = make_element(Gst, "audioresample", "tts_resample")
        caps = make_element(Gst, "capsfilter", "tts_caps")
        caps.set_property("caps", Gst.Caps.from_string(OUTPUT_CAPS))
        level = make_element(Gst, "level", self.tts_level_name)
        set_property_if_present(level, "interval", 100_000_000)
        set_property_if_present(level, "post-messages", True)
        mix_queue = make_element(Gst, "queue", "tts_mix_queue")
        set_property_if_present(mix_queue, "max-size-time", TTS_APP_QUEUE_NS)
        set_property_if_present(mix_queue, "max-size-bytes", 0)
        set_property_if_present(mix_queue, "max-size-buffers", 0)

        self._add_and_link([src, input_queue, convert, resample, caps, level, mix_queue])
        self._link_to_mixer(mix_queue, mixer)
        self.tts_appsrc = src

    def _add_output_branch(self, mixer) -> None:
        Gst = self.Gst
        convert = make_element(Gst, "audioconvert", "mix_convert")
        resample = make_element(Gst, "audioresample", "mix_resample")
        caps = make_element(Gst, "capsfilter", "mix_caps")
        caps.set_property("caps", Gst.Caps.from_string(OUTPUT_CAPS))
        queue = make_element(Gst, "queue", "mix_output_queue")
        set_property_if_present(queue, "leaky", 2)
        set_property_if_present(queue, "max-size-time", int(0.25 * 1_000_000_000))
        set_property_if_present(queue, "max-size-bytes", 0)
        set_property_if_present(queue, "max-size-buffers", 0)
        sink = make_element(Gst, "fdsink", "pipe_sink")
        sink.set_property("fd", self.pipe_fd)
        sink.set_property("sync", False)
        sink.set_property("blocksize", max(1, int(OUTPUT_RATE * OUTPUT_CHANNELS * 2 * MIXER_BUFFER_MS / 1000)))
        set_property_if_present(sink, "async", False)
        set_property_if_present(sink, "enable-last-sample", False)
        set_property_if_present(sink, "max-lateness", -1)

        self._add_and_link([convert, resample, caps, queue, sink])
        if not mixer.link(convert):
            raise RuntimeError("Could not link mixer to output branch")

    def _handle_control_requests(self) -> None:
        while True:
            try:
                item = self._control_requests.get_nowait()
            except queue.Empty:
                return
            try:
                response = self._handle_tts_webrtc_offer(item.payload)
            except Exception as exc:
                log.exception("WebRTC TTS negotiation failed")
                response = {"ok": False, "error": str(exc)}
            item.response_queue.put(response)

    def _handle_tts_webrtc_offer(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = str(payload.get("action") or "offer").lower()
        if action == "control":
            return self._handle_tts_webrtc_control(payload)
        if action != "offer":
            raise ValueError(f"Unsupported TTS WebRTC mixer action: {action}")
        if self.pipeline is None or self.mixer is None or self.tts_appsrc is None:
            raise RuntimeError("Zone mixer is not ready")
        sdp = str(payload.get("sdp") or "")
        sdp_type = str(payload.get("type") or "offer").lower()
        if not sdp:
            raise ValueError("sdp is required")
        if sdp_type != "offer":
            raise ValueError("Only WebRTC SDP offers are accepted")

        session_id = safe_request_id(payload.get("session_id") or payload.get("request_id"))
        active_request_id = safe_request_id(payload.get("request_id") or session_id)
        duck_gain = clamp_float(payload.get("duck_gain"), 0.0, 1.0, self._tts_duck_gain)
        log.info("WebRTC TTS offer received session_id=%s request_id=%s", session_id, active_request_id)
        with self._sessions_lock:
            has_existing_session = session_id in self._sessions
        if has_existing_session:
            self._remove_webrtc_session(session_id, reason="renegotiated")

        response = self._run_webrtc_coro(
            self._create_aiortc_session(
                session_id=session_id,
                request_id=active_request_id,
                duck_gain=duck_gain,
                remote_sdp=sdp,
                remote_type=sdp_type,
            ),
            timeout_s=WEBRTC_NEGOTIATION_TIMEOUT_SECONDS,
        )
        log.info(
            "WebRTC TTS negotiated session_id=%s request_id=%s duck_gain=%.2f",
            session_id,
            active_request_id,
            duck_gain,
        )
        return response

    async def _create_aiortc_session(
        self,
        *,
        session_id: str,
        request_id: str,
        duck_gain: float,
        remote_sdp: str,
        remote_type: str,
    ) -> dict[str, Any]:
        from aiortc import RTCPeerConnection, RTCSessionDescription

        peer = RTCPeerConnection()
        session = TtsWebRtcSession(
            session_id=session_id,
            peer=peer,
            duck_gain=duck_gain,
            created_at=time.monotonic(),
            active_request_id=request_id,
        )
        with self._sessions_lock:
            self._sessions[session_id] = session
            self._active_session_id = session_id

        @peer.on("track")
        def on_track(track) -> None:
            if getattr(track, "kind", "") != "audio":
                log.info("Ignoring non-audio WebRTC track session_id=%s kind=%s", session_id, getattr(track, "kind", ""))
                return
            task = asyncio.create_task(self._consume_tts_track(session, track))
            session.tasks.add(task)
            task.add_done_callback(session.tasks.discard)
            log.info("WebRTC TTS audio track attached session_id=%s", session_id)

        @peer.on("connectionstatechange")
        async def on_connection_state_change() -> None:
            session.connection_state = str(peer.connectionState)
            log.info("WebRTC TTS connection session_id=%s state=%s", session_id, session.connection_state)
            if peer.connectionState in {"failed", "closed", "disconnected"} and session.terminal_at <= 0:
                session.terminal_at = time.monotonic()

        try:
            await peer.setRemoteDescription(RTCSessionDescription(sdp=remote_sdp, type=remote_type))
            answer = await peer.createAnswer()
            await peer.setLocalDescription(answer)
            await wait_aiortc_ice_complete(peer, timeout_s=WEBRTC_ICE_GATHER_TIMEOUT_SECONDS)
            description = peer.localDescription
            if description is None:
                raise RuntimeError("aiortc did not create a local SDP answer")
            return {
                "ok": True,
                "type": description.type,
                "sdp": description.sdp,
                "session_id": session_id,
                "request_id": request_id,
                "transport": "webrtc",
                "engine": "aiortc-receiver-gstreamer-appsrc",
                "internal_audio_target": "gstreamer-audiomixer",
                "persistent": True,
            }
        except Exception:
            with self._sessions_lock:
                self._sessions.pop(session_id, None)
                if self._active_session_id == session_id:
                    self._active_session_id = ""
            await session.close()
            raise

    async def _consume_tts_track(self, session: TtsWebRtcSession, track) -> None:
        from aiortc.mediastreams import MediaStreamError
        from av.audio.resampler import AudioResampler
        import numpy as np

        resampler = AudioResampler(format="s16", layout="mono", rate=TTS_APPSRC_RATE)
        try:
            while not self._stop and not session.closed:
                try:
                    frame = await track.recv()
                except MediaStreamError:
                    break
                session.media_started = True
                for out_frame in resampler.resample(frame):
                    samples = out_frame.to_ndarray()
                    samples = np.ascontiguousarray(samples.reshape(-1), dtype=np.int16)
                    if samples.size == 0:
                        continue
                    self._push_tts_samples(samples.tobytes(), sample_count=int(samples.size), session=session)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("WebRTC TTS audio receiver failed session_id=%s", session.session_id)
        finally:
            if session.terminal_at <= 0:
                session.terminal_at = time.monotonic()
            log.info("WebRTC TTS audio receiver ended session_id=%s", session.session_id)

    def _push_tts_samples(self, data: bytes, *, sample_count: int, session: TtsWebRtcSession) -> None:
        appsrc = self.tts_appsrc
        if appsrc is None or self.Gst is None or not data:
            return
        buffer = self.Gst.Buffer.new_allocate(None, len(data), None)
        buffer.fill(0, data)
        buffer.duration = self.Gst.util_uint64_scale_int(sample_count, self.Gst.SECOND, TTS_APPSRC_RATE)
        result = appsrc.emit("push-buffer", buffer)
        if enum_nick(result) not in {"ok", "success"}:
            log.debug("TTS appsrc push-buffer returned %s", enum_nick(result))
        if s16le_audio_is_audible(data):
            self._mark_tts_activity(time.monotonic(), session)

    def _handle_tts_webrtc_control(self, payload: dict[str, Any]) -> dict[str, Any]:
        session_id = safe_request_id(payload.get("session_id") or payload.get("request_id"))
        with self._sessions_lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise RuntimeError(f"No active WebRTC TTS session for {session_id}")
        active_request_id = safe_request_id(payload.get("request_id") or session.active_request_id or session_id)
        session.active_request_id = active_request_id
        session.duck_gain = clamp_float(payload.get("duck_gain"), 0.0, 1.0, session.duck_gain)
        with self._sessions_lock:
            self._active_session_id = session_id
        log.info(
            "WebRTC TTS control session_id=%s request_id=%s duck_gain=%.2f",
            session_id,
            active_request_id,
            session.duck_gain,
        )
        return {
            "ok": True,
            "session_id": session_id,
            "request_id": active_request_id,
            "transport": "webrtc",
            "engine": "aiortc-receiver-gstreamer-appsrc",
            "internal_audio_target": "gstreamer-audiomixer",
            "persistent": True,
        }

    def _add_and_link(self, elements: list[object]) -> None:
        for element in elements:
            self.pipeline.add(element)
        for left, right in zip(elements, elements[1:]):
            if not left.link(right):
                raise RuntimeError(f"Could not link {left.get_name()} -> {right.get_name()}")

    def _link_to_mixer(self, src_element, mixer):
        src_pad = src_element.get_static_pad("src")
        if hasattr(mixer, "request_pad_simple"):
            sink_pad = mixer.request_pad_simple("sink_%u")
        else:
            sink_pad = None
        if sink_pad is None:
            sink_pad = mixer.get_request_pad("sink_%u")
        if sink_pad is None or src_pad is None or src_pad.link(sink_pad) != self.Gst.PadLinkReturn.OK:
            raise RuntimeError(f"Could not link {src_element.get_name()} to {MIXER_ELEMENT}")
        return sink_pad

    def _mark_tts_activity(self, now: float, session: TtsWebRtcSession | None) -> None:
        request_id = session.active_request_id if session is not None else self._tts_request_id
        if session is not None and not request_id:
            request_id = session.session_id
        duck_gain = session.duck_gain if session is not None else self._tts_duck_gain
        if session is not None:
            session.last_audio_at = now
        if not self._tts_active or self._tts_request_id != request_id:
            label = f" request_id={request_id}" if request_id else ""
            log.info("TTS WebRTC audio active%s", label)
        self._tts_request_id = request_id
        self._tts_duck_gain = clamp_float(duck_gain, 0.0, 1.0, DEFAULT_DUCK_GAIN)
        self._tts_active = True
        self._tts_last_activity_at = now
        self._duck_target = self._tts_duck_gain

    def _run_pipeline_loop(self) -> None:
        while not self._stop and self.pipeline is not None:
            self._drain_glib()
            self._handle_control_requests()
            self._handle_bus_messages()
            self._cleanup_webrtc_sessions()
            self._update_tts_activity()
            self._update_ducking()
            time.sleep(DUCK_UPDATE_SECONDS)

    def _drain_glib(self) -> None:
        if self.GLib is None:
            return
        context = self.GLib.MainContext.default()
        while context.pending():
            context.iteration(False)

    def _handle_bus_messages(self) -> None:
        Gst = self.Gst
        while self.bus is not None:
            msg = self.bus.pop_filtered(
                Gst.MessageType.ERROR
                | Gst.MessageType.WARNING
                | Gst.MessageType.EOS
                | Gst.MessageType.ELEMENT
            )
            if msg is None:
                return
            src_name = msg.src.get_name() if msg.src is not None else ""
            if msg.type == Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                raise PipelineRestart(f"{err.message}; {debug or 'no debug'}")
            if msg.type == Gst.MessageType.WARNING:
                warn, debug = msg.parse_warning()
                log.warning("GStreamer warning from %s: %s; %s", src_name, warn.message, debug or "no debug")
            elif msg.type == Gst.MessageType.EOS:
                raise PipelineRestart("unexpected pipeline EOS")
            elif msg.type == Gst.MessageType.ELEMENT:
                structure = msg.get_structure()
                if structure is not None and structure.has_name("level") and src_name == self.tts_level_name:
                    session = self._active_session()
                    if level_message_is_audible(structure):
                        self._mark_tts_activity(time.monotonic(), session)
                    continue
                if structure is not None and "drop" in structure.get_name().lower():
                    log.warning("GStreamer element message from %s: %s", src_name, structure.to_string())

    def _active_session(self) -> TtsWebRtcSession | None:
        with self._sessions_lock:
            return self._sessions.get(self._active_session_id)

    def _cleanup_webrtc_sessions(self) -> None:
        now = time.monotonic()
        with self._sessions_lock:
            sessions = list(self._sessions.items())
        for session_id, session in sessions:
            reason = ""
            if session.terminal_at > 0 and now - session.terminal_at >= 0.5:
                reason = "terminal"
            elif not session.media_started and now - session.created_at >= WEBRTC_NO_MEDIA_TIMEOUT_SECONDS:
                reason = "no_media"
            if reason:
                self._remove_webrtc_session(session_id, reason=reason)

    def _remove_webrtc_session(self, session_id: str, *, reason: str) -> None:
        with self._sessions_lock:
            session = self._sessions.pop(session_id, None)
            if self._active_session_id == session_id:
                self._active_session_id = ""
        if session is None:
            return
        log.info("Removing WebRTC TTS session session_id=%s reason=%s", session_id, reason)
        try:
            self._run_webrtc_coro(session.close(), timeout_s=3.0)
        except Exception as exc:
            log.warning("Could not close WebRTC session %s cleanly: %s", session_id, exc)

    def _update_tts_activity(self) -> None:
        if not self._tts_active:
            return
        now = time.monotonic()
        if now - self._tts_last_activity_at < TTS_IDLE_RELEASE_SECONDS:
            self._duck_target = self._tts_duck_gain
            return
        self._tts_active = False
        self._duck_target = 1.0
        self._duck_hold_until = max(self._duck_hold_until, now + TTS_DUCK_TAIL_SECONDS)
        self._duck_hold_gain = min(self._duck_hold_gain, self._tts_duck_gain)
        label = f" request_id={self._tts_request_id}" if self._tts_request_id else ""
        log.info("TTS WebRTC audio idle%s", label)

    def _update_ducking(self) -> None:
        if self.music_mixer_pad is None:
            return
        now = time.monotonic()
        elapsed = max(0.0, now - self._last_duck_update)
        self._last_duck_update = now

        target = clamp_float(self._duck_target, 0.0, 1.0, 1.0)
        if now < self._duck_hold_until:
            target = min(target, clamp_float(self._duck_hold_gain, 0.0, 1.0, 1.0))
        else:
            self._duck_hold_gain = 1.0
        seconds = DUCK_ATTACK_SECONDS if target < self._duck_level else DUCK_RELEASE_SECONDS
        step = elapsed / max(seconds, 0.001)
        if target < self._duck_level:
            self._duck_level = max(target, self._duck_level - step)
        else:
            self._duck_level = min(target, self._duck_level + step)
        set_property_if_present(self.music_mixer_pad, "volume", self._duck_level)

    def _stop_pipeline(self) -> None:
        for session_id in list(self._sessions):
            self._remove_webrtc_session(session_id, reason="pipeline_stop")
        if self.pipeline is not None:
            try:
                self.pipeline.set_state(self.Gst.State.NULL)
            except Exception:
                log.exception("Could not stop GStreamer pipeline cleanly")
        self.pipeline = None
        self.bus = None
        self.mixer = None
        self.music_mixer_pad = None
        self.tts_appsrc = None
        if self.pipe_fd is not None:
            try:
                os.close(self.pipe_fd)
            except OSError:
                pass
            self.pipe_fd = None


def require_gstreamer():
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import GLib, Gst

    Gst.init(None)
    return Gst, GLib


def require_aiortc_runtime() -> None:
    try:
        import aiortc  # noqa: F401
        import av  # noqa: F401
        import numpy  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "WebRTC TTS requires Python packages aiortc, av, and numpy in the Shiri mixer environment."
        ) from exc


async def wait_aiortc_ice_complete(peer: Any, *, timeout_s: float) -> None:
    if peer.iceGatheringState == "complete":
        return
    done = asyncio.Event()

    @peer.on("icegatheringstatechange")
    def on_ice_gathering_state_change() -> None:
        if peer.iceGatheringState == "complete":
            done.set()

    try:
        await asyncio.wait_for(done.wait(), timeout=timeout_s)
    except TimeoutError:
        return
    except asyncio.TimeoutError:
        return


def make_element(Gst, factory: str, name: str):
    element = Gst.ElementFactory.make(factory, name)
    if element is None:
        raise RuntimeError(f"Missing GStreamer element '{factory}'")
    return element


def set_property_if_present(element, prop: str, value) -> bool:
    if element.find_property(prop) is None:
        return False
    try:
        element.set_property(prop, value)
        return True
    except (TypeError, ValueError):
        return False


def enum_nick(value) -> str:
    if value is None:
        return ""
    nick = getattr(value, "value_nick", None)
    if nick:
        return str(nick).replace("-", "_")
    name = getattr(value, "name", None)
    if name:
        return str(name).lower().replace("-", "_")
    return str(value).lower().replace("-", "_")


def level_message_is_audible(structure) -> bool:
    for field in ("peak", "rms"):
        for value in structure_float_values(structure, field):
            if value > TTS_ACTIVE_LEVEL_DB:
                return True
    return False


def structure_float_values(structure, field: str) -> list[float]:
    try:
        value = structure.get_value(field)
    except Exception:
        return []
    if value is None:
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    try:
        return [float(item) for item in value]
    except (TypeError, ValueError):
        return []


def s16le_audio_is_audible(data: bytes) -> bool:
    if not data:
        return False
    try:
        import numpy as np

        samples = np.frombuffer(data, dtype=np.int16)
        if samples.size == 0:
            return False
        rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))
        return rms >= TTS_AUDIBLE_RMS
    except Exception:
        return any(data)


def clamp_float(value: object, minimum: float, maximum: float, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, minimum), maximum)


def safe_request_id(value: object) -> str:
    text = str(value or "").strip()
    allowed = []
    for char in text:
        if char.isalnum() or char in "_.-":
            allowed.append(char)
        else:
            allowed.append("_")
    text = "".join(allowed).strip("._-")
    return text or f"tts_{int(time.time() * 1000)}"


def safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("Could not remove %s: %s", path, exc)


class suppress_oserror:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, _exc, _tb):
        return exc_type is not None and issubclass(exc_type, (OSError, RuntimeError))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture-dev", required=True)
    parser.add_argument("--grp-dir", required=True, type=Path)
    parser.add_argument("--tts-webrtc-socket", type=Path)
    parser.add_argument("--tts-duck-gain", type=float, default=DEFAULT_DUCK_GAIN)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    GstZoneMixer(
        capture_dev=args.capture_dev,
        grp_dir=args.grp_dir,
        tts_webrtc_socket=args.tts_webrtc_socket,
        tts_duck_gain=args.tts_duck_gain,
    ).run()


if __name__ == "__main__":
    main()
