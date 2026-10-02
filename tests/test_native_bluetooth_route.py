"""Portable observation/admission regressions; no bus, units or PCM devices."""
from __future__ import annotations

import asyncio
import ast
from copy import deepcopy
from collections import deque
from fractions import Fraction
import importlib.util
import hashlib
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

np = pytest.importorskip('numpy')
av = pytest.importorskip('av')
pytest.importorskip('aiortc')
ROOT = Path(__file__).resolve().parent.parent


def module(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT/'tests/linux'/file)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


route = module('bluetooth_route_test', 'native_bluetooth_route.py')
fixture = module('bluetooth_route_fixture_test', 'check_native_bluetooth_route.py')
supervisor = module('bluetooth_route_supervisor_test', 'run_native_bluetooth_route.py')


def checker_coroutine(name, environment):
    """Run the exact owned checker body inside its declared local closure."""
    tree = ast.parse((ROOT/'tests/linux/check_native_bluetooth_route.py').read_text())
    exercise = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'exercise')
    function = next(node for node in ast.walk(exercise) if isinstance(node, ast.AsyncFunctionDef) and node.name == name)
    factory = ast.FunctionDef(name='make', args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
                              body=[ast.Assign(targets=[ast.Name(id='initial_flushes', ctx=ast.Store())], value=ast.Constant(None)),
                                    function, ast.Return(value=ast.Name(id=name, ctx=ast.Load()))], decorator_list=[])
    wrapped = ast.fix_missing_locations(ast.Module(body=[factory], type_ignores=[]))
    exec(compile(wrapped, str(ROOT/'tests/linux/check_native_bluetooth_route.py'), 'exec'), environment)
    return environment['make']()


async def actual_granted_source():
    from shiri.runtime.native import NativeController, NativeHandle
    from test_native_audio import begin, controller
    original, writer, client = controller()
    native = NativeController(fixture.A, original.mixer, client)
    await native.initialize()
    handle = NativeHandle(native)
    grant = await native.begin(begin(), handle)
    expected = {'session_id': grant.session_id, 'incarnation': str(UUID(bytes=grant.incarnation)),
                'epoch': grant.epoch, 'generation': grant.generation, 'stage': 'granted', 'frames': 0}
    return native, writer, handle, grant, expected


def checker_health_environment(native, expected):
    from shiri.domain import Room, SpeakerRef
    timing = fixture.group.load_minimum_coverage_module()
    definitions = [Room(id=identifier, slot=6+n, name=f'Validation {n}', airplay_name=f'Validation {n}',
        interface='eth0', enabled=True, local_audio_device=f'hw:CARD=Loopback,DEV={1-n},SUBDEV=7',
        speakers=[SpeakerRef(id='0', name='Local', protocol='alsa')]) for n, identifier in enumerate((fixture.A, fixture.B))]
    frozen = timing.freeze(definitions, {identifier: {'ready': True, 'error': None,
        'source': {'ready': True, 'owner': None}, 'native_blocks': 0,
        'timing_relay_delay_ms': 140, 'output_buffer_ms': 40} for identifier in (fixture.A, fixture.B)})
    state = SimpleNamespace(processes={}, selected_ids=['0'], current_volume=100,
                            desired=definitions[0],
                            client=SimpleNamespace(outputs=AsyncMock(return_value=[{'id': '0', 'selected': True}]),
                                                   request=AsyncMock(return_value={'state': 'stop', 'volume': 100})))
    report = {}
    async def rpc(*_args, **_kwargs):
        return {**native.mixer.health(), **native.health(), 'speech_session_id': None}
    env = vars(fixture).copy() | {
        'daemon': SimpleNamespace(healthy=lambda: None), 'api_process': SimpleNamespace(alive=True),
        'a_guard': SimpleNamespace(check=lambda: None), 'b_guard': SimpleNamespace(check=lambda: None),
        'states': {fixture.A: state}, 'expected_units': {fixture.A: {}},
        'broker': SimpleNamespace(_worker_socket=lambda _state: '/not-opened',
                                 _worker_rpc=AsyncMock(return_value={'ready': True, 'error': None, 'peer_authorized': True,
                                     'active': False, 'uses_system_bus': False, 'uses_alsa_devices': False, 'flushes': 0})),
        'producers': {fixture.A: expected}, 'report': report, 'call_rpc': rpc,
        'group': SimpleNamespace(producer_status=lambda value: deepcopy(value),
                                 load_minimum_coverage_module=lambda: timing), 'phase': 'startup',
        'frozen_timing': frozen,
        'a_native_expected': None, 'players': {}, 'anchors': {}, 'controls': [],
        'volume_expected': 100, 'pending_volume': None, 'generation_guard': fixture.NativeSourceGenerationGuard(),
    }
    return env, report


async def test_actual_grant_without_pcm_is_admitted_only_until_irreversible_first_pcm_and_before_onset():
    from test_native_audio import pcm as native_pcm
    native, writer, handle, grant, expected = await actual_granted_source()
    env, report = checker_health_environment(native, expected)
    healthy = checker_coroutine('healthy', env)
    try:
        assert native.actor.snapshot()['owner']['session_id'] == grant.session_id
        assert native.mixer.health()['native_generation'] is None and native.mixer.health()['native_blocks'] == 0
        await healthy()
        observation = report['native_source_last_observed'][fixture.A]
        assert observation['accepted'] and observation['scope'] == 'exact_granted_source_before_first_pcm_only'
        with pytest.raises(route.RuntimeFailure, match='required accepted-PCM generation'):
            await healthy(require_generation=True)
        assert not report['native_source_last_observed'][fixture.A]['accepted']
        first_rejected = deepcopy(report['native_source_first_rejected'][fixture.A])
        with pytest.raises(route.RuntimeFailure, match='required accepted-PCM generation'):
            await healthy()  # requesting onset proof seals the allowance even before a late first packet
        await native.message(native_pcm(grant), handle)
        await healthy(require_generation=True)
        assert len(writer.packets) == 1 and native.mixer.health()['native_generation'] == expected['generation']
        assert report['native_source_last_observed'][fixture.A]['scope'] == 'exact_accepted_pcm_generation'
        assert report['native_source_first_rejected'][fixture.A] == first_rejected
        native.mixer._last_packet = None  # exact prior accepted PCM evidence cannot revert to granted-only
        with pytest.raises(route.RuntimeFailure, match='required accepted-PCM generation'):
            await healthy()
        assert report['native_source_last_observed'][fixture.A]['health']['native_blocks'] == 1
    finally:
        await native.close()
        native.mixer.close()


@pytest.mark.parametrize('field,value', [('zone_id', 'wrong'), ('session_id', 'wrong'), ('epoch', 2), ('incarnation', 'wrong'), ('protocol', 'cast')])
async def test_pre_pcm_startup_never_waives_actual_source_owner_and_keeps_failure_snapshot(field, value):
    native, _writer, _handle, _grant, expected = await actual_granted_source()
    health = {**native.mixer.health(), **native.health()}
    health['source']['owner'][field] = value
    report = {}
    try:
        with pytest.raises(route.RuntimeFailure, match='source incarnation'):
            fixture.NativeSourceGenerationGuard().check(fixture.A, health, expected, 'startup', report)
        observed = report['native_source_last_observed'][fixture.A]
        assert not observed['accepted'] and observed['source_owner'][field] == value
        assert observed['expected']['session_id'] == expected['session_id']
    finally:
        await native.close()
        native.mixer.close()


@pytest.mark.parametrize('field,value,phase', [
    ('native_generation', 2, 'startup'), ('native_generation', True, 'startup'),
    ('native_blocks', 1, 'startup'), ('native_blocks', True, 'startup'),
    ('native_blocks', 2**100, 'startup'),
    ('native_generation', None, 'speech_active'), ('source_ready', False, 'startup'),
    ('stage', 'streaming', 'startup'), ('frames', 960, 'startup'),
    ('expected_generation', 2, 'startup'),
])
async def test_startup_exception_refuses_wrong_generation_or_any_poststart_evidence(field, value, phase):
    native, _writer, _handle, _grant, expected = await actual_granted_source()
    health = {**native.mixer.health(), **native.health()}
    if field == 'expected_generation':
        expected['generation'] = value
    elif field in {'stage', 'frames'}:
        expected[field] = value
    elif field == 'source_ready':
        health['source']['ready'] = value
    else:
        health[field] = value
    report = {}
    try:
        with pytest.raises(route.RuntimeFailure):
            fixture.NativeSourceGenerationGuard().check(fixture.A, health, expected, phase, report)
        assert report['native_source_last_observed'][fixture.A]['accepted'] is False
    finally:
        await native.close()
        native.mixer.close()


