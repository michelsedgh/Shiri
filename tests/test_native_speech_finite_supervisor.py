"""Separate finite fixture admission/lifecycle; real Opus/artifacts, external facts simulated."""
import ast
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from shiri.runtime.system import RuntimeFailure

np = pytest.importorskip('numpy')
pytest.importorskip('aiortc')
ROOT = Path(__file__).parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT/relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


controller = load('tested_finite_controller', 'tests/linux/run_native_speech_finite.py')
supervisor = controller.supervisor
reference_tests = load('finite_supervisor_emitted_reference', 'tests/test_native_speech_finite.py')
legacy_tests = load('finite_supervisor_external_epoch_facts', 'tests/test_native_latency_epoch_supervisor.py')
epoch_tests = load('finite_supervisor_external_calendars', 'tests/test_native_latency_epochs.py')


@pytest.fixture(scope='module')
def emitted():
    return reference_tests.encoded_reference()[0]


def refresh_ready(record, role):
    finite = controller.finite
    route = record['original_target_route']
    started = record['offer_request_monotonic_ns']+1000
    released = record['offer_response_monotonic_ns']-1000
    prepared = started if role == 'idle' else 0
    mixed = released-1000
    owner = route['source_owner']
    nonce = uuid4().hex
    identity = {key: record[key] for key in ('session_id', 'request_id')}
    ack = {'incarnation': route['source_identity']['incarnation'].replace('-', ''),
        'session_id': None if role == 'idle' else owner['session_id'].replace('-', ''),
        'epoch': route['source_identity']['epoch'], 'generation': 1 if role == 'idle' else route['native_generation'],
        'operation_generation': route['source_operation_generation'], 'room_id': route['source_identity']['zone_id'].replace('-', ''),
        'launch_generation': route['launch_generation'], 'speech_id': nonce, 'action': 'ready' if role == 'idle' else 'observe',
        'connected': True, 'ready': True, 'prepared_monotonic_ns': prepared, 'mixed_monotonic_ns': mixed, 'output_count': 1}
    health = {'ready': True, 'error': None, 'source': {**deepcopy(route['source_identity']), 'ready': True, 'error': None},
        'source_operation_generation': route['source_operation_generation'], 'native_generation': route['native_generation'],
        'speech_session_id': identity['session_id'], 'speech_startup_identity': {'speech_id': nonce, **identity},
        'speech_startup_authenticated_ready_ack': deepcopy(ack), 'speech_startup_phase': 'ready', 'speech_startup_error': None,
        'speech_startup_started_monotonic_ns': started, 'speech_startup_prepared_monotonic_ns': prepared,
        'speech_startup_first_mix_monotonic_ns': mixed, 'speech_startup_released_monotonic_ns': released,
        'speech_startup_pending_frames': 0, 'speech_startup_setup_budget_ns': 5_000_000_000, 'speech_startup_performance_qualified': False}
    def observed(at):
        return {'observed_before_monotonic_ns': at, 'observed_after_monotonic_ns': at+1, 'worker_health': deepcopy(health)}
    record.update(timer_ready_ack=ack, timer_ready_observation=observed(record['offer_response_monotonic_ns']+1000), offer_scope=finite.READY_SCOPE)
    record['timer_ready_measurements'] = finite.validate_ready_observation(record['timer_ready_observation'], role, route, identity,
        offer_request_ns=record['offer_request_monotonic_ns'], offer_response_ns=record['offer_response_monotonic_ns'])
    for row in record['rows']:
        first = row['source']['first_source_frame_to_encoder_monotonic_ns']
        row.update(pre_release_readiness=observed(first-1000), post_utterance_readiness=observed(first+3_000_000_000),
            acknowledged_mix_to_encoder_ms=(first-mixed)/1e6,
            acknowledged_mix_to_final_reference_origin_ms=(row['final_reference_origin_monotonic_ns']-mixed)/1e6)


