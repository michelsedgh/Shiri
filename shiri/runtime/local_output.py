"""Host-local ALSA bridge for a room's paired Bluetooth output.

OwnTone writes to one snd-aloop playback pair inside its isolated environment.
This worker captures the opposite side on the host, where BlueALSA can reach
the host system D-Bus. It owns no network speaker or synchronization clock.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import logging
import os
from pathlib import Path
import signal
import time

from shiri.rpc import RpcError, serve_rpc

log = logging.getLogger(__name__)
HOST_BUS = "unix:path=/run/dbus/system_bus_socket"
SOURCE_CAPS = "audio/x-raw,format=S16LE,rate=48000,channels=2,layout=interleaved"


def device_argument(value):
    """Preserve the exact ALSA URI while rejecting unusable CLI strings."""
    if not value or len(value.encode("utf-8")) > 128 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise argparse.ArgumentTypeError("ALSA device must be 1 to 128 UTF-8 bytes without controls")
    return value


class GstLocalOutput:
    def __init__(self, capture: str, device: str, *, test_sink=False):
        import gi
        gi.require_version("Gst", "1.0")
        gi.require_version("GstAudio", "1.0")
        from gi.repository import Gst, GstAudio
        Gst.init(None)
        self.Gst, self.GstAudio = Gst, GstAudio
        self.capture, self.device = capture, device
        self.error = None
        self.warning = None
        self.closed = False
        self.frames_forwarded = 0
        self.last_output_at = None
        self._needs_latency = False
        self.pipeline = Gst.Pipeline.new("room-local-output")
        self.bus = self.pipeline.get_bus()
        # Read relevant bus messages in the emitting thread, then drop them.
        # No unbounded queue of state/QoS/warning messages survives between polls.
        self.bus.set_sync_handler(self._bus_message, None)

        def element(factory, name, **properties):
            value = Gst.ElementFactory.make(factory, name)
            if value is None:
                raise RuntimeError(f"Missing GStreamer element: {factory}")
            for key, setting in properties.items():
                value.set_property(key.replace("_", "-"), setting)
            self.pipeline.add(value)
            return value

        try:
            self.source = element("alsasrc", "capture", device=capture, provide_clock=False,
                                  buffer_time=120000, latency_time=20000)
            self.format = element("capsfilter", "capture-format", caps=Gst.Caps.from_string(SOURCE_CAPS))
            self.queue = element("queue", "bounded-output-queue", max_size_time=200_000_000,
                                 max_size_bytes=38400, max_size_buffers=10, leaky=2, flush_on_eos=True)
            convert = element("audioconvert", "output-convert")
            resample = element("audioresample", "output-resample")
            self.sink = (element("fakesink", "local-output", sync=True, **{"async": False}) if test_sink else
                         element("alsasink", "local-output", device=device, sync=True,
                                 buffer_time=120000, latency_time=20000, **{"async": False}))
            elements = [self.source, self.format, self.queue, convert, resample, self.sink]
            for before, after in zip(elements, elements[1:], strict=False):
                if not before.link(after):
                    raise RuntimeError(f"Could not connect {before.name} to {after.name}")
            self.sink.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self._output)
            if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError(self.error or "The local audio output pipeline could not start")
        except BaseException:
            self.close()
            raise

    def _bus_message(self, _bus, message, _data):
        if message.type == self.Gst.MessageType.ERROR:
            error, _debug = message.parse_error()
            self.error = str(error)[:2000]
        elif message.type == self.Gst.MessageType.EOS:
            self.error = "The local audio output pipeline ended unexpectedly"
        elif message.type == self.Gst.MessageType.CLOCK_LOST:
            self.error = "The local audio device clock was lost"
        elif message.type == self.Gst.MessageType.WARNING:
            warning, _debug = message.parse_warning()
            self.warning = str(warning)[:1000]
        elif message.type == self.Gst.MessageType.LATENCY:
            self._needs_latency = True
        return self.Gst.BusSyncReply.DROP

    def _output(self, pad, probe):
        buffer = probe.get_buffer()
        caps = pad.get_current_caps()
        if buffer is not None and caps is not None:
            audio = self.GstAudio.AudioInfo.new_from_caps(caps)
            if audio is not None and audio.bpf:
                self.frames_forwarded += buffer.get_size() // audio.bpf
                self.last_output_at = time.monotonic()
        return self.Gst.PadProbeReturn.OK

    def poll(self):
        if self._needs_latency and not self.closed:
            self._needs_latency = False
            self.pipeline.recalculate_latency()
        if self.error:
            raise RuntimeError(self.error)

    def health(self):
        _change, state, _pending = self.pipeline.get_state(0)
        ready = not self.closed and self.error is None and state == self.Gst.State.PLAYING
        caps = self.sink.get_static_pad("sink").get_current_caps()
        structure = caps.get_structure(0) if caps is not None and not caps.is_empty() else None
        return {
            "ready": ready,
            "status": "stopped" if self.closed else "error" if self.error else "running" if ready else "starting",
            "error": self.error,
            "warning": self.warning,
            "capture": self.capture,
            "device": self.device,
            "system_bus": os.environ.get("DBUS_SYSTEM_BUS_ADDRESS"),
            "frames_forwarded": self.frames_forwarded,
            "last_output_at": self.last_output_at,
            "queue_bytes": self.queue.get_property("current-level-bytes"),
            "queue_time_ms": self.queue.get_property("current-level-time") / 1_000_000,
            "format": structure.get_value("format") if structure else None,
            "rate": structure.get_value("rate") if structure else None,
            "channels": structure.get_value("channels") if structure else None,
        }

    async def dispatch(self, operation, payload):
        if operation != "health" or payload:
            raise RpcError("invalid_request", "The local audio bridge accepts only an empty health request")
        return self.health()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.pipeline.set_state(self.Gst.State.NULL)
        self.bus.set_sync_handler(None, None)


async def run(args):
    # OwnTone deliberately has a private bus. The configured host BlueALSA
    # plugin must use this bus rather than inheriting OwnTone's environment.
    os.environ["DBUS_SYSTEM_BUS_ADDRESS"] = HOST_BUS
    # A second standalone worker must not replace a live bridge's socket or
    # signal handlers. Keep the inode stable so all contenders lock one file.
    lock_fd = os.open(str(args.socket) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException as exc:
        os.close(lock_fd)
        if isinstance(exc, BlockingIOError):
            raise RuntimeError("A local audio bridge already owns this room socket") from None
        raise
    bridge = None
    server = None
    try:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for name in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(name, stop.set)
        bridge = GstLocalOutput(args.capture, args.device, test_sink=args.test_sink)
        server = await serve_rpc(args.socket, bridge.dispatch, mode=0o600, allowed_uids={os.getuid()})
        while not stop.is_set():
            bridge.poll()
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.05)
            except asyncio.TimeoutError:
                pass
    finally:
        try:
            if server:
                server.close()
                await server.wait_closed()
        finally:
            try:
                if bridge:
                    bridge.close()
                # Failed startup never owns the existing socket/file path.
                if server:
                    with contextlib.suppress(FileNotFoundError):
                        Path(args.socket).unlink()
            finally:
                os.close(lock_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", required=True, type=device_argument)
    parser.add_argument("--device", required=True, type=device_argument)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--test-sink", action="store_true", help="Discard captured PCM for isolated integration tests")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(run(args))
    except (RuntimeError, OSError) as error:
        log.error("Local audio output stopped: %s", error)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