async def test_actual_onset_cannot_qualify_decoded_pcm_before_full_generation_recheck():
    native, _writer, _handle, _grant, expected = await actual_granted_source()
    env, report = checker_health_environment(native, expected)
    healthy = checker_coroutine('healthy', env)
    capture = decoded(count=64)
    b_guard = SimpleNamespace(index=0, check=lambda: None)
    env.update(healthy=healthy, daemon=SimpleNamespace(capture=capture, healthy=lambda: None),
               capture=SimpleNamespace(poll=lambda: None), b_guard=b_guard)
    env['group'].music_onset_index = lambda _capture: 0
    onset = checker_coroutine('onset', env)
    try:
        with pytest.raises(route.RuntimeFailure, match='required accepted-PCM generation'):
            await onset()
        observed = report['native_source_last_observed'][fixture.A]
        assert observed['generation_required'] is True and observed['accepted'] is False
    finally:
        await native.close()
        native.mixer.close()


async def test_generation_failure_evidence_is_bounded_and_excludes_unrelated_payloads():
    native, _writer, _handle, _grant, expected = await actual_granted_source()
    expected['commands'] = ['private-command-data']*1000
    health = {**native.mixer.health(), **native.health(), 'unrelated_payload': 'not-retained'*10000}
    health['source']['unexpected_private_payload'] = 'not-retained'*10000
    health['native_blocks'] = 2**100
    report = {}
    try:
        with pytest.raises(route.RuntimeFailure, match='malformed'):
            fixture.NativeSourceGenerationGuard().check(fixture.A, health, expected, 'startup', report)
        serialized = fixture.json.dumps(report)
        assert len(serialized) < 4096 and 'private-command-data' not in serialized and 'not-retained' not in serialized
        assert report['native_source_first_rejected'][fixture.A]['health']['native_blocks'] == '<integer outside signed64range>'
    finally:
        await native.close()
        native.mixer.close()


def pcm(frames=960, first=0, gain=1., voice=0., frequency=440, previous=0., dc=0.):
    positions = (first+np.arange(frames))/48000
    values = (8192*gain*np.sin(2*np.pi*frequency*positions)+voice*np.sin(2*np.pi*880*positions)
              +previous*np.sin(2*np.pi*440*positions)+dc).astype('<i2')
    return np.repeat(values[:, None], 2, axis=1).astype('<i2').tobytes()


def encode_blocks(count=100, gain=1., voice=0., frequency=440, previous=0.):
    """Real independent libav SBC encoder drives the real production decoder."""
    encoder = av.CodecContext.create('sbc', 'w')
    encoder.sample_rate, encoder.layout, encoder.format = 48000, 'stereo', 's16'
    encoder.bit_rate, encoder.time_base = 328000, Fraction(1, 48000)
    encoder.open()
    assert encoder.frame_size == 128
    result = []
    for first in range(0, count*128, 128):
        frame = av.AudioFrame(format='s16', layout='stereo', samples=128)
        frame.planes[0].update(pcm(128, first, gain, voice, frequency, previous))
        frame.sample_rate, frame.pts, frame.time_base = 48000, first, Fraction(1, 48000)
        result.extend(bytes(packet) for packet in encoder.encode(frame))
    result.extend(bytes(packet) for packet in encoder.encode(None))
    assert len(result) == count
    return result


def packets(encoded, *, sequence=1, timestamp=10, ssrc=0, batch=8):
    result = []
    for index in range(0, len(encoded), batch):
        pieces = encoded[index:index+batch]
        result.append(struct.pack('!BBHII', 0x80, 96, sequence & 0xffff, timestamp & 0xffffffff, ssrc)
                      +bytes([len(pieces)])+b''.join(pieces))
        sequence += 1
        timestamp += 128*len(pieces)
    return result


def decoded(gain=1., voice=0., count=200, frequency=440, previous=0.):
    capture = route.RtpCapture()
    capture.fixture_encoder = av.CodecContext.create('sbc', 'w')
    capture.fixture_encoder.sample_rate, capture.fixture_encoder.layout, capture.fixture_encoder.format = 48000, 'stereo', 's16'
    capture.fixture_encoder.bit_rate, capture.fixture_encoder.time_base = 328000, Fraction(1, 48000)
    capture.fixture_encoder.open()
    capture.fixture_frame = 0
    append_actual_sbc(capture, gain=gain, voice=voice, frequency=frequency, previous=previous, count=count)
    return capture


def test_real_sbc_decode_and_exact_sample_counter_wraps():
    capture = route.RtpCapture()
    encoded = encode_blocks(24)
    data = packets(encoded, sequence=65535, timestamp=2**32-1024)
    for index, packet in enumerate(data):
        capture.feed(packet, 100+index*.021)
    assert capture.total_frames == 24*128
    assert [item['sequence'] for item in capture.records] == [65535, 0, 1]
    assert [item['timestamp'] for item in capture.records] == [2**32-1024, 0, 1024]
    measured = route.fit_tones(b''.join(capture.chunks)[512*4:])
    assert .95 < measured['amplitude_440']/8192 < 1.05
    assert measured['amplitude_880'] < measured['amplitude_440']*.01
    assert capture.evidence()['timing_scope'].startswith('RTP sample-count')


@pytest.mark.parametrize('mutation, match', [
    (lambda data: b'\x81'+data[1:], 'extension'),
    (lambda data: data[:1]+b'\x61'+data[2:], 'payload'),
    (lambda data: data[:12]+b'\x88'+data[13:], 'fragment'),
    (lambda data: data[:12]+b'\x00'+data[13:], 'frame envelope'),
    (lambda data: data[:-1], 'truncated'),
    (lambda data: data+b'x', 'extra'),
    (lambda data: data[:13]+b'\x00'+data[14:], 'wrong-sync'),
    (lambda data: data[:14]+bytes([data[14] & 0x3f])+data[15:], 'profile'),
])
def test_malformed_actual_frame_never_becomes_a_decoded_receipt(mutation, match):
    packet = packets(encode_blocks(8))[0]
    capture = route.RtpCapture()
    with pytest.raises(route.RuntimeFailure, match=match):
        capture.feed(mutation(packet), 100.)
    assert capture.records == [] and capture.total_pcm == 0 and capture.error
    with pytest.raises(route.RuntimeFailure):
        capture.feed(packet, 101.)


@pytest.mark.parametrize('position', [16, 17, 20, 23])
def test_real_sbc_checksum_rejects_corrupted_header_or_scale_factor(position):
    packet = bytearray(packets(encode_blocks(8))[0])
    packet[position] ^= 1
    with pytest.raises(route.RuntimeFailure, match='CRC'):
        route.RtpCapture().feed(bytes(packet), 100.)


@pytest.mark.parametrize('position, value, match', [
    (2, 1, 'lost, repeated'), (2, 3, 'lost, repeated'),
    (4, 10, 'timestamp'), (4, 1035, 'timestamp'), (8, 123, 'identity'),
])
def test_real_rtp_gap_repeat_timestamp_or_identity_is_sticky(position, value, match):
    data = packets(encode_blocks(16))
    capture = route.RtpCapture()
    capture.feed(data[0], 100.)
    changed = bytearray(data[1])
    struct.pack_into('!H' if position == 2 else '!I', changed, position, value)
    with pytest.raises(route.RuntimeFailure, match=match):
        capture.feed(bytes(changed), 100.02)
    assert len(capture.records) == 1
    with pytest.raises(route.RuntimeFailure):
        capture.feed(data[1], 100.04)


@pytest.mark.parametrize('maximum, value', [('MAX_PACKETS', 0), ('MAX_RTP_BYTES', 10), ('MAX_PCM_BYTES', 10)])
def test_capture_bound_is_enforced_before_retention(monkeypatch, maximum, value):
    packet = packets(encode_blocks(8))[0]
    monkeypatch.setattr(route, maximum, value)
    capture = route.RtpCapture()
    with pytest.raises(route.RuntimeFailure, match='retention bound'):
        capture.feed(packet, 100.)
    assert not capture.records and not capture.raw and capture.total_pcm == 0


def test_gap_is_allowed_only_inside_predeclared_source_barrier(monkeypatch):
    data = packets(encode_blocks(32))
    capture = route.RtpCapture()
    now = time.monotonic()
    capture.feed(data[0], now)
    monkeypatch.setattr(route.time, 'monotonic', lambda: now+.01)
    monkeypatch.setattr(route.time, 'monotonic_ns', lambda: round((now+.01)*1e9))
    capture.begin_barrier('takeover')
    capture.feed(data[1], now+4)
    capture.check()
    monkeypatch.setattr(route.time, 'monotonic', lambda: now+4.01)
    monkeypatch.setattr(route.time, 'monotonic_ns', lambda: round((now+4.01)*1e9))
    capture.complete_barrier()
    capture.feed(data[2], now+4.021)
    assert capture.max_gap == 4 and capture.max_continuous_gap < .3
    capture.feed(data[3], now+4.4)
    with pytest.raises(route.RuntimeFailure, match='outside explicit source barriers'):
        capture.check()


