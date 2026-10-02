"""Admitted physical pins stay owned through exact output-process teardown."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from shiri.domain import Room
from shiri.rpc import RpcError
from shiri.runtime.broker import Broker, RuntimeRoom
from shiri.runtime.system import RuntimeFailure
from shiri.runtime.units import VIEW
from shiri.settings import Settings


def broker(tmp_path):
    return Broker(Settings(state_dir=tmp_path / "api", runtime_state_dir=tmp_path / "state",
                           runtime_dir=tmp_path / "run"))


def definition(**changes):
    return Room(id=str(uuid4()), slot=7, name="Kitchen", airplay_name="Kitchen zone", interface="eth0",
                enabled=True, **changes)


def pin(node="/dev/snd/pcmC5D0p"):
    return SimpleNamespace(playback_node=node, pcm="plughw:CARD=VerifiedDAC,DEV=0,SUBDEV=0",
                           manifest={"card_index": 5, "device": 0, "subdevice": 0},
                           validate=Mock(), close=Mock())


@pytest.mark.asyncio
async def test_raw_physical_name_is_refused_before_cleanup_slot_or_network(tmp_path, monkeypatch):
    owner = broker(tmp_path)
    room = RuntimeRoom(definition(local_audio_device="plughw:USB"), tmp_path / "room")
    room.processes = {"old-output": object()}
    owner._stop_room = AsyncMock()
    owner.network = SimpleNamespace(interfaces=AsyncMock(), create_receiver=AsyncMock())
    monkeypatch.setattr("shiri.runtime.broker.alsa_inventory", lambda: [])
    with pytest.raises(RuntimeFailure, match="verified device inventory binding"):
        await owner._start_room(room)
    owner._stop_room.assert_not_awaited()
    owner.network.interfaces.assert_not_awaited()
    owner.network.create_receiver.assert_not_awaited()
    assert not owner.slot_locks[7].locked() and room.local_pin is None


def test_opaque_binding_resolves_exact_registry_intent_without_name_fallback(tmp_path):
    owner = broker(tmp_path)
    identifier = "shiri:device=" + str(uuid4())
    admitted = pin()
    owner.local_devices = SimpleNamespace(resolve=Mock(return_value=admitted))
    assert owner._resolve_local_pin(identifier, 7) is admitted
    owner.local_devices.resolve.assert_called_once_with(identifier)
    owner.local_devices.resolve.side_effect = RuntimeFailure("Pinned physical device is missing")
    with pytest.raises(RuntimeFailure, match="missing"):
        owner._resolve_local_pin(identifier, 7)


@pytest.mark.asyncio
async def test_serial_and_port_bindings_of_same_pcm_node_conflict_during_reconcile(tmp_path):
    owner = broker(tmp_path)
    first, second = pin(), pin()
    owner.local_devices = SimpleNamespace(resolve=Mock(side_effect=[first, second]))
    one = definition(local_audio_device="shiri:device=" + str(uuid4()))
    two = one.model_copy(update={"id": str(uuid4()), "slot": 6, "name": "Bedroom", "airplay_name": "Bedroom zone",
                                 "local_audio_device": "shiri:device=" + str(uuid4())})
    with pytest.raises(RpcError, match="separate ALSA playback device nodes"):
        await owner.reconcile({"rooms": [one.model_dump(), two.model_dump()]})
    first.close.assert_called_once()
    second.close.assert_called_once()
    assert not owner.rooms and not owner.local_node_leases


@pytest.mark.asyncio
async def test_node_handoff_waits_for_output_unit_stop_and_pin_survives_failure(tmp_path):
    owner = broker(tmp_path)
    one = RuntimeRoom(definition(), tmp_path / "one")
    two = RuntimeRoom(one.desired.model_copy(update={"id": str(uuid4()), "slot": 6}), tmp_path / "two")
    first, replacement = pin(), pin()
    await owner._reserve_local_pin(one, first)
    output = SimpleNamespace(stop=AsyncMock(side_effect=RuntimeFailure("Invocation changed")))
    one.processes = {"owntone": output}
    owner.network = SimpleNamespace(manifest={"processes": {}}, forget_process=Mock(), remove=AsyncMock())
    with pytest.raises(RuntimeFailure, match="Invocation changed"):
        await owner._stop_room(one)
    first.close.assert_not_called()
    owner.network.remove.assert_not_awaited()
    with pytest.raises(RuntimeFailure, match="still owned"):
        await owner._reserve_local_pin(two, replacement)
    assert two.local_pin is None and owner.local_node_leases[first.playback_node] == one.desired.id
    output.stop.side_effect = None
    owner._stop_sender = AsyncMock()
    await owner._stop_room(one)
    first.close.assert_called_once()
    await owner._reserve_local_pin(two, replacement)
    assert owner.local_node_leases[first.playback_node] == two.desired.id
    await owner._release_local_pin(two)


@pytest.mark.asyncio
async def test_broker_inventory_and_binding_validate_exact_request_before_publishing(tmp_path):
    owner = broker(tmp_path)
    owner.ready = True
    owner.local_devices = SimpleNamespace(inventory=Mock(return_value={"devices": []}),
                                         bind=Mock(return_value={"device": "shiri:device=" + str(uuid4())}))
    assert await owner.dispatch("local_devices", {}) == {"devices": []}
    body = {"selection_id": "ab" * 32, "binding": "serial", "conversion": True}
    expected = owner.local_devices.bind.return_value
    assert await owner.dispatch("bind_local_device", body) == expected
    owner.local_devices.bind.assert_called_once_with(body["selection_id"], "serial", conversion=True)
    with pytest.raises(RpcError, match="Select one inventoried"):
        await owner.dispatch("bind_local_device", body | {"path": "/etc/shadow"})
    assert owner.local_devices.bind.call_count == 1


@pytest.mark.asyncio
async def test_output_launch_binds_only_admitted_pcm_and_root_readonly_typed_configuration(tmp_path, monkeypatch):
    owner = broker(tmp_path)
    room = RuntimeRoom(definition(local_audio_device="shiri:device=" + str(uuid4())), tmp_path / "room")
    admitted = pin()
    admitted.fingerprint = {"version": 1, "kind": "pci", "binding": "path",
                            "device_path": "/devices/pci0000:00/0000:00:01.0",
                            "driver": "/bus/pci/drivers/snd_test", "device": 0, "subdevice": 0}
    owner._resolve_local_pin = Mock(return_value=admitted)
    owner.network = SimpleNamespace(installation_tag="ebdb6996", manifest={"processes": {}},
        interfaces=AsyncMock(return_value=[{"name": "eth0", "eligible": True}]),
        create_receiver=AsyncMock(return_value={"namespace": "shiri_rx_abcdef12", "interface": "receiver0"}))
    owner._ensure_sender = AsyncMock(return_value={"namespace": "shiri_ot_abcdef12",
                                                   "api_ip": "10.190.1.2", "api_host_ip": "10.190.1.1"})
    owner._account = lambda _owner, role, slot=None: {"name": f"shiri-{role}-7", "uid": 200, "gid": 200}
    def prepared(path, *args, **kwargs):
        path.mkdir(parents=True, exist_ok=True)
        return path
    monkeypatch.setattr("shiri.runtime.broker.private_directory", prepared)
    monkeypatch.setattr("shiri.runtime.broker.prepare_room_view", lambda *_: None)
    monkeypatch.setattr("shiri.runtime.broker.file_owner", lambda *_: None)
    fake_server = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
    monkeypatch.setattr("shiri.runtime.broker.serve_rpc", AsyncMock(return_value=fake_server))
    owner._start_namespace_services = AsyncMock(return_value={})
    owner.binary = lambda name: "/opt/shiri/sbin/" + name
    captured = []
    async def launch(key, spec, log):
        captured.append(spec)
        if spec.role == "owntone":
            raise RuntimeFailure("Output launch captured")
        return SimpleNamespace(stop=AsyncMock())
    owner.unit_manager = SimpleNamespace(start=AsyncMock(side_effect=launch))
    try:
        with pytest.raises(RuntimeFailure, match="Output launch captured"):
            await owner._start_room(room)
        output = captured[-1]
        assert output.role == "owntone" and output.devices == (admitted.playback_node, "/dev/snd/controlC5")
        assert output.supplementary_groups == ("audio",)
        assert output.pcm_exec is not None
        assert output.pcm_exec.helper == "/opt/shiri/libexec/shiri-pcm-exec"
        assert output.command == output.pcm_exec.command()
        assert output.pcm_exec.payload[0] == "/opt/shiri/sbin/owntone"
        assert f"ALSA_CONFIG_PATH={VIEW}/config/alsa.conf" in output.environment
        binds = {(item.source, item.target, item.writable) for item in output.binds}
        for name, base in [("alsa.conf", "config"), ("pcm-identity.json", "credentials")]:
            assert (str(room.directory / base / name), str(VIEW / base / name), False) in binds
        assert (str(room.directory / "config" / "alsa.conf"), str(VIEW / "config" / "alsa.conf"), True) not in binds
        assert json.loads((room.directory / "credentials" / "pcm-identity.json").read_text()) == admitted.manifest
        assert admitted.playback_node in owner.local_node_leases
        admitted.close.assert_not_called()
    finally:
        await owner._release_local_pin(room)
        if owner.slot_locks[7].locked():
            owner.slot_locks[7].release()
        if room.signal_server:
            room.signal_server.close()
            await room.signal_server.wait_closed()
