"""Actual source operation health and adversarial finite readiness receipts."""
from copy import deepcopy
from collections import deque
from dataclasses import replace
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

from shiri.runtime.native import NativeHandle
from shiri.runtime.system import RuntimeFailure
from shiri.runtime.timing import Kind
from test_native_audio import begin, controller as actual_controller
from test_native_speech_finite import encoded_reference
from test_native_speech_finite_supervisor import controller, session
from test_native_group_observation import harness as group


@pytest.mark.asyncio
async def test_health_source_operation_tracks_actual_acknowledged_barriers_not_ui_revision():
    native, _writer, client = actual_controller()
    operations = []
    def observed():
        health = native.health()
        assert health['source_operation_generation'] == native.operation_generation
        assert health['source_operation_generation'] == client.requests[-1][2]['operation_generation']
        operations.append(health['source_operation_generation'])
    try:
        await native.initialize()
        observed()
        native.control_intent(999)
        assert native.health()['control_revision'] == 999
        assert native.health()['source_operation_generation'] == operations[-1]
        handle = NativeHandle(native)
        grant = await native.begin(begin(), handle)
        observed()
        flushed = await native.message(replace(grant, kind=Kind.FLUSH, generation=grant.generation+1), handle)
        observed()
        await native.message(replace(flushed, kind=Kind.END), handle)
        observed()
        assert all(first < second for first, second in zip(operations, operations[1:], strict=False))
        assert native.health()['source']['owner'] is None
    finally:
        await native.close()
        native.mixer.close()


@pytest.fixture(scope='module')
def emitted():
    return encoded_reference()[0]


@pytest.mark.parametrize('role', ['idle', 'native'])
@pytest.mark.parametrize('key', ['incarnation', 'session_id', 'epoch', 'generation', 'operation_generation',
    'room_id', 'launch_generation', 'speech_id', 'action'])
@pytest.mark.parametrize('wrong_type', [False, True])
def test_all_nine_authenticated_echo_fields_are_exact_and_typed(tmp_path, emitted, role, key, wrong_type):
    record = session(tmp_path, emitted, role)
    observation = record['timer_ready_observation']
    ack = observation['worker_health']['speech_startup_authenticated_ready_ack']
    value = ack[key]
    ack[key] = (True if type(value) is int else 7) if wrong_type else (
        value+1 if type(value) is int else uuid4().hex if value is None or key != 'action' else 'release')
    record['timer_ready_ack'] = deepcopy(ack)
    with pytest.raises(RuntimeFailure):
        controller.validate_session(record, role, tmp_path, expected_uid=os.getuid())


@pytest.mark.parametrize('fault', ['current_api', 'api_request', 'retired', 'startup_error', 'pending_prefix',
    'operation', 'operation_bool', 'launch', 'nonce', 'stale_dispatch', 'setup_deadline', 'first_tick_claim',
    'warm_successor_nonce', 'pre_readiness_after_encoder', 'post_readiness_before_encoder', 'relabel_encoder', 'relabel_final'])
def test_complete_pcm_cannot_hide_stale_readiness_or_relabel_its_measurements(tmp_path, emitted, fault):
    record = session(tmp_path, emitted)
    health = record['timer_ready_observation']['worker_health']
    if fault == 'current_api':
        health['speech_session_id'] = str(uuid4())
    elif fault == 'api_request':
        health['speech_startup_identity']['request_id'] = str(uuid4())
    elif fault == 'retired':
        health['speech_startup_phase'] = 'retired'
    elif fault == 'startup_error':
        health['speech_startup_error'] = 'prefix_age'
    elif fault == 'pending_prefix':
        health['speech_startup_pending_frames'] = 960
    elif fault.startswith('operation'):
        health['source_operation_generation'] = True if fault == 'operation_bool' else health['source_operation_generation']+1
    elif fault == 'launch':
        record['original_target_route']['launch_generation'] = uuid4().hex
    elif fault == 'nonce':
        health['speech_startup_identity']['speech_id'] = uuid4().hex
    elif fault == 'stale_dispatch':
        health['speech_startup_first_mix_monotonic_ns'] = health['speech_startup_released_monotonic_ns']-100_000_000
        health['speech_startup_authenticated_ready_ack']['mixed_monotonic_ns'] = health['speech_startup_first_mix_monotonic_ns']
    elif fault == 'setup_deadline':
        health['speech_startup_released_monotonic_ns'] = health['speech_startup_started_monotonic_ns']+5_000_000_000
    elif fault == 'first_tick_claim':
        record['offer_scope'] = 'first ever player tick or physical render'
    elif fault == 'warm_successor_nonce':
        for key in ('pre_release_readiness', 'post_utterance_readiness'):
            value = record['rows'][1][key]['worker_health']
            nonce = uuid4().hex
            value['speech_startup_identity']['speech_id'] = nonce
            value['speech_startup_authenticated_ready_ack']['speech_id'] = nonce
    elif fault == 'pre_readiness_after_encoder':
        record['rows'][0]['pre_release_readiness']['observed_after_monotonic_ns'] += 10_000_000_000
    elif fault == 'post_readiness_before_encoder':
        record['rows'][0]['post_utterance_readiness']['observed_before_monotonic_ns'] = record['offer_response_monotonic_ns']+1
    else:
        record['rows'][0]['acknowledged_mix_to_encoder_ms' if fault == 'relabel_encoder' else 'acknowledged_mix_to_final_reference_origin_ms'] += 10_000
    with pytest.raises(RuntimeFailure):
        controller.validate_session(record, 'idle', tmp_path, expected_uid=os.getuid())