def test_a_late_gap_cannot_be_reclassified_as_a_source_barrier(monkeypatch):
    capture = decoded(count=8)
    monkeypatch.setattr(route.time, 'monotonic', lambda: capture.last_at+.31)
    with pytest.raises(route.RuntimeFailure, match='stopped delivering'):
        capture.begin_barrier('end')
    assert capture.barriers == []


@pytest.mark.parametrize('gain, voice, expected', [(1., 0., 1.), (.125, 0., .125), (.2, 600., .2)])
def test_lossy_real_av_decode_passes_only_the_declared_music_plateau(gain, voice, expected):
    baseline = decoded(count=300)
    measured = route.fit_tones(b''.join(baseline.chunks[2:]))
    observed = decoded(gain, voice, 400)
    gate = route.Plateau(measured['amplitude_440'], expected, voice=voice > 0,
                         voice_floor=measured['amplitude_880'])
    passed = False
    for data, at in zip(observed.chunks[2:], observed.at[2:], strict=True):
        passed = gate.push(data, at)
    assert passed and gate.evidence()['consecutive_frames'] > 48000*.4


@pytest.mark.parametrize('gain, voice', [(1., 600.), (.35, 600.), (.2, 0.), (.125, 600.)])
def test_wrong_duck_or_missing_voice_cannot_pass_lossy_plateau(gain, voice):
    gate = route.Plateau(8192., .2, voice=True, voice_floor=1.)
    observed = decoded(gain, voice, 300)
    with pytest.raises(route.RuntimeFailure, match='settling law'):
        assert not any(gate.push(data, at) for data, at in zip(observed.chunks[2:], observed.at[2:], strict=True))


def test_plateau_does_not_hide_a_missing_block_in_median():
    gate = route.Plateau(8192., .2, voice=True, voice_floor=1.)
    for index in range(18):
        assert not gate.push(pcm(gain=.2, voice=600, first=index*960), index*.02+100)
    with pytest.raises(route.RuntimeFailure, match='changed again'):
        gate.push(bytes(960*4), 100.36)
    assert gate.failure and len(gate.observations) == 19
    with pytest.raises(route.RuntimeFailure, match='changed again'):
        gate.push(pcm(gain=.2, voice=600), 100.38)


def append_actual_sbc(capture, *, gain=1., voice=0., frequency=440, previous=0., count=200, dc=0.):
    """Continue the exact RTP sample clock through a changed real SBC payload."""
    timestamp = (capture.timestamp+capture.previous_frames) & 0xffffffff if capture.timestamp is not None else 10
    sequence = capture.sequence+1 if capture.sequence is not None else 1
    ssrc = capture.ssrc if capture.ssrc is not None else 0
    encoded = []
    for _index in range(count):
        first = capture.fixture_frame
        frame = av.AudioFrame(format='s16', layout='stereo', samples=128)
        frame.planes[0].update(pcm(128, first, gain, voice, frequency, previous, dc))
        frame.sample_rate, frame.pts, frame.time_base = 48000, first, Fraction(1, 48000)
        encoded.extend(bytes(packet) for packet in capture.fixture_encoder.encode(frame))
        capture.fixture_frame += 128
    assert len(encoded) == count
    for packet in packets(encoded, sequence=sequence, timestamp=timestamp, ssrc=ssrc):
        at = capture.last_at+capture.previous_frames/48000 if capture.last_at is not None else time.monotonic()
        capture.feed(packet, at)


@pytest.mark.parametrize('amplitude', [4096, 8192, 16383])
def test_real_sbc_pure_baseline_accepts_reviewed_levels_and_all_fragment_sizes(amplitude):
    capture = decoded(gain=amplitude/8192, count=400)
    raw = b''.join(capture.chunks[2:])  # Established stream, after measured codec priming.
    fragments = [raw[first:first+128*4] for first in range(0, len(raw), 128*4)]
    baseline = route.pure_baseline(fragments)
    assert .95 < baseline['amplitude_440']/amplitude < 1.05
    assert max(baseline[f'amplitude_{frequency}'] for frequency in (660, 880, 1320)) < 8
    guard = route.DecodedGuard(capture)
    guard.qualify(440, baseline['amplitude_440'], 1., False, 2, 'pure-baseline')
    guard.check(live=False)
    assert guard.phases[-1]['checked_blocks'] == len(capture.chunks)-2



@pytest.mark.parametrize('amplitude', [4096, 8192, 16383])
def test_real_sbc_successor_preserves_the_original_known_music_level(amplitude):
    capture = decoded(gain=amplitude/8192, count=400)
    baseline = route.pure_baseline(capture.chunks[2:])
    guard = route.DecodedGuard(capture)
    guard.check(live=False)
    first = len(capture.chunks)
    append_actual_sbc(capture, gain=amplitude/8192, frequency=660, count=400)
    guard.qualify(660, baseline['amplitude_440'], 1., False, first+2, 'successor')
    guard.check(live=False)
    assert guard.steady_phase['checked_blocks'] == len(capture.chunks)-(first+2)
    append_actual_sbc(capture, gain=amplitude*.7/8192, frequency=660)
    with pytest.raises(route.RuntimeFailure, match='exact music gain'):
        guard.check(live=False)

def test_actual_sbc_880_contamination_cannot_become_its_own_noise_floor():
    contaminated = decoded(voice=80., count=400)
    with pytest.raises(route.RuntimeFailure, match='undeclared source or voice'):
        route.pure_baseline(contaminated.chunks[2:])
    measured = route.fit_tones(b''.join(contaminated.chunks[2:]))
    assert 75 < measured['amplitude_880'] < 85
    with pytest.raises(route.RuntimeFailure, match='verified pure baseline'):
        route.Plateau(measured['amplitude_440'], 1., voice=False,
                      voice_floor=measured['amplitude_880'], previous_voice=1800)


def test_postqualification_real_sbc_old440_cannot_leak_under_successor660():
    capture = decoded(frequency=660, count=400)
    baseline = route.fit_tones(b''.join(capture.chunks[2:]))['amplitude_660']
    guard = route.DecodedGuard(capture)
    guard.qualify(660, baseline, 1., False, 2, 'successor')
    guard.check(live=False)
    first_bad = len(capture.chunks)
    append_actual_sbc(capture, frequency=660, previous=4096)
    with pytest.raises(route.RuntimeFailure, match='forbidden source or voice'):
        guard.check(live=False)
    assert guard.error and guard.failed_block == first_bad
    with pytest.raises(route.RuntimeFailure, match='forbidden source or voice'):
        guard.check(live=False)


@pytest.mark.parametrize('label', ['silent_restore', 'closed_restore'])
def test_real_sbc_voice_after_qualified_restoration_is_sticky_through_the_final_tail(label):
    capture = decoded(count=400)
    baseline = route.pure_baseline(capture.chunks[2:])
    guard = route.DecodedGuard(capture)
    guard.qualify(440, baseline['amplitude_440'], 1., False, 2, label)
    guard.check(live=False)
    first_bad = len(capture.chunks)
    append_actual_sbc(capture, voice=2000.)
    with pytest.raises(route.RuntimeFailure, match='forbidden source or voice'):
        guard.check(live=False)
    assert guard.failed_block == first_bad
    assert guard.phases[-1]['checked_blocks'] == first_bad-2


def test_real_sbc_wrong_gain_after_volume50_plateau_never_becomes_a_good_tail():
    capture = decoded(gain=.125, count=400)
    guard = route.DecodedGuard(capture)
    guard.qualify(440, 8192., .125, False, 2, 'volume50')
    guard.check(live=False)
    append_actual_sbc(capture, gain=.35)
    with pytest.raises(route.RuntimeFailure, match='exact music gain'):
        guard.check(live=False)


def test_qualification_replays_bad_already_observed_tail_instead_of_starting_at_latest_index():
    capture = decoded(count=400)
    guard = route.DecodedGuard(capture)
    guard.arm(440, 8192.)
    guard.check(live=False)
    append_actual_sbc(capture, voice=2000.)
    guard.check(live=False)  # Generic transition survival does not qualify purity.
    assert guard.index == len(capture.chunks)
    with pytest.raises(route.RuntimeFailure, match='forbidden source or voice'):
        guard.qualify(440, 8192., 1., False, 2, 'restore')
    assert guard.failed_block < guard.index


