"""Actual source-error health -> zone supervision; no process/network actors."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from shiri.domain import Conflict
from shiri.runtime.audio import AudioWorker
from shiri.runtime.broker import RuntimeRoom
from shiri.runtime.native import NativeHandle
from shiri.runtime.system import RuntimeFailure
from shiri.runtime.timing import TimingError
from test_native_audio import begin, controller, pcm
from test_runtime_review import broker, definition, wait_until


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['source_only', 'replacement_start', 'old_unit_stop'])
async def test_persistent_native_source_fault_rebuilds_exact_pair_with_backoff_and_preserves_sibling(tmp_path, fault):
    native, writer, backend = controller()
    healthy_native, healthy_writer, _ = controller()
    replacement = None
    task = None
    try:
        await native.initialize()
        await healthy_native.initialize()
        healthy_handle = NativeHandle(healthy_native)
        healthy_token = await healthy_native.begin(begin(), healthy_handle)
        await healthy_native.message(pcm(healthy_token), healthy_handle)
        backend.bad_ack = True
        failed_handle = NativeHandle(native)
        with pytest.raises(TimingError, match='exact owner'):
            await native.begin(begin(), failed_handle)
        snapshot = native.actor.snapshot()
        assert snapshot['error'] and not snapshot['ready'] and snapshot['owner'] is None
        assert failed_handle.closed and not writer.packets
        failed_worker = AudioWorker(native.mixer, native=native)
        healthy_worker = AudioWorker(healthy_native.mixer, native=healthy_native)
        health = await failed_worker.dispatch('health', {})
        assert health['ready'] is False and health['error'] == snapshot['error']
        assert health['source'] == snapshot
        # A real fault remains sealed until a fresh actor/backend pair replaces it.
        with pytest.raises(Conflict):
            await native.begin(begin(), NativeHandle(native))

        service = broker(tmp_path)
        service.ready = True
        wanted = definition(id=native.actor.snapshot()['zone_id'])
        sibling_wanted = definition(id=healthy_native.actor.snapshot()['zone_id'], slot=1)
        room = RuntimeRoom(wanted, tmp_path/'failed', status='running', applied=wanted)
        sibling = RuntimeRoom(sibling_wanted, tmp_path/'healthy', status='running', applied=sibling_wanted)
        service.rooms = {wanted.id: room, sibling_wanted.id: sibling}
        service.sender_users = set(service.rooms)
        service.sender = {'namespace': 'shared-owned-sender'}
        service.network = SimpleNamespace(healthy=AsyncMock(return_value=True), remove=AsyncMock(),
                                          forget_process=Mock(), manifest={'processes': {}})
        service._stop_reserved_units = AsyncMock()
        service._stop_sender = AsyncMock()
        service._retire_speech_endpoint = Mock()
        service._release_local_pin = AsyncMock()
        old_incarnation = snapshot['incarnation']
        stopped = []
        stop_blocked = fault == 'old_unit_stop'
        class OwnedUnit:
            def __init__(self, name, native=None):
                self.name, self.native, self.alive = name, native, True
                self.process = SimpleNamespace(returncode=None)
            async def stop(self):
                if self.name == 'shairport' and stop_blocked:
                    raise RuntimeFailure('exact old receiver unit has not stopped')
                stopped.append(self.name)
                self.alive = False
                if self.native:
                    await self.native.close()
        old_units = {name: OwnedUnit(name, native if name == 'audio' else None)
                     for name in ('owntone', 'audio', 'shairport')}
        room.processes = dict(old_units)
        sibling_unit = OwnedUnit('healthy-audio', healthy_native)
        sibling.processes = {'audio': sibling_unit}
        def client():
            return SimpleNamespace(request=AsyncMock(return_value={'state':'stop'}), close=AsyncMock())
        room.client, sibling.client = client(), client()
        room.receiver, sibling.receiver = {'namespace': 'failed-old'}, {'namespace': 'healthy-owned'}
        room.held_speakers = {('owntone','failed-speaker')}
        sibling.held_speakers = {('owntone','healthy-speaker')}
        service.speaker_leases = {('owntone','failed-speaker'): wanted.id,
                                 ('owntone','healthy-speaker'): sibling_wanted.id}
        room.reserved_slot = wanted.slot
        await service.slot_locks[wanted.slot].acquire()
        sibling.reserved_slot = sibling_wanted.slot
        await service.slot_locks[sibling_wanted.slot].acquire()
        async def worker_rpc(observed, name, operation, payload, **kwargs):
            assert operation == 'health' and name == 'audio'
            return await (failed_worker if observed is room else healthy_worker).dispatch(operation, payload)
        service._worker_rpc = worker_rpc
        starts = 0
        async def start_exact_pair(observed):
            nonlocal replacement, starts
            assert observed is room
            starts += 1
            assert all(not unit.alive for unit in old_units.values())
            assert wanted.id not in service.speaker_leases.values()
            assert not service.slot_locks[wanted.slot].locked()
            if fault == 'replacement_start' and starts == 1:
                raise RuntimeFailure('replacement backend unavailable')
            replacement, _, _ = controller()
            await replacement.initialize()
            assert replacement.actor.snapshot()['incarnation'] != old_incarnation
            observed.client = client()
            observed.processes = {'owntone': OwnedUnit('new-owntone'),
                                  'audio': OwnedUnit('new-audio', replacement),
                                  'shairport': OwnedUnit('new-shairport')}
            observed.receiver = {'namespace':'failed-new'}
            observed.reserved_slot = wanted.slot
            await service.slot_locks[wanted.slot].acquire()
            service.sender_users.add(wanted.id)
            observed.status, observed.error = 'running', None
        service._start_room = start_exact_pair
        await service._probe_room(sibling)
        assert not sibling.restart_required and not sibling.wake.is_set()
        await service._probe_room(room)
        assert room.restart_required and room.wake.is_set() and room.error == snapshot['error']
        task = asyncio.create_task(service._room_loop(room))
        if fault != 'source_only':
            await wait_until(lambda: room.status == 'error')
            assert room.failures == 1 and room.retry_at >= asyncio.get_running_loop().time() + 1
            assert room.restart_required and not room.wake.is_set()
            await service._probe_room(room)
            assert not room.wake.is_set(), 'Failed recovery must respect its finite backoff'
            if fault == 'old_unit_stop':
                assert starts == 0 and room.processes == old_units
                assert service.speaker_leases[('owntone','failed-speaker')] == wanted.id
                assert service.slot_locks[wanted.slot].locked()
                stop_blocked = False
            room.retry_at = asyncio.get_running_loop().time() - 1
            await service._probe_room(room)
        await wait_until(lambda: replacement is not None and room.status == 'running' and not room.restart_required)
        assert starts == (2 if fault == 'replacement_start' else 1)
        assert stopped[:3] == ['shairport', 'audio', 'owntone']
        assert native.closed and all(not unit.alive for unit in old_units.values())
        assert room.failures == 0 and room.retry_at == 0
        assert service.network.remove.await_args_list[0].args == (f'receiver:{wanted.id}',)
        assert sibling.processes == {'audio': sibling_unit} and sibling_unit.alive
        assert sibling.client.close.await_count == 0 and not sibling.restart_required
        assert service.speaker_leases[('owntone','healthy-speaker')] == sibling_wanted.id
        assert service.slot_locks[sibling_wanted.slot].locked() and sibling_wanted.id in service.sender_users
        assert service._stop_sender.await_count == 0
        assert healthy_native.actor.owns(healthy_handle.token) and len(healthy_writer.packets) == 1
        # Late old-owner writes still fail; fresh pair starts with a fresh identity.
        assert not native.actor.write(healthy_handle.token, lambda: pytest.fail('stale owner leaked'))
    finally:
        if task:
            service._closing = True
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await native.close()
        await healthy_native.close()
        if replacement:
            await replacement.close()
