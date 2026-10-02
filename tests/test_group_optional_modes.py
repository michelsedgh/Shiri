"""Optional-mode admission and orchestration without any kernel/device path."""
import ast
import asyncio
from copy import deepcopy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from shiri.runtime.system import RuntimeFailure

pytest.importorskip('numpy', reason='Grouped manual observer requires the audio extra')
pytest.importorskip('aiortc', reason='Grouped manual observer requires the audio extra')

SOURCE = Path(__file__).parent/'linux/check_native_grouping.py'


def legacy_run_body(run):
    body = next(node.body for node in run.body if isinstance(node, ast.Try))
    wrapper = next(index for index, node in enumerate(body) if isinstance(node, ast.If)
                   and any(isinstance(child, ast.Attribute) and child.attr == 'exercise'
                           and isinstance(child.value, ast.Name) and child.value.id == 'epoch'
                           for child in ast.walk(node)))
    # The explicit fresh epoch branch is separate. These tests continue to
    # execute the SAME maintained legacy hooks and shared retirement tail.
    assert ast.unparse(body[wrapper].test) == 'latency_epoch is not None'
    return body[:wrapper]+body[wrapper].orelse+body[wrapper+1:]


def admission():
    tree = ast.parse(SOURCE.read_text())
    function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'run_check')
    # Stop before the report or any OS observation. The real optional imports
    # and exact mutual-exclusion validation remain in this executable fragment.
    boundary = next(index for index, node in enumerate(function.body) if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == 'report' for target in node.targets))
    function.name = 'admit'
    function.body = function.body[:boundary]+ast.parse('return stress, faults, latency, extended').body
    namespace = {'require': lambda value, message: None if value else (_ for _ in ()).throw(RuntimeFailure(message)),
                 'importlib': importlib, 'sys': sys, 'HERE': SOURCE.resolve()}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), str(SOURCE), 'exec'), namespace)
    return namespace['admit']


