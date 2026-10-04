"""Bounded room readiness leases against fake clocks and gated backend setup.

These tests use ordinary room intent and record runtime RPCs. They never create
speech sessions, load an audio model, contact a VM or play sound.
"""

import asyncio
import re
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from shiri.domain import Conflict, NotFound, RoomCreate, RoomPatch, SpeakerRef
from shiri.readiness import RoomReadinessCoordinator, WarmRenew, WarmRequest
from shiri.rpc import RpcError
from shiri.service import RoomService
from shiri.settings import Settings
from shiri.store import Store
from shiri.tts.models import DEFAULT_MODEL_ID


class Clock:
    def __init__(self):
        self.value = 10_000_000_000

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += round(seconds * 1_000_000_000)


class WarmRuntime:
    """Recording connection ownership seam, with independently gated setup."""

    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.observations = []
        self.connections = {}
        self.acquire_entered = asyncio.Event()
        self.acquire_gate = None
        self.release_gate = None
        self.release_entered = asyncio.Event()
        self.failure = None
        self.receipt_changes = {}
        self.observe_changes = {}

    async def call(self, operation, payload=None):
        assert operation == "warm", "Presence must not request speech or PCM"
        message = dict(payload)
        action = message["action"]
        lease_id = message["lease_id"]
        if action == "observe":
            self.observations.append(message)
            if lease_id not in self.connections:
                raise RpcError("session_conflict", "The exact connection has retired")
            return {"state": "connected", "connected": True, "launch_generation": "a" * 32,
                    "output_count": 1, **self.observe_changes}
        self.calls.append(message)
        if action == "acquire":
            self.acquire_entered.set()
            if self.acquire_gate is not None:
                await self.acquire_gate.wait()
            if self.failure:
                raise self.failure
            self.connections[lease_id] = message
        elif action == "renew":
            if lease_id not in self.connections:
                raise RpcError("lease_not_found", "This connection lease is terminal")
            self.connections[lease_id] = message
        elif action == "release":
            self.release_entered.set()
            if self.release_gate is not None:
                await self.release_gate.wait()
            self.connections.pop(lease_id, None)
        else:
            raise AssertionError(action)
        return {"state": "released" if action == "release" else "connected",
                "connected": action != "release", "launch_generation": "a" * 32,
                "output_count": 1, **self.receipt_changes}


class TtsReadiness:
    def __init__(self):
        self.calls = []
        self.entered = asyncio.Event()
        self.gate = None
        self.failure = None
        self.state = "ready"

    async def warm(self, model_id, *, purpose):
        self.calls.append((model_id, purpose))
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.failure is not None:
            raise self.failure
        return {"state": self.state, "model_id": model_id, "busy": self.state == "busy"}


@pytest.fixture
async def rig(tmp_path):
    clock = Clock()
    store = Store(tmp_path / "warm.sqlite3")
    runtime = WarmRuntime(clock)
    room = store.create_room(RoomCreate(name="Kitchen", interface="eth0", nobly_room_id="Kitchen/exact"))
    room = store.update_room(room.id, RoomPatch(enabled=True), room.revision)
    room = store.assign_speakers(room.id, [SpeakerRef(id="101", name="Kitchen speaker", protocol="airplay2")],
                                 room.revision)
    other = store.create_room(RoomCreate(name="Other", interface="eth0"))
    service = RoomService(store, runtime)
    coordinator = RoomReadinessCoordinator(service, now_ns=clock)
    result = SimpleNamespace(clock=clock, store=store, runtime=runtime, room=room, other=other,
                             service=service, coordinator=coordinator)
    yield result
    if runtime.acquire_gate is not None:
        runtime.acquire_gate.set()
    if runtime.release_gate is not None:
        runtime.release_gate.set()
    await result.coordinator.close()
    store.close()


def acquire_request(number=1, **changes):
    return WarmRequest(request_id=f"{number:032x}", **changes)


def renew_request(number=100, **changes):
    return WarmRenew(request_id=f"{number:032x}", **changes)


async def until(predicate):
    async def observe():
        while not predicate():  # noqa: ASYNC110 - bounded by the enclosing two-second deadline.
            await asyncio.sleep(.001)
    await asyncio.wait_for(observe(), 2)


async def reconcile(rig):
    async with rig.service._mutation:
        await rig.coordinator.reconcile_locked()


async def automatic(rig, mode=True):
    await rig.coordinator.close()
    rig.coordinator = RoomReadinessCoordinator(rig.service, now_ns=rig.clock, automatic=mode)
    return rig.coordinator


async def maintain(rig, observed=None):
    async with rig.service._mutation:
        await rig.coordinator.maintain_locked(observed)


def auto_hold(rig):
    # Tests retain the internal identity to verify exact backend ownership.
    # The public room observation deliberately does not expose this identity.
    lease = rig.coordinator.leases.get(rig.coordinator._auto.get(rig.room.id))
    return {**lease.public(rig.clock()), "_automatic": True} if lease else None


def acquire_count(rig):
    return sum(call["action"] == "acquire" for call in rig.runtime.calls)


