"""Broker retirement proofs; no units, devices, or network links are changed.

The inode/type checks of the unlink helper have separate real-datagram tests.
Here its call is observed while the actual broker locks, stop/start methods,
durable process map, and backend state perform the recovery transaction.
"""

import asyncio
from copy import deepcopy
import os
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from shiri.domain import Room
from shiri.runtime import broker as module
from shiri.runtime.broker import Broker, RuntimeRoom
from shiri.runtime.system import RuntimeFailure
from shiri.settings import Settings


class Proof:
    """An exact externally checked unit's stop, with an observable await."""

    def __init__(self, name, events):
        self.name, self.events = name, events
        self.stopped = False
        self.failure = None
        self.entered = asyncio.Event()
        self.release = None

    async def stop(self):
        self.events.append(("stop-enter", self.name))
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.failure is not None:
            raise self.failure
        self.stopped = True
        self.events.append(("stop-proved", self.name))


@pytest.fixture
def recovery(monkeypatch):
    # Short paths keep the real Unix endpoint portable on Darwin.
    with TemporaryDirectory(prefix="shiri-retire-order-", dir="/tmp") as temporary:
        root = Path(temporary)
        service = Broker(Settings(state_dir=root / "api", runtime_state_dir=root / "state",
                                  runtime_dir=root / "run", runtime_socket=root / "run" / "rpc.sock"))
        definition = Room.model_validate({"id": str(uuid4()), "slot": 0, "name": "Kitchen",
                                          "airplay_name": "Kitchen input", "interface": "eth0", "enabled": True})
        room = RuntimeRoom(definition.model_copy(update={"revision": 2}), root / "room")
        room.directory.mkdir(mode=0o700)
        parent = room.directory / "overlay"
        parent.mkdir()
        parent.chmod(0o2710)
        endpoint = parent / "speech.sock"
        channel = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        channel.bind(str(endpoint))
        endpoint.chmod(0o660)
        channel.close()  # Retained name after a daemon died without unlinking.
        events, accounts, proofs = [], [], {}
        old_lstat = Path.lstat

        def observed_lstat(path):
            info = old_lstat(path)
            if path == room.directory and info.st_uid != 0:
                # Only the privileged enclosing-dir identity seam is modeled.
                # Directory/socket inodes, modes, and unlink remain real.
                values = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
                info = SimpleNamespace(**{**values, "st_uid": 0})
            return info

        monkeypatch.setattr(Path, "lstat", observed_lstat)

        def account(owner, role, slot):
            accounts.append((owner, role, slot))
            return {"name": f"shiri-{role}-{slot}", "uid": os.getuid() or 1001,
                    "gid": os.getgid() or 1002}

        service._account = account
        foreign_key = f"{uuid4()}:owntone"
        foreign_entry = {"unit": "foreign-room-exact-unit", "invocation_id": uuid4().hex}
        manifest = {"processes": {foreign_key: foreign_entry}}

        def forget(key):
            events.append(("forget", key))
            manifest["processes"].pop(key)

        async def remove(key):
            events.append(("remove-network", key))

        async def stop_saved(entry):
            name = entry["unit"]
            assert entry is entries[name]  # Pass the exact durable identity.
            await proofs[name].stop()

        entries = {}
        service.network = SimpleNamespace(manifest=manifest, forget_process=forget, remove=remove,
                                          interfaces=AsyncMock(return_value=[{"name": "eth0", "eligible": True}]),
                                          create_receiver=AsyncMock(return_value={"interface": "receiver0"}))
        service.runner.stop_saved = stop_saved
        service.sender_users = {"healthy-peer"}
        service.rooms[definition.id] = room

        def add_reserved(name):
            key = f"{definition.id}:{name}"
            entry = {"unit": name, "invocation_id": uuid4().hex, "cgroup_inode": 4242,
                     "boot_id": str(uuid4()), "argv": ["owned-backend"]}
            entries[name] = entry
            manifest["processes"][key] = entry
            proofs[name] = Proof(name, events)
            return proofs[name]

        def tracked(name):
            proof = add_reserved(name)
            room.processes[name] = proof
            return proof

        retired = []
        retirement_failure = [None]

        def retire(path, output_uid, audio_gid):
            assert room.control_lock.locked()
            assert path == parent and endpoint.is_socket()
            assert not any(key.startswith(definition.id + ":") for key in manifest["processes"])
            assert all(proof.stopped for proof in proofs.values())
            assert (output_uid, audio_gid) == (os.getuid() or 1001, os.getgid() or 1002)
            events.append(("retire", str(path)))
            retired.append((room.backend_definition, room.active_local_device))
            if retirement_failure[0] is not None:
                raise retirement_failure[0]
            endpoint.unlink()
            return True

        monkeypatch.setattr(module, "retire_speech_endpoint", retire)
        yield SimpleNamespace(service=service, room=room, definition=definition, endpoint=endpoint,
                              parent=parent, events=events, proofs=proofs, entries=entries,
                              manifest=manifest, foreign_key=foreign_key, foreign_entry=foreign_entry,
                              accounts=accounts, retired=retired, retirement_failure=retirement_failure,
                              add_reserved=add_reserved, tracked=tracked)