@pytest.mark.asyncio
async def test_default_group_mode_imports_no_optional_failure_or_stress_helper(monkeypatch):
    monkeypatch.setattr(importlib.util, 'spec_from_file_location', lambda *_args: pytest.fail('Default mode imported an optional helper'))
    assert await admission()() == (None, None, None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize('stress,faults,latency', [(True, True, False), (1, False, False), (False, 1, False),
    ('true', False, False), (False, None, False), (True, False, True), (False, True, True), (False, False, 1)])
async def test_invalid_or_compound_modes_reject_before_any_helper_or_owned_fixture(monkeypatch, stress, faults, latency):
    monkeypatch.setattr(importlib.util, 'spec_from_file_location', lambda *_args: pytest.fail('Invalid mode imported a helper'))
    with pytest.raises(RuntimeFailure, match='boolean modes|mutually exclusive'):
        await admission()(speech_stress=stress, zone_faults=faults, latency_probe=latency)


@pytest.mark.asyncio
@pytest.mark.parametrize('stress_mode,fault_mode,latency_mode,filename,seconds,budget', [
    (True, False, False, 'native_speech_stress.py', 420, 128*1024*1024),
    (False, True, False, 'native_zone_faults.py', 180, 48*1024*1024),
    (False, False, True, 'native_latency_probe.py', 480, 128*1024*1024),
])
async def test_explicit_mode_import_retains_its_separate_producer_and_capture_budget(
        stress_mode, fault_mode, latency_mode, filename, seconds, budget):
    stress, faults, latency, extended = await admission()(speech_stress=stress_mode,
                                                        zone_faults=fault_mode, latency_probe=latency_mode)
    assert extended is (stress if stress_mode else faults if fault_mode else latency)
    assert Path(extended.__file__).name == filename
    assert extended.PRODUCER_SECONDS == seconds and extended.CAPTURE_BYTES == budget
    if fault_mode:
        assert extended.FaultContext.__dataclass_fields__['clock_offset']  # Import-safe registration, no GI or media construction.


@pytest.mark.asyncio
async def test_real_fault_hook_preserves_protected_receipt_and_fresh_a_owner_for_later_races():
    spec = importlib.util.spec_from_file_location('fault_hook_actual_observation_dependencies', SOURCE)
    observed_group = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(observed_group)
    tree = ast.parse(SOURCE.read_text())
    run = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'run_check')
    body = legacy_run_body(run)
    hook = next(node for node in body if isinstance(node, ast.If) and isinstance(node.test, ast.Name)
                and node.test.id == 'faults' and any(isinstance(child, ast.Attribute) and child.attr == 'FaultContext'
                                                   for child in ast.walk(node)))
    function = ast.AsyncFunctionDef(name='exercise_hook', args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[],
        kw_defaults=[], defaults=[]), body=[hook]+ast.parse('return fault_observer').body, decorator_list=[])
    done, calls = asyncio.Event(), []
    async def monitor_loop():
        await done.wait()
        calls.append('old all-room monitor joined')
    monitor = asyncio.create_task(monitor_loop())
    original_b = object()
    producers = {'A': 'old-a', 'B': original_b}
    report = {'producer_final': {'A': {'session_id': 'old-a'}, 'B': {'session_id': 'original-b'}}}
    captures, guards, states = {}, {}, {}
    observer = object()
    async def exercise(context):
        assert context.target == 'A' and context.untouched == 'B'
        assert context.captures is captures and context.producers is producers and context.pcm_guards is guards
        assert context.states is states and context.capture_factory == 'fault-capture'
        assert context.untouched_health_check is observed_group.require_untouched_music_health
        calls.append('original-b continuous observer already started')
        await context.handoff()
        producers['A'] = 'recovered-a'
        return observer
    namespace = {'faults': SimpleNamespace(FaultContext=lambda **fields: SimpleNamespace(**fields), exercise=exercise),
        'report': report, 'deepcopy': deepcopy, 'done': done, 'monitor': monitor,
        'api': object(), 'broker': object(), 'room_states': states, 'captures': captures,
        'producers': producers, 'pcm_guards': guards, 'temporary': Path('/never-created'), 'clock': object(),
        'base_time': 123, 'offset': 789, 'capture_class': 'fault-capture', 'FinalPcmGuard': object(),
        'launch_producer': object(), 'publish_command': object(), 'music_onset_index': object(),
        'retain_failed_capture': object(), 'A': 'A', 'B': 'B',
        'require_untouched_music_health': observed_group.require_untouched_music_health,
        'producer_status': lambda handle: {'session_id': 'original-b' if handle is original_b else handle}}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), str(SOURCE), 'exec'), namespace)
    assert await namespace['exercise_hook']() is observer
    assert calls == ['original-b continuous observer already started', 'old all-room monitor joined']
    assert producers['B'] is original_b
    assert report['protected_producer_final'] == {'A': {'session_id': 'old-a'}, 'B': {'session_id': 'original-b'}}
    assert report['producer_final'] == {'A': {'session_id': 'recovered-a'}, 'B': {'session_id': 'original-b'}}


