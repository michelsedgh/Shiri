"""Meaningful opt-in fixture regressions; no VM, sysfs or hardware is opened."""
import asyncio
import ast
from collections import deque
from copy import deepcopy
import importlib.util
import hashlib
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from shiri.runtime.system import RuntimeFailure

np = pytest.importorskip('numpy', reason='Digital latency probes require the audio extra')
pytest.importorskip('aiortc', reason='Latency sender uses real Opus peers')

PATH = Path(__file__).parent/'linux/native_latency_probe.py'
spec = importlib.util.spec_from_file_location('tested_native_latency_probe', PATH)
probe = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = probe
spec.loader.exec_module(probe)


def pcm(amplitude, frequency=880):
    values = (amplitude*np.sin(2*np.pi*frequency*np.arange(960)/48000)).astype('<i2')
    return np.repeat(values[:, None], 2, axis=1).tobytes()


def feed(gate, amplitudes, *, frequency=880, origin=10_000_000_000):
    answers = []
    for index, amplitude in enumerate(amplitudes):
        at = origin+index*20_000_000
        answers.append(gate.push(pcm(amplitude, frequency), at, at/1e9+.002))
    return answers


def test_constant_or_wrong_voice_cannot_count_a_coded_idle_marker():
    for frequency in (440, 880, 1320):
        gate = probe.MarkerGate(audible=True, trigger_ns=10_000_000_000)
        assert not any(feed(gate, [424]*50, frequency=frequency))
        assert not gate.receipt()['passed']


def test_actual_coded_carrier_retains_absolute_encoder_latency_and_callback_scope():
    gate = probe.MarkerGate(audible=True, trigger_ns=6_000_000_000)
    answers = feed(gate, [424]*6+[276]*6+[424]*12)
    assert any(answers) and gate.passed
    receipt = gate.receipt()
    assert receipt['encoder_to_final_pts_seconds'] == 4
    assert receipt['encoder_to_final_callback_seconds'] == pytest.approx(4.002)
    assert receipt['coded_120ms_levels'] == ['high', 'low', 'high']


def test_stale_voice_before_trigger_and_brief_voice_cannot_count_as_onset():
    gate = probe.MarkerGate(audible=True, trigger_ns=10_500_000_000)
    assert not any(feed(gate, [424]*6+[276]*6+[424]*12))
    assert gate.receipt()['first_matching_absolute_pts_ns'] is None
    gate = probe.MarkerGate(audible=True, trigger_ns=10_000_000_000)
    assert not any(feed(gate, [424]*6+[276]*6+[424]*6))  # 360ms is too short.


def test_silence_resets_voice_code_streak_and_requires_new_consecutive_proof():
    gate = probe.MarkerGate(audible=True, trigger_ns=10_000_000_000)
    assert not any(feed(gate, [424]*6+[276]*6+[0]*30+[424]*6))
    assert gate.frames == 6*960
    assert not gate.passed


@pytest.mark.parametrize('fault', ['backwards', 'partial', 'clip'])
def test_malformed_marker_clock_pcm_or_clip_is_a_hard_failure(fault):
    gate = probe.MarkerGate(audible=True, trigger_ns=10_000_000_000)
    gate.push(pcm(424), 10_000_000_000, 10.)
    with pytest.raises(RuntimeFailure):
        if fault == 'backwards':
            gate.push(pcm(424), 9_000_000_000, 10.1)
        elif fault == 'partial':
            gate.push(b'bad', 10_020_000_000, 10.1)
        else:
            gate.push(np.full(1920, 32767, dtype='<i2').tobytes(), 10_020_000_000, 10.1)


def test_warm_silence_needs_real_final_pcm_and_cannot_hide_voice_tail():
    gate = probe.MarkerGate(audible=False, trigger_ns=10_000_000_000)
    assert not any(feed(gate, [0]*10+[276]*10+[0]*10))
    assert gate.frames == 10*960
    assert any(feed(gate, [0]*24, origin=10_600_000_000))


