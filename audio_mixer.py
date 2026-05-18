#!/usr/bin/env python3
"""
audio_mixer.py - Shiri zone audio mixer.

OwnTone reads one raw PCM FIFO per zone. This process feeds that FIFO with a
single long-running GStreamer mixer pipeline:

  ALSA loopback capture -> volume -> \
  silence clock bed ------------------> audiomixer -> OwnTone FIFO
  RTP/L16 TTS receiver --------------> /

TTS audio reaches the mixer as RTP/L16 packets; Python never pushes audio
buffers into GStreamer. A permanent receiver branch owns packet timing, jitter
handling, depayloading, conversion, resampling, and mixing.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import logging
import os
import signal
import time
from pathlib import Path


OUTPUT_RATE = 44100
OUTPUT_CHANNELS = 2
OUTPUT_CAPS = (
    f"audio/x-raw,format=S16LE,layout=interleaved,"
    f"rate={OUTPUT_RATE},channels={OUTPUT_CHANNELS}"
)

DEFAULT_TTS_RATE = 24000
DEFAULT_TTS_CHANNELS = 1
DEFAULT_RTP_PAYLOAD_TYPE = 96
DEFAULT_RTP_JITTER_MS = 240

DEFAULT_DUCK_GAIN = 0.28
DUCK_ATTACK_SECONDS = 0.08
DUCK_RELEASE_SECONDS = 0.45
DUCK_UPDATE_SECONDS = 0.02
TTS_IDLE_RELEASE_SECONDS = 1.50
TTS_DUCK_TAIL_SECONDS = 0.50

PIPE_RETRY_SECONDS = 0.4
PIPE_LOG_INTERVAL_SECONDS = 5.0
CONTROL_POLL_SECONDS = 0.10


log = logging.getLogger("shiri.audio_mixer")


class PipelineRestart(RuntimeError):
    """Raised when the GStreamer pipeline must be rebuilt."""


class GstZoneMixer:
    def __init__(
        self,
        *,
        capture_dev: str,
        grp_dir: Path,
        tts_rtp_port: int | None = None,
        tts_rate: int = DEFAULT_TTS_RATE,
        tts_channels: int = DEFAULT_TTS_CHANNELS,
        tts_payload_type: int = DEFAULT_RTP_PAYLOAD_TYPE,
        tts_duck_gain: float = DEFAULT_DUCK_GAIN,
        rtp_jitter_ms: int = DEFAULT_RTP_JITTER_MS,
    ) -> None:
        self.capture_dev = capture_dev
        self.grp_dir = grp_dir
        self.pipe_path = grp_dir / "pipes" / "audio.pipe"
        self.control_path = grp_dir / "state" / "tts_rtp_control.json"
        self.tts_rtp_port = int(tts_rtp_port or 0)
        self.tts_rate = int(tts_rate or DEFAULT_TTS_RATE)
        self.tts_channels = int(tts_channels or DEFAULT_TTS_CHANNELS)
        self.tts_payload_type = int(tts_payload_type or DEFAULT_RTP_PAYLOAD_TYPE)
        self.rtp_jitter_ms = int(rtp_jitter_ms or DEFAULT_RTP_JITTER_MS)
        self.mixer_pid_path = grp_dir / "state" / "mixer.pid"
        self.legacy_arecord_pid_path = grp_dir / "state" / "arecord.pid"

        self.Gst = None
        self.pipeline = None
        self.bus = None
        self.mixer = None
        self.pipe_fd: int | None = None
        self.music_volume = None
        self.tts_jitter = None
        self._tts_jitter_start_stats = None

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
        self._control_mtime_ns = 0
        self._last_control_poll = 0.0

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_stop)
        signal.signal(signal.SIGINT, self._handle_stop)
        self.mixer_pid_path.write_text(str(os.getpid()))
        safe_unlink(self.legacy_arecord_pid_path)

        log.info(
            "Starting GStreamer mixer capture_dev=%s tts_rtp_port=%d rate=%d channels=%d payload=%d grp_dir=%s",
            self.capture_dev,
            self.tts_rtp_port,
            self.tts_rate,
            self.tts_channels,
            self.tts_payload_type,
            self.grp_dir,
        )
        try:
            self.Gst = require_gstreamer()
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
        self.mixer = mixer

        self._add_silence_branch(mixer)
        self._add_music_branch(mixer)
        self._add_tts_rtp_branch(mixer)
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

    def _add_tts_rtp_branch(self, mixer) -> None:
        if self.tts_rtp_port <= 0:
            log.info("TTS RTP receiver disabled")
            return

        Gst = self.Gst
        src = make_element(Gst, "udpsrc", "tts_rtp_src")
        src.set_property("address", "0.0.0.0")
        src.set_property("port", self.tts_rtp_port)
        src.set_property("caps", Gst.Caps.from_string(
            "application/x-rtp,"
            "media=(string)audio,"
            "encoding-name=(string)L16,"
            f"payload=(int){self.tts_payload_type},"
            f"clock-rate=(int){self.tts_rate},"
            f"channels=(int){self.tts_channels}"
        ))
        set_property_if_present(src, "buffer-size", 4 * 1024 * 1024)
        set_property_if_present(src, "mtu", 4096)

        jitter = make_element(Gst, "rtpjitterbuffer", "tts_jitter")
        jitter.set_property("latency", self.rtp_jitter_ms)
        set_property_if_present(jitter, "do-lost", True)
        set_property_if_present(jitter, "post-drop-messages", True)
        set_property_if_present(jitter, "drop-on-latency", False)
        self.tts_jitter = jitter

        depay = make_element(Gst, "rtpL16depay", "tts_depay")
        convert = make_element(Gst, "audioconvert", "tts_convert")
        resample = make_element(Gst, "audioresample", "tts_resample")
        caps = make_element(Gst, "capsfilter", "tts_output_caps")
        caps.set_property("caps", Gst.Caps.from_string(OUTPUT_CAPS))
        level = make_element(Gst, "level", "tts_level")
        set_property_if_present(level, "interval", 100_000_000)
        set_property_if_present(level, "post-messages", True)
        queue = make_element(Gst, "queue", "tts_queue")
        set_property_if_present(queue, "max-size-time", int(1.0 * 1_000_000_000))
        set_property_if_present(queue, "max-size-bytes", 0)
        set_property_if_present(queue, "max-size-buffers", 0)

        self._add_and_link([src, jitter, depay, convert, resample, caps, level, queue])
        self._link_to_mixer(queue, mixer)
        log.info(
            "TTS RTP receiver listening on 0.0.0.0:%d payload=%d L16/%d/%d jitter_ms=%d",
            self.tts_rtp_port,
            self.tts_payload_type,
            self.tts_rate,
            self.tts_channels,
            self.rtp_jitter_ms,
        )

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
        if sink_pad is None or src_pad is None or src_pad.link(sink_pad) != self.Gst.PadLinkReturn.OK:
            raise RuntimeError(f"Could not link {src_element.get_name()} to audiomixer")

    def _mark_tts_activity(self, now: float) -> None:
        if not self._tts_active:
            label = f" request_id={self._tts_request_id}" if self._tts_request_id else ""
            log.info("TTS RTP audio active%s", label)
            self._tts_jitter_start_stats = self._read_tts_jitter_stats()
        self._tts_active = True
        self._tts_last_activity_at = now
        self._duck_target = self._tts_duck_gain

    def _run_pipeline_loop(self) -> None:
        while not self._stop and self.pipeline is not None:
            self._handle_bus_messages()
            self._poll_tts_control()
            self._update_tts_activity()
            self._update_ducking()
            time.sleep(DUCK_UPDATE_SECONDS)

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
            if msg.type == Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                raise PipelineRestart(f"{err.message}; {debug or 'no debug'}")
            if msg.type == Gst.MessageType.WARNING:
                warn, debug = msg.parse_warning()
                log.warning("GStreamer warning from %s: %s; %s",
                            msg.src.get_name(), warn.message, debug or "no debug")
            elif msg.type == Gst.MessageType.EOS:
                raise PipelineRestart("unexpected pipeline EOS")
            elif msg.type == Gst.MessageType.ELEMENT:
                structure = msg.get_structure()
                if (
                    structure is not None
                    and structure.has_name("level")
                    and msg.src is not None
                    and msg.src.get_name() == "tts_level"
                ):
                    self._mark_tts_activity(time.monotonic())
                    continue
                if structure is not None and "drop" in structure.get_name().lower():
                    log.warning("GStreamer element message from %s: %s", msg.src.get_name(), structure.to_string())

    def _poll_tts_control(self) -> None:
        now = time.monotonic()
        if now - self._last_control_poll < CONTROL_POLL_SECONDS:
            return
        self._last_control_poll = now
        try:
            stat = self.control_path.stat()
        except FileNotFoundError:
            return
        except OSError as exc:
            log.warning("Could not stat TTS RTP control file: %s", exc)
            return
        if stat.st_mtime_ns <= self._control_mtime_ns:
            return
        self._control_mtime_ns = stat.st_mtime_ns
        try:
            with self.control_path.open("r") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Could not read TTS RTP control file: %s", exc)
            return
        self._tts_duck_gain = clamp_float(data.get("duck_gain"), 0.0, 1.0, DEFAULT_DUCK_GAIN)
        self._tts_request_id = str(data.get("request_id") or "")
        log.info("Loaded TTS RTP control request_id=%s duck_gain=%.2f",
                 self._tts_request_id or "-", self._tts_duck_gain)

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
        log.info("TTS RTP audio idle%s", label)
        self._log_tts_jitter_stats()

    def _log_tts_jitter_stats(self) -> None:
        current = self._read_tts_jitter_stats()
        if current is None:
            return
        start = self._tts_jitter_start_stats or {}
        pushed = current.get("num-pushed", 0) - start.get("num-pushed", 0)
        lost = current.get("num-lost", 0) - start.get("num-lost", 0)
        late = current.get("num-late", 0) - start.get("num-late", 0)
        duplicates = current.get("num-duplicates", 0) - start.get("num-duplicates", 0)
        log.info(
            "TTS RTP jitterbuffer stats request_id=%s pushed=%d lost=%d late=%d duplicates=%d avg_jitter_ns=%d",
            self._tts_request_id or "-",
            pushed,
            lost,
            late,
            duplicates,
            current.get("avg-jitter", 0),
        )
        self._tts_jitter_start_stats = None

    def _read_tts_jitter_stats(self) -> dict | None:
        if self.tts_jitter is None or self.tts_jitter.find_property("stats") is None:
            return None
        try:
            stats = self.tts_jitter.get_property("stats")
        except Exception:
            log.debug("Could not read TTS RTP jitter stats", exc_info=True)
            return None
        if stats is None:
            return None
        values = {}
        for key in ("num-pushed", "num-lost", "num-late", "num-duplicates", "avg-jitter"):
            try:
                values[key] = int(stats.get_value(key) or 0)
            except Exception:
                values[key] = 0
        return values

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
        if self.pipeline is not None:
            try:
                self.pipeline.set_state(self.Gst.State.NULL)
            except Exception:
                log.exception("Could not stop GStreamer pipeline cleanly")
        self.pipeline = None
        self.bus = None
        self.mixer = None
        self.music_volume = None
        self.tts_jitter = None
        self._tts_jitter_start_stats = None
        if self.pipe_fd is not None:
            try:
                os.close(self.pipe_fd)
            except OSError:
                pass
            self.pipe_fd = None


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
    parser.add_argument("--tts-rtp-port", type=int, default=0)
    parser.add_argument("--tts-rate", type=int, default=DEFAULT_TTS_RATE)
    parser.add_argument("--tts-channels", type=int, default=DEFAULT_TTS_CHANNELS)
    parser.add_argument("--tts-payload-type", type=int, default=DEFAULT_RTP_PAYLOAD_TYPE)
    parser.add_argument("--tts-duck-gain", type=float, default=DEFAULT_DUCK_GAIN)
    parser.add_argument("--rtp-jitter-ms", type=int, default=DEFAULT_RTP_JITTER_MS)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    GstZoneMixer(
        capture_dev=args.capture_dev,
        grp_dir=args.grp_dir,
        tts_rtp_port=args.tts_rtp_port,
        tts_rate=args.tts_rate,
        tts_channels=args.tts_channels,
        tts_payload_type=args.tts_payload_type,
        tts_duck_gain=args.tts_duck_gain,
        rtp_jitter_ms=args.rtp_jitter_ms,
    ).run()


if __name__ == "__main__":
    main()
