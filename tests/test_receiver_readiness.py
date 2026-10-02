"""Exact process/namespace/socket startup evidence; no live daemon is mutated."""
import asyncio
import ipaddress
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from shiri.runtime import receiver_probe as probe
from shiri.runtime import receiver_readiness as readiness
from shiri.runtime.system import RuntimeFailure

PID, BIRTH, UID, GID = 4242, '123', 1001, 1002
BOOT = '95688d55-d767-4548-9ffb-086072b54f24'
INTERFACE, ADDRESS = 'sr123456789abc', '192.168.1.51'
HEADER = ' sl local_address rem_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode\n'


def row(inode=789, uid=UID, address='0.0.0.0', port=7000, state='0A'):
    packed = ipaddress.ip_address(address).packed
    encoded = ''.join(f'{int.from_bytes(packed[i:i+4], os.sys.byteorder):08X}' for i in range(0, len(packed), 4))
    remote = '0' * len(encoded)
    return f'0: {encoded}:{port:04X} {remote}:0000 {state} 0:0 00:0 0 {uid} 0 {inode}\n'


def proc_view(tmp_path):
    namespace = tmp_path/'namespace'
    namespace.write_text('held fake namespace')
    directory = tmp_path/'proc'/str(PID)
    for child in ['ns', 'net', 'fd']:
        (directory/child).mkdir(parents=True, exist_ok=True)
    (directory/'ns/net').symlink_to(namespace)
    (directory/'stat').write_text(f'{PID} (comm with ) parentheses) '+' '.join(['S', *(['0']*18), BIRTH, '0']))
    (directory/'net/dev').write_text('Inter-| Receive | Transmit\n face |bytes\n lo: 0\n '+INTERFACE+': 0\n')
    (directory/'net/tcp').write_text(HEADER+row())
    (directory/'net/tcp6').write_text(HEADER)
    (directory/'fd/9').symlink_to('socket:[789]')
    info = namespace.stat()
    return directory, (info.st_dev, info.st_ino)


def sample(directory, namespace):
    return probe.sample(PID, BIRTH, namespace, UID, ADDRESS, INTERFACE, root=directory.parent)


@pytest.mark.parametrize('address', ['0.0.0.0', ADDRESS])
def test_exact_process_owns_lan_listener(tmp_path, address):
    directory, namespace = proc_view(tmp_path)
    (directory/'net/tcp').write_text(HEADER+row(address=address))
    assert sample(directory, namespace) == [
        {'family': 'ipv4', 'address': address, 'port': 7000, 'uid': UID, 'inode': 789}]


@pytest.mark.parametrize('field', ['foreign_fd', 'wrong_port', 'established', 'ipv6_only'])
def test_namespace_listener_without_exact_mainpid_endpoint_is_not_ready(tmp_path, field):
    directory, namespace = proc_view(tmp_path)
    if field == 'foreign_fd':
        (directory/'fd/9').unlink()
        (directory/'fd/9').symlink_to('socket:[999]')
    elif field == 'wrong_port':
        (directory/'net/tcp').write_text(HEADER+row(port=7001))
    elif field == 'established':
        (directory/'net/tcp').write_text(HEADER+row(state='01'))
    else:
        (directory/'net/tcp').write_text(HEADER)
        (directory/'net/tcp6').write_text(HEADER+row(address='::'))
    assert sample(directory, namespace) == []


@pytest.mark.parametrize('changes', [{'uid': UID+1}, {'address': '127.0.0.1'}, {'address': '192.168.1.52'}])
def test_owned_listener_with_wrong_credentials_or_address_fails(tmp_path, changes):
    directory, namespace = proc_view(tmp_path)
    (directory/'net/tcp').write_text(HEADER+row(**changes))
    with pytest.raises(probe.ProbeFailure, match='credentials or bind address'):
        sample(directory, namespace)


def test_socket_closed_between_descriptor_snapshots_is_not_ready(tmp_path, monkeypatch):
    directory, namespace = proc_view(tmp_path)
    monkeypatch.setattr(probe, 'sockets', Mock(side_effect=[{789}, set()]))
    assert sample(directory, namespace) == []


@pytest.mark.parametrize('change', ['birth', 'namespace', 'interface'])
def test_pid_reuse_namespace_replacement_and_foreign_lan_are_rejected(tmp_path, change):
    directory, namespace = proc_view(tmp_path)
    if change == 'birth':
        (directory/'stat').write_text(f'{PID} (new process) '+' '.join(['S', *(['0']*18), '124', '0']))
    elif change == 'namespace':
        other = tmp_path/'different-namespace'
        other.write_text('foreign')
        (directory/'ns/net').unlink()
        (directory/'ns/net').symlink_to(other)
    else:
        with (directory/'net/dev').open('a') as stream:
            stream.write(' unrelated0: 0\n')
    with pytest.raises(probe.ProbeFailure, match='replaced|namespace|interface'):
        sample(directory, namespace)


