"""Cancellation preserves owned resources and actual production continuations."""
from __future__ import annotations

import ast
import asyncio
import gc
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import dbus_next.aio
from dbus_next import MessageType
from anyio import CancelScope
import httpx
import pytest

from shiri.deadline import bounded
from shiri.domain import Room, SpeakerRef
from shiri.runtime.backend import OwnToneClient
from shiri.runtime.broker import Broker, RuntimeRoom
from shiri.runtime.system import RuntimeFailure
from shiri.runtime.units import OwnedUnit, UnitManager
from shiri.settings import Settings


async def test_pending_child_is_cancelled_and_joined_before_caller_cancellation_returns():
    entered, finished = asyncio.Event(), asyncio.Event()
    async def child():
        entered.set()
        try:
            await asyncio.Future()
        finally:
            finished.set()
    caller = asyncio.create_task(bounded(child(), 1))
    await entered.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert caller.cancelled() and finished.is_set()


@pytest.mark.parametrize("cancellation", ["scope", "repeated_task"])
async def test_caller_cancellation_cannot_interrupt_owned_child_cleanup(cancellation):
    entered, retiring, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    retired = []
    resource = object()
    scope = None

    async def child():
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            retiring.set()
            await release.wait()
            return resource

    async def own():
        nonlocal scope
        if cancellation == "scope":
            with CancelScope() as scope:
                await bounded(child(), 1, abandoned=retired.append)
        else:
            await bounded(child(), 1, abandoned=retired.append)

    caller = asyncio.create_task(own())
    try:
        await entered.wait()
        if cancellation == "scope":
            scope.cancel()
        else:
            caller.cancel()
        await retiring.wait()
        if cancellation == "repeated_task":
            caller.cancel()
        # Give the join and level-triggered cancellation several turns while
        # the resource's actual retirement is still deliberately blocked.
        for _ in range(5):
            await asyncio.sleep(0)
        assert not caller.done() and retired == []
        release.set()
        if cancellation == "scope":
            await caller
        else:
            with pytest.raises(asyncio.CancelledError):
                await caller
        assert retired == [resource]
    finally:
        release.set()
        await asyncio.gather(caller, return_exceptions=True)


async def test_same_turn_completed_result_is_abandoned_on_cancel_before_acceptance():
    read_fd, write_fd = os.pipe()
    retired = []
    caller = None
    def abandon(value):
        assert value == write_fd
        retired.append(value)
        os.close(value)
    async def child():
        asyncio.get_running_loop().call_soon(caller.cancel)
        return write_fd
    caller = asyncio.create_task(bounded(child(), 1, abandoned=abandon))
    try:
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert retired == [write_fd]
        with pytest.raises(OSError):
            os.fstat(write_fd)
    finally:
        os.close(read_fd)
        if not retired:
            os.close(write_fd)


async def test_accepted_done_resource_returns_without_a_final_join_cancellation_window():
    caller = None
    accepted, retired = [], []
    async def child():
        return object()
    task = asyncio.create_task(child())
    task.add_done_callback(lambda _done: asyncio.get_running_loop().call_soon(
        asyncio.get_running_loop().call_soon, caller.cancel))
    async def accept():
        value = await bounded(task, 1, abandoned=retired.append)
        accepted.append(value)
        return value
    caller = asyncio.create_task(accept())
    value = await caller
    await asyncio.sleep(0)
    assert accepted == [value] and not retired and not caller.cancelled()


async def test_timeout_joins_a_child_that_finishes_during_cancel_and_retires_its_result():
    finished, retired = asyncio.Event(), []
    resource = object()
    async def child():
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            finished.set()
            return resource
    with pytest.raises(asyncio.TimeoutError):
        await bounded(child(), .001, abandoned=retired.append)
    assert finished.is_set() and retired == [resource]


async def test_rejected_done_exception_is_consumed_even_without_abandoned_callback():
    loop = asyncio.get_running_loop()
    notices, original = [], loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: notices.append(context))
    try:
        child = loop.create_future()
        child.set_exception(RuntimeError('owned completed error'))
        with pytest.raises(asyncio.TimeoutError):
            await bounded(child, 0)
        del child
        gc.collect()
        await asyncio.sleep(0)
        assert not notices
    finally:
        loop.set_exception_handler(original)


async def test_child_error_remains_primary_and_bluealsa_alias_preserves_calls():
    from shiri.runtime.bluealsa import _bounded
    original = RuntimeError('exact child failure')
    async def failed():
        raise original
    with pytest.raises(RuntimeError) as result:
        await _bounded(failed(), 1)
    assert result.value is original
    assert await _bounded(asyncio.sleep(0, result=7), None) == 7