def populated(value):
    room = value.room
    value.tracked("owntone")
    value.tracked("audio")
    value.add_reserved("shairport")  # Partial launch: absent in-memory handle.
    room.backend_definition = value.definition
    room.active_local_device = "owned-playback-endpoint"
    room.client = SimpleNamespace(close=AsyncMock())
    room.receiver = {"namespace": "exact-old-receiver"}
    room.launch_generation = uuid4().hex
    room.held_speakers = {("network", "123")}
    value.service.speaker_leases[("network", "123")] = room.desired.id
    return room


@pytest.mark.asyncio
async def test_retirement_follows_tracked_and_reserved_stops_before_backend_state_is_forgotten(recovery):
    room = populated(recovery)
    client = room.client
    await recovery.service._stop_room(room)
    ordered = [event[0] for event in recovery.events]
    assert ordered.count("stop-proved") == 3
    assert ordered.index("retire") > max(index for index, event in enumerate(ordered) if event == "stop-proved")
    assert ordered.index("remove-network") > ordered.index("retire")
    assert recovery.retired == [(recovery.definition, "owned-playback-endpoint")]
    assert recovery.accounts == [(room.desired.id, "output", recovery.definition.slot),
                                 (room.desired.id, "audio", recovery.definition.slot)]
    assert room.backend_definition is None and room.active_local_device is None
    assert room.processes == {} and room.receiver is None and room.client is None
    client.close.assert_awaited_once()
    assert not recovery.endpoint.exists() and not room.held_speakers
    assert recovery.manifest["processes"] == {recovery.foreign_key: recovery.foreign_entry}
    assert recovery.service.sender_users == {"healthy-peer"}