def test_namespace_cannot_change_during_listener_observation(tmp_path, monkeypatch):
    directory, namespace = proc_view(tmp_path)
    original = probe.sockets
    def changed(*args, **kwargs):
        found = original(*args, **kwargs)
        other = tmp_path/'replacement'
        other.write_text('foreign')
        (directory/'ns/net').unlink()
        (directory/'ns/net').symlink_to(other)
        return found
    monkeypatch.setattr(probe, 'sockets', changed)
    with pytest.raises(probe.ProbeFailure, match='namespace'):
        sample(directory, namespace)


def test_tcp_and_proc_size_bounds_fail_closed(tmp_path, monkeypatch):
    with pytest.raises(probe.ProbeFailure, match='malformed'):
        probe.listeners(HEADER+'truncated row\n', 'ipv4')
    path = tmp_path/'oversized'
    path.write_text('x'*32)
    monkeypatch.setattr(probe, 'MAX_PROC_BYTES', 16)
    with pytest.raises(probe.ProbeFailure, match='bound'):
        probe.bounded_text(path)


def test_ready_socket_after_monotonic_deadline_is_rejected(tmp_path, monkeypatch):
    namespace = tmp_path/'namespace'
    namespace.write_text('held namespace')
    fd = os.open(namespace, os.O_RDONLY)
    try:
        args = SimpleNamespace(pid=PID, birth=BIRTH, timeout=1, interface=INTERFACE, address=ADDRESS,
                               pidfd=99, namespace_fd=fd, uid=UID, gid=GID, parent=100)
        monkeypatch.setattr(probe, 'bounded_text', lambda _p: f'Pid:\t{PID}\n')
        monkeypatch.setattr(probe, 'confine', Mock())
        monkeypatch.setattr(probe.select, 'poll', lambda: SimpleNamespace(register=Mock(), poll=lambda _t: []))
        monkeypatch.setattr(probe, 'sample', Mock(return_value=[{'family': 'ipv4'}]))
        monkeypatch.setattr(probe.time, 'monotonic', Mock(side_effect=[0, .1, 1.1]))
        with pytest.raises(probe.ProbeFailure, match='missed its startup deadline'):
            probe.run(args)
    finally:
        os.close(fd)


@pytest.fixture
def parent_admission(tmp_path, monkeypatch):
    namespace = tmp_path/'held-namespace'
    namespace.write_text('held namespace')
    group = tmp_path/'held-cgroup'
    group.write_text('held cgroup')
    info = namespace.stat()
    namespace_name = 'shiri_rx_b265eb7d_123456789abc'
    expected = {'name': 'shairport', 'unit': 'exact.service', 'boot_id': BOOT,
                'namespace': '/run/netns/'+namespace_name, 'user': 'shiri-receiver-7',
                'group': 'shiri-receiver-7', 'cgroup_inode': group.stat().st_ino,
                'invocation_id': 'a'*32}
    current = {'MainPID': PID, 'ActiveState': 'active', 'invocation_id': 'a'*32}
    def verify(entry, actual):
        if entry['invocation_id'] != actual['invocation_id']:
            raise RuntimeFailure('invocation changed')
    manager = SimpleNamespace(inspect=AsyncMock(side_effect=lambda _name: current.copy()),
                              verify=Mock(side_effect=verify), open_cgroup=lambda _entry: os.open(group, os.O_RDONLY))
    unit = SimpleNamespace(identity=lambda: expected.copy(), process=SimpleNamespace(pid=PID), manager=manager, alive=True)
    receiver = {'namespace': namespace_name, 'inode': info.st_ino, 'boot_id': BOOT, 'ip': ADDRESS, 'interface': INTERFACE}
    account = {'name': 'shiri-receiver-7', 'uid': UID, 'gid': GID}
    observation = {'ready': True, 'pid': PID, 'birth': BIRTH, 'uid': UID, 'gid': GID,
                   'namespace_dev': info.st_dev, 'namespace_inode': info.st_ino,
                   'listeners': [{'family': 'ipv4', 'address': '0.0.0.0', 'port': 7000, 'uid': UID, 'inode': 789}]}
    process = SimpleNamespace(returncode=0, kill=Mock(), wait=AsyncMock(return_value=0))
    process.communicate = AsyncMock(side_effect=lambda: (json.dumps(observation).encode(), b''))
    spawn = AsyncMock(return_value=process)
    original_open, original_stat = os.open, Path.stat
    monkeypatch.setattr(readiness.os, 'open', lambda path, *args, **kwargs:
                        original_open(namespace if str(path).startswith('/run/netns/') else path, *args, **kwargs))
    monkeypatch.setattr(Path, 'stat', lambda path, *args, **kwargs:
                        original_stat(namespace if str(path).startswith('/run/netns/') else path, *args, **kwargs))
    monkeypatch.setattr(readiness.os.path, 'ismount', lambda _path: True)
    monkeypatch.setattr(readiness.os, 'pidfd_open', lambda _pid: original_open(namespace, os.O_RDONLY), raising=False)
    monkeypatch.setattr(readiness.select, 'poll', lambda: SimpleNamespace(register=Mock(), poll=lambda _t: []))
    monkeypatch.setattr(readiness, 'boot_id', lambda: BOOT)
    monkeypatch.setattr(readiness, 'process_birth', lambda _pid: BIRTH)
    monkeypatch.setattr(readiness, 'trusted_file', lambda path, **_kwargs: path)
    monkeypatch.setattr(readiness.asyncio, 'create_subprocess_exec', spawn)
    return SimpleNamespace(unit=unit, receiver=receiver, account=account, observation=observation,
                           current=current, process=process, spawn=spawn, expected=expected, namespace=namespace)


