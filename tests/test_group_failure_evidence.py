"""Failures retain exact private diagnostics before disposable rooms vanish."""
# Bounded fixture file observations occur between awaits.
# ruff: noqa: ASYNC240

import ast
import asyncio
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shiri.runtime import units
from shiri.runtime.system import RuntimeFailure

SPEC = importlib.util.spec_from_file_location('group_failure_evidence_test',
    Path(__file__).parent/'linux/group_failure_evidence.py')
evidence = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evidence)
ROOM = 'b6786543-7eb2-443d-83b1-65b984123a76'
BOOT = '49d00e63-ed5f-45a0-bc99-63cde4f1f15b'
INVOCATION = '1'*32


@pytest.fixture(autouse=True)
def root_view(monkeypatch):
    """Preserve real type/mode/inodes; emulate only root ownership on macOS."""
    original_fstat, original_lstat = os.fstat, Path.lstat
    def owned(info):
        values = list(info)
        values[4] = 0
        return os.stat_result(values)
    monkeypatch.setattr(evidence.os, 'fstat', lambda descriptor: owned(original_fstat(descriptor)))
    monkeypatch.setattr(Path, 'lstat', lambda path: owned(original_lstat(path)))
    monkeypatch.setattr(units, 'boot_id', lambda: BOOT)
    monkeypatch.setattr(evidence, 'boot_id', lambda: BOOT)


def source(tmp_path, *, producer=True):
    root = tmp_path.resolve()/'rooms'/ROOM
    logs = root/'native-validation/logs' if producer else root/'logs'
    logs.mkdir(parents=True)
    path = logs/'shairport.log'
    path.write_text('before\nRuntimeError: Native timing ingress failed: TimingError\n')
    path.chmod(0o600)
    service = units.UnitSpec(units.new_unit('b265eb7d', ROOM, 'shairport'), 'shairport',
                             'shiri-receiver-6', 'shiri-receiver-6', ('/usr/bin/python3.10',))
    entry = service.intent()
    entry.update(invocation_id=INVOCATION, control_group='/system.slice/'+service.name,
                 cgroup_inode=12345, log_path=str(path))
    current = {'InvocationID': INVOCATION, 'ActiveState': 'failed', 'SubState': 'failed',
               'Result': 'exit-code', 'MainPID': 0, 'ExecMainPID': 1234, 'ExecMainCode': 1, 'ExecMainStatus': 1}
    handle = SimpleNamespace(identity=lambda: deepcopy(entry), manager=SimpleNamespace(inspect=AsyncMock(return_value=current)))
    key = ROOM+':shairport'
    value = {'key': key, 'entry': entry, 'handle': handle, 'root': root, 'log_path': path}
    if producer:
        status_dir = root/'native-validation/status'
        status_dir.mkdir(mode=0o700)
        status_path = status_dir/'status.json'
        status_path.write_text(json.dumps({'uid': 0, 'gid': os.getgid(), 'pid': 1234, 'frames': 38400,
            'stage': 'streaming', 'finished': True, 'generation': 1, 'session_id': ROOM,
            'error': 'BrokenPipeError: Native receiver closed', 'commands': [{'generation': 1, 'action': 'run'}]}))
        status_path.chmod(0o600)
        value['producer'] = {'path': status_path, 'account': {'uid': 0, 'gid': os.getgid()}}
    return value


def broker_for(record):
    handle = record['handle']
    producer = record.get('producer')
    producers = {ROOM: {'unit': handle, 'key': record['key'], 'status': producer['path'],
                        'account': producer['account']}} if producer else {}
    broker = SimpleNamespace(config=SimpleNamespace(runtime_state_dir=record['root'].parents[1]),
        sender_processes={}, network=SimpleNamespace(installation_tag='b265eb7d',
            manifest={'processes': {record['key']: deepcopy(record['entry'])}}))
    rooms = {ROOM: SimpleNamespace(directory=record['root'], processes={'shairport': handle})}
    return broker, rooms, producers


