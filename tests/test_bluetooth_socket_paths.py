"""Short syscall addresses retain full room/launch identity without live daemons."""
from __future__ import annotations

import asyncio
import os
import socket
import stat
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from shiri.domain import Room
from shiri.rpc import call_rpc, serve_rpc
from shiri.runtime.bluealsa import HandoffServer
from shiri.runtime.broker import Broker, RuntimeRoom
from shiri.runtime.system import RuntimeFailure
from shiri.runtime.unix_directory import PinnedUnixDirectory
from shiri.settings import Settings


def pin(path, mode=0o700):
    path.mkdir(parents=True, mode=mode)
    path.chmod(mode)
    return PinnedUnixDirectory(path, uid=os.geteuid(), gid=os.getegid(), mode=mode)


def room(tmp_path):
    definition = Room(id='b6786543-7eb2-443d-83b1-65b984123a76', slot=7, name='A',
        airplay_name='A', interface='eth0', enabled=True,
        local_audio_device='bluealsa:DEV=AA:BB:CC:DD:EE:01,PROFILE=a2dp')
    broker = Broker(Settings(runtime_state_dir=tmp_path, runtime_dir=tmp_path/'run'))
    state = RuntimeRoom(definition, tmp_path/'rooms'/definition.id)
    state.launch_generation = uuid4().hex
    return broker, state


def test_default_room_and_full_generation_paths_reproduce_original_address_limit():
    identifier, generation = 'b6786543-7eb2-443d-83b1-65b984123a76', 'a'*32
    root = Settings().runtime_state_dir/'rooms'/identifier
    assert len(os.fsencode(root/'bridge-handoff'/generation/'pcm.sock')) >= 108
    assert len(os.fsencode(root/'bridge-state'/generation/'bridge.sock')) >= 108
    # Worker paths and final exact-socket mount destination already fit.
    assert len(os.fsencode('/run/shiri-worker/handoff/pcm.sock')) < 108
    assert len(os.fsencode('/run/shiri-worker/state/bridge.sock')) < 108
    assert len(os.fsencode('/run/shiri-worker/bridge/final-pcm.sock')) < 108


def test_each_address_lease_duplicates_inode_and_close_cannot_reuse_it(tmp_path):
    original = pin(tmp_path/'old')
    other = pin(tmp_path/'new')
    lifecycle_fd = original.descriptor
    try:
        with original.address('bridge.sock') as address:
            operation_fd = int(address.parent.name)
            assert operation_fd != lifecycle_fd and not os.get_inheritable(operation_fd)
            assert len(os.fsencode(address)) < 108
            expected = (os.fstat(operation_fd).st_dev, os.fstat(operation_fd).st_ino)
            original.close()
            # Force reuse, including on a host whose allocator chooses another
            # descriptor. The in-flight operation still owns its old duplicate.
            os.dup2(other.descriptor, lifecycle_fd, inheritable=False)
            assert os.fstat(lifecycle_fd).st_ino == os.fstat(other.descriptor).st_ino
            assert (os.fstat(operation_fd).st_dev, os.fstat(operation_fd).st_ino) == expected
        with pytest.raises(OSError):
            os.fstat(operation_fd)
        with pytest.raises(RuntimeFailure, match='retired'):
            with original.address('bridge.sock'):
                pytest.fail('retired generation acquired an address')
    finally:
        original.close()
        os.close(lifecycle_fd)
        other.close()


@pytest.mark.parametrize('change', ['replacement', 'mode', 'symlink'])
def test_new_leases_refuse_a_changed_canonical_directory(tmp_path, change):
    directory = pin(tmp_path/'original')
    try:
        if change == 'replacement':
            directory.path.rename(tmp_path/'old')
            directory.path.mkdir(mode=0o700)
        elif change == 'symlink':
            directory.path.rename(tmp_path/'old')
            directory.path.symlink_to(tmp_path/'old', target_is_directory=True)
        else:
            directory.path.chmod(0o750)
        with pytest.raises(RuntimeFailure, match='replaced|credentials'):
            with directory.address('bridge.sock'):
                pytest.fail('changed directory acquired an address')
    finally:
        directory.close()


