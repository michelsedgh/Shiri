"""Clock changes require a fresh native configuration and verified output mode."""
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from shiri.domain import Room, SpeakerRef
from shiri.rpc import RpcError
from shiri.runtime.backend import OwnToneClient, normalize_output
from shiri.runtime.broker import Broker, RuntimeRoom
from shiri.runtime.configuration import backend_configs
from shiri.runtime.system import RuntimeFailure
from shiri.settings import Settings


def room(**changes):
    return Room(id=str(uuid4()), slot=0, name="Living room", airplay_name="House input",
                interface="eth0", enabled=True, **changes)


def broker(tmp_path):
    return Broker(Settings(state_dir=tmp_path / "state", runtime_state_dir=tmp_path / "runtime",
                           runtime_dir=tmp_path / "run", runtime_socket=tmp_path / "run/runtime.sock"))


def test_clock_config_uses_exact_identity_instead_of_duplicate_or_renamed_labels(tmp_path):
    speakers = [SpeakerRef(id="101", name='Same "name"', protocol="airplay2", airplay_timing="ntp"),
                SpeakerRef(id="202", name='Same "name"', protocol="airplay2", airplay_timing="ptp"),
                SpeakerRef(id="303", name="Automatic", protocol="airplay2")]
    definition = room(speakers=speakers)
    def render(value, directory):
        return backend_configs(value, directory, {"interface": "receiver0"},
            {"api_host_ip": "10.211.0.1", "api_ip": "10.211.0.2"},
            broker_socket=tmp_path / "broker.sock", all_receiver_names=["House input"], password="private",
            view_directory=Path('/run/shiri-worker'))[1].read_text()
    first = render(definition, tmp_path / "before")
    renamed = definition.model_copy(update={"speakers": [s.model_copy(update={"name": "Renamed"}) for s in speakers]})
    second = render(renamed, tmp_path / "after")
    for text in (first, second):
        assert 'shiri_airplay_timing "101" { protocol = "ntp" }' in text
        assert 'shiri_airplay_timing "202" { protocol = "ptp" }' in text
        assert 'shiri_airplay_timing "303"' not in text
        assert 'ptp_disable' not in text
    assert first == second


def test_clock_change_is_material_but_balance_rename_and_order_are_not(tmp_path):
    service = broker(tmp_path)
    a = SpeakerRef(id="101", name="A", protocol="airplay2", airplay_timing="ntp")
    b = SpeakerRef(id="202", name="B", protocol="airplay2")
    original = room(speakers=[a, b])
    same = original.model_copy(update={"speakers": [b, a.model_copy(update={"name": "Renamed", "balance_percent": 50})]})
    assert service._material(original) == service._material(same)
    automatic = original.model_copy(update={"speakers": [a.model_copy(update={"airplay_timing": "auto"}), b]})
    assert service._material(original) != service._material(automatic)


@pytest.mark.asyncio
async def test_direct_selection_cannot_bypass_persisted_clock_restart(tmp_path):
    service = broker(tmp_path)
    original = room(speakers=[SpeakerRef(id="101", name="A", protocol="airplay2")])
    runtime = RuntimeRoom(original, tmp_path / "room")
    runtime.status = "running"
    runtime.backend_definition = original
    runtime.client = SimpleNamespace(outputs=AsyncMock(), select=AsyncMock(), volume_settings=AsyncMock())
    service.rooms[original.id] = runtime
    with pytest.raises(RpcError, match="restart with its selected clock"):
        await service.set_outputs(runtime, [original.speakers[0].model_copy(update={"airplay_timing": "ntp"})])
    runtime.client.outputs.assert_not_awaited()
    runtime.client.select.assert_not_awaited()
    assert runtime.desired == original and service.speaker_leases == {}


@pytest.mark.parametrize("reported", [None, "ptp"])
@pytest.mark.asyncio
async def test_missing_or_wrong_native_clock_prevents_selection(reported):
    requests = []
    client = OwnToneClient("http://private", transport=httpx.MockTransport(
        lambda request: requests.append(request) or httpx.Response(204)))
    speaker = SpeakerRef(id="101", name="A", protocol="airplay2", airplay_timing="ntp")
    raw = {"id": "101", "name": "A", "type": "AirPlay 2"}
    if reported is not None:
        raw["airplay_timing"] = reported
    try:
        with pytest.raises(RuntimeFailure, match="did not apply the requested AirPlay clock"):
            await client.select([speaker], [normalize_output(raw, set())])
        assert requests == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_changed_native_clock_readback_cannot_confirm_selection():
    requests = []
    def respond(request):
        requests.append((request.method, request.url.path))
        if request.url.path == "/api/outputs/set":
            return httpx.Response(204)
        return httpx.Response(200, json={"outputs": [
            {"id": "101", "name": "A", "type": "AirPlay 2", "selected": True, "airplay_timing": "ptp"}]})
    client = OwnToneClient("http://private", transport=httpx.MockTransport(respond))
    speaker = SpeakerRef(id="101", name="A", protocol="airplay2", airplay_timing="ntp")
    output = normalize_output({"id": "101", "name": "A", "type": "AirPlay 2", "airplay_timing": "ntp"}, set())
    try:
        with pytest.raises(RuntimeFailure, match="did not retain the requested AirPlay clock"):
            await client.select([speaker], [output])
        assert requests == [("PUT", "/api/outputs/set"), ("GET", "/api/outputs")]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_reverting_to_auto_cannot_apply_on_an_old_forced_clock_backend(tmp_path):
    from shiri.runtime.broker import Superseded
    service = broker(tmp_path)
    automatic = room(speakers=[SpeakerRef(id="101", name="A", protocol="airplay2")])
    forced = automatic.model_copy(update={"speakers": [automatic.speakers[0].model_copy(update={"airplay_timing": "ntp"})]})
    runtime = RuntimeRoom(automatic, tmp_path / "room")
    runtime.backend_definition = forced
    runtime.client = SimpleNamespace(outputs=AsyncMock(), select=AsyncMock(), volume_settings=AsyncMock())
    with pytest.raises(Superseded):
        await service._restore_outputs(runtime)
    runtime.client.outputs.assert_not_awaited()
    assert runtime.selected_ids == []


@pytest.mark.asyncio
async def test_applied_auto_snapshot_cannot_hide_an_old_native_clock_configuration(tmp_path):
    service = broker(tmp_path)
    automatic = room(speakers=[SpeakerRef(id="101", name="A", protocol="airplay2")])
    forced = automatic.model_copy(update={"speakers": [automatic.speakers[0].model_copy(update={"airplay_timing": "ntp"})]})
    runtime = RuntimeRoom(automatic, tmp_path / "room")
    runtime.backend_definition = forced
    runtime.applied = automatic
    runtime.status = "running"
    runtime.client = object()
    order = []
    async def stop(value):
        order.append("retire forced backend")
        value.client = None
        value.backend_definition = None
    async def start(value):
        order.append("start automatic backend")
        value.backend_definition = value.desired
        value.status = "running"
        service._closing = True
        return value.desired, value.current_volume
    service._stop_room = AsyncMock(side_effect=stop)
    service._start_room = AsyncMock(side_effect=start)
    service.ready = True
    runtime.wake.set()
    await service._room_loop(runtime)
    assert order == ["retire forced backend", "start automatic backend"]
    assert runtime.backend_definition == automatic
