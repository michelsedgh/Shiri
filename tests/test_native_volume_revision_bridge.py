"""Real phone-event, persistence and worker-revision path; hardware edges are held doubles."""

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from shiri.domain import RoomCreate, RoomPatch, SpeakerRef
from shiri.rpc import RpcError
from shiri.runtime import broker as broker_module
from shiri.runtime.native import NativeController, NativeHandle, NativeMixer
from shiri.runtime.timing import FLAG_AIRPLAY2, Kind, Packet
from shiri.service import RoomService
from shiri.settings import Settings
from shiri.store import Store
from test_runtime_review import wait_until


class Writer:
    reader_present = True
    written_bytes = dropped_bytes = 0

    def reset(self, owner):
        self.owner = owner

    def close(self):
        pass


class Backend:
    """Only the actual output protocol and its current-owner volume gate are replaced."""
    base_url = 'http://unused.invalid'

    def __init__(self):
        self.owner = None
        self.volume_calls = []

    async def request(self, method, path, *, json):
        if path == '/api/player/shiri-source':
            self.owner = {key: json[key] for key in ('incarnation', 'session_id', 'epoch', 'generation')}
        elif path == '/api/player/shiri-volume':
            assert {key: json[key] for key in self.owner} == self.owner
            self.volume_calls.append(json['volume'])
        else:
            raise AssertionError(path)
        return dict(json)

    async def volume(self, volume):
        self.volume_calls.append(volume)


class Runtime:
    def __init__(self, broker, room):
        self.broker, self.room = broker, room

    async def call(self, operation, payload=None):
        if operation == 'health':
            return self.broker.snapshot()
        if operation == 'ack_phone_volume':
            return await self.broker.ack_phone_volume(self.room, payload)
        if operation == 'reconcile':
            return await self.broker.reconcile(payload)
        raise AssertionError(operation)


@pytest.fixture
async def path(tmp_path, monkeypatch):
    store = Store(':memory:')
    definition = store.create_room(RoomCreate(name='Phone volume review', interface='sim0'))
    definition = store.update_room(definition.id, RoomPatch(enabled=True), definition.revision)
    broker = broker_module.Broker(Settings(state_dir=tmp_path / 'api', runtime_dir=tmp_path / 'run',
                                          runtime_state_dir=tmp_path / 'root'))
    broker.ready = True
    room = broker_module.RuntimeRoom(definition, tmp_path / 'room', current_volume=definition.volume)
    room.directory.mkdir()
    room.launch_generation = 'a1' * 16
    room.processes['audio'] = SimpleNamespace(name='audio', process=SimpleNamespace(pid=12345), alive=True)
    backend = Backend()
    room.client = backend
    broker.rooms[definition.id] = room
    service = RoomService(store, Runtime(broker, room))
    mixer = NativeMixer(Path('/unused'), writer=Writer(), now_ns=lambda: 15_000_000_500)
    native = NativeController(definition.id, mixer, backend, control_revision=definition.revision)
    worker_calls, receipts = [], []
    delivered = asyncio.Event()
    fail_worker = [False]

    async def worker_rpc(socket_path, operation, payload, *, timeout):
        assert socket_path == room.directory / 'audio.sock'
        if operation == 'receiver-master':
            assert timeout == 1
            return native.receiver_volume.queue(**payload)
        assert operation == 'control-intent' and timeout == 2
        worker_calls.append(dict(payload))
        if fail_worker[0]:
            fail_worker[0] = False
            raise RpcError('runtime_unavailable', 'Held worker ACK loss')
        return native.control_intent(payload['revision'])

    async def phone_rpc(socket_path, operation, payload, *, timeout):
        assert socket_path == room.directory / 'signals.sock'
        assert operation == 'native-volume' and timeout == 2
        receipt = await broker._native_volume(room, payload)
        receipts.append(receipt)
        return receipt

    monkeypatch.setattr(broker_module, 'call_rpc', worker_rpc)
    original_acknowledge = native.acknowledge_volume
    def acknowledge(event_id):
        original_acknowledge(event_id)
        delivered.set()
    monkeypatch.setattr(native, 'acknowledge_volume', acknowledge)
    await native.initialize()
    handle = NativeHandle(native)
    grant = await native.begin(Packet(Kind.BEGIN, uuid4().bytes, flags=FLAG_AIRPLAY2, group=uuid4().bytes), handle)
    await native.start_volume_bridge(room.directory / 'signals.sock', room.launch_generation,
                                     rpc=phone_rpc, retry_seconds=.001)
    review = SimpleNamespace(store=store, broker=broker, room=room, backend=backend, service=service,
                             native=native, handle=handle, grant=grant, receipts=receipts,
                             worker_calls=worker_calls, fail_worker=fail_worker, delivered=delivered)
    try:
        yield review
    finally:
        await native.close()
        mixer.close()
        store.close()