@pytest.mark.parametrize('kind', ['dc', 'broadband', 'right_channel'])
def test_fitted_carrier_absence_cannot_falsely_admit_actual_nonsilent_pcm(kind):
    gate = probe.MarkerGate(audible=False, trigger_ns=10_000_000_000)
    rng = np.random.default_rng(123)
    if kind == 'dc':
        samples = np.full((960, 2), 10000, dtype='<i2')
    elif kind == 'broadband':
        samples = rng.integers(-10000, 10000, size=(960, 2), dtype=np.int16)
    else:
        samples = np.zeros((960, 2), dtype='<i2')
        samples[:, 1] = 10000
    for index in range(24):
        assert not gate.push(samples.tobytes(), 10_000_000_000+index*20_000_000, 10+index*.02)
    assert not gate.passed
    assert gate.receipt()['last_spectrum']['peak'] >= 9000


@pytest.mark.asyncio
async def test_real_rtc_marker_track_is_gated_and_warm_marker_keeps_the_same_peer():
    peer, tone = probe.make_peer()
    pending = asyncio.create_task(tone.recv())
    try:
        await asyncio.sleep(.025)
        assert not pending.done() and tone.samples == 0 and tone.triggers == []
        tone.release()
        frame = await asyncio.wait_for(pending, 1)
        assert frame.samples == 960 and frame.pts == 0
        assert len(tone.triggers) == 1 and tone.triggers[0]['utterance'] == 1
        tone.audible = False
        silence = await tone.recv()
        assert not np.any(silence.to_ndarray())
        tone.release()
        second = await tone.recv()
        assert second.pts == 1920 and len(tone.triggers) == 2
        assert tone.triggers[1]['utterance'] == 2
        assert tone.triggers[1]['first_emitted_monotonic_ns'] > tone.triggers[0]['first_emitted_monotonic_ns']
        assert peer.getSenders()[0].track is tone
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        tone.stop()
        await asyncio.wait_for(peer.close(), 2)


def test_capture_fences_are_sticky_and_do_not_consume_rejected_buffer():
    class Sequence:
        def push(self, metadata, frames, rate):
            raise RuntimeFailure('Missing actual PCM sample offsets')
        def evidence(self):
            return {}
    capture = probe.capture_type(object)()
    capture.error = None
    capture.capture_dropped = 0
    capture.needs_latency = False
    capture.total = 0
    capture.sequence = Sequence()
    capture.chunks, capture.captured_at = [], []
    capture.pending = deque([(1., pcm(424), 48000, 2, 'S16LE', {})])
    for _ in range(2):
        with pytest.raises(RuntimeFailure, match='Missing actual PCM'):
            capture.poll()
    assert capture.total == 0 and len(capture.pending) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('bad_tail', [False, True])
async def test_capture_shutdown_checks_and_retains_exact_final_callback_tail(bad_tail):
    class Sequence:
        def __init__(self):
            self.frames = self.blocks = 0
        def push(self, metadata, frames, rate):
            if metadata.get('bad'):
                raise RuntimeFailure('Final pending sample offsets repeat')
            self.frames += frames
            self.blocks += 1
        def evidence(self):
            return {'verified_frames': self.frames, 'verified_blocks': self.blocks}
    capture = probe.capture_type(object)()
    capture.error = None
    capture.capture_dropped = 0
    capture.needs_latency = False
    capture.total = 0
    capture.sequence = Sequence()
    capture.chunks, capture.captured_at = [], []
    capture.pending = deque([(1., pcm(424), 48000, 2, 'S16LE', {})])
    capture.device = 'test-only'
    capture.rate = capture.channels = capture.format = None
    capture.discontinuities = 1
    capture.max_packet_gap = 0.
    capture.expected_base = 1
    capture.clock_offset_ns = 0
    capture.Gst = SimpleNamespace(State=SimpleNamespace(NULL='null'))
    capture.pipeline = SimpleNamespace(get_state=lambda timeout: SimpleNamespace(state='null'))
    def close():
        # A streaming callback which finishes during Gst NULL transition.
        assert capture.sequence.frames == 960
        capture.pending.append((1.02, pcm(276), 48000, 2, 'S16LE', {'bad': bad_tail}))
    capture.close = close
    retained = []
    def retain(cap, directory, label):
        assert cap is capture
        retained.append((cap.sequence.evidence(), tuple(cap.chunks)))
        return {'label': label}
    context = SimpleNamespace(target='A', captures={'A': capture}, temporary=Path('/unused'), retain_capture=retain,
                              pcm_guards={'A': SimpleNamespace(capture=capture, check=lambda **_kwargs: None)})
    if bad_tail:
        with pytest.raises(RuntimeFailure, match='pending sample offsets repeat'):
            await probe.close_capture(context, 'final-tail')
        assert retained == [] and capture.error == 'Final pending sample offsets repeat'
        with pytest.raises(RuntimeFailure, match='pending sample offsets repeat'):
            capture.poll()
    else:
        result = await probe.close_capture(context, 'final-tail')
        assert result['label'] == 'final-tail'
        assert result['capture_stop_boundary']['null_verified_monotonic_ns'] >= result['capture_stop_boundary']['requested_monotonic_ns']
        assert retained[0][0] == {'verified_frames': 1920, 'verified_blocks': 2}
        assert len(retained[0][1]) == 2 and not capture.pending


