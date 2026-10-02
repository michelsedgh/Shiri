"""Frozen carrier and real emitted Opus across the native40ms duck ramp.

These verify the digital oracle, not an output or a phone. The onset comes
only from pre-voice PCM; corrupt voice cannot be fitted as a music ramp.
"""
from __future__ import annotations

import numpy as np
import pytest

from shiri.runtime.system import RuntimeFailure
from test_native_speech_finite import encoded_reference, finite

quiet = finite.quiet_module()


@pytest.fixture(scope='module')
def emitted():
    return encoded_reference()[0]


def scene(reference, *, start, lead=1440, phase=.31, slope=1., frame_shift=0):
    first = 9600
    baseline_frames = np.arange(quiet.BASELINE_FRAMES)
    baseline = 8192*np.sin(2*np.pi*440*baseline_frames/quiet.RATE+phase)
    baseline = np.repeat(np.trunc(baseline).astype('<i2')[:, None], 2, axis=1).tobytes()
    carrier = quiet.freeze_carrier(baseline, first_frame=0, duck_gain=.2)
    end = start+len(reference['pcm'])
    frames = np.arange(end+quiet.QUIET_FRAMES+24000)
    # Match native lround of S16 music followed by mono speech addition.
    music = np.trunc(8192*np.sin(2*np.pi*440*((first+frames+frame_shift) % quiet.RATE)/quiet.RATE+phase))
    down = np.maximum(quiet.WIRE_DUCK, 1.-np.maximum(0, frames+1-(start-lead))*slope/quiet.DUCK_FRAMES)
    gain = np.minimum(1., down+np.maximum(0, frames+1-end-6240)/quiet.RESTORE_FRAMES)
    rounded = np.sign(music*gain)*np.floor(np.abs(music*gain)+.5)
    data = np.repeat(rounded.astype(np.int32)[:, None], 2, axis=1)
    data[start:end] += reference['pcm'][:, None]
    return data.astype('<i2'), carrier, first


@pytest.mark.parametrize('start', [2400, 3120, 3851, 8160])
@pytest.mark.parametrize('lead', [960, 1440, 1920, 3840])
def test_complete_original_prefix_survives_ramp_crossing_capture_period(emitted, start, lead):
    data, carrier, first = scene(emitted, start=start, lead=lead)
    result = finite.verify_complete(data.tobytes(), emitted, start_bounds=(0, start+960), music=True,
                                   quiet_carrier=carrier, capture_first_frame=first)
    assert result['alignment_start_frame'] == start
    assert result['verified_reference_frames'] == len(emitted['pcm'])
    assert result['duck_prevoice_max_window_residual_rms'] < 1.
    assert max(row['residual_rms'] for row in result['all_windows']) < 1.
    assert result['quiet_residual_rms'] < 1.
    assert result['speech_latency_performance_passed'] is False


@pytest.mark.parametrize('fault', ['missing_first20ms', 'missing_opening10ms', 'interior20ms', 'tail20ms',
                                 'left_only', 'foreign_left', 'wrong_level', 'phase_drift',
                                 'wrong_duck_slope', 'carrier_frame_shift', 'prevoice_chirp', 'unducked'])
def test_prevoice_only_duck_admission_cannot_fit_away_voice_or_route_faults(emitted, fault):
    start = 8160
    data, carrier, first = scene(emitted, start=start,
                                 lead=0 if fault == 'unducked' else 1440,
                                 slope=1.2 if fault == 'wrong_duck_slope' else 1.,
                                 frame_shift=1 if fault == 'carrier_frame_shift' else 0)
    data = data.astype(np.int32)
    end = start+len(emitted['pcm'])
    if fault == 'missing_first20ms':
        data[start:start+960] -= emitted['pcm'][:960, None]
    elif fault == 'missing_opening10ms':
        # The first decoded sample can be zero; erase a measurable opening
        # span rather than pretending one S16-zero sample is audible loss.
        data[start:start+480] -= emitted['pcm'][:480, None]
    elif fault == 'interior20ms':
        data[start+24000:start+24960] -= emitted['pcm'][24000:24960, None]
    elif fault == 'tail20ms':
        at = finite.BODY_FRAMES-960
        data[start+at:start+at+960] -= emitted['pcm'][at:at+960, None]
    elif fault == 'left_only':
        data[start:end, 1] -= emitted['pcm']
    elif fault == 'foreign_left':
        data[start+24000:start+24960, 0] += 1000
    elif fault == 'wrong_level':
        data[start:end] -= np.rint(emitted['pcm']*.2).astype(np.int32)[:, None]
    elif fault == 'phase_drift':
        at = start+finite.BODY_FRAMES//2
        data[at:end-1] += emitted['pcm'][finite.BODY_FRAMES//2+1:, None]-emitted['pcm'][finite.BODY_FRAMES//2:-1, None]
    elif fault == 'prevoice_chirp':
        data[start-960:start] += finite.waveform(body_frames=8*960, seed=13)[:960, None]
    with pytest.raises(RuntimeFailure):
        finite.verify_complete(data.astype('<i2').tobytes(), emitted, start_bounds=(0, start+960), music=True,
                               quiet_carrier=carrier, capture_first_frame=first)