async def state(rig, admitted, expected="connected", *, model_state=None):
    async def observe():
        while True:
            if admitted.get("_automatic"):
                lease = rig.coordinator.leases[admitted["lease_id"]]
                result = lease.public(rig.clock())
            else:
                result = await rig.coordinator.get(rig.room.id, admitted["lease_id"])
            if result["state"] == expected and (model_state is None or result["model"].get("state") == model_state):
                return result
            await asyncio.sleep(.001)
    return await asyncio.wait_for(observe(), 2)


@pytest.mark.parametrize("ttl", [False, True, None, 0, 4, 301, "60", 60.0, float("nan")])
@pytest.mark.parametrize("request_type", [WarmRequest, WarmRenew])
def test_ttl_is_bounded_and_not_coerced(request_type, ttl):
    with pytest.raises(ValidationError):
        request_type(request_id="1" * 32, ttl_seconds=ttl)


@pytest.mark.parametrize("request_type", [WarmRequest, WarmRenew])
@pytest.mark.parametrize("request_id", ["", "a" * 31, "a" * 33, "A" * 32, "g" * 32, 1, None])
def test_request_identity_is_canonical(request_type, request_id):
    with pytest.raises(ValidationError):
        request_type(request_id=request_id)


def test_warm_request_defaults_and_boundary_ttls():
    request = WarmRequest()
    assert re.fullmatch("[0-9a-f]{32}", request.request_id)
    assert request.ttl_seconds == 60 and request.purpose == "presence" and request.model_id is None
    assert WarmRequest(ttl_seconds=5).ttl_seconds == 5
    assert WarmRequest(ttl_seconds=300, purpose="interaction").ttl_seconds == 300
    assert WarmRequest().request_id != request.request_id


@pytest.mark.parametrize("changes", [{"purpose": "always"}, {"purpose": True}, {"text": "Do not speak"},
                                      {"duck_gain": .2}, {"attack_ms": 300}, {"room_id": "other"}])
def test_presence_contract_cannot_become_audio_or_hidden_continuous_work(changes):
    with pytest.raises(ValidationError):
        WarmRequest(**changes)


async def test_acquire_returns_pending_without_waiting_or_holding_room_mutation(rig):
    rig.runtime.acquire_gate = asyncio.Event()
    admitted = await rig.coordinator.acquire(acquire_request(), external_id="Kitchen/exact")
    assert admitted["state"] == "pending" and admitted["admitted_room_id"] == rig.room.id
    assert re.fullmatch("[0-9a-f]{32}", admitted["lease_id"])
    assert admitted["remaining_ms"] == 60_000 and admitted["acoustic_ready"] is None
    await asyncio.wait_for(rig.runtime.acquire_entered.wait(), 2)
    async with rig.service._mutation:
        pass
    assert rig.runtime.calls[0]["action"] == "acquire"
    assert rig.runtime.calls[0]["deadline_monotonic_ns"] == rig.clock() + 60_000_000_000
    assert re.fullmatch("[0-9a-f]{64}", rig.runtime.calls[0]["transport_fingerprint"])
    assert not {"duck_gain", "pcm", "text", "speech_id", "session_id"}.intersection(rig.runtime.calls[0])
    rig.runtime.acquire_gate.set()
    connected = await state(rig, admitted)
    assert connected["connection"]["connected"] is True and connected["acoustic_ready"] is None


async def test_acquire_replay_does_not_extend_deadline_or_start_another_connection(rig):
    body = acquire_request(ttl_seconds=30)
    admitted = await rig.coordinator.acquire(body, room_id=rig.room.id)
    await state(rig, admitted)
    rig.clock.advance(7)
    replay = await rig.coordinator.acquire(body, room_id=rig.room.id)
    assert replay["lease_id"] == admitted["lease_id"] and replay["remaining_ms"] == 23_000
    assert len(rig.runtime.calls) == 1
    for changed in (acquire_request(ttl_seconds=31), acquire_request(purpose="interaction"),
                    acquire_request(model_id="some-model")):
        with pytest.raises(Conflict):
            await rig.coordinator.acquire(changed, room_id=rig.room.id)
    with pytest.raises(Conflict):
        await rig.coordinator.acquire(body, room_id=rig.other.id)
    assert len(rig.runtime.calls) == 1


async def test_acquire_replay_after_deadline_reports_expiry_without_resurrecting_connection(rig):
    body = acquire_request(ttl_seconds=5)
    admitted = await rig.coordinator.acquire(body, room_id=rig.room.id)
    await state(rig, admitted)
    rig.clock.advance(5)
    replay = await rig.coordinator.acquire(body, room_id=rig.room.id)
    assert replay["lease_id"] == admitted["lease_id"]
    assert replay["state"] == "expired" and replay["remaining_ms"] == 0
    await until(lambda: not rig.runtime.connections)
    assert acquire_count(rig) == 1


