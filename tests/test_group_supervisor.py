"""Run the supervisor's real child/cleanup paths without kernel resources."""
import ast
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import os
import sys

import pytest

from shiri.runtime.system import RuntimeFailure, atomic_json

SOURCE = Path(__file__).resolve().parents[1] / 'tests/linux/run_native_grouping.py'


def functions(*names, namespace):
    tree = ast.parse(SOURCE.read_text())
    body = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name in names]
    assert len(body) == len(names)
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), str(SOURCE), 'exec'), namespace)
    return namespace


class Child:
    def __init__(self, *, cooperative=False, unkillable=False):
        self.returncode, self.cooperative, self.unkillable = None, cooperative, unkillable
        self.calls, self.exited = [], asyncio.Event()

    def terminate(self):
        self.calls.append('terminate')
        if self.cooperative:
            self.returncode = -15
            self.exited.set()

    def kill(self):
        self.calls.append('kill')
        if not self.unkillable:
            self.returncode = -9
            self.exited.set()

    async def wait(self):
        await self.exited.wait()
        self.calls.append('reaped')
        return self.returncode


@pytest.mark.asyncio
@pytest.mark.parametrize('cooperative', [True, False])
async def test_direct_child_ignoring_term_is_killed_and_reaped_within_bounded_grace(cooperative):
    namespace = functions('stop_child', namespace={'asyncio': asyncio})
    child = Child(cooperative=cooperative)
    await asyncio.wait_for(namespace['stop_child'](child, grace=.001, kill_timeout=.01), .1)
    assert child.calls == (['terminate', 'reaped'] if cooperative else ['terminate', 'kill', 'reaped'])
    assert child.returncode in {-15, -9}


@pytest.mark.asyncio
async def test_unreapable_child_times_out_instead_of_blocking_namespace_recovery_forever():
    namespace = functions('stop_child', namespace={'asyncio': asyncio})
    child = Child(unkillable=True)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(namespace['stop_child'](child, grace=.001, kill_timeout=.001), .1)
    assert child.calls == ['terminate', 'kill']


@pytest.mark.asyncio
async def test_already_reaped_child_is_never_signalled():
    namespace = functions('stop_child', namespace={'asyncio': asyncio})
    child = Child()
    child.returncode = 0
    await namespace['stop_child'](child, grace=.001)
    assert child.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('remaining_owner', [False, True])
