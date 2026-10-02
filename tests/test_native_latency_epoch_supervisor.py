"""Fresh-epoch controller fences; no VM, root namespace or audio is opened."""
import ast
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from shiri.runtime.system import RuntimeFailure

pytest.importorskip('numpy')
pytest.importorskip('aiortc')
PATH = Path(__file__).parent/'linux/run_native_latency_probe.py'
spec = importlib.util.spec_from_file_location('tested_latency_epoch_supervisor', PATH)
supervisor = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = supervisor
spec.loader.exec_module(supervisor)


class Phase:
    def __init__(self, offset=0, role='idle'):
        self.epoch_id, self.offset_ms, self.phase = str(uuid4()), offset, role

    def receipt(self):
        return {'version': 1, 'id': self.epoch_id, 'offset_ms': self.offset_ms, 'phase': self.phase}


def inner_report(phase, admission, *, at=None):
    now = at or datetime.now(timezone.utc)
    return {'started_at': now.isoformat(), 'finished_at': now.isoformat(), 'mode': 'latency_probe',
            'passed': True, 'cleanup': {'exact_owned_units_stopped': True, 'lan_empty': True},
            'cleanup_errors': [], 'native_lab': deepcopy(admission),
            'latency_epoch': {**phase.receipt(), 'passed': True, 'rows': []}}


