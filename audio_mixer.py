#!/usr/bin/env python3
"""
audio_mixer.py - Shiri zone audio mixer.

OwnTone reads one raw PCM FIFO per zone. This process feeds that FIFO with a
single GStreamer pipeline:

  ALSA loopback capture  -> volume -> \
  live silence bed ------------------> audiomixer -> OwnTone FIFO
  TTS appsrc -----------> convert/resample -> /

Python only owns control-plane work here: websocket stream intake, TTS appsrc
buffering, ducking targets, and FIFO reconnects. GStreamer owns live capture,
format conversion, resampling, mixing, clipping, and output pacing.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import errno
import fcntl
import json
import logging
import os
import queue
import re
import signal
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path


OUTPUT_RATE = 44100
OUTPUT_CHANNELS = 2
SAMPLE_WIDTH = 2
OUTPUT_CAPS = (
    f"audio/x-raw,format=S16LE,layout=interleaved,"
    f"rate={OUTPUT_RATE},channels={OUTPUT_CHANNELS}"
)

DEFAULT_DUCK_GAIN = 0.28
DUCK_ATTACK_SECONDS = 0.08
DUCK_RELEASE_SECONDS = 0.45
DUCK_UPDATE_SECONDS = 0.02
TTS_DUCK_START_FRACTION = 0.65

TTS_PREBUFFER_SECONDS = 0.10
TTS_PUSH_SECONDS = 0.02
TTS_START_LEAD_SECONDS = 0.05
TTS_BUFFER_AHEAD_SECONDS = 0.35
TTS_DRAIN_GRACE_SECONDS = 0.03
TTS_MAX_PUSH_BUFFERS_PER_TICK = 32

PIPE_RETRY_SECONDS = 0.4
PIPE_LOG_INTERVAL_SECONDS = 5.0
STREAM_START_TIMEOUT_SECONDS = 5.0
STREAM_STALL_TIMEOUT_SECONDS = 6.0
WS_RECV_TIMEOUT_SECONDS = 10.0
PLAYBACK_ACK_GRACE_SECONDS = 20.0
PLAYBACK_ACK_MAX_SECONDS = 120.0


log = logging.getLogger("shiri.audio_mixer")


class PipelineRestart(RuntimeError):
    """Raised when the GStreamer pipeline must be rebuilt."""


@dataclass
class LiveTTSBuffer:
    data: bytearray = field(default_factory=bytearray)
    lock: threading.Lock = field(default_factory=threading.Lock)
    complete: bool = False
    error: str | None = None
    total_received: int = 0
    created_at: float = field(default_factory=time.monotonic)
    last_write_at: float = field(default_factory=time.monotonic)

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        with self.lock:
            if self.complete:
                return
            self.data.extend(chunk)
            self.total_received += len(chunk)
            self.last_write_at = time.monotonic()

    def finish(self, error: str | None = None) -> None:
        with self.lock:
            self.complete = True
            if error:
                self.error = error
            self.last_write_at = time.monotonic()

    def available_bytes(self) -> int:
        with self.lock:
            return len(self.data)

    def read(self, limit: int) -> bytes:
        with self.lock:
            if not self.data:
                return b""
            size = min(limit, len(self.data))
            chunk = bytes(self.data[:size])
            del self.data[:size]
            return chunk


@dataclass
class TTSClip:
    request_id: str
    sample_rate: int
    channels: int
    sample_width: int
    duck_gain: float
    kind: str
    live_buffer: LiveTTSBuffer
    pending: bytes = b""
    loaded_at: float = field(default_factory=time.monotonic)
    last_progress_at: float = field(default_factory=time.monotonic)
    started_at: float | None = None
    pushed_buffers: int = 0
    pushed_bytes: int = 0
    pushed_duration_ns: int = 0
    next_pts_ns: int | None = None
    finished: threading.Event = field(default_factory=threading.Event)
    finish_reason: str | None = None
    finish_error: str | None = None
    finished_at: float | None = None

    @property
    def frame_width(self) -> int:
        return max(1, self.channels * self.sample_width)

    @property
    def push_chunk_bytes(self) -> int:
        frames = max(1, int(self.sample_rate * TTS_PUSH_SECONDS))
        return frames * self.frame_width

    @property
    def prebuffer_bytes(self) -> int:
        frames = max(1, int(self.sample_rate * TTS_PREBUFFER_SECONDS))
        return frames * self.frame_width

    def ready_to_start(self) -> bool:
        available = self.live_buffer.available_bytes()
        return available >= self.prebuffer_bytes or (self.live_buffer.complete and available > 0)

    def source_size(self) -> int:
        return self.live_buffer.total_received

    def source_complete(self) -> bool:
        return self.live_buffer.complete

    def exhausted(self) -> bool:
        return self.live_buffer.complete and self.live_buffer.available_bytes() <= 0 and not self.pending

    def read_chunk(self) -> bytes:
        raw = self.live_buffer.read(self.push_chunk_bytes)
        if raw:
            self.pending += raw

        usable = (len(self.pending) // self.frame_width) * self.frame_width
        if usable <= 0:
            return b""

        chunk = self.pending[:usable]
        self.pending = self.pending[usable:]
        self.last_progress_at = time.monotonic()
        return chunk

    def should_drop(self) -> str | None:
        if self.live_buffer.complete:
            if self.live_buffer.error:
                return self.live_buffer.error
            return None
        now = time.monotonic()
        available = self.live_buffer.available_bytes()
        if self.live_buffer.total_received <= 0 and now - self.live_buffer.created_at > STREAM_START_TIMEOUT_SECONDS:
            return "websocket stream never received audio"
        if available <= 0 and now - self.live_buffer.last_write_at > STREAM_STALL_TIMEOUT_SECONDS:
            return "websocket stream stopped receiving audio before finish"
        return None

    def cleanup(self) -> None:
        return None

    def estimated_duration_seconds(self) -> float:
        frames = self.source_size() / max(1, self.frame_width)
        return frames / max(1, self.sample_rate)

    def mark_finished(self, *, reason: str, error: str | None = None) -> None:
        self.finish_reason = reason
        self.finish_error = error
        self.finished_at = time.monotonic()
        self.finished.set()

    def playback_result(self) -> dict[str, object]:
        return {
            "played": self.finished.is_set() and not self.finish_error,
            "playback_reason": self.finish_reason,
            "playback_error": self.finish_error,
            "received_bytes": self.source_size(),
            "streamed_bytes": self.source_size(),
            "played_bytes": self.pushed_bytes,
            "pushed_buffers": self.pushed_buffers,
            "estimated_audio_seconds": round(self.estimated_duration_seconds(), 3),
        }


class GstZoneMixer:
    def __init__(self, *, capture_dev: str, grp_dir: Path, tts_ws_port: int | None = None) -> None:
        self.capture_dev = capture_dev
        self.grp_dir = grp_dir
        self.pipe_path = grp_dir / "pipes" / "audio.pipe"
        self.tts_ws_port = int(tts_ws_port or 0)
        self.mixer_pid_path = grp_dir / "state" / "mixer.pid"
        self.legacy_arecord_pid_path = grp_dir / "state" / "arecord.pid"

        self.Gst = None
        self.pipeline = None
        self.bus = None
        self.pipe_fd: int | None = None
        self.tts_src = None
        self.music_volume = None

        self._stop = False
        self._active_clip: TTSClip | None = None
        self._tts_caps_key: tuple[int, int] | None = None
        self._duck_level = 1.0
        self._duck_target = 1.0
        self._last_duck_update = time.monotonic()
        self._last_pipe_wait_log = 0.0
        self._live_clips: queue.Queue[TTSClip] = queue.Queue()
        self._ws_thread: threading.Thread | None = None

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_stop)
        signal.signal(signal.SIGINT, self._handle_stop)
        self.mixer_pid_path.write_text(str(os.getpid()))
        safe_unlink(self.legacy_arecord_pid_path)

        log.info("Starting GStreamer mixer capture_dev=%s grp_dir=%s", self.capture_dev, self.grp_dir)
        try:
            self.Gst = require_gstreamer()
            self._start_tts_websocket()
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
            self._stop_pipeline()
            safe_unlink(self.mixer_pid_path)
            safe_unlink(self.legacy_arecord_pid_path)
            log.info("GStreamer mixer stopped")

    def _handle_stop(self, _signum: int, _frame: object) -> None:
        self._stop = True

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

    def _start_pipeline(self) -> None:
        if self.pipeline is not None:
            return
        Gst = self.Gst
        self.pipeline = Gst.Pipeline.new("shiri-zone-mixer")

        mixer = make_element(Gst, "audiomixer", "mix")
        set_property_if_present(mixer, "ignore-inactive-pads", True)
        set_property_if_present(mixer, "output-buffer-duration", 10_000_000)
        self.pipeline.add(mixer)

        self._add_silence_branch(mixer)
        self._add_music_branch(mixer)
        self._add_tts_branch(mixer)
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
        set_property_if_present(src, "samplesperbuffer", max(1, int(OUTPUT_RATE * 0.02)))
        caps = make_element(Gst, "capsfilter", "silence_caps")
        caps.set_property("caps", Gst.Caps.from_string(OUTPUT_CAPS))
        queue = make_element(Gst, "queue", "silence_queue")
        self._add_and_link([src, caps, queue])
        self._link_to_mixer(queue, mixer)

    def _add_music_branch(self, mixer) -> None:
        Gst = self.Gst
        src = make_element(Gst, "alsasrc", "loopback_src")
        src.set_property("device", self.capture_dev)
        src.set_property("do-timestamp", True)
        set_property_if_present(src, "provide-clock", False)
        set_property_if_present(src, "latency-time", 10_000)
        set_property_if_present(src, "buffer-time", 50_000)

        caps = make_element(Gst, "capsfilter", "loopback_caps")
        caps.set_property("caps", Gst.Caps.from_string(OUTPUT_CAPS))
        queue = make_element(Gst, "queue", "loopback_queue")
        self.music_volume = make_element(Gst, "volume", "music_volume")
        self.music_volume.set_property("volume", 1.0)

        self._add_and_link([src, caps, queue, self.music_volume])
        self._link_to_mixer(self.music_volume, mixer)

    def _add_tts_branch(self, mixer) -> None:
        Gst = self.Gst
        self.tts_src = make_element(Gst, "appsrc", "tts_src")
        self.tts_src.set_property("is-live", True)
        self.tts_src.set_property("format", Gst.Format.TIME)
        self.tts_src.set_property("block", False)
        self.tts_src.set_property("do-timestamp", False)
        self.tts_src.set_property("emit-signals", False)
        set_property_if_present(self.tts_src, "max-bytes", 2 * 1024 * 1024)
        set_property_if_present(self.tts_src, "min-latency", 0)
        set_property_if_present(self.tts_src, "max-latency", int(0.5 * 1_000_000_000))

        buffer_queue = make_element(Gst, "queue", "tts_buffer")
        set_property_if_present(buffer_queue, "max-size-time", int(0.5 * 1_000_000_000))
        set_property_if_present(buffer_queue, "max-size-bytes", 0)
        convert = make_element(Gst, "audioconvert", "tts_convert")
        resample = make_element(Gst, "audioresample", "tts_resample")
        caps = make_element(Gst, "capsfilter", "tts_output_caps")
        caps.set_property("caps", Gst.Caps.from_string(OUTPUT_CAPS))

        self._add_and_link([self.tts_src, buffer_queue, convert, resample, caps])
        self._link_to_mixer(caps, mixer)

    def _add_output_branch(self, mixer) -> None:
        Gst = self.Gst
        convert = make_element(Gst, "audioconvert", "mix_convert")
        resample = make_element(Gst, "audioresample", "mix_resample")
        caps = make_element(Gst, "capsfilter", "mix_caps")
        caps.set_property("caps", Gst.Caps.from_string(OUTPUT_CAPS))
        sink = make_element(Gst, "fdsink", "pipe_sink")
        sink.set_property("fd", self.pipe_fd)
        sink.set_property("sync", False)
        set_property_if_present(sink, "async", False)
        set_property_if_present(sink, "enable-last-sample", False)

        self._add_and_link([convert, resample, caps, sink])
        if not mixer.link(convert):
            raise RuntimeError("Could not link mixer to output branch")

    def _add_and_link(self, elements: list[object]) -> None:
        for element in elements:
            self.pipeline.add(element)
        for left, right in zip(elements, elements[1:]):
            if not left.link(right):
                raise RuntimeError(f"Could not link {left.get_name()} -> {right.get_name()}")

    def _link_to_mixer(self, src_element, mixer) -> None:
        src_pad = src_element.get_static_pad("src")
        if hasattr(mixer, "request_pad_simple"):
            sink_pad = mixer.request_pad_simple("sink_%u")
        else:
            sink_pad = None
        if sink_pad is None:
            sink_pad = mixer.get_request_pad("sink_%u")
        if sink_pad is None or src_pad.link(sink_pad) != self.Gst.PadLinkReturn.OK:
            raise RuntimeError(f"Could not link {src_element.get_name()} to audiomixer")

    def _run_pipeline_loop(self) -> None:
        while not self._stop and self.pipeline is not None:
            self._handle_bus_messages()
            self._pump_tts()
            self._update_ducking()
            time.sleep(DUCK_UPDATE_SECONDS)

    def _handle_bus_messages(self) -> None:
        Gst = self.Gst
        while self.bus is not None:
            msg = self.bus.pop_filtered(
                Gst.MessageType.ERROR
                | Gst.MessageType.WARNING
                | Gst.MessageType.EOS
                | Gst.MessageType.STATE_CHANGED
            )
            if msg is None:
                return
            if msg.type == Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                raise PipelineRestart(f"{err.message}; {debug or 'no debug'}")
            if msg.type == Gst.MessageType.WARNING:
                warn, debug = msg.parse_warning()
                log.warning("GStreamer warning from %s: %s; %s",
                            msg.src.get_name(), warn.message, debug or "no debug")
            elif msg.type == Gst.MessageType.EOS:
                raise PipelineRestart("unexpected pipeline EOS")

    def _pump_tts(self) -> None:
        if self.tts_src is None:
            return
        if self._active_clip is None:
            self._active_clip = self._load_next_clip()
            if self._active_clip is None:
                self._duck_target = 1.0
                return

        clip = self._active_clip
        drop_reason = clip.should_drop()
        if drop_reason:
            log.warning("Dropping stalled TTS request %s: %s", clip.request_id, drop_reason)
            clip.mark_finished(reason="dropped", error=drop_reason)
            clip.cleanup()
            self._active_clip = None
            self._duck_target = 1.0
            return

        source_size = clip.source_size()
        if source_size > 0:
            self._duck_target = clip.duck_gain

        if not clip.ready_to_start():
            if clip.source_complete() and source_size <= 0:
                log.warning("Dropping empty TTS request %s", clip.request_id)
                clip.mark_finished(reason="empty", error="empty TTS stream")
                clip.cleanup()
                self._active_clip = None
                self._duck_target = 1.0
                return
            if source_size <= 0:
                self._duck_target = 1.0
            return

        self._duck_target = clip.duck_gain
        if clip.started_at is None:
            if not self._duck_ready_for_tts(clip.duck_gain):
                return
            self._start_active_clip()

        self._fill_tts_appsrc(clip)

        if clip.exhausted() and self._tts_buffer_ahead_ns(clip) <= int(TTS_DRAIN_GRACE_SECONDS * 1_000_000_000):
            self._finish_active_clip(clip, reason="drained")

    def _start_active_clip(self) -> None:
        clip = self._active_clip
        if clip is None:
            return
        self._set_tts_caps(clip)
        clip.started_at = time.monotonic()
        clip.next_pts_ns = self._pipeline_running_time_ns() + int(TTS_START_LEAD_SECONDS * 1_000_000_000)
        log.info(
            "Starting %s TTS request %s sample_rate=%d channels=%d duck_gain=%.2f prebuffer=%d bytes start_pts_ms=%d",
            clip.kind,
            clip.request_id,
            clip.sample_rate,
            clip.channels,
            clip.duck_gain,
            min(clip.source_size(), clip.prebuffer_bytes),
            int((clip.next_pts_ns or 0) / 1_000_000),
        )

    def _set_tts_caps(self, clip: TTSClip) -> None:
        caps_key = (clip.sample_rate, clip.channels)
        if caps_key == self._tts_caps_key:
            return
        caps_text = (
            "audio/x-raw,format=S16LE,layout=interleaved,"
            f"rate={clip.sample_rate},channels={clip.channels}"
        )
        self.tts_src.set_property("caps", self.Gst.Caps.from_string(caps_text))
        self._tts_caps_key = caps_key
        log.info("Configured TTS appsrc caps: %s", caps_text)

    def _fill_tts_appsrc(self, clip: TTSClip) -> None:
        target_ahead_ns = int(TTS_BUFFER_AHEAD_SECONDS * 1_000_000_000)
        pushed = 0
        while (
            not self._stop
            and self._tts_buffer_ahead_ns(clip) < target_ahead_ns
            and pushed < TTS_MAX_PUSH_BUFFERS_PER_TICK
        ):
            chunk = clip.read_chunk()
            if not chunk:
                return
            self._push_tts_buffer(clip, chunk)
            pushed += 1

    def _push_tts_buffer(self, clip: TTSClip, chunk: bytes) -> None:
        frames = len(chunk) // clip.frame_width
        duration_ns = int(frames * 1_000_000_000 / clip.sample_rate)
        pts_ns = clip.next_pts_ns
        if pts_ns is None:
            pts_ns = self._pipeline_running_time_ns()
        buffer = self.Gst.Buffer.new_allocate(None, len(chunk), None)
        buffer.fill(0, chunk)
        buffer.pts = pts_ns
        buffer.dts = pts_ns
        buffer.duration = duration_ns
        if clip.pushed_buffers == 0:
            buffer.set_flags(self.Gst.BufferFlags.DISCONT)
        ret = self.tts_src.emit("push-buffer", buffer)
        if ret != self.Gst.FlowReturn.OK:
            raise PipelineRestart(f"TTS appsrc push failed: {ret.value_nick}")
        clip.pushed_buffers += 1
        clip.pushed_bytes += len(chunk)
        clip.pushed_duration_ns += duration_ns
        clip.next_pts_ns = pts_ns + duration_ns

    def _tts_buffer_ahead_ns(self, clip: TTSClip) -> int:
        if clip.next_pts_ns is None:
            return 0
        return max(0, clip.next_pts_ns - self._pipeline_running_time_ns())

    def _finish_active_clip(self, clip: TTSClip, *, reason: str) -> None:
        received_bytes = clip.source_size()
        log.info(
            "Finished TTS request %s reason=%s received=%d pushed=%d buffers=%d",
            clip.request_id,
            reason,
            received_bytes,
            clip.pushed_bytes,
            clip.pushed_buffers,
        )
        clip.mark_finished(reason=reason)
        clip.cleanup()
        self._active_clip = None
        self._duck_target = 1.0

    def _duck_ready_for_tts(self, duck_gain: float) -> bool:
        target = clamp_float(duck_gain, 0.0, 1.0, DEFAULT_DUCK_GAIN)
        if target >= 0.98:
            return True
        start_level = 1.0 - ((1.0 - target) * TTS_DUCK_START_FRACTION)
        return self._duck_level <= start_level

    def _pipeline_running_time_ns(self) -> int:
        if self.pipeline is None:
            return 0
        clock = self.pipeline.get_clock()
        if clock is None:
            return 0
        return max(0, int(clock.get_time() - self.pipeline.get_base_time()))

    def _load_next_clip(self) -> TTSClip | None:
        try:
            return self._live_clips.get_nowait()
        except queue.Empty:
            return None

    def _update_ducking(self) -> None:
        if self.music_volume is None:
            return
        now = time.monotonic()
        elapsed = max(0.0, now - self._last_duck_update)
        self._last_duck_update = now

        target = clamp_float(self._duck_target, 0.0, 1.0, 1.0)
        seconds = DUCK_ATTACK_SECONDS if target < self._duck_level else DUCK_RELEASE_SECONDS
        step = elapsed / max(seconds, 0.001)
        if target < self._duck_level:
            self._duck_level = max(target, self._duck_level - step)
        else:
            self._duck_level = min(target, self._duck_level + step)
        self.music_volume.set_property("volume", self._duck_level)

    def _stop_pipeline(self) -> None:
        if self.pipeline is not None:
            try:
                self.pipeline.set_state(self.Gst.State.NULL)
            except Exception:
                log.exception("Could not stop GStreamer pipeline cleanly")
        self.pipeline = None
        self.bus = None
        self.tts_src = None
        self.music_volume = None
        self._tts_caps_key = None
        if self.pipe_fd is not None:
            try:
                os.close(self.pipe_fd)
            except OSError:
                pass
            self.pipe_fd = None

    def _start_tts_websocket(self) -> None:
        if self.tts_ws_port <= 0:
            return
        self._ws_thread = threading.Thread(
            target=self._run_tts_websocket_server,
            name=f"tts-ws-{self.tts_ws_port}",
            daemon=True,
        )
        self._ws_thread.start()

    def _run_tts_websocket_server(self) -> None:
        try:
            asyncio.run(self._serve_tts_websocket())
        except Exception:
            log.exception("TTS websocket server failed")

    async def _serve_tts_websocket(self) -> None:
        import websockets

        async with websockets.serve(
            self._handle_tts_websocket,
            "0.0.0.0",
            self.tts_ws_port,
            max_size=2 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
        ):
            log.info("TTS websocket listening on 0.0.0.0:%d", self.tts_ws_port)
            while not self._stop:
                await asyncio.sleep(0.2)

    async def _handle_tts_websocket(self, websocket, _path: str | None = None) -> None:
        clip: TTSClip | None = None
        queued = False
        try:
            first = await asyncio.wait_for(websocket.recv(), timeout=WS_RECV_TIMEOUT_SECONDS)
            if not isinstance(first, str):
                await websocket.send(json.dumps({"type": "error", "error": "first message must be JSON start"}))
                return
            start = json.loads(first)
            if start.get("type") != "start":
                await websocket.send(json.dumps({"type": "error", "error": "first message must have type=start"}))
                return
            clip = self._clip_from_websocket_start(start)
            await websocket.send(json.dumps({
                "type": "started",
                "request_id": clip.request_id,
                "sample_rate": clip.sample_rate,
                "channels": clip.channels,
                "sample_width": clip.sample_width,
            }))

            async for message in websocket:
                if isinstance(message, bytes):
                    clip.live_buffer.append(message)
                    if not queued and clip.live_buffer.total_received > 0:
                        self._live_clips.put(clip)
                        queued = True
                        log.info(
                            "Queued websocket TTS request %s after first audio received=%d",
                            clip.request_id,
                            clip.live_buffer.total_received,
                        )
                    continue
                payload = json.loads(message)
                msg_type = payload.get("type")
                if msg_type == "end":
                    if not queued and clip.live_buffer.total_received > 0:
                        self._live_clips.put(clip)
                        queued = True
                        log.info(
                            "Queued websocket TTS request %s at stream end received=%d",
                            clip.request_id,
                            clip.live_buffer.total_received,
                        )
                    clip.live_buffer.finish()
                    log.info(
                        "Received websocket TTS request %s complete received=%d",
                        clip.request_id,
                        clip.live_buffer.total_received,
                    )
                    playback = await self._wait_for_playback_result(clip, queued=queued)
                    await websocket.send(json.dumps({
                        "type": "ended",
                        "request_id": clip.request_id,
                        "received_bytes": clip.live_buffer.total_received,
                        **playback,
                    }))
                    return
                if msg_type == "cancel":
                    if queued:
                        clip.live_buffer.finish(str(payload.get("error") or "cancelled"))
                    log.info("Cancelled websocket TTS request %s", clip.request_id)
                    await websocket.send(json.dumps({"type": "cancelled", "request_id": clip.request_id}))
                    return
                await websocket.send(json.dumps({"type": "error", "error": f"unknown message type: {msg_type}"}))
            if queued and clip.live_buffer is not None:
                clip.live_buffer.finish()
        except Exception as exc:
            if queued and clip is not None and clip.live_buffer is not None:
                clip.live_buffer.finish(str(exc))
            with contextlib.suppress(Exception):
                await websocket.send(json.dumps({"type": "error", "error": str(exc)}))

    async def _wait_for_playback_result(self, clip: TTSClip, *, queued: bool) -> dict[str, object]:
        if not queued:
            return {
                "played": False,
                "playback_reason": "empty",
                "playback_error": "websocket ended without audio",
                "streamed_bytes": 0,
                "played_bytes": 0,
                "pushed_buffers": 0,
                "estimated_audio_seconds": 0.0,
            }
        timeout = min(
            PLAYBACK_ACK_MAX_SECONDS,
            max(PLAYBACK_ACK_GRACE_SECONDS, clip.estimated_duration_seconds() + PLAYBACK_ACK_GRACE_SECONDS),
        )
        completed = await asyncio.to_thread(clip.finished.wait, timeout)
        if completed:
            return clip.playback_result()
        log.warning(
            "Timed out waiting for TTS playback to drain request=%s received=%d pushed=%d",
            clip.request_id,
            clip.source_size(),
            clip.pushed_bytes,
        )
        result = clip.playback_result()
        result.update({
            "played": False,
            "playback_reason": "timeout",
            "playback_error": "mixer did not finish playback before websocket ack timeout",
        })
        return result

    def _clip_from_websocket_start(self, payload: dict) -> TTSClip:
        sample_rate = int(payload.get("sample_rate") or 24000)
        channels = int(payload.get("channels") or 1)
        sample_width = int(payload.get("sample_width") or 2)
        audio_format = str(payload.get("format") or "pcm_s16le").lower().strip(".")
        if audio_format not in {"pcm_s16le", "raw"}:
            raise ValueError("websocket TTS only accepts raw signed 16-bit PCM")
        if sample_rate <= 0 or channels <= 0:
            raise ValueError("sample_rate and channels must be positive")
        if sample_width != 2:
            raise ValueError("websocket TTS only accepts 16-bit PCM")
        return TTSClip(
            request_id=safe_request_id(payload.get("request_id")),
            sample_rate=sample_rate,
            channels=channels,
            sample_width=sample_width,
            duck_gain=clamp_float(payload.get("duck_gain"), 0.0, 1.0, DEFAULT_DUCK_GAIN),
            kind="websocket",
            live_buffer=LiveTTSBuffer(),
        )


def require_gstreamer():
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    return Gst


def make_element(Gst, factory: str, name: str):
    element = Gst.ElementFactory.make(factory, name)
    if element is None:
        raise RuntimeError(f"Missing GStreamer element '{factory}'")
    return element


def set_property_if_present(element, prop: str, value) -> bool:
    if element.find_property(prop) is None:
        return False
    element.set_property(prop, value)
    return True


def get_int_property(element, prop: str, default: int) -> int:
    if element.find_property(prop) is None:
        return default
    try:
        value = element.get_property(prop)
    except Exception:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def clamp_float(value: object, minimum: float, maximum: float, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, minimum), maximum)


def safe_request_id(value: object) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("._-")
    return text or f"tts_{int(time.time() * 1000)}"


def safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("Could not remove %s: %s", path, exc)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture-dev", required=True)
    parser.add_argument("--grp-dir", required=True, type=Path)
    parser.add_argument("--tts-ws-port", type=int, default=0)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    GstZoneMixer(capture_dev=args.capture_dev, grp_dir=args.grp_dir, tts_ws_port=args.tts_ws_port).run()


if __name__ == "__main__":
    main()
