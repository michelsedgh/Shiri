"""Declared restoration is removable; decoded voice and altered calendars are not."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
import importlib.util
from pathlib import Path
import sys

import numpy as np
import pytest

from shiri.runtime.system import RuntimeFailure
from test_native_speech_finite import encoded_reference, finite

spec = importlib.util.spec_from_file_location('declared_finite_quiet_test', Path(__file__).with_name('linux')/'native_finite_quiet.py')
quiet = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = quiet
spec.loader.exec_module(quiet)


def full_music(first, count, *, phase=.31):
    return 8192*np.sin(2*np.pi*440*((first+np.arange(count)) % quiet.RATE)/quiet.RATE+phase)


def stereo(values):
    return np.repeat(np.trunc(values).astype('<i2')[:, None], 2, axis=1)


@pytest.fixture
def carrier():
    raw = stereo(full_music(0, quiet.BASELINE_FRAMES)).tobytes()
    return quiet.freeze_carrier(raw, first_frame=0, duck_gain=.2)


def restored(first, count, onset, *, slope=1., phase=.31):
    gain = np.minimum(1., quiet.WIRE_DUCK+np.maximum(0, np.arange(count)+1-onset)*slope/quiet.RESTORE_FRAMES)
    return stereo(full_music(first, count, phase=phase)*gain)


@pytest.mark.parametrize('onset', [-12000, -4800, 0, 6240, 12000])
def test_one_native_fixed_slope_and_phase_passes_whole_quiet_and_continuation(carrier, onset):
    result = quiet.verify_quiet(restored(9600, 33600, onset), carrier, first_frame=9600)
    assert result['quiet_residual_rms'] < 1.
    assert result['continuation_max_residual_rms'] < 1.
    assert result['continuation_verified_frames'] == 24000
    assert result['restored_gain1_verified_frames'] > 0
    if onset in (0, 6240):
        assert abs(result['restore_onset_quiet_frame']-onset) <= 1
    with pytest.raises(FrozenInstanceError):
        carrier.coefficients = (0., 0.)


@pytest.mark.parametrize('fault', ['phase', 'slope_fast', 'slope_slow', 'nonlinear', 'stereo', 'frame_shift', 'chirp', 'late_chirp', 'wrong_gain'])
def test_one_restore_onset_cannot_hide_wrong_phase_slope_stereo_calendar_or_voice(carrier, fault):
    first, count, onset = 9600, 33600, 6240
    data = restored(first, count, onset, slope=1.2 if fault == 'slope_fast' else .8 if fault == 'slope_slow' else 1.,
                    phase=.36 if fault == 'phase' else .31).astype(np.int32)
    if fault == 'nonlinear':
        t = np.arange(count)/quiet.RATE
        data += stereo(300*np.sin(2*np.pi*440*t+.31)*np.sin(2*np.pi*9*t)).astype(np.int32)
    elif fault == 'stereo':
        data[3000:5000, 1] += 1
    elif fault == 'frame_shift':
        first += 1
    elif fault in {'chirp', 'late_chirp'}:
        at = 1920 if fault == 'chirp' else quiet.QUIET_FRAMES+1920
        data[at:at+960] += finite.waveform(body_frames=8*960, seed=13)[:960, None]
    elif fault == 'wrong_gain':
        data = np.trunc(data*.97).astype(np.int32)
    with pytest.raises(RuntimeFailure):
        quiet.verify_quiet(data.astype('<i2'), carrier, first_frame=first)


@pytest.mark.parametrize('fault', ['size', 'duck', 'amplitude', 'stereo', 'unsteady', 'origin'])
def test_preoffer_freeze_rejects_wrong_baseline(fault):
    raw = stereo(full_music(0, quiet.BASELINE_FRAMES))
    first, duck = 0, .2
    if fault == 'size':
        raw = raw[:-1]
    elif fault == 'duck':
        duck = .28
    elif fault == 'amplitude':
        raw = stereo(full_music(0, quiet.BASELINE_FRAMES)*.2)
    elif fault == 'stereo':
        raw[5, 1] += 1
    elif fault == 'unsteady':
        raw = stereo(full_music(0, quiet.BASELINE_FRAMES)*np.linspace(.99, 1.01, quiet.BASELINE_FRAMES))
    else:
        first = -1
    with pytest.raises(RuntimeFailure):
        quiet.freeze_carrier(raw.tobytes(), first_frame=first, duck_gain=duck)


@pytest.fixture(scope='module')
def emitted():
    return encoded_reference()[0]


def complete_native(reference):
    # Independent baseline precedes the capture; fixture phase is bound to its
    # exact frame offset, not a voice alignment or a downstream timestamp.
    baseline = stereo(full_music(0, quiet.BASELINE_FRAMES)).tobytes()
    carrier = finite.quiet_module().freeze_carrier(baseline, first_frame=0, duck_gain=.2)
    first, start = 9600, 1440
    end = start+len(reference['pcm'])
    count = end+quiet.QUIET_FRAMES+24000
    positions = np.arange(count)
    gain = np.minimum(1., quiet.WIRE_DUCK+np.maximum(0, positions+1-end-6240)/quiet.RESTORE_FRAMES)
    data = stereo(full_music(first, count)*gain).astype(np.int32)
    data[start:end] += reference['pcm'][:, None]
    return data.astype('<i2'), carrier, first, start


def test_full_original_decoded_prefix_body_codec_and_quiet_pass_with_known_native_restoration(emitted):
    data, carrier, first, start = complete_native(emitted)
    result = finite.verify_complete(data.tobytes(), emitted, start_bounds=(0, 2400), music=True,
                                   quiet_carrier=carrier, capture_first_frame=first)
    assert result['alignment_start_frame'] == start
    assert result['verified_body_frames'] == finite.BODY_FRAMES
    assert result['verified_codec_tail_frames'] == finite.CODEC_TAIL_FRAMES
    assert result['verified_quiet_frames'] == finite.QUIET_FRAMES
    assert result['quiet_residual_rms'] < 1
    assert result['continuation_verified_frames'] == 24000
    assert result['speech_latency_performance_passed'] is False


@pytest.mark.parametrize('fault', ['delayed_decoded_voice', 'misplaced_codec_tail', 'missing_prefix', 'interior_loss', 'foreign_left'])
def test_declared_music_model_cannot_hide_exact_decoded_voice_tail_or_content_loss(emitted, fault):
    raw, carrier, first, start = complete_native(emitted)
    data = raw.astype(np.int32)
    end = start+len(emitted['pcm'])
    if fault == 'delayed_decoded_voice':
        data[end+960:end+2880] += emitted['pcm'][1920:3840, None]
    elif fault == 'misplaced_codec_tail':
        data[end+finite.QUIET_FRAMES+960:end+finite.QUIET_FRAMES+2880] += emitted['pcm'][1920:3840, None]
    elif fault == 'foreign_left':
        data[end+1920:end+2880, 0] += emitted['pcm'][1920:2880]
    else:
        at = start if fault == 'missing_prefix' else start+24000
        data[at:at+960] -= emitted['pcm'][at-start:at-start+960, None]
    with pytest.raises(RuntimeFailure):
        finite.verify_complete(data.astype('<i2').tobytes(), emitted, start_bounds=(0, 2400), music=True,
                               quiet_carrier=carrier, capture_first_frame=first)
