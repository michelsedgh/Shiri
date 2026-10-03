"""Real HTTP client contract and room actor; only device/server edges are doubled."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import httpx
import pytest
from shiri.domain import Room, RoomCreate, SpeakerRef
from shiri.runtime.backend import OwnToneClient
from shiri.runtime.broker import Broker, RuntimeRoom
from shiri.runtime.system import RuntimeFailure
from shiri.service import RoomService
from shiri.settings import Settings
from shiri.store import Store

def speaker(balance=60):
    return SpeakerRef(id="101", name="Speaker", protocol="airplay2", balance_percent=balance)

def definition(**changes):
    return Room.model_validate({"id": str(uuid4()), "slot": 0, "name": "Kitchen", "airplay_name": "Kitchen",
                                "interface": "eth0", "enabled": True, "speakers": [speaker()], **changes})

@pytest.mark.asyncio
@pytest.mark.parametrize("wrong", ["ack", "master", "balance", None])
async def test_room_gain_settings_require_exact_http_ack_and_readback(wrong):
    calls = []
    def server(request):
        path = request.url.path
        calls.append((request.method, path))
        if path == "/api/player/shiri-volume-settings":
            body = json.loads(request.content)
            assert body == {"volume": 15, "outputs": [{"id": "101", "balance_percent": 60}]}
            if wrong == "ack":
                body["outputs"][0]["balance_percent"] = 100
            return httpx.Response(200, json=body)
        if path == "/api/player":
            return httpx.Response(200, json={"volume": 9 if wrong == "master" else 15})
        if path == "/api/outputs":
            return httpx.Response(200, json={"outputs": [{"id": "101", "name": "Speaker", "type": "AirPlay 2", "selected": True,
                                                         "volume": 9, "balance_percent": 100 if wrong == "balance" else 60}]})
        raise AssertionError((request.method, path))
    client = OwnToneClient("http://private.invalid", transport=httpx.MockTransport(server))
    try:
        if wrong:
            with pytest.raises(RuntimeFailure):
                await client.volume_settings(15, [speaker()])
        else:
            assert await client.volume_settings(15, [speaker()]) == {"ok": True}
        assert all(path not in {"/api/outputs/set", "/api/player/pause", "/api/player/play"} for _, path in calls)
    finally:
        await client.close()

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["volume", "balance"])
async def test_running_room_volume_or_balance_does_not_reselect_pause_or_restart(tmp_path, kind):
    broker = Broker(Settings(state_dir=tmp_path / "api", runtime_state_dir=tmp_path / "root", runtime_dir=tmp_path / "run"))
    broker.ready = True
    previous = definition(volume=50)
    room = RuntimeRoom(previous, tmp_path / "room", current_volume=50)
    room.applied, room.status = previous, "running"
    room.backend_definition, room.active_timing = previous, room.timing
    room.client = SimpleNamespace(volume_settings=AsyncMock())
    broker._sync_worker_intent = AsyncMock()
    broker._restore_outputs = AsyncMock(side_effect=AssertionError("No selection for gain mutations"))
    broker._stop_room = AsyncMock(side_effect=AssertionError("No restart for gain mutations"))
    broker._start_room = AsyncMock(side_effect=AssertionError("No startup for gain mutations"))
    desired = previous.model_copy(update={"revision": previous.revision + 1,
                                         **({"volume": 15} if kind == "volume" else {"speakers": [speaker(40)]})})
    room.desired, room.current_volume = desired, desired.volume
    task = asyncio.create_task(broker._room_loop(room))
    room.wake.set()
    for _ in range(100):
        if room.applied == desired:
            break
        await asyncio.sleep(0)
    assert room.applied == desired
    room.client.volume_settings.assert_awaited_once_with(desired.volume, desired.speakers)
    broker._restore_outputs.assert_not_awaited()
    broker._stop_room.assert_not_awaited()
    broker._start_room.assert_not_awaited()
    broker._closing = True
    room.wake.set()
    await task

@pytest.mark.asyncio
async def test_start_or_reassignment_stages_trims_before_selection_under_the_lease(tmp_path):
    broker = Broker(Settings(state_dir=tmp_path / "api", runtime_state_dir=tmp_path / "root", runtime_dir=tmp_path / "run"))
    room = RuntimeRoom(definition(volume=15), tmp_path / "room", current_volume=15)
    calls = []
    async def record(name, result, *args):
        calls.append(name)
        return result
    room.client = SimpleNamespace(outputs=lambda *args: record("inventory", []),
                                  volume_settings=lambda *args: record("settings", {"ok": True}),
                                  select=lambda *args: record("select", {"ok": True}))
    broker._reserve_speakers = lambda *args: record("reserve", {("owntone", "101")})
    broker._commit_speakers = lambda *args: record("commit", None)
    await broker._restore_outputs(room)
    assert calls == ["inventory", "reserve", "settings", "select", "commit"]
    assert room.selected_ids == ["101"] and room.status == "running"

@pytest.mark.asyncio
async def test_lost_gain_ack_preserves_playing_route_and_retries_without_reselection(tmp_path):
    broker = Broker(Settings(state_dir=tmp_path / "api", runtime_state_dir=tmp_path / "root", runtime_dir=tmp_path / "run"))
    broker.ready = True
    previous = definition(volume=50)
    room = RuntimeRoom(previous, tmp_path / "room", current_volume=15)
    room.applied, room.status = previous, "running"
    room.backend_definition, room.active_timing = previous, room.timing
    room.desired = previous.model_copy(update={"volume": 15, "revision": previous.revision + 1})
    client = SimpleNamespace(volume_settings=AsyncMock(side_effect=[RuntimeFailure("Lost gain ACK"), {"ok": True}]))
    room.client = client
    broker._sync_worker_intent = AsyncMock()
    broker._restore_outputs = AsyncMock(side_effect=AssertionError("No reselection on gain retry"))
    broker._stop_room = AsyncMock(side_effect=AssertionError("No restart on gain retry"))
    task = asyncio.create_task(broker._room_loop(room))
    room.wake.set()
    for _ in range(100):
        if room.gain_pending:
            break
        await asyncio.sleep(0)
    assert room.gain_pending and room.status == "degraded" and room.client is client
    assert room.applied == previous and not room.restart_required
    room.wake.set()
    for _ in range(100):
        if room.applied == room.desired:
            break
        await asyncio.sleep(0)
    assert room.applied == room.desired and room.status == "running" and not room.gain_pending
    assert client.volume_settings.await_count == 2
    broker._restore_outputs.assert_not_awaited()
    broker._stop_room.assert_not_awaited()
    broker._closing = True
    room.wake.set()
    await task

@pytest.mark.asyncio
async def test_service_saves_trim_durably_before_reconcile_and_keeps_room_master(tmp_path):
    with Store(tmp_path / "state.sqlite3") as store:
        room = store.create_room(RoomCreate(name="Kitchen", interface="eth0"))
        room = store.assign_speakers(room.id, [speaker(100)], room.revision)
        reconciled = []
        async def runtime_call(operation, payload=None):
            if operation == "health":
                return {"rooms": []}
            assert operation == "reconcile"
            saved = store.get_room(room.id)
            assert saved.speakers[0].balance_percent == 60 and saved.volume == 50
            reconciled.append(payload)
            return {"ok": True}
        service = RoomService(store, SimpleNamespace(call=runtime_call))
        result = await service.balance(room.id, "101", 60, room.revision)
        assert result["room"]["volume"] == 50 and result["room"]["speakers"][0]["balance_percent"] == 60
        assert len(reconciled) == 1