@pytest.mark.asyncio
async def test_late_zero_native_tail_fails_the_real_music_guard_and_invalidates_row_receipt():
    group = group_module()
    capture = SimpleNamespace(chunks=[pcm(8192, 440)], captured_at=[1.], absolute={1.: 10_000_000_000},
        max_packet_gap=0., last_packet_at=probe.time.monotonic())
    capture.poll = lambda: None
    capture.Gst = SimpleNamespace(State=SimpleNamespace(NULL='null'))
    capture.pipeline = SimpleNamespace(get_state=lambda timeout: SimpleNamespace(state='null'))
    row = {'kind': 'native_calendar', 'passed': True, 'status': 'complete'}
    capture.validation_rows = [row]
    guard = group.FinalPcmGuard(capture)
    guard.begin(0)
    def close():
        # Complete correctly framed silence produced during the final await.
        capture.chunks.append(pcm(0))
        capture.captured_at.append(1.02)
        capture.absolute[1.02] = 10_020_000_000
    capture.close = close
    context = SimpleNamespace(target='A', captures={'A': capture}, pcm_guards={'A': guard},
        temporary=Path('/unused'), retain_capture=lambda *_args: pytest.fail('Bad native tail was retained as accepted'))
    with pytest.raises(RuntimeFailure, match='lost the continuous440'):
        await probe.close_capture(context, 'late-native-tail')
    assert row['passed'] is False and row['status'] == 'failed'
    assert guard.failed_block['index'] == 1
    with pytest.raises(RuntimeFailure, match='lost the continuous440'):
        guard.check()


@pytest.mark.asyncio
@pytest.mark.parametrize('tail', ['healthy', 'zero', 'caps', 'offset'])
async def test_authorized_slow_null_transition_keeps_real_content_caps_and_offset_fences(tail):
    import time
    group = group_module()
    capture = probe.capture_type(object)()
    capture.error = None
    capture.capture_dropped = 0
    capture.needs_latency = False
    capture.total = 0
    capture.sequence = group.observation.PcmSequence()
    capture.chunks, capture.captured_at = [], []
    initial = {'offset': 0, 'offset_end': 960, 'pts': 0, 'duration': 20_000_000, 'discont': True}
    capture.pending = deque([(1., pcm(8192, 440), 48000, 2, 'S16LE', initial)])
    capture.device = 'test-only'
    capture.rate = capture.channels = capture.format = None
    capture.discontinuities = 1
    capture.max_packet_gap = 0.
    capture.last_packet_at = time.monotonic()
    capture.expected_base = 1
    capture.clock_offset_ns = 0
    capture.absolute = {1.: 10_000_000_000, 1.02: 10_020_000_000}
    capture.Gst = SimpleNamespace(State=SimpleNamespace(NULL='null'))
    capture.pipeline = SimpleNamespace(get_state=lambda timeout: SimpleNamespace(state='null'))
    row = {'passed': True, 'status': 'complete'}
    capture.validation_rows = [row]
    last_packet = capture.last_packet_at
    def close():
        time.sleep(.32)  # Actual yielding NULL-transition duration, not a fake clock.
        metadata = {'offset': 960, 'offset_end': 1920, 'pts': 20_000_000,
                    'duration': 20_000_000, 'discont': False}
        if tail == 'offset':
            metadata.update(offset=0, offset_end=960)
        capture.pending.append((1.02, pcm(0 if tail == 'zero' else 8192, 440),
                                44100 if tail == 'caps' else 48000, 2, 'S16LE', metadata))
    capture.close = close
    guard = group.FinalPcmGuard(capture)
    guard.begin(0)
    retained = []
    context = SimpleNamespace(target='A', captures={'A': capture}, pcm_guards={'A': guard}, temporary=Path('/unused'),
        retain_capture=lambda *_args: retained.append(True) or {})
    if tail == 'healthy':
        artifact = await probe.close_capture(context, 'slow-null')
        assert artifact['capture_stop_boundary']['transition_seconds'] >= .32
        assert guard.checked_blocks == 2 and retained == [True] and row['passed']
        # Default remains strict: a stopped capture is not a live stream.
        with pytest.raises(RuntimeFailure, match='callback gap'):
            guard.check()
    else:
        with pytest.raises(RuntimeFailure):
            await probe.close_capture(context, 'slow-null')
        assert not retained and not row['passed'] and row['status'] == 'failed'
        assert row['capture_stop_boundary']['transition_seconds'] >= .32
    assert capture.last_packet_at == last_packet  # Never fabricated/reset.