async def test_cancelled_speaker_selection_does_not_commit_intent_or_continue_mutations(tmp_path):
    definition = Room(id=str(uuid4()), slot=0, name='Private room', airplay_name='Private input',
                      interface='test0', enabled=True,
                      speakers=[SpeakerRef(id='456',name='Original configured speaker',protocol='airplay2')])
    service = Broker(Settings(state_dir=tmp_path/'api', runtime_state_dir=tmp_path/'runtime',
                              runtime_dir=tmp_path/'run', runtime_socket=tmp_path/'unused.sock'))
    state = RuntimeRoom(definition, tmp_path/definition.id, status='running', current_volume=77,
                        timing=(500,600),active_timing=(500,600))
    service.rooms[definition.id] = state
    selected, trace, caller = False, [], None
    async def peer(request):
        nonlocal selected
        trace.append((request.method, request.url.path))
        if request.url.path == '/api/player/shiri-volume-settings':
            settings = json.loads(request.content)
            assert settings == {'volume': 77, 'outputs': [{'id': '123', 'balance_percent': 100}]}
            return httpx.Response(200, json=settings)
        if request.url.path == '/api/player':
            return httpx.Response(200, json={'volume': 77})
        if request.url.path == '/api/outputs/set':
            selected = True  # A cancellation cannot undo an already-applied mutation.
            asyncio.get_running_loop().call_soon(caller.cancel)
            return httpx.Response(200, json={'ok': True})
        if request.url.path == '/api/outputs':
            return httpx.Response(200, json={'outputs': [{'id': 123, 'name': 'Private speaker',
                'type': 'AirPlay 2', 'selected': selected, 'offset_ms': 0, 'balance_percent': 100}]})
        raise AssertionError('Cancelled update continued into '+request.url.path)
    state.client = OwnToneClient('http://not-resolved.invalid', password='in-memory-only',
                                transport=httpx.MockTransport(peer))
    speaker = SpeakerRef(id='123', name='Private speaker', protocol='airplay2')
    caller = asyncio.create_task(service.set_outputs(state, [speaker]))
    try:
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert state.status == 'degraded' and state.wake.is_set()
        assert [item.id for item in state.desired.speakers] == ['456'] and not state.selected_ids
        assert service.speaker_leases[('owntone', '123')] == definition.id
        # Gains are staged under the lease before selection can start sound.
        # Once selection is cancelled, no later mutation/readback may commit it.
        assert trace == [('GET', '/api/outputs'), ('POST', '/api/player/shiri-volume-settings'),
                         ('GET', '/api/player'), ('GET', '/api/outputs'), ('PUT', '/api/outputs/set')]
    finally:
        await state.client.close()


def production_monitor(manager, unit):
    # Use the actual owned production observer, with only the manager peer fake.
    import shiri.runtime.units as units
    tree = ast.parse(Path(units.__file__).read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'UnitManager')
    start = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'start')
    monitor = next(node for node in ast.walk(start) if isinstance(node, ast.AsyncFunctionDef) and node.name == 'monitor')
    environment = {'self': manager, 'unit': unit, 'asyncio': asyncio, 'RuntimeFailure': RuntimeFailure}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[monitor], type_ignores=[])), '<actual-unit-monitor>', 'exec'), environment)
    return environment['monitor']


async def test_owned_unit_stop_joins_monitor_when_its_last_manager_reply_becomes_ready(tmp_path):
    started, reply_ready = asyncio.Event(), asyncio.Event()
    class Bus:
        async def call(self, _message):
            started.set()
            await reply_ready.wait()
            return SimpleNamespace(message_type=MessageType.ERROR,
                error_name='org.freedesktop.systemd1.NoSuchUnit', body=[])
    manager = UnitManager(None, None, bus=Bus())
    async def proved_stopped(_entry):
        reply_ready.set()
        await asyncio.sleep(0)
    manager.stop_saved = proved_stopped
    unit = OwnedUnit('owntone', manager, {'unit': 'shiri-12345678-'+uuid4().hex+'-owntone-'+uuid4().hex+'.service'},
                     tmp_path/'not-created.log')
    unit.monitor = asyncio.create_task(production_monitor(manager, unit)())
    await started.wait()
    stopper = asyncio.create_task(unit.stop())
    try:
        done, _ = await asyncio.wait({stopper}, timeout=.2)
        assert done and unit.monitor.cancelled()
        assert unit.process.returncode == 0 and unit.current == {'ActiveState': 'inactive'}
    finally:
        unit.monitor.cancel()
        await asyncio.gather(unit.monitor, stopper, return_exceptions=True)


