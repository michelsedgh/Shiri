"""Real broker startup/room actor; process, network and OwnTone HTTP edges are held."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from shiri.domain import Room, SpeakerRef
from shiri.runtime import broker as module
from shiri.runtime.system import RuntimeFailure
from shiri.settings import Settings
from test_runtime_review import wait_until


@pytest.fixture
async def startup(tmp_path, monkeypatch):
    room_definition = Room(id=str(uuid4()), slot=0, name="Startup", airplay_name="Startup input", interface="eth0", enabled=True,
                           speakers=[SpeakerRef(id="1", name="Speaker", protocol="airplay2")], volume=23)
    broker = module.Broker(Settings(state_dir=tmp_path/"api", runtime_dir=tmp_path/"run", runtime_state_dir=tmp_path/"root"))
    room = module.RuntimeRoom(room_definition, tmp_path/"room", current_volume=23, timing=(500,600))
    broker.rooms[room_definition.id] = room
    broker.ready = True
    receiver_started, receiver_ready = asyncio.Event(), asyncio.Event()
    discovered, discovery_allowed = asyncio.Event(), asyncio.Event()
    fixture = SimpleNamespace(broker=broker, room=room, receiver_started=receiver_started, receiver_ready=receiver_ready,
                              discovered=discovered, discovery_allowed=discovery_allowed, fail_group=False,
                              hold_discovery=False, events=[], selections=[], gain_calls=[], intent_calls=[])
    fixture.backend = SimpleNamespace(base_url="http://unused.invalid", master=23, selected=[])
    async def request(method, path, **kwargs):
        assert method == "GET" and path == "/api/player"
        return {"state":"stop", "volume": fixture.backend.master}
    async def volume(value):
        fixture.backend.master = value
    async def outputs(excluded):
        fixture.events.append("outputs")
        if fixture.hold_discovery and not discovered.is_set():
            discovered.set()
            await discovery_allowed.wait()
        return [{"id":"1", "name":"Speaker"}]
    async def volume_settings(value, speakers):
        fixture.events.append("gain_ack")
        fixture.gain_calls.append((value, [speaker.id for speaker in speakers]))
        fixture.backend.master = value
    async def select(speakers, outputs):
        if fixture.fail_group and speakers:
            raise RuntimeFailure("Configured speaker unavailable")
        fixture.events.append("selection_ack")
        fixture.selections.append([speaker.id for speaker in speakers])
        fixture.backend.selected = fixture.selections[-1]
        return {"ok":True}
    fixture.backend.request, fixture.backend.volume = request, volume
    fixture.backend.outputs, fixture.backend.volume_settings, fixture.backend.select = outputs, volume_settings, select
    monkeypatch.setattr(module, "OwnToneClient", lambda *_args, **_kwargs: fixture.backend)
    broker.network = SimpleNamespace(interfaces=AsyncMock(return_value=[{"name":"eth0", "eligible":True}]),
                                     create_receiver=AsyncMock(return_value={"interface":"receiver0", "namespace":"fake-receiver"}))
    broker._ensure_sender = AsyncMock(return_value={"api_host_ip":"10.190.1.1", "api_ip":"10.190.1.2", "namespace":"fake-sender"})
    broker._resolve_local_pin = Mock(return_value=None)
    broker._account = lambda owner,role,slot=None: {"name":f"fake-{role}", "uid":200, "gid":200}
    broker._stop_reserved_units = AsyncMock()
    broker._retire_speech_endpoint = Mock()
    broker._start_namespace_services = AsyncMock(return_value={})
    broker._refresh_processes = AsyncMock()
    broker.binary = lambda name: "/unused/"+name
    def directory(path, *_args, **_kwargs):
        path.mkdir(parents=True, exist_ok=True)
        return path
    monkeypatch.setattr(module, "private_directory", directory)
    monkeypatch.setattr(module, "prepare_room_view", lambda *_args: None)
    monkeypatch.setattr(module, "file_owner", lambda *_args: None)
    server = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
    monkeypatch.setattr(module, "serve_rpc", AsyncMock(return_value=server))
    async def worker_rpc(socket, operation, payload, *, timeout):
        if operation == "control-intent":
            fixture.intent_calls.append(payload["revision"])
        elif operation not in {"health", "receiver-master"}:
            raise AssertionError(operation)
        return {"ready":True}
    monkeypatch.setattr(module, "call_rpc", worker_rpc)
    async def start_process(key, name, command, directory, **kwargs):
        fixture.events.append(name)
        if name == "shairport":
            assert fixture.selections, "Receiver exposed before the first complete selection ACK"
            assert fixture.backend.master == room.current_volume
            assert room.status == "starting", "Selection alone must not report receiver readiness"
            receiver_started.set()
        return SimpleNamespace(alive=True, name=name, process=SimpleNamespace(pid=12345, returncode=None))
    broker._start_process = start_process
    async def wait_receiver_ready(*args):
        await receiver_ready.wait()
    monkeypatch.setattr("shiri.runtime.receiver_readiness.wait_receiver_ready", wait_receiver_ready)
    try:
        yield fixture
    finally:
        discovery_allowed.set()
        receiver_ready.set()
        if room.signal_server:
            room.signal_server.close()
            await room.signal_server.wait_closed()
        await broker._release_speakers(room)
        if broker.slot_locks[room_definition.slot].locked():
            broker.slot_locks[room_definition.slot].release()


@pytest.mark.parametrize("empty", [False, True])
async def test_receiver_advertisement_follows_gain_and_selection_ack_while_empty_setup_keeps_discovery(startup, empty):
    if empty:
        startup.room.desired = startup.room.desired.model_copy(update={"speakers":[]})
        startup.room.timing = (40,140)
    task = asyncio.create_task(startup.broker._start_room(startup.room))
    try:
        await asyncio.wait_for(startup.receiver_started.wait(), 1)
        assert startup.room.status == "starting"
        assert startup.events.index("gain_ack") < startup.events.index("selection_ack") < startup.events.index("shairport")
        assert startup.selections == [[] if empty else ["1"]]
        assert (await startup.room.client.outputs(set()))[0]["id"] == "1", "Enabled empty room must retain OwnTone discovery"
        startup.receiver_ready.set()
        applied, master = await asyncio.wait_for(task, 1)
        assert applied == startup.room.desired and master == startup.room.current_volume == 23
        assert startup.room.status == "running"
    finally:
        startup.receiver_ready.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_unavailable_configured_group_fails_before_advertisement_and_room_actor_backs_off(startup):
    startup.fail_group = True
    cleanup_calls = []
    async def cleanup(room):
        cleanup_calls.append(room)
        await startup.broker._release_speakers(room)
        room.client = None
        room.processes.clear()
        room.receiver = None
        if startup.broker.slot_locks[room.desired.slot].locked():
            startup.broker.slot_locks[room.desired.slot].release()
    startup.broker._stop_room = cleanup
    task = asyncio.create_task(startup.broker._room_loop(startup.room))
    try:
        startup.room.wake.set()
        await wait_until(lambda: startup.room.status == "error")
        assert not startup.receiver_started.is_set() and "shairport" not in startup.events
        assert startup.room.error == "Configured speaker unavailable"
        assert startup.room.failures == 1 and startup.room.retry_at > asyncio.get_running_loop().time()
        assert startup.room.client is None and not startup.broker.speaker_leases and cleanup_calls == [startup.room]
    finally:
        startup.broker._closing = True
        startup.room.wake.set()
        await asyncio.wait_for(task, 1)


async def test_newer_master_during_preselection_cannot_be_advertised_as_acknowledged(startup):
    startup.hold_discovery = True
    task = asyncio.create_task(startup.broker._start_room(startup.room))
    try:
        await asyncio.wait_for(startup.discovered.wait(), 1)
        previous = startup.room.desired
        newer = previous.model_copy(update={"volume":69, "revision":previous.revision+1})
        await startup.broker.reconcile({"rooms":[newer.model_dump(mode="json")]})
        startup.discovery_allowed.set()
        with pytest.raises(module.Superseded):
            await asyncio.wait_for(task, 1)
        assert startup.gain_calls == [(23,["1"])] and not startup.receiver_started.is_set()
    finally:
        startup.discovery_allowed.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_releasing_own_failed_selection_wakes_waiting_sibling_without_bypassing_own_backoff(startup):
    sibling_definition = startup.room.desired.model_copy(update={"id":str(uuid4()), "slot":1, "name":"Waiting", "airplay_name":"Waiting input"})
    sibling = module.RuntimeRoom(sibling_definition, startup.room.directory.parent/"sibling", status="degraded")
    startup.broker.rooms[sibling_definition.id] = sibling
    startup.room.status = "degraded"
    keys = await startup.broker._reserve_speakers(startup.room, startup.room.desired.speakers, None)
    await startup.broker._commit_speakers(startup.room, keys)
    await startup.broker._release_speakers(startup.room)
    assert sibling.wake.is_set() and not startup.room.wake.is_set()
    assert not startup.broker.speaker_leases