class Observer:
    def __init__(self):
        self.checks = 0
    async def check(self):
        self.checks += 1


def offset_context(*, enabled=False, saved_offset=0, actual_offset=0):
    saved = {'revision': 3, 'enabled': enabled, 'runtime': {'status': 'running'},
             'speakers': [{'id': '0', 'offset_ms': saved_offset}]}
    operations = []
    class Api:
        async def room(self, identifier):
            assert identifier == 'A'
            return deepcopy(saved)
        async def request(self, method, path, **kwargs):
            operations.append((method, path, kwargs))
            return {'runtime_accepted': True}
        async def patch(self, identifier, changes):
            saved.update(changes)
    async def outputs(ids):
        return [{'id': '0', 'selected': True, 'offset_ms': actual_offset}]
    pin = SimpleNamespace(validate=lambda: None, manifest={'card_index': 1, 'device': 1, 'subdevice': 7})
    state = SimpleNamespace(desired=SimpleNamespace(enabled=enabled), status='running', local_pin=pin,
        client=SimpleNamespace(outputs=outputs), selected_ids=['0'], processes={})
    context = SimpleNamespace(api=Api(), states={'A': state}, target='A', declared_horizon_ns=4_000_000_000,
        broker=SimpleNamespace(_worker_socket=lambda state: Path('/never/audio.sock')))
    return context, saved, operations


@pytest.mark.asyncio
async def test_enabled_room_cannot_receive_a_latency_offset_write():
    context, saved, operations = offset_context(enabled=True)
    with pytest.raises(RuntimeFailure, match='disabled exact target'):
        await probe.set_offset(context, Observer(), 0, {})
    assert operations == []


@pytest.mark.asyncio
async def test_stale_or_wrong_saved_local0_offset_fails_readback_after_exact_revision_write():
    context, saved, operations = offset_context(saved_offset=0)
    with pytest.raises(RuntimeFailure, match='Saved local0 offset'):
        await probe.set_offset(context, Observer(), -2000, {})
    assert operations[0] == ('PATCH', '/api/v1/rooms/A/speakers/0/offset',
                             {'json': {'expected_revision': 3, 'offset_ms': -2000}})


