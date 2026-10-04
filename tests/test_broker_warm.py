"""Room/launch admission fences using broker methods and a recording worker seam.

These tests start no processes, services, network connections or audio devices.
The stale unconfirmed-release test also uses the real AudioWorker lease registry.
"""

import asyncio
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from shiri.domain import Room, SpeakerRef
from shiri.readiness import transport_fingerprint
from shiri.rpc import RpcError
from shiri.runtime.audio import AudioWorker
from shiri.runtime.broker import Broker, RuntimeRoom
from shiri.settings import Settings


@pytest.fixture
def rig(tmp_path, monkeypatch):
    definition = Room(id=str(uuid4()), slot=0, name="Living room", airplay_name="Living room input",
                      interface="eth0", enabled=True,
                      speakers=[SpeakerRef(id="101", name="Speaker", protocol="airplay2")])
    room = RuntimeRoom(definition, tmp_path / "room", status="running", selected_ids=["101"],
                       client=SimpleNamespace(base_url="http://fixture.invalid"),
                       processes={"audio": SimpleNamespace(name="audio", alive=True, process=SimpleNamespace(pid=123))},
                       launch_generation="a" * 32)
    broker = Broker(Settings(state_dir=tmp_path / "state", runtime_dir=tmp_path / "run",
                             runtime_state_dir=tmp_path / "runtime"))
    broker.rooms[definition.id] = room
    broker.ready, broker.error = True, None
    calls = []

    async def rpc(socket, operation, payload, *, timeout):
        calls.append((socket, operation, dict(payload), timeout))
        return {"state": "connected", "connected": True, "output_count": 1}

    monkeypatch.setattr("shiri.runtime.broker.call_rpc", rpc)
    return SimpleNamespace(broker=broker, room=room, calls=calls)


def message(rig, *, action="acquire", lease_id="1" * 32, launch="a" * 32):
    payload = {"room_id": rig.room.desired.id, "action": action, "lease_id": lease_id,
               "transport_fingerprint": transport_fingerprint(rig.room.desired),
               "launch_generation": launch}
    if action == "acquire":
        payload["deadline_monotonic_ns"] = time.monotonic_ns() + 60_000_000_000
    return payload


async def test_exact_launch_and_worker_socket_are_preserved_without_speech_reservation(rig):
    payload = message(rig)
    result = await rig.broker.dispatch("warm", payload)
    assert result == {"state": "connected", "connected": True, "output_count": 1,
                      "launch_generation": "a" * 32}
    assert rig.calls == [(rig.room.directory / "audio.sock", "warm", {
        "action": "acquire", "lease_id": "1" * 32,
        "deadline_monotonic_ns": payload["deadline_monotonic_ns"],
    }, 8)]
    assert rig.broker.sessions == rig.broker.session_generations == {}
    assert not rig.broker.pending_sessions and not rig.broker._session_operations


@pytest.mark.parametrize("action", ["acquire", "observe", "release"])
@pytest.mark.parametrize("launch", ["b" * 32, "A" * 32, "", False, 1])
async def test_stale_or_invalid_expected_launch_never_reaches_worker(rig, action, launch):
    with pytest.raises(RpcError) as error:
        await rig.broker.dispatch("warm", message(rig, action=action, launch=launch))
    assert error.value.code in {"session_conflict", "invalid_request"}
    assert rig.calls == []


@pytest.mark.parametrize("change", ["interface", "slot", "airplay_name", "enabled",
                                   "speaker_id", "protocol", "offset", "clock"])