@pytest.mark.asyncio
async def test_actual_latency_hook_closes_baseline_peer_and_hands_off_without_replacing_b():
    spec = importlib.util.spec_from_file_location('latency_hook_actual_observation_dependencies', SOURCE)
    observed_group = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(observed_group)
    tree = ast.parse(SOURCE.read_text())
    run = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'run_check')
    body = legacy_run_body(run)
    hook = next(node for node in body if isinstance(node, ast.If) and isinstance(node.test, ast.Name)
                and node.test.id == 'latency' and any(isinstance(child, ast.Attribute) and child.attr == 'LatencyContext'
                                                     for child in ast.walk(node)))
    function = ast.AsyncFunctionDef(name='exercise_hook', args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[],
        kw_defaults=[], defaults=[]), body=[hook]+ast.parse('return fault_observer').body, decorator_list=[])
    done, calls = asyncio.Event(), []
    async def monitor_loop():
        await done.wait()
        calls.append('old all-room monitor joined')
    monitor = asyncio.create_task(monitor_loop())
    original_b = object()
    producers = {'A': 'old-a', 'B': original_b}
    report = {'producer_final': {'A': {'session_id': 'old-a'}, 'B': {'session_id': 'original-b'}}}
    captures, guards, states = {}, {}, {}
    observer = object()
    async def exercise(context):
        assert context.target == 'A' and context.untouched == 'B'
        assert context.captures is captures and context.producers is producers and context.pcm_guards is guards
        assert context.binding == 'shiri:device=exact-a' and context.declared_horizon_ns == 4_000_000_000
        assert context.capture_factory == 'latency-capture'
        assert context.group.calibrate_capture == 'actual-independent-probe'
        assert context.frozen_timing is None
        assert context.group.FrozenWorkerTiming is observed_group.FrozenWorkerTiming
        assert context.group.require_untouched_music_health is observed_group.require_untouched_music_health
        calls.append('original-b continuous observer already started')
        await context.handoff()
        producers['A'] = 'restored-zero-a'
        return observer
    class Peer:
        connectionState = 'connected'
        async def close(self):
            calls.append('baseline peer closed')
            self.connectionState = 'closed'
    namespace = {'latency': SimpleNamespace(LatencyContext=lambda **fields: SimpleNamespace(**fields), exercise=exercise),
        'report': report, 'deepcopy': deepcopy, 'done': done, 'monitor': monitor, 'asyncio': asyncio,
        'require': lambda value, message: None if value else (_ for _ in ()).throw(RuntimeFailure(message)),
        'peer': Peer(), 'tone': SimpleNamespace(stop=lambda: calls.append('baseline publisher stopped')),
        'api': object(), 'broker': object(), 'room_states': states, 'captures': captures,
        'producers': producers, 'pcm_guards': guards, 'temporary': Path('/never-created'), 'clock': object(),
        'base_time': 123, 'offset': 789, 'capture_class': 'latency-capture', 'FinalPcmGuard': object(),
        'launch_producer': object(), 'publish_command': object(), 'music_onset_index': object(),
        'retain_failed_capture': object(), 'A': 'A', 'B': 'B', 'bindings': {'A': 'shiri:device=exact-a'},
        'RELAY_DELAY_NS': 4_000_000_000, 'SimpleNamespace': SimpleNamespace,
        'declared_horizon_ns': 4_000_000_000, 'frozen_timing': None,
        'FrozenWorkerTiming': observed_group.FrozenWorkerTiming,
        'require_untouched_music_health': observed_group.require_untouched_music_health,
        'capture_snapshot': object(), 'declared_capture': object(), 'modulation': object(), 'measure_alignment': object(),
        'validate_alignment': object(), 'calibrate_capture': 'actual-independent-probe', 'failure_evidence': object(),
        'producer_status': lambda handle: {'session_id': 'original-b' if handle is original_b else handle}}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), str(SOURCE), 'exec'), namespace)
    assert await namespace['exercise_hook']() is observer
    assert calls == ['baseline publisher stopped', 'baseline peer closed',
                     'original-b continuous observer already started', 'old all-room monitor joined']
    assert producers['B'] is original_b and report['protected_producer_final']['A']['session_id'] == 'old-a'
    assert report['producer_final']['A']['session_id'] == 'restored-zero-a'


@pytest.mark.asyncio
@pytest.mark.parametrize('seconds,lead,room', [(90, -1_950_000_000, 'A'), (480, -1_950_000_000, 'B'),
                                           (480, -1, 'A'), (480, True, 'A'), (True, 220_000_000, 'A')])
