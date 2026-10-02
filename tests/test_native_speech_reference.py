"""Local decoded-reference prototype proofs; no hardware or manual fixture runs."""
import asyncio
from collections import deque
from copy import deepcopy
from dataclasses import FrozenInstanceError
from fractions import Fraction
import hashlib
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import threading
from uuid import uuid4

import pytest

from shiri.runtime.native import NativeMixer
from shiri.runtime.system import RuntimeFailure

np = pytest.importorskip('numpy')
pytest.importorskip('aiortc')
OpusEncoder = pytest.importorskip('aiortc.codecs.opus').OpusEncoder
AudioFrame = pytest.importorskip('av').AudioFrame

ROOT = Path(__file__).parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT/'linux'/filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


reference = load('speech_reference_prototype_tests', 'native_speech_reference.py')
stress = load('speech_reference_measure_tests', 'native_speech_stress.py')
historical_replay = pytest.mark.skipif(
    os.environ.get('SHIRI_HISTORICAL_REPLAY') != '1',
    reason='Historical stress28 replay lacks original Opus payloads; same-settings encoding is platform dependent',
)


def encoded_reference(frequency=1320, packets=8):
    decoder, encoder = reference.DecodedReference(str(uuid4())), OpusEncoder()
    payloads = []
    for index in range(packets):
        values = (600*np.sin(2*np.pi*frequency*(index*960+np.arange(960))/48000)).astype('<i2')
        frame = AudioFrame(format='s16', layout='mono', samples=960)
        frame.planes[0].update(values.tobytes())
        frame.sample_rate, frame.pts, frame.time_base = 48000, index*960, Fraction(1, 48000)
        packets_out, pts = encoder.encode(frame)
        assert len(packets_out) == 1 and pts == index*960
        payloads.append(packets_out[0])
        decoder.feed(packets_out[0], pts)
    return decoder, payloads


def terminal_observation(session_id, action='cancel'):
    room = str(uuid4())
    identity = {'session_id': session_id, 'request_id': str(uuid4())}
    owner = {'zone_id': room, 'incarnation': str(uuid4()), 'session_id': str(uuid4()), 'epoch': 1}
    begin = {'room_id': room.replace('-', ''), 'incarnation': owner['incarnation'].replace('-', ''),
        'session_id': owner['session_id'].replace('-', ''), 'epoch': 1, 'generation': 1,
        'operation_generation': 2, 'launch_generation': uuid4().hex, 'speech_id': uuid4().hex,
        'action': 'begin', 'connected': True, 'ready': True, 'prepared_monotonic_ns': 0,
        'mixed_monotonic_ns': 100_000_000, 'output_count': 1}
    health = {'speech_startup_authenticated_begin_ack': deepcopy(begin),
        'speech_startup_authenticated_retirement_ack': {**begin, 'action': action,
            'connected': False, 'ready': False, 'prepared_monotonic_ns': 0, 'mixed_monotonic_ns': 0, 'output_count': 0},
        'speech_startup_identity': {**identity, 'speech_id': begin['speech_id']}, 'source': {'owner': deepcopy(owner)},
        'speech_session_id': None, 'speech_cleanup_pending': 0, 'speech_ready': True, 'speech_cleanup_error': None}
    return health, room, identity, owner, begin


@pytest.mark.parametrize('action', ['cancel', 'finish'])
def test_only_exact_authenticated_terminal_admits_the_current_voice_and_music_owner(action):
    values = terminal_observation(str(uuid4()), action)
    result = reference.admit_terminal(*values)
    assert result.action == action and result.session_id == values[2]['session_id']
    assert result.speech_id == values[4]['speech_id'] and len(result.scope) == 8
    with pytest.raises(FrozenInstanceError):
        result.action = 'cancel'


@pytest.mark.parametrize('fault', ['missing', 'extra', 'unknown', 'connected', 'ready', 'counter', 'boolcounter',
    'stalevoice', 'stalemusic', 'staleepoch', 'api', 'request', 'begin', 'native-id', 'pending', 'cleanup', 'source'])