async def phone(review, volume):
    # Encode/decode the native packet rather than inject a broker-ready event.
    packet = Packet.decode(replace(review.grant, kind=Kind.VOLUME, frames=volume).encode())
    review.delivered.clear()
    await review.native.message(packet, review.handle)
    await asyncio.wait_for(review.delivered.wait(), timeout=1)
    return review.receipts[-1]


async def test_every_fresh_phone_move_after_poll_is_applied_without_web_volume_changes(path):
    initial = path.room.desired.revision
    for number, volume in enumerate((18, 39, 63, 0, 27, 100, 12), start=1):
        receipt = await phone(path, volume)
        assert receipt['accepted'], f'Fresh phone move {number} was rejected after a previous phone commit'
        await path.service.sync_phone_volume()
        await path.service.reconcile()
        stored = path.store.get_room(path.room.desired.id)
        assert stored.volume == path.room.current_volume == volume
        assert path.native.control_revision == stored.revision == initial + number
    assert path.backend.volume_calls == [18, 39, 63, 0, 27, 100, 12]
    assert not path.room.wake.is_set(), 'A phone ACK must not reselect or restart physical outputs'


async def test_worker_sync_failure_keeps_phone_receipt_for_exact_retry(path):
    assert (await phone(path, 18))['accepted']
    pending = dict(path.room.phone_volume_update)
    old_revision = path.native.control_revision
    path.fail_worker[0] = True
    await path.service.sync_phone_volume()
    assert path.store.get_room(path.room.desired.id).revision == old_revision + 1
    assert path.room.phone_volume_update == pending
    assert path.native.control_revision == old_revision
    await path.service.sync_phone_volume()
    assert path.room.phone_volume_update is None
    assert path.native.control_revision == old_revision + 1
    assert path.backend.volume_calls == [18]
    assert (await phone(path, 39))['accepted']
    await path.service.sync_phone_volume()
    assert path.store.get_room(path.room.desired.id).volume == 39


async def test_queued_phone_moves_stay_immediate_and_next_fresh_move_uses_committed_revision(path):
    assert (await phone(path, 18))['accepted']
    assert (await phone(path, 39))['accepted']
    await path.service.sync_phone_volume()
    assert path.room.current_volume == 39
    assert path.room.phone_volume_update['volume'] == 39
    assert path.native.control_revision == path.room.desired.revision
    await path.service.sync_phone_volume()
    assert path.room.phone_volume_update is None
    assert path.store.get_room(path.room.desired.id).volume == 39
    assert (await phone(path, 63))['accepted']
    await path.service.sync_phone_volume()
    assert path.backend.volume_calls == [18, 39, 63]
    assert path.store.get_room(path.room.desired.id).volume == 63