async def test_bad_adverse_calendar_rejects_before_receiver_identity_or_stop(seconds, lead, room):
    spec = importlib.util.spec_from_file_location('group_latency_producer_admission_tests', SOURCE)
    group = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(group)
    state = SimpleNamespace(desired=SimpleNamespace(id=group.A if room == 'A' else group.B))
    # No processes/manager are supplied: invalid calendar must fail before
    # observing or stopping even an original receiver.
    with pytest.raises(RuntimeFailure, match='undeclared'):
        await group.launch_producer(None, state, None, 0, duration_seconds=seconds, arrival_lead_ns=lead)


@pytest.mark.asyncio
@pytest.mark.parametrize('lead', [None, -1_950_000_000])
async def test_actual_canonical_launch_publishes_explicit_latency_calendar(tmp_path, monkeypatch, lead):
    import json
    source = Path(__file__).with_name('test_native_group_observation.py')
    spec = importlib.util.spec_from_file_location('latency_owned_receiver_fixture', source)
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    # Reuse the existing exact identity/cgroup/reservation fixture; the real
    # launch function still observes/stops/replaces the proven old handle and
    # publishes its actual root-owned config. No kernel/unit startup occurs.
    fixture = fixtures.receiver_replacement.__wrapped__(tmp_path, monkeypatch)
    await fixtures.harness.launch_producer(fixture.broker, fixture.state, fixture.root, 0,
                                          duration_seconds=480, arrival_lead_ns=lead)
    config = json.loads((fixture.root/'config/producer.json').read_text())
    assert config['duration_seconds'] == 480
    assert config['arrival_lead_ns'] == (220_000_000 if lead is None else lead)
    assert fixture.events == ['exact original stopped', 'canonical replacement reserved']
    assert fixture.state.processes['shairport'] is not fixture.original


@pytest.mark.parametrize('live', [True, False, 0, 1, None, 'false'])
def test_post_null_guard_skips_only_current_age_with_explicit_boolean_flag(live, monkeypatch):
    spec = importlib.util.spec_from_file_location('latency_tail_fixture', Path(__file__).with_name('test_native_group_observation.py'))
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    group = fixtures.harness
    capture = fixtures.constant_capture(seconds=1)
    capture.last_packet_at = 100.
    monkeypatch.setattr(group, 'time', SimpleNamespace(monotonic=lambda: 100.32))
    guard = group.FinalPcmGuard(capture)
    guard.begin(0)
    if live is False:
        guard.check(live=live)
        assert guard.checked_blocks == len(capture.chunks)
    else:
        with pytest.raises(RuntimeFailure, match='300ms|boolean'):
            guard.check(live=live)
        assert guard.checked_blocks == 0


@pytest.mark.parametrize('bad', ['gap', 'content', 'offset'])
def test_post_null_guard_retains_observed_gap_and_final_queued_content_failures(bad, monkeypatch):
    spec = importlib.util.spec_from_file_location('latency_bad_tail_fixture', Path(__file__).with_name('test_native_group_observation.py'))
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    group = fixtures.harness
    capture = fixtures.constant_capture(seconds=1)
    capture.last_packet_at = 100.
    monkeypatch.setattr(group, 'time', SimpleNamespace(monotonic=lambda: 100.32))
    if bad == 'gap':
        capture.max_packet_gap = .3
    elif bad == 'content':
        capture.chunks[-1] = bytes(3840)
    else:
        capture.poll = lambda: (_ for _ in ()).throw(RuntimeFailure('Final offset changed'))
    guard = group.FinalPcmGuard(capture)
    guard.begin(0)
    with pytest.raises(RuntimeFailure, match='Observed|continuous|offset'):
        guard.check(live=False)
    assert guard.error and guard.checked_blocks < len(capture.chunks)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['baseline', 'latency', 'latency-bad-tail'])