def test_transition_boundary_precedes_mutation_and_retains_every_nonqualifying_block():
    capture = decoded(count=400)
    guard = route.DecodedGuard(capture)
    guard.qualify(440, 8192., 1., False, 2, 'baseline')
    boundary = guard.begin_transition()
    first = len(capture.chunks)
    assert boundary['first_block'] == first and boundary['previous_ratio'] == 1.
    append_actual_sbc(capture, gain=.35, voice=600.)
    gate = route.Plateau(8192., .2, voice=True, voice_floor=1., first_block=boundary['first_block'],
                         previous_ratio=boundary['previous_ratio'], previous_voice_active=False)
    with pytest.raises(route.RuntimeFailure, match='settling law'):
        for index in range(first, len(capture.chunks)):
            gate.push(capture.chunks[index], capture.at[index])
    assert gate.observations[0]['block'] == first
    assert all(not item['matched'] for item in gate.observations)
    assert gate.changed_frame == 0 and gate.failure


def test_restoration_can_use_the_bounded_lease_and250ms_law_but_not_an_unbounded_prefix():
    gate = route.Plateau(8192., 1., voice=False, voice_floor=1., previous_ratio=.2,
                         previous_voice_active=True)
    # Silent packets can precede gain restoration while the250ms audible
    # lease expires. The valid restore then fits within the250ms gain law.
    for index in range(15):
        assert not gate.push(pcm(gain=.2, first=index*960), 100+index*.02)
    for index in range(20):
        gain = min(1., .2+(index+1)*.08)
        gate.push(pcm(gain=gain, first=(15+index)*960), 100+(15+index)*.02)
    assert gate.settled and not gate.failure
    delayed = route.Plateau(8192., 1., voice=False, voice_floor=1., previous_ratio=.2,
                            previous_voice_active=True)
    with pytest.raises(route.RuntimeFailure, match='settling law'):
        for index in range(40):
            delayed.push(pcm(gain=.2, first=index*960), 100+index*.02)
    assert delayed.failure and delayed.observations



@pytest.mark.parametrize('amplitude', [4096, 8192, 16383])
@pytest.mark.parametrize('returned', [440, 660, 880, 1320, 'dc'])
def test_real_sbc_retirement_accepts_actual_silence_then_rejects_small_returning_content(amplitude, returned):
    capture = decoded(gain=amplitude/8192, count=400)
    baseline = route.pure_baseline(capture.chunks[2:])
    guard = route.DecodedGuard(capture)
    guard.check(live=False)
    first_silence = len(capture.chunks)
    append_actual_sbc(capture, gain=0., count=400)
    # Retain and inspect the codec's actual transition prefix. Qualification
    # begins only after its first two complete received packets have settled.
    prefix = [route.fit_tones(data) for data in capture.chunks[first_silence:first_silence+2]]
    assert prefix and all(item['peak'] < 32760 for item in prefix)
    guard.retire(baseline['amplitude_440']*.01, first_block=first_silence+2)
    guard.check(live=False)
    first_return = len(capture.chunks)
    if returned == 'dc':
        append_actual_sbc(capture, gain=0., dc=20.)
    else:
        append_actual_sbc(capture, gain=20/8192, frequency=returned)
    measured = route.fit_tones(b''.join(capture.chunks[first_return+2:]))
    assert measured['rms'] < baseline['amplitude_440']*.01  # Preimage would accept.
    assert (measured['dc'] if returned == 'dc' else measured[f'amplitude_{returned}']) > 8
    with pytest.raises(route.RuntimeFailure, match='carrier or DC after exact source retirement'):
        guard.check(live=False)
    assert guard.failed_block >= first_return and guard.error


def test_retirement_replays_each_qualified_silence_block_and_queued_returning_tail():
    capture = decoded(gain=0., count=400)
    guard = route.DecodedGuard(capture)
    guard.check(live=False)
    append_actual_sbc(capture, gain=20/8192, frequency=660)
    with pytest.raises(route.RuntimeFailure, match='carrier or DC after exact source retirement'):
        guard.retire(81.92, first_block=2)
    assert guard.error and guard.failed_block >= 50

def test_codec_guard_rejects_complete_zero_tail_and_clipping_after_onset():
    capture = decoded(count=32)
    guard = route.DecodedGuard(capture)
    guard.check(live=False)
    guard.arm(440, 8192.)
    capture.chunks.append(bytes(960*4))
    with pytest.raises(route.RuntimeFailure, match='lost its native music carrier'):
        guard.check(live=False)
    assert guard.failed_block == 4
    with pytest.raises(route.RuntimeFailure):
        guard.check(live=False)
    clipped = decoded(count=8)
    clipped.chunks[0] = np.full((960, 2), 32767, dtype='<i2').tobytes()
    with pytest.raises(route.RuntimeFailure, match='clipped'):
        route.DecodedGuard(clipped).check(live=False)


def test_mute_right_channel_cannot_pass_codec_music_or_voice_presence():
    capture = decoded(count=8)
    guard = route.DecodedGuard(capture)
    guard.check(live=False)
    guard.arm(440, 8192.)
    data = np.frombuffer(pcm(gain=.2, voice=600.), dtype='<i2').reshape(-1, 2).copy()
    data[:, 1] = 0
    capture.chunks.append(data.astype('<i2').tobytes())
    with pytest.raises(route.RuntimeFailure, match='lost its native music carrier'):
        guard.check(live=False)
    gate = route.Plateau(8192., .2, voice=True, voice_floor=1.)
    assert not gate.push(data.astype('<i2').tobytes(), 100.)


def test_late_a_pcm_after_acknowledged_end_remains_a_sticky_failure():
    capture = decoded(count=16)
    guard = route.DecodedGuard(capture)
    guard.check(live=False)
    guard.retire(81.92)
    capture.chunks.append(pcm(frequency=660))
    with pytest.raises(route.RuntimeFailure, match='after exact source retirement'):
        guard.check(live=False)
    assert guard.error and guard.failed_block == 2


async def test_transport_drain_uses_actual_socket_and_true_decoder_without_a_pcm_relay():
    capture = route.RtpCapture()
    mock = route.RouteBlueZ(SimpleNamespace(), capture)
    try:
        sender, mock.rtp = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    except OSError:
        pytest.skip('This kernel does not provide Unix SEQPACKET socketpairs')
    mock.rtp.setblocking(False)
    task = asyncio.create_task(mock.drain())
    packet = packets(encode_blocks(8))[0]
    try:
        sender.send(packet)
        for _ in range(100):
            if capture.records:
                break
            await asyncio.sleep(.001)
        assert capture.records[0]['frames'] == 1024 and capture.total_pcm == 4096
        assert not capture.error
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        sender.close()
        mock.rtp.close()
    assert task.done()


async def test_private_cleanup_attempts_every_owned_child_and_preserves_lease_boundary(monkeypatch, tmp_path):
    daemon = route.PrivateDaemon(tmp_path/'private', tmp_path/'binary', 'a'*64)
    daemon.manager = SimpleNamespace(leases={'MAC': object()})
    stop = AsyncMock()
    monkeypatch.setattr(route.private, 'stop', stop)
    with pytest.raises(route.RuntimeFailure, match='release their leases'):
        await daemon.close(leases_released=False)
    assert stop.await_count == 0
    daemon.manager.leases.clear()
    daemon.daemon = SimpleNamespace(pid=12345, returncode=0)
    daemon.bus_process = object()
    daemon.mock = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError('failure')))
    await_error = pytest.raises(route.RuntimeFailure, match='cleanup failed')
    with await_error:
        await daemon.close(leases_released=True)
    assert stop.await_count == 2
    assert daemon.receipt['cleanup']['actual_daemon_stopped'] is True
    assert daemon.receipt['cleanup']['mock_transport_closed'] is False
    assert daemon.receipt['cleanup']['private_bus_stopped'] is True


async def test_crc_fault_during_encoder_stop_cannot_hide_behind_a_successful_cleanup(monkeypatch, tmp_path):
    capture = route.RtpCapture()
    packets_ = packets(encode_blocks(16))
    capture.feed(packets_[0], time.monotonic())
    before = capture.evidence()
    damaged = bytearray(packets_[1])
    damaged[16] ^= 1
    daemon = route.PrivateDaemon(tmp_path/'unused', tmp_path/'binary', 'a'*64)
    daemon.manager = SimpleNamespace(leases={})
    daemon.daemon = SimpleNamespace(pid=12345, returncode=None)
    daemon.bus_process = SimpleNamespace(pid=12346, returncode=None)
    daemon.mock = route.RouteBlueZ(SimpleNamespace(), capture)
    async def late_packet():
        await asyncio.sleep(.01)
        capture.feed(bytes(damaged), time.monotonic())
    daemon.mock.drain_task = asyncio.create_task(late_packet())
    async def stop(child):
        await asyncio.sleep(.03)
        child.returncode = -15
    monkeypatch.setattr(route.private, 'stop', stop)
    with pytest.raises(route.RuntimeFailure, match='cleanup failed'):
        await daemon.close(leases_released=True)
    assert before['error'] is None and 'CRC' in capture.error
    assert daemon.mock.drain_task.done() and not daemon.mock.drain_task.cancelled()
    assert daemon.receipt['cleanup'] == {
        'actual_daemon_stopped': True, 'mock_transport_closed': False, 'private_bus_stopped': True}
    assert daemon.receipt['transport_stop_boundary'].get('retired_monotonic_ns') is None
    assert daemon.receipt['encoder_stop_boundary']['returncode'] == -15


