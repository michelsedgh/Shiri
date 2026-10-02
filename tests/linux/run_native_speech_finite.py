#!/usr/bin/env python3
"""Explicit finite cold/warm speech diagnostic in independently cleaned Linux fixtures.

Uses the maintained owned epoch supervisor. This result cannot promote the
separate repeating-marker latency matrix or claim a release latency budget.
"""
from __future__ import annotations

# Bounded manual fixture/artifact observations, never production operations.
# ruff: noqa: ASYNC240
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import stat
import sys
from uuid import UUID, uuid4

import numpy as np

from shiri.domain import Room
from shiri.runtime.latency import latency_plan
from shiri.runtime.system import RuntimeFailure, atomic_json, boot_id

HERE = Path(__file__).resolve()

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

supervisor = load('finite_owned_epoch_supervisor', HERE.with_name('run_native_latency_probe.py'))
finite = load('finite_owned_opus_reference', HERE.with_name('native_speech_finite.py'))
group = supervisor.group
RESULT = group.WORK/'shiri-v2-native-finite-speech-supervisor-result.json'
FIXTURE_SECONDS = 1200
EXTERNAL_SECONDS = 1380
COMPLETE = 'finite_emitted_opus_prefix_body_tail_verified'
PERFORMANCE = 'pending_declared_and_characterized_software_budget'


def require(value, message):
    if not value:
        raise RuntimeFailure(message)


def read_artifact(item, directory, *, expected_uid=0, limit):
    """Exact original diagnostic artifact: no links, relocation or mutable read."""
    require(type(item) is dict and set(item) == {'path', 'bytes', 'sha256'}
            and type(item['bytes']) is int and 0 < item['bytes'] <= limit
            and type(item['sha256']) is str and len(item['sha256']) == 64,
            'Finite artifact declaration is malformed or unbounded')
    path = Path(item['path'])
    require(path.is_absolute() and path.parent == directory and path.name.startswith('finite-speech-')
            and path.resolve(strict=True) == path, 'Finite artifact escaped its held fixture directory')
    parent = directory.lstat()
    require(stat.S_ISDIR(parent.st_mode) and parent.st_uid == expected_uid and not parent.st_mode & 0o022,
            'Finite artifact parent is not exactly protected')
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and before.st_uid == expected_uid and before.st_nlink == 1
            and stat.S_IMODE(before.st_mode) == 0o600 and before.st_size == item['bytes'],
            'Finite artifact is not its exact bounded single-link private file')
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        initial = os.fstat(descriptor)
        require(supervisor.report_signature(initial) == supervisor.report_signature(before), 'Finite artifact replaced before read')
        blocks, size = [], 0
        while data := os.read(descriptor, 65536):
            blocks.append(data)
            size += len(data)
            require(size <= limit, 'Finite artifact grew beyond its explicit bound')
        require(supervisor.report_signature(initial) == supervisor.report_signature(os.fstat(descriptor))
                == supervisor.report_signature(path.lstat()) and size == before.st_size,
                'Finite artifact changed during its held read')
        data = b''.join(blocks)
        require(hashlib.sha256(data).hexdigest() == item['sha256'], 'Finite artifact bytes differ from the original receipt')
        return data
    finally:
        os.close(descriptor)