def journal_line(record, message, **changes):
    value = {'_SYSTEMD_UNIT': record['entry']['unit'], '_SYSTEMD_INVOCATION_ID': INVOCATION,
             '_BOOT_ID': BOOT.replace('-', ''), '__MONOTONIC_TIMESTAMP': '62113037229',
             'MESSAGE': message}
    value.update(changes)
    return json.dumps(value).encode()+b'\n'


async def test_exact_failed_invocation_and_dead_producer_survive_destructive_room_cleanup(tmp_path):
    record = source(tmp_path)
    broker, rooms, producers = broker_for(record)
    admitted, errors = evidence.owned_sources(broker, rooms, producers)
    assert errors == [] and len(admitted) == 1
    async def journal(actual, sanitizer):
        return evidence.journal_records(journal_line(record, 'Native timing ingress failed: TimingError'), actual, sanitizer)
    receipt = await evidence.capture(tmp_path.resolve()/'failure', admitted, journal=journal)
    shutil.rmtree(record['root'])
    artifact = Path(receipt['path'])
    captured = json.loads(artifact.read_text())['units'][record['key']]
    assert captured['status']['alive'] is False
    assert captured['status']['ExecMainStatus'] == 1
    assert captured['producer_status']['state']['frames'] == 38400
    assert captured['producer_status']['state']['error']['text'].startswith('BrokenPipeError')
    assert captured['journal']['records'][0]['text'].endswith('TimingError')
    assert 'TimingError' in captured['logs'][0]['text']
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    assert stat.S_IMODE(artifact.parent.stat().st_mode) == 0o700
    assert receipt['bytes'] <= evidence.ARTIFACT_BYTES


def test_clock_sampling_receipts_preserve_rejected_attempts_separately_from_accepted_packets(tmp_path):
    record = source(tmp_path)
    path = record['producer']['path']
    status = json.loads(path.read_text())
    status.update(clock_sample_attempts=29, clock_sample_retries=3,
                  max_attempt_clock_bracket_ns=1_347_960, max_clock_bracket_ns=900_000)
    path.write_text(json.dumps(status))
    captured = evidence.producer_status(record, evidence.Sanitizer())['state']
    assert captured['clock_sample_attempts'] == 29
    assert captured['clock_sample_retries'] == 3
    assert captured['max_attempt_clock_bracket_ns'] == 1_347_960
    assert captured['max_clock_bracket_ns'] == 900_000


@pytest.mark.parametrize('key', ['clock_sample_attempts', 'clock_sample_retries', 'max_attempt_clock_bracket_ns'])
@pytest.mark.parametrize('value', [True, -1, 2**64, 1.0])
def test_clock_sampling_receipts_reject_noninteger_or_out_of_range_telemetry(tmp_path, key, value):
    record = source(tmp_path)
    path = record['producer']['path']
    status = json.loads(path.read_text())
    status[key] = value
    path.write_text(json.dumps(status))
    with pytest.raises(RuntimeFailure, match='invalid bounded counter'):
        evidence.producer_status(record, evidence.Sanitizer())


@pytest.mark.parametrize('mutation', ['wrong-unit', 'wrong-invocation', 'wrong-boot', 'wrong-role', 'wrong-log-path'])
def test_nonmatching_owned_handle_is_never_admitted_for_historical_reads(tmp_path, mutation):
    record = source(tmp_path)
    broker, rooms, producers = broker_for(record)
    saved = broker.network.manifest['processes'][record['key']]
    if mutation == 'wrong-unit':
        saved['unit'] = saved['unit'].replace('b265eb7d', 'ffffffff')
    elif mutation == 'wrong-invocation':
        saved['invocation_id'] = '2'*32
    elif mutation == 'wrong-boot':
        record['entry']['boot_id'] = saved['boot_id'] = '39d00e63-ed5f-45a0-bc99-63cde4f1f15b'
    elif mutation == 'wrong-role':
        record['entry']['name'] = saved['name'] = 'audio'
    else:
        record['entry']['log_path'] = saved['log_path'] = str(tmp_path/'unrelated-house.log')
    admitted, errors = evidence.owned_sources(broker, rooms, producers)
    assert admitted == [] and errors[0]['key'] == record['key']


@pytest.mark.parametrize('field,value', [('_SYSTEMD_UNIT', 'legacy-house.service'),
    ('_SYSTEMD_INVOCATION_ID', '2'*32), ('_BOOT_ID', 'f'*32)])
