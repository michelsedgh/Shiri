#!/usr/bin/env python3
"""
audio_mixer.py - Shiri zone audio mixer.

OwnTone reads one raw PCM FIFO per zone. This process feeds that FIFO with a
single long-running GStreamer mixer pipeline:

  ALSA loopback capture  -> volume -> \
  live silence bed ------------------> audiomixer -> OwnTone FIFO
  live TTS appsrc branch ------------> /

Each TTS websocket creates a short-lived branch. Python pushes raw PCM bytes
into appsrc; rawaudioparse turns those bytes into timestamped audio frames.
GStreamer owns backpressure, conversion, resampling, mixing, and output pacing.
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
TTS_DUCK_TAIL_SECONDS = 0.20

PIPE_RETRY_SECONDS = 0.4
PIPE_LOG_INTERVAL_SECONDS = 5.0
PIPELINE_READY_TIMEOUT_SECONDS = 5.0
# This is queued-ahead audio, not startup latency. Playback starts as soon as
# the first buffers arrive; the queue lets faster-than-realtime TTS producers
# finish sending without being forced to trickle at speaker playback speed.
TTS_QUEUE_SECONDS = 30.0
TTS_START_LEAD_SECONDS = 0.03
TTS_DRAIN_MIN_SECONDS = 5.0
TTS_DRAIN_MARGIN_SECONDS = 1.5
TTS_DRAIN_MAX_SECONDS = 120.0


log = logging.getLogger("shiri.audio_mixer")


class PipelineRestart(RuntimeError):
    """Raised when the GStreamer pipeline must be rebuilt."""


class PipelineNotReady(RuntimeError):
    """Raised when a TTS stream arrives before the mixer pipeline is ready."""


@dataclass(frozen=True)
class TTSStreamSpec:
    request_id: str
    sample_rate: int
    channels: int
    sample_width: int
    duck_gain: float

    @property
    def frame_width(self) -> int:
        return max(1, self.channels * self.sample_width)

    @property
    def bytes_per_second(self) -> int:
        return max(1, self.sample_rate * self.frame_width)


@dataclass
class LiveTTSBranch:
    spec: TTSStreamSpec
    appsrc: object
    elements: list[object]
    mixer_pad: object
    src_pad: object
    eos_probe_id: int | None
    finished: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    pushed_bytes: int = 0
    pushed_buffers: int = 0
    started_at: float = field(default_factory=time.monotonic)
    first_audio_at: float | None = None
    last_audio_at: float | None = None
    offset_set: bool = False
    closed: bool = False

    @property
    def request_id(self) -> str:
        return self.spec.request_id

    @property
    def frame_width(self) -> int:
        return self.spec.frame_width

    @property
    def duck_gain(self) -> float:
        return self.spec.duck_gain


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
        self.mixer = None
        self.pipe_fd: int | None = None
        self.music_volume = None

        self._stop = False
        self._duck_level = 1.0
        self._duck_target = 1.0
        self._duck_hold_gain = 1.0
        self._duck_hold_until = 0.0
        self._last_duck_update = time.monotonic()
        self._last_pipe_wait_log = 0.0

        self._gst_lock = threading.RLock()
        self._tts_stream_lock = threading.Lock()
        self._active_branch: LiveTTSBranch | None = None
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
        with self._gst_lock:
            if self.pipeline is not None:
                return
            Gst = self.Gst
            self.pipeline = Gst.Pipeline.new("shiri-zone-mixer")

            mixer = make_element(Gst, "audiomixer", "mix")
            set_property_if_present(mixer, "ignore-inactive-pads", True)
            set_property_if_present(mixer, "output-buffer-duration", 10_000_000)
            self.pipeline.add(mixer)
            self.mixer = mixer

            self._add_silence_branch(mixer)
            self._add_music_branch(mixer)
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
        self._link_chain(elements)

    def _link_chain(self, elements: list[object]) -> None:
        for left, right in zip(elements, elements[1:]):
            if not left.link(right):
                raise RuntimeError(f"Could not link {left.get_name()} -> {right.get_name()}")

    def _link_to_mixer(self, src_element, mixer) -> object:
        src_pad = src_element.get_static_pad("src")
        sink_pad = self._request_mixer_pad(mixer)
        if src_pad is None or src_pad.link(sink_pad) != self.Gst.PadLinkReturn.OK:
            raise RuntimeError(f"Could not link {src_element.get_name()} to audiomixer")
        return sink_pad

    def _request_mixer_pad(self, mixer=None):
        mixer = mixer or self.mixer
        if mixer is None:
            raise PipelineNotReady("GStreamer mixer is not ready")
        if hasattr(mixer, "request_pad_simple"):
            sink_pad = mixer.request_pad_simple("sink_%u")
        else:
            sink_pad = None
        if sink_pad is None:
            sink_pad = mixer.get_request_pad("sink_%u")
        if sink_pad is None:
            raise RuntimeError("Could not request audiomixer sink pad")
        return sink_pad

    def _run_pipeline_loop(self) -> None:
        while not self._stop and self.pipeline is not None:
            self._handle_bus_messages()
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

    def _create_live_tts_branch(self, spec: TTSStreamSpec) -> LiveTTSBranch:
        with self._gst_lock:
            if self.pipeline is None or self.mixer is None:
                raise PipelineNotReady("GStreamer mixer pipeline is not ready")
            if self._active_branch is not None:
                raise RuntimeError("Another TTS stream is already active")

            Gst = self.Gst
            appsrc = make_element(Gst, "appsrc", f"tts_src_{spec.request_id}")
            appsrc.set_property("is-live", False)
            appsrc.set_property("format", Gst.Format.BYTES)
            appsrc.set_property("block", True)
            appsrc.set_property("do-timestamp", False)
            appsrc.set_property("emit-signals", False)
            appsrc.set_property("caps", Gst.Caps.from_string(
                "audio/x-unaligned-raw,format=S16LE,layout=interleaved,"
                f"rate={spec.sample_rate},channels={spec.channels}"
            ))
            queue_time_ns = int(TTS_QUEUE_SECONDS * 1_000_000_000)
            set_property_if_present(appsrc, "max-bytes", max(4096, int(spec.bytes_per_second * TTS_QUEUE_SECONDS)))
            set_property_if_present(appsrc, "min-percent", 20)

            parse = make_element(Gst, "rawaudioparse", f"tts_parse_{spec.request_id}")
            parse.set_property("use-sink-caps", True)
            set_property_if_present(parse, "disable-passthrough", True)

            queue_element = make_element(Gst, "queue", f"tts_queue_{spec.request_id}")
            set_property_if_present(queue_element, "max-size-time", queue_time_ns)
            set_property_if_present(queue_element, "max-size-bytes", 0)
            set_property_if_present(queue_element, "max-size-buffers", 0)
            convert = make_element(Gst, "audioconvert", f"tts_convert_{spec.request_id}")
            resample = make_element(Gst, "audioresample", f"tts_resample_{spec.request_id}")
            caps = make_element(Gst, "capsfilter", f"tts_caps_{spec.request_id}")
            caps.set_property("caps", Gst.Caps.from_string(OUTPUT_CAPS))
            volume = make_element(Gst, "volume", f"tts_volume_{spec.request_id}")
            volume.set_property("volume", 1.0)

            elements = [appsrc, parse, queue_element, convert, resample, caps, volume]
            mixer_pad = None
            try:
                for element in elements:
                    self.pipeline.add(element)
                self._link_chain(elements)

                src_pad = volume.get_static_pad("src")
                mixer_pad = self._request_mixer_pad()
                if src_pad is None or src_pad.link(mixer_pad) != Gst.PadLinkReturn.OK:
                    raise RuntimeError("Could not link live TTS branch to audiomixer")

                branch = LiveTTSBranch(
                    spec=spec,
                    appsrc=appsrc,
                    elements=elements,
                    mixer_pad=mixer_pad,
                    src_pad=src_pad,
                    eos_probe_id=None,
                )
                branch.eos_probe_id = src_pad.add_probe(
                    Gst.PadProbeType.EVENT_DOWNSTREAM,
                    self._tts_branch_event_probe,
                    branch,
                )

                for element in elements:
                    if not element.sync_state_with_parent():
                        raise RuntimeError(f"Could not sync {element.get_name()} with mixer pipeline")

                self._active_branch = branch
                self._duck_target = spec.duck_gain
                log.info(
                    "Started live TTS stream %s sample_rate=%d channels=%d duck_gain=%.2f",
                    spec.request_id,
                    spec.sample_rate,
                    spec.channels,
                    spec.duck_gain,
                )
                return branch
            except Exception:
                if mixer_pad is not None:
                    with contextlib.suppress(Exception):
                        self.mixer.release_request_pad(mixer_pad)
                for element in reversed(elements):
                    with contextlib.suppress(Exception):
                        element.set_state(Gst.State.NULL)
                    with contextlib.suppress(Exception):
                        self.pipeline.remove(element)
                raise

    def _tts_branch_event_probe(self, _pad, info, branch: LiveTTSBranch):
        event = info.get_event()
        if event is not None and event.type == self.Gst.EventType.EOS:
            branch.finished.set()
            return self.Gst.PadProbeReturn.DROP
        return self.Gst.PadProbeReturn.OK

    def _push_live_tts_buffer(self, branch: LiveTTSBranch, chunk: bytes) -> None:
        if not chunk:
            return
        with branch.lock:
            if branch.closed:
                raise RuntimeError(f"TTS stream {branch.request_id} is already closed")
            if not branch.offset_set:
                start_offset_ns = self._pipeline_running_time_ns() + int(TTS_START_LEAD_SECONDS * 1_000_000_000)
                branch.src_pad.set_offset(start_offset_ns)
                branch.first_audio_at = time.monotonic()
                branch.offset_set = True
                log.info(
                    "Anchored live TTS stream %s at running_time_ms=%d",
                    branch.request_id,
                    int(start_offset_ns / 1_000_000),
                )

        buffer = self.Gst.Buffer.new_allocate(None, len(chunk), None)
        buffer.fill(0, chunk)

        ret = branch.appsrc.emit("push-buffer", buffer)
        if ret != self.Gst.FlowReturn.OK:
            raise RuntimeError(f"TTS appsrc push failed: {ret.value_nick}")

        with branch.lock:
            if not branch.closed:
                branch.pushed_buffers += 1
                branch.pushed_bytes += len(chunk)
                branch.last_audio_at = time.monotonic()

    def _end_live_tts_stream(self, branch: LiveTTSBranch) -> None:
        with branch.lock:
            if branch.closed:
                return
        ret = branch.appsrc.emit("end-of-stream")
        if ret not in {self.Gst.FlowReturn.OK, self.Gst.FlowReturn.FLUSHING}:
            raise RuntimeError(f"TTS appsrc EOS failed: {ret.value_nick}")

    async def _wait_for_branch_drain(self, branch: LiveTTSBranch) -> bool:
        timeout = self._branch_drain_timeout_seconds(branch)
        deadline = time.monotonic() + timeout
        expected_end = self._branch_expected_end_time(branch)
        log.info(
            "Waiting up to %.1fs for live TTS stream %s to drain audio_duration_ms=%d remaining_ms=%d",
            timeout,
            branch.request_id,
            int(self._branch_audio_duration_seconds(branch) * 1000),
            int(max(0.0, expected_end - time.monotonic()) * 1000),
        )
        while time.monotonic() < deadline:
            now = time.monotonic()
            if branch.finished.is_set() and now >= expected_end:
                return True
            await asyncio.sleep(0.02)
        return branch.finished.is_set()

    def _branch_audio_duration_seconds(self, branch: LiveTTSBranch) -> float:
        return branch.pushed_bytes / max(1, branch.spec.bytes_per_second)

    def _branch_expected_end_time(self, branch: LiveTTSBranch) -> float:
        start = branch.first_audio_at if branch.first_audio_at is not None else time.monotonic()
        return start + TTS_START_LEAD_SECONDS + self._branch_audio_duration_seconds(branch)

    def _branch_drain_timeout_seconds(self, branch: LiveTTSBranch) -> float:
        remaining = max(0.0, self._branch_expected_end_time(branch) - time.monotonic())
        timeout = remaining + TTS_DRAIN_MARGIN_SECONDS
        return min(TTS_DRAIN_MAX_SECONDS, max(TTS_DRAIN_MIN_SECONDS, timeout))

    async def _create_live_tts_branch_when_ready(self, spec: TTSStreamSpec) -> LiveTTSBranch:
        deadline = time.monotonic() + PIPELINE_READY_TIMEOUT_SECONDS
        last_error: Exception | None = None
        while not self._stop and time.monotonic() < deadline:
            try:
                return self._create_live_tts_branch(spec)
            except PipelineNotReady as exc:
                last_error = exc
                await asyncio.sleep(0.05)
        raise RuntimeError(str(last_error or "GStreamer mixer pipeline is not ready"))

    def _pipeline_running_time_ns(self) -> int:
        if self.pipeline is None:
            return 0
        clock = self.pipeline.get_clock()
        if clock is None:
            return 0
        return max(0, int(clock.get_time() - self.pipeline.get_base_time()))

    def _destroy_live_tts_branch(self, branch: LiveTTSBranch, *, reason: str, hold_duck: bool = True) -> None:
        with branch.lock:
            if branch.closed:
                return
            branch.closed = True

        with self._gst_lock:
            Gst = self.Gst
            if branch.eos_probe_id is not None:
                with contextlib.suppress(Exception):
                    branch.src_pad.remove_probe(branch.eos_probe_id)
                branch.eos_probe_id = None
            with contextlib.suppress(Exception):
                branch.src_pad.unlink(branch.mixer_pad)
            for element in reversed(branch.elements):
                with contextlib.suppress(Exception):
                    element.set_state(Gst.State.NULL)
            if self.pipeline is not None:
                for element in branch.elements:
                    with contextlib.suppress(Exception):
                        self.pipeline.remove(element)
            if self.mixer is not None:
                with contextlib.suppress(Exception):
                    self.mixer.release_request_pad(branch.mixer_pad)

            if self._active_branch is branch:
                self._active_branch = None
                self._duck_target = 1.0
                if hold_duck and branch.pushed_bytes > 0:
                    self._duck_hold_until = max(self._duck_hold_until, time.monotonic() + TTS_DUCK_TAIL_SECONDS)
                    self._duck_hold_gain = min(self._duck_hold_gain, branch.duck_gain)

        log.info(
            "Closed live TTS stream %s reason=%s pushed=%d buffers=%d",
            branch.request_id,
            reason,
            branch.pushed_bytes,
            branch.pushed_buffers,
        )

    def _update_ducking(self) -> None:
        if self.music_volume is None:
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
        self.music_volume.set_property("volume", self._duck_level)

    def _stop_pipeline(self) -> None:
        with self._gst_lock:
            branch = self._active_branch
            if branch is not None:
                with branch.lock:
                    branch.closed = True
                self._active_branch = None
            if self.pipeline is not None:
                try:
                    self.pipeline.set_state(self.Gst.State.NULL)
                except Exception:
                    log.exception("Could not stop GStreamer pipeline cleanly")
            self.pipeline = None
            self.bus = None
            self.mixer = None
            self.music_volume = None
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
        try:
            while True:
                first = await websocket.recv()
                if not isinstance(first, str):
                    await websocket.send(json.dumps({"type": "error", "error": "message must be JSON start"}))
                    continue
                start = json.loads(first)
                if start.get("type") != "start":
                    await websocket.send(json.dumps({"type": "error", "error": "message must have type=start"}))
                    continue

                keep_open = await self._receive_tts_stream(websocket, start)
                if not keep_open:
                    return
        except Exception as exc:
            with contextlib.suppress(Exception):
                await websocket.send(json.dumps({"type": "error", "error": str(exc)}))

    async def _receive_tts_stream(self, websocket, start: dict) -> bool:
        branch: LiveTTSBranch | None = None
        stream_lock_acquired = False
        spec = self._stream_spec_from_websocket_start(start)
        try:
            stream_lock_acquired = self._tts_stream_lock.acquire(blocking=False)
            if not stream_lock_acquired:
                await websocket.send(json.dumps({"type": "error", "error": "another TTS stream is active"}))
                return True

            branch = await self._create_live_tts_branch_when_ready(spec)
            await websocket.send(json.dumps({
                "type": "started",
                "request_id": spec.request_id,
                "sample_rate": spec.sample_rate,
                "channels": spec.channels,
                "sample_width": spec.sample_width,
            }))

            async for message in websocket:
                if isinstance(message, bytes):
                    await asyncio.to_thread(self._push_live_tts_buffer, branch, message)
                    continue

                payload = json.loads(message)
                msg_type = payload.get("type")
                if msg_type == "end":
                    log.info(
                        "Received websocket end for live TTS stream %s received=%d audio_duration_ms=%d",
                        spec.request_id,
                        branch.pushed_bytes,
                        int(self._branch_audio_duration_seconds(branch) * 1000),
                    )
                    await asyncio.to_thread(self._end_live_tts_stream, branch)
                    await websocket.send(json.dumps({
                        "type": "ended",
                        "request_id": spec.request_id,
                        "received_bytes": branch.pushed_bytes,
                    }))
                    if not await self._wait_for_branch_drain(branch):
                        log.warning("Timed out waiting for live TTS stream %s to drain", spec.request_id)
                    self._destroy_live_tts_branch(branch, reason="end")
                    branch = None
                    return True
                if msg_type == "cancel":
                    self._destroy_live_tts_branch(branch, reason=str(payload.get("error") or "cancelled"), hold_duck=False)
                    branch = None
                    await websocket.send(json.dumps({"type": "cancelled", "request_id": spec.request_id}))
                    return True
                await websocket.send(json.dumps({"type": "error", "error": f"unknown message type: {msg_type}"}))

            if branch is not None:
                if branch.pushed_bytes > 0:
                    await asyncio.to_thread(self._end_live_tts_stream, branch)
                    await self._wait_for_branch_drain(branch)
                    self._destroy_live_tts_branch(branch, reason="websocket_closed")
                else:
                    self._destroy_live_tts_branch(branch, reason="websocket_closed_empty", hold_duck=False)
                branch = None
            return False
        except Exception as exc:
            if branch is not None:
                self._destroy_live_tts_branch(branch, reason=f"error: {exc}", hold_duck=False)
                branch = None
            with contextlib.suppress(Exception):
                await websocket.send(json.dumps({"type": "error", "error": str(exc)}))
            return False
        finally:
            if branch is not None:
                self._destroy_live_tts_branch(branch, reason="handler_exit", hold_duck=False)
            if stream_lock_acquired:
                self._tts_stream_lock.release()

    def _stream_spec_from_websocket_start(self, payload: dict) -> TTSStreamSpec:
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
        return TTSStreamSpec(
            request_id=safe_request_id(payload.get("request_id")),
            sample_rate=sample_rate,
            channels=channels,
            sample_width=sample_width,
            duck_gain=clamp_float(payload.get("duck_gain"), 0.0, 1.0, DEFAULT_DUCK_GAIN),
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