@pytest.mark.asyncio
async def test_pending_reserved_stop_keeps_endpoint_and_old_backend_identity(recovery):
    room = populated(recovery)
    pending = recovery.proofs["shairport"]
    pending.release = asyncio.Event()
    task = asyncio.create_task(recovery.service._stop_room(room))
    try:
        await asyncio.wait_for(pending.entered.wait(), 1)
        assert room.control_lock.locked()
        assert recovery.endpoint.is_socket() and not recovery.retired
        assert room.backend_definition is recovery.definition
        assert f"{room.desired.id}:shairport" in recovery.manifest["processes"]
        assert room.client.close.await_count == 0
        pending.release.set()
        await asyncio.wait_for(task, 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert len(recovery.retired) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("unit", ["audio", "owntone", "shairport"])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_any_failed_exact_stop_refuses_retirement_and_preserves_unreleased_state(recovery, unit, cancelled):
    room = populated(recovery)
    failure = asyncio.CancelledError() if cancelled else RuntimeFailure("Exact unit remains populated")
    recovery.proofs[unit].failure = failure
    with pytest.raises(type(failure)):
        await recovery.service._stop_room(room)
    assert recovery.endpoint.is_socket() and not recovery.retired
    assert room.backend_definition is recovery.definition
    assert room.active_local_device == "owned-playback-endpoint"
    assert room.client.close.await_count == 0 and room.receiver is not None
    assert room.held_speakers == {("network", "123")}
    assert f"{room.desired.id}:{unit}" in recovery.manifest["processes"]
    assert not any(event[0] == "remove-network" for event in recovery.events)
    assert recovery.foreign_key in recovery.manifest["processes"]


@pytest.mark.asyncio
async def test_retirement_failure_does_not_forget_backend_or_release_physical_state(recovery):
    room = populated(recovery)
    pin = SimpleNamespace(close=Mock())
    room.local_pin, room.held_local_node = pin, "exact-device-node"
    recovery.service.local_node_leases[room.held_local_node] = room.desired.id
    recovery.retirement_failure[0] = RuntimeFailure("Unexpected socket identity")
    with pytest.raises(RuntimeFailure, match="Unexpected socket identity"):
        await recovery.service._stop_room(room)
    assert all(proof.stopped for proof in recovery.proofs.values())
    assert room.backend_definition is recovery.definition and room.client.close.await_count == 0
    assert room.local_pin is pin and room.held_local_node == "exact-device-node"
    pin.close.assert_not_called()
    assert room.held_speakers and room.receiver is not None and recovery.endpoint.is_socket()
    assert not any(event[0] == "remove-network" for event in recovery.events)


def start_boundary(value, monkeypatch):
    value.service._ensure_sender = AsyncMock(return_value={"api_host_ip": "10.190.1.1", "api_ip": "10.190.1.2"})
    value.service._resolve_local_pin = Mock(return_value=None)

    def prepare(*_args, **_kwargs):
        assert not value.endpoint.exists()
        assert len(value.retired) == 1
        assert all(proof.stopped for proof in value.proofs.values())
        value.events.append(("prepare-new-backend", None))
        raise RuntimeFailure("Reached new backend preparation")

    renderer = Mock(side_effect=prepare)
    monkeypatch.setattr(module, "backend_configs", renderer)
    return renderer


@pytest.mark.asyncio
@pytest.mark.parametrize("retained_intent", [False, True])
async def test_initial_start_without_memory_retires_socket_before_new_backend_prepare(
    recovery, monkeypatch, retained_intent,
):
    if retained_intent:
        recovery.add_reserved("owntone")
        recovery.add_reserved("audio")
    renderer = start_boundary(recovery, monkeypatch)
    assert recovery.room.processes == {} and recovery.room.receiver is None
    try:
        with pytest.raises(RuntimeFailure, match="Reached new backend preparation"):
            await recovery.service._start_room(recovery.room)
        renderer.assert_called_once()
        names = [event[0] for event in recovery.events]
        assert names.index("retire") < names.index("prepare-new-backend")
        if retained_intent:
            assert names.index("retire") > max(index for index, event in enumerate(names) if event == "stop-proved")
        assert recovery.retired == [(None, None)]
        assert recovery.manifest["processes"] == {recovery.foreign_key: recovery.foreign_entry}
    finally:
        # The artificial post-cleanup boundary leaves a reserved startup slot;
        # the real room loop performs stop cleanup on a failed daemon launch.
        if recovery.room.reserved_slot is not None:
            recovery.service.slot_locks[recovery.room.reserved_slot].release()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_at", ["reserved-stop", "retirement"])
async def test_initial_recovery_failure_never_prepares_or_starts_a_new_daemon(recovery, monkeypatch, failure_at):
    proof = recovery.add_reserved("audio")
    if failure_at == "reserved-stop":
        proof.failure = RuntimeFailure("Recovery refused")
    else:
        recovery.retirement_failure[0] = RuntimeFailure("Recovery refused")
    before = deepcopy(recovery.manifest["processes"])
    renderer = start_boundary(recovery, monkeypatch)
    try:
        with pytest.raises(RuntimeFailure, match="Recovery refused"):
            await recovery.service._start_room(recovery.room)
        renderer.assert_not_called()
        recovery.service._ensure_sender.assert_not_awaited()
        recovery.service.network.create_receiver.assert_not_awaited()
        assert recovery.endpoint.is_socket()
        assert recovery.room.backend_definition is None and not recovery.room.processes
        if failure_at == "reserved-stop":
            assert recovery.manifest["processes"] == before and not recovery.retired
    finally:
        if recovery.room.reserved_slot is not None:
            recovery.service.slot_locks[recovery.room.reserved_slot].release()


@pytest.mark.parametrize("network_present", [False, True])
def test_absent_endpoint_is_harmless_without_account_or_unlink_authority(recovery, network_present):
    recovery.endpoint.unlink()
    recovery.parent.rmdir()
    if not network_present:
        recovery.service.network = None
    recovery.service._retire_speech_endpoint(recovery.room)
    assert not recovery.accounts and not recovery.retired


def test_present_endpoint_without_durable_unit_ownership_is_refused(recovery):
    recovery.service.network = None
    with pytest.raises(RuntimeFailure, match="exact room unit ownership"):
        recovery.service._retire_speech_endpoint(recovery.room)
    assert recovery.endpoint.is_socket() and not recovery.accounts and not recovery.retired


def test_nonprivate_enclosing_room_cannot_authorize_endpoint_retirement(recovery):
    recovery.room.directory.chmod(0o750)
    with pytest.raises(RuntimeFailure, match="root-private room directory"):
        recovery.service._retire_speech_endpoint(recovery.room)
    assert recovery.endpoint.is_socket() and not recovery.accounts and not recovery.retired