def validate_session(record, role, directory, *, expected_uid=0):
    require(type(record) is dict and record.get('role') == role
            and record.get('cold_utterance_completeness_passed') is True
            and record.get('cold_utterance_completeness_status') == COMPLETE
            and record.get('cleanup_errors') == [] and 'failure' not in record,
            'Finite session did not complete its prefix/body/tail and peer cleanup')
    session = record.get('session_id')
    require(type(session) is str and str(UUID(session)) == session, 'Finite session has no canonical actual peer identity')
    before, after = record.get('offer_request_monotonic_ns'), record.get('offer_response_monotonic_ns')
    require(type(before) is int and type(after) is int and 0 < before <= after,
            'Finite offer lacks exact original request/response clocks')
    if role == 'idle':
        require(record.get('player_before_offer', {}).get('state') == 'stop', 'Finite cold target was primed before its offer')
    rows = record.get('rows')
    require(type(rows) is list and len(rows) == 2
            and [row.get('kind') for row in rows] == [f'cold_{role}', f'warm_{role}'],
            'Finite session lacks both original cold and warm utterances')
    contract = record.get('capture_timing_contract', {})
    require(type(contract.get('declared_before_offer_monotonic_ns')) is int
            and 0 < contract['declared_before_offer_monotonic_ns'] <= before,
            'Finite timestamp budget was not declared before its actual offer')
    original_route = record.get('original_target_route')
    require(type(original_route) is dict and set(original_route) == {'source_owner', 'source_identity', 'source_operation_generation', 'native_generation', 'launch_generation', 'selected_output', 'selected_ids', 'local_pin', 'units'}
            and (original_route['source_owner'] is None) == (role == 'idle')
            and original_route['source_identity'].get('owner') == original_route['source_owner']
            and original_route['selected_ids'] == ['0']
            and original_route['selected_output'].get('id') == '0'
            and original_route['selected_output'].get('protocol') == 'alsa'
            and original_route['selected_output'].get('selected') is True
            and type(original_route['selected_output'].get('offset_ms')) is int
            and original_route['units'], 'Finite session lacks its exact original target source/unit/output route')
    identity = {'session_id': session, 'request_id': record.get('request_id')}
    require(type(identity['request_id']) is str and str(UUID(identity['request_id'])) == identity['request_id'],
            'Finite session lacks its exact original API request identity')
    measured = finite.validate_ready_observation(record.get('timer_ready_observation'), role, original_route, identity,
        offer_request_ns=before, offer_response_ns=after)
    ack = record['timer_ready_observation']['worker_health']['speech_startup_authenticated_ready_ack']
    require(supervisor.exact_json(record.get('timer_ready_ack'), ack)
            and supervisor.exact_json(record.get('timer_ready_measurements'), measured) and record.get('offer_scope') == finite.READY_SCOPE,
            'Finite session relabels its authenticated acknowledged-mix clocks or scope')
    verified = []
    previous = None
    paths = set()
    for index, row in enumerate(rows):
        player = row.get('pre_release_player', {})
        require(supervisor.exact_json(row.get('pre_release_target_route'), original_route), 'Finite utterance changed its original held target route')
        if index == 1:
            previous_player = rows[0].get('player_after_utterance', {})
            after_player = row.get('player_after_utterance', {})
            require(player.get('state') == previous_player.get('state') == after_player.get('state') == 'play'
                    and player.get('item_id') is not None
                    and player.get('item_id') == previous_player.get('item_id') == after_player.get('item_id'),
                    'Finite warm row reused a stopped/replaced OwnTone program')
        elif role == 'native':
            require(player.get('state') == 'play' and player.get('item_id') == record.get('player_before_offer', {}).get('item_id'),
                    'Finite cold native row replaced its original program')
        require(supervisor.exact_json(row.get('post_utterance_target_route'), original_route),
                'Finite delivery changed its original source/unit/output route')
        for key in ('pre_release_readiness', 'post_utterance_readiness'):
            finite.validate_ready_observation(row.get(key), role, original_route, identity,
                offer_request_ns=before, offer_response_ns=after)
            require(supervisor.exact_json(row[key]['worker_health']['speech_startup_authenticated_ready_ack'], ack),
                    'Finite utterance reused another API preparation or nonce')
        if index == 1:
            samples = row.get('warm_player_samples', [])
            require(type(samples) is list and 2 <= len(samples) <= 1000
                    and all(type(sample.get('monotonic_ns')) is int
                            and sample.get('player', {}).get('state') == 'play'
                            and sample['player'].get('item_id') == player.get('item_id') for sample in samples)
                    and all(first['monotonic_ns'] <= second['monotonic_ns'] for first, second in zip(samples, samples[1:], strict=False)),
                    'Finite warm row cannot prove the already running driver remained alive')
        source = row.get('source', {})
        require(row.get('session_id') == session and row.get('same_session_for_cold_and_warm') is True
                and row.get('cold_utterance_completeness_passed') is True
                and row.get('cold_utterance_completeness_status') == COMPLETE
                and row.get('speech_latency_performance_passed') is False
                and row.get('speech_latency_performance_status') == PERFORMANCE
                and source.get('body_frames') == finite.BODY_FRAMES,
                'Finite row relabels peer, utterance completeness or performance')
        emitted, requested = source.get('first_source_frame_to_encoder_monotonic_ns'), row.get('utterance_request_monotonic_ns')
        require(type(emitted) is int and type(requested) is int and after <= requested <= emitted
                and (previous is None or previous < requested), 'Finite utterance clocks were reused or relabeled')
        previous = emitted
        require(row['pre_release_readiness']['observed_after_monotonic_ns'] <= emitted
                and emitted <= row['post_utterance_readiness']['observed_before_monotonic_ns'],
                'Finite readiness observation did not bracket its actual emitted utterance')
        artifacts = row.get('artifacts', {})
        expected_artifacts = {'decoded_reference', 'final_pcm', 'final_timestamps'}
        if role == 'native':
            expected_artifacts.add('music_baseline')
        require(set(artifacts) == expected_artifacts, 'Finite row lost its original source/output artifacts')
        for artifact in artifacts.values():
            require(artifact['path'] not in paths, 'Finite utterance artifact was reused')
            paths.add(artifact['path'])
        decoded = read_artifact(artifacts['decoded_reference'], directory, expected_uid=expected_uid,
                                limit=(finite.BODY_FRAMES+finite.CODEC_TAIL_FRAMES)*2)
        data = read_artifact(artifacts['final_pcm'], directory, expected_uid=expected_uid, limit=finite.MAX_CAPTURE_FRAMES*4)
        timestamps = json.loads(read_artifact(artifacts['final_timestamps'], directory, expected_uid=expected_uid,
                                              limit=1024*1024), object_pairs_hook=supervisor.strict_object,
                                parse_constant=supervisor.no_constant)
        origin, quality = finite.validate_final_timestamps(timestamps, len(data), record.get('capture_timing_contract'),
            start_frame=row.get('alignment_start_frame'))
        require(supervisor.exact_json(row.get('capture_timestamp_quality'), quality), 'Finite capture timestamp quality was relabeled')
        frames = len(data)//4
        reference = {'pcm': np.frombuffer(decoded, dtype='<i2'), 'reference_frames': len(decoded)//2,
            'quiet_frames': finite.QUIET_FRAMES, 'source': source, 'session_id': session,
            'decoded_pcm_sha256': row['decoded_pcm_sha256'], 'payload_calendar_sha256': row['payload_calendar_sha256']}
        quiet_carrier, capture_first_frame = None, None
        if role == 'native':
            carrier_contract = record.get('music_carrier_contract', {})
            declared = carrier_contract.get('declared_before_offer_monotonic_ns')
            require(type(declared) is int and 0 < declared <= before
                    and supervisor.exact_json(carrier_contract.get('original_target_route'), original_route),
                    'Finite music carrier was not frozen on its original route before offer')
            carrier_evidence = carrier_contract.get('carrier', {})
            baseline = read_artifact(artifacts['music_baseline'], directory, expected_uid=expected_uid,
                limit=finite.QUIET_FRAMES*4)
            baseline_timings = carrier_contract.get('timings')
            require(type(baseline_timings) is list and len(baseline_timings) == finite.QUIET_FRAMES//finite.FRAMES,
                    'Finite music carrier lost its bounded original baseline timing records')
            _, baseline_quality = finite.validate_final_timestamps(baseline_timings, len(baseline),
                record.get('capture_timing_contract'), start_frame=0)
            require(supervisor.exact_json(carrier_contract.get('timestamp_quality'), baseline_quality)
                    and baseline_timings[-1]['callback_monotonic_ns'] <= declared,
                    'Finite music carrier relabeled its original pre-offer capture clocks')
            quiet_carrier = finite.quiet_module().freeze_carrier(baseline,
                first_frame=carrier_evidence.get('first_frame'), duck_gain=carrier_evidence.get('duck_gain'))
            require(carrier_evidence.get('first_frame') == baseline_timings[0]['gst_metadata']['offset']
                    and supervisor.exact_json(carrier_evidence, quiet_carrier.evidence()),
                    'Finite music carrier changed its exact pre-offer bytes/phase/calendar')
            capture_first_frame = row.get('capture_first_frame')
            require(type(capture_first_frame) is int and capture_first_frame == timestamps[0]['gst_metadata']['offset']
                    and capture_first_frame >= quiet_carrier.first_frame+finite.QUIET_FRAMES,
                    'Finite native capture reused a pre-offer carrier frame origin')
        repeat = finite.verify_complete(data, reference,
            start_bounds=(0, frames-reference['reference_frames']-finite.QUIET_FRAMES), music=role == 'native',
            quiet_carrier=quiet_carrier, capture_first_frame=capture_first_frame)
        require(all(supervisor.exact_json(row.get(key), value) for key, value in repeat.items()),
                'Finite row changed independently recomputed full prefix/body/tail evidence')
        actual = {'final_reference_origin_monotonic_ns': origin,
            'source_to_final_reference_origin_ms': (origin-emitted)/1e6,
            'offer_request_to_encoder_ms': (emitted-before)/1e6,
            'utterance_request_to_encoder_ms': (emitted-requested)/1e6,
            'offer_request_to_final_reference_origin_ms': (origin-before)/1e6,
            'acknowledged_mix_to_encoder_ms': (emitted-ack['mixed_monotonic_ns'])/1e6,
            'acknowledged_mix_to_final_reference_origin_ms': (origin-ack['mixed_monotonic_ns'])/1e6}
        require(all(supervisor.exact_json(row.get(key), value) for key, value in actual.items()),
                'Finite source/final/request clocks were relabeled')
        verified.append({'kind': row['kind'], **actual, 'source': source, 'session_id': session,
            'timer_ready_measurements': measured, 'timer_ready_ack': ack, 'capture_timestamp_quality': quality,
            'cold_utterance_completeness_passed': True, 'cold_utterance_completeness_status': COMPLETE,
            'speech_latency_performance_passed': False, 'speech_latency_performance_status': PERFORMANCE})
    return verified