async def test_lost_old_ack_cannot_rebase_queued_phone_event_over_newer_ui_intent(path):
    assert (await phone(path, 18))['accepted']
    assert (await phone(path, 39))['accepted']
    path.fail_worker[0] = True
    await path.service.sync_phone_volume()  # 18 commits, ACK retained because worker RPC failed.
    committed = path.store.get_room(path.room.desired.id)
    ui = path.store.update_room(committed.id, RoomPatch(volume=72), committed.revision)
    await path.broker.reconcile({'rooms': [ui.model_dump(mode='json')]})
    await path.broker._sync_worker_intent(path.room)  # The live room actor's exact worker-intent step.
    await path.service.sync_phone_volume()  # Replay original receipt; preserve queued base=3.
    assert path.room.phone_volume_update['base_revision'] == committed.revision
    assert path.native.control_revision == ui.revision
    await path.service.sync_phone_volume()  # Queue is stale against newer UI volume.
    assert path.store.get_room(ui.id) == ui
    assert path.room.current_volume == 72
    assert path.room.phone_volume_update is None
    assert path.native.control_revision == ui.revision
    assert (await phone(path, 63))['accepted']
    await path.service.sync_phone_volume()
    assert path.store.get_room(ui.id).volume == 63


async def test_replayed_native_event_is_a_receipt_not_another_volume_command(path):
    path.delivered.clear()
    await path.native.message(Packet.decode(replace(path.grant, kind=Kind.VOLUME, frames=18).encode()), path.handle)
    event = dict(path.native.events[0])
    await asyncio.wait_for(path.delivered.wait(), timeout=1)
    saved_id = UUID(path.receipts[0]['event_id']).hex
    saved = path.room.native_volume_receipts[saved_id].copy()
    await path.service.sync_phone_volume()
    receipt = await path.broker._native_volume(path.room, {'launch_generation': path.room.launch_generation, **event})
    assert receipt == path.receipts[0]
    assert path.room.native_volume_receipts[saved_id] == saved
    assert path.native.control_revision == path.room.desired.revision
    assert path.backend.volume_calls == [18]


async def test_phone_commit_updates_receiver_default_without_sender_echo_then_web_edit_notifies(path):
    from unittest.mock import AsyncMock
    previous = path.room.desired
    assert (await phone(path, 18))['accepted']
    await path.service.sync_phone_volume()
    assert path.native.receiver_volume.volume == 18 and not path.native.receiver_volume.notify
    assert path.room.phone_volume_revision == path.room.desired.revision
    path.room.applied, path.room.status = previous, 'running'
    path.room.client.volume_settings = AsyncMock()
    task = asyncio.create_task(path.broker._room_loop(path.room))
    try:
        path.room.wake.set()
        await wait_until(lambda: path.room.applied == path.room.desired)
        assert not path.native.receiver_volume.notify
        web = path.room.desired.model_copy(update={'volume': 69, 'revision': path.room.desired.revision+1})
        path.room.desired, path.room.current_volume = web, web.volume
        path.room.wake.set()
        await wait_until(lambda: path.room.applied == path.room.desired)
        packet = path.native.receiver_volume.packet()
        assert packet.volume == 69 and packet.notify and packet.revision == web.revision
    finally:
        path.broker._closing = True
        path.room.wake.set()
        await task


async def route_actor(path, monkeypatch, *, hold=None, fail_selection=False):
    """Run the real broker actor; only discovery/gain/selection HTTP edges are held."""
    from shiri.runtime.system import RuntimeFailure
    original = SpeakerRef(id="1", name="Original", protocol="airplay2")
    previous = path.room.desired.model_copy(update={"speakers": [original]})
    path.room.desired = path.room.applied = previous
    path.room.backend_definition = previous
    path.room.status = "running"
    path.room.timing = path.room.active_timing = (500, 600)
    path.backend.gain_calls, path.backend.selection_calls = [], []
    path.backend.master, path.backend.selected = previous.volume, [original.id]
    path.feedback_calls = []
    entered, allowed = asyncio.Event(), asyncio.Event()
    observed, proceed = asyncio.Event(), asyncio.Event()
    original_queue = path.native.receiver_volume.queue

    def queue(revision, volume, notify):
        path.feedback_calls.append((revision, volume, notify))
        return original_queue(revision, volume, notify)

    monkeypatch.setattr(path.native.receiver_volume, "queue", queue)

    async def outputs(excluded):
        await asyncio.sleep(0)  # Real discovery HTTP yields even on an immediate reply.
        if hold == "outputs" and not entered.is_set():
            entered.set()
            await allowed.wait()
        return []

    async def volume_settings(volume, speakers):
        if hold == "gain" and not entered.is_set():
            entered.set()
            await allowed.wait()
        path.backend.gain_calls.append((volume, [speaker.id for speaker in speakers], path.room.applied.revision))
        path.backend.master = volume

    async def select(speakers, outputs):
        if hold in {"selection", "outputs"} and not observed.is_set():
            observed.set()
            await proceed.wait()
        path.backend.selection_calls.append([speaker.id for speaker in speakers])
        if fail_selection and speakers:
            raise RuntimeFailure("Held unavailable selected speaker")
        path.backend.selected = [speaker.id for speaker in speakers]
        return {"ok": True}

    path.backend.outputs = outputs
    path.backend.volume_settings = volume_settings
    path.backend.select = select
    keys = await path.broker._reserve_speakers(path.room, [original], None)
    await path.broker._commit_speakers(path.room, keys)
    task = asyncio.create_task(path.broker._room_loop(path.room))
    path.room.task = task
    return previous, task, entered, allowed, observed, proceed