@pytest.mark.parametrize('name', ['../bridge.sock', '/bridge.sock', 'bridge.sock/child', 'bad\x00.sock', '', 'x'*40+'.sock'])
def test_alias_never_admits_a_path_or_unbounded_name(tmp_path, name):
    directory = pin(tmp_path/'private')
    try:
        with pytest.raises(RuntimeFailure, match='basename'):
            with directory.address(name):
                pytest.fail('unexpected basename admitted')
    finally:
        directory.close()


@pytest.mark.skipif(sys.platform != 'linux', reason='Real Linux proc directory aliases and SEQPACKET')
def test_real_handoff_binds_beyond_canonical_limit_and_connects_same_inode(tmp_path):
    parent = tmp_path/'var/lib/shiri-runtime/rooms'/'b6786543-7eb2-443d-83b1-65b984123a76'/'bridge-handoff'/('a'*32)
    parent.mkdir(parents=True, mode=0o700)
    parent.chmod(0o700)
    path = parent/'pcm.sock'
    assert len(os.fsencode(path)) >= 108
    preimage = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        with pytest.raises(OSError, match='path too long'):
            preimage.bind(str(path))
    finally:
        preimage.close()
    server = HandoffServer(path, object(), os.getegid(), root_uid=os.geteuid())
    parent_fd = server.parent.descriptor
    client = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    client.settimeout(1)
    try:
        assert path.lstat().st_ino == server.inode and stat.S_ISSOCK(path.lstat().st_mode)
        with server.parent.address('pcm.sock') as address:
            client.connect(str(address))
        peer, _ = server.listener.accept()
        try:
            client.send(b'exact room')
            assert peer.recv(64) == b'exact room'
        finally:
            peer.close()
    finally:
        client.close()
        server.close()
    assert not path.exists()
    with pytest.raises(OSError):
        os.fstat(parent_fd)


@pytest.mark.skipif(sys.platform != 'linux', reason='Real Linux proc directory aliases and SEQPACKET')
@pytest.mark.parametrize('change', ['parent', 'socket', 'mode', 'symlink'])
def test_handoff_retirement_refuses_replacement_and_always_closes_exact_fds(tmp_path, change):
    parent = tmp_path/'handoff'
    parent.mkdir(mode=0o700)
    path = parent/'pcm.sock'
    server = HandoffServer(path, object(), os.getegid(), root_uid=os.geteuid())
    parent_fd = server.parent.descriptor
    replacement = None
    if change == 'parent':
        parent.rename(tmp_path/'old')
        parent.mkdir(mode=0o700)
        (parent/'pcm.sock').write_bytes(b'foreign')
    elif change == 'socket':
        path.unlink()
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        replacement.bind(str(path))
    elif change == 'symlink':
        path.unlink()
        target = tmp_path/'target'
        target.write_bytes(b'foreign')
        path.symlink_to(target)
    else:
        path.chmod(0o600)
    try:
        with pytest.raises(RuntimeFailure, match='replaced|credentials'):
            server.close()
        assert path.exists() or path.is_symlink()
        assert server.listener.fileno() == -1 and server.parent.descriptor is None
        with pytest.raises(OSError):
            os.fstat(parent_fd)
    finally:
        if replacement is not None:
            replacement.close()