async def test_renew_replay_and_shorter_new_renewal_cannot_shorten_latest_expiry(rig):
    admitted = await rig.coordinator.acquire(acquire_request(ttl_seconds=30), room_id=rig.room.id)
    await state(rig, admitted)
    rig.clock.advance(10)
    body = renew_request(ttl_seconds=60)
    renewed = await rig.coordinator.renew(rig.room.id, admitted["lease_id"], body)
    assert renewed["remaining_ms"] == 60_000
    await until(lambda: len(rig.runtime.calls) >= 2)
    rig.clock.advance(10)
    calls = len(rig.runtime.calls)
    replay = await rig.coordinator.renew(rig.room.id, admitted["lease_id"], body)
    assert replay["remaining_ms"] == 50_000 and len(rig.runtime.calls) == calls
    short = await rig.coordinator.renew(rig.room.id, admitted["lease_id"], renew_request(101, ttl_seconds=5))
    assert short["remaining_ms"] == 50_000
    with pytest.raises(Conflict):
        await rig.coordinator.renew(rig.room.id, admitted["lease_id"], renew_request(ttl_seconds=61))


async def test_release_is_terminal_and_replays_cannot_resurrect_a_lease(rig):
    body = acquire_request()
    admitted = await rig.coordinator.acquire(body, room_id=rig.room.id)
    await state(rig, admitted)
    renewed_body = renew_request()
    await rig.coordinator.renew(rig.room.id, admitted["lease_id"], renewed_body)
    await until(lambda: len(rig.runtime.calls) >= 2)
    released = await rig.coordinator.release(rig.room.id, admitted["lease_id"])
    assert released["state"] == "released" and released["remaining_ms"] == 0
    await until(lambda: admitted["lease_id"] not in rig.runtime.connections)
    calls = len(rig.runtime.calls)
    assert (await rig.coordinator.release(rig.room.id, admitted["lease_id"]))["state"] == "released"
    assert (await rig.coordinator.acquire(body, room_id=rig.room.id))["state"] == "released"
    assert (await rig.coordinator.renew(rig.room.id, admitted["lease_id"], renewed_body))["state"] == "released"
    with pytest.raises(Conflict):
        await rig.coordinator.renew(rig.room.id, admitted["lease_id"], renew_request(101))
    assert len(rig.runtime.calls) == calls and not rig.runtime.connections


async def test_expiration_uses_admission_clock_and_stays_terminal_after_setup(rig):
    rig.runtime.acquire_gate = asyncio.Event()
    admitted = await rig.coordinator.acquire(acquire_request(ttl_seconds=5), room_id=rig.room.id)
    await asyncio.wait_for(rig.runtime.acquire_entered.wait(), 2)
    rig.clock.advance(5)
    expired = await rig.coordinator.get(rig.room.id, admitted["lease_id"])
    assert expired["state"] == "expired" and expired["remaining_ms"] == 0
    rig.runtime.acquire_gate.set()
    await until(lambda: any(call["action"] == "release" for call in rig.runtime.calls))
    assert (await rig.coordinator.get(rig.room.id, admitted["lease_id"]))["state"] == "expired"
    assert not rig.runtime.connections
    with pytest.raises(Conflict):
        await rig.coordinator.renew(rig.room.id, admitted["lease_id"], renew_request())


async def test_expiration_checked_when_late_setup_reply_arrives_without_polling(rig):
    rig.runtime.acquire_gate = asyncio.Event()
    admitted = await rig.coordinator.acquire(acquire_request(ttl_seconds=5), room_id=rig.room.id)
    await asyncio.wait_for(rig.runtime.acquire_entered.wait(), 2)
    rig.clock.advance(6)
    rig.runtime.acquire_gate.set()
    await state(rig, admitted, "expired")
    await until(lambda: admitted["lease_id"] not in rig.runtime.connections)


async def test_release_cancels_queued_renewal_and_original_pending_setup(rig):
    rig.runtime.acquire_gate = asyncio.Event()
    admitted = await rig.coordinator.acquire(acquire_request(), room_id=rig.room.id)
    await asyncio.wait_for(rig.runtime.acquire_entered.wait(), 2)
    await rig.coordinator.renew(rig.room.id, admitted["lease_id"], renew_request())
    await asyncio.sleep(0)
    await rig.coordinator.release(rig.room.id, admitted["lease_id"])
    rig.runtime.acquire_gate.set()
    await until(lambda: any(call["action"] == "release" for call in rig.runtime.calls))
    await rig.coordinator.close()
    assert (await rig.coordinator.get(rig.room.id, admitted["lease_id"]))["state"] == "released"
    assert not rig.runtime.connections
    assert [call["action"] for call in rig.runtime.calls].count("acquire") == 1


async def test_independent_leases_are_not_released_by_another_owner(rig):
    first = await rig.coordinator.acquire(acquire_request(ttl_seconds=5), room_id=rig.room.id)
    second = await rig.coordinator.acquire(acquire_request(2), room_id=rig.room.id)
    await state(rig, first)
    await state(rig, second)
    rig.clock.advance(5)
    await reconcile(rig)
    await until(lambda: first["lease_id"] not in rig.runtime.connections)
    assert (await rig.coordinator.get(rig.room.id, first["lease_id"]))["state"] == "expired"
    assert (await rig.coordinator.get(rig.room.id, second["lease_id"]))["state"] == "connected"
    assert set(rig.runtime.connections) == {second["lease_id"]}
    await rig.coordinator.release(rig.room.id, first["lease_id"])
    assert set(rig.runtime.connections) == {second["lease_id"]}