@pytest.mark.parametrize("action", ["acquire", "observe"])
async def test_material_transport_changes_refuse_old_lease_before_worker_rpc(rig, change, action):
    payload = message(rig, action=action)
    changes = {"interface": "eth1", "slot": 1, "airplay_name": "New receiver", "enabled": False}
    if change in changes:
        rig.room.desired = rig.room.desired.model_copy(update={change: changes[change]})
    else:
        updates = {"speaker_id": {"id": "102"}, "protocol": {"protocol": "airplay1"},
                   "offset": {"offset_ms": 30}, "clock": {"airplay_timing": "ntp"}}
        speaker = rig.room.desired.speakers[0].model_copy(update=updates[change])
        rig.room.desired = rig.room.desired.model_copy(update={"speakers": [speaker]})
    with pytest.raises(RpcError) as error:
        await rig.broker.dispatch("warm", payload)
    assert error.value.code == "session_conflict" and rig.calls == []


async def test_volume_balance_and_labels_preserve_existing_connection_intent(rig):
    payload = message(rig, action="observe")
    speaker = rig.room.desired.speakers[0].model_copy(update={"name": "Renamed speaker", "balance_percent": 75})
    rig.room.desired = rig.room.desired.model_copy(update={
        "volume": 23, "name": "Renamed room", "nobly_room_id": "new-binding", "speakers": [speaker], "revision": 8,
    })
    assert (await rig.broker.dispatch("warm", payload))["launch_generation"] == "a" * 32
    assert len(rig.calls) == 1 and rig.calls[0][2] == {"action": "observe", "lease_id": "1" * 32}


@pytest.mark.parametrize("condition", ["no_selected_outputs", "no_client", "stopped", "removing", "unknown_room"])
async def test_unavailable_room_cannot_begin_connection_setup(rig, condition):
    payload = message(rig)
    if condition == "no_selected_outputs":
        rig.room.selected_ids = []
    elif condition == "no_client":
        rig.room.client = None
    elif condition == "stopped":
        rig.room.status = "stopped"
    elif condition == "removing":
        rig.room.removing = True
    else:
        payload["room_id"] = str(uuid4())
    with pytest.raises(RpcError):
        await rig.broker.dispatch("warm", payload)
    assert rig.calls == []


async def test_exact_release_after_audio_worker_stopped_is_idempotent_without_rpc(rig):
    rig.room.processes.clear()
    rig.room.status, rig.room.client = "stopped", None
    payload = message(rig, action="release")
    for _ in range(2):
        assert await rig.broker.dispatch("warm", payload) == {"state": "released", "launch_generation": "a" * 32}
    assert rig.calls == []


@pytest.mark.parametrize("changes", [{"action": "speech"}, {"action": "renew"}, {"lease_id": "A" * 32},
                                     {"lease_id": True}, {"text": "Do not admit audio"}, {"duck_gain": .1},
                                     {"session_id": "voice"}, {"unknown": True},
                                     {"deadline_monotonic_ns": True}, {"deadline_monotonic_ns": "120"}])
async def test_control_schema_rejects_audio_fields_and_bad_admission_before_worker(rig, changes):
    with pytest.raises(RpcError) as error:
        await rig.broker.dispatch("warm", {**message(rig), **changes})
    assert error.value.code == "invalid_request" and rig.calls == []


@pytest.mark.parametrize("action", ["observe", "release"])
async def test_observation_and_release_cannot_carry_an_acquire_deadline(rig, action):
    payload = {**message(rig, action=action), "deadline_monotonic_ns": time.monotonic_ns() + 60_000_000_000}
    with pytest.raises(RpcError) as error:
        await rig.broker.dispatch("warm", payload)
    assert error.value.code == "invalid_request" and rig.calls == []


async def test_missing_acquire_deadline_is_refused_before_worker(rig):
    payload = message(rig)
    del payload["deadline_monotonic_ns"]
    with pytest.raises(RpcError) as error:
        await rig.broker.dispatch("warm", payload)
    assert error.value.code == "invalid_request" and rig.calls == []