async def test_valid_late_audio_is_decoded_before_eof_and_checked_against_retirement(monkeypatch, tmp_path):
    capture = route.RtpCapture()
    packets_ = packets(encode_blocks(16))
    capture.feed(packets_[0], time.monotonic())
    guard = route.DecodedGuard(capture)
    guard.check(live=False)
    guard.retire(81.92)
    peer_closed = asyncio.Event()
    daemon = route.PrivateDaemon(tmp_path/'unused', tmp_path/'binary', 'a'*64)
    daemon.manager = SimpleNamespace(leases={})
    daemon.daemon = SimpleNamespace(pid=12345, returncode=None)
    daemon.bus_process = SimpleNamespace(pid=12346, returncode=None)
    daemon.mock = route.RouteBlueZ(SimpleNamespace(), capture)
    daemon.mock.transport_fd = SimpleNamespace(close=peer_closed.set)
    async def late_packet_then_eof():
        await asyncio.sleep(.01)
        capture.feed(packets_[1], time.monotonic())
        await peer_closed.wait()
        daemon.mock.drain_eof = {'observed_monotonic_ns': time.monotonic_ns(),
                                 'packets': len(capture.records), 'decoded_frames': capture.total_frames}
    daemon.mock.drain_task = asyncio.create_task(late_packet_then_eof())
    async def stop(child):
        await asyncio.sleep(.03)
        child.returncode = -15
    monkeypatch.setattr(route.private, 'stop', stop)
    await daemon.close(leases_released=True)
    assert len(capture.records) == 2 and daemon.mock.drain_task.done()
    boundary = daemon.receipt['transport_stop_boundary']
    assert boundary['producer_descriptor_closed_monotonic_ns'] <= boundary['eof']['observed_monotonic_ns']
    assert boundary['eof']['packets'] == 2 and boundary['eof']['decoded_frames'] == 2048
    with pytest.raises(route.RuntimeFailure, match='after exact source retirement'):
        guard.check(live=False)
    assert guard.failed_block == 1 and guard.error


@pytest.mark.parametrize('tail', ['silent', 'retired-audio', 'crc'])
async def test_actual_final_flow_retains_and_validates_the_whole_shutdown_tail(monkeypatch, tmp_path, tail):
    capture = route.RtpCapture()
    first = packets(encode_blocks(8, gain=0))[0]
    successor = bytearray(packets(encode_blocks(8, gain=0 if tail == 'silent' else 1),
                                 sequence=2, timestamp=1034)[0])
    if tail == 'crc':
        successor[16] ^= 1
    capture.feed(first, time.monotonic())
    guard = route.DecodedGuard(capture)
    guard.check(live=False)
    guard.retire(81.92)
    peer_closed = asyncio.Event()
    daemon = route.PrivateDaemon(tmp_path/'unused', tmp_path/'binary', 'a'*64)
    daemon.capture, daemon.manager = capture, SimpleNamespace(leases={})
    daemon.daemon = SimpleNamespace(pid=12345, returncode=None)
    daemon.bus_process = SimpleNamespace(pid=12346, returncode=None)
    daemon.mock = route.RouteBlueZ(SimpleNamespace(), capture)
    daemon.mock.transport_fd = SimpleNamespace(close=peer_closed.set)
    async def late_packet_then_eof():
        await asyncio.sleep(.01)
        capture.feed(bytes(successor), time.monotonic())
        await peer_closed.wait()
        daemon.mock.drain_eof = {'observed_monotonic_ns': time.monotonic_ns(),
                                 'packets': len(capture.records), 'decoded_frames': capture.total_frames}
    daemon.mock.drain_task = asyncio.create_task(late_packet_then_eof())
    async def stop(child):
        await asyncio.sleep(.03)
        child.returncode = -15
    monkeypatch.setattr(route.private, 'stop', stop)
    report = {'cleanup': {}, 'artifacts': {}, 'private_daemon': daemon.receipt,
              'per_buffer_evidence': {'bluetooth': guard.evidence()}, 'sbc_capture': capture.evidence()}
    errors = []
    await fixture.finish_private_bluetooth(daemon, guard, tmp_path, report, errors)
    assert daemon.mock.drain_task.done() and daemon.receipt['cleanup']['actual_daemon_stopped']
    retained = report['artifacts']['a_sbc_transport']
    raw = await asyncio.to_thread(Path(retained['rtp']['path']).read_bytes)
    pcm_bytes = await asyncio.to_thread(Path(retained['pcm']['path']).read_bytes)
    assert raw == bytes(capture.raw) and pcm_bytes == b''.join(capture.chunks)
    assert retained['rtp']['sha256'] == hashlib.sha256(raw).hexdigest()
    assert retained['pcm']['sha256'] == hashlib.sha256(pcm_bytes).hexdigest()
    if tail == 'silent':
        assert not errors and all(report['cleanup'].values())
        assert report['sbc_capture']['packets'] == 2 and report['sbc_capture']['decoded_frames'] == 2048
        assert report['per_buffer_evidence']['bluetooth']['checked_blocks'] == 2
        assert not report['per_buffer_evidence']['bluetooth']['error']
    elif tail == 'retired-audio':
        assert errors and report['cleanup']['private_daemon_closed'] is True
        assert report['sbc_capture']['packets'] == 2 and len(pcm_bytes) == 2048*4
        assert report['per_buffer_evidence']['bluetooth']['failed_block'] == 1
        assert 'after exact source retirement' in report['per_buffer_evidence']['bluetooth']['error']
    else:
        assert errors and report['cleanup']['private_daemon_closed'] is False
        assert 'CRC' in report['sbc_capture']['error']
        assert report['sbc_capture']['packets'] == 1


async def test_real_seqpacket_transport_consumes_all_queued_audio_before_close():
    capture = route.RtpCapture()
    mock = route.RouteBlueZ(SimpleNamespace(), capture)
    try:
        mock.transport_fd, mock.rtp = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    except OSError:
        pytest.skip('This kernel does not provide Unix SEQPACKET socketpairs')
    mock.rtp.setblocking(False)
    mock.drain_task = asyncio.create_task(mock.drain())
    data = packets(encode_blocks(16))
    try:
        for packet in data:
            mock.transport_fd.send(packet)
        await mock.close()
        assert capture.total_frames == 2048 and len(capture.records) == 2
        assert mock.drain_task.done() and not mock.drain_task.cancelled()
        assert mock.drain_stop_boundary['eof']['packets'] == 2 and not capture.error
        assert mock.rtp is None and mock.transport_fd is None
    finally:
        if mock.drain_task and not mock.drain_task.done():
            mock.drain_task.cancel()
            await asyncio.gather(mock.drain_task, return_exceptions=True)
        for peer in (mock.rtp, mock.transport_fd):
            if peer:
                peer.close()


@pytest.mark.parametrize('ending', ['cancelled', 'without-eof'])
async def test_cancelled_or_generic_done_observer_cannot_prove_transport_retirement(ending):
    mock = route.RouteBlueZ(SimpleNamespace(), route.RtpCapture())
    if ending == 'cancelled':
        mock.drain_task = asyncio.create_task(asyncio.sleep(10))
        mock.drain_task.cancel()
        await asyncio.gather(mock.drain_task, return_exceptions=True)
    else:
        mock.drain_task = asyncio.create_task(asyncio.sleep(0))
        await mock.drain_task
    with pytest.raises(route.RuntimeFailure, match='retirement|exact transport tail'):
        await mock.close()
    assert mock.drain_stop_boundary.get('retired_monotonic_ns') is None


def test_private_directory_collision_is_never_adopted_or_deleted(tmp_path):
    import os
    directory = tmp_path/'existing'
    directory.mkdir()
    sentinel = directory/'sentinel'
    sentinel.write_text('preserve')
    daemon = route.PrivateDaemon(directory, tmp_path/'binary', 'a'*64)
    with pytest.raises(FileExistsError):
        daemon.create_directory(os.getegid())
    with pytest.raises(route.RuntimeFailure, match='not created'):
        daemon.remove_directory()
    assert sentinel.read_text() == 'preserve' and daemon.directory_fd is None


