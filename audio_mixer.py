#!/usr/bin/env python3
"""
audio_mixer.py - Shiri zone audio mixer.

OwnTone reads one raw PCM FIFO per zone. This process feeds that FIFO with a
single GStreamer pipeline:

  ALSA loopback capture  -> volume -> \
  live silence bed ------------------> audiomixer -> OwnTone FIFO
  TTS appsrc -----------> convert/resample -> /

Python only owns control-plane work here: queue/stream discovery, TTS appsrc
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
import re
import signal
import threading
import time
import wave
from dataclasses import dataclass, field
from glob import glob
from pathlib import Path


OUTPUT_RATE = 44100
OUTPUT_CHANNELS = 2
SAMPLE_WIDTH = 2
OUTPUT_CAPS = (
    f"audio/x-raw,format=S16LE,layout=interleaved,"
    f"rate={OUTPUT_RATE},channels={OUTPUT_CHANNELS}"
)

DEFAULT_DUCK_GAIN = 0.28
DUCK_ATTACK_SECONDS = 0.04
DUCK_RELEASE_SECONDS = 0.25
DUCK_UPDATE_SECONDS = 0.02

TTS_PREBUFFER_SECONDS = 0.10
TTS_PUSH_SECONDS = 0.02
TTS_TARGET_QUEUE_SECONDS = 0.18

PIPE_RETRY_SECONDS = 0.4
PIPE_LOG_INTERVAL_SECONDS = 5.0
WS_RECV_TIMEOUT_SECONDS = 10.0


log = logging.getLogger("shiri.audio_mixer")


class PipelineRestart(RuntimeError):
    """Raised when the GStreamer pipeline must be rebuilt."""


@dataclass
class DirectTTSSession:
    request_id: str
    sample_rate: int
    channels: int
    sample_width: int
    duck_gain: float
    pending: bytes = b""
    next_pts_ns: int | None = None
    received_bytes: int = 0
    pushed_bytes: int = 0
    pushed_buffers: int = 0
    started_at: float = field(default_factory=time.monotonic)

    @property
    def frame_width(self) -> int:
        return max(1, self.channels * self.sample_width)

    @property
    def push_chunk_bytes(self) -> int:
        frames = max(1, int(self.sample_rate * TTS_PUSH_SECONDS))
        return frames * self.frame_width


@dataclass
class TTSClip:
    request_id: str
    sample_rate: int
    channels: int
    sample_width: int
    duck_gain: float
    kind: str
    data: bytes | None = None
    pcm_path: Path | None = None
    meta_path: Path | None = None
    done_path: Path | None = None
    cleanup_paths: tuple[Path, ...] = ()
    position: int = 0
    pending: bytes = b""
    loaded_at: float = field(default_factory=time.monotonic)
    last_progress_at: float = field(default_factory=time.monotonic)
    started_at: float | None = None
    fully_pushed_at: float | None = None
    next_pts_ns: int | None = None
    pushed_buffers: int = 0

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
        size = self.source_size()
        return size > 0

    def source_size(self) -> int:
        if self.data is not None:
            return len(self.data)
        if self.pcm_path is None:
            return 0
        try:
            return self.pcm_path.stat().st_size
        except OSError:
            return 0

    def source_complete(self) -> bool:
        if self.data is not None:
            return True
        if self.done_path and self.done_path.exists():
            return True
        if not self.meta_path:
            return False
        try:
            meta = json.loads(self.meta_path.read_text())
            return bool(meta.get("complete"))
        except Exception:
            return False

    def exhausted(self) -> bool:
        return self.source_complete() and self.position >= self.source_size() and not self.pending

    def read_chunk(self) -> bytes:
        raw = self._read_raw(self.push_chunk_bytes)
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
        if self.kind != "stream" or self.source_complete():
            return None
        return None

    def cleanup(self) -> None:
        for path in self.cleanup_paths:
            safe_unlink(path)

    def _read_raw(self, limit: int) -> bytes:
        if self.data is not None:
            if self.position >= len(self.data):
                return b""
            chunk = self.data[self.position:self.position + limit]
            self.position += len(chunk)
            return chunk

        if self.pcm_path is None:
            return b""
        try:
            available = self.pcm_path.stat().st_size
            if available <= self.position:
                return b""
            with self.pcm_path.open("rb") as fh:
                fh.seek(self.position)
                chunk = fh.read(min(limit, available - self.position))
            self.position += len(chunk)
            return chunk
        except OSError:
            return b""


class GstZoneMixer:
    def __init__(self, *, capture_dev: str, grp_dir: Path, tts_ws_port: int | None = None) -> None:
        self.capture_dev = capture_dev
        self.grp_dir = grp_dir
        self.pipe_path = grp_dir / "pipes" / "audio.pipe"
        self.queue_dir = grp_dir / "tts_queue"
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
        self._ws_thread: threading.Thread | None = None
        self._direct_lock = threading.Lock()
        self._direct_active = False
        self._direct_request_id: str | None = None

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_stop)
        signal.signal(signal.SIGINT, self._handle_stop)
        self.queue_dir.mkdir(parents=True, exist_ok=True)
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
        self.tts_src.set_property("block", True)
        self.tts_src.set_property("do-timestamp", False)
        self.tts_src.set_property("emit-signals", False)
        set_property_if_present(self.tts_src, "max-time", int(TTS_TARGET_QUEUE_SECONDS * 1_000_000_000))
        set_property_if_present(self.tts_src, "max-bytes", 512 * 1024)
        set_property_if_present(self.tts_src, "min-latency", 0)
        set_property_if_present(self.tts_src, "max-latency", int(0.5 * 1_000_000_000))

        queue = make_element(Gst, "queue", "tts_queue")
        set_property_if_present(queue, "max-size-time", int(0.25 * 1_000_000_000))
        set_property_if_present(queue, "max-size-bytes", 0)
        convert = make_element(Gst, "audioconvert", "tts_convert")
        resample = make_element(Gst, "audioresample", "tts_resample")
        caps = make_element(Gst, "capsfilter", "tts_output_caps")
        caps.set_property("caps", Gst.Caps.from_string(OUTPUT_CAPS))

        self._add_and_link([self.tts_src, queue, convert, resample, caps])
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
        if self._direct_active:
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
            clip.cleanup()
            self._active_clip = None
            self._duck_target = 1.0
            return

        if not clip.ready_to_start():
            if clip.source_complete() and clip.source_size() <= 0:
                log.warning("Dropping empty TTS request %s", clip.request_id)
                clip.cleanup()
                self._active_clip = None
                self._duck_target = 1.0
                return
            self._duck_target = 1.0
            return

        if clip.started_at is None:
            self._start_active_clip()

        self._duck_target = clip.duck_gain
        self._fill_tts_queue(clip)

        if clip.exhausted():
            if clip.fully_pushed_at is None:
                clip.fully_pushed_at = time.monotonic()
            end_ns = clip.next_pts_ns or self._pipeline_running_time_ns()
            if self._pipeline_running_time_ns() >= end_ns:
                log.info("Finished TTS request %s", clip.request_id)
                clip.cleanup()
                self._active_clip = None
                self._duck_target = 1.0

    def _start_active_clip(self) -> None:
        clip = self._active_clip
        if clip is None:
            return
        self._set_tts_caps(clip)
        clip.started_at = time.monotonic()
        clip.next_pts_ns = self._pipeline_running_time_ns() + int(0.05 * 1_000_000_000)
        log.info(
            "Starting %s TTS request %s sample_rate=%d channels=%d duck_gain=%.2f prebuffer=%d bytes",
            clip.kind,
            clip.request_id,
            clip.sample_rate,
            clip.channels,
            clip.duck_gain,
            min(clip.source_size(), clip.prebuffer_bytes),
        )

    def _set_tts_caps(self, clip: TTSClip) -> None:
        self._set_tts_caps_values(clip.sample_rate, clip.channels)

    def _set_tts_caps_values(self, sample_rate: int, channels: int) -> None:
        caps_key = (sample_rate, channels)
        if caps_key == self._tts_caps_key:
            return
        caps_text = (
            "audio/x-raw,format=S16LE,layout=interleaved,"
            f"rate={sample_rate},channels={channels}"
        )
        self.tts_src.set_property("caps", self.Gst.Caps.from_string(caps_text))
        self._tts_caps_key = caps_key
        log.info("Configured TTS appsrc caps: %s", caps_text)

    def _fill_tts_queue(self, clip: TTSClip) -> None:
        target_pts_ns = self._pipeline_running_time_ns() + int(TTS_TARGET_QUEUE_SECONDS * 1_000_000_000)
        while clip.next_pts_ns is None or clip.next_pts_ns < target_pts_ns:
            chunk = clip.read_chunk()
            if not chunk:
                return

            frames = len(chunk) // clip.frame_width
            duration_ns = int(frames * 1_000_000_000 / clip.sample_rate)
            buffer = self.Gst.Buffer.new_allocate(None, len(chunk), None)
            buffer.fill(0, chunk)
            if clip.next_pts_ns is None:
                clip.next_pts_ns = self._pipeline_running_time_ns() + int(0.05 * 1_000_000_000)
            buffer.pts = clip.next_pts_ns
            buffer.dts = clip.next_pts_ns
            buffer.duration = duration_ns
            if clip.pushed_buffers == 0:
                buffer.set_flags(self.Gst.BufferFlags.DISCONT)
            clip.next_pts_ns += duration_ns
            clip.pushed_buffers += 1
            ret = self.tts_src.emit("push-buffer", buffer)
            if ret != self.Gst.FlowReturn.OK:
                raise PipelineRestart(f"TTS appsrc push failed: {ret.value_nick}")

    def _pipeline_running_time_ns(self) -> int:
        if self.pipeline is None:
            return 0
        clock = self.pipeline.get_clock()
        if clock is None:
            return 0
        return max(0, int(clock.get_time() - self.pipeline.get_base_time()))

    def _load_next_clip(self) -> TTSClip | None:
        clip = self._load_next_wav_clip()
        if clip is not None:
            return clip
        return None

    def _load_next_wav_clip(self) -> TTSClip | None:
        for meta_path_str in sorted(glob(str(self.queue_dir / "*.json"))):
            meta_path = Path(meta_path_str)
            try:
                meta = json.loads(meta_path.read_text())
                audio_path = Path(meta["audio_path"])
                if not audio_path.exists():
                    log.warning("Dropping TTS queue item with missing audio: %s", audio_path)
                    safe_unlink(meta_path)
                    continue
                clip = load_wav_clip(
                    request_id=str(meta.get("request_id") or meta_path.stem),
                    audio_path=audio_path,
                    meta_path=meta_path,
                    duck_gain=clamp_float(meta.get("duck_gain"), 0.0, 1.0, DEFAULT_DUCK_GAIN),
                )
                safe_unlink(audio_path)
                safe_unlink(meta_path)
                return clip
            except Exception:
                log.exception("Failed to load queued TTS item %s", meta_path)
                safe_unlink(meta_path)
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
        session: DirectTTSSession | None = None
        try:
            first = await asyncio.wait_for(websocket.recv(), timeout=WS_RECV_TIMEOUT_SECONDS)
            if not isinstance(first, str):
                await websocket.send(json.dumps({"type": "error", "error": "first message must be JSON start"}))
                return
            start = json.loads(first)
            if start.get("type") != "start":
                await websocket.send(json.dumps({"type": "error", "error": "first message must have type=start"}))
                return
            session = await asyncio.to_thread(self._begin_direct_tts_session, start)
            await websocket.send(json.dumps({
                "type": "started",
                "request_id": session.request_id,
                "sample_rate": session.sample_rate,
                "channels": session.channels,
                "sample_width": session.sample_width,
            }))

            async for message in websocket:
                if isinstance(message, bytes):
                    await asyncio.to_thread(self._push_direct_tts_chunk, session, message)
                    continue
                payload = json.loads(message)
                msg_type = payload.get("type")
                if msg_type == "end":
                    await asyncio.to_thread(self._finish_direct_tts_session, session)
                    await websocket.send(json.dumps({
                        "type": "ended",
                        "request_id": session.request_id,
                        "received_bytes": session.received_bytes,
                        "pushed_bytes": session.pushed_bytes,
                    }))
                    log.info(
                        "Finished websocket TTS request %s received=%d pushed=%d buffers=%d",
                        session.request_id,
                        session.received_bytes,
                        session.pushed_bytes,
                        session.pushed_buffers,
                    )
                    return
                if msg_type == "cancel":
                    await asyncio.to_thread(self._cancel_direct_tts_session, session)
                    await websocket.send(json.dumps({"type": "cancelled", "request_id": session.request_id}))
                    return
                await websocket.send(json.dumps({"type": "error", "error": f"unknown message type: {msg_type}"}))
            if session is not None:
                await asyncio.to_thread(self._finish_direct_tts_session, session)
        except Exception as exc:
            if session is not None:
                await asyncio.to_thread(self._cancel_direct_tts_session, session)
            with contextlib.suppress(Exception):
                await websocket.send(json.dumps({"type": "error", "error": str(exc)}))

    def _begin_direct_tts_session(self, payload: dict) -> DirectTTSSession:
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
        if self.tts_src is None or self.pipeline is None:
            raise RuntimeError("GStreamer pipeline is not ready")
        session = DirectTTSSession(
            request_id=safe_request_id(payload.get("request_id")),
            sample_rate=sample_rate,
            channels=channels,
            sample_width=sample_width,
            duck_gain=clamp_float(payload.get("duck_gain"), 0.0, 1.0, DEFAULT_DUCK_GAIN),
        )
        with self._direct_lock:
            if self._direct_active or self._active_clip is not None:
                raise RuntimeError("TTS mixer is busy")
            self._direct_active = True
            self._direct_request_id = session.request_id
            self._set_tts_caps_values(session.sample_rate, session.channels)
            session.next_pts_ns = self._pipeline_running_time_ns() + int(0.05 * 1_000_000_000)
            self._duck_target = session.duck_gain
        log.info(
            "Starting websocket TTS request %s sample_rate=%d channels=%d duck_gain=%.2f",
            session.request_id,
            session.sample_rate,
            session.channels,
            session.duck_gain,
        )
        return session

    def _push_direct_tts_chunk(self, session: DirectTTSSession, chunk: bytes) -> None:
        if not chunk:
            return
        session.received_bytes += len(chunk)
        data = session.pending + chunk
        usable = (len(data) // session.frame_width) * session.frame_width
        session.pending = data[usable:]
        if usable <= 0:
            return

        offset = 0
        while offset < usable:
            part = data[offset:offset + session.push_chunk_bytes]
            offset += len(part)
            self._push_direct_tts_buffer(session, part)

    def _push_direct_tts_buffer(self, session: DirectTTSSession, chunk: bytes) -> None:
        frames = len(chunk) // session.frame_width
        if frames <= 0:
            return
        duration_ns = int(frames * 1_000_000_000 / session.sample_rate)
        with self._direct_lock:
            if not self._direct_active or self._direct_request_id != session.request_id:
                raise RuntimeError("TTS session is no longer active")
            if self.tts_src is None:
                raise RuntimeError("TTS appsrc is not ready")
            if session.next_pts_ns is None:
                session.next_pts_ns = self._pipeline_running_time_ns() + int(0.05 * 1_000_000_000)
            buffer = self.Gst.Buffer.new_allocate(None, len(chunk), None)
            buffer.fill(0, chunk)
            buffer.pts = session.next_pts_ns
            buffer.dts = session.next_pts_ns
            buffer.duration = duration_ns
            if session.pushed_buffers == 0:
                buffer.set_flags(self.Gst.BufferFlags.DISCONT)
            session.next_pts_ns += duration_ns
            session.pushed_bytes += len(chunk)
            session.pushed_buffers += 1
            self._duck_target = session.duck_gain
            ret = self.tts_src.emit("push-buffer", buffer)
        if ret != self.Gst.FlowReturn.OK:
            raise PipelineRestart(f"TTS appsrc push failed: {ret.value_nick}")

    def _finish_direct_tts_session(self, session: DirectTTSSession) -> None:
        if session.pending:
            padded = session.pending
            remainder = len(padded) % session.frame_width
            if remainder:
                padded += b"\x00" * (session.frame_width - remainder)
            session.pending = b""
            offset = 0
            while offset < len(padded):
                part = padded[offset:offset + session.push_chunk_bytes]
                offset += len(part)
                self._push_direct_tts_buffer(session, part)
        end_ns = session.next_pts_ns or self._pipeline_running_time_ns()
        while not self._stop and self._pipeline_running_time_ns() < end_ns:
            time.sleep(0.02)
        with self._direct_lock:
            if self._direct_request_id == session.request_id:
                self._direct_active = False
                self._direct_request_id = None
                self._duck_target = 1.0

    def _cancel_direct_tts_session(self, session: DirectTTSSession) -> None:
        with self._direct_lock:
            if self._direct_request_id == session.request_id:
                self._direct_active = False
                self._direct_request_id = None
                self._duck_target = 1.0


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


def load_wav_clip(*, request_id: str, audio_path: Path, meta_path: Path, duck_gain: float) -> TTSClip:
    with wave.open(str(audio_path), "rb") as wav:
        channels = int(wav.getnchannels())
        sample_width = int(wav.getsampwidth())
        sample_rate = int(wav.getframerate())
        data = wav.readframes(wav.getnframes())
    if sample_width != 2:
        raise ValueError(f"Unsupported WAV sample width: {sample_width}; expected 16-bit PCM")
    if channels <= 0 or sample_rate <= 0:
        raise ValueError(f"Invalid WAV format channels={channels} sample_rate={sample_rate}")
    return TTSClip(
        request_id=request_id,
        sample_rate=sample_rate,
        channels=channels,
        sample_width=sample_width,
        duck_gain=duck_gain,
        kind="queued",
        data=data,
        cleanup_paths=(audio_path, meta_path),
    )


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