async def test_real_finally_reaps_resistant_child_and_preserves_namespace_until_all_room_ownership_is_empty(
        tmp_path, remaining_owner):
    tree = ast.parse(SOURCE.read_text())
    run = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'run')
    guarded = next(node for node in run.body if isinstance(node, ast.Try))
    # Only the maintained finally body executes. Every process/namespace
    # creation path is absent, and restoring the old repeated TERM implementation
    # causes this regression to time out before the child can be reaped.
    arguments = ['result', 'process', 'runner', 'root', 'node', 'descriptor', 'original_descriptor',
                 'inode', 'baseline', 'legacy', 'timed_out']
    fragment = ast.AsyncFunctionDef(name='cleanup', args=ast.arguments(
        posonlyargs=[], args=[ast.arg(arg=name) for name in arguments],
        kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=ast.parse('errors, log, namespace = [], None, "owned-parent"').body + guarded.finalbody
        + [ast.Return(ast.Name(id='result', ctx=ast.Load()))], decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[fragment], type_ignores=[]))
    calls, child = [], Child()
    state, root = tmp_path/'state', tmp_path/'private'
    state.mkdir()
    root.mkdir()
    (state/'ownership.json').write_text(json.dumps({'installation_id': 'exact-installation',
        'processes': {'room:output': {'pid': 99}} if remaining_owner else {}, 'networks': {}}))
    node = tmp_path/'namespace'
    node.write_text('fake namespace inode')
    info = node.stat()

    class Runner:
        async def run(self, command, **_kwargs):
            calls.append(command)
            return SimpleNamespace(stdout='')

        async def json(self, _command):
            return [{'ifname': 'lo'}]

    async def host_snapshot():
        calls.append('host snapshot')
        return baseline

    namespace = functions('stop_child', namespace={'asyncio': asyncio})
    real_stop = namespace['stop_child']

    async def bounded_stop(process, **kwargs):
        # Verify the production grace selection, then use a tiny real timeout
        # for the fake child. Its cancellation/kill/reap semantics are unchanged.
        assert kwargs == {'grace': 70}
        await real_stop(process, grace=.001, kill_timeout=.01)

    baseline, legacy = 'host baseline', 'legacy baseline'
    namespace.update(stop_child=bounded_stop, datetime=datetime, timezone=timezone,
        json=json, boot_id=lambda: 'boot-A', RuntimeFailure=RuntimeFailure, atomic_json=atomic_json,
        RESULT=tmp_path/'result.json', group=SimpleNamespace(STATE=state, observation=SimpleNamespace(base=SimpleNamespace(
            host_snapshot=host_snapshot, closed_slot=lambda: calls.append('slot closed'))),
            legacy_snapshot=lambda: legacy),
        os=SimpleNamespace(fstat=lambda _fd: info, close=lambda _fd: calls.append('held FD closed')))
    exec(compile(module, str(SOURCE), 'exec'), namespace)
    result = await asyncio.wait_for(namespace['cleanup'](
        {'cleanup': {}, 'boot_id': 'boot-A', 'installation_id': 'exact-installation', 'harness_passed': True},
        child, Runner(), root, node, 31, 32, (info.st_dev, info.st_ino), baseline, legacy, True), .1)
    assert child.calls == ['terminate', 'kill', 'reaped']
    deletions = [call for call in calls if isinstance(call, list) and 'delete' in call]
    if remaining_owner:
        assert deletions == []
        assert result['passed'] is False
        assert result['cleanup_errors'] == ['parent cleanup: RuntimeFailure: Candidate ownership remains; preserve parent for exact recovery']
    else:
        assert deletions == [['ip', 'netns', 'delete', 'owned-parent']]
        assert result['passed'] is True
    assert result['cleanup']['original_host_and_legacy_preserved'] is True
    assert calls.count('held FD closed') == 2
    assert json.loads((tmp_path/'result.json').read_text())['passed'] is result['passed']


