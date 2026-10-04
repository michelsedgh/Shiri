"""Delayed health observations must belong to the room launch they inspected."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from shiri.domain import Room
from shiri.runtime.broker import Broker, RuntimeRoom
from shiri.runtime.system import RuntimeFailure
from shiri.settings import Settings


@pytest.fixture
def rig(tmp_path):
    broker = Broker(Settings(state_dir=tmp_path / "state", runtime_dir=tmp_path / "run",
                             runtime_state_dir=tmp_path / "runtime"))
    definition = Room(id=str(uuid4()), slot=0, name="Kitchen", airplay_name="Kitchen input",
                      interface="eth0", enabled=True)
    room = RuntimeRoom(definition, tmp_path / "room", status="running",
                       client=SimpleNamespace(request=AsyncMock(return_value={"old_player": True})),
                       receiver={"namespace": "old-receiver"}, launch_generation="a" * 32,
                       processes={"audio": SimpleNamespace(alive=True)})
    broker.rooms[definition.id] = room
    broker.network = SimpleNamespace(healthy=AsyncMock(return_value=True))
    broker._worker_rpc = AsyncMock(return_value={"ready": True})
    return broker, room


@pytest.mark.parametrize("phase", ["network", "player", "worker", "bluetooth"])
@pytest.mark.parametrize("retirement", ["stop", "restart"])
@pytest.mark.parametrize("failure", [False, True])
async def test_retired_room_probe_cannot_fail_monitor_or_change_successor(rig, phase, retirement, failure):
    broker, room = rig
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        if failure:
            raise RuntimeFailure("retired launch failed")
        return {"old_observation": True} if phase in {"player", "worker"} else True

    if phase == "network":
        broker.network.healthy = delayed
    elif phase == "player":
        room.client.request = delayed
    elif phase == "worker":
        broker._worker_rpc = delayed
    else:
        room.bluetooth_admission = SimpleNamespace(check=delayed)

    probe = asyncio.create_task(broker._probe_room(room))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        # Model the lifecycle transition while a real asynchronous probe waits.
        # The replacement is already healthy; an old reply cannot restart it.
        room.launch_generation = "b" * 32 if retirement == "restart" else None
        room.receiver = {"namespace": "new-receiver"} if retirement == "restart" else None
        room.client = SimpleNamespace(request=AsyncMock(return_value={})) if retirement == "restart" else None
        room.processes = {"audio": SimpleNamespace(alive=True)} if retirement == "restart" else {}
        room.bluetooth_admission = None
        room.status = "running" if retirement == "restart" else "stopped"
        room.player = {"new_player": True}
        room.activity = {"new_activity": True}
        room.receiver_volume = {"new_volume": True}
        room.error = None
        room.last_health_at = "new-observation"
        broker.sessions["new-speech"] = room.desired.id
        broker.session_generations["new-speech"] = object()
        release.set()
        await asyncio.wait_for(probe, 1)
        assert room.player == {"new_player": True}
        assert room.activity == {"new_activity": True}
        assert room.receiver_volume == {"new_volume": True}
        assert room.last_health_at == "new-observation"
        assert room.error is None
        assert not room.restart_required and not room.wake.is_set()
        assert broker.sessions == {"new-speech": room.desired.id}
    finally:
        release.set()
        probe.cancel()
        await asyncio.gather(probe, return_exceptions=True)


@pytest.mark.parametrize("failure", [False, True])
async def test_retired_sender_probe_cannot_restart_current_rooms(rig, monkeypatch, failure):
    broker, room = rig
    broker.sender = {"namespace": "old-sender"}

    async def healthy(record):
        assert record["namespace"] == "old-sender"
        broker.sender = {"namespace": "new-sender"}
        if failure:
            raise RuntimeFailure("old sender retired")
        return False

    async def probe(current):
        assert current is room
        broker._closing = True

    broker.network.healthy = healthy
    broker._probe_room = probe
    monkeypatch.setattr("shiri.runtime.broker.asyncio.sleep", AsyncMock())
    await broker._health_monitor()
    assert not room.restart_required and not room.wake.is_set()


@pytest.mark.parametrize("phase", ["network", "player", "worker", "bluetooth"])
async def test_current_launch_failure_still_requests_recovery(rig, phase):
    broker, room = rig
    failing = AsyncMock(side_effect=RuntimeFailure("current launch failed"))
    if phase == "network":
        broker.network.healthy = failing
    elif phase == "player":
        room.client.request = failing
    elif phase == "worker":
        broker._worker_rpc = failing
    else:
        room.bluetooth_admission = SimpleNamespace(check=failing)
    await broker._probe_room(room)
    assert room.restart_required and room.wake.is_set()
    assert room.error == "current launch failed"


async def test_live_speech_receipt_does_not_hide_current_bluetooth_failure(rig):
    broker, room = rig
    room.processes["bluetooth-output"] = SimpleNamespace(alive=True)
    broker.sessions["current-speech"] = room.desired.id
    broker.session_generations["current-speech"] = object()
    broker._worker_rpc.side_effect = [
        {"ready": True, "speech_session_id": "current-speech"},
        {"ready": False, "error": "Bluetooth output stopped"},
    ]
    await broker._probe_room(room)
    assert room.restart_required and room.wake.is_set()
    assert room.error == "Bluetooth output stopped"
    assert broker.sessions == {"current-speech": room.desired.id}