async def test_rpc_lease_is_retained_to_completion_and_cancelled_lease_is_closed(tmp_path, monkeypatch):
    broker, state = room(tmp_path)
    directory = pin(state.directory/'bridge-state'/state.launch_generation)
    state.bluetooth_rpc_directory = directory
    started, finish = asyncio.Event(), asyncio.Event()
    operation_fds = []
    async def request(address, operation, payload, **kwargs):
        assert operation == 'health' and payload == {} and kwargs == {'timeout': 1}
        descriptor = int(address.parent.name)
        operation_fds.append(descriptor)
        assert os.fstat(descriptor).st_ino == directory.expected[1]
        started.set()
        await finish.wait()
        return {'ready': True}
    monkeypatch.setattr('shiri.runtime.broker.call_rpc', request)
    try:
        pending = asyncio.create_task(broker._worker_rpc(state, 'bluetooth-output', 'health', {}, timeout=1))
        await asyncio.wait_for(started.wait(), .5)
        assert os.fstat(operation_fds[-1]).st_ino == directory.expected[1]
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        with pytest.raises(OSError):
            os.fstat(operation_fds[-1])
        started.clear()
        pending = asyncio.create_task(broker._worker_rpc(state, 'bluetooth-output', 'health', {}, timeout=1))
        await asyncio.wait_for(started.wait(), .5)
        finish.set()
        assert await pending == {'ready': True}
        with pytest.raises(OSError):
            os.fstat(operation_fds[-1])
        assert os.fstat(directory.descriptor).st_ino == directory.expected[1]
    finally:
        directory.close()


async def test_inflight_rpc_keeps_old_inode_after_lifecycle_fd_reuse(tmp_path, monkeypatch):
    broker, state = room(tmp_path)
    old = pin(state.directory/'bridge-state'/state.launch_generation)
    new_generation = uuid4().hex
    new = pin(state.directory/'bridge-state'/new_generation)
    state.bluetooth_rpc_directory = old
    lifecycle_fd, old_inode = old.descriptor, old.expected[1]
    entered, release = asyncio.Event(), asyncio.Event()
    observed = []
    async def delayed(address, operation, payload, **kwargs):
        descriptor = int(address.parent.name)
        entered.set()
        await release.wait()
        observed.append(os.fstat(descriptor).st_ino)
        return {'ready': True}
    monkeypatch.setattr('shiri.runtime.broker.call_rpc', delayed)
    try:
        pending = asyncio.create_task(broker._worker_rpc(state, 'bluetooth-output', 'health', {}, timeout=1))
        await asyncio.wait_for(entered.wait(), .5)
        old.close()
        os.dup2(new.descriptor, lifecycle_fd, inheritable=False)
        state.bluetooth_rpc_directory, state.launch_generation = new, new_generation
        release.set()
        with pytest.raises(RuntimeFailure, match='retired'):
            await pending
        assert observed == [old_inode] and old_inode != new.expected[1]
    finally:
        old.close()
        os.close(lifecycle_fd)
        new.close()


@pytest.mark.skipif(sys.platform != 'linux', reason='Actual Linux proc alias RPC connection and peer credentials')
async def test_delayed_rpc_cannot_follow_lifecycle_fd_reuse_to_successor(tmp_path, monkeypatch):
    broker, state = room(tmp_path)
    old_generation = state.launch_generation
    old = pin(state.directory/'bridge-state'/old_generation)
    new_generation = uuid4().hex
    new = pin(state.directory/'bridge-state'/new_generation)
    state.bluetooth_rpc_directory = old
    receipts = []
    servers = []
    async def handler_old(operation, payload):
        receipts.append('old')
        return {'ready': True, 'scope': 'old'}
    async def handler_new(operation, payload):
        receipts.append('new')
        return {'ready': True, 'scope': 'new'}
    for directory, handler in [(old, handler_old), (new, handler_new)]:
        with directory.address('bridge.sock') as address:
            servers.append(await serve_rpc(address, handler, allowed_uids={os.geteuid()}))
    lifecycle_fd, entered, release = old.descriptor, asyncio.Event(), asyncio.Event()
    async def delayed(address, operation, payload, **kwargs):
        entered.set()
        await release.wait()
        return await call_rpc(address, operation, payload, **kwargs)
    monkeypatch.setattr('shiri.runtime.broker.call_rpc', delayed)
    try:
        pending = asyncio.create_task(broker._worker_rpc(state, 'bluetooth-output', 'health', {}, timeout=1))
        await asyncio.wait_for(entered.wait(), .5)
        old.close()
        os.dup2(new.descriptor, lifecycle_fd, inheritable=False)
        state.bluetooth_rpc_directory, state.launch_generation = new, new_generation
        release.set()
        with pytest.raises(RuntimeFailure, match='retired'):
            await pending
        assert receipts == ['old']
        assert (await broker._worker_rpc(state, 'bluetooth-output', 'health', {}, timeout=1))['scope'] == 'new'
        assert receipts == ['old', 'new']
    finally:
        for server in servers:
            server.close()
            await server.wait_closed()
        old.close()
        os.close(lifecycle_fd)
        new.close()