def test_unrelated_journal_rows_are_rejected_even_if_the_command_returned_them(tmp_path, field, value):
    record = source(tmp_path)
    raw = journal_line(record, 'actual exact error')+journal_line(record, 'unrelated secret log', **{field: value})
    captured = evidence.journal_records(raw, record, evidence.Sanitizer())
    assert [row['text'] for row in captured['records']] == ['actual exact error']
    assert captured['rejected_records'] == 1


@pytest.mark.parametrize('substitution', ['file-symlink', 'parent-symlink', 'hardlink', 'writable-parent', 'fifo'])
def test_untrusted_file_substitutions_refuse_reads_without_following_or_blocking(tmp_path, substitution):
    record = source(tmp_path)
    path = record['log_path']
    if substitution == 'file-symlink':
        target = path.with_name('unrelated.log')
        path.rename(target)
        path.symlink_to(target)
    elif substitution == 'parent-symlink':
        target = path.parent.with_name('replacement')
        path.parent.rename(target)
        path.parent.symlink_to(target, target_is_directory=True)
    elif substitution == 'hardlink':
        os.link(path, path.with_name('second-link'))
    elif substitution == 'writable-parent':
        path.parent.chmod(0o777)
    else:
        path.unlink()
        os.mkfifo(path, 0o600)
    with pytest.raises((OSError, RuntimeFailure)):
        evidence.read_source(path, root=record['root'], owner=0, tail=True)


def test_size_bound_keeps_complete_log_tail_and_refuses_oversized_status(tmp_path):
    record = source(tmp_path)
    record['log_path'].write_bytes(b'x'*(evidence.FILE_BYTES*3)+b'\nlast exact TimingError\n')
    raw, metadata = evidence.read_source(record['log_path'], root=record['root'], owner=0, tail=True)
    assert raw == b'last exact TimingError\n' and metadata['truncated']
    assert len(raw) <= evidence.FILE_BYTES
    record['producer']['path'].write_bytes(b'x'*(evidence.FILE_BYTES+1))
    with pytest.raises(RuntimeFailure, match='byte bound'):
        evidence.producer_status(record, evidence.Sanitizer())


async def test_secret_sdp_and_credential_lines_never_reach_retained_artifacts(tmp_path):
    record = source(tmp_path)
    secret, token = '0123456789superprivate', 'other-api-private'
    messages = f'TimingError {secret}\nAuthorization: Basic {token}\na=ice-pwd:private\nv=0\npassword={secret}\n'
    record['log_path'].write_text(messages)
    async def journal(actual, sanitizer):
        return evidence.journal_records(journal_line(record, messages), actual, sanitizer)
    receipt = await evidence.capture(tmp_path.resolve()/'failure', [record], private=(secret, token), journal=journal)
    raw = Path(receipt['path']).read_text()
    assert secret not in raw and token not in raw
    assert 'a=ice-pwd' not in raw and 'Authorization: Basic' not in raw and 'password=' not in raw
    assert 'TimingError [redacted]' in raw


async def test_global_deadline_cancels_capture_but_preserves_already_captured_status(tmp_path):
    record = source(tmp_path)
    cancelled = asyncio.Event()
    async def stalled(*_):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    begin = asyncio.get_running_loop().time()
    receipt = await evidence.capture(tmp_path.resolve()/'failure', [record], journal=stalled, deadline=.04)
    assert asyncio.get_running_loop().time()-begin < .5
    assert cancelled.is_set() and receipt['deadline_exceeded']
    captured = json.loads(Path(receipt['path']).read_text())
    assert captured['units'][record['key']]['producer_status']['state']['frames'] == 38400


async def test_journal_failure_is_separate_from_original_failure_and_does_not_skip_cleanup(tmp_path):
    record = source(tmp_path)
    original = RuntimeFailure('Exact B PCM block1724 failed')
    failure = {'type': type(original).__name__, 'message': str(original)}
    async def unavailable(*_):
        raise RuntimeFailure('Journal command failed')
    receipt = await evidence.capture(tmp_path.resolve()/'failure', [record], journal=unavailable)
    shutil.rmtree(record['root'])
    assert failure == {'type': 'RuntimeFailure', 'message': 'Exact B PCM block1724 failed'}
    captured = json.loads(Path(receipt['path']).read_text())
    assert {'source': 'journal', 'type': 'RuntimeFailure'} in captured['units'][record['key']]['errors']
    assert not record['root'].exists()


