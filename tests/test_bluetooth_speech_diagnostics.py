"""Exact scope, bounded transport and failure/cancellation evidence regressions."""
from __future__ import annotations

import asyncio
import ast
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from shiri.runtime.backend import OwnToneClient
from shiri.runtime.system import RuntimeFailure

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('bluetooth_speech_diagnostics_test',
                                             ROOT/'tests/linux/native_speech_diagnostics.py')
diag = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = diag
spec.loader.exec_module(diag)


def context():
    identifier, launch, incarnation, session = (uuid4().hex for _ in range(4))
    health = {'ready': True, 'error': None, 'native_generation': 1, 'source_operation_generation': 2,
              'source': {'zone_id': identifier, 'incarnation': incarnation, 'epoch': 1,
                         'ready': True, 'error': None, 'transitioning': False,
                         'owner': {'zone_id': identifier, 'incarnation': incarnation,
                                   'session_id': session, 'epoch': 1}}}
    expected = diag.source_binding(identifier, launch, health)
    value = dict.fromkeys(diag.INTEGER_FIELDS, 0) | dict.fromkeys(diag.BOOL_FIELDS, False)
    value.update(expected, version=1, scope=diag.SCOPE, reserve_ns=20_000_000,
                 configured=True, source_initialized=True, observed_monotonic_ns=1,
                 admitted_frames=960, mixed_frames=480, underflow_events=1, underflow_frames=480)
    return identifier, launch, health, expected, value


def client_for(value, requests, *, response=None):
    def transport(request):
        requests.append(request)
        assert request.method == 'GET' and request.url.path == diag.PATH
        assert request.headers['authorization'].startswith('Basic ')
        return response or httpx.Response(200, json=value)
    return OwnToneClient('http://private.invalid:3869', password='not-recorded',
                         transport=httpx.MockTransport(transport))


@pytest.mark.parametrize('field,value', [
    ('scope', 'utterance'), ('room_id', '0'*32), ('launch_generation', '0'*32),
    ('incarnation', '0'*32), ('session_id', None), ('epoch', 2), ('generation', 2),
    ('operation_generation', 3), ('version', True), ('reserve_ns', 0),
    ('configured', 1), ('source_initialized', False), ('source_faulted', True),
    ('queued_frames', 12_001), ('underflow_frames', -1), ('mixed_frames', True),
    ('admitted_frames', 2**63), ('observed_monotonic_ns', 0),
])
def test_snapshot_never_qualifies_wrong_scope_source_or_types(field, value):
    _identifier, _launch, _health, expected, original = context()
    original[field] = value
    with pytest.raises(diag.DiagnosticFailure):
        diag.validate_status(original, expected)


def test_exact_snapshot_preserves_lifetime_counters_without_utterance_attribution():
    _identifier, _launch, _health, expected, value = context()
    observed = diag.validate_status(value, expected)
    assert observed == value and observed is not value
    assert observed['scope'] == diag.SCOPE and observed['underflow_events'] == 1
    assert 'utterance_id' not in observed and 'speech_session_id' not in observed
    value['undeclared_secret'] = 'not-retained'*100_000
    with pytest.raises(diag.DiagnosticFailure, match='status_schema'):
        diag.validate_status(value, expected)


async def test_actual_authenticated_stream_is_small_and_duplicate_keys_are_refused():
    _identifier, _launch, _health, _expected, value = context()
    requests = []
    client = client_for(value, requests)
    try:
        assert await diag.read_status(client) == value
        assert len(requests) == 1
    finally:
        await client.close()
    for body, code in ((b'x'*(diag.MAX_BYTES+1), 'status_bytes'),
                       (b'{"version":1,"version":1}', 'status_duplicate_key')):
        client = client_for(value, [], response=httpx.Response(200, content=body))
        try:
            with pytest.raises(diag.DiagnosticFailure, match=code):
                await diag.read_status(client)
        finally:
            await client.close()