@pytest.mark.asyncio
async def test_parent_accepts_only_exact_unit_namespace_and_owned_listener(parent_admission):
    a = parent_admission
    assert await readiness.wait_receiver_ready(a.unit, a.receiver, a.account) == a.observation
    args, kwargs = a.spawn.call_args
    assert args[1:3] == ('-I', '-S')
    assert len(kwargs['pass_fds']) == 2 and kwargs['env'] == {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C.UTF-8'}
    assert a.unit.manager.verify.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['pid', 'invocation', 'birth', 'namespace'])
async def test_parent_rejects_replacement_after_observer_reply(parent_admission, monkeypatch, change):
    a = parent_admission
    async def reply():
        if change == 'pid':
            a.current['MainPID'] += 1
        elif change == 'invocation':
            a.current['invocation_id'] = 'b'*32
        elif change == 'birth':
            monkeypatch.setattr(readiness, 'process_birth', lambda _pid: '124')
        else:
            monkeypatch.setattr(readiness.Path, 'stat', Mock(return_value=SimpleNamespace(st_dev=1, st_ino=2)))
        return json.dumps(a.observation).encode(), b''
    a.process.communicate.side_effect = reply
    with pytest.raises(RuntimeFailure, match='changed'):
        await readiness.wait_receiver_ready(a.unit, a.receiver, a.account)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['pid', 'namespace', 'loopback', 'foreign_uid', 'ipv6_only'])
async def test_parent_rejects_forged_or_insufficient_observer_fields(parent_admission, change):
    a = parent_admission
    if change == 'pid':
        a.observation['pid'] += 1
    elif change == 'namespace':
        a.observation['namespace_inode'] += 1
    elif change == 'loopback':
        a.observation['listeners'][0]['address'] = '127.0.0.1'
    elif change == 'foreign_uid':
        a.observation['listeners'][0]['uid'] += 1
    else:
        a.observation['listeners'][0].update(family='ipv6', address='::')
    with pytest.raises(RuntimeFailure, match='another process|LAN listener|IPv4 endpoint'):
        await readiness.wait_receiver_ready(a.unit, a.receiver, a.account)


@pytest.mark.asyncio
async def test_dead_child_fails_without_waiting_for_deadline(parent_admission):
    a = parent_admission
    a.process.returncode = 1
    a.observation.clear()
    a.observation.update(ready=False, error='Receiver exited during startup readiness')
    with pytest.raises(RuntimeFailure, match='exited'):
        await readiness.wait_receiver_ready(a.unit, a.receiver, a.account)


@pytest.mark.asyncio
async def test_total_deadline_includes_manager_inspection(parent_admission):
    a = parent_admission
    async def slow(_name):
        await asyncio.sleep(10)
    a.unit.manager.inspect.side_effect = slow
    with pytest.raises(RuntimeFailure, match='total deadline'):
        await readiness.wait_receiver_ready(a.unit, a.receiver, a.account, timeout=.01)
    a.spawn.assert_not_awaited()