async def test_real_diagnostic_child_timeout_is_killed_and_reaped(tmp_path):
    pid_path = tmp_path/'child.pid'
    code = 'import os,pathlib,time;pathlib.Path(os.environ["PID_PATH"]).write_text(str(os.getpid()));time.sleep(30)'
    # The helper deliberately excludes ambient environment; use an argument in
    # the real child instead of inheriting a test credential-bearing variable.
    code = code.replace('os.environ["PID_PATH"]', 'os.sys.argv[1]')
    with pytest.raises(asyncio.TimeoutError):
        await evidence.bounded_process([sys.executable, '-c', code, str(pid_path)], timeout=.3)
    assert pid_path.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_path.read_text()), 0)


async def test_real_oversized_diagnostic_stdout_cannot_allocate_or_wait_without_bound(tmp_path):
    pid_path = tmp_path/'child.pid'
    code = 'import os,pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));os.write(1,b"x"*1048576);time.sleep(30)'
    raw, metadata = await evidence.bounded_process([sys.executable, '-c', code, str(pid_path)], limit=4096, timeout=1.)
    assert len(raw) == 4096 and metadata['truncated']
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_path.read_text()), 0)


async def test_exact_journal_command_uses_conjunctive_ids_instead_of_a_time_range(tmp_path, monkeypatch):
    record = source(tmp_path)
    executable = tmp_path.resolve()/'journalctl'
    executable.write_text('held executable fixture')
    executable.chmod(0o755)
    monkeypatch.setattr(evidence, 'trusted_file', lambda *_args, **_kwargs: executable)
    runner = AsyncMock(return_value=(journal_line(record, 'TimingError'), {'truncated': False, 'exit_code': 0}))
    monkeypatch.setattr(evidence, 'bounded_process', runner)
    captured = await evidence.exact_journal(record, evidence.Sanitizer())
    argv = runner.call_args.args[0]
    assert f'_SYSTEMD_UNIT={record["entry"]["unit"]}' in argv
    assert '_SYSTEMD_INVOCATION_ID='+INVOCATION in argv
    assert '_BOOT_ID='+BOOT.replace('-', '') in argv
    assert not any(value == '+' or value.startswith('--since') or value.startswith('--unit') for value in argv)
    assert captured['records'][0]['text'] == 'TimingError'


async def test_live_successor_invocation_never_counts_as_current_failed_unit_status(tmp_path):
    record = source(tmp_path)
    record['handle'].manager.inspect.return_value.update(InvocationID='2'*32, ActiveState='active', MainPID=5678)
    with pytest.raises(RuntimeFailure, match='different invocation'):
        await evidence.unit_status(record, evidence.Sanitizer())


async def test_old_role_log_error_is_explicitly_historical_while_exact_journal_is_invocation_scoped(tmp_path):
    record = source(tmp_path)
    record['log_path'].write_text('Previous run: Native input missed its initial presentation anchor\n')
    async def journal(actual, sanitizer):
        return evidence.journal_records(journal_line(record, 'Current exact invocation is healthy'), actual, sanitizer)
    receipt = await evidence.capture(tmp_path.resolve()/'failure', [record], journal=journal)
    unit = json.loads(Path(receipt['path']).read_text())['units'][record['key']]
    assert 'Previous run' in unit['logs'][0]['text']
    assert unit['logs'][0]['invocation_scoped'] is False
    assert 'Historical' in unit['logs'][0]['scope'] and 'earlier invocations' in unit['logs'][0]['scope']
    assert unit['journal']['invocation_scoped'] is True
    assert 'unit, invocation and boot' in unit['journal']['scope']
    assert unit['journal']['records'][0]['_SYSTEMD_INVOCATION_ID'] == INVOCATION
    assert unit['journal']['records'][0]['_BOOT_ID'] == BOOT.replace('-', '')


