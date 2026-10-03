"""Descriptor-only Bluetooth routes retain ownership through exact teardown."""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from shiri.domain import Room
from shiri.runtime.broker import Broker, RuntimeRoom
from shiri.runtime.system import RuntimeFailure
from shiri.settings import Settings

DEVICE = "bluealsa:DEV=AA:BB:CC:DD:EE:01,PROFILE=a2dp"


def runtime(tmp_path):
    broker = Broker(Settings(state_dir=tmp_path / "api", runtime_state_dir=tmp_path / "state",
                             runtime_dir=tmp_path / "run"))
    definition = Room(id=str(uuid4()), slot=7, name="Room", airplay_name="Room zone", interface="eth0",
                      enabled=True, local_audio_device=DEVICE)
    return broker, RuntimeRoom(definition, tmp_path / "room")


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [
    "OwnTone 29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-framed1","", "29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-framed1",
    "29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1",
    "29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-resample1-framed10",
    "29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-resample1-framed1-other",
    "29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-resample1-framed1",
    "29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-resample1-framed1-alsa10",
    "29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-resample1-framed1-alsa1",
    "29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-resample1-framed1-alsa1-other",
    "OwnTone 29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-resample1-framed1-alsa1-speech1-ready1-anchor1-jitter1-owner1-balance1-transition1-bed1-event1",
    "OwnTone 29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-resample1-framed1-alsa1-speech1-ready1-anchor1-jitter1-owner1-balance1-transition1-bed1-event1-idle10",
    "OwnTone 29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-resample1-framed1-alsa1-speech1-ready1-anchor1-jitter1-owner1-balance1-transition1-bed1-event1-idle1-other"])
async def test_old_backend_cannot_launch_or_clean_existing_bluetooth_route(tmp_path, version):
    broker, room = runtime(tmp_path)
    broker.versions["owntone"] = version
    room.processes["old"] = object()
    broker._stop_room = AsyncMock()
    broker._resolve_local_pin = Mock()
    broker.network = SimpleNamespace(interfaces=AsyncMock())
    with pytest.raises(RuntimeFailure, match="reviewed framed backend"):
        await broker._start_room(room)
    broker._stop_room.assert_not_awaited()
    broker._resolve_local_pin.assert_not_called()
    broker.network.interfaces.assert_not_awaited()
    assert not broker.slot_locks[7].locked()


@pytest.mark.asyncio
async def test_distinct_bluetooth_routes_do_not_reserve_loopback_playback_nodes(tmp_path):
    broker, room = runtime(tmp_path)
    broker._resolve_local_pin = Mock(side_effect=AssertionError("Bluetooth must not open ALSA"))
    broker._room_loop = AsyncMock()
    other = room.desired.model_copy(update={"id": str(uuid4()), "slot": 6, "name": "Other", "airplay_name": "Other zone",
                                           "local_audio_device": DEVICE.replace(":01,", ":02,")})
    await broker.reconcile({"rooms": [room.desired.model_dump(), other.model_dump()]})
    await asyncio.gather(*(room.task for room in broker.rooms.values()))
    broker._resolve_local_pin.assert_not_called()
    assert not broker.local_node_leases and len(broker.rooms) == 2


def test_bluetooth_pin_resolution_never_falls_back_to_alsa_inventory(tmp_path, monkeypatch):
    broker, _room = runtime(tmp_path)
    inventory = Mock(side_effect=AssertionError("No ALSA Bluetooth fallback"))
    monkeypatch.setattr("shiri.runtime.broker.alsa_inventory", inventory)
    assert broker._resolve_local_pin(DEVICE, 7) is None
    inventory.assert_not_called()


@pytest.mark.asyncio
async def test_admitted_fds_remain_owned_when_exact_output_stop_fails(tmp_path):
    broker, room = runtime(tmp_path)
    admission = SimpleNamespace(close=AsyncMock())
    room.bluetooth_admission = admission
    room.bluetooth_handoff = SimpleNamespace(close=Mock())
    output = SimpleNamespace(stop=AsyncMock(side_effect=RuntimeFailure("Invocation replaced")))
    bridge = SimpleNamespace(stop=AsyncMock())
    room.processes = {"bluetooth-output": bridge, "owntone": output}
    broker.network = SimpleNamespace(manifest={"processes": {}}, forget_process=Mock(), remove=AsyncMock())
    with pytest.raises(RuntimeFailure, match="Invocation replaced"):
        await broker._stop_room(room)
    admission.close.assert_not_awaited()
    bridge.stop.assert_not_awaited()
    assert room.bluetooth_admission is admission
    output.stop.side_effect = None
    await broker._stop_room(room)
    bridge.stop.assert_awaited_once()
    admission.close.assert_awaited_once_with(verified_unit_stopped=True)
    assert room.bluetooth_admission is None


