"""Real phone-event, persistence and worker-revision path; hardware edges are held doubles."""

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from shiri.domain import RoomCreate, RoomPatch
from shiri.rpc import RpcError
from shiri.runtime import broker as broker_module
from shiri.runtime.native import NativeController, NativeHandle, NativeMixer
from shiri.runtime.timing import FLAG_AIRPLAY2, Kind, Packet
from shiri.service import RoomService
from shiri.settings import Settings
from shiri.store import Store


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