@pytest.mark.asyncio
async def test_actual_backend_offset_mismatch_prevents_cold_marker_admission(monkeypatch):
    context, _, _ = offset_context(saved_offset=-2000, actual_offset=0)
    monkeypatch.setattr(probe, 'playback_closed', lambda _: None)
    with pytest.raises(RuntimeFailure, match='does not report'):
        await probe.ready_target(context, Observer(), {'pin': context.states['A'].local_pin.manifest, 'saved_offset_ms': -2000})


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['pin', 'horizon', 'owner'])
async def test_changed_device_horizon_or_live_owner_prevents_cold_setup(monkeypatch, fault):
    context, _, _ = offset_context()
    health = {'ready': True, 'source': {'ready': True, 'owner': None}, 'timing_relay_delay_ms': 4000}
    manifest = deepcopy(context.states['A'].local_pin.manifest)
    if fault == 'pin':
        manifest['device'] = 0
    elif fault == 'horizon':
        health['timing_relay_delay_ms'] = 3000
    else:
        health['source']['owner'] = {'session_id': 'live'}
    async def health_rpc(*_args, **_kwargs):
        return health
    monkeypatch.setattr(probe, 'call_rpc', health_rpc)
    monkeypatch.setattr(probe, 'playback_closed', lambda _: None)
    with pytest.raises(RuntimeFailure):
        await probe.ready_target(context, Observer(), {'pin': manifest, 'saved_offset_ms': 0})


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_failed_or_cancelled_receiver_preparation_restores_monitor_and_original_handle(tmp_path, monkeypatch, cancel):
    entered = asyncio.Event()
    async def old_monitor():
        await asyncio.Event().wait()
    async def new_monitor():
        await asyncio.Event().wait()
    async def launch(*_args, **_kwargs):
        entered.set()
        if cancel:
            await asyncio.Event().wait()
        raise RuntimeFailure('Preparation failed before replacement admission')
    broker = SimpleNamespace(_monitor=asyncio.create_task(old_monitor()), _health_monitor=new_monitor)
    context = SimpleNamespace(states={'A': SimpleNamespace(directory=tmp_path)}, target='A', broker=broker,
        report={'latency_probe': {'preparations': []}}, producers={'A': 'old-historical-handle'}, launch_producer=launch)
    monkeypatch.setattr(probe, 'root_directory', lambda path: path.mkdir(parents=True))
    task = asyncio.create_task(probe.replace_receiver(context, Observer(), 'test'))
    try:
        await entered.wait()
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeFailure):
            await task
        assert context.producers['A'] == 'old-historical-handle'
        assert not broker._monitor.done()
        assert context.report['latency_probe']['preparations'][0]['monitor_restored_monotonic_ns'] > 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        broker._monitor.cancel()
        await asyncio.gather(broker._monitor, return_exceptions=True)


def test_absolute_calendar_receipt_subtracts_only_independent_instrument_offset():
    result = probe.calendar_receipt({'relative_offset_ms': 7}, {'capture_offset_ms': 2, 'horizon_half_width_ms': 27})
    assert result['corrected_horizon_error_ms'] == 5
    assert result['independent_half_width_ms'] == 27
    assert 'maximum_corrected_error_ms' not in result
    assert 'baseline only' in result['relative_alignment_scope']


@pytest.mark.parametrize('fault', ['absent', 'old', 'unscoped', 'different_message'])
def test_adverse_row_cannot_claim_rejection_from_absence_or_unverified_runtime_evidence(fault):
    journal = {'invocation_scoped': True, 'records': [{'text': 'Native input missed or lost its initial presentation anchor',
                                                     '__MONOTONIC_TIMESTAMP': '11000000'}]}
    if fault == 'absent':
        journal['records'] = []
    elif fault == 'old':
        journal['records'][0]['__MONOTONIC_TIMESTAMP'] = '9999999'
    elif fault == 'unscoped':
        journal['invocation_scoped'] = False
    else:
        journal['records'][0]['text'] = 'A different source ended'
    with pytest.raises(RuntimeFailure, match='current-invocation'):
        probe.startup_rejection(journal, 10_000_000_000)


def test_adverse_backend_receipt_requires_a_current_scoped_rejection():
    assert probe.startup_rejection({'invocation_scoped': True, 'records': [
        {'text': 'Native input missed or lost its initial presentation anchor', '__MONOTONIC_TIMESTAMP': '11000000'}]},
        10_000_000_000) == ['11000000']


@pytest.mark.asyncio
async def test_early_matrix_failure_retains_all_nine_rows_and_preserves_the_primary_error(tmp_path, monkeypatch):
    calls = []
    class Watched:
        def __init__(self, context, evidence):
            self.evidence = evidence
        async def initialize(self):
            calls.append('original B observer initialized')
        async def close(self):
            calls.append('original B observer closed')
            raise RuntimeFailure('Secondary cleanup failure')
    async def handoff():
        assert calls == ['original B observer initialized']
        calls.append('old monitor handed off')
    async def closed(*_args):
        return {'historical': True}
    async def failed(*_args):
        raise RuntimeFailure('Exact target did not release its device')
    monkeypatch.setattr(probe, 'UntouchedObserver', Watched)
    monkeypatch.setattr(probe, 'close_capture', closed)
    monkeypatch.setattr(probe, 'disable_target', failed)
    context = SimpleNamespace(target='A', untouched='B', states={'A': SimpleNamespace(local_pin=SimpleNamespace(manifest={})), 'B': object()},
        declared_horizon_ns=4_000_000_000, report={'group_alignment': {'relative_offset_ms': 0}}, handoff=handoff)
    with pytest.raises(RuntimeFailure, match='did not release'):
        await probe.exercise(context)
    saved = context.report['latency_probe']
    assert len(saved['rows']) == 9 and not saved['passed']
    assert all(row['status'] == 'pending' and not row['passed'] for row in saved['rows'])
    assert saved['observer_cleanup_error'] == 'RuntimeFailure'
    assert calls[-1] == 'original B observer closed'