@pytest.mark.parametrize('failure', ['cancel', 'error'])
async def test_unaccepted_manager_connection_disposes_only_its_exact_candidate(monkeypatch, failure):
    candidates, caller = [], None
    class Candidate:
        def __init__(self, **_kwargs):
            self.disconnections = 0
            candidates.append(self)
        async def connect(self):
            if failure == 'error':
                raise OSError('in-memory connection refused')
            asyncio.get_running_loop().call_soon(caller.cancel)
            return self
        def disconnect(self):
            self.disconnections += 1
    monkeypatch.setattr(dbus_next.aio, 'MessageBus', Candidate)
    manager = UnitManager(None, None)
    caller = asyncio.create_task(manager.connect())
    with pytest.raises(asyncio.CancelledError if failure == 'cancel' else RuntimeFailure):
        await caller
    assert manager.bus is None and len(candidates) == 1 and candidates[0].disconnections == 1
    existing = object()
    manager.bus = existing
    assert await manager.connect() is existing and len(candidates) == 1


async def test_cancelled_rpc_response_aborts_exact_writer_and_does_not_run_continuation(monkeypatch):
    import shiri.rpc as rpc
    caller, encoded, aborted, continued = None, None, False, False
    class Reader:
        async def readexactly(self, size):
            import struct
            if size == 4:
                return struct.pack('!I', len(encoded))
            asyncio.get_running_loop().call_soon(caller.cancel)
            return encoded
    class Writer:
        def __init__(self):
            self.transport = SimpleNamespace(abort=self.abort)
        def abort(self):
            nonlocal aborted
            aborted = True
        def write(self, data):
            nonlocal encoded
            request = json.loads(data[4:])
            encoded = json.dumps({'id': request['id'], 'ok': True, 'result': {'ok': True}}).encode()
        async def drain(self):
            return
    async def connect(_path):
        return Reader(), Writer()
    monkeypatch.setattr(rpc.asyncio, 'open_unix_connection', connect)
    async def operation():
        nonlocal continued
        await rpc.call_rpc('/not-opened', 'speech', {'action': 'offer'})
        continued = True
    caller = asyncio.create_task(operation())
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert aborted and not continued


@pytest.mark.parametrize('music', [False, True])
async def test_cancelled_readiness_offer_retires_exact_peer_and_cannot_control_successor(music):
    from test_audio import FakePeer
    from test_native_audio import begin
    from test_speech_startup import native_controller
    from shiri.rpc import RpcError
    from shiri.runtime.audio import AudioWorker
    from shiri.runtime.native import NativeHandle
    native, writer, client, overlay = await native_controller()
    if music:
        await native.begin(begin(), NativeHandle(native))
    original_owner = native.actor.snapshot()['owner']
    source_calls = [row for row in client.requests if row[1] == '/api/player/shiri-source']
    peers = []
    def peer_factory():
        peer = FakePeer()
        peers.append(peer)
        return peer
    worker = AudioWorker(native.mixer, native=native, peer_factory=peer_factory)
    client.connect()
    client.first_mix()
    previous, caller, retiring = client.request, None, {}
    async def request(method, path, *, json):
        reply = await previous(method, path, json=json)
        if not retiring and path == '/api/player/shiri-speech-ready' and json['action'] == 'begin':
            retiring.update(session=worker.session, preparation=native.mixer.speech_preparation)
            asyncio.get_running_loop().call_soon(caller.cancel)
        return reply
    client.request = request
    sdp = ('v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n'
           'm=audio 9 UDP/TLS/RTP/SAVPF 111\r\nc=IN IP4 0.0.0.0\r\na=sendonly\r\na=rtpmap:111 opus/48000/2\r\n')
    def offer(session):
        return {'session_id': session, 'request_id': 'private-offer', 'sdp': sdp, 'type': 'offer'}
    caller = asyncio.create_task(worker.offer(offer('cancelled-current'), .2))
    try:
        with pytest.raises(asyncio.CancelledError):
            await caller
        old_session, old_preparation = retiring['session'], retiring['preparation']
        await asyncio.gather(*tuple(worker._disposals), return_exceptions=True)
        assert worker.session is None and old_preparation.retired and peers[0].close_calls == 1
        assert native.actor.snapshot()['owner'] == original_owner
        assert [row for row in client.requests if row[1] == '/api/player/shiri-source'] == source_calls
        assert not old_preparation.push(b'12', 1, .01, lambda *_args: pytest.fail('Retired speech emitted'))
        await worker.offer(offer('exact-successor'), .7)
        successor = native.mixer.speech_preparation
        native.retire_speech(old_session)
        assert native.mixer.speech_preparation is successor and not successor.retired
        with pytest.raises(RpcError, match='Another speech producer'):
            await worker.dispatch('speech', {'action': 'control', 'session_id': 'cancelled-current',
                                            'request_id': 'stale-control', 'duck_gain': .01})
        assert worker.session.duck_gain == .7 and native.actor.snapshot()['owner'] == original_owner
    finally:
        await worker.close()