async def stop_route_actor(path, task, *events):
    for event in events:
        event.set()
    path.broker._closing = True
    path.room.wake.set()
    await asyncio.wait_for(task, 1)


@pytest.mark.parametrize("new_volume", [50, 69])
async def test_acknowledged_assignment_restores_receiver_master_and_only_master_changes_notify(path, monkeypatch, new_volume):
    previous, task, entered, allowed, observed, proceed = await route_actor(path, monkeypatch, hold="selection")
    speaker = SpeakerRef(id="2", name="Replacement", protocol="airplay2")
    desired = previous.model_copy(update={"speakers": [speaker], "volume": new_volume, "revision": previous.revision+1})
    try:
        await path.broker.reconcile({"rooms": [desired.model_dump(mode="json")]})
        await asyncio.wait_for(observed.wait(), 1)
        assert path.backend.master == new_volume
        assert path.native.receiver_volume.volume == previous.volume
        assert not path.native.receiver_volume.applied_intent and not path.feedback_calls
        proceed.set()
        await wait_until(lambda: path.room.applied == desired)
        packet = path.native.receiver_volume.packet()
        assert path.native.receiver_volume.applied_intent
        assert packet.volume == new_volume and packet.revision == desired.revision
        assert packet.notify is (new_volume != previous.volume)
        assert path.backend.selected == [speaker.id]
        assert path.feedback_calls == [(desired.revision, new_volume, new_volume != previous.volume)]
        assert path.broker.speaker_leases == {("owntone", speaker.id): desired.id}
    finally:
        await stop_route_actor(path, task, allowed, proceed)


@pytest.mark.parametrize("route", ["assignment", "gain"])
async def test_newer_reconcile_during_backend_ack_preserves_exact_applied_intent_and_final_feedback(path, monkeypatch, route):
    previous, task, entered, allowed, observed, proceed = await route_actor(
        path, monkeypatch, hold="outputs" if route == "assignment" else "gain")
    intermediate_speaker = SpeakerRef(id="2", name="Replacement", protocol="airplay2") if route == "assignment" else previous.speakers[0]
    final_speaker = SpeakerRef(id="3", name="Latest", protocol="airplay2") if route == "assignment" else previous.speakers[0]
    intermediate = previous.model_copy(update={"speakers": [intermediate_speaker], "volume": 69, "revision": previous.revision+1})
    latest = intermediate.model_copy(update={"speakers": [final_speaker], "volume": 84, "revision": intermediate.revision+1})
    try:
        await path.broker.reconcile({"rooms": [intermediate.model_dump(mode="json")]})
        await asyncio.wait_for(entered.wait(), 1)
        await path.broker.reconcile({"rooms": [latest.model_dump(mode="json")]})
        allowed.set()
        if route == "assignment":
            await asyncio.wait_for(observed.wait(), 1)
            assert path.backend.gain_calls == [(69, [intermediate_speaker.id], previous.revision)]
            assert not path.feedback_calls and not path.native.receiver_volume.applied_intent
            proceed.set()
        await wait_until(lambda: path.room.applied == latest)
        assert path.backend.gain_calls == [(69, [intermediate_speaker.id], previous.revision),
                                           (84, [final_speaker.id], intermediate.revision)]
        assert path.backend.master == path.room.current_volume == 84
        if route == "assignment":
            assert path.backend.selected == [final_speaker.id]
        packet = path.native.receiver_volume.packet()
        assert packet.volume == 84 and packet.revision == latest.revision and packet.notify
        assert path.feedback_calls == [(latest.revision, 84, True)]
        assert path.broker.speaker_leases == {("owntone", final_speaker.id): latest.id}
    finally:
        await stop_route_actor(path, task, allowed, proceed)