def test_repeated_matrix_row_is_refused_and_all_other_rows_remain_retained():
    rows = [{'kind': kind, 'offset_ms': offset, 'status': 'pending', 'passed': False}
            for offset in probe.OFFSETS for kind in ('cold_idle', 'warm_idle', 'native_calendar')]
    row = probe.matrix_row(rows, 'cold_idle', -2000)
    assert row is rows[0] and row['status'] == 'running'
    with pytest.raises(RuntimeFailure):
        probe.matrix_row(rows, 'cold_idle', -2000)
    assert len(rows) == 9 and sum(row['status'] == 'pending' for row in rows) == 8


def source_reader(tmp_path, monkeypatch):
    """Real held-file bytes; explicitly simulated root ownership in the stat."""
    source = PATH.with_name('run_native_latency_probe.py')
    tree = ast.parse(source.read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'source_receipts')
    import stat
    def root_stat(descriptor):
        info = os.fstat(descriptor)
        return SimpleNamespace(**{name: getattr(info, name) for name in (
            'st_mode', 'st_nlink', 'st_size', 'st_dev', 'st_ino', 'st_mtime_ns', 'st_ctime_ns')}, st_uid=0)
    os_proxy = SimpleNamespace(open=os.open, read=os.read, close=os.close, fstat=root_stat,
        O_RDONLY=os.O_RDONLY, O_CLOEXEC=os.O_CLOEXEC, O_NOFOLLOW=os.O_NOFOLLOW)
    namespace = {'SOURCE_FILES': ('fixture.py',), 'trusted_file': lambda path: path,
                 'os': os_proxy, 'Path': Path, 'stat': stat, 'RuntimeFailure': RuntimeFailure, 'hashlib': hashlib}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), str(source), 'exec'), namespace)
    return namespace['source_receipts'], os_proxy


def test_runner_source_receipt_hashes_real_py_bytes_and_never_cached_bytecode(tmp_path, monkeypatch):
    reader, _ = source_reader(tmp_path, monkeypatch)
    data = b'print("actual admitted source")\n'
    (tmp_path/'fixture.py').write_bytes(data)
    (tmp_path/'fixture.py').chmod(0o600)
    (tmp_path/'fixture.pyc').write_bytes(b'unrelated cached bytecode')
    saved = reader(tmp_path)
    assert set(saved) == {'fixture.py'}
    assert saved['fixture.py']['sha256'] == hashlib.sha256(data).hexdigest()
    assert saved['fixture.py']['bytes'] == len(data)


@pytest.mark.parametrize('fault', ['symlink', 'oversize', 'changed_during_read'])
def test_runner_rejects_substituted_unbounded_or_mutating_source(tmp_path, monkeypatch, fault):
    reader, os_proxy = source_reader(tmp_path, monkeypatch)
    path = tmp_path/'fixture.py'
    path.write_text('admitted source\n')
    path.chmod(0o600)
    if fault == 'symlink':
        path.rename(tmp_path/'target')
        path.symlink_to(tmp_path/'target')
    elif fault == 'oversize':
        path.write_bytes(b'x'*(1024*1024+1))
    else:
        original_read = os_proxy.read
        changed = False
        def read(descriptor, count):
            nonlocal changed
            result = original_read(descriptor, count)
            if not changed:
                changed = True
                path.write_text('different protected bytes\n')
            return result
        os_proxy.read = read
    with pytest.raises((OSError, RuntimeFailure)):
        reader(tmp_path)