@pytest.mark.asyncio
@pytest.mark.parametrize("durable_failure", [False, True])
async def test_socket_publication_follows_exact_pid_handoff_and_durable_receipt(tmp_path, monkeypatch, durable_failure):
    broker, room = runtime(tmp_path)
    generation = uuid4().hex
    room.launch_generation = generation
    bridge, output = {"name": "shiri-bridge-7", "uid": 1001, "gid": 1001}, {"uid": 2002, "gid": 2002}
    admission = SimpleNamespace(endpoint=object())
    broker.bluealsa = SimpleNamespace(admit=AsyncMock(return_value=admission))
    handoff = SimpleNamespace(authorize=Mock(), wait_delivered=AsyncMock())
    monkeypatch.setattr("shiri.runtime.bluealsa.HandoffServer", Mock(return_value=handoff))
    monkeypatch.setattr("shiri.runtime.broker.private_directory", lambda path, *a, **k: path)
    directory = SimpleNamespace(close=Mock())
    pin_directory = Mock(return_value=directory)
    monkeypatch.setattr("shiri.runtime.broker.PinnedUnixDirectory", pin_directory)
    process = SimpleNamespace(process=SimpleNamespace(pid=12345), entry={})
    broker._start_process = AsyncMock(return_value=process)
    broker._wait_worker = AsyncMock()
    descriptors = [os.open("/dev/null", os.O_RDONLY) for _ in range(2)]
    record = {"directory": str(tmp_path / "published")}
    monkeypatch.setattr("shiri.runtime.socket_publication.prepare", Mock(return_value=(record, *descriptors)))
    events = []

    async def remember(key, observed):
        assert key == f"{room.desired.id}:bluetooth-output" and observed is process
        assert observed.entry["socket_publication"] == record
        events.append("durable")
        if durable_failure:
            raise OSError("Full disk")

    broker._remember_process = AsyncMock(side_effect=remember)
    publish = Mock(side_effect=lambda *args: events.append("publish") or tmp_path / "published" / "final-pcm.sock")
    discard = Mock()
    monkeypatch.setattr("shiri.runtime.socket_publication.publish", publish)
    monkeypatch.setattr("shiri.runtime.socket_publication.discard", discard)
    if durable_failure:
        with pytest.raises(OSError, match="Full disk"):
            await broker._start_bluetooth(room, bridge, output, generation)
        assert events == ["durable"]
        publish.assert_not_called()
        discard.assert_called_once_with(record)
    else:
        bound = await broker._start_bluetooth(room, bridge, output, generation)
        assert events == ["durable", "publish"] and bound.writable is False
        assert bound.target == "/run/shiri-worker/bridge/final-pcm.sock"
        discard.assert_not_called()
    handoff.authorize.assert_called_once_with(1001, 12345)
    launch = broker._start_process.call_args
    assert launch.args[1] == "bluetooth-output" and launch.kwargs["account"] == bridge
    assert not {"devices", "host_bus", "bus", "namespace"}.intersection(launch.kwargs)
    assert room.bluetooth_admission is admission and room.processes["bluetooth-output"] is process
    assert room.bluetooth_rpc_directory is directory
    pin_directory.assert_called_once_with(room.directory / 'bridge-state' / generation,
                                          uid=bridge['uid'], gid=bridge['gid'], mode=0o700)
    for fd in descriptors:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.asyncio
@pytest.mark.parametrize("removing", [False, True])
async def test_failed_descriptor_release_keeps_room_and_retries_disabled_cleanup(tmp_path, removing):
    broker, room = runtime(tmp_path)
    room.desired = room.desired.model_copy(update={"enabled": False})
    room.removing = removing
    room.status, room.retry_at = "error", 0
    room.bluetooth_admission = SimpleNamespace(close=AsyncMock())
    room.task = SimpleNamespace(done=lambda: True)
    broker.rooms[room.desired.id] = room
    await broker._probe_room(room)
    assert broker.rooms[room.desired.id] is room and room.wake.is_set()
    room.bluetooth_admission.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_exact_buffer_and_framed_marker_reaches_pin_admission(tmp_path):
    broker, room = runtime(tmp_path)
    broker.versions["owntone"] = "OwnTone 29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-resample1-framed1-alsa1-speech1-ready1-anchor1-jitter1-owner1-balance1-transition1-bed1-event1-idle1"
    broker._resolve_local_pin = Mock(side_effect=RuntimeFailure("Pin admission reached"))
    with pytest.raises(RuntimeFailure, match="Pin admission reached"):
        await broker._start_room(room)
    broker._resolve_local_pin.assert_called_once_with(DEVICE, 7)