def validate_fixture(report, phase, admission, *, expected_uid=0, work=None):
    helper = supervisor.load_matrix_helper()
    supervisor.validate_epoch_report(report, phase, admission, supervisor.report_time(report['started_at']),
                                     supervisor.report_time(report['finished_at']), finite_speech=True)
    epoch = report['latency_epoch']
    require(epoch.get('diagnostic') == 'finite_emitted_opus' and epoch.get('rows') == []
            and 'speech_latency_measurements' not in epoch
            and epoch.get('measured_signal_timeline_cleanup_passed') is True
            and epoch.get('cold_utterance_completeness_passed') is True
            and epoch.get('cold_utterance_completeness_status') == COMPLETE
            and epoch.get('speech_latency_performance_passed') is False
            and epoch.get('speech_latency_performance_status') == PERFORMANCE,
            'Separate finite fixture was confused with the repeating marker matrix')
    plan = epoch.get('frozen_plan', {})
    definitions = [Room.model_validate(room) for room in plan.get('saved_intent', [])]
    policy = latency_plan(definitions)
    require(helper.intent_digest(plan['saved_intent']) == plan.get('intent_sha256')
            and plan.get('common_horizon_ns') == policy.common_horizon_ns
            and plan.get('output_buffers_ms') == {room.room_id: room.output_buffer_ms for room in policy.rooms}
            and {room.id for room in definitions if room.enabled} == {helper.A, helper.B}, 'Finite fixture H/B/saved intent changed')
    for room in definitions:
        if room.enabled:
            require(len(room.speakers) == 1 and room.speakers[0].protocol == 'alsa' and room.speakers[0].id == '0'
                    and room.speakers[0].offset_ms == (phase.offset_ms if room.id == helper.A else 0),
                    'Finite fixture changed its exact reviewed output offsets')
    baseline = epoch.get('independent_capture_baselines', {})
    require(set(baseline) == {helper.A, helper.B}, 'Finite fixture lacks independent pre-backend calibration')
    calendars = epoch.get('native_calendars', {})
    program_rooms = {helper.B} if phase.phase == 'idle' else {helper.A, helper.B}
    require(set(calendars) == program_rooms, 'Finite cold target was incorrectly admitted as a native program')
    for calibration in baseline.values():
        require(all(calibration.get('cleanup', {}).get(key) is True for key in
                    ('exact_output_unit_stopped', 'capture_null', 'held_pin_closed')), 'Finite calibration was not independently retired')
        require(helper.numeric(calibration.get('capture_offset_ms')) and helper.numeric(calibration.get('horizon_half_width_ms'))
                and calibration['horizon_half_width_ms'] > 0, 'Finite independent capture bracket is invalid')
    for identifier, calendar in calendars.items():
        offset = phase.offset_ms if identifier == helper.A else 0
        require(calendar.get('declared_final_origin_ns') == epoch['common_presentation_ns']+policy.common_horizon_ns+offset*1_000_000,
                'Finite native calendar changed its actual P/H/offset')
        helper.validate_alignment_receipt(calendar['alignment'], tight=False)
        horizon = calendar.get('horizon', {})
        calibration = baseline[identifier]
        require(horizon.get('raw_displacement_ms') == calendar['alignment']['relative_offset_ms']
                and horizon.get('independent_capture_offset_ms') == calibration['capture_offset_ms']
                and horizon.get('predeclared_half_width_ms') == calibration['horizon_half_width_ms']
                and helper.numeric(horizon.get('corrected_horizon_error_ms'))
                and abs(horizon['corrected_horizon_error_ms']-(horizon['raw_displacement_ms']-calibration['capture_offset_ms'])) < 1e-9
                and abs(horizon['corrected_horizon_error_ms']) <= calibration['horizon_half_width_ms'], 'Finite native calendar misses independent capture bracket')
        continuity = calendar.get('frame_continuity', {})
        require(continuity.get('mode') == 'sample_offsets' and type(continuity.get('verified_blocks')) is int
                and continuity['verified_blocks'] >= 100 and continuity.get('verified_frames', 0) > 0, 'Finite native frame proof is incomplete')
    if phase.phase == 'native':
        alignment = epoch.get('configured_offset_alignment', {})
        require(alignment.get('declared_offsets_ms') == {helper.A: phase.offset_ms, helper.B: 0},
                'Finite group timing changed its declared correction')
        helper.validate_alignment_receipt(alignment.get('raw_b_minus_a', {}), tight=False)
        require(abs(alignment['raw_b_minus_a']['relative_offset_ms']+phase.offset_ms) <= 2,
                'Finite raw group timing misses its exact configured offset')
        residual = alignment.get('residual', {})
        for window in (residual, residual.get('early', {}), residual.get('late', {})):
            helper.validate_alignment_receipt(window, tight=True)
        require(helper.numeric(residual.get('drift_ms')) and abs(residual['drift_ms']) <= 2
                and abs(residual['drift_ms']-(residual['late']['relative_offset_ms']-residual['early']['relative_offset_ms'])) < 1e-9,
                'Finite group timing drifts across its original coded program')
        original = epoch.get('target_original', {})
        require(original.get('source_owner') is not None and original.get('player', {}).get('item_id') is not None,
                'Finite native target lacks its original advancing program identity')
    untouched = epoch.get('untouched', {})
    require(untouched.get('failure') is None and untouched.get('original_capture_preserved') is True
            and len(untouched.get('control_samples', [])) >= 3
            and untouched.get('pcm', {}).get('failed_block') is None
            and untouched.get('pcm', {}).get('untouched_reference_blocks', 0) > 0,
            'Finite fixture lost original continuous B control/content proof')
    stopped = epoch.get('stop_boundary', {}).get('requested_monotonic_ns')
    tail = epoch.get('tail', {})
    require(type(stopped) is int and stopped > 0 and all(tail.get(key) is True for key in
            ('both_captures_null', 'pre_stop_pcm_verified', 'frame_sequences_verified', 'teardown_labeled'))
            and set(tail.get('captures', {})) == {helper.A, helper.B}, 'Finite fixture tail/stop proof is incomplete')
    for final in tail['captures'].values():
        sequence = final.get('capture', {}).get('frame_continuity', {})
        require(final.get('pcm', {}).get('failed_block') is None and final.get('capture', {}).get('capture_dropped_bytes') == 0
                and final['pcm'].get('stop_requested_monotonic_ns') == stopped
                and type(final.get('null_verified_monotonic_ns')) is int and final['null_verified_monotonic_ns'] >= stopped
                and sequence.get('mode') == 'sample_offsets' and sequence.get('verified_frames', -1)*4 == final['capture'].get('observed_bytes'),
                'Finite final cleanup hides a PCM/timing/queue failure')
    retirements = epoch.get('producer_retirement', {})
    require(set(retirements) == program_rooms, 'Finite original producer retirement set changed')
    for retirement in retirements.values():
        require(retirement.get('retired') is True and retirement.get('identity', {}).get('invocation_id')
                and type(retirement.get('requested_monotonic_ns')) is int and type(retirement.get('verified_monotonic_ns')) is int
                and stopped <= retirement['requested_monotonic_ns'] <= retirement['verified_monotonic_ns'], 'Finite producer retirement lacks exact identity/time')
    daemon = epoch.get('daemon_retirement', {})
    require(daemon.get('retired') is True and daemon.get('empty_ownership_verified') is True
            and type(daemon.get('requested_monotonic_ns')) is int and type(daemon.get('verified_monotonic_ns')) is int
            and daemon['requested_monotonic_ns'] <= daemon['verified_monotonic_ns']
            and set(daemon.get('units', {})) == {helper.A, helper.B}, 'Finite daemon retirement/ownership is incomplete')
    directory = Path(report.get('artifacts', {}).get('private_directory', ''))
    require(directory.is_absolute() and directory.parent == (work or group.WORK)
            and directory.name.startswith('native-group-') and directory.resolve(strict=True) == directory,
            'Finite artifacts escaped their exact fresh fixture directory')
    record = epoch.get('finite_speech')
    require(report.get('finite_speech_diagnostics') == [record], 'Finite diagnostic was replaced or primed by another session')
    route = record.get('original_target_route', {})
    require(route.get('source_identity', {}).get('zone_id') == helper.A
            and route.get('selected_output', {}).get('offset_ms') == phase.offset_ms
            and route.get('units') == epoch.get('initial_unit_identities', {}).get(helper.A),
            'Finite target route differs from exact prepared offset/unit identities')
    if phase.phase == 'native':
        require(route.get('source_owner') == epoch.get('target_original', {}).get('source_owner'),
                'Finite target route replaced its original native source')
    contract = record.get('capture_timing_contract', {})
    target_baseline = baseline[helper.A]
    require(contract.get('negotiated_period_frames') == target_baseline.get('negotiated_period_frames')
            and contract.get('period_uncertainty_ms') == target_baseline.get('period_uncertainty_ms')
            and contract.get('base_time_ns') == target_baseline.get('capture', {}).get('common_clock_base_ns')
            and contract.get('clock_offset_to_monotonic_ns') == target_baseline.get('capture', {}).get('clock_offset_to_monotonic_ns'),
            'Finite timestamp contract differs from independently prepared capture/period calibration')
    rows = validate_session(record, phase.phase, directory, expected_uid=expected_uid)
    for row in rows:
        calibration = baseline[helper.A]
        corrected = row['source_to_final_reference_origin_ms']-calibration['capture_offset_ms']
        row.update(independent_capture_offset_ms=calibration['capture_offset_ms'],
            independent_capture_half_width_ms=calibration['horizon_half_width_ms'],
            capture_corrected_source_to_final_reference_origin_ms=corrected,
            capture_corrected_source_to_final_bracket_ms=[corrected-calibration['horizon_half_width_ms'],
                                                        corrected+calibration['horizon_half_width_ms']],
            actual_target_output_buffer_ms=plan['output_buffers_ms'][helper.A],
            configured_target_offset_ms=phase.offset_ms)
    return {'phase': phase.receipt(), 'rows': rows, 'independent_capture_baselines': baseline,
            'frozen_plan': plan, 'untouched': untouched, 'tail': tail,
            'scope': 'Separate finite digital utterance evidence; repeating marker matrix remains separately qualified'}