def test_private_directory_replacement_retains_the_foreign_path_and_held_original(tmp_path):
    import os
    directory = tmp_path/'private'
    daemon = route.PrivateDaemon(directory, tmp_path/'binary', 'a'*64)
    daemon.create_directory(os.getegid())
    original = tmp_path/'moved-original'
    directory.rename(original)
    directory.mkdir(mode=0o710)
    sentinel = directory/'sentinel'
    sentinel.write_text('foreign')
    try:
        with pytest.raises(route.RuntimeFailure, match='exact owned parent'):
            daemon.remove_directory()
        assert sentinel.read_text() == 'foreign' and original.exists()
        assert os.fstat(daemon.directory_fd).st_ino == original.stat().st_ino
    finally:
        os.close(daemon.directory_fd)
        daemon.directory_fd = None


def test_only_fresh_pinned_private_directory_can_be_removed(tmp_path):
    import os
    directory = tmp_path/'private'
    daemon = route.PrivateDaemon(directory, tmp_path/'binary', 'a'*64)
    daemon.create_directory(os.getegid())
    (directory/'owned-file').write_text('fixture')
    descriptor = daemon.directory_fd
    daemon.remove_directory()
    assert not directory.exists() and daemon.directory_fd is None
    with pytest.raises(OSError):
        os.fstat(descriptor)


def original_capture():
    """Real frozen capture/sequence/guard definitions; no GStreamer constructor."""
    capture = fixture.faults.capture_type(object)()
    capture.error, capture.capture_dropped, capture.needs_latency = None, 0, False
    capture.total, capture.sequence = 0, fixture.group.observation.PcmSequence()
    capture.chunks, capture.captured_at = [], []
    capture.pending = deque([(1., pcm(), 48000, 2, 'S16LE',
                             {'offset': 0, 'offset_end': 960, 'pts': 0,
                              'duration': 20_000_000, 'discont': True})])
    capture.device, capture.rate, capture.channels, capture.format = 'not-opened', None, None, None
    capture.discontinuities, capture.max_packet_gap = 1, 0.
    capture.last_packet_at = time.monotonic()
    capture.expected_base, capture.clock_offset_ns = 1, 0
    capture.absolute = {1.: 10_000_000_000, 1.02: 10_020_000_000}
    capture.Gst = SimpleNamespace(State=SimpleNamespace(NULL='null'))
    capture.pipeline = SimpleNamespace(get_state=lambda timeout: SimpleNamespace(state='null'))
    guard = fixture.group.FinalPcmGuard(capture)
    guard.begin(0)
    guard.preserve_reference(fixture.group.observation.music_block(pcm(), 48000, 8))
    return capture, guard


@pytest.mark.parametrize('tail', ['healthy', 'zero', 'caps', 'offset', 'discont'])
async def test_slow_null_boundary_checks_actual_queued_content_and_sequence_without_fake_live_age(tail):
    capture, b_guard = original_capture()
    a_guard = route.DecodedGuard(decoded(count=8))
    monitor_joined = asyncio.Event()
    done = asyncio.Event()
    async def watch():
        try:
            await asyncio.Event().wait()
        finally:
            monitor_joined.set()
    monitor = asyncio.create_task(watch())
    await asyncio.sleep(0)  # Enter the owned monitor's cancellation boundary.
    old_packet_at = capture.last_packet_at
    def close():
        assert monitor_joined.is_set()
        time.sleep(.32)  # A genuinely slow authorized NULL transition.
        metadata = {'offset': 960, 'offset_end': 1920, 'pts': 20_000_000,
                    'duration': 20_000_000, 'discont': tail == 'discont'}
        if tail == 'offset':
            metadata.update(offset=0, offset_end=960)
        capture.pending.append((1.02, pcm(gain=0 if tail == 'zero' else 1.),
                                44100 if tail == 'caps' else 48000, 2, 'S16LE', metadata))
    capture.close = close
    report = {}
    if tail == 'healthy':
        await fixture.finish_observers(monitor, done, capture, b_guard, a_guard, report)
        assert b_guard.checked_blocks == 2 and b_guard.reference_blocks == 2
        with pytest.raises(route.RuntimeFailure, match='callback gap'):
            b_guard.check()  # A stopped capture cannot still prove live output.
    else:
        with pytest.raises(route.RuntimeFailure):
            await fixture.finish_observers(monitor, done, capture, b_guard, a_guard, report)
        assert capture.error or b_guard.error
        with pytest.raises(route.RuntimeFailure):
            b_guard.check(live=False)
    assert monitor.done() and monitor_joined.is_set() and done.is_set()
    boundary = report['original_b_stop_boundary']
    assert boundary['control_observer_joined_monotonic_ns'] <= boundary['requested_monotonic_ns']
    assert boundary['null_verified_monotonic_ns']-boundary['requested_monotonic_ns'] >= 320_000_000
    assert capture.last_packet_at == old_packet_at


async def test_monitor_primary_failure_still_closes_capture_and_cannot_be_replaced_by_secondary_tail():
    capture, b_guard = original_capture()
    a_guard = route.DecodedGuard(decoded(count=8))
    primary = route.RuntimeFailure('exact control failure')
    async def failed():
        raise primary
    monitor = asyncio.create_task(failed())
    await asyncio.sleep(0)
    closed = []
    def close():
        closed.append(True)
        capture.pending.append((1.02, pcm(gain=0.), 48000, 2, 'S16LE',
                                {'offset': 960, 'offset_end': 1920, 'pts': 20_000_000,
                                 'duration': 20_000_000, 'discont': False}))
    capture.close = close
    report = {}
    with pytest.raises(route.RuntimeFailure, match='exact control failure') as error:
        await fixture.finish_observers(monitor, asyncio.Event(), capture, b_guard, a_guard, report)
    assert error.value is primary and closed == [True]
    assert report['per_buffer_evidence']['untouched']['failed_block']['index'] == 1


async def test_failed_pre_stop_buffer_does_not_skip_owned_null_cleanup():
    capture, b_guard = original_capture()
    capture.pending[0] = (1., pcm(gain=0.), *capture.pending[0][2:])
    a_guard = route.DecodedGuard(decoded(count=8))
    monitor = asyncio.create_task(asyncio.sleep(10))
    closed = []
    capture.close = lambda: closed.append(True)
    with pytest.raises(route.RuntimeFailure):
        await fixture.finish_observers(monitor, asyncio.Event(), capture, b_guard, a_guard, {})
    assert closed == [True] and monitor.done()


async def test_bridge_keeps_production_no_namespace_and_other_default_units_are_isolated(monkeypatch):
    broker = object.__new__(fixture.IsolatedBluetoothBroker)
    broker.test_parent_namespace = 'shiri_group_run_12345678'
    launch = AsyncMock(return_value='unit')
    monkeypatch.setattr(fixture.Broker, '_start_process', launch)
    await broker._start_process('room:bluetooth-output', 'bluetooth-output', ['worker'], Path('/private'))
    assert launch.await_args.kwargs['namespace'] is None
    await broker._start_process('room:audio', 'audio', ['worker'], Path('/private'))
    assert launch.await_args.kwargs['namespace'] == broker.test_parent_namespace
    with pytest.raises(route.RuntimeFailure, match='no-namespace'):
        await broker._start_process('room:bluetooth-output', 'bluetooth-output', ['worker'], Path('/private'), namespace='/wrong')


async def test_exact_old_producer_cannot_forget_a_replacement_during_stop():
    old, fresh = {'unit': 'old', 'invocation': 'old'}, {'unit': 'new', 'invocation': 'new'}
    records = {'room:shairport': old}
    forgotten = []
    unit = SimpleNamespace(identity=lambda: deepcopy(old), alive=True)
    async def stopped():
        records['room:shairport'] = fresh
        unit.alive = False
    unit.stop = stopped
    network = SimpleNamespace(manifest={'processes': records}, forget_process=lambda key: forgotten.append(key))
    state = SimpleNamespace(processes={'shairport': object()})
    errors = await fixture.stop_producers(SimpleNamespace(network=network), {'room': state},
        {'room': {'key': 'room:shairport', 'unit': unit}}, {})
    assert not errors and forgotten == [] and records['room:shairport'] == fresh