async def test_room_and_lease_uuid_are_required_for_followups(rig):
    admitted = await rig.coordinator.acquire(acquire_request(), room_id=rig.room.id)
    await state(rig, admitted)
    for method in (rig.coordinator.get, rig.coordinator.release):
        with pytest.raises(NotFound):
            await method(rig.other.id, admitted["lease_id"])
        with pytest.raises(NotFound):
            await method(rig.room.id, "f" * 32)
    with pytest.raises(NotFound):
        await rig.coordinator.renew(rig.other.id, admitted["lease_id"], renew_request())
    assert set(rig.runtime.connections) == {admitted["lease_id"]}


async def test_nobly_admission_is_exact_and_binding_move_never_retargets_existing_lease(rig):
    with pytest.raises(NotFound):
        await rig.coordinator.acquire(acquire_request(), external_id="kitchen/exact")
    admitted = await rig.coordinator.acquire(acquire_request(2), external_id="Kitchen/exact")
    await state(rig, admitted)
    old = rig.store.get_room(rig.room.id)
    rig.store.update_room(old.id, RoomPatch(nobly_room_id="previous"), old.revision)
    other = rig.store.get_room(rig.other.id)
    rig.store.update_room(other.id, RoomPatch(nobly_room_id="Kitchen/exact"), other.revision)
    await reconcile(rig)
    assert (await rig.coordinator.get(rig.room.id, admitted["lease_id"]))["state"] == "connected"
    await rig.coordinator.renew(rig.room.id, admitted["lease_id"], renew_request())
    await until(lambda: len(rig.runtime.calls) >= 2)
    assert {call["room_id"] for call in rig.runtime.calls} == {rig.room.id}


async def test_disabled_or_unassigned_room_never_starts_connection(rig):
    with pytest.raises(Conflict):
        await rig.coordinator.acquire(acquire_request(), room_id=rig.other.id)
    other = rig.store.get_room(rig.other.id)
    rig.store.update_room(other.id, RoomPatch(enabled=True), other.revision)
    with pytest.raises(Conflict):
        await rig.coordinator.acquire(acquire_request(2), room_id=rig.other.id)
    assert rig.runtime.calls == []


@pytest.mark.parametrize("change", ["disable", "interface", "receiver", "speakers", "offset", "clock", "delete"])
async def test_material_room_changes_revoke_pending_connection_and_fence_late_reply(rig, change):
    rig.runtime.acquire_gate = asyncio.Event()
    admitted = await rig.coordinator.acquire(acquire_request(), room_id=rig.room.id)
    await asyncio.wait_for(rig.runtime.acquire_entered.wait(), 2)
    room = rig.store.get_room(rig.room.id)
    if change == "disable":
        rig.store.update_room(room.id, RoomPatch(enabled=False), room.revision)
    elif change == "interface":
        rig.store.update_room(room.id, RoomPatch(interface="eth1"), room.revision)
    elif change == "receiver":
        rig.store.update_room(room.id, RoomPatch(airplay_name="A new receiver"), room.revision)
    elif change == "speakers":
        rig.store.assign_speakers(room.id, [], room.revision)
    elif change == "offset":
        rig.store.update_speaker_offset(room.id, "101", 10, room.revision)
    elif change == "clock":
        rig.store.update_speaker_airplay_timing(room.id, "101", "ntp", room.revision)
    else:
        room = rig.store.update_room(room.id, RoomPatch(enabled=False), room.revision)
        rig.store.delete_room(room.id, room.revision)
    await reconcile(rig)
    rig.runtime.acquire_gate.set()
    revoked = await state(rig, admitted, "revoked")
    assert revoked["remaining_ms"] == 0
    await until(lambda: any(call["action"] == "release" for call in rig.runtime.calls))
    assert not rig.runtime.connections


async def test_volume_balance_labels_and_binding_changes_preserve_compatible_connection(rig):
    admitted = await rig.coordinator.acquire(acquire_request(), room_id=rig.room.id)
    await state(rig, admitted)
    room = rig.store.get_room(rig.room.id)
    room = rig.store.update_room(room.id, RoomPatch(name="Renamed", nobly_room_id="new-binding", volume=19,
                                                   duck_gain=.4), room.revision)
    room = rig.store.update_speaker_balance(room.id, "101", 25, room.revision)
    speaker = room.speakers[0].model_copy(update={"name": "Different label"})
    rig.store.assign_speakers(room.id, [speaker], room.revision)
    await reconcile(rig)
    assert (await rig.coordinator.get(rig.room.id, admitted["lease_id"]))["state"] == "connected"
    assert len(rig.runtime.calls) == 1 and set(rig.runtime.connections) == {admitted["lease_id"]}


async def test_reconciliation_does_not_wait_for_remote_release_under_mutation_guard(rig):
    admitted = await rig.coordinator.acquire(acquire_request(), room_id=rig.room.id)
    await state(rig, admitted)
    rig.runtime.release_gate = asyncio.Event()
    room = rig.store.get_room(rig.room.id)
    rig.store.update_room(room.id, RoomPatch(enabled=False), room.revision)
    await asyncio.wait_for(reconcile(rig), 2)
    await asyncio.wait_for(rig.runtime.release_entered.wait(), 2)
    assert (await rig.coordinator.get(rig.room.id, admitted["lease_id"]))["state"] == "revoked"
    async with rig.service._mutation:
        pass
    rig.runtime.release_gate.set()
    await until(lambda: not rig.runtime.connections)


