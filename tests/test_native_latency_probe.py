"""Meaningful opt-in fixture regressions; no VM, sysfs or hardware is opened."""
import asyncio
import ast
from collections import deque
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


class Observer:
    def __init__(self):
        self.checks = 0
    async def check(self):
        self.checks += 1


def test_repeated_matrix_row_is_refused_and_all_other_rows_remain_retained():
    rows = [{'kind': kind, 'offset_ms': offset, 'status': 'pending', 'passed': False}
            for offset in (-2000, 0, 2000) for kind in ('cold_idle', 'warm_idle', 'native_calendar')]
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