async def test_rpc_rejects_mismatched_or_retired_generation_before_connect(tmp_path, monkeypatch):
    broker, state = room(tmp_path)
    directory = pin(state.directory/'bridge-state'/state.launch_generation)
    state.bluetooth_rpc_directory = directory
    request = AsyncMock()
    monkeypatch.setattr('shiri.runtime.broker.call_rpc', request)
    try:
        state.launch_generation = uuid4().hex
        with pytest.raises(RuntimeFailure, match='exact live room launch'):
            await broker._worker_rpc(state, 'bluetooth-output', 'health', {})
        request.assert_not_awaited()
        state.launch_generation = directory.path.name
        directory.close()
        with pytest.raises(RuntimeFailure, match='retired'):
            await broker._worker_rpc(state, 'bluetooth-output', 'health', {})
        request.assert_not_awaited()
    finally:
        directory.close()


@pytest.mark.parametrize('generation', [None, True, 'a'*31, 'g'*32, '0'*32, 'a'*8+'-'+'a'*23])
async def test_malformed_rpc_launch_is_refused_before_connect(tmp_path, monkeypatch, generation):
    broker, state = room(tmp_path)
    directory = pin(state.directory/'bridge-state'/state.launch_generation)
    state.bluetooth_rpc_directory = directory
    request = AsyncMock()
    monkeypatch.setattr('shiri.runtime.broker.call_rpc', request)
    try:
        state.launch_generation = generation
        with pytest.raises(RuntimeFailure, match='exact live room launch'):
            await broker._worker_rpc(state, 'bluetooth-output', 'health', {})
        request.assert_not_awaited()
    finally:
        directory.close()


@pytest.mark.parametrize('failure', ['tracked', 'reserved', None])
async def test_directory_retirement_waits_for_tracked_and_reserved_units(tmp_path, monkeypatch, failure):
    broker, state = room(tmp_path)
    events = []
    holder = SimpleNamespace(close=lambda: events.append('directory_close'))
    state.bluetooth_rpc_directory = holder
    async def stopped():
        events.append('tracked_stop')
        if failure == 'tracked':
            raise RuntimeFailure('tracked unit failed')
    process = SimpleNamespace(stop=stopped)
    state.processes['bluetooth-output'] = process
    async def reserved(identifier):
        events.append('reserved_stop')
        if failure == 'reserved':
            raise RuntimeFailure('reserved unit failed')
    broker._stop_reserved_units = reserved
    broker._retire_speech_endpoint = Mock()
    broker.network = SimpleNamespace(forget_process=Mock(), remove=AsyncMock())
    broker._release_local_pin = AsyncMock()
    broker._release_speakers = AsyncMock()
    broker._stop_sender = AsyncMock()
    if failure:
        with pytest.raises(RuntimeFailure, match='failed'):
            await broker._stop_room(state)
        assert state.bluetooth_rpc_directory is holder and 'directory_close' not in events
    else:
        await broker._stop_room(state)
        assert events[:3] == ['tracked_stop', 'reserved_stop', 'directory_close']
        assert state.bluetooth_rpc_directory is None


async def test_non_bluetooth_rpc_preserves_original_explicit_audio_path(tmp_path, monkeypatch):
    broker, state = room(tmp_path)
    request = AsyncMock(return_value={'ready': True})
    monkeypatch.setattr('shiri.runtime.broker.call_rpc', request)
    audio = tmp_path/'explicit-audio.sock'
    assert await broker._worker_rpc(state, 'audio', 'health', {}, timeout=1, socket=audio) == {'ready': True}
    request.assert_awaited_once_with(audio, 'health', {}, timeout=1)