async def run(offset_ms=0):
    result = {'started_at': datetime.now(timezone.utc).isoformat(), 'mode': 'finite_speech', 'passed': False,
        'measured_signal_timeline_cleanup_passed': False, 'cold_utterance_completeness_passed': False,
        'cold_utterance_completeness_status': 'not_completed', 'speech_latency_performance_passed': False,
        'speech_latency_performance_status': 'not_completed', 'fixtures': [], 'cleanup': {}, 'cleanup_errors': [],
        'deadlines': {'child_seconds': supervisor.CHILD_SECONDS, 'fixture_cleanup_seconds': supervisor.EPOCH_CLEANUP_SECONDS,
                      'two_fixture_seconds': FIXTURE_SECONDS, 'external_watchdog_seconds': EXTERNAL_SECONDS},
        'scope': 'Separate cold/warm finite Opus prefix/body/tail, digital final audio only; no nine-row/minimum-latency/phone/acoustic claim'}
    source = admission = installation = baseline = protected = None
    try:
        require(sys.platform == 'linux' and os.geteuid() == 0 and boot_id() and group.NATIVE_LAB is not None,
                'Finite speech requires Linux root and an explicit admitted fresh-lab profile')
        require(type(offset_ms) is int and offset_ms in (-2000, 0, 2000), 'Finite offset must be exact reviewed epoch choice')
        source = supervisor.source_receipts(group.PROJECT, finite_speech=True)
        installation, admission = supervisor.matrix_admission(source, finite_speech=True)
        result.update(source_files=source, native_lab=admission, installation_id=installation, boot_id=boot_id())
        group.observation.base.closed_slot()
        protected = group.legacy_snapshot()
        baseline = await group.observation.base.host_snapshot()
        helper = supervisor.load_matrix_helper()
        phases = tuple(helper.Phase(str(uuid4()), offset_ms, role) for role in ('idle', 'native'))
        result['planned_phases'] = [phase.receipt() for phase in phases]
        async def collect():
            verified = []
            original_units, original_owners, sessions = set(), set(), set()
            for phase in phases:
                try:
                    record = await supervisor.run_epoch(phase, source, admission, installation, baseline, protected, finite_speech=True)
                except asyncio.CancelledError as exc:
                    if hasattr(exc, 'epoch_record'):
                        result['fixtures'].append(exc.epoch_record)
                    raise
                result['fixtures'].append(record)
                atomic_json(RESULT, result)
                require(record.get('passed') is True, 'Finite fixture/owned cleanup failed; remaining fixture was not launched')
                proof = await asyncio.to_thread(validate_fixture, record['inner_report'], phase, admission)
                untouched = proof['untouched']
                units = {unit['invocation_id'] for unit in untouched['original_unit_identities'].values()}
                owner = tuple(untouched['source_owner'].get(key) for key in ('session_id', 'epoch', 'incarnation'))
                session = proof['rows'][0]['session_id']
                require(units and not units.intersection(original_units) and all(value is not None for value in owner)
                        and owner not in original_owners and session not in sessions, 'Finite fixture reused prior source/unit/peer actors')
                original_units.update(units)
                original_owners.add(owner)
                sessions.add(session)
                verified.append(proof)
            result['verified_fixtures'] = verified
            result['all_fixtures_verified'] = True
        await asyncio.wait_for(collect(), FIXTURE_SECONDS)
    except BaseException as exc:
        result['failure'] = supervisor.failure(exc)
    finally:
        if source is not None:
            try:
                supervisor.matrix_admission(source, admission, installation, finite_speech=True)
                result['cleanup']['source_lab_ownership_preserved'] = True
            except Exception as exc:
                result['cleanup_errors'].append({'stage': 'final_admission', **supervisor.failure(exc)})
        if baseline is not None:
            try:
                require(supervisor.exact_json(await group.observation.base.host_snapshot(), baseline)
                        and supervisor.exact_json(group.legacy_snapshot(), protected), 'Finite original host/protected state changed')
                group.observation.base.closed_slot()
                result['cleanup']['original_host_protected_preserved'] = True
            except Exception as exc:
                result['cleanup_errors'].append({'stage': 'final_baseline', **supervisor.failure(exc)})
        result['passed'] = bool(result.get('all_fixtures_verified') is True and len(result['fixtures']) == 2
            and all(record.get('passed') is True for record in result['fixtures']) and not result['cleanup_errors']
            and all(result['cleanup'].get(key) is True for key in ('source_lab_ownership_preserved', 'original_host_protected_preserved')))
        if result['passed']:
            result.update(measured_signal_timeline_cleanup_passed=True, cold_utterance_completeness_passed=True,
                cold_utterance_completeness_status=COMPLETE, speech_latency_performance_passed=False,
                speech_latency_performance_status=PERFORMANCE)
        result['finished_at'] = datetime.now(timezone.utc).isoformat()
        atomic_json(RESULT, result)
    return 0 if result['passed'] else 1


async def dispatch(offset_ms):
    task, loop = asyncio.current_task(), asyncio.get_running_loop()
    for name in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(name, task.cancel)
    try:
        return await run(offset_ms)
    finally:
        for name in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(name)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--offset-ms', type=int, choices=(-2000, 0, 2000), default=0)
    raise SystemExit(asyncio.run(dispatch(parser.parse_args().offset_ms)))