async def test_failed_setup_releases_only_its_connection_and_cannot_restart(rig):
    rig.runtime.failure = RpcError("backend_unavailable", "Injected warm failure")
    body = acquire_request()
    admitted = await rig.coordinator.acquire(body, room_id=rig.room.id)
    failed = await state(rig, admitted, "failed")
    assert failed["remaining_ms"] == 0 and failed["acoustic_ready"] is None
    await until(lambda: any(call["action"] == "release" for call in rig.runtime.calls))
    calls = len(rig.runtime.calls)
    assert (await rig.coordinator.acquire(body, room_id=rig.room.id))["state"] == "failed"
    assert len(rig.runtime.calls) == calls and not rig.runtime.connections


@pytest.mark.parametrize("receipt", [{"state": "heard"}, {"launch_generation": "wrong"},
                                     {"connected": "yes"}, {"output_count": True}, {"output_count": 0}])
async def test_invalid_connection_receipt_cannot_publish_ready(rig, receipt):
    rig.runtime.receipt_changes = receipt
    admitted = await rig.coordinator.acquire(acquire_request(), room_id=rig.room.id)
    failed = await state(rig, admitted, "failed")
    assert failed["acoustic_ready"] is None
    await until(lambda: admitted["lease_id"] not in rig.runtime.connections)


async def test_per_room_active_limit_preserves_existing_owners_and_admits_after_release(rig):
    admitted = [await rig.coordinator.acquire(acquire_request(i), room_id=rig.room.id) for i in range(1, 5)]
    for lease in admitted:
        await state(rig, lease)
    with pytest.raises(Conflict, match="capacity"):
        await rig.coordinator.acquire(acquire_request(5), room_id=rig.room.id)
    assert set(rig.runtime.connections) == {lease["lease_id"] for lease in admitted}
    await rig.coordinator.release(rig.room.id, admitted[0]["lease_id"])
    last = await rig.coordinator.acquire(acquire_request(5), room_id=rig.room.id)
    await state(rig, last)
    assert (await rig.coordinator.get(rig.room.id, admitted[1]["lease_id"]))["state"] == "connected"


async def test_retained_terminal_capacity_never_evicts_a_fence_to_admit_new_work(rig):
    first = None
    for number in range(1, 65):
        admitted = await rig.coordinator.acquire(acquire_request(number), room_id=rig.room.id)
        first = first or admitted
        await rig.coordinator.release(rig.room.id, admitted["lease_id"])
    with pytest.raises(Conflict, match="capacity"):
        await rig.coordinator.acquire(acquire_request(65), room_id=rig.room.id)
    assert (await rig.coordinator.get(rig.room.id, first["lease_id"]))["state"] == "released"
    rig.clock.advance(601)
    await reconcile(rig)
    with pytest.raises(NotFound):
        await rig.coordinator.get(rig.room.id, first["lease_id"])
    admitted = await rig.coordinator.acquire(acquire_request(65), room_id=rig.room.id)
    await state(rig, admitted)


async def test_shutdown_joins_pending_setup_and_rejects_new_leases(rig):
    rig.runtime.acquire_gate = asyncio.Event()
    admitted = await rig.coordinator.acquire(acquire_request(), room_id=rig.room.id)
    await asyncio.wait_for(rig.runtime.acquire_entered.wait(), 2)
    await rig.coordinator.close()
    assert not rig.runtime.connections
    assert (await rig.coordinator.get(rig.room.id, admitted["lease_id"]))["state"] == "released"
    with pytest.raises(Conflict, match="shutting down"):
        await rig.coordinator.acquire(acquire_request(2), room_id=rig.room.id)


async def test_shutdown_joins_original_setup_when_latest_renewal_never_started(rig):
    rig.runtime.acquire_gate = asyncio.Event()
    admitted = await rig.coordinator.acquire(acquire_request(), room_id=rig.room.id)
    await asyncio.wait_for(rig.runtime.acquire_entered.wait(), 2)
    await rig.coordinator.renew(rig.room.id, admitted["lease_id"], renew_request())
    # No scheduling turn for the newly created renewal task before shutdown.
    await asyncio.wait_for(rig.coordinator.close(), 2)
    rig.runtime.acquire_gate.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not rig.runtime.connections
    assert (await rig.coordinator.get(rig.room.id, admitted["lease_id"]))["state"] == "released"


async def test_presence_without_model_does_not_touch_generation_worker(rig):
    model = rig.coordinator.tts = TtsReadiness()
    admitted = await rig.coordinator.acquire(acquire_request(), room_id=rig.room.id)
    connected = await state(rig, admitted)
    assert model.calls == [] and connected["model"] == {}