def complete_inner():
    return {'passed': True, 'same_mac_lease_denied': True,
            'minimum_policy': True,
            'frozen_worker_timing': {'version': 1, 'policy': 'actual_production_minimum',
                'common_horizon_ns': 140_000_000, 'output_buffers_ms': {fixture.A: 40, fixture.B: 40},
                'saved_routes': [{'room_id': identifier, 'local_audio_device': 'hw:CARD=Loopback,DEV=0',
                    'speakers': [{'id': '0', 'protocol': 'alsa', 'offset_ms': 0}]} for identifier in sorted((fixture.A, fixture.B))],
                'frozen_monotonic_ns': 1, 'frozen_before_owner_or_pcm': True, 'scope': 'pure fixture'},
            'private_daemon': {'binary_sha256': 'a'*64, 'host_bus_used': False, 'physical_adapter_used': False,
                'encoder_stop_boundary': {'pid': 12345, 'returncode': -15,
                    'requested_monotonic_ns': 1, 'verified_monotonic_ns': 2},
                'transport_stop_boundary': {'requested_monotonic_ns': 3,
                    'producer_descriptor_closed_monotonic_ns': 4, 'retired_monotonic_ns': 6,
                    'receiver_descriptor_closed_monotonic_ns': 7,
                    'eof': {'observed_monotonic_ns': 5, 'packets': 100, 'decoded_frames': 102400}}},
            'transitions': {name: {'passed': True} for name in ('final_volume50', 'final_volume100', 'duck_and_voice',
                            'silent_restore', 'resumed_voice', 'closed_restore')},
            'speech': {'passed': True}, 'takeover': {'passed': True}, 'end': {'passed': True},
            'sbc_capture': {'error': None, 'packets': 100, 'decoded_frames': 102400},
            'per_buffer_evidence': {'bluetooth': {'error': None, 'checked_blocks': 100},
                                    'untouched': {'failed_block': None, 'untouched_reference_blocks': 100}},
            'cleanup': dict.fromkeys({f'producer_{identifier}_stopped' for identifier in (fixture.A, fixture.B)} | {
                'b_capture_null', 'disposable_rooms_deleted', 'api_stopped', 'api_client_closed', 'broker_closed',
                'bluetooth_lease_released', 'private_daemon_closed', 'private_directory_removed',
                'isolated_lan_closed', 'empty_manifest', 'slot7_closed', 'host_and_legacy_preserved'}, True),
            'cleanup_errors': []}


def test_supervisor_requires_all_route_stage_evidence_and_exact_binary():
    inner = complete_inner()
    assert supervisor.valid_inner(inner, 'a'*64, 0)
    for label in inner['transitions']:
        partial = deepcopy(inner)
        partial['transitions'][label]['passed'] = False
        assert not supervisor.valid_inner(partial, 'a'*64, 0)
    for key in ('speech', 'takeover', 'end'):
        partial = deepcopy(inner)
        partial[key]['passed'] = False
        assert not supervisor.valid_inner(partial, 'a'*64, 0)
    assert not supervisor.valid_inner(inner, 'b'*64, 0)
    assert not supervisor.valid_inner(inner, 'a'*64, 1)


@pytest.mark.parametrize('kind', ['sbc_capture', 'per_buffer_evidence', 'cleanup'])
def test_generic_pass_cannot_hide_missing_observer_or_owned_cleanup_receipts(kind):
    inner = complete_inner()
    inner[kind] = {}
    assert not supervisor.valid_inner(inner, 'a'*64, 0)


@pytest.mark.parametrize('mutation', ['encoder-live', 'missing-encoder', 'missing-eof', 'early-eof', 'missing-tail', 'frames'])
def test_supervisor_requires_exact_producer_stop_and_all_retained_transport_tail(mutation):
    inner = complete_inner()
    private = inner['private_daemon']
    if mutation == 'encoder-live':
        private['encoder_stop_boundary']['returncode'] = None
    elif mutation == 'missing-encoder':
        private.pop('encoder_stop_boundary')
    elif mutation == 'missing-eof':
        private['transport_stop_boundary'].pop('eof')
    elif mutation == 'early-eof':
        private['transport_stop_boundary']['eof']['observed_monotonic_ns'] = 1
    elif mutation == 'missing-tail':
        private['transport_stop_boundary']['eof']['packets'] = 99
    else:
        private['transport_stop_boundary']['eof']['decoded_frames'] -= 128
    assert not supervisor.valid_inner(inner, 'a'*64, 0)