@pytest.mark.skipif(sys.platform != 'linux', reason='Actual Linux O_PATH socket inode rollback')
@pytest.mark.parametrize('stage', ['chown', 'chmod', 'verify', 'listen', 'setblocking'])
def test_handoff_constructor_rolls_back_exact_created_socket_on_failure(tmp_path, monkeypatch, stage):
    parent = tmp_path/'handoff'
    parent.mkdir(mode=0o700)
    path = parent/'pcm.sock'
    real_socket, real_open = socket.socket, os.open
    directory_type = PinnedUnixDirectory
    sockets, pins, inode_fds = [], [], []
    class TrackedDirectory(directory_type):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.calls = 0
            pins.append(self)
        def verify(self):
            self.calls += 1
            if stage == 'verify' and self.calls == 2:
                raise OSError('injected verify failure')
            return super().verify()
    class Listener:
        def __init__(self, *args):
            self.real = real_socket(*args)
            sockets.append(self.real)
        def __getattr__(self, name):
            return getattr(self.real, name)
        def listen(self, count):
            if stage == 'listen':
                raise OSError('injected listen failure')
            self.real.listen(count)
        def setblocking(self, value):
            if stage == 'setblocking':
                raise OSError('injected setblocking failure')
            self.real.setblocking(value)
    def tracked_open(name, flags, *args, **kwargs):
        descriptor = real_open(name, flags, *args, **kwargs)
        if name == 'pcm.sock' and flags & os.O_PATH:
            inode_fds.append(descriptor)
        return descriptor
    monkeypatch.setattr('shiri.runtime.bluealsa.PinnedUnixDirectory', TrackedDirectory)
    monkeypatch.setattr('shiri.runtime.bluealsa.socket.socket', Listener)
    monkeypatch.setattr('shiri.runtime.bluealsa.os.open', tracked_open)
    if stage in {'chmod', 'chown'}:
        def fail(*_args, **_kwargs):
            raise OSError(f'injected {stage} failure')
        monkeypatch.setattr(f'shiri.runtime.bluealsa.os.{stage}', fail)
    with pytest.raises(OSError, match=f'injected {stage} failure'):
        HandoffServer(path, object(), os.getegid(), root_uid=os.geteuid())
    assert not path.exists() and not path.is_symlink()
    assert len(sockets) == len(pins) == len(inode_fds) == 1
    assert sockets[0].fileno() == -1 and pins[0].descriptor is None
    with pytest.raises(OSError):
        os.fstat(inode_fds[0])


@pytest.mark.skipif(sys.platform != 'linux', reason='Actual Linux O_PATH socket inode rollback')
@pytest.mark.parametrize('replacement', ['parent', 'socket'])
def test_constructor_rollback_preserves_replacements_and_primary_exception(tmp_path, monkeypatch, replacement):
    parent = tmp_path/'handoff'
    parent.mkdir(mode=0o700)
    path = parent/'pcm.sock'
    real_socket, directory_type = socket.socket, PinnedUnixDirectory
    sockets, pins = [], []
    class TrackedDirectory(directory_type):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            pins.append(self)
    class Listener:
        def __init__(self, *args):
            self.real = real_socket(*args)
            sockets.append(self.real)
        def __getattr__(self, name):
            return getattr(self.real, name)
        def listen(self, _count):
            if replacement == 'parent':
                parent.rename(tmp_path/'old')
                parent.mkdir(mode=0o700)
            else:
                path.unlink()
            path.write_bytes(b'foreign replacement')
            raise OSError('original listen failure')
    monkeypatch.setattr('shiri.runtime.bluealsa.PinnedUnixDirectory', TrackedDirectory)
    monkeypatch.setattr('shiri.runtime.bluealsa.socket.socket', Listener)
    with pytest.raises(OSError, match='original listen failure') as raised:
        HandoffServer(path, object(), os.getegid(), root_uid=os.geteuid())
    assert isinstance(raised.value.__cause__, RuntimeFailure)
    assert path.read_bytes() == b'foreign replacement'
    assert sockets[0].fileno() == -1 and pins[0].descriptor is None