async def test_shared_takeover_only_follows_checked_latency_restored_capture_tail(mode):
    tree = ast.parse(SOURCE.read_text())
    run = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'run_check')
    body = legacy_run_body(run)
    start = next(index for index, node in enumerate(body) if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name) and node.test.id == 'latency'
        and any(isinstance(child, ast.Attribute) and child.attr == 'close_capture' for child in ast.walk(node)))
    end = next(index for index in range(start, len(body)) if isinstance(body[index], ast.Expr)
        and isinstance(body[index].value, ast.Call) and isinstance(body[index].value.func, ast.Name)
        and body[index].value.func.id == 'publish_command')
    function = ast.AsyncFunctionDef(name='retire', args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[],
        kw_defaults=[], defaults=[]), body=body[start:end+1], decorator_list=[])
    events, receipt, context = [], {'verified_all_queued_tail': True}, object()
    async def close_capture(supplied, label):
        assert supplied is context and label == 'latency-restored-zero-before-takeover'
        events.append('post-NULL content and sequence checked')
        if mode == 'latency-bad-tail':
            raise RuntimeFailure('Late queued PCM is invalid')
        return receipt
    capture = SimpleNamespace(close=lambda: events.append('default capture close'),
        pipeline=SimpleNamespace(get_state=lambda _: SimpleNamespace(state='NULL')),
        Gst=SimpleNamespace(State=SimpleNamespace(NULL='NULL')))
    report = {'cleanup': {}, 'latency_probe': {}}
    namespace = {'asyncio': asyncio, 'latency': SimpleNamespace(close_capture=close_capture) if mode != 'baseline' else None,
        'context': context, 'report': report, 'captures': {'A': capture}, 'A': 'A',
        'require': lambda value, message: None if value else (_ for _ in ()).throw(RuntimeFailure(message)),
        'producers': {'A': {'command': 'exact-current-a', 'account': 'exact-receiver'}},
        'publish_command': lambda *_args: events.append('new-source takeover published')}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), str(SOURCE), 'exec'), namespace)
    if mode == 'latency-bad-tail':
        with pytest.raises(RuntimeFailure, match='Late queued'):
            await namespace['retire']()
        assert events == ['post-NULL content and sequence checked'] and not report['cleanup']
    else:
        await namespace['retire']()
        assert events == ['default capture close' if mode == 'baseline' else 'post-NULL content and sequence checked',
                          'new-source takeover published']
        assert report['cleanup']['initial_a_capture_null'] is True
        if mode == 'latency':
            assert report['latency_probe']['restored_capture_before_takeover'] is receipt


@pytest.mark.asyncio
@pytest.mark.parametrize('speech_stress,zone_faults,latency_probe,name', [
    (False, False, False, 'baseline.json'), (True, False, False, 'shiri-v2-native-speech-stress-result.json'),
    (False, True, False, 'shiri-v2-native-zone-faults-result.json'),
    (False, False, True, 'shiri-v2-native-latency-probe-result.json'),
])
async def test_actual_whole_harness_early_preflight_error_writes_original_failure_without_cleanup_name_error(
        monkeypatch, tmp_path, speech_stress, zone_faults, latency_probe, name):
    import json
    spec = importlib.util.spec_from_file_location('group_early_cleanup_test', SOURCE)
    group = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(group)
    # Exercise the whole maintained function. This rejection occurs before
    # reading the ownership manifest, opening any FD, creating a unit or using
    # GI; a finally-only fixture cannot stand in for this admission evidence.
    monkeypatch.setattr(group, 'os', SimpleNamespace(geteuid=lambda: 501))
    monkeypatch.setattr(group, 'RESULT', tmp_path/'baseline.json')
    assert await group.run_check(speech_stress=speech_stress, zone_faults=zone_faults, latency_probe=latency_probe) == 1
    result = json.loads((tmp_path/name).read_text())
    assert result['passed'] is False and result['cleanup'] == {} and result['cleanup_errors'] == []
    assert result['failure'] == {'type': 'RuntimeFailure', 'message': 'Run explicitly as Linux root', 'causes': []}
    assert sorted(path.name for path in tmp_path.iterdir()) == [name]