@pytest.mark.parametrize('blocked', [False, 'full', 'clock', 'caps'])
def test_actual_capture_callback_retains_original_metadata_only_for_admitted_buffers(blocked):
    capture = group.Capture.__new__(group.Capture)
    capture.absolute, capture.buffer_metadata, capture.pending = {}, {}, deque()
    capture.expected_base, capture.clock_offset_ns = 4_000_000_000, 31
    capture.last_packet_at, capture.max_packet_gap, capture.discontinuities, capture.capture_dropped = None, 0, 0, 0
    capture.Gst = SimpleNamespace(FlowReturn=SimpleNamespace(OK='ok', ERROR='error', EOS='eos'), BufferFlags=SimpleNamespace(DISCONT=1))
    capture.GstAudio = SimpleNamespace(AudioInfo=SimpleNamespace(new_from_caps=lambda caps: SimpleNamespace(bpf=4, channels=2, rate=44100 if blocked == 'caps' else 48000)))
    capture.pipeline = SimpleNamespace(get_base_time=lambda: capture.expected_base+(1 if blocked == 'clock' else 0))
    buffer = SimpleNamespace(offset=960, offset_end=1920, pts=20_000_000, duration=20_000_000,
        has_flags=lambda flag: False, get_size=lambda: 3840, extract_dup=lambda first, size: bytes(size))
    caps = SimpleNamespace(get_structure=lambda index: SimpleNamespace(get_value=lambda key: 'S16LE'), to_string=lambda: 'external-caps')
    sample = SimpleNamespace(get_buffer=lambda: buffer, get_caps=lambda: caps)
    sink = SimpleNamespace(emit=lambda operation: sample)
    if blocked == 'full':
        capture.pending.extend([None]*128)
    result = capture._sample(sink)
    if blocked:
        assert not capture.absolute and not capture.buffer_metadata
        assert result == ('ok' if blocked == 'full' else 'error')
        if blocked == 'full':
            assert capture.capture_dropped == 3840
    else:
        assert result == 'ok' and len(capture.pending) == 1
        at = capture.pending[0][0]
        assert capture.absolute[at] == capture.expected_base+buffer.pts-capture.clock_offset_ns
        assert capture.buffer_metadata[at] == {'offset': 960, 'offset_end': 1920, 'pts': 20_000_000, 'duration': 20_000_000, 'discont': False}
        buffer.offset = 99
        assert capture.buffer_metadata[at]['offset'] == 960


def timestamp_facts():
    # Controlled sample coordinates with representative millisecond capture
    # jitter. These are no physical timing or old-artifact qualification claim.
    contract = {'base_time_ns': 1, 'clock_offset_to_monotonic_ns': 0, 'negotiated_period_frames': 960,
        'period_uncertainty_ms': 20, 'jitter_budget_ns': 20_000_000, 'declared_before_offer_monotonic_ns': 1}
    records = []
    for index, jitter in enumerate((0, 5_111_792, 384958, -3_951_583, -661292)):
        pts = 10_000_000_000+index*20_000_000+jitter
        records.append({'absolute_pts_monotonic_ns': pts, 'callback_monotonic_ns': 10_100_000_000+index*20_000_000,
            'frames': 960, 'gst_metadata': {'offset': index*960, 'offset_end': (index+1)*960,
                'pts': pts-1, 'duration': 20_000_000, 'discont': False}})
    return contract, records


def test_exact_sample_offsets_preserve_truthful_raw_pts_jitter_and_aligned_origin():
    contract, records = timestamp_facts()
    origin, quality = controller.finite.validate_final_timestamps(records, 5*3840, contract, start_frame=1440)
    assert origin == records[1]['absolute_pts_monotonic_ns']+10_000_000
    assert quality['verified_frames'] == 4800 and quality['verified_buffers'] == 5
    assert quality['adjacent_jitter_max_ns'] == 5_111_792
    assert quality['cumulative_jitter_min_ns'] == -3_951_583
    assert quality['jitter_budget_ns'] == 20_000_000


@pytest.mark.parametrize('fault', ['gap', 'repeat', 'duration', 'discont', 'callback', 'transform',
    'clock_step', 'clock_drift', 'invented_budget', 'missing_metadata', 'bytes', 'period'])
def test_jitter_acceptance_cannot_hide_actual_frame_loss_or_unbounded_clock_mapping(fault):
    contract, records = timestamp_facts()
    byte_count = 5*3840
    if fault in {'gap', 'repeat'}:
        records[2]['gst_metadata']['offset'] += 960 if fault == 'gap' else -960
        records[2]['gst_metadata']['offset_end'] += 960 if fault == 'gap' else -960
    elif fault == 'duration':
        records[2]['gst_metadata']['duration'] += 3
    elif fault == 'discont':
        records[2]['gst_metadata']['discont'] = True
    elif fault == 'callback':
        records[2]['callback_monotonic_ns'] = records[1]['callback_monotonic_ns']
    elif fault == 'transform':
        records[2]['absolute_pts_monotonic_ns'] += 1
    elif fault in {'clock_step', 'clock_drift'}:
        for index, record in enumerate(records):
            drift = 21_000_000 if fault == 'clock_step' and index >= 2 else index*6_000_000 if fault == 'clock_drift' else 0
            record['absolute_pts_monotonic_ns'] = 10_000_000_000+index*20_000_000+drift
            record['gst_metadata']['pts'] = record['absolute_pts_monotonic_ns']-1
    elif fault == 'invented_budget':
        contract['jitter_budget_ns'] += 1
    elif fault == 'missing_metadata':
        records[2].pop('gst_metadata')
    elif fault == 'bytes':
        byte_count -= 4
    else:
        contract['period_uncertainty_ms'] += 1
    with pytest.raises(RuntimeFailure):
        controller.finite.validate_final_timestamps(records, byte_count, contract, start_frame=1440)