async def test_interaction_primes_default_model_and_room_connection_concurrently(rig):
    model = rig.coordinator.tts = TtsReadiness()
    model.gate = asyncio.Event()
    rig.runtime.acquire_gate = asyncio.Event()
    admitted = await rig.coordinator.acquire(acquire_request(purpose="interaction"), room_id=rig.room.id)
    await asyncio.wait_for(model.entered.wait(), 2)
    await asyncio.wait_for(rig.runtime.acquire_entered.wait(), 2)
    assert model.calls == [(DEFAULT_MODEL_ID, "interaction")]
    model.gate.set()
    rig.runtime.acquire_gate.set()
    connected = await state(rig, admitted, model_state="ready")
    assert connected["model"]["model_id"] == DEFAULT_MODEL_ID and connected["acoustic_ready"] is None


@pytest.mark.parametrize("observation", ["busy", "unavailable"])
async def test_worker_busy_or_failure_preserves_successful_room_connection(rig, observation):
    model = rig.coordinator.tts = TtsReadiness()
    if observation == "busy":
        model.state = "busy"
    else:
        model.failure = Conflict("Another model operation owns generation")
    admitted = await rig.coordinator.acquire(acquire_request(model_id="selected-model"), room_id=rig.room.id)
    connected = await state(rig, admitted, model_state=observation)
    assert connected["connection"]["connected"] is True
    assert set(rig.runtime.connections) == {admitted["lease_id"]}
    assert not any(call["action"] == "release" for call in rig.runtime.calls)
    assert model.calls == [("selected-model", "presence")]


async def test_room_launch_change_revokes_cached_connection_on_observation(rig):
    admitted = await rig.coordinator.acquire(acquire_request(), room_id=rig.room.id)
    await state(rig, admitted)
    rig.runtime.observe_changes = {"launch_generation": "b" * 32}
    revoked = await rig.coordinator.get(rig.room.id, admitted["lease_id"])
    assert revoked["state"] == "revoked" and revoked["remaining_ms"] == 0
    await until(lambda: admitted["lease_id"] not in rig.runtime.connections)


async def test_transient_observation_failure_is_degraded_and_recovers_without_new_acquire(rig):
    admitted = await rig.coordinator.acquire(acquire_request(), room_id=rig.room.id)
    await state(rig, admitted)
    rig.runtime.observe_changes = {"connected": "invalid"}
    degraded = await rig.coordinator.get(rig.room.id, admitted["lease_id"])
    assert degraded["state"] == "degraded" and degraded["acoustic_ready"] is None
    rig.runtime.observe_changes = {}
    assert (await rig.coordinator.get(rig.room.id, admitted["lease_id"]))["state"] == "connected"
    assert len(rig.runtime.calls) == 1


async def test_request_retention_cap_refuses_new_receipts_without_eviction(rig):
    body = acquire_request()
    admitted = await rig.coordinator.acquire(body, room_id=rig.room.id)
    await state(rig, admitted)
    for number in range(1000, 1255):
        await rig.coordinator.renew(rig.room.id, admitted["lease_id"], renew_request(number))
        await until(lambda expected=number - 998: len(rig.runtime.calls) == expected)
    with pytest.raises(Conflict, match="retention"):
        await rig.coordinator.renew(rig.room.id, admitted["lease_id"], renew_request(1255))
    assert (await rig.coordinator.acquire(body, room_id=rig.room.id))["lease_id"] == admitted["lease_id"]


@pytest.mark.parametrize("mode", [False, "on_demand"])
async def test_on_demand_mode_never_starts_autonomous_connections(rig, mode):
    await automatic(rig, mode)
    for _ in range(3):
        await maintain(rig)
        rig.clock.advance(60)
    assert rig.runtime.calls == []
    assert rig.coordinator.room_observation(rig.room.id) == {"mode": "on_demand", "hold": None}
    admitted = await rig.coordinator.acquire(acquire_request(), room_id=rig.room.id)
    await state(rig, admitted)
    assert acquire_count(rig) == 1


async def test_ready_policy_runs_without_nobly_or_model_and_renews_thirty_second_guard(rig):
    room = rig.store.get_room(rig.room.id)
    rig.store.update_room(room.id, RoomPatch(nobly_room_id=None), room.revision)
    await automatic(rig)
    model = rig.coordinator.tts = TtsReadiness()
    await maintain(rig)
    admitted = auto_hold(rig)
    assert admitted["purpose"] == "presence" and admitted["remaining_ms"] == 60_000
    await state(rig, admitted)
    assert rig.coordinator.room_observation(rig.room.id)["mode"] == "ready"
    assert model.calls == [] and rig.coordinator.receipts == {}
    rig.clock.advance(29)
    await maintain(rig)
    assert acquire_count(rig) == 1
    rig.clock.advance(1)
    await maintain(rig)
    await until(lambda: acquire_count(rig) == 2)
    renewed = await state(rig, admitted)
    assert renewed["remaining_ms"] == 60_000
    assert rig.runtime.calls[-1]["deadline_monotonic_ns"] == rig.clock() + 60_000_000_000
    for expected in range(3, 16):
        rig.clock.advance(30)
        await maintain(rig)
        await until(lambda expected=expected: acquire_count(rig) == expected)
        assert (await state(rig, admitted))["remaining_ms"] == 60_000
    assert auto_hold(rig)["lease_id"] == admitted["lease_id"] and len(rig.coordinator.leases) == 1
    assert model.calls == []