async def test_json_escape_expansion_cannot_exceed_the_total_private_artifact_budget(tmp_path):
    record = source(tmp_path)
    # Valid complete log lines can expand sixfold when JSON escapes controls.
    # Exercise the real serializer, not just the input-byte limit.
    record['log_path'].write_bytes((b'\x01'*200+b'\n')*500)
    rotated = Path(str(record['log_path'])+'.1')
    shutil.copyfile(record['log_path'], rotated)
    rotated.chmod(0o600)
    sources = [dict(record, key=f'{ROOM}:bounded-fixture-{index}') for index in range(evidence.MAX_UNITS)]
    async def no_journal(*_):
        return {'records': []}
    receipt = await evidence.capture(tmp_path.resolve()/'failure', sources, journal=no_journal)
    value = json.loads(Path(receipt['path']).read_text())
    assert receipt['bytes'] <= evidence.ARTIFACT_BYTES
    assert any(error['type'] == 'ArtifactByteLimit' for unit in value['units'].values() for error in unit['errors'])
    assert all(unit['producer_status']['state']['frames'] == 38400 for unit in value['units'].values())


async def test_cancellation_reaps_an_actual_silent_diagnostic_child(tmp_path):
    pid_path = tmp_path/'child.pid'
    code = 'import os,pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(30)'
    operation = asyncio.create_task(evidence.bounded_process([sys.executable, '-c', code, str(pid_path)], timeout=30))
    deadline = asyncio.get_running_loop().time()+1.
    while not pid_path.exists() and asyncio.get_running_loop().time() < deadline:  # noqa: ASYNC110
        await asyncio.sleep(.01)
    assert pid_path.exists()
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_path.read_text()), 0)


def test_harness_capture_is_before_producer_stop_and_room_delete_and_supervisor_retains_reference():
    root = Path(__file__).parent/'linux'
    parsed = ast.parse((root/'check_native_grouping.py').read_text())
    calls = {ast.unparse(node.func): node.lineno for node in ast.walk(parsed) if isinstance(node, ast.Call)}
    assert calls['failure_evidence.capture'] < calls["handle['unit'].stop"] < calls['api.patch']
    assert "result['harness_failure_diagnostics'] = evidence" in (root/'run_native_grouping.py').read_text()


def soak_progress_status(tmp_path):
    """Use the original protected status file, not a mocked sanitizer result."""
    record = source(tmp_path)
    path = record['producer']['path']
    value = json.loads(path.read_text())
    value['delivery_observation'] = {
        'stage': 'progress_publish', 'frame': 38400,
        'target_ns': 1_000_000_000, 'observed_ns': 1_006_206_000, 'lateness_ns': 6_206_000,
    }
    value['progress_publication'] = {
        'started_ns': 1_000_000_000, 'finished_ns': 1_001_000_000,
        'duration_ns': 1_000_000, 'max_duration_ns': 6_206_000, 'count': 7, 'passed': True,
    }
    return record, path, value


def read_soak_progress_status(record, path, value):
    path.write_text(json.dumps(value))
    return evidence.producer_status(record, evidence.Sanitizer())


@pytest.mark.parametrize('stage', [
    'command_read', 'preclock', 'clock_sample', 'native_send', 'progress_publish',
])
def test_soak_progress_exact_maps_survive_original_protected_status_reader(tmp_path, stage):
    record, path, value = soak_progress_status(tmp_path)
    value['delivery_observation']['stage'] = stage
    captured = read_soak_progress_status(record, path, value)
    assert captured['state']['delivery_observation'] == value['delivery_observation']
    assert captured['state']['progress_publication'] == value['progress_publication']
    assert captured['state']['frames'] == 38400
    assert captured['state']['generation'] == 1
    assert captured['state']['session_id'] == ROOM
    assert captured['state']['stage'] == 'streaming'
    assert captured['state']['finished'] is True
    assert captured['source']['source_bytes'] == path.stat().st_size
    assert captured['source']['bytes_read'] <= evidence.FILE_BYTES
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_soak_progress_inflight_publication_is_retained_without_inventing_success(tmp_path):
    record, path, value = soak_progress_status(tmp_path)
    value['progress_publication'].update(finished_ns=0, duration_ns=0, passed=False)
    value['delivery_observation'].update(observed_ns=0, lateness_ns=-20_000_000)
    captured = read_soak_progress_status(record, path, value)['state']
    assert captured['progress_publication'] == value['progress_publication']
    assert captured['progress_publication']['passed'] is False
    assert captured['delivery_observation']['lateness_ns'] == -20_000_000


