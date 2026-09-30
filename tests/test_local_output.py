import argparse
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace

import pytest

from shiri.rpc import RpcError
from shiri.runtime.local_output import GstLocalOutput, device_argument


def test_device_argument_preserves_the_exact_configured_alsa_uri():
    device = "bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp"
    assert device_argument(device) == device
    assert device_argument("hw:CARD=USB,DEV=0") == "hw:CARD=USB,DEV=0"


@pytest.mark.parametrize("device", ["", "a" * 129, "é" * 65, "hw:card\n", "hw:\x00", "hw:\x7f"])
def test_unusable_or_unbounded_device_arguments_are_rejected(device):
    with pytest.raises(argparse.ArgumentTypeError):
        device_argument(device)


@pytest.mark.parametrize("operation,payload", [("stop", {}), ("music", {}), ("health", {"device": "different"})])
async def test_bridge_rpc_cannot_change_devices_or_control_other_audio(operation, payload):
    bridge = GstLocalOutput.__new__(GstLocalOutput)
    with pytest.raises(RpcError) as failure:
        await bridge.dispatch(operation, payload)
    assert failure.value.code == "invalid_request"


def test_bus_retains_only_bounded_latest_error_and_warning():
    bridge = GstLocalOutput.__new__(GstLocalOutput)
    bridge.Gst = SimpleNamespace(
        MessageType=SimpleNamespace(ERROR=1, WARNING=2, EOS=3, LATENCY=4, CLOCK_LOST=5),
        BusSyncReply=SimpleNamespace(DROP="drop"),
    )
    bridge.error = bridge.warning = None
    bridge._needs_latency = False
    warning = SimpleNamespace(type=2, parse_warning=lambda: ("w" * 5000, "debug"))
    error = SimpleNamespace(type=1, parse_error=lambda: ("e" * 5000, "debug"))
    assert bridge._bus_message(None, warning, None) == "drop"
    assert bridge._bus_message(None, error, None) == "drop"
    assert len(bridge.warning) == 1000 and len(bridge.error) == 2000
    assert bridge._bus_message(None, SimpleNamespace(type=4), None) == "drop"
    assert bridge._needs_latency
    assert bridge._bus_message(None, SimpleNamespace(type=99), None) == "drop"
    assert bridge._bus_message(None, SimpleNamespace(type=5), None) == "drop"
    assert "clock was lost" in bridge.error


def test_bridge_close_releases_pipeline_and_bus_once():
    calls = []
    bridge = GstLocalOutput.__new__(GstLocalOutput)
    bridge.closed = False
    bridge.Gst = SimpleNamespace(State=SimpleNamespace(NULL="null"))
    bridge.pipeline = SimpleNamespace(set_state=lambda state: calls.append(("state", state)))
    bridge.bus = SimpleNamespace(set_sync_handler=lambda callback, data: calls.append(("bus", callback, data)))
    bridge.close()
    bridge.close()
    assert bridge.closed and calls == [("state", "null"), ("bus", None, None)]


def test_output_statistics_use_the_populated_audio_info_returned_by_gi():
    bridge = GstLocalOutput.__new__(GstLocalOutput)
    bridge.Gst = SimpleNamespace(PadProbeReturn=SimpleNamespace(OK="ok"))
    bridge.GstAudio = SimpleNamespace(AudioInfo=SimpleNamespace(new_from_caps=lambda _caps: SimpleNamespace(bpf=4)))
    bridge.frames_forwarded, bridge.last_output_at = 0, None
    pad = SimpleNamespace(get_current_caps=lambda: "S16LE stereo")
    probe = SimpleNamespace(get_buffer=lambda: SimpleNamespace(get_size=lambda: 3840))
    assert bridge._output(pad, probe) == "ok"
    assert bridge.frames_forwarded == 960 and bridge.last_output_at is not None


def fake_worker(monkeypatch):
    from shiri.runtime import local_output
    from shiri.rpc import serve_rpc

    instances, stops = [], []

    class Bridge:
        def __init__(self, *args, **kwargs):
            self.closed = False
            instances.append(self)

        def poll(self):
            pass

        async def dispatch(self, operation, payload):
            return {"ready": not self.closed}

        def close(self):
            self.closed = True

    async def portable_server(path, handler, **kwargs):
        # macOS has no Linux SO_PEERCRED; ownership is covered by Linux tests.
        return await serve_rpc(path, handler, mode=kwargs["mode"])

    monkeypatch.setattr(local_output, "GstLocalOutput", Bridge)
    monkeypatch.setattr(local_output, "serve_rpc", portable_server)
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda _signal, callback: stops.append(callback))
    monkeypatch.setenv("DBUS_SYSTEM_BUS_ADDRESS", "test-original-bus")
    return local_output, instances, stops


@pytest.fixture
def short_socket_directory():
    # Darwin's AF_UNIX path limit is shorter than pytest's default temp path.
    with tempfile.TemporaryDirectory(prefix="shiri-bridge-", dir="/tmp") as directory:
        yield Path(directory)


async def test_second_bridge_cannot_replace_the_live_socket_or_open_audio(short_socket_directory, monkeypatch):
    from shiri.rpc import call_rpc

    module, instances, stops = fake_worker(monkeypatch)
    args = SimpleNamespace(capture="hw:Loopback,0,6", device="hw:USB", test_sink=True,
                           socket=short_socket_directory / "bridge.sock")
    first = asyncio.create_task(module.run(args))
    try:
        for _ in range(100):
            if args.socket.exists():
                break
            await asyncio.sleep(0.005)
        assert await call_rpc(args.socket, "health") == {"ready": True}
        with pytest.raises(RuntimeError, match="already owns"):
            await module.run(args)
        assert len(instances) == 1 and len(stops) == 2
        assert await call_rpc(args.socket, "health") == {"ready": True}
    finally:
        if stops:
            stops[0]()
        await asyncio.wait_for(first, 1)
    assert instances[0].closed and not args.socket.exists()
    assert (short_socket_directory / "bridge.sock.lock").exists()


async def test_failed_bridge_startup_preserves_non_socket_file_and_releases_lock(tmp_path, monkeypatch):
    module, instances, _stops = fake_worker(monkeypatch)
    args = SimpleNamespace(capture="hw:Loopback,0,6", device="hw:USB", test_sink=True,
                           socket=tmp_path / "bridge.sock")
    args.socket.write_text("must survive failed startup")
    for _ in range(2):
        with pytest.raises(RuntimeError, match="non-socket"):
            await module.run(args)
        assert args.socket.read_text() == "must survive failed startup"
        assert instances[-1].closed