async def test_failed_assignment_does_not_publish_master_and_retry_still_notifies(path, monkeypatch):
    previous, task, entered, allowed, observed, proceed = await route_actor(path, monkeypatch, fail_selection=True)
    desired = previous.model_copy(update={"speakers": [SpeakerRef(id="2", name="Replacement", protocol="airplay2")],
                                          "volume": 69, "revision": previous.revision+1})
    try:
        await path.broker.reconcile({"rooms": [desired.model_dump(mode="json")]})
        await wait_until(lambda: path.room.status == "degraded")
        assert not path.feedback_calls and not path.native.receiver_volume.applied_intent
        assert path.room.applied == previous
        async def recovered_select(speakers, outputs):
            path.backend.selected = [speaker.id for speaker in speakers]
            return {"ok": True}
        path.backend.select = recovered_select
        path.room.wake.set()
        await wait_until(lambda: path.room.applied == desired)
        assert path.native.receiver_volume.packet().notify
        assert path.feedback_calls == [(desired.revision, 69, True)]
    finally:
        await stop_route_actor(path, task, allowed, proceed)


@pytest.mark.parametrize("newer_during_restore", [False, True])
async def test_startup_final_selection_reports_exact_acknowledged_latest_definition(path, monkeypatch, newer_during_restore):
    previous, task, entered, allowed, observed, proceed = await route_actor(
        path, monkeypatch, hold="outputs" if newer_during_restore else None)
    boot_entered, boot_allowed = asyncio.Event(), asyncio.Event()
    path.room.client = None
    async def held_launch(room):
        # Process/network launch is held; the production final-start intent,
        # OwnTone selection ACK and receiver feedback orchestration are real.
        boot_entered.set()
        await boot_allowed.wait()
        room.client = path.backend
        async with room.control_lock:
            return await path.broker._finish_start_outputs(room)
    path.broker._start_room = held_launch
    intermediate = previous.model_copy(update={"speakers": [SpeakerRef(id="2", name="Post-start", protocol="airplay2")],
                                               "volume": 69, "revision": previous.revision+1})
    latest = intermediate.model_copy(update={"speakers": [SpeakerRef(id="3", name="Latest", protocol="airplay2")],
                                             "volume": 84, "revision": intermediate.revision+1}) if newer_during_restore else intermediate
    try:
        path.room.wake.set()
        await asyncio.wait_for(boot_entered.wait(), 1)
        await path.broker.reconcile({"rooms": [intermediate.model_dump(mode="json")]})
        boot_allowed.set()
        if newer_during_restore:
            await asyncio.wait_for(entered.wait(), 1)
            await path.broker.reconcile({"rooms": [latest.model_dump(mode="json")]})
            allowed.set()
            await asyncio.wait_for(observed.wait(), 1)
            assert path.backend.gain_calls == [(69, ["2"], previous.revision)]
            assert not path.feedback_calls
            proceed.set()
        await wait_until(lambda: path.room.applied == latest)
        assert path.backend.gain_calls[0] == (69, ["2"], previous.revision)
        if newer_during_restore:
            assert path.backend.gain_calls[1] == (84, ["3"], intermediate.revision)
        assert path.backend.master == latest.volume and path.backend.selected == [latest.speakers[0].id]
        packet = path.native.receiver_volume.packet()
        assert packet.revision == latest.revision and packet.volume == latest.volume and packet.notify
        assert path.feedback_calls == [(latest.revision, latest.volume, True)]
    finally:
        await stop_route_actor(path, task, boot_allowed, allowed, proceed)