@pytest.mark.parametrize('lateness', [-2**63, 0, 2**63-1])
def test_soak_progress_retains_declared_integer_boundaries_exactly(tmp_path, lateness):
    record, path, value = soak_progress_status(tmp_path)
    value['delivery_observation'].update(frame=2**64-1, target_ns=0,
                                         observed_ns=2**64-1, lateness_ns=lateness)
    value['progress_publication'].update(started_ns=0, finished_ns=2**64-1,
        duration_ns=2**64-1, max_duration_ns=2**64-1, count=2**64-1)
    captured = read_soak_progress_status(record, path, value)['state']
    assert captured['delivery_observation'] == value['delivery_observation']
    assert captured['progress_publication'] == value['progress_publication']


@pytest.mark.parametrize('mapping', ['delivery_observation', 'progress_publication'])
@pytest.mark.parametrize('bad', [None, [], True, 'arbitrary nested text'])
def test_soak_progress_rejects_nonmapping_telemetry_from_real_status(tmp_path, mapping, bad):
    record, path, value = soak_progress_status(tmp_path)
    value[mapping] = bad
    with pytest.raises(RuntimeFailure, match='invalid bounded telemetry'):
        read_soak_progress_status(record, path, value)


@pytest.mark.parametrize('mapping,key', [
    ('delivery_observation', 'stage'), ('delivery_observation', 'frame'),
    ('delivery_observation', 'target_ns'), ('delivery_observation', 'observed_ns'),
    ('delivery_observation', 'lateness_ns'), ('progress_publication', 'started_ns'),
    ('progress_publication', 'finished_ns'), ('progress_publication', 'duration_ns'),
    ('progress_publication', 'max_duration_ns'), ('progress_publication', 'count'),
    ('progress_publication', 'passed'),
])
def test_soak_progress_rejects_each_missing_required_key(tmp_path, mapping, key):
    record, path, value = soak_progress_status(tmp_path)
    del value[mapping][key]
    with pytest.raises(RuntimeFailure, match='invalid bounded telemetry'):
        read_soak_progress_status(record, path, value)


@pytest.mark.parametrize('mapping', ['delivery_observation', 'progress_publication'])
def test_soak_progress_rejects_extra_nested_fields_instead_of_copying_them(tmp_path, mapping):
    record, path, value = soak_progress_status(tmp_path)
    value[mapping]['extra'] = {'Authorization': 'must never enter the artifact'}
    with pytest.raises(RuntimeFailure, match='invalid bounded telemetry'):
        read_soak_progress_status(record, path, value)


@pytest.mark.parametrize('mapping,key', [
    ('delivery_observation', 'frame'), ('delivery_observation', 'target_ns'),
    ('delivery_observation', 'observed_ns'), ('delivery_observation', 'lateness_ns'),
    ('progress_publication', 'started_ns'), ('progress_publication', 'finished_ns'),
    ('progress_publication', 'duration_ns'), ('progress_publication', 'max_duration_ns'),
    ('progress_publication', 'count'),
])
def test_soak_progress_rejects_bool_for_each_integer_field(tmp_path, mapping, key):
    record, path, value = soak_progress_status(tmp_path)
    value[mapping][key] = True
    with pytest.raises(RuntimeFailure, match='invalid bounded telemetry'):
        read_soak_progress_status(record, path, value)


@pytest.mark.parametrize('mapping,key', [
    ('delivery_observation', 'frame'), ('progress_publication', 'count'),
])
@pytest.mark.parametrize('bad', [-1, 2**64, 1.0, '1', {}, None])
def test_soak_progress_rejects_out_of_range_or_malformed_unsigned_values(tmp_path, mapping, key, bad):
    record, path, value = soak_progress_status(tmp_path)
    value[mapping][key] = bad
    with pytest.raises(RuntimeFailure, match='invalid bounded telemetry'):
        read_soak_progress_status(record, path, value)