def test_terminal_qualification_rejects_unknown_stale_or_unretired_scope(fault):
    health, room, identity, owner, begin = terminal_observation(str(uuid4()))
    end = health['speech_startup_authenticated_retirement_ack']
    if fault == 'missing':
        health['speech_startup_authenticated_retirement_ack'] = None
    elif fault == 'extra':
        end['unexpected'] = 0
    elif fault == 'unknown':
        end['action'] = 'close'
    elif fault in ('connected', 'ready'):
        end[fault] = True
    elif fault in ('counter', 'boolcounter'):
        end['output_count'] = 1 if fault == 'counter' else False
    elif fault in ('stalevoice', 'stalemusic', 'staleepoch'):
        key = {'stalevoice': 'speech_id', 'stalemusic': 'session_id', 'staleepoch': 'epoch'}[fault]
        end[key] = uuid4().hex if key != 'epoch' else 2
    elif fault == 'api':
        health['speech_startup_identity']['session_id'] = str(uuid4())
    elif fault == 'request':
        health['speech_startup_identity']['request_id'] = str(uuid4())
    elif fault == 'begin':
        health['speech_startup_authenticated_begin_ack']['launch_generation'] = uuid4().hex
    elif fault == 'native-id':
        begin['speech_id'] = 'z'*32
        health['speech_startup_authenticated_begin_ack'] = deepcopy(begin)
        end['speech_id'] = begin['speech_id']
    elif fault == 'pending':
        health['speech_cleanup_pending'] = 1
    elif fault == 'cleanup':
        health['speech_cleanup_error'] = 'not retired'
    else:
        health['source']['owner']['session_id'] = str(uuid4())
    with pytest.raises(RuntimeFailure):
        reference.admit_terminal(health, room, identity, owner, begin)