async def test_ready_policy_uses_finite_expiry_if_maintenance_stops(rig):
    await automatic(rig)
    await maintain(rig)
    admitted = auto_hold(rig)
    await state(rig, admitted)
    rig.clock.advance(60)
    await reconcile(rig)
    assert auto_hold(rig)["state"] == "expired"
    await until(lambda: not rig.runtime.connections)
    assert rig.runtime.calls[0]["deadline_monotonic_ns"] == rig.clock() and acquire_count(rig) == 1


async def test_automatic_hold_identity_is_private_and_public_controls_cannot_mutate_it(rig):
    await automatic(rig)
    await maintain(rig)
    admitted = auto_hold(rig)
    await state(rig, admitted)
    public = rig.coordinator.room_observation(rig.room.id)["hold"]
    assert "lease_id" not in public and "admitted_room_id" not in public
    assert public["state"] == "connected" and public["acoustic_ready"] is None
    lease_id = admitted["lease_id"]
    original = rig.coordinator.leases[lease_id].public(rig.clock())
    original_calls = list(rig.runtime.calls)
    with pytest.raises(NotFound):
        rig.coordinator._get(rig.room.id, lease_id)
    for operation in (rig.coordinator.get(rig.room.id, lease_id),
                      rig.coordinator.renew(rig.room.id, lease_id, renew_request()),
                      rig.coordinator.release(rig.room.id, lease_id)):
        with pytest.raises(NotFound):
            await operation
    assert rig.coordinator.leases[lease_id].public(rig.clock()) == original
    assert rig.runtime.calls == original_calls and rig.coordinator.receipts == {}


async def test_rejected_automatic_renewal_retry_remains_not_found_after_internal_pruning(rig):
    await automatic(rig)
    await maintain(rig)
    admitted = auto_hold(rig)
    await state(rig, admitted)
    renewal = renew_request()
    with pytest.raises(NotFound):
        await rig.coordinator.renew(rig.room.id, admitted["lease_id"], renewal)
    assert renewal.request_id not in rig.coordinator.receipts
    room = rig.store.get_room(rig.room.id)
    rig.store.update_room(room.id, RoomPatch(enabled=False), room.revision)
    await reconcile(rig)
    await until(lambda: not rig.runtime.connections)
    await maintain(rig)
    assert admitted["lease_id"] not in rig.coordinator.leases
    with pytest.raises(NotFound):
        await rig.coordinator.renew(rig.room.id, admitted["lease_id"], renewal)
    assert renewal.request_id not in rig.coordinator.receipts


async def test_disabling_room_retires_automatic_hold_and_reenable_starts_fresh_window(rig):
    await automatic(rig, "adaptive")
    await maintain(rig)
    old = auto_hold(rig)
    await state(rig, old)
    room = rig.store.get_room(rig.room.id)
    room = rig.store.update_room(room.id, RoomPatch(enabled=False), room.revision)
    await reconcile(rig)
    await maintain(rig)
    await until(lambda: not rig.runtime.connections)
    assert acquire_count(rig) == 1
    rig.clock.advance(301)
    rig.store.update_room(room.id, RoomPatch(enabled=True), room.revision)
    await reconcile(rig)
    await maintain(rig)
    fresh = auto_hold(rig)
    assert fresh["lease_id"] != old["lease_id"]
    assert (await state(rig, fresh))["remaining_ms"] == 60_000


async def test_automatic_failures_back_off_to_two_minutes_and_success_resets_delay(rig):
    await automatic(rig)
    rig.runtime.failure = RpcError("backend_unavailable", "Sleeping speaker")
    await maintain(rig)
    for delay in (5, 10, 20, 40, 80, 120, 120):
        admitted = auto_hold(rig)
        await state(rig, admitted, "failed")
        calls = acquire_count(rig)
        rig.clock.advance(delay - 1)
        await maintain(rig)
        assert acquire_count(rig) == calls
        rig.clock.advance(1)
        await maintain(rig)
        await until(lambda calls=calls: acquire_count(rig) == calls + 1)
    await state(rig, auto_hold(rig), "failed")
    rig.runtime.failure = None
    rig.clock.advance(120)
    await maintain(rig)
    recovered = auto_hold(rig)
    await state(rig, recovered)
    rig.runtime.failure = RpcError("backend_unavailable", "Speaker went away again")
    rig.clock.advance(30)
    await maintain(rig)
    await state(rig, recovered, "failed")
    calls = acquire_count(rig)
    rig.clock.advance(4)
    await maintain(rig)
    assert acquire_count(rig) == calls
    rig.clock.advance(1)
    await maintain(rig)
    await until(lambda: acquire_count(rig) == calls + 1)
    assert not rig.coordinator.receipts and len(rig.coordinator.leases) <= 2


async def test_automatic_transport_churn_prunes_internal_fences_without_consuming_api_receipts(rig):
    await automatic(rig)
    await maintain(rig)
    await state(rig, auto_hold(rig))
    for number in range(1, 70):
        room = rig.store.get_room(rig.room.id)
        rig.store.update_speaker_offset(room.id, "101", number, room.revision)
        await reconcile(rig)
        await maintain(rig)
        await state(rig, auto_hold(rig))
        assert len(rig.coordinator.leases) <= 2
    assert rig.coordinator.receipts == {} and acquire_count(rig) == 70