def write_report(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def read_report(path):
    return supervisor.read_epoch_report(path, expected_uid=os.getuid())


def test_legacy_run_body_is_byte_identical_to_reviewed_base():
    source = PATH.read_text()
    node = next(node for node in ast.parse(source).body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'run')
    body = '\n'.join(source.splitlines()[node.lineno-1:node.end_lineno])+'\n'
    assert hashlib.sha256(body.encode()).hexdigest() == '19e8868874101a44d2bb624f8c275c12506942af7cbd7493e9bd369c63c3838a'


def test_aware_datetime_window_accepts_equivalent_timezone_without_lexical_comparison():
    phase, admission = Phase(), {'version': 1}
    now = datetime.now(timezone.utc)
    report = inner_report(phase, admission, at=now.astimezone(timezone(timedelta(hours=-5))))
    supervisor.validate_epoch_report(report, phase, admission, now-timedelta(seconds=1), now+timedelta(seconds=1))


@pytest.mark.parametrize('change', ['stale', 'future', 'reverse', 'naive', 'offset_bool', 'offset_float',
                                  'version_bool', 'uuid', 'phase', 'mode', 'failed', 'cleanup', 'empty_cleanup',
                                  'cleanup_errors', 'admission_type', 'failure'])
def test_child_receipt_cannot_relabel_stale_wrong_or_failed_epoch(change):
    phase, admission = Phase(), {'version': 1}
    now = datetime.now(timezone.utc)
    report = inner_report(phase, admission, at=now)
    if change == 'stale':
        report['started_at'] = (now-timedelta(seconds=2)).isoformat()
    elif change == 'future':
        report['finished_at'] = (now+timedelta(seconds=2)).isoformat()
    elif change == 'reverse':
        report['finished_at'] = (now-timedelta(seconds=.1)).isoformat()
    elif change == 'naive':
        report['started_at'] = now.replace(tzinfo=None).isoformat()
    elif change == 'offset_bool':
        report['latency_epoch']['offset_ms'] = False
    elif change == 'offset_float':
        report['latency_epoch']['offset_ms'] = 0.0
    elif change == 'version_bool':
        report['latency_epoch']['version'] = True
    elif change == 'uuid':
        report['latency_epoch']['id'] = str(uuid4())
    elif change == 'phase':
        report['latency_epoch']['phase'] = 'native'
    elif change == 'mode':
        report['mode'] = 'group'
    elif change == 'failed':
        report['latency_epoch']['passed'] = False
    elif change == 'cleanup':
        report['cleanup']['lan_empty'] = 1
    elif change == 'empty_cleanup':
        report['cleanup'] = {}
    elif change == 'cleanup_errors':
        report['cleanup_errors'] = ['surviving child']
    elif change == 'admission_type':
        report['native_lab']['version'] = True
    elif change == 'failure':
        report['failure'] = {'type': 'PCMError'}
    with pytest.raises(RuntimeFailure):
        supervisor.validate_epoch_report(report, phase, admission, now-timedelta(seconds=1), now+timedelta(seconds=1))


def test_held_report_bytes_have_exact_digest_and_inode_proof(tmp_path):
    path = tmp_path/'epoch-result.json'
    value = inner_report(Phase(), {'version': 1})
    write_report(path, value)
    read, proof = read_report(path)
    assert read == value
    assert proof == {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                     'bytes': path.stat().st_size, 'st_dev': path.stat().st_dev, 'st_ino': path.stat().st_ino}


@pytest.mark.parametrize('fault', ['symlink', 'hardlink', 'fifo', 'writable', 'empty', 'oversize', 'duplicate', 'nan'])
def test_report_file_refuses_unsafe_shape_and_ambiguous_json(tmp_path, monkeypatch, fault):
    path = tmp_path/'epoch-result.json'
    if fault == 'symlink':
        source = tmp_path/'source'
        write_report(source, {})
        path.symlink_to(source)
    elif fault == 'hardlink':
        source = tmp_path/'source'
        write_report(source, {})
        os.link(source, path)
    elif fault == 'fifo':
        os.mkfifo(path)
    elif fault == 'writable':
        write_report(path, {})
        path.chmod(0o620)
    elif fault == 'empty':
        path.touch()
        path.chmod(0o600)
    elif fault == 'oversize':
        monkeypatch.setattr(supervisor, 'REPORT_LIMIT', 8)
        write_report(path, {'long': 'x'*20})
    elif fault == 'duplicate':
        path.write_text('{"passed":false,"passed":true}')
        path.chmod(0o600)
    else:
        path.write_text('{"clock":NaN}')
        path.chmod(0o600)
    with pytest.raises((RuntimeFailure, OSError)):
        read_report(path)


@pytest.mark.parametrize('when', ['before_open', 'during_read', 'metadata'])
def test_equally_safe_report_replacement_or_metadata_change_is_rejected(tmp_path, monkeypatch, when):
    path = tmp_path/'epoch-result.json'
    write_report(path, {'receipt': 'original'})
    replacement = tmp_path/'replacement'
    write_report(replacement, {'receipt': 'successor'})
    old_open, old_read = supervisor.os.open, supervisor.os.read
    fired = False
    def changed_open(name, *args, **kwargs):
        nonlocal fired
        if Path(name) == path and when == 'before_open' and not fired:
            fired = True
            os.replace(replacement, path)
        return old_open(name, *args, **kwargs)
    def changed_read(descriptor, size):
        nonlocal fired
        data = old_read(descriptor, size)
        if data and not fired and when != 'before_open':
            fired = True
            if when == 'during_read':
                os.replace(replacement, path)
            else:
                path.chmod(0o620)
        return data
    monkeypatch.setattr(supervisor.os, 'open', changed_open)
    monkeypatch.setattr(supervisor.os, 'read', changed_read)
    with pytest.raises(RuntimeFailure, match='replaced|changed'):
        read_report(path)
    assert fired


class FakeRunner:
    def __init__(self, node_dir):
        self.node_dir, self.commands = node_dir, []
        self.pids, self.links, self.on_delete = '', [{'ifname': 'lo'}], None

    async def run(self, args, *, timeout):
        self.commands.append(list(args))
        if args[:3] == ['ip', 'netns', 'add']:
            (self.node_dir/args[3]).write_bytes(b'namespace')
        if args[:3] == ['ip', 'netns', 'delete']:
            if self.on_delete:
                self.on_delete()
            (self.node_dir/args[3]).unlink()
        return SimpleNamespace(stdout=self.pids if args[:3] == ['ip', 'netns', 'pids'] else '')

    async def json(self, args):
        self.commands.append(list(args))
        return deepcopy(self.links)


class Child:
    def __init__(self, *, returncode=0, pending=False):
        self.returncode = None
        self.event = asyncio.Event()
        self.exit = returncode
        self.terminated, self.killed, self.waits = False, False, 0
        if not pending:
            self.event.set()

    async def wait(self):
        self.waits += 1
        await self.event.wait()
        if self.returncode is None:
            self.returncode = self.exit
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.exit = -15
        self.event.set()

    def kill(self):
        self.killed = True
        self.exit = -9
        self.event.set()


@pytest.fixture
def lab(tmp_path, monkeypatch):
    work, nodes, state = tmp_path/'work', tmp_path/'netns', tmp_path/'state'
    for path in (work, nodes, state):
        path.mkdir(mode=0o700)
    host = tmp_path/'original-netns'
    host.write_bytes(b'original')
    manifest = {'installation_id': str(uuid4()), 'processes': {}, 'networks': {}}
    write_report(state/'ownership.json', manifest)
    admission = {'version': 1, 'boot_id': 'test-boot', 'installation_id': manifest['installation_id']}
    source = {'source.py': {'sha256': 'f'*64}}
    baseline, protected = {'links': ['original-only']}, {'legacy': 'not-applicable'}
    runner = FakeRunner(nodes)
    original_reader = supervisor.read_epoch_report
    monkeypatch.setattr(supervisor, 'read_epoch_report', lambda path: original_reader(path, expected_uid=os.getuid()))
    monkeypatch.setattr(supervisor, 'NETNS_DIRECTORY', nodes)
    monkeypatch.setattr(supervisor, 'ORIGINAL_NAMESPACE', host)
    monkeypatch.setattr(supervisor, 'RESULT', work/'controller.json')
    monkeypatch.setattr(supervisor, 'boot_id', lambda: 'test-boot')
    monkeypatch.setattr(supervisor, 'Runner', lambda: runner)
    monkeypatch.setattr(supervisor, 'source_receipts', lambda project: deepcopy(source))
    monkeypatch.setattr(supervisor.group, 'WORK', work)
    monkeypatch.setattr(supervisor.group, 'STATE', state)
    monkeypatch.setattr(supervisor.group, 'NATIVE_LAB', object())
    monkeypatch.setattr(supervisor.group, 'native_lab_admission', lambda value, mode: deepcopy(admission))
    monkeypatch.setattr(supervisor.group, 'legacy_snapshot', lambda: deepcopy(protected))
    async def host_snapshot():
        return deepcopy(baseline)
    monkeypatch.setattr(supervisor.group.observation.base, 'host_snapshot', host_snapshot)
    monkeypatch.setattr(supervisor.group.observation.base, 'closed_slot', lambda: None)
    monkeypatch.setattr(supervisor.group.isolated_lan, 'namespace_identity',
                        lambda descriptor: (os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino))
    def private_directory(path):
        path.mkdir(mode=0o700)
        info = path.stat()
        return info.st_dev, info.st_ino
    def directory_unchanged(path, identity):
        info = path.lstat()
        if (info.st_dev, info.st_ino) != identity or path.is_symlink() or info.st_mode & 0o077:
            raise RuntimeFailure('Supervisor directory changed')
    monkeypatch.setattr(supervisor, 'private_directory', private_directory)
    monkeypatch.setattr(supervisor, 'directory_unchanged', directory_unchanged)
    children, launches, launch_hooks = [], [], []
    launched = asyncio.Event()
    async def spawn(*args, **kwargs):
        assert '--latency-probe' not in args
        values = list(args)
        phase = Phase(int(values[values.index('--latency-offset-ms')+1]), values[values.index('--latency-phase')+1])
        phase.epoch_id = values[values.index('--latency-epoch-id')+1]
        path = Path(values[values.index('--latency-result')+1])
        launches.append({'args': values, 'phase': phase, 'path': path, 'fds': kwargs['pass_fds']})
        for handle in kwargs['pass_fds']:
            os.fstat(handle)
        child = Child()
        children.append(child)
        report = inner_report(phase, admission)
        if launch_hooks:
            hook = launch_hooks.pop(0)
            hook(child, report, launches[-1])
        write_report(path, report)
        launched.set()
        return child
    monkeypatch.setattr(supervisor.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(supervisor.sys, 'platform', 'linux')
    monkeypatch.setattr(supervisor.os, 'geteuid', lambda: 0)
    return SimpleNamespace(work=work, nodes=nodes, state=state, host=host, manifest=manifest,
        admission=admission, source=source, baseline=baseline, protected=protected, runner=runner,
        children=children, launches=launches, hooks=launch_hooks, launched=launched)


async def epoch(lab, phase=None):
    return await supervisor.run_epoch(phase or Phase(), lab.source, lab.admission,
        lab.manifest['installation_id'], lab.baseline, lab.protected, runner=lab.runner)


@pytest.mark.asyncio
async def test_actual_parent_file_fds_are_held_through_launch_and_exact_cleanup(lab):
    phase = Phase(-2000, 'native')
    result = await epoch(lab, phase)
    assert result['passed'], result
    assert result['inner_report']['latency_epoch']['id'] == phase.epoch_id
    assert all(value is True for value in result['cleanup'].values())
    assert not list(lab.nodes.iterdir())
    assert lab.host.read_bytes() == b'original'
    assert result['report_proof']['path'] == str(lab.launches[0]['path'])
    for handle in lab.launches[0]['fds']:
        with pytest.raises(OSError):
            os.fstat(handle)
    assert json.loads((Path(result['private_directory'])/'supervisor.json').read_text()) == result


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['child_failure', 'wrong_epoch', 'namespace_replaced', 'remaining_pids',
                                  'remaining_link', 'remaining_manifest', 'source_changed', 'lab_changed',
                                  'boot_changed', 'host_changed', 'protected_changed', 'post_cleanup_source_changed'])
async def test_failed_epoch_retains_receipt_and_never_deletes_unproved_parent(lab, monkeypatch, fault):
    def trigger(child, report, launch):
        if fault == 'child_failure':
            child.exit = 1
        elif fault == 'wrong_epoch':
            report['latency_epoch']['id'] = str(uuid4())
        elif fault == 'namespace_replaced':
            node = lab.nodes/launch['args'][launch['args'].index('--parent-namespace')+1]
            replacement = lab.nodes/'replacement'
            replacement.write_bytes(b'foreign')
            os.replace(replacement, node)
        elif fault == 'remaining_pids':
            lab.runner.pids = '123\n'
        elif fault == 'remaining_link':
            lab.runner.links.append({'ifname': 'foreign'})
        elif fault == 'remaining_manifest':
            changed = deepcopy(lab.manifest)
            changed['processes'] = {'owned': {}}
            write_report(lab.state/'ownership.json', changed)
        elif fault == 'source_changed':
            monkeypatch.setattr(supervisor, 'source_receipts', lambda project: {})
        elif fault == 'lab_changed':
            monkeypatch.setattr(supervisor.group, 'native_lab_admission', lambda value, mode: {'version': 2})
        elif fault == 'boot_changed':
            monkeypatch.setattr(supervisor, 'boot_id', lambda: 'other-boot')
        elif fault == 'host_changed':
            lab.baseline['links'].append('changed')
        elif fault == 'protected_changed':
            monkeypatch.setattr(supervisor.group, 'legacy_snapshot', lambda: {'legacy': 'changed'})
        else:
            lab.runner.on_delete = lambda: monkeypatch.setattr(supervisor, 'source_receipts', lambda project: {})
    lab.hooks.append(trigger)
    # Save the admitted baseline separately from the mutable actual host fixture.
    old_baseline = deepcopy(lab.baseline)
    result = await supervisor.run_epoch(Phase(), lab.source, lab.admission,
        lab.manifest['installation_id'], old_baseline, lab.protected, runner=lab.runner)
    assert not result['passed']
    assert 'inner_report' in result
    assert json.loads((Path(result['private_directory'])/'supervisor.json').read_text()) == result
    preserved = {'namespace_replaced', 'remaining_pids', 'remaining_link', 'remaining_manifest',
                 'source_changed', 'lab_changed', 'boot_changed'}
    deletions = [args for args in lab.runner.commands if args[:3] == ['ip', 'netns', 'delete']]
    assert bool(deletions) is (fault not in preserved)
    if fault in preserved:
        assert list(lab.nodes.iterdir())
    assert lab.children[0].returncode is not None


@pytest.mark.asyncio
async def test_cancel_during_live_child_reaps_and_retains_before_reraising(lab):
    def waiting(child, report, launch):
        child.event.clear()
    lab.hooks.append(waiting)
    task = asyncio.create_task(epoch(lab))
    await lab.launched.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    # Task awaits may reconstruct CancelledError; the exact owned durable
    # receipt remains authoritative after cancellation and joined cleanup.
    path = lab.launches[0]['path'].with_name('supervisor.json')
    record, proof = supervisor.read_epoch_report(path)
    assert proof['path'] == str(path)
    assert record['phase'] == lab.launches[0]['phase'].receipt()
    assert record['private_directory'] == str(path.parent)
    assert record['failure']['type'] == 'CancelledError'
    assert lab.children[0].terminated and lab.children[0].returncode == -15
    assert record['cleanup']['direct_child_reaped']
    assert not list(lab.nodes.iterdir())
    assert not record['passed'] and 'inner_report' in record


@pytest.mark.asyncio
async def test_failed_primary_child_and_cleanup_failure_both_survive(lab):
    def fail(child, report, launch):
        report['passed'] = False
        lab.runner.pids = 'surviving-peer\n'
    lab.hooks.append(fail)
    result = await epoch(lab)
    assert result['failure']['message'].startswith('Epoch report failed')
    assert any('PIDs remain' in item['message'] for item in result['cleanup_errors'])
    assert result['inner_report']['passed'] is False
    assert list(lab.nodes.iterdir())


def matrix_helper(monkeypatch):
    phases = tuple(Phase(offset, role) for offset in (-2000, 0, 2000) for role in ('idle', 'native'))
    aggregate_calls = []
    def aggregate(reports, expected):
        aggregate_calls.append(deepcopy(reports))
        assert len(reports) == len(expected) == 6
        return {'passed': True, 'rows': list(range(9)), 'measured_signal_timeline_cleanup_passed': True,
                'speech_latency_performance_passed': False,
                'speech_latency_performance_status': 'pending_declared_and_characterized_software_budget',
                'cold_utterance_completeness_passed': False,
                'cold_utterance_completeness_status': 'pending_finite_opus_prefix_tail_reference'}
    monkeypatch.setattr(supervisor, 'load_matrix_helper', lambda: SimpleNamespace(phases=lambda: phases,
        aggregate_epoch_reports=aggregate))
    return phases, aggregate_calls


@pytest.mark.asyncio
async def test_matrix_launches_six_fresh_names_results_and_only_then_aggregates(lab, monkeypatch):
    phases, aggregate_calls = matrix_helper(monkeypatch)
    def assert_previous_clean(child, report, launch):
        assert len(list(lab.nodes.iterdir())) == 1
    lab.hooks.extend([assert_previous_clean]*6)
    code = await supervisor.run_matrix()
    result = json.loads(supervisor.RESULT.read_text())
    assert code == 0 and result['passed']
    assert len(result['epochs']) == 6 and len(aggregate_calls) == 1
    assert [item['phase'] for item in result['epochs']] == [phase.receipt() for phase in phases]
    assert len({item['namespace'] for item in result['epochs']}) == 6
    assert len({str(item['path']) for item in lab.launches}) == 6
    assert all(item['path'].name == 'epoch-result.json' and item['path'].parent.parent == lab.work for item in lab.launches)
    assert result['deadlines']['matrix_seconds'] == 3000 and result['deadlines']['external_watchdog_seconds'] == 3180
    assert not list(lab.nodes.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['child', 'cleanup', 'aggregate'])
async def test_matrix_never_promotes_partial_or_failed_cleanup(lab, monkeypatch, fault):
    phases, aggregate_calls = matrix_helper(monkeypatch)
    if fault == 'aggregate':
        def reject(reports, expected):
            raise RuntimeFailure('Exact H/B or final PCM evidence did not pass')
        monkeypatch.setattr(supervisor, 'load_matrix_helper', lambda: SimpleNamespace(phases=lambda: phases,
            aggregate_epoch_reports=reject))
    else:
        lab.hooks.append(lambda child, report, launch: None)
        def fail(child, report, launch):
            if fault == 'child':
                report['latency_epoch']['passed'] = False
            else:
                lab.runner.links.append({'ifname': 'remaining'})
        lab.hooks.append(fail)
    code = await supervisor.run_matrix()
    result = json.loads(supervisor.RESULT.read_text())
    assert code == 1 and not result['passed'] and 'latency_matrix' not in result
    assert len(result['epochs']) == (6 if fault == 'aggregate' else 2)
    assert not aggregate_calls
    if fault == 'cleanup':
        assert list(lab.nodes.iterdir())


@pytest.mark.asyncio
async def test_matrix_cancellation_retains_owned_epoch_without_advancing(lab, monkeypatch):
    _, aggregate_calls = matrix_helper(monkeypatch)
    lab.hooks.append(lambda child, report, launch: child.event.clear())
    task = asyncio.create_task(supervisor.run_matrix())
    await lab.launched.wait()
    task.cancel()
    assert await task == 1
    result = json.loads(supervisor.RESULT.read_text())
    assert len(result['epochs']) == 1 and result['failure']['type'] == 'CancelledError'
    assert result['epochs'][0]['cleanup']['direct_child_reaped']
    assert not aggregate_calls and not list(lab.nodes.iterdir())


@pytest.mark.asyncio
async def test_direct_child_ignoring_term_is_killed_and_reaped(tmp_path, monkeypatch):
    # A real local child, with no namespace/VM access, exercises unreaped-PID ownership.
    import signal
    ready = tmp_path/'ready'
    original = asyncio.create_subprocess_exec
    child = await original(sys.executable, '-c',
        'import signal,time,pathlib;signal.signal(signal.SIGTERM,signal.SIG_IGN);'
        f'pathlib.Path({str(ready)!r}).touch();time.sleep(60)',
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        for _ in range(100):
            if ready.exists():
                break
            await asyncio.sleep(.01)
        assert ready.exists()
        await supervisor.stop_child(child, grace=.01, kill_timeout=1)
        assert child.returncode == -signal.SIGKILL
    finally:
        if child.returncode is None:
            child.kill()
        await child.wait()


@pytest.mark.asyncio
async def test_child_deadline_retains_report_and_reaps_without_success(lab, monkeypatch):
    monkeypatch.setattr(supervisor, 'CHILD_SECONDS', .01)
    lab.hooks.append(lambda child, report, launch: child.event.clear())
    result = await epoch(lab)
    assert not result['passed']
    assert result['failure']['type'] == 'RuntimeFailure'
    assert '480 seconds' in result['failure']['message']
    assert result['inner_report']['passed'] is True  # Historical child claim cannot override a timeout.
    assert result['cleanup']['direct_child_reaped'] and lab.children[0].terminated
    assert not list(lab.nodes.iterdir())


@pytest.mark.asyncio
async def test_matrix_deadline_stops_current_child_and_keeps_completed_epoch(lab, monkeypatch):
    _, aggregate_calls = matrix_helper(monkeypatch)
    monkeypatch.setattr(supervisor, 'MATRIX_SECONDS', .025)
    lab.hooks.append(lambda child, report, launch: None)
    lab.hooks.append(lambda child, report, launch: child.event.clear())
    assert await supervisor.run_matrix() == 1
    result = json.loads(supervisor.RESULT.read_text())
    assert len(result['epochs']) == 2 and result['epochs'][0]['passed']
    assert result['epochs'][1]['failure']['type'] == 'CancelledError'
    assert result['failure']['type'] == 'TimeoutError'
    assert len(lab.children) == 2 and lab.children[1].terminated
    assert not aggregate_calls and not list(lab.nodes.iterdir())


@pytest.mark.asyncio
async def test_cleanup_deadline_cancels_owned_commands_closes_fds_and_preserves_parent(lab, monkeypatch):
    monkeypatch.setattr(supervisor, 'EPOCH_CLEANUP_SECONDS', .015)
    called = asyncio.Event()
    old_json = lab.runner.json
    async def stalled(args):
        called.set()
        await asyncio.Event().wait()
        return await old_json(args)
    monkeypatch.setattr(lab.runner, 'json', stalled)
    result = await epoch(lab)
    assert called.is_set() and not result['passed']
    assert result['cleanup_errors'][0]['stage'] == 'cleanup_deadline_or_cancellation'
    assert result['cleanup_errors'][0]['type'] == 'TimeoutError'
    assert list(lab.nodes.iterdir())
    for handle in lab.launches[0]['fds']:
        with pytest.raises(OSError):
            os.fstat(handle)
    assert lab.children[0].returncode is not None


@pytest.mark.asyncio
async def test_original_namespace_replacement_prevents_cleanup_and_preserves_primary(lab):
    def replace_host(child, report, launch):
        new = lab.host.with_name('new-original')
        new.write_bytes(b'foreign')
        os.replace(new, lab.host)
        report['latency_epoch']['passed'] = False
    lab.hooks.append(replace_host)
    result = await epoch(lab)
    assert not result['passed'] and result['failure']['message'].startswith('Epoch report failed')
    assert any('Original host namespace identity changed' in item['message'] for item in result['cleanup_errors'])
    assert list(lab.nodes.iterdir()) and lab.host.read_bytes() == b'foreign'


@pytest.mark.asyncio
async def test_namespace_creation_error_preserves_unadmitted_object_and_fds(lab, monkeypatch):
    original_run = lab.runner.run
    async def fail_after_creation(args, *, timeout):
        result = await original_run(args, timeout=timeout)
        if args[:3] == ['ip', 'netns', 'add']:
            raise RuntimeFailure('Injected netns add partial failure')
        return result
    monkeypatch.setattr(lab.runner, 'run', fail_after_creation)
    result = await epoch(lab)
    assert result['failure']['message'] == 'Injected netns add partial failure'
    assert not result['passed'] and not lab.children
    assert any('Unadmitted namespace' in item['message'] for item in result['cleanup_errors'])
    assert list(lab.nodes.iterdir())
    assert not any(args[:3] == ['ip', 'netns', 'delete'] for args in lab.runner.commands)


@pytest.mark.asyncio
async def test_cancellation_during_spawn_still_obtains_and_reaps_exact_direct_child(lab, monkeypatch):
    old_spawn = supervisor.asyncio.create_subprocess_exec
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed_spawn(*args, **kwargs):
        entered.set()
        await release.wait()
        return await old_spawn(*args, **kwargs)
    monkeypatch.setattr(supervisor.asyncio, 'create_subprocess_exec', delayed_spawn)
    task = asyncio.create_task(epoch(lab))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert len(lab.children) == 1 and lab.children[0].returncode is not None
    path = lab.launches[0]['path'].with_name('supervisor.json')
    record, proof = supervisor.read_epoch_report(path)
    assert proof['path'] == str(path)
    assert record['phase'] == lab.launches[0]['phase'].receipt()
    assert record['private_directory'] == str(path.parent)
    assert record['failure']['type'] == 'CancelledError'
    assert record['cleanup']['direct_child_reaped']
    assert not record['passed'] and 'inner_report' in record
    assert not list(lab.nodes.iterdir())


@pytest.mark.asyncio
async def test_cleanup_metadata_lost_root_never_overwrites_replaced_supervisor_directory(lab, monkeypatch):
    def replace_root(child, report, launch):
        root = launch['path'].parent
        old = root.with_name(root.name+'-old')
        root.rename(old)
        root.mkdir(mode=0o700)
        write_report(root/'epoch-result.json', report)
        (root/'supervisor.json').write_text('foreign receipt')
    lab.hooks.append(replace_root)
    result = await epoch(lab)
    assert not result['passed'] and 'Supervisor directory changed' in result['failure']['message']
    assert (Path(result['private_directory'])/'supervisor.json').read_text() == 'foreign receipt'
    assert any(item['stage'] == 'retained_supervisor_receipt' for item in result['cleanup_errors'])


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['missing', 'duplicate_uuid', 'wrong_phase', 'offset_bool'])
async def test_invalid_phase_plan_never_creates_a_parent_or_child(lab, monkeypatch, fault):
    phases = list(matrix_helper(monkeypatch)[0])
    if fault == 'missing':
        phases.pop()
    elif fault == 'duplicate_uuid':
        phases[1].epoch_id = phases[0].epoch_id
    elif fault == 'wrong_phase':
        phases[1].phase = 'other'
    else:
        phases[2].offset_ms = False
    monkeypatch.setattr(supervisor, 'load_matrix_helper', lambda: SimpleNamespace(phases=lambda: phases))
    assert await supervisor.run_matrix() == 1
    result = json.loads(supervisor.RESULT.read_text())
    assert not result['passed'] and not result['epochs']
    assert not lab.launches and not lab.runner.commands


def test_wrong_report_uid_is_refused_without_mutating_bytes(tmp_path):
    path = tmp_path/'epoch-result.json'
    write_report(path, {'passed': True})
    before = path.read_bytes(), path.stat().st_ino, path.stat().st_mode
    with pytest.raises(RuntimeFailure, match='protected'):
        supervisor.read_epoch_report(path, expected_uid=os.getuid()+1)
    assert (path.read_bytes(), path.stat().st_ino, path.stat().st_mode) == before


@pytest.mark.asyncio
async def test_successful_integrity_matrix_retains_pending_performance_without_claiming_minimum(lab, monkeypatch):
    matrix_helper(monkeypatch)
    assert await supervisor.run_matrix() == 0
    result = json.loads(supervisor.RESULT.read_text())
    assert result['passed'] and result['measured_signal_timeline_cleanup_passed']
    assert result['speech_latency_performance_passed'] is False
    assert result['speech_latency_performance_status'] == 'pending_declared_and_characterized_software_budget'
    assert result['cold_utterance_completeness_passed'] is False
    assert result['cold_utterance_completeness_status'] == 'pending_finite_opus_prefix_tail_reference'
    for name in ('measured_signal_timeline_cleanup_passed', 'speech_latency_performance_passed',
                 'speech_latency_performance_status', 'cold_utterance_completeness_passed',
                 'cold_utterance_completeness_status'):
        assert type(result[name]) is type(result['latency_matrix'][name])
        assert result[name] == result['latency_matrix'][name]


@pytest.mark.asyncio
async def test_incomplete_child_cannot_promote_measurement_or_performance_labels(lab, monkeypatch):
    matrix_helper(monkeypatch)
    lab.hooks.append(lambda child, report, launch: report.update(passed=False))
    assert await supervisor.run_matrix() == 1
    result = json.loads(supervisor.RESULT.read_text())
    assert result['measured_signal_timeline_cleanup_passed'] is False
    assert result['speech_latency_performance_passed'] is False
    assert result['speech_latency_performance_status'] == 'not_completed'
    assert result['cold_utterance_completeness_passed'] is False
    assert result['cold_utterance_completeness_status'] == 'not_completed'


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['measurement_missing', 'measurement_false', 'performance_missing',
                                  'performance_integer', 'status_missing', 'status_empty'])