async def test_before_active_after_and_failure_are_bounded_and_never_reset_lifetime_counters():
    contexts, clients, requests, states = [], [], [], {}
    for _ in range(2):
        identifier, launch, health, _expected, value = context()
        contexts.append((identifier, health))
        client = client_for(value, requests)
        clients.append(client)
        states[identifier] = SimpleNamespace(launch_generation=launch, client=client)
    async def health(state, timeout):
        assert timeout == .1
        return deepcopy(next(value for key, value in contexts if states[key] is state))
    report = {}
    collector = diag.SpeechDiagnostics(states, health, report)
    try:
        for phase in ('before_tts', 'during_tts', 'after_tts_restore'):
            await collector.capture(phase)
        primary = RuntimeFailure('original spectral rejection')
        await collector.failure(primary, 'speech_active')
        await collector.failure(primary, 'later')
        for _ in range(3):
            await collector.capture('extra')
        evidence = report['speech_status_diagnostics']
        assert len(requests) == len(evidence['observations']) == 12
        assert evidence['omitted_observations'] == 2
        assert all(row['binding_verified'] and row['snapshot']['underflow_frames'] == 480
                   for row in evidence['observations'])
        assert evidence['counter_scope'] == 'room_launch_lifetime'
        assert evidence['utterance_identity_available'] is False
        assert all('not-recorded' not in json.dumps(row) for row in evidence['observations'])
        assert [row['phase'] for row in evidence['observations'][:8]] == [
            'before_tts', 'before_tts', 'during_tts', 'during_tts',
            'after_tts_restore', 'after_tts_restore', 'failure:speech_active', 'failure:speech_active']
    finally:
        for client in clients:
            await client.close()


async def test_source_change_between_worker_observations_cannot_verify_current_binding():
    identifier, launch, health, _expected, value = context()
    client = client_for(value, [])
    state = SimpleNamespace(launch_generation=launch, client=client)
    calls = 0
    async def worker(_state, _timeout):
        nonlocal calls
        calls += 1
        result = deepcopy(health)
        if calls == 2:
            result['source_operation_generation'] += 1
        return result
    report = {}
    try:
        await diag.SpeechDiagnostics({identifier: state}, worker, report).capture('during_tts')
        row = report['speech_status_diagnostics']['observations'][0]
        assert not row['binding_verified'] and 'snapshot' not in row
        assert row['diagnostic_error']['code'] == 'worker_operation_changed_during_read'
    finally:
        await client.close()


def exact_watch(healthy, collector):
    """Execute the actual fixture's first-failure edge, not a copied surrogate."""
    tree = ast.parse((ROOT/'tests/linux/check_native_bluetooth_route.py').read_text())
    exercise = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'exercise')
    watch = next(node for node in exercise.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'watch')
    factory = ast.parse('def make():\n control_error = None\n return None\n').body[0]
    factory.body[-1:] = [watch, ast.parse('return watch, lambda: control_error').body[0]]
    environment = {'asyncio': asyncio, 'healthy': healthy, 'speech_diagnostics': collector,
                   'done': asyncio.Event(), 'phase': 'speech_active'}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[factory], type_ignores=[])), '<actual-watch>', 'exec'), environment)
    return environment['make']()


@pytest.mark.parametrize('mode', ['http_rejection', 'malformed_scope', 'dependency_cancel'])
async def test_actual_watch_records_optional_failure_before_cleanup_and_preserves_primary(mode):
    identifier, launch, health, _expected, value = context()
    if mode == 'malformed_scope':
        value['room_id'] = '0'*32
    client = client_for(value, [], response=httpx.Response(503) if mode == 'http_rejection' else None)
    state = SimpleNamespace(launch_generation=launch, client=client)
    async def worker(_state, _timeout):
        if mode == 'dependency_cancel':
            raise asyncio.CancelledError()
        return health
    report = {}
    collector = diag.SpeechDiagnostics({identifier: state}, worker, report)
    primary = RuntimeFailure('unchanged spectral floor rejection')
    async def rejected():
        raise primary
    watch, control_error = exact_watch(rejected, collector)
    cleanup = False
    try:
        with pytest.raises(RuntimeFailure) as error:
            await watch()
        assert error.value is primary and control_error() is primary
        row = report['speech_status_diagnostics']['observations'][0]
        assert not cleanup and not row['binding_verified'] and row['diagnostic_error']
        cleanup = True
        await collector.failure(primary, 'later_cleanup')
        assert len(report['speech_status_diagnostics']['observations']) == 1
    finally:
        await client.close()