async def test_inflight_launch_replacement_cannot_return_old_ready_receipt(rig, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def rpc(_socket, _operation, _payload, **_options):
        entered.set()
        await release.wait()
        return {"state": "connected", "connected": True, "output_count": 1}

    monkeypatch.setattr("shiri.runtime.broker.call_rpc", rpc)
    pending = asyncio.create_task(rig.broker.dispatch("warm", message(rig)))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        rig.room.launch_generation = "b" * 32
        release.set()
        with pytest.raises(RpcError) as error:
            await asyncio.wait_for(pending, 1)
        assert error.value.code == "session_conflict"
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_warm_rpc_keeps_room_control_lock_until_exact_receipt_completes(rig, monkeypatch):
    entered, release, mutated = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def rpc(_socket, _operation, _payload, **_options):
        entered.set()
        await release.wait()
        return {"state": "connected", "connected": True, "output_count": 1}

    async def room_change():
        async with rig.room.control_lock:
            mutated.set()

    monkeypatch.setattr("shiri.runtime.broker.call_rpc", rpc)
    pending = asyncio.create_task(rig.broker.dispatch("warm", message(rig)))
    mutation = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        mutation = asyncio.create_task(room_change())
        await asyncio.sleep(0)
        assert not mutated.is_set() and not pending.done()
        release.set()
        assert (await asyncio.wait_for(pending, 1))["launch_generation"] == "a" * 32
        await asyncio.wait_for(mutation, 1)
        assert mutated.is_set()
    finally:
        release.set()
        await asyncio.gather(*(task for task in (pending, mutation) if task), return_exceptions=True)


async def test_warm_failure_leaves_music_source_health_and_speech_reservations_unchanged(rig, monkeypatch):
    rig.room.player = {"state": "play", "position_ms": 1234}
    rig.broker.sessions["retained-voice"] = rig.room.desired.id
    voice_generation = rig.broker.session_generations["retained-voice"] = object()
    before = await rig.broker.dispatch("health", {})

    async def rpc(_socket, _operation, _payload, **_options):
        raise RpcError("audio_unavailable", "Speaker setup refused")

    monkeypatch.setattr("shiri.runtime.broker.call_rpc", rpc)
    with pytest.raises(RpcError):
        await rig.broker.dispatch("warm", message(rig))
    assert await rig.broker.dispatch("health", {}) == before
    assert rig.broker.sessions == {"retained-voice": rig.room.desired.id}
    assert rig.broker.session_generations["retained-voice"] is voice_generation
    assert not rig.room.restart_required and not rig.room.wake.is_set()


async def test_unconfirmed_old_release_installs_only_its_worker_fence_and_preserves_replacement(rig, monkeypatch):
    class Native:
        def __init__(self):
            self.ids, self.releases = set(), []

        async def warm_connection(self, identity, _deadline):
            self.ids.add(identity)
            return {"state": "connected", "connected": True, "output_count": 1}

        async def observe_warm_connection(self, identity):
            assert identity in self.ids
            return {"state": "connected", "connected": True, "output_count": 1}

        async def release_warm_connection(self, identity):
            self.releases.append(identity)
            self.ids.discard(identity)

        async def close(self):
            pass

    old = message(rig, action="release", launch=None)
    rig.room.launch_generation = "b" * 32
    native = Native()
    worker = AudioWorker(SimpleNamespace(close=lambda: None), native=native)

    async def rpc(_socket, operation, payload, **_options):
        return await worker.dispatch(operation, payload)

    monkeypatch.setattr("shiri.runtime.broker.call_rpc", rpc)
    try:
        await rig.broker.dispatch("warm", message(rig, lease_id="2" * 32, launch="b" * 32))
        retained = set(native.ids)
        assert (await rig.broker.dispatch("warm", old))["state"] == "released"
        assert native.ids == retained and native.releases == [] and worker.session is None
        with pytest.raises(RpcError) as error:
            await rig.broker.dispatch("warm", message(rig, launch="b" * 32))
        assert error.value.code == "session_conflict" and native.ids == retained
        assert (await rig.broker.dispatch("warm", message(rig, action="observe", lease_id="2" * 32,
                                                        launch="b" * 32)))["connected"] is True
    finally:
        await worker.close()