async def test_aggregate_without_exact_scope_labels_is_not_promoted(lab, monkeypatch, fault):
    phases, _ = matrix_helper(monkeypatch)
    labels = {'passed': True, 'measured_signal_timeline_cleanup_passed': True,
              'speech_latency_performance_passed': False,
              'speech_latency_performance_status': 'pending_declared_and_characterized_software_budget',
              'cold_utterance_completeness_passed': False,
              'cold_utterance_completeness_status': 'pending_finite_opus_prefix_tail_reference'}
    if fault == 'measurement_missing':
        labels.pop('measured_signal_timeline_cleanup_passed')
    elif fault == 'measurement_false':
        labels['measured_signal_timeline_cleanup_passed'] = False
    elif fault == 'performance_missing':
        labels.pop('speech_latency_performance_passed')
    elif fault == 'performance_integer':
        labels['speech_latency_performance_passed'] = 0
    elif fault == 'status_missing':
        labels.pop('speech_latency_performance_status')
    else:
        labels['speech_latency_performance_status'] = ''
    monkeypatch.setattr(supervisor, 'load_matrix_helper', lambda: SimpleNamespace(phases=lambda: phases,
        aggregate_epoch_reports=lambda reports, expected: labels))
    assert await supervisor.run_matrix() == 1
    result = json.loads(supervisor.RESULT.read_text())
    assert len(result['epochs']) == 6 and all(epoch['passed'] for epoch in result['epochs'])
    assert not result['passed'] and result['measured_signal_timeline_cleanup_passed'] is False
    assert result['speech_latency_performance_status'] == 'not_completed'


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['passed_missing', 'passed_integer', 'status_missing', 'status_empty'])
async def test_aggregate_without_exact_cold_completeness_labels_is_not_promoted(lab, monkeypatch, fault):
    phases, _ = matrix_helper(monkeypatch)
    labels = {'passed': True, 'measured_signal_timeline_cleanup_passed': True,
              'speech_latency_performance_passed': False,
              'speech_latency_performance_status': 'pending_declared_and_characterized_software_budget',
              'cold_utterance_completeness_passed': False,
              'cold_utterance_completeness_status': 'pending_finite_opus_prefix_tail_reference'}
    if fault == 'passed_missing':
        labels.pop('cold_utterance_completeness_passed')
    elif fault == 'passed_integer':
        labels['cold_utterance_completeness_passed'] = 0
    elif fault == 'status_missing':
        labels.pop('cold_utterance_completeness_status')
    else:
        labels['cold_utterance_completeness_status'] = ''
    monkeypatch.setattr(supervisor, 'load_matrix_helper', lambda: SimpleNamespace(phases=lambda: phases,
        aggregate_epoch_reports=lambda reports, expected: labels))
    assert await supervisor.run_matrix() == 1
    result = json.loads(supervisor.RESULT.read_text())
    assert len(result['epochs']) == 6 and all(epoch['passed'] for epoch in result['epochs'])
    assert not result['passed'] and result['measured_signal_timeline_cleanup_passed'] is False
    assert result['cold_utterance_completeness_status'] == 'not_completed'