async def test_adaptive_startup_window_ends_without_activity_and_preserves_external_lease(rig):
    await automatic(rig, "adaptive")
    await maintain(rig)
    auto = auto_hold(rig)
    await state(rig, auto)
    rig.clock.advance(290)
    external = await rig.coordinator.acquire(acquire_request(), room_id=rig.room.id)
    await state(rig, external)
    rig.clock.advance(10)
    await maintain(rig)
    assert auto_hold(rig) is None or auto_hold(rig)["state"] == "expired"
    await until(lambda: auto["lease_id"] not in rig.runtime.connections)
    assert (await rig.coordinator.get(rig.room.id, external["lease_id"]))["state"] == "connected"
    rig.clock.advance(1)
    await maintain(rig)
    assert acquire_count(rig) == 2


@pytest.mark.parametrize("kind", ["music_active", "speech_active"])
async def test_fresh_room_activity_extends_adaptive_window_without_nobly(rig, kind):
    await automatic(rig, "adaptive")
    await maintain(rig)
    await state(rig, auto_hold(rig))
    rig.clock.advance(290)
    await maintain(rig, {"rooms": [{"room_id": rig.room.id, "activity": {
        kind: True, "observed_monotonic_ns": rig.clock(),
    }}]})
    admitted = auto_hold(rig)
    await state(rig, admitted)
    assert admitted["remaining_ms"] == 60_000
    rig.clock.advance(299)
    await maintain(rig)
    fresh = auto_hold(rig)
    assert fresh["remaining_ms"] == 1_000
    await state(rig, fresh)
    rig.clock.advance(1)
    await maintain(rig)
    await until(lambda: not rig.runtime.connections)
    assert auto_hold(rig)["state"] == "expired"


@pytest.mark.parametrize("age_ns,active", [(15_000_000_001, True), (-1, True), (0, False), (0, 1), (0, "yes")])
async def test_stale_future_or_nonboolean_activity_cannot_extend_adaptive_window(rig, age_ns, active):
    await automatic(rig, "adaptive")
    await maintain(rig)
    await state(rig, auto_hold(rig))
    rig.clock.advance(290)
    await maintain(rig, {"rooms": [{"room_id": rig.room.id, "activity": {
        "music_active": active, "observed_monotonic_ns": rig.clock() - age_ns,
    }}]})
    if auto_hold(rig)["state"] == "pending":
        await state(rig, auto_hold(rig))
    rig.clock.advance(10)
    await maintain(rig)
    await until(lambda: not rig.runtime.connections)
    assert auto_hold(rig)["state"] == "expired"


async def test_activity_at_freshness_boundary_and_explicit_touch_restart_adaptive_readiness(rig):
    await automatic(rig, "adaptive")
    await maintain(rig)
    await state(rig, auto_hold(rig))
    rig.clock.advance(300)
    await maintain(rig)
    await until(lambda: not rig.runtime.connections)
    rig.coordinator.touch(rig.room.id)
    await maintain(rig)
    await state(rig, auto_hold(rig))
    rig.clock.advance(290)
    await maintain(rig, {"rooms": [{"room_id": rig.room.id, "activity": {
        "speech_active": True, "observed_monotonic_ns": rig.clock() - 15_000_000_000,
    }}]})
    assert (await state(rig, auto_hold(rig)))["remaining_ms"] == 60_000


async def test_material_change_after_idle_pruning_opens_a_new_adaptive_startup_window(rig):
    await automatic(rig, "adaptive")
    await maintain(rig)
    await state(rig, auto_hold(rig))
    rig.clock.advance(300)
    await maintain(rig)
    await until(lambda: not rig.runtime.connections)
    await asyncio.sleep(0)
    await maintain(rig)
    assert auto_hold(rig) is None
    room = rig.store.get_room(rig.room.id)
    rig.store.update_speaker_offset(room.id, "101", 10, room.revision)
    await reconcile(rig)
    await maintain(rig)
    assert auto_hold(rig) is not None
    await state(rig, auto_hold(rig))


@pytest.mark.parametrize("activity", [None, [], "invalid"])
async def test_invalid_runtime_activity_shape_is_ignored_without_breaking_maintenance(rig, activity):
    await automatic(rig, "adaptive")
    await maintain(rig, {"rooms": [{"room_id": rig.room.id, "activity": activity}]})
    await state(rig, auto_hold(rig))


def test_default_policy_keeps_rooms_ready_with_explicit_power_saving_choices():
    assert Settings().speaker_readiness == "ready"
    assert Settings(speaker_readiness="adaptive").speaker_readiness == "adaptive"
    assert Settings(speaker_readiness="ready").speaker_readiness == "ready"
    assert Settings(speaker_readiness="on_demand").speaker_readiness == "on_demand"
    for value in (True, False, None, "always", ""):
        with pytest.raises(ValueError, match="Speaker readiness"):
            Settings(speaker_readiness=value)