async def test_room_deadline_records_timeout_and_joins_its_owned_observation():
    identifier, launch, _health, _expected, value = context()
    client = client_for(value, [])
    state = SimpleNamespace(launch_generation=launch, client=client)
    stopped = asyncio.Event()
    async def blocked(_state, _timeout):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    report = {}
    started = time.monotonic()
    try:
        await diag.SpeechDiagnostics({identifier: state}, blocked, report).capture('during_tts')
        assert time.monotonic()-started < .6 and stopped.is_set()
        row = report['speech_status_diagnostics']['observations'][0]
        assert row['diagnostic_error']['type'] == 'TimeoutError' and not row['binding_verified']
        assert not any(task.get_name() == 'private-bluetooth-speech-status' for task in asyncio.all_tasks())
    finally:
        await client.close()


async def test_external_cancellation_stays_cancelled_and_joins_diagnostic_task():
    identifier, launch, _health, _expected, value = context()
    client = client_for(value, [])
    state = SimpleNamespace(launch_generation=launch, client=client)
    entered, stopped = asyncio.Event(), asyncio.Event()
    async def blocked(_state, _timeout):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    report = {}
    collector = diag.SpeechDiagnostics({identifier: state}, blocked, report)
    task = asyncio.create_task(collector.capture('during_tts'))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set() and task.cancelled()
        row = report['speech_status_diagnostics']['observations'][0]
        assert row['diagnostic_error']['code'] == 'diagnostic_cancelled'
        assert not any(item.get_name() == 'private-bluetooth-speech-status' for item in asyncio.all_tasks())
        cancelled = asyncio.CancelledError()
        await collector.failure(cancelled, 'cancelled')
        assert not collector.failure_started  # No GET on a cancelled primary edge.
    finally:
        await client.close()


async def test_concurrent_exercise_rejection_joins_first_watch_snapshot_before_cleanup():
    identifier, launch, health, _expected, value = context()
    client = client_for(value, [])
    state = SimpleNamespace(launch_generation=launch, client=client)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    async def worker(_state, _timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return health
    report = {}
    collector = diag.SpeechDiagnostics({identifier: state}, worker, report)
    primary = RuntimeFailure('original spectral rejection')
    async def rejected():
        raise primary
    watch, control_error = exact_watch(rejected, collector)
    watcher = asyncio.create_task(watch())
    cleanup = False
    async def exercise_rejection():
        nonlocal cleanup
        await collector.failure(primary, 'same_first_rejection')
        cleanup = True
    join = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert control_error() is primary
        join = asyncio.create_task(exercise_rejection())
        await asyncio.sleep(.01)
        assert not cleanup and not join.done()
        release.set()
        with pytest.raises(RuntimeFailure) as error:
            await watcher
        await join
        assert error.value is primary and cleanup
        rows = report['speech_status_diagnostics']['observations']
        assert len(rows) == 1 and rows[0]['binding_verified']
    finally:
        release.set()
        await asyncio.gather(watcher, *(tuple([join]) if join else ()), return_exceptions=True)
        await client.close()


@pytest.mark.parametrize('scheduled', [False, True])
async def test_same_turn_dependency_and_external_cancellation_never_hide_external_cancel(scheduled):
    identifier, launch, _health, _expected, value = context()
    client = client_for(value, [])
    state = SimpleNamespace(launch_generation=launch, client=client)
    holder = {}
    async def dependency(_state, _timeout):
        if scheduled:
            asyncio.get_running_loop().call_soon(holder['caller'].cancel)
        else:
            holder['caller'].cancel()
        raise asyncio.CancelledError()
    report = {}
    collector = diag.SpeechDiagnostics({identifier: state}, dependency, report)
    task = holder['caller'] = asyncio.create_task(collector.capture('during_tts'))
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
        row = report['speech_status_diagnostics']['observations'][0]
        assert row['diagnostic_error']['code'] == 'diagnostic_cancelled'
        assert not any(item.get_name() == 'private-bluetooth-speech-status' for item in asyncio.all_tasks())
    finally:
        await client.close()