def session(directory, emitted, role='idle'):
    finite = controller.finite
    record = {'role': role, 'session_id': emitted['session_id'], 'request_id': str(uuid4()), 'cold_utterance_completeness_passed': True,
              'cold_utterance_completeness_status': controller.COMPLETE, 'cleanup_errors': [],
              'offer_request_monotonic_ns': 9_000_000_000, 'offer_response_monotonic_ns': 9_001_000_000,
              'timer_ready_ack': None, 'player_before_offer': {'state': 'stop' if role == 'idle' else 'play', 'item_id': 1}, 'rows': [],
              'capture_timing_contract': {'base_time_ns': 1, 'clock_offset_to_monotonic_ns': 0, 'negotiated_period_frames': 960,
                  'period_uncertainty_ms': 20, 'jitter_budget_ns': 20_000_000, 'declared_before_offer_monotonic_ns': 8_999_000_000},
              'original_target_route': {'source_owner': None if role == 'idle' else {'session_id': 'original'},
                  'source_operation_generation': 7, 'native_generation': None if role == 'idle' else 1, 'launch_generation': uuid4().hex,
                  'source_identity': {'zone_id': epoch_tests.epoch.A, 'incarnation': str(uuid4()), 'epoch': 0,
                                      'owner': None if role == 'idle' else {'session_id': 'original'}},
                  'selected_ids': ['0'], 'selected_output': {'id': '0', 'protocol': 'alsa', 'selected': True, 'offset_ms': 0},
                  'local_pin': {'device': 0, 'subdevice': 7}, 'units': {'owntone': {'invocation': 'original'}}}}
    if role == 'native':
        source = record['original_target_route']['source_identity']
        owner = {'zone_id': source['zone_id'], 'incarnation': source['incarnation'], 'epoch': 1,
                 'protocol': 'airplay2', 'session_id': str(uuid4())}
        source.update(epoch=1, owner=deepcopy(owner))
        record['original_target_route']['source_owner'] = owner
    for index, kind in enumerate(('cold', 'warm')):
        reference = deepcopy(emitted)
        reference['source']['first_source_frame_to_encoder_monotonic_ns'] += index*10_000_000_000
        reference['source']['rtp_frame_index'] += index*finite.RATE*2
        reference['source']['utterance'] = index+1
        data = reference_tests.observed(reference, start=1440, music=role == 'native').tobytes()
        padding = -len(data)//4 % finite.FRAMES
        data += bytes(padding*4)
        quiet_carrier, music_baseline, capture_first_frame = None, None, None
        if role == 'native':
            values = 8192*np.sin(2*np.pi*440*np.arange(finite.QUIET_FRAMES)/finite.RATE)
            music_baseline = np.repeat(values.astype('<i2')[:, None], 2, axis=1).tobytes()
            quiet_carrier = finite.quiet_module().freeze_carrier(music_baseline, first_frame=0, duck_gain=.2)
            capture_first_frame = finite.QUIET_FRAMES+index*finite.RATE*10
            if padding:
                values = 1638*np.sin(2*np.pi*440*np.arange(len(data)//4-padding, len(data)//4)/finite.RATE)
                data = data[:-padding*4]+np.repeat(values.astype('<i2')[:, None], 2, axis=1).tobytes()
            baseline_timings = [{'absolute_pts_monotonic_ns': 8_790_000_000+at*1_000_000_000//finite.RATE,
                'callback_monotonic_ns': 8_791_000_000+at*1_000_000_000//finite.RATE, 'frames': finite.FRAMES,
                'gst_metadata': {'offset': at, 'offset_end': at+finite.FRAMES,
                    'pts': 8_790_000_000+at*1_000_000_000//finite.RATE-1, 'duration': 20_000_000, 'discont': False}}
                for at in range(0, finite.QUIET_FRAMES, finite.FRAMES)]
            _, quality = finite.validate_final_timestamps(baseline_timings, len(music_baseline), record['capture_timing_contract'], start_frame=0)
            record['music_carrier_contract'] = {'declared_before_offer_monotonic_ns': 8_990_000_000,
                'original_target_route': deepcopy(record['original_target_route']), 'carrier': quiet_carrier.evidence(),
                'timings': baseline_timings, 'timestamp_quality': quality}
        first = reference['source']['first_source_frame_to_encoder_monotonic_ns']
        clocks = [{'absolute_pts_monotonic_ns': first+500_000_000+at*1_000_000_000//finite.RATE,
                   'callback_monotonic_ns': first+501_000_000+at*1_000_000_000//finite.RATE,
                   'frames': min(finite.FRAMES, len(data)//4-at),
                   'gst_metadata': {'offset': at, 'offset_end': at+finite.FRAMES, 'pts': first+500_000_000+at*1_000_000_000//finite.RATE-1,
                                    'duration': 20_000_000, 'discont': False}} for at in range(0, len(data)//4, finite.FRAMES)]
        if role == 'native':
            for clock in clocks:
                clock['gst_metadata']['offset'] += capture_first_frame
                clock['gst_metadata']['offset_end'] += capture_first_frame
        row = finite.verify_complete(data, reference, start_bounds=(0, 2400), music=role == 'native',
            quiet_carrier=quiet_carrier, capture_first_frame=capture_first_frame)
        _origin, quality = finite.validate_final_timestamps(clocks, len(data), record['capture_timing_contract'], start_frame=row['alignment_start_frame'])
        row.update(capture_timestamp_quality=quality, kind=f'{kind}_{role}', pre_release_player={'state': 'play', 'item_id': 1},
                   player_after_utterance={'state': 'play', 'item_id': 1},
                   pre_release_target_route=deepcopy(record['original_target_route']),
                   post_utterance_target_route=deepcopy(record['original_target_route']),
                   warm_player_samples=[{'monotonic_ns': first, 'player': {'state': 'play', 'item_id': 1}},
                                        {'monotonic_ns': first+1_000_000, 'player': {'state': 'play', 'item_id': 1}}] if index else [],
                   utterance_request_monotonic_ns=first-20_000_000,
                   same_session_for_cold_and_warm=True, final_reference_origin_monotonic_ns=first+530_000_000,
                   source_to_final_reference_origin_ms=530., offer_request_to_encoder_ms=(first-9_000_000_000)/1e6,
                   utterance_request_to_encoder_ms=20., offer_request_to_final_reference_origin_ms=(first+530_000_000-9_000_000_000)/1e6,
                   artifacts=finite.retain_diagnostic(directory, reference, data, clocks, music_baseline=music_baseline))
        if role == 'native':
            row['capture_first_frame'] = capture_first_frame
        record['rows'].append(row)
    refresh_ready(record, role)
    return record


@pytest.mark.parametrize('role', ['idle', 'native'])
def test_independently_recompute_exact_full_finite_reference_artifacts_and_request_clocks(tmp_path, emitted, role):
    record = session(tmp_path, emitted, role)
    verified = controller.validate_session(record, role, tmp_path, expected_uid=os.getuid())
    assert [row['kind'] for row in verified] == [f'cold_{role}', f'warm_{role}']
    assert all(row['source_to_final_reference_origin_ms'] == 530. for row in verified)
    assert all(row['cold_utterance_completeness_passed'] is True and row['speech_latency_performance_passed'] is False for row in verified)


@pytest.mark.parametrize('fault', ['primed', 'warm_missing', 'role', 'peer', 'complete', 'cleanup', 'performance',
    'minimum_claim', 'clock', 'frames', 'prefix', 'tail', 'reference_hash', 'artifact_escape', 'artifact_link',
    'artifact_replaced', 'artifact_shared', 'timestamp_gap', 'timestamp_relabel', 'timer_ready_ack'])
def test_finite_receipt_cannot_promote_primed_partial_relabelled_or_bad_artifacts(tmp_path, emitted, fault):
    record = session(tmp_path, emitted)
    row = record['rows'][0]
    if fault == 'primed':
        record['player_before_offer']['state'] = 'play'
    elif fault == 'warm_missing':
        record['rows'].pop()
    elif fault == 'role':
        record['role'] = 'native'
    elif fault == 'peer':
        row['session_id'] = str(uuid4())
    elif fault == 'complete':
        row['cold_utterance_completeness_passed'] = False
    elif fault == 'cleanup':
        record['cleanup_errors'] = ['peer survived']
    elif fault == 'performance':
        row['speech_latency_performance_passed'] = True
    elif fault == 'minimum_claim':
        row['speech_latency_performance_status'] = 'minimum_verified'
    elif fault == 'clock':
        row['source_to_final_reference_origin_ms'] += 10_000
    elif fault == 'frames':
        row['verified_reference_frames'] -= 960
    elif fault in {'prefix', 'tail'}:
        item = row['artifacts']['final_pcm']
        path = Path(item['path'])
        data = bytearray(path.read_bytes())
        start = 1440*4 if fault == 'prefix' else (1440+controller.finite.BODY_FRAMES-960)*4
        count = 17280*4 if fault == 'prefix' else 960*4
        data[start:start+count] = bytes(count)
        path.write_bytes(data)
        import hashlib
        item['sha256'] = hashlib.sha256(data).hexdigest()  # Self-consistent forged capture is still rejected.
    elif fault == 'reference_hash':
        row['decoded_pcm_sha256'] = 'f'*64
    elif fault == 'artifact_escape':
        row['artifacts']['final_pcm']['path'] = str(tmp_path.parent/'elsewhere.pcm')
    elif fault == 'artifact_link':
        item = row['artifacts']['final_pcm']
        os.link(item['path'], tmp_path/'hardlink.pcm')
    elif fault == 'artifact_replaced':
        path = Path(row['artifacts']['final_pcm']['path'])
        path.unlink()
        path.symlink_to(row['artifacts']['decoded_reference']['path'])
    elif fault == 'artifact_shared':
        record['rows'][1]['artifacts'] = deepcopy(row['artifacts'])
    elif fault == 'timestamp_gap':
        item = row['artifacts']['final_timestamps']
        path = Path(item['path'])
        clocks = json.loads(path.read_text())
        clocks[2]['gst_metadata']['offset'] += 1
        clocks[2]['gst_metadata']['offset_end'] += 1
        data = (json.dumps(clocks)+'\n').encode()
        path.write_bytes(data)
        import hashlib
        item.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    elif fault == 'timestamp_relabel':
        row['final_reference_origin_monotonic_ns'] += 10_000_000_000
    else:
        record['timer_ready_ack'] = {'ready': True}
    with pytest.raises((RuntimeFailure, FileNotFoundError)):
        controller.validate_session(record, 'idle', tmp_path, expected_uid=os.getuid())


@pytest.mark.asyncio
@pytest.mark.parametrize('role', ['idle', 'native'])
async def test_real_coroutine_finite_seam_calls_original_observer_without_old_markers(monkeypatch, role):
    path = ROOT/'tests/linux/native_latency_epochs.py'
    tree = ast.parse(path.read_text())
    exercise = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'exercise')
    branch = next(node for node in exercise.body if isinstance(node, ast.If) and 'finite_speech' in ast.unparse(node.test))
    code = ast.fix_missing_locations(ast.Module(body=[ast.AsyncFunctionDef(name='seam', args=ast.arguments(posonlyargs=[],
        args=[], kwonlyargs=[], kw_defaults=[], defaults=[]), body=[branch], decorator_list=[])], type_ignores=[]))
    untouched_capture, original_guard = object(), object()
    context = SimpleNamespace(finite_speech=True, report={'mode': 'finite_speech'}, phase=SimpleNamespace(phase=role),
        captures={epoch_tests.epoch.B: untouched_capture}, producers={epoch_tests.epoch.B: object()},
        evidence={'rows': [{'kind': 'pending_old_matrix'}]}, pcm_guards={epoch_tests.epoch.B: original_guard})
    if role == 'native':
        context.captures[epoch_tests.epoch.A] = object()
        context.producers[epoch_tests.epoch.A] = object()
    observer = SimpleNamespace(sample=AsyncMock())
    original_observer = observer
    calls = []
    def start_capture(current, pin):
        assert current is context and pin == {'actual': 'pin'} and epoch_tests.epoch.A not in context.producers
        calls.append('capture-read-only-start')
        current.captures[epoch_tests.epoch.A] = object()
    async def measure(current, observed, *, role):
        assert current is context and observed is original_observer
        assert current.captures[epoch_tests.epoch.B] is untouched_capture
        assert current.pcm_guards[epoch_tests.epoch.B] is original_guard
        calls.append(f'finite-cold-then-warm-{role}')
        return {'cold_utterance_completeness_passed': True}
    fake_finite = SimpleNamespace(measure_session=measure)
    old_module = importlib.util.module_from_spec
    monkeypatch.setattr(importlib.util, 'module_from_spec', lambda spec: fake_finite if spec.name == 'native_epoch_finite_speech' else old_module(spec))
    old_spec = importlib.util.spec_from_file_location
    def spec_from_location(name, location):
        return SimpleNamespace(name=name, loader=SimpleNamespace(exec_module=lambda module: None)) if name == 'native_epoch_finite_speech' else old_spec(name, location)
    monkeypatch.setattr(importlib.util, 'spec_from_file_location', spec_from_location)
    namespace = {'context': context, 'observer': observer, 'configuration': {'pin': {'actual': 'pin'}},
        'latency': SimpleNamespace(start_capture=start_capture), 'A': epoch_tests.epoch.A, '__file__': str(path), 'require': controller.require}
    exec(compile(code, str(path), 'exec'), namespace)
    assert await namespace['seam']() is original_observer
    assert calls == (['capture-read-only-start'] if role == 'idle' else [])+[f'finite-cold-then-warm-{role}']
    assert context.evidence['rows'] == [] and 'speech_latency_measurements' not in context.evidence
    assert context.evidence['diagnostic'] == 'finite_emitted_opus'
    assert context.evidence['speech_latency_performance_passed'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('kwargs', [{'finite_speech': True}, {'finite_speech': 1},
    {'finite_speech': True, 'speech_stress': True}])
async def test_direct_finite_child_requires_explicit_epoch_and_exclusive_boolean_mode(kwargs):
    with pytest.raises(RuntimeFailure):
        await supervisor.group.run_check(**kwargs)


@pytest.fixture
def lab(tmp_path, monkeypatch):
    monkeypatch.setattr(legacy_tests, 'supervisor', supervisor)
    state = legacy_tests.lab.__wrapped__(tmp_path, monkeypatch)
    monkeypatch.setattr(supervisor, 'source_receipts', lambda project, **kwargs: deepcopy(state.source))
    return state


@pytest.mark.asyncio
async def test_finite_exact_parent_lifecycle_launches_explicit_child_without_legacy_marker_flag(lab):
    def finite_report(child, report, launch):
        report['mode'] = 'finite_speech'
        assert '--finite-speech' in launch['args'] and '--latency-probe' not in launch['args']
    lab.hooks.append(finite_report)
    result = await supervisor.run_epoch(legacy_tests.Phase(), lab.source, lab.admission,
        lab.manifest['installation_id'], lab.baseline, lab.protected, runner=lab.runner, finite_speech=True)
    assert result['passed'], result
    assert all(value is True for value in result['cleanup'].values()) and not list(lab.nodes.iterdir())
    for descriptor in lab.launches[0]['fds']:
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.asyncio
async def test_finite_parent_does_not_adopt_legacy_marker_report(lab):
    result = await supervisor.run_epoch(legacy_tests.Phase(), lab.source, lab.admission,
        lab.manifest['installation_id'], lab.baseline, lab.protected, runner=lab.runner, finite_speech=True)
    assert result['passed'] is False and result['inner_report']['mode'] == 'latency_probe'
    assert result['cleanup']['direct_child_reaped'] and result['cleanup']['parent_namespace_deleted']


@pytest.mark.asyncio
async def test_finite_controller_refuses_without_explicit_root_lab_and_never_spawns(tmp_path, monkeypatch):
    monkeypatch.setattr(controller, 'RESULT', tmp_path/'result.json')
    monkeypatch.setattr(controller.group, 'NATIVE_LAB', None)
    spawn = AsyncMock()
    monkeypatch.setattr(supervisor, 'run_epoch', spawn)
    assert await controller.run() == 1
    report = json.loads((tmp_path/'result.json').read_text())
    assert report['cold_utterance_completeness_passed'] is False and report['fixtures'] == []
    spawn.assert_not_called()



def fixture_report(tmp_path, emitted, role):
    reports, phases = epoch_tests.reports()
    index = 2 if role == 'idle' else 3
    report, phase = reports[index], phases[index]
    now = datetime.now(timezone.utc).isoformat()
    report.update(mode='finite_speech', started_at=now, finished_at=now)
    directory = tmp_path/f'native-group-{phase.epoch_id}'
    directory.mkdir(mode=0o700)
    record = session(directory, emitted, role)
    for item in report['latency_epoch']['independent_capture_baselines'].values():
        item.update(negotiated_period_frames=960, period_uncertainty_ms=20,
            capture={'common_clock_base_ns': 1, 'clock_offset_to_monotonic_ns': 0})
    report.update(artifacts={'private_directory': str(directory)}, finite_speech_diagnostics=[record])
    epoch = report['latency_epoch']
    epoch.pop('speech_latency_measurements')
    epoch.update(diagnostic='finite_emitted_opus', rows=[], finite_speech=record,
                 cold_utterance_completeness_passed=True, cold_utterance_completeness_status=controller.COMPLETE)
    record['original_target_route']['source_identity']['zone_id'] = epoch_tests.epoch.A
    epoch['initial_unit_identities'] = {epoch_tests.epoch.A: deepcopy(record['original_target_route']['units'])}
    if role == 'native':
        epoch['target_original']['source_owner'].update(zone_id=epoch_tests.epoch.A, protocol='airplay2')
        record['original_target_route']['source_owner'] = deepcopy(epoch['target_original']['source_owner'])
        record['original_target_route']['source_identity'].update(owner=deepcopy(epoch['target_original']['source_owner']),
            incarnation=epoch['target_original']['source_owner']['incarnation'], epoch=epoch['target_original']['source_owner']['epoch'])
    if role == 'native':
        record['music_carrier_contract']['original_target_route'] = deepcopy(record['original_target_route'])
    for row in record['rows']:
        row['pre_release_target_route'] = deepcopy(record['original_target_route'])
        row['post_utterance_target_route'] = deepcopy(record['original_target_route'])
    refresh_ready(record, role)
    return report, phase


@pytest.mark.parametrize('role', ['idle', 'native'])
def test_exact_finite_fixture_recomputes_calibration_original_music_tail_and_full_utterances(tmp_path, emitted, role):
    report, phase = fixture_report(tmp_path, emitted, role)
    proof = controller.validate_fixture(report, phase, report['native_lab'], expected_uid=os.getuid(), work=tmp_path)
    assert len(proof['rows']) == 2 and all(row['capture_corrected_source_to_final_reference_origin_ms'] == 530. for row in proof['rows'])
    assert all(row['capture_corrected_source_to_final_bracket_ms'] == [503., 557.] for row in proof['rows'])
    assert all(row['speech_latency_performance_passed'] is False for row in proof['rows'])
    assert report['latency_epoch']['rows'] == [] and 'latency_matrix' not in report


@pytest.mark.parametrize('fault', ['mode', 'uuid', 'calibration', 'h', 'b', 'offset', 'calendar', 'bracket',
    'raw_alignment', 'early_alignment', 'late_alignment', 'drift', 'untouched', 'pcm_failure', 'stop',
    'capture_null', 'dropped', 'frame_calendar', 'producer', 'daemon', 'duplicate_session', 'matrix_rows', 'matrix_measurements'])
def test_finite_fixture_cannot_waive_original_plan_timing_continuity_or_cleanup(tmp_path, emitted, fault):
    report, phase = fixture_report(tmp_path, emitted, 'native')
    epoch = report['latency_epoch']
    if fault == 'mode':
        report['mode'] = 'latency_probe'
    elif fault == 'uuid':
        epoch['id'] = str(uuid4())
    elif fault == 'calibration':
        epoch['independent_capture_baselines'][epoch_tests.epoch.A]['cleanup']['capture_null'] = False
    elif fault == 'h':
        epoch['frozen_plan']['common_horizon_ns'] -= 1
    elif fault == 'b':
        epoch['frozen_plan']['output_buffers_ms'][epoch_tests.epoch.A] = 750
    elif fault == 'offset':
        epoch['frozen_plan']['saved_intent'][0]['speakers'][0]['offset_ms'] = 1
    elif fault == 'calendar':
        epoch['native_calendars'][epoch_tests.epoch.A]['declared_final_origin_ns'] += 1
    elif fault == 'bracket':
        epoch['native_calendars'][epoch_tests.epoch.A]['horizon']['corrected_horizon_error_ms'] = 500
    elif fault == 'raw_alignment':
        epoch['configured_offset_alignment']['raw_b_minus_a']['relative_offset_ms'] = 3
    elif fault in {'early_alignment', 'late_alignment'}:
        epoch['configured_offset_alignment']['residual'][fault.split('_')[0]]['relative_offset_ms'] = 3
    elif fault == 'drift':
        epoch['configured_offset_alignment']['residual']['drift_ms'] = 1
    elif fault == 'untouched':
        epoch['untouched']['original_capture_preserved'] = False
    elif fault == 'pcm_failure':
        epoch['untouched']['pcm']['failed_block'] = {'error': 'prior corruption'}
    elif fault == 'stop':
        epoch['stop_boundary']['requested_monotonic_ns'] = True
    elif fault == 'capture_null':
        epoch['tail']['both_captures_null'] = False
    elif fault == 'dropped':
        epoch['tail']['captures'][epoch_tests.epoch.B]['capture']['capture_dropped_bytes'] = 4
    elif fault == 'frame_calendar':
        epoch['tail']['captures'][epoch_tests.epoch.B]['capture']['observed_bytes'] += 4
    elif fault == 'producer':
        epoch['producer_retirement'][epoch_tests.epoch.B]['retired'] = False
    elif fault == 'daemon':
        epoch['daemon_retirement']['empty_ownership_verified'] = False
    elif fault == 'duplicate_session':
        report['finite_speech_diagnostics'].append(deepcopy(epoch['finite_speech']))
    elif fault == 'matrix_rows':
        epoch['rows'] = [{'kind': 'cold_idle', 'passed': True}]
    else:
        epoch['speech_latency_measurements'] = []
    with pytest.raises(RuntimeFailure):
        controller.validate_fixture(report, phase, report['native_lab'], expected_uid=os.getuid(), work=tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize('failed', [False, True])
async def test_two_fixture_controller_retains_partial_failure_and_never_promotes_matrix(lab, tmp_path, monkeypatch, failed):
    monkeypatch.setattr(controller, 'RESULT', tmp_path/'finite-result.json')
    monkeypatch.setattr(controller, 'boot_id', lambda: 'test-boot')
    calls = []
    async def execute(phase, *args, **kwargs):
        assert kwargs == {'finite_speech': True}
        calls.append(phase)
        return {'passed': not failed, 'inner_report': {'actual': phase.receipt()}, 'cleanup': {'done': True}}
    async def host():
        return deepcopy(lab.baseline)
    def verify(report, phase, admission):
        return {'phase': phase.receipt(), 'rows': [{'session_id': str(uuid4())}, {}],
            'untouched': {'original_unit_identities': {'owntone': {'invocation_id': str(uuid4())}},
                          'source_owner': {'session_id': str(uuid4()), 'epoch': 1, 'incarnation': str(uuid4())}}}
    monkeypatch.setattr(supervisor, 'run_epoch', execute)
    monkeypatch.setattr(controller, 'validate_fixture', verify)
    assert await controller.run(0) == (1 if failed else 0)
    report = json.loads((tmp_path/'finite-result.json').read_text())
    assert len(calls) == len(report['fixtures']) == (1 if failed else 2)
    assert [phase.phase for phase in calls] == (['idle'] if failed else ['idle', 'native'])
    assert len({phase.epoch_id for phase in calls}) == len(calls)
    assert report['cold_utterance_completeness_passed'] is (not failed)
    assert report['speech_latency_performance_passed'] is False and 'latency_matrix' not in report
    assert report['cleanup'] == {'source_lab_ownership_preserved': True, 'original_host_protected_preserved': True}


@pytest.mark.parametrize('fault', ['warm_stopped', 'cold_stopped_after_completion', 'warm_item', 'warm_route', 'warm_source', 'warm_unit', 'warm_stopped_after_capture', 'warm_item_after_capture'])
def test_controller_rejects_complete_waveforms_that_cannot_prove_already_warm_same_driver(tmp_path, emitted, fault):
    record = session(tmp_path, emitted)
    if fault == 'warm_stopped':
        record['rows'][1]['pre_release_player']['state'] = 'stop'
    elif fault == 'cold_stopped_after_completion':
        record['rows'][0]['player_after_utterance']['state'] = 'stop'
    elif fault == 'warm_item':
        record['rows'][1]['pre_release_player']['item_id'] = 2
    elif fault == 'warm_stopped_after_capture':
        record['rows'][1]['player_after_utterance']['state'] = 'stop'
    elif fault == 'warm_item_after_capture':
        record['rows'][1]['player_after_utterance']['item_id'] = 2
    elif fault == 'warm_route':
        record['rows'][1]['pre_release_target_route']['selected_output']['offset_ms'] = 1
    elif fault == 'warm_source':
        record['rows'][1]['pre_release_target_route']['source_owner'] = {'session_id': 'replacement'}
    else:
        record['rows'][1]['pre_release_target_route']['units']['owntone']['invocation'] = 'replacement'
    with pytest.raises(RuntimeFailure, match='warm row|held target route'):
        controller.validate_session(record, 'idle', tmp_path, expected_uid=os.getuid())


@pytest.mark.parametrize('fault', ['baseline_missing', 'late_declaration', 'other_route', 'coefficient',
    'baseline_origin', 'capture_origin', 'late_callback', 'baseline_pcm', 'timestamp_quality'])
def test_native_carrier_supervisor_recomputes_and_binds_preoffer_bytes_clocks_and_frame_calendar(tmp_path, emitted, fault):
    record = session(tmp_path, emitted, 'native')
    carrier = record['music_carrier_contract']
    if fault == 'baseline_missing':
        record['rows'][0]['artifacts'].pop('music_baseline')
    elif fault == 'late_declaration':
        carrier['declared_before_offer_monotonic_ns'] = record['offer_request_monotonic_ns']+1
    elif fault == 'other_route':
        carrier['original_target_route']['source_operation_generation'] += 1
    elif fault == 'coefficient':
        carrier['carrier']['coefficients'][0] += 30
    elif fault == 'baseline_origin':
        carrier['carrier']['first_frame'] += 1
    elif fault == 'capture_origin':
        record['rows'][0]['capture_first_frame'] += 1
    elif fault == 'late_callback':
        carrier['timings'][-1]['callback_monotonic_ns'] = carrier['declared_before_offer_monotonic_ns']+1
    elif fault == 'timestamp_quality':
        carrier['timestamp_quality']['verified_frames'] -= 1
    else:
        path = Path(record['rows'][0]['artifacts']['music_baseline']['path'])
        path.write_bytes(bytes(len(path.read_bytes())))
    with pytest.raises(RuntimeFailure):
        controller.validate_session(record, 'native', tmp_path, expected_uid=os.getuid())