def group_module():
    source = PATH.with_name('check_native_grouping.py')
    spec = importlib.util.spec_from_file_location('latency_test_actual_group_math', source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
@pytest.mark.parametrize('error_ms,passes', [(0, True), (15, True), (35, False)])
async def test_real_coded_pcm_row_uses_declared_offset_and_independent_absolute_bound(monkeypatch, error_ms, passes):
    group = group_module()
    # A deliberate -2000ms output correction belongs in the declared final
    # origin. Only the independently measured capture offset is subtracted.
    expected = 12_000_000_000
    instrument_ms = 7
    capture = group.declared_capture(expected+(instrument_ms+error_ms)*1_000_000,
                                     0, probe.RATE*7)
    count = sum(len(data)//4 for data in capture.chunks)
    receipt = {'verified_blocks': len(capture.chunks), 'verified_frames': count}
    capture.sequence = SimpleNamespace(evidence=lambda: deepcopy(receipt))
    capture.poll = lambda: {'frame_continuity': receipt}
    owner = {'session_id': 'exact-native-session'}
    health = {'ready': True, 'source': {'ready': True, 'owner': owner}, 'speech_session_id': None,
              'native_blocks': 250, 'dropped_bytes': 0}
    async def rpc(*_args, **_kwargs):
        return {**health, 'source': {**health['source'], 'owner': None}} if not admitted[0] else health
    async def replace(*_args, **_kwargs):
        admitted[0] = True
        return {'command': 'simulated-command', 'account': {}}, owner
    async def closed(*_args):
        return {}
    async def disabled(*_args):
        pass
    async def ready(*_args):
        pass
    admitted = [False]
    monkeypatch.setattr(probe, 'call_rpc', rpc)
    monkeypatch.setattr(probe, 'replace_receiver', replace)
    monkeypatch.setattr(probe, 'close_capture', closed)
    monkeypatch.setattr(probe, 'disable_target', disabled)
    monkeypatch.setattr(probe, 'ready_target', ready)
    monkeypatch.setattr(probe, 'time', SimpleNamespace(monotonic_ns=lambda: 9_000_000_000, monotonic=lambda: 99.))
    capture.validation_rows = []
    monkeypatch.setattr(probe, 'start_capture', lambda *_args: capture)
    context = SimpleNamespace(target='A', captures={'A': capture}, declared_horizon_ns=4_000_000_000,
        states={'A': object()}, broker=SimpleNamespace(_worker_socket=lambda state: 'never-used'), group=group,
        pcm_guards={'A': SimpleNamespace(begin=lambda index: None, check=lambda: None)},
        onset_index=lambda cap: 0, publish_command=lambda *_args: None)
    configuration = {'saved_offset_ms': -2000, 'pin': {},
                     'capture_baseline': {'capture_offset_ms': instrument_ms, 'horizon_half_width_ms': 27}}
    rows = [{'kind': 'native_calendar', 'offset_ms': -2000, 'status': 'pending', 'passed': False}]
    if passes:
        await probe.native_row(context, Observer(), configuration, rows)
    else:
        with pytest.raises(RuntimeFailure, match='independently bounded'):
            await probe.native_row(context, Observer(), configuration, rows)
    assert rows[0]['declared_final_origin_ns'] == expected
    assert rows[0]['horizon']['corrected_horizon_error_ms'] == pytest.approx(error_ms, abs=1)
    assert rows[0]['frame_continuity'] == receipt
    assert rows[0]['alignment']['correlation'] > .99
    assert rows[0]['passed'] is passes


@pytest.mark.asyncio
async def test_original_untouched_observer_close_preserves_a_late_control_error(monkeypatch):
    evidence = {'passed': True}
    state = SimpleNamespace(desired=SimpleNamespace(model_dump=lambda **_kwargs: {}), processes={})
    capture = object()
    guard = SimpleNamespace(reference={'music': 8000}, check=lambda: None, evidence=lambda: {})
    context = SimpleNamespace(untouched='B', states={'B': state}, captures={'B': capture}, pcm_guards={'B': guard},
        broker=SimpleNamespace(sender_processes={}))
    observer = probe.UntouchedObserver(context, evidence)
    async def failed():
        raise RuntimeFailure('Untouched actor changed')
    observer.watcher = asyncio.create_task(failed())
    await asyncio.sleep(0)
    with pytest.raises(RuntimeFailure, match='actor changed'):
        await observer.close()
    assert evidence['passed'] is False
    assert evidence['untouched']['failure'] == 'Untouched actor changed'


@pytest.mark.asyncio
async def test_observed_wait_cannot_retry_away_a_sticky_control_failure():
    class Broken:
        async def check(self):
            raise RuntimeFailure('Actual untouched440 block went silent')
    called = []
    async def predicate():
        called.append(True)
        return True
    with pytest.raises(RuntimeFailure, match='went silent'):
        await probe.observed_wait(predicate, Broken(), 1, 'unused')
    assert called == []