@pytest.mark.parametrize('bad', [-2**63-1, 2**63, 1.0, '1', {}, None])
def test_soak_progress_rejects_invalid_signed_lateness(tmp_path, bad):
    record, path, value = soak_progress_status(tmp_path)
    value['delivery_observation']['lateness_ns'] = bad
    with pytest.raises(RuntimeFailure, match='invalid bounded telemetry'):
        read_soak_progress_status(record, path, value)


@pytest.mark.parametrize('bad', ['streaming', '', True, ['native_send'], {'native_send': 1}])
def test_soak_progress_rejects_unknown_or_nested_delivery_stages(tmp_path, bad):
    record, path, value = soak_progress_status(tmp_path)
    value['delivery_observation']['stage'] = bad
    with pytest.raises(RuntimeFailure, match='invalid bounded telemetry'):
        read_soak_progress_status(record, path, value)


@pytest.mark.parametrize('bad', [0, 1, 'true', None, {}])
def test_soak_progress_rejects_nonboolean_publication_result(tmp_path, bad):
    record, path, value = soak_progress_status(tmp_path)
    value['progress_publication']['passed'] = bad
    with pytest.raises(RuntimeFailure, match='invalid bounded telemetry'):
        read_soak_progress_status(record, path, value)


def test_soak_progress_oversize_status_still_fails_original_source_byte_bound(tmp_path):
    record, path, value = soak_progress_status(tmp_path)
    value['delivery_observation']['stage'] = 'x' * (evidence.FILE_BYTES+1)
    with pytest.raises(RuntimeFailure, match='fixed byte bound'):
        read_soak_progress_status(record, path, value)


async def test_soak_progress_retained_artifact_survives_cleanup_with_original_redaction(tmp_path):
    record, path, value = soak_progress_status(tmp_path)
    value['error'] = 'Original bounded native cadence refusal\nAuthorization: private-value'
    read_soak_progress_status(record, path, value)
    async def no_journal(*_args):
        return {'records': []}
    receipt = await evidence.capture(tmp_path.resolve()/'failure', [record], journal=no_journal)
    shutil.rmtree(record['root'])
    raw = Path(receipt['path']).read_bytes()
    state = json.loads(raw)['units'][record['key']]['producer_status']['state']
    assert state['delivery_observation'] == value['delivery_observation']
    assert state['progress_publication'] == value['progress_publication']
    assert state['frames'] == 38400 and state['finished'] is True
    assert state['error']['text'] == 'Original bounded native cadence refusal'
    assert state['error']['omitted_sensitive_lines'] == 1
    assert b'private-value' not in raw and b'Authorization' not in raw
    assert receipt['bytes'] == len(raw) <= evidence.ARTIFACT_BYTES
    assert stat.S_IMODE(Path(receipt['path']).stat().st_mode) == 0o600


@pytest.mark.parametrize('mapping', ['delivery_observation', 'progress_publication'])
async def test_soak_progress_malformed_optional_map_preserves_unit_journal_and_primary_log(tmp_path, mapping):
    record, path, value = soak_progress_status(tmp_path)
    value[mapping]['unknown'] = {'secret': 'never-copy-this-value'}
    path.write_text(json.dumps(value))
    async def journal(actual, sanitizer):
        return evidence.journal_records(journal_line(record, 'Original bounded cadence refusal'), actual, sanitizer)
    receipt = await evidence.capture(tmp_path.resolve()/'failure', [record], journal=journal)
    raw = Path(receipt['path']).read_bytes()
    unit = json.loads(raw)['units'][record['key']]
    assert unit['errors'] == [{'source': 'producer_status', 'type': 'RuntimeFailure'}]
    assert 'producer_status' not in unit
    assert unit['status']['alive'] is False and unit['status']['ExecMainStatus'] == 1
    assert unit['journal']['records'][0]['text'] == 'Original bounded cadence refusal'
    assert 'Native timing ingress failed: TimingError' in unit['logs'][0]['text']
    assert b'never-copy-this-value' not in raw
    assert receipt['bytes'] == len(raw) <= evidence.ARTIFACT_BYTES