@pytest.mark.asyncio
@pytest.mark.parametrize('replaced_before_launch', [None, 'parent', 'original'])
async def test_supervisor_launches_through_inherited_exact_namespace_fd_without_remounting_sysfs(
        tmp_path, replaced_before_launch):
    work, state, nodes = tmp_path/'work', tmp_path/'state', tmp_path/'netns'
    work.mkdir()
    state.mkdir()
    nodes.mkdir()
    original = tmp_path/'original-namespace'
    original.write_text('original host namespace')
    (state/'ownership.json').write_text(json.dumps({'installation_id': 'exact-lab-installation',
                                                   'processes': {}, 'networks': {}}))
    inner, calls, launches, inherited = tmp_path/'inner.json', [], [], []

    def paths(value):
        return {'/run/netns': nodes, '/proc/self/ns/net': original}.get(str(value), Path(value))

    class Runner:
        async def run(self, command, **_kwargs):
            calls.append(command)
            if command[:3] == ['ip', 'netns', 'add']:
                (nodes/command[3]).write_text('owned namespace inode')
            if command[:3] == ['ip', 'netns', 'exec'] and replaced_before_launch:
                node = nodes/command[3] if replaced_before_launch == 'parent' else original
                node.unlink()
                node.write_text('replacement namespace inode')
            if command[:3] == ['ip', 'netns', 'delete']:
                (nodes/command[3]).unlink()
            return SimpleNamespace(stdout='')

        async def json(self, _command):
            return [{'ifname': 'lo'}]

    async def launch(*arguments, **options):
        launches.append((arguments, options))
        assert arguments[0] == '/usr/bin/nsenter' and arguments[2] == '--'
        descriptor = int(arguments[1].removeprefix('--net=/proc/self/fd/'))
        original_descriptor = int(arguments[arguments.index('--original-netns-fd')+1])
        assert options['pass_fds'] == (descriptor, original_descriptor)
        assert descriptor != original_descriptor
        inherited.extend(options['pass_fds'])
        node = nodes/arguments[arguments.index('--parent-namespace')+1]
        held, current = os.fstat(descriptor), node.stat()
        assert (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino)
        assert held.st_ino != original.stat().st_ino
        host = os.fstat(original_descriptor)
        assert (host.st_dev, host.st_ino) == (original.stat().st_dev, original.stat().st_ino)
        # A new inner report comes from this exact launch, avoiding stale-pass
        # reports while no child, systemd unit or network resource is created.
        atomic_json(inner, {'started_at': datetime.now(timezone.utc).isoformat(), 'passed': True, 'native_lab': {'fixture': 'exact'}})
        child = Child(cooperative=True)
        child.returncode = 0
        child.exited.set()
        return child

    async def snapshot():
        return 'unchanged original host'

    def directory(path, mode=0o700):
        path.mkdir(parents=True, exist_ok=True, mode=mode)

    fake_asyncio = SimpleNamespace(wait_for=asyncio.wait_for, TimeoutError=asyncio.TimeoutError,
                                   create_subprocess_exec=launch, subprocess=asyncio.subprocess)
    namespace = functions('run', 'stop_child', namespace={'asyncio': fake_asyncio,
        'datetime': datetime, 'timezone': timezone, 'json': json, 'Path': paths, 'uuid4': uuid4,
        'os': SimpleNamespace(geteuid=lambda: 0, open=lambda path, *args: os.open(paths(path), *args),
            fstat=os.fstat, close=os.close,
            environ={}, O_RDONLY=os.O_RDONLY, O_NOFOLLOW=os.O_NOFOLLOW, O_CLOEXEC=os.O_CLOEXEC),
        'sys': SimpleNamespace(platform='linux', executable=sys.executable), 'Runner': Runner,
        'RuntimeFailure': RuntimeFailure, 'atomic_json': atomic_json, 'boot_id': lambda: 'boot-A',
        'root_directory': directory, 'RESULT': tmp_path/'result.json', 'HERE': SOURCE,
        'group': SimpleNamespace(NATIVE_LAB=object(), native_lab_admission=lambda *_: {'fixture': 'exact'},
            WORK=work, STATE=state, RESULT=inner, PROJECT=tmp_path,
            isolated_lan=SimpleNamespace(namespace_identity=lambda fd: (os.fstat(fd).st_dev, os.fstat(fd).st_ino)),
            observation=SimpleNamespace(base=SimpleNamespace(closed_slot=lambda: None, host_snapshot=snapshot)),
            legacy_snapshot=lambda: 'unchanged legacy')})
    outcome = await namespace['run']()
    result = json.loads((tmp_path/'result.json').read_text())
    if replaced_before_launch:
        assert outcome == 1 and launches == []
        assert 'changed before launch' in result['failure']['message']
        if replaced_before_launch == 'parent':
            assert not any(command[:3] == ['ip', 'netns', 'delete'] for command in calls)
            assert list(nodes.iterdir())  # The unowned replacement is preserved.
    else:
        assert outcome == 0 and result['passed'] is True and len(launches) == 1
        assert list(nodes.iterdir()) == []
        assert result['cleanup'] == {'parent_namespace_deleted': True, 'original_host_and_legacy_preserved': True, 'native_lab_preserved': True}
        assert result['original_namespace_identity'] == {'st_dev': original.stat().st_dev, 'st_ino': original.stat().st_ino}
        for descriptor in inherited:
            with pytest.raises(OSError):
                os.fstat(descriptor)
