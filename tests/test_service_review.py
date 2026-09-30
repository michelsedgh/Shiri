"""Independent volume/reconciliation races using actual SQLite and broker methods.

Rooms stay disabled and the broker never starts hardware workers. Only the RPC
delivery timing is controlled; receipts, acknowledgments, and reconciliation
use production implementations.
"""
import asyncio
from types import SimpleNamespace

import pytest

from shiri.domain import RoomCreate, RoomPatch
from shiri.rpc import RpcError
from shiri.runtime.broker import Broker, RuntimeRoom
from shiri.service import RoomService
from shiri.settings import Settings
from shiri.store import Store


class ReviewRuntime:
    def __init__(self, broker, room):
        self.broker, self.room = broker, room
        self.pause_reconcile = False
        self.captured = asyncio.Event()
        self.release = asyncio.Event()
        self.fail_next_ack = False
        self.acknowledgments = []

    async def call(self, operation, payload=None):
        if operation == "health":
            return self.broker.snapshot()
        if operation == "reconcile":
            if self.pause_reconcile:
                self.captured.set()
                await self.release.wait()
            return await self.broker.reconcile(payload)
        if operation == "ack_phone_volume":
            self.acknowledgments.append(dict(payload))
            if self.fail_next_ack:
                self.fail_next_ack = False
                raise RpcError("runtime_unavailable", "The acknowledgment was lost after the SQLite commit")
            return await self.broker.ack_phone_volume(self.room, payload)
        raise AssertionError(f"Unexpected review runtime operation: {operation}")


@pytest.fixture
def review(tmp_path):
    store = Store(tmp_path / "intent.sqlite3")
    definition = store.create_room(RoomCreate(name="Review room", interface="sim0"))
    broker = Broker(Settings(state_dir=tmp_path, runtime_dir=tmp_path / "run",
                             runtime_state_dir=tmp_path / "runtime"))
    room = RuntimeRoom(definition, tmp_path / "runtime" / definition.id, current_volume=definition.volume)
    broker.rooms[definition.id] = room
    runtime = ReviewRuntime(broker, room)
    service = RoomService(store, runtime)
    try:
        yield SimpleNamespace(store=store, definition=definition, broker=broker, room=room,
                              runtime=runtime, service=service)
    finally:
        store.close()


async def test_older_reconcile_cannot_regress_a_new_phone_receipt_and_runtime_revision(review):
    review.runtime.pause_reconcile = True
    old = asyncio.create_task(review.service.reconcile())
    try:
        await asyncio.wait_for(review.runtime.captured.wait(), 1)
        await review.broker.set_volume(review.room, 18, pending=True)
        await review.service.sync_phone_volume()
        saved = review.store.get_room(review.definition.id)
        assert (saved.revision, saved.volume) == (2, 18)
        assert (review.room.desired.revision, review.room.current_volume) == (2, 18)
        review.runtime.release.set()
        await asyncio.wait_for(old, 1)
        assert review.store.get_room(saved.id) == saved
        assert (review.room.desired.revision, review.room.current_volume) == (2, 18)
        assert review.room.desired.volume == 18
    finally:
        review.runtime.release.set()
        await asyncio.wait_for(asyncio.gather(old, return_exceptions=True), 1)


async def test_equal_revision_reconcile_still_applies_ui_fields_not_in_the_volume_ack(review):
    ui = review.store.update_room(review.definition.id,
                                  RoomPatch(name="UI room name", duck_gain=0.4, volume=72), 1)
    # The phone event is stale. Its rejection ACK advances only runtime volume
    # and revision, while the rest of the UI definition is still outstanding.
    await review.broker.set_volume(review.room, 18, pending=True)
    await review.service.sync_phone_volume()
    assert review.room.desired.revision == ui.revision
    assert review.room.desired.name == review.definition.name
    assert review.room.current_volume == 72
    await review.service.reconcile()
    assert review.room.desired == ui
    assert review.room.desired.duck_gain == 0.4
    assert review.room.current_volume == 72


async def test_reconcile_of_first_committed_phone_move_keeps_the_latest_immediate_move(review):
    await review.broker.set_volume(review.room, 18, pending=True)
    await review.broker.set_volume(review.room, 39, pending=True)
    await review.service.reconcile()
    first = review.store.get_room(review.definition.id)
    assert (first.revision, first.volume) == (2, 18)
    assert review.room.current_volume == 39
    assert review.room.phone_volume_update["volume"] == 39
    assert review.room.phone_volume_update["base_revision"] == 2
    await review.service.sync_phone_volume()
    last = review.store.get_room(first.id)
    assert (last.revision, last.volume) == (3, 39)
    assert review.room.current_volume == 39
    assert review.room.phone_volume_update is None


async def test_lost_old_ack_preserves_a_new_phone_move_created_after_newer_ui_intent(review):
    await review.broker.set_volume(review.room, 18, pending=True)
    review.runtime.fail_next_ack = True
    await review.service.sync_phone_volume()
    ui = review.store.update_room(review.definition.id, RoomPatch(volume=72), 2)
    await review.broker.reconcile({"rooms": [ui.model_dump(mode="json")]})
    await review.broker.set_volume(review.room, 39, pending=True)
    assert review.room.phone_volume_next["base_revision"] == 3
    await review.service.sync_phone_volume()  # Replay receipt A, original commit revision 2.
    assert review.runtime.acknowledgments[-1]["committed_revision"] == 2
    assert review.room.phone_volume_update["base_revision"] == 3
    assert review.room.current_volume == 39
    await review.service.sync_phone_volume()
    newest = review.store.get_room(ui.id)
    assert (newest.revision, newest.volume) == (4, 39)
    assert review.room.current_volume == 39 and review.room.phone_volume_update is None


async def test_receipt_replay_does_not_rebase_an_old_queued_move_onto_newer_ui_intent(review):
    await review.broker.set_volume(review.room, 18, pending=True)
    await review.broker.set_volume(review.room, 39, pending=True)
    review.runtime.fail_next_ack = True
    await review.service.sync_phone_volume()
    ui = review.store.update_room(review.definition.id, RoomPatch(volume=72), 2)
    await review.broker.reconcile({"rooms": [ui.model_dump(mode="json")]})
    await review.service.sync_phone_volume()
    assert review.room.phone_volume_update["base_revision"] == 2
    await review.service.sync_phone_volume()
    assert review.store.get_room(ui.id) == ui
    assert review.room.current_volume == 72 and review.room.phone_volume_update is None