@pytest.mark.parametrize('remainder', [1, 120, 480, 959])
def test_sample_level_cancel_has_one_endpoint_and_natural_finish_keeps_whole_packets(remainder):
    decoder, _ = encoded_reference(packets=96)
    decoded = decoder.snapshot()
    carrier = reference.Carrier(music(0, 2400), next_frame=2400)
    cutoff, first, count = 48*960+remainder, 36*960, 36*960
    ended, restored = reference.Alignment(decoder.session_id, 1920, cutoff), reference.GainPlan(1920, cutoff)
    blocks = tuple(native_block(at, carrier, decoded, ended, restored) for at in range(0, first+count, 960))
    data = b''.join(blocks[first//960:])
    active, gain = reference.Alignment(decoder.session_id, 1920), reference.GainPlan(1920)
    values = terminal_observation(decoder.session_id)
    terminal = reference.admit_terminal(*values)
    proposals = []
    alignment, final_gain, result = reference.admit_restore(data, first, carrier, decoded, active, gain,
        (first, first+count-9600), 880, stress.spectrum, terminal=terminal, proposed=proposals.append)
    assert alignment.stop_frame == final_gain.restore_frame == cutoff
    assert result['terminal'] == 'cancel' and result['cutoff_integer_candidates'] <= 36000
    assert proposals[0]['alignment_stop_frame'] == proposals[0]['restore_frame'] == cutoff
    assert reference.verify_session(blocks, 0, carrier, decoded, alignment, final_gain, 880, stress.spectrum)['verified']
    natural = reference.admit_terminal(*terminal_observation(decoder.session_id, 'finish'))
    with pytest.raises(RuntimeFailure, match='immutable'):
        reference.admit_restore(data, first, carrier, decoded, active, gain,
            (first, first+count-9600), 880, stress.spectrum, terminal=natural)
    full = 50*960+1920
    full_alignment, full_gain = reference.Alignment(decoder.session_id, 1920, full), reference.GainPlan(1920, full)
    complete = b''.join(native_block(at, carrier, decoded, full_alignment, full_gain)
                        for at in range(first, first+count, 960))
    admitted, natural_gain, _ = reference.admit_restore(complete, first, carrier, decoded, active, gain,
        (first, first+count-9600), 880, stress.spectrum, terminal=natural)
    assert admitted.stop_frame == natural_gain.restore_frame == full


@pytest.mark.parametrize('fault', ['voice-phase', 'foreign', 'stale-own', 'missing-music', 'stereo', 'restore-law'])
def test_sample_cancel_proposal_never_fits_away_original_whole_buffer_defects(fault):
    decoder, _ = encoded_reference(packets=96)
    decoded = decoder.snapshot()
    carrier = reference.Carrier(music(0, 2400), next_frame=2400)
    cutoff, first, count = 48*960+480, 36*960, 36*960
    ended, gain = reference.Alignment(decoder.session_id, 1920, cutoff), reference.GainPlan(1920, cutoff)
    blocks = [native_block(at, carrier, decoded, ended, gain) for at in range(first, first+count, 960)]
    if fault == 'restore-law':
        bad = reference.GainPlan(1920, cutoff+480)
        blocks = [native_block(at, carrier, decoded, ended, bad) for at in range(first, first+count, 960)]
    else:
        at = 25 if fault == 'stale-own' else 5
        pcm = np.frombuffer(blocks[at], dtype='<i2').reshape(-1, 2).copy()
        if fault == 'missing-music':
            pcm[576:] = 0
        elif fault == 'stereo':
            pcm[360:600, 1] += 20
        else:
            hz = 880 if fault == 'foreign' else 1320
            marker = (600*np.sin(2*np.pi*hz*np.arange(240)/48000+.37)).astype('<i2')
            pcm[360:600] += marker[:, None]
        blocks[at] = pcm.tobytes()
    terminal = reference.admit_terminal(*terminal_observation(decoder.session_id))
    with pytest.raises(RuntimeFailure):
        reference.admit_restore(b''.join(blocks), first, carrier, decoded,
            reference.Alignment(decoder.session_id, 1920), reference.GainPlan(1920),
            (first, first+count-9600), 880, stress.spectrum, terminal=terminal)


def music(first, count=960, *, speech=0, frequency=1320):
    times = (first+np.arange(count))/48000
    mono = (8192*np.sin(2*np.pi*440*times)+speech*np.sin(2*np.pi*frequency*times)).astype('<i2')
    return np.repeat(mono[:, None], 2, axis=1).tobytes()


def native_block(first, carrier, decoded, alignment, gain, count=960):
    """Use the real native gain/mix seam, not a mirror of the verifier."""
    mixer = NativeMixer.__new__(NativeMixer)
    # Set only the real mixer's initial state at the start of this test block.
    mixer._gain = float(gain.values(np.array([first-1], dtype=np.int64))[0])
    frames = np.arange(first, first+count, dtype=np.int64)
    index = frames-alignment.start_frame
    active = (index >= 0) & (index < len(decoded))
    if alignment.stop_frame is not None:
        active &= frames < alignment.stop_frame
    voice = np.zeros(count, dtype='<i2')
    voice[active] = decoded[index[active]]
    mixer.speech = deque(map(int, voice))
    raw = np.repeat(carrier.values(frames).astype('<i2')[:, None], 2, axis=1).tobytes()
    boundaries = sorted({first, first+count, *(point for point in (gain.duck_frame, gain.restore_frame)
                        if point is not None and first < point < first+count)})
    parts = []
    for start, end in zip(boundaries, boundaries[1:], strict=False):
        mixer._target_gain = (1. if start < gain.duck_frame
                              or gain.restore_frame is not None and start >= gain.restore_frame else .2)
        parts.append(mixer._mix(raw[(start-first)*4:(end-first)*4], end-start))
    return b''.join(parts)


def check(data, first, carrier, decoded, alignment, gain, foreign=880):
    return reference.verify_block(data, first, carrier, decoded, alignment, gain, foreign, stress.spectrum)


@historical_replay
def test_retained_actual28_music_corner_and_codec_onset_reference_replay():
    a = (ROOT/'fixtures/stress28/a.pcm').read_bytes()
    b = (ROOT/'fixtures/stress28/b.pcm').read_bytes()
    assert hashlib.sha256(a).hexdigest() == '3562a897ec7acae0cb7ce5b647c35e59273a367f6da8367836eea69d4f327ccb'
    assert hashlib.sha256(b).hexdigest() == '4ff63afbb1db8b707a49c27dfe219bc916435b9439b9eab44350beb4af678780'
    decoder, _ = encoded_reference(packets=4)
    decoded = decoder.snapshot()
    carrier = reference.Carrier(a[:2400*4], next_frame=2400)
    alignment = reference.Alignment(decoder.session_id, 2688)
    gain = reference.GainPlan(2688)
    # This replay uses independently encoded identical source settings: the
    # historical run did not retain payload bytes. It diagnoses the old model;
    # future acceptance requires the exact produced/received-payload proof.
    assert stress.spectrum(b[2*3840:3*3840])['voice'][880] > 10
    assert stress.spectrum(b[3*3840:])['voice'][880] > 20
    receipts = [check(b[i*3840:(i+1)*3840], i*960, carrier, decoded, alignment, gain) for i in range(4)]
    assert max(row['foreign_rms'] for row in receipts) < .32
    assert max(row['residual_rms'] for row in receipts) < 4


@pytest.mark.parametrize('duck', [0, 120, 768, 960])
def test_real_native_piecewise_gain_and_exact_decoded_opus_are_not_foreign(duck):
    decoder, _ = encoded_reference()
    decoded = decoder.snapshot()
    carrier = reference.Carrier(music(0, 2400), next_frame=2400)
    alignment = reference.Alignment(decoder.session_id, duck)
    gain = reference.GainPlan(duck)
    for first in range(0, 5760, 960):
        data = native_block(first, carrier, decoded, alignment, gain)
        receipt = check(data, first, carrier, decoded, alignment, gain)
        assert receipt['residual_rms'] <= 1 and receipt['foreign_rms'] < 1
    restore = reference.GainPlan(duck, 5760)
    ended = reference.Alignment(decoder.session_id, duck, 5760)
    for first in range(5760, 20160, 960):
        data = native_block(first, carrier, decoded, ended, restore)
        assert check(data, first, carrier, decoded, ended, restore)['residual_rms'] <= 1


@pytest.mark.parametrize('edge', [0, 120, 360, 720])
@pytest.mark.parametrize('phase', [i*np.pi/8 for i in range(16)])
def test_fixed_reference_cannot_absorb_any_phase_five_ms_foreign_voice(edge, phase):
    decoder, _ = encoded_reference()
    decoded = decoder.snapshot()
    carrier = reference.Carrier(music(0, 2400), next_frame=2400)
    alignment, gain = reference.Alignment(decoder.session_id, 120), reference.GainPlan(120)
    first = 2880
    data = native_block(first, carrier, decoded, alignment, gain)
    contaminated = np.frombuffer(data, dtype='<i2').reshape(-1, 2).copy()
    marker = (600*np.sin(2*np.pi*880*np.arange(240)/48000+phase)).astype('<i2')
    contaminated[edge:edge+240] += marker[:, None]
    with pytest.raises(RuntimeFailure, match='immutable|wrong-room'):
        check(contaminated.tobytes(), first, carrier, decoded, alignment, gain)
    assert alignment.start_frame == 120 and gain.duck_frame == 120


@pytest.mark.parametrize('edge', [0, 120, 360, 720])
def test_actual_foreign_decoded_payload_and_music_omission_cannot_be_normalized(edge):
    decoder, _ = encoded_reference()
    foreign, _ = encoded_reference(880)
    decoded = decoder.snapshot()
    carrier = reference.Carrier(music(0, 2400), next_frame=2400)
    alignment, gain = reference.Alignment(decoder.session_id, 120), reference.GainPlan(120)
    data = native_block(2880, carrier, decoded, alignment, gain)
    contaminated = np.frombuffer(data, dtype='<i2').reshape(-1, 2).copy()
    contaminated[edge:edge+240] += foreign.snapshot()[1920:2160, None]
    with pytest.raises(RuntimeFailure, match='immutable|wrong-room'):
        check(contaminated.tobytes(), 2880, carrier, decoded, alignment, gain)
    cut = np.frombuffer(data, dtype='<i2').reshape(-1, 2).copy()
    cut[576:] = 0  # Exact independent384-frame /8ms defect class.
    with pytest.raises(RuntimeFailure, match='immutable'):
        check(cut.tobytes(), 2880, carrier, decoded, alignment, gain)


@pytest.mark.parametrize('speech,frequency', [(0, 1320), (100, 880), (100, 1320)])
def test_carrier_is_bounded_and_never_learns_peer_or_own_marker(speech, frequency):
    if speech:
        with pytest.raises(RuntimeFailure, match='speech'):
            reference.Carrier(music(0, 2400, speech=speech, frequency=frequency), next_frame=2400)
    else:
        with pytest.raises(RuntimeFailure, match='pure music'):
            reference.Carrier(bytes(2400*4), next_frame=2400)
    with pytest.raises(RuntimeFailure, match='two whole'):
        reference.Carrier(music(0, 4801), next_frame=4801)


@pytest.mark.parametrize('bad', [b'', b'x'*2049, bytearray(b'123')])
def test_packet_contract_failure_is_permanent_and_does_not_advance_reference(bad):
    decoder = reference.DecodedReference(str(uuid4()))
    with pytest.raises(RuntimeFailure):
        decoder.feed(bad, 0)
    assert decoder.packet_count == decoder.next_pts == decoder.total == 0 and decoder.error
    with pytest.raises(RuntimeFailure, match='permanently fenced'):
        decoder.feed(b'ok', 0)
    with pytest.raises(RuntimeFailure, match='failed or cleared'):
        decoder.snapshot()


@pytest.mark.parametrize('pts', [True, -960, 960, 1920])
def test_exact_packet_calendar_rejects_drop_replay_or_reorder(pts):
    decoder, payloads = encoded_reference(packets=1)
    if pts == 960:
        decoder.feed(payloads[0], pts)
        assert decoder.packet_count == 2
    else:
        with pytest.raises(RuntimeFailure, match='dropped, repeated or reordered'):
            decoder.feed(payloads[0], pts)
        assert decoder.packet_count == 1


def test_memory_fence_copy_lifetime_and_immutable_alignment():
    decoder, payloads = encoded_reference(packets=1)
    decoder.total = reference.REFERENCE_BYTES-960*2
    decoder.feed(payloads[0], 960)
    assert decoder.total == reference.REFERENCE_BYTES
    with pytest.raises(RuntimeFailure, match='8MiB'):
        decoder.feed(payloads[0], 1920)
    assert decoder.packet_count == 2 and decoder.next_pts == 1920
    decoder.clear()
    assert decoder.closed and decoder.decoder is decoder.resampler is None and not decoder.parts
    alignment = reference.Alignment(str(uuid4()), 0)
    with pytest.raises(FrozenInstanceError):
        alignment.start_frame = 1


def test_snapshot_authority_cannot_be_made_writable_even_after_source_changes_or_clear():
    decoder, payloads = encoded_reference(packets=1)
    frozen = decoder.snapshot()
    original = frozen.tobytes()
    with pytest.raises(ValueError):
        frozen.pcm.flags.writeable = True
    with pytest.raises(ValueError):
        frozen.pcm[0] = 32767
    decoder.feed(payloads[0], 960)
    decoder.clear()
    assert frozen.tobytes() == original and frozen.packet_count == 1
    with pytest.raises(ValueError):
        frozen.pcm.flags.writeable = True


@historical_replay
def test_onset_search_recovers_actual28_once_and_never_refits_a_later_buffer():
    a = (ROOT/'fixtures/stress28/a.pcm').read_bytes()
    b = (ROOT/'fixtures/stress28/b.pcm').read_bytes()
    decoder, _ = encoded_reference(packets=4)
    decoded = decoder.snapshot()
    carrier = reference.Carrier(a[:9600], next_frame=2400)
    alignment, gain, evidence = reference.admit_onset(b, 0, carrier, decoded, decoder.session_id,
                                                     (0, 2880), (0, 2880), 880, stress.spectrum)
    assert alignment.start_frame == gain.duck_frame == 2688
    assert evidence['checked_frames'] == 3840 and evidence['maximum_foreign_rms'] < .32
    assert evidence['work_seconds'] <= reference.ADMISSION_SECONDS
    assert evidence['maximum_fft_points'] == 32768 and evidence['maximum_rounds'] == 15
    shifted = reference.Alignment(decoder.session_id, alignment.start_frame+1)
    with pytest.raises(RuntimeFailure, match='immutable'):
        check(b[-3840:], 2880, carrier, decoded, shifted, gain)
    assert alignment.start_frame == 2688 and gain.duck_frame == 2688


@pytest.mark.parametrize('offset', [-960, 0, 960])
def test_onset_search_selects_one_integer_mapping_with_fixed_gain_level(offset):
    decoder, _ = encoded_reference(packets=12)
    decoded = decoder.snapshot()
    carrier = reference.Carrier(music(0, 2400), next_frame=2400)
    gain = reference.GainPlan(2688)
    original = reference.Alignment(decoder.session_id, 2688+offset)
    data = b''.join(native_block(first, carrier, decoded, original, gain) for first in range(0, 9600, 960))
    alignment, proposed, receipt = reference.admit_onset(data, 0, carrier, decoded, decoder.session_id,
                                             (0, 3840), (0, 7680), 880, stress.spectrum)
    assert alignment.start_frame == original.start_frame and proposed.duck_frame == gain.duck_frame
    assert receipt['maximum_residual_rms'] <= 1
    assert not decoded.pcm.flags.writeable
    with pytest.raises(ValueError):
        decoded.pcm[0] = 200


@pytest.mark.parametrize('phase', [i*np.pi/4 for i in range(8)])
@pytest.mark.parametrize('edge', [0, 120, 360, 720])
def test_onset_admission_cannot_fit_away_five_ms_wrong_room_wave(phase, edge):
    decoder, _ = encoded_reference(packets=12)
    decoded = decoder.snapshot()
    carrier = reference.Carrier(music(0, 2400), next_frame=2400)
    gain, original = reference.GainPlan(2688), reference.Alignment(decoder.session_id, 2688)
    data = b''.join(native_block(first, carrier, decoded, original, gain) for first in range(0, 9600, 960))
    samples = np.frombuffer(data, dtype='<i2').reshape(-1, 2).copy()
    start = 2880+edge
    marker = (600*np.sin(2*np.pi*880*np.arange(240)/48000+phase)).astype('<i2')
    samples[start:start+240] += marker[:, None]
    with pytest.raises(RuntimeFailure, match='immutable|wrong-room'):
        reference.admit_onset(samples.tobytes(), 0, carrier, decoded, decoder.session_id,
                              (0, 3840), (0, 7680), 880, stress.spectrum)


@pytest.mark.parametrize('restore_minus_stop', [-5760, 0, 5760])
def test_closed_prefix_and_restore_are_admitted_once_then_full_tail_is_checked(restore_minus_stop):
    decoder, _ = encoded_reference(packets=64)
    decoded = decoder.snapshot()
    carrier = reference.Carrier(music(0, 2400), next_frame=2400)
    active_alignment, active_gain = reference.Alignment(decoder.session_id, 0), reference.GainPlan(0)
    cutoff, first = 48*960, 42*960
    restored_gain = reference.GainPlan(0, cutoff+restore_minus_stop)
    ended = reference.Alignment(decoder.session_id, 0, cutoff)
    data = b''.join(native_block(at, carrier, decoded, ended, restored_gain)
                    for at in range(first, first+24000, 960))
    alignment, gain, receipt = reference.admit_restore(data, first, carrier, decoded, active_alignment,
                            active_gain, (first, first+14400), 880, stress.spectrum)
    assert alignment.start_frame == 0 and alignment.stop_frame == cutoff
    assert gain.duck_frame == 0 and gain.restore_frame == restored_gain.restore_frame
    assert receipt['whole_packet_candidates'] <= 26 and receipt['maximum_residual_rms'] <= 1
    tail = native_block(first+24000, carrier, decoded, alignment, gain)
    assert check(tail, first+24000, carrier, decoded, alignment, gain)['residual_rms'] <= 1
    contaminated = np.frombuffer(tail, dtype='<i2').reshape(-1, 2).copy()
    contaminated[720:] += decoded[1920:2160, None]
    with pytest.raises(RuntimeFailure, match='immutable'):
        check(contaminated.tobytes(), first+24000, carrier, decoded, alignment, gain)


def test_maximum_eof_lease_then_real_native_restore_and_clean_tail_fit_declared_window():
    decoder, _ = encoded_reference(packets=96)
    decoded = decoder.snapshot()
    carrier = reference.Carrier(music(0, 2400), next_frame=2400)
    cutoff = 50*960
    first = cutoff-960
    ended, gain = reference.Alignment(decoder.session_id, 0, cutoff), reference.GainPlan(0, cutoff+13920)
    data = b''.join(native_block(at, carrier, decoded, ended, gain)
                    for at in range(first, first+35520, 960))
    alignment, restored, receipt = reference.admit_restore(data, first, carrier, decoded,
        reference.Alignment(decoder.session_id, 0), reference.GainPlan(0), (first, first+25920), 880, stress.spectrum)
    assert alignment.stop_frame == cutoff and restored.restore_frame == cutoff+13920
    assert receipt['whole_packet_candidates'] <= 39 and receipt['maximum_residual_rms'] <= 1
    tail = native_block(first+35520, carrier, decoded, alignment, restored)
    assert check(tail, first+35520, carrier, decoded, alignment, restored)['residual_rms'] <= 1
    contaminated = np.frombuffer(tail, dtype='<i2').reshape(-1, 2).copy()
    contaminated[720:] += (600*np.sin(2*np.pi*880*np.arange(240)/48000+.2)).astype('<i2')[:, None]
    with pytest.raises(RuntimeFailure, match='immutable|wrong-room'):
        check(contaminated.tobytes(), first+35520, carrier, decoded, alignment, restored)


def test_restore_memory_calendar_and_deadline_rejections_are_bounded(monkeypatch):
    decoder, _ = encoded_reference(packets=96)
    decoded = decoder.snapshot()
    carrier = reference.Carrier(music(0, 2400), next_frame=2400)
    alignment, gain = reference.Alignment(decoder.session_id, 0), reference.GainPlan(0)
    with pytest.raises(RuntimeFailure, match='200..750ms'):
        reference.admit_restore(bytes(38*3840), 40000, carrier, decoded, alignment, gain,
                               (40000, 41000), 880, stress.spectrum)
    clock = iter([0, 1.000001])
    monkeypatch.setattr(reference, 'time', SimpleNamespace(monotonic=lambda: next(clock)))
    with pytest.raises(RuntimeFailure, match='1s work budget'):
        reference.admit_restore(bytes(30*3840), 40000, carrier, decoded, alignment, gain,
                               (40000, 41000), 880, stress.spectrum)


def test_session_bounds_cancellation_and_deadline_cannot_produce_admitted_evidence(monkeypatch):
    decoder, _ = encoded_reference(packets=12)
    decoded = decoder.snapshot()
    carrier = reference.Carrier(music(0, 2400), next_frame=2400)
    data = b''.join(music(first) for first in range(0, 9600, 960))
    with pytest.raises(RuntimeFailure, match='onset PCM'):
        reference.admit_onset(data, 0, carrier, decoded, str(uuid4()), (0, 3840), (0, 7680), 880, stress.spectrum)
    with pytest.raises(RuntimeFailure, match='integer bounds'):
        reference.admit_onset(data, 0, carrier, decoded, decoder.session_id, (0, 5000), (0, 7680), 880, stress.spectrum)
    canceled = threading.Event()
    canceled.set()
    with pytest.raises(RuntimeFailure, match='canceled'):
        reference.admit_onset(data, 0, carrier, decoded, decoder.session_id, (0, 3840), (0, 7680),
                              880, stress.spectrum, canceled=canceled)
    clock = iter([0, 1.000001])
    monkeypatch.setattr(reference, 'time', SimpleNamespace(monotonic=lambda: next(clock)))
    with pytest.raises(RuntimeFailure, match='1s work budget'):
        reference.admit_onset(data, 0, carrier, decoded, decoder.session_id, (0, 3840), (0, 7680), 880, stress.spectrum)


def session_vector():
    decoder, _ = encoded_reference(packets=64)
    decoded = decoder.snapshot()
    carrier = reference.Carrier(music(0, 2400), next_frame=2400)
    alignment, gain = reference.Alignment(decoder.session_id, 1920, 48000), reference.GainPlan(1920, 48000)
    blocks = tuple(native_block(first, carrier, decoded, alignment, gain) for first in range(0, 62400, 960))
    return decoder, decoded, carrier, alignment, gain, blocks


def test_audible_receipt_requires_entire_session_and_closed_tail_before_reference_is_cleared():
    decoder, decoded, carrier, alignment, gain, blocks = session_vector()
    result = reference.verify_session(blocks, 0, carrier, decoded, alignment, gain, 880, stress.spectrum)
    assert result['verified'] and result['session_id'] == decoder.session_id
    assert result['produced_packets'] == 64 and result['verified_received_prefix_frames'] == 48000-1920
    assert result['audible_verified_frames'] >= 19200 and result['verified_frames'] == 62400
    assert result['maximum_residual_rms'] <= 1 and result['maximum_foreign_rms'] < 1
    evidence = decoder.evidence()
    assert evidence['storage_byte_limit'] == evidence['snapshot_byte_limit'] == 8*1024*1024
    assert evidence['peak_pcm_reference_bytes'] == 16*1024*1024
    assert result['payload_calendar_sha256'] == evidence['payload_calendar_sha256']
    decoder.clear()
    assert not decoder.parts and decoded.pcm.nbytes == 64*960*2
    assert result['produced_packets'] == 64  # Durable receipt, not cleared mutable statistics.


@pytest.mark.parametrize('mutation', ['gap', 'foreign', 'stale-own-tail', 'missing-tail', 'missing-session'])
def test_qualifying_voice_prefix_cannot_hide_any_later_or_closed_tail_failure(mutation):
    _, decoded, carrier, alignment, gain, blocks = session_vector()
    buffers = list(blocks)
    if mutation == 'missing-tail':
        buffers = buffers[:59]
    elif mutation == 'missing-session':
        alignment = reference.Alignment(str(uuid4()), alignment.start_frame, alignment.stop_frame)
    else:
        index = 45 if mutation != 'stale-own-tail' else 64
        pcm = np.frombuffer(buffers[index], dtype='<i2').reshape(-1, 2).copy()
        if mutation == 'gap':
            pcm[576:] = 0
        elif mutation == 'foreign':
            pcm[720:] += (600*np.sin(2*np.pi*880*np.arange(240)/48000+.9)).astype('<i2')[:, None]
        else:
            pcm[720:] += decoded[1920:2160, None]
        buffers[index] = pcm.tobytes()
    with pytest.raises(RuntimeFailure):
        reference.verify_session(tuple(buffers), 0, carrier, decoded, alignment, gain, 880, stress.spectrum)


def test_whole_reference_cancellation_and_capture_memory_bound_never_issue_audible_receipts():
    _, decoded, carrier, alignment, gain, blocks = session_vector()
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(RuntimeFailure, match='canceled'):
        reference.verify_session(blocks, 0, carrier, decoded, alignment, gain, 880, stress.spectrum, canceled=cancel)
    oversized = (bytes(reference.MAX_SESSION_CAPTURE_BYTES+4),)
    with pytest.raises(RuntimeFailure, match='capture exceeds8MiB'):
        reference.verify_session(oversized, 0, carrier, decoded, alignment, gain, 880, stress.spectrum)


@pytest.mark.asyncio
async def test_public_avpacket_is_not_reencoded_and_cancel_fences_exact_publisher(monkeypatch):
    tone = reference.packet_track(1320, str(uuid4()))
    try:
        packet = await tone.recv()
        assert bytes(packet) and packet.pts == 0 and packet.time_base == Fraction(1, 48000)
        payloads, pts = OpusEncoder().pack(packet)
        assert payloads == [bytes(packet)] and pts == 0 and tone.reference.packet_count == 1
        started = asyncio.Event()
        async def canceled_encode(*args):
            started.set()
            await asyncio.Future()
        monkeypatch.setattr(reference, 'asyncio', SimpleNamespace(sleep=asyncio.sleep, to_thread=canceled_encode))
        task = asyncio.create_task(tone.recv())
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert tone.error == 'CancelledError' and tone.readyState == 'ended'
        assert tone.reference.packet_count == 1 and tone.samples == 960
        with pytest.raises(RuntimeFailure, match='stopped, failed'):
            await tone.recv()
    finally:
        tone.stop()
        tone.reference.clear()


@pytest.mark.asyncio
async def test_actual_local_rtc_emitted_packet_decode_matches_independent_continuous_reference():
    from aiortc import RTCConfiguration, RTCPeerConnection, RTCRtpSender
    from av import AudioResampler
    sender, receiver = (RTCPeerConnection(RTCConfiguration(iceServers=[])) for _ in range(2))
    tone = reference.packet_track(1320, str(uuid4()))
    incoming = asyncio.Future()
    @receiver.on('track')
    def track(value):
        incoming.set_result(value)
    transceiver = sender.addTransceiver(tone, direction='sendonly')
    transceiver.setCodecPreferences([codec for codec in RTCRtpSender.getCapabilities('audio').codecs
                                    if codec.mimeType.lower() == 'audio/opus'])
    async def receive():
        await sender.setLocalDescription(await sender.createOffer())
        await receiver.setRemoteDescription(sender.localDescription)
        await receiver.setLocalDescription(await receiver.createAnswer())
        await sender.setRemoteDescription(receiver.localDescription)
        remote = await incoming
        resampler = AudioResampler(format='s16', layout='mono', rate=48000)
        parts = []
        for _ in range(12):
            frame = await remote.recv()
            assert frame.samples == 960 and frame.sample_rate == 48000
            converted = resampler.resample(frame)
            assert len(converted) == 1 and converted[0].samples == 960
            parts.append(bytes(converted[0].planes[0])[:1920])
        return b''.join(parts)
    try:
        observed = await asyncio.wait_for(receive(), 8)
        assert sender.connectionState == receiver.connectionState == 'connected'
        produced = tone.reference.snapshot().tobytes()
        assert observed == produced[:len(observed)] and len(observed) == 12*1920
        assert tone.reference.packet_count >= 12 and tone.error is None
        stats = await sender.getStats()
        outbound = [value for value in stats.values() if value.type == 'outbound-rtp' and value.kind == 'audio']
        assert len(outbound) == 1 and 12 <= outbound[0].packetsSent <= tone.reference.packet_count
    finally:
        tone.stop()
        await asyncio.wait_for(asyncio.gather(sender.close(), receiver.close()), 4)
        tone.reference.clear()
    assert sender.connectionState == receiver.connectionState == 'closed'