async def test_supervisor_reaps_its_actual_signal_ignoring_child_with_bounded_kill():
    process = await asyncio.create_subprocess_exec(sys.executable, '-c',
        'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print("ready",flush=True); time.sleep(30)',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        assert await asyncio.wait_for(process.stdout.readline(), 2) == b'ready\n'
        await asyncio.wait_for(supervisor.stop_child(process, grace=.02, kill_timeout=1), 2)
        assert process.returncode is not None and process.returncode < 0
        await supervisor.stop_child(process, grace=.02, kill_timeout=1)  # Reaped ownership is never killed again.
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def file_stat_as_root(info):
    """Only root authority is simulated; reads/path/inode/time observations are real."""
    return SimpleNamespace(**{name: 0 if name == 'st_uid' else getattr(info, name) for name in
        ('st_dev', 'st_ino', 'st_uid', 'st_nlink', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns')})


def test_source_receipt_hashes_named_python_bytes_not_cached_bytecode(monkeypatch, tmp_path):
    source = tmp_path/'probe.py'
    source.write_bytes(b'actual_python_source = 123\n')
    (tmp_path/'probe.pyc').write_bytes(b'unrelated stale cached bytecode')
    source.chmod(0o600)
    monkeypatch.setattr(supervisor, 'SOURCE_FILES', ('probe.py',))
    monkeypatch.setattr(supervisor, 'trusted_file', lambda path: path)
    original = os.fstat
    monkeypatch.setattr(supervisor.os, 'fstat', lambda descriptor: file_stat_as_root(original(descriptor)))
    receipt = supervisor.source_receipts(tmp_path)['probe.py']
    assert receipt['sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert receipt['bytes'] == source.stat().st_size and receipt['st_ino'] == source.stat().st_ino


def test_source_path_replacement_during_held_read_cannot_receive_a_proof(monkeypatch, tmp_path):
    source, replacement = tmp_path/'probe.py', tmp_path/'replacement'
    source.write_bytes(b'trusted = 1\n')
    replacement.write_bytes(b'foreign = 2\n')
    source.chmod(0o600)
    replacement.chmod(0o600)
    monkeypatch.setattr(supervisor, 'SOURCE_FILES', ('probe.py',))
    monkeypatch.setattr(supervisor, 'trusted_file', lambda path: path)
    original_stat, original_read = os.fstat, os.read
    monkeypatch.setattr(supervisor.os, 'fstat', lambda descriptor: file_stat_as_root(original_stat(descriptor)))
    switched = []
    def read(descriptor, limit):
        data = original_read(descriptor, limit)
        if data and not switched:
            replacement.replace(source)
            switched.append(True)
        return data
    monkeypatch.setattr(supervisor.os, 'read', read)
    with pytest.raises(route.RuntimeFailure, match='changed while its proof'):
        supervisor.source_receipts(tmp_path)
    assert switched == [True] and source.read_bytes() == b'foreign = 2\n'


def test_import_does_not_launch_a_bus_socket_codec_or_device():
    code = r'''
import asyncio, importlib.util, socket, subprocess, sys
from unittest.mock import Mock
import av
import dbus_next.aio
for target, name in [(asyncio,'create_subprocess_exec'), (subprocess,'run'),
                     (socket,'socket'), (av,'CodecContext'), (dbus_next.aio,'MessageBus')]:
    setattr(target, name, Mock(side_effect=AssertionError('import resource creation')))
for name in ('native_bluetooth_route.py','check_native_bluetooth_route.py','run_native_bluetooth_route.py'):
    spec=importlib.util.spec_from_file_location('import_'+name, sys.argv[1]+'/tests/linux/'+name)
    loaded=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=loaded
    spec.loader.exec_module(loaded)
'''
    result = subprocess.run([sys.executable, '-c', code, str(ROOT)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.fixture
def actual_unit_authority(tmp_path, monkeypatch):
    """Actual generated v5 policy and real socket inode; no systemd launch."""
    from dataclasses import asdict
    import shiri.runtime.units as units
    from shiri.runtime.bluealsa import PCMEndpoint
    monkeypatch.setattr(units, 'boot_id', lambda: 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee')
    spec = units.UnitSpec(units.new_unit('b2651234', fixture.A, 'bluetooth-output'),
        'bluetooth-output', 'shiri-bridge-6', 'shiri-bridge-6',
        ('/usr/bin/python3', '-m', 'shiri.runtime.bluetooth_output'),
        binds=(units.Bind('/private/bridge-state', '/run/shiri-worker/control', True),),
        environment=('PYTHONPATH=/private/reviewed-source',))
    receipt = spec.intent()
    receipt.update(invocation_id='a'*32, control_group='/system.slice/'+spec.name, cgroup_inode=987)
    import tempfile
    import shutil
    scratch = Path(tempfile.mkdtemp(prefix='bta-', dir='/tmp'))
    directory = scratch/'bridge-published'/('b'*32)
    directory.mkdir(parents=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    # /proc FD aliases are production-only Linux; this short real socket is
    # sufficient for the schema fixture's held inode, without network/devices.
    listener.bind(str(directory/'final-pcm.sock'))
    named = (directory/'final-pcm.sock').stat()
    parent = directory.stat()
    uid, gid = (1234, 1235) if os.geteuid() == 0 else (os.geteuid(), os.getegid())
    if os.geteuid() == 0:
        os.chown(directory/'final-pcm.sock', uid, gid)
    os.chmod(directory/'final-pcm.sock', 0o660)
    receipt['socket_publication'] = {'directory':str(directory), 'directory_dev':parent.st_dev,
        'directory_inode':parent.st_ino, 'socket_dev':named.st_dev, 'socket_inode':named.st_ino,
        'uid':uid, 'gid':gid, 'initial_gid':gid}
    endpoint = PCMEndpoint(':1.25', '/org/bluealsa/hci0/dev_AA_BB_CC_DD_EE_FF/a2dpsrc/sink',
        '/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF', route.private.MAC, 1, 0x8210,
        'S16_LE', 2, 2, 48000, 'SBC', True, True)
    manager = object()
    admission = SimpleNamespace(manager=manager, endpoint=endpoint, worker_pid=2345,
        envelope=lambda: {'endpoint':asdict(endpoint)})
    bridge = SimpleNamespace(identity=lambda: deepcopy(receipt), entry=receipt,
                             process=SimpleNamespace(pid=2345))
    output_receipt = {'user':'shiri-output-6'}
    output = SimpleNamespace(identity=lambda: deepcopy(output_receipt), entry=output_receipt)
    state = SimpleNamespace(bluetooth_admission=admission,
        processes={'bluetooth-output':bridge, 'owntone':output})
    units.UnitManager.validate_saved(receipt)
    yield state, SimpleNamespace(manager=manager), receipt
    listener.close()
    shutil.rmtree(scratch)


def test_actual_unitspec_canonical_empty_namespace_is_accepted_and_observed(actual_unit_authority):
    state, daemon, receipt = actual_unit_authority
    assert receipt['namespace'] == '' and 'NetworkNamespacePath' not in receipt['properties']
    report = {}
    evidence = fixture.endpoint_evidence(state, daemon, report)
    assert evidence['bridge'] == receipt
    assert report['bluetooth_endpoint_last_observed']['authority_verified'] is True
    receipt['properties']['CapabilityBoundingSet'] = 'later mutation'
    assert report['bluetooth_endpoint_last_observed']['bridge']['properties']['CapabilityBoundingSet'] == ''


@pytest.mark.parametrize('name,value', [('namespace',None),('namespace','/run/netns/shiri_foreign'),
    ('namespace','missing'),('NetworkNamespacePath',''),('NetworkNamespacePath',None),
    ('NetworkNamespacePath','/run/netns/shiri_foreign'),('CapabilityBoundingSet','CAP_NET_ADMIN'),
    ('AmbientCapabilities','CAP_NET_BIND_SERVICE'),('NoNewPrivileges','no'),('PrivateDevices','no'),
    ('DevicePolicy','auto'),('DeviceAllow','/dev/null rw /dev/snd/controlC7 rw'),
    ('DeviceAllow','/dev/null rw /dev/zero rw /dev/random r /dev/urandom r /dev/mem rw'),
    ('SupplementaryGroups','audio'),('SupplementaryGroups','shiri-bridge-7'),
    ('Environment','DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/dbus/system_bus_socket'),
    ('Environment','ALSA_CONFIG_PATH=/private/alsa.conf'),('InaccessiblePaths',''),
    ('RestrictAddressFamilies','AF_UNIX AF_INET'),('BindReadOnlyPaths','/run/dbus:/run/dbus:norbind'),
    ('policy_version',True),('user','root'),('user','shiri-output-6'),
    ('same_output_user',True),('pid_mismatch',2346),('missing_publication',True),
    ('publication_inode',0),('publication_field',True)])
def test_actual_unitspec_authority_mutants_reject_without_erasing_observation(actual_unit_authority, name, value):
    state, daemon, receipt = actual_unit_authority
    if name == 'namespace':
        if value == 'missing':
            receipt.pop(name)
        else:
            receipt[name] = value
    elif name in {'policy_version','user'}:
        receipt[name] = value
    elif name == 'same_output_user':
        state.processes['owntone'].entry['user'] = receipt['user']
    elif name == 'pid_mismatch':
        state.bluetooth_admission.worker_pid = value
    elif name == 'missing_publication':
        receipt.pop('socket_publication')
    elif name == 'publication_inode':
        receipt['socket_publication']['socket_inode'] = value
    elif name == 'publication_field':
        receipt['socket_publication']['unexpected'] = value
    else:
        receipt['properties'][name] = value
    report = {}
    with pytest.raises(route.RuntimeFailure):
        fixture.endpoint_evidence(state, daemon, report)
    observed = report['bluetooth_endpoint_last_observed']
    assert observed['bridge'] == receipt and observed['authority_verified'] is False
    assert observed['observed_bridge_pid'] == 2345


def test_observation_precedes_unchanged_endpoint_capability_assertion(actual_unit_authority):
    state, daemon, receipt = actual_unit_authority
    from dataclasses import replace
    endpoint = replace(state.bluetooth_admission.endpoint, synchronous_drop=False)
    state.bluetooth_admission.endpoint = endpoint
    report = {}
    with pytest.raises(route.RuntimeFailure,match='SBC/capability'):
        fixture.endpoint_evidence(state, daemon, report)
    assert report['bluetooth_endpoint_last_observed']['bridge'] == receipt
    assert report['bluetooth_endpoint_last_observed']['authority_verified'] is False



def codec_plateau_block(index, *, gain=1., voice=0., foreign=0., dc=0.):
    x = np.frombuffer(pcm(first=index*960, gain=gain, voice=voice), dtype='<i2').reshape(-1, 2).astype(np.int32)
    t = np.arange(len(x))/route.RATE
    x += (foreign*np.sin(2*np.pi*660*t)).astype(np.int32)[:, None]
    x += int(dc)
    return x.astype('<i2').tobytes()


@pytest.mark.parametrize('foreign,dc', [(60., 0.), (0., 30.)])
def test_plateau_never_settles_on_gain_qualified_foreign_source_or_dc(foreign, dc):
    gate = route.Plateau(8192., 1., voice=False, voice_floor=1., previous_ratio=.2, previous_voice_active=True)
    with pytest.raises(route.RuntimeFailure, match='settling law'):
        for index in range(40):
            gate.push(codec_plateau_block(index, foreign=foreign, dc=dc), 100+index*.02)
    assert gate.qualifying_first is None and not gate.settled
    assert all(not entry['matched'] for entry in gate.observations)
    assert gate.settle_frames == 26080


def test_forbidden_block_inside_qualifying_window_is_fatal_without_restart():
    gate = route.Plateau(8192., 1., voice=False, voice_floor=1.)
    for index in range(6):
        assert not gate.push(codec_plateau_block(index), 100+index*.02)
    with pytest.raises(route.RuntimeFailure, match='changed again'):
        gate.push(codec_plateau_block(6, foreign=60.), 100.12)
    assert gate.settled and gate.qualifying_first == 0 and gate.failure


def test_after_window_queued_tail_keeps_original_qualified_foreign_floor():
    chunks = [codec_plateau_block(index, foreign=60. if index == 22 else 0.) for index in range(24)]
    gate = route.Plateau(8192., 1., voice=False, voice_floor=1.)
    for index, block in enumerate(chunks):
        if gate.push(block, 100+index*.02):
            break
    assert gate.frames >= 19200 and gate.blocks >= 3 and gate.last_at-gate.first_at >= .35
    guard = route.DecodedGuard(SimpleNamespace(chunks=chunks, check=lambda **kwargs: None))
    with pytest.raises(route.RuntimeFailure, match='forbidden'):
        guard.qualify(440, 8192., 1., False, gate.qualifying_first, 'closed_restore')
    assert guard.failed_block == 22


@pytest.mark.parametrize('gain,expected_voice,voice', [( .5, False, 0.), (1., True, 0.), (1., False, 100.)])
def test_stricter_eligibility_keeps_original_wrong_gain_and_voice_rejections(gain, expected_voice, voice):
    gate = route.Plateau(8192., 1., voice=expected_voice, voice_floor=1., previous_ratio=.2,
                         previous_voice_active=not expected_voice)
    with pytest.raises(route.RuntimeFailure, match='settling law'):
        for index in range(40):
            gate.push(codec_plateau_block(index, gain=gain, voice=voice), 100+index*.02)
    assert not gate.settled


def test_stricter_eligibility_preserves_duplicate_callback_rejection():
    gate = route.Plateau(8192., 1., voice=False, voice_floor=1.)
    gate.push(codec_plateau_block(0), 100.)
    with pytest.raises(route.RuntimeFailure, match='did not advance'):
        gate.push(codec_plateau_block(1), 100.)
