"""Exact emitted Opus/C queue execution; external startup clocks are controlled.

These portable tests claim neither rootless HTTP/real OwnTone startup nor any
physical output. The separate optional measure_session does that in a lab.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from fractions import Fraction
import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import socket
import struct
import sys
import tempfile
from types import SimpleNamespace
from uuid import uuid4

import pytest

from shiri.runtime.native import NativeController, NativeMixer
from shiri.runtime.speech_output import HEADER, HEADER_BYTES, PCM, SpeechOutput
from shiri.runtime.system import RuntimeFailure

np = pytest.importorskip('numpy')
OpusEncoder = pytest.importorskip('aiortc.codecs.opus').OpusEncoder
AudioFrame = pytest.importorskip('av').AudioFrame
ROOT = Path(__file__).parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT/relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


finite = load('finite_actual_helper', 'tests/linux/native_speech_finite.py')
c_replay = load('finite_actual_c', 'tests/native/check_finite_speech.py')
reference = finite.reference_module()


def encoded_reference(*, body_frames=finite.BODY_FRAMES, seed=1):
    source = finite.waveform(body_frames=body_frames, seed=seed)
    decoder, encoder = reference.DecodedReference(str(uuid4())), OpusEncoder()
    payloads = []
    for first in range(0, body_frames+finite.CODEC_TAIL_FRAMES+finite.QUIET_FRAMES, finite.FRAMES):
        pcm = source[first:first+finite.FRAMES] if first < body_frames else np.zeros(finite.FRAMES, dtype='<i2')
        frame = AudioFrame(format='s16', layout='mono', samples=finite.FRAMES)
        frame.planes[0].update(pcm.tobytes())
        frame.sample_rate, frame.pts, frame.time_base = finite.RATE, first, Fraction(1, finite.RATE)
        packets, pts = encoder.encode(frame)
        assert len(packets) == 1 and pts == first
        payloads.append(packets[0])
        decoder.feed(packets[0], pts)
    trigger = {'utterance': 1, 'first_source_frame_to_encoder_monotonic_ns': 10_000_000_000,
               'rtp_frame_index': 0, 'body_frames': body_frames, 'codec_tail_frames': finite.CODEC_TAIL_FRAMES,
               'source_pcm_sha256': hashlib.sha256(source.tobytes()).hexdigest(), 'signal_seed': seed}
    track = SimpleNamespace(samples=decoder.next_pts, reference=decoder, error=None)
    return finite.frozen_reference(track, trigger), decoder.snapshot().pcm, payloads


@pytest.fixture(scope='module')
def emitted():
    return encoded_reference()


def observed(ref, *, start=1440, music=False):
    samples = np.zeros((start+len(ref['pcm'])+finite.QUIET_FRAMES+finite.FRAMES, 2), dtype=float)
    samples[start:start+len(ref['pcm'])] = ref['pcm'][:, None]
    if music:
        frames = np.arange(len(samples))
        samples += (1638*np.sin(2*np.pi*440*frames/finite.RATE))[:, None]
    return samples.astype('<i2')


@pytest.mark.parametrize('music', [False, True])
@pytest.mark.parametrize('start', [0, 119, 1440, 3851])
def test_exact_nonrepeating_emitted_opus_retains_all_opening_body_and_codec_tail(emitted, music, start):
    ref, _, _ = emitted
    actual = observed(ref, start=start, music=music)
    result = finite.verify_complete(actual.tobytes(), ref, start_bounds=(0, start+finite.FRAMES), music=music)
    assert result['alignment_start_frame'] == start
    assert result['cold_utterance_completeness_passed'] is True
    assert result['verified_body_frames'] == finite.BODY_FRAMES
    assert result['verified_codec_tail_frames'] == finite.CODEC_TAIL_FRAMES
    assert result['verified_quiet_frames'] == finite.QUIET_FRAMES
    assert len(result['all_windows']) == 65
    assert result['speech_latency_performance_passed'] is False
    assert result['source']['source_pcm_sha256'] != result['decoded_pcm_sha256']  # Actual lossy Opus decode.


@pytest.mark.parametrize('mutation', ['prefix360ms', 'first20ms', 'tail240ms', 'last20ms', 'tail_capture',
                                     'interior20ms', 'repeated_prefix', 'stale_tail', 'left_only', 'phase_drift'])
@pytest.mark.parametrize('music', [False, True])
def test_qualifying_later_audio_cannot_hide_any_finite_prefix_body_tail_loss(emitted, mutation, music):
    ref, _, _ = emitted
    start = 1440
    data = observed(ref, start=start, music=music)
    background = data.copy()
    background[start:start+len(ref['pcm'])] -= ref['pcm'][:, None]
    if mutation == 'prefix360ms':
        data[start:start+17280] = background[start:start+17280]
    elif mutation == 'first20ms':
        data[start:start+960] = background[start:start+960]
    elif mutation == 'tail240ms':
        at = start+finite.BODY_FRAMES-11520
        data[at:start+len(ref['pcm'])] = background[at:start+len(ref['pcm'])]
    elif mutation == 'last20ms':
        at = start+finite.BODY_FRAMES-960
        data[at:at+1920] = background[at:at+1920]
    elif mutation == 'tail_capture':
        data = data[:start+finite.BODY_FRAMES]
    elif mutation == 'interior20ms':
        at = start+23040
        data[at:at+960] = background[at:at+960]
    elif mutation == 'repeated_prefix':
        data[start:start+17280] = data[start+17280:start+34560]
    elif mutation == 'stale_tail':
        data[start+len(ref['pcm']):start+len(ref['pcm'])+960] += ref['pcm'][1920:2880, None]
    elif mutation == 'left_only':
        data[:, 1] = background[:, 1]
    else:
        at = start+finite.BODY_FRAMES//2
        data[at:start+len(ref['pcm'])-1] = data[at+1:start+len(ref['pcm'])]
    with pytest.raises(RuntimeFailure):
        finite.verify_complete(data.tobytes(), ref, start_bounds=(0, 2400), music=music)


def test_mild_downstream_lowpass_and_quantization_fit_predeclared_lossy_tolerance(emitted):
    ref, _, _ = emitted
    actual = observed(ref).astype(float)
    # Independent, fixed3tap downstream filter (not fitted by the verifier).
    for channel in range(2):
        actual[:, channel] = np.convolve(actual[:, channel], [.04, .92, .04], mode='same')
    actual = (np.rint(actual/2)*2).astype('<i2')
    result = finite.verify_complete(actual.tobytes(), ref, start_bounds=(0, 2400))
    assert result['cold_utterance_completeness_passed'] and .85 <= result['one_admitted_level'] <= 1.15


def test_short_reference_requires_actual_codec_flush_and_full_closed_tail():
    ref, _, _ = encoded_reference(body_frames=8*finite.FRAMES, seed=2)
    assert finite.verify_complete(observed(ref).tobytes(), ref, start_bounds=(0, 2400))['verified_body_frames'] == 7680
    broken = dict(ref, pcm=ref['pcm'][:-960])
    with pytest.raises(RuntimeFailure, match='relabeled'):
        finite.verify_complete(observed(ref).tobytes(), broken, start_bounds=(0, 2400))


async def test_actual_public_avpackets_decode_same_payloads_once_and_warm_reuses_continuous_session():
    from aiortc.codecs.opus import OpusDecoder
    from aiortc.jitterbuffer import JitterFrame
    from av import AudioResampler
    track = finite.packet_track(str(uuid4()), body_frames=8*finite.FRAMES)
    received, decoder = [], OpusDecoder()
    resampler = AudioResampler(format='s16', layout='mono', rate=finite.RATE)
    try:
        pending = asyncio.create_task(track.recv())
        await asyncio.sleep(.01)
        assert not pending.done() and track.samples == track.reference.packet_count == 0
        track.release()
        packet = await pending
        encoder = OpusEncoder()
        for index in range(2*(8+5+10)):
            if index:
                packet = await track.recv()
            payloads, pts = encoder.pack(packet)
            assert payloads == [bytes(packet)] and pts == index*960
            frames = decoder.decode(JitterFrame(payloads[0], pts))
            converted = resampler.resample(frames[0])
            received.append(bytes(converted[0].planes[0])[:1920])
            if index == 22:
                first = finite.frozen_reference(track, track.triggers[0])
                track.release()
        assert b''.join(received) == track.reference.snapshot().tobytes()
        second = finite.frozen_reference(track, track.triggers[1])
        assert first['session_id'] == second['session_id']
        assert first['source']['rtp_frame_index'] == 0 and second['source']['rtp_frame_index'] == 23*960
        assert first['decoded_pcm_sha256'] != second['decoded_pcm_sha256']  # Continuous codec state.
    finally:
        track.stop()
        track.reference.clear()
    assert track.readyState == 'ended' and track.reference.closed


@pytest.fixture(scope='module')
def actual_c():
    if not (shutil.which('clang') or shutil.which('cc')):
        pytest.skip('Exact maintained C source requires a compiler')
    with c_replay.compiled_driver() as pair:
        yield pair


def legacy_immediate_datagram(message):
    """Adapt only this immutable v1 preimage proof; production admits v2 only."""
    header = HEADER.unpack(message[:HEADER_BYTES])
    assert header[1:4] == (2, PCM, 96) and len(message) == HEADER_BYTES+header[4]
    legacy = struct.Struct("!8sBBHIQ16s16sQIIII")
    return legacy.pack(header[0], 1, header[2], 80, *header[4:13])+message[HEADER_BYTES:]


def datagrams(decoded):
    temporary = tempfile.TemporaryDirectory(prefix='sf-', dir='/tmp')
    directory = Path(temporary.name)/'speech'
    directory.mkdir()
    output_uid = os.getuid() or 1001
    os.chown(directory, output_uid if os.getuid() == 0 else -1, os.getgid())
    directory.chmod(0o2710)
    path = directory/'speech.sock'
    receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    receiver.bind(str(path))
    path.chmod(0o660)
    receiver.setblocking(False)
    # Exact private synthetic daemon ownership on root Linux, as in the
    # maintained speech socket fixture. No privileged credential claim.
    os.chown(path, output_uid if os.getuid() == 0 else -1, os.getgid())
    path.chmod(0o660)
    now = [10_000_000_000]
    output = SpeechOutput(path, str(uuid4()), uuid4().hex, output_uid, now_ns=lambda: now[0])
    output.begin(uuid4().hex)
    result = []
    try:
        for index, first in enumerate(range(0, len(decoded), 960)):
            now[0] = 10_000_000_000+index*20_000_000
            assert output.push(decoded[first:first+960].tobytes(), 960)
            message = receiver.recv(4096)
            header = HEADER.unpack(message[:HEADER_BYTES])
            assert header[8] == now[0] and header[9] == 960
            result.append((now[0], legacy_immediate_datagram(message)))
        assert output.sent_frames == len(decoded) and output.dropped_frames == 0
        return result, output.room, output.launch
    finally:
        output.close()
        receiver.close()
        temporary.cleanup()


def startup_events(packets, delay_ns, *, extra_tail=12):
    first = packets[0][0]+delay_ns
    ticks = len(packets)+extra_tail
    events, cursor = [], 0
    for index in range(ticks):
        now = first+index*20_000_000
        while cursor < len(packets) and packets[cursor][0] <= now:
            # In production shiri_speech_poll is at the actual playback tick,
            # so datagrams queued before the first tick are admitted NOW.
            events.append((1, now, packets[cursor][1]))
            cursor += 1
        events.append((2, now, bytes(3840)))
    assert cursor == len(packets)
    return events


@pytest.mark.parametrize('delay_ns', [0, 100_000_000, 230_000_000])
def test_actual_c_valid_preexpiry_startup_retains_complete_finite_opus(actual_c, emitted, tmp_path, delay_ns):
    ref, decoded, _ = emitted
    packets, room, launch = datagrams(decoded)
    data, stats = c_replay.replay(actual_c[0], startup_events(packets, delay_ns), room=room, launch=launch)
    result = finite.verify_complete(data, ref, start_bounds=(0, 960))
    assert result['alignment_start_frame'] == 0 and result['cold_utterance_completeness_passed']
    assert stats['refused'] == stats['expired'] == stats['remaining_frames'] == 0
    assert not stats['kernel_credentials_proven']


@pytest.mark.parametrize('delay_ns', [250_000_000, 590_000_000])
def test_actual_c_cold_startup_expiry_erases_prefix_even_though_later_finite_voice_survives(
    actual_c, emitted, tmp_path, delay_ns
):
    ref, decoded, _ = emitted
    packets, room, launch = datagrams(decoded)
    data, stats = c_replay.replay(actual_c[0], startup_events(packets, delay_ns), room=room, launch=launch)
    assert stats['refused'] > 0 and stats['admitted'] > 30
    assert np.sqrt(np.mean(np.frombuffer(data, dtype='<i2').astype(float)**2)) > 100
    with pytest.raises(RuntimeFailure):
        finite.verify_complete(data, ref, start_bounds=(0, 960))
    if delay_ns == 590_000_000:
        # Actual C gate rejects18old20ms packets = missing360ms prefix.
        assert stats['refused'] == 18
        assert data[:960*4] == np.repeat(decoded[17280:18240, None], 2, axis=1).astype('<i2').tobytes()


def test_actual_c_exact_250ms_freshness_boundary_and_guard_preimage_sensitivity(actual_c, emitted, tmp_path):
    _, decoded, _ = emitted
    packets, room, launch = datagrams(decoded[:960])
    origin, message = packets[0]
    for age, admitted in [(249_999_999, 1), (250_000_000, 0)]:
        data, stats = c_replay.replay(actual_c[0], [(1, origin+age, message), (2, origin+age, bytes(3840))],
                                    room=room, launch=launch)
        assert stats['admitted'] == admitted
        assert (data == np.repeat(decoded[:960, None], 2, axis=1).astype('<i2').tobytes()) == bool(admitted)
    patch = c_replay.PATCH.read_text()
    assert patch.count('now - emitted >= SPEECH_AGE_NS') == 1
    broken = tmp_path/'accept-expired.patch'
    broken.write_text(patch.replace('now - emitted >= SPEECH_AGE_NS', '0', 1))
    with c_replay.compiled_driver(patch=broken) as (binary, _):
        data, stats = c_replay.replay(binary, [(1, origin+250_000_000, message),
                                             (2, origin+250_000_000, bytes(3840))], room=room, launch=launch)
        # Disabled admission guard is observable even though mix expiry still
        # correctly retires the queued stale samples before final presentation.
        assert stats['admitted'] == 1 and stats['expired'] == 960 and data == bytes(3840)


async def test_actual_idle_anchor_is_B_plus_100_without_music_ownership_or_extra_backend_commands():
    from shiri.runtime.timing import ZERO_UUID
    now = 10_000_000_000
    packets, requests = [], []
    class Writer:
        reader_present, written_bytes, dropped_bytes = True, 0, 0
        def reset(self, owner):
            self.owner = owner
        def write(self, packet):
            assert (packet.incarnation, packet.session, packet.epoch, packet.generation) == self.owner
            packets.append(packet)
        def close(self):
            pass
    class Output:
        def set_gain(self, gain):
            self.gain = gain
        def push(self, pcm, frames):
            return True
        def control(self, active, gain):
            return True
        def close(self):
            pass
    class Client:
        async def request(self, method, path, *, json):
            requests.append((method, path, json))
            return dict(json)
    mixer = NativeMixer(Path('/unused'), writer=Writer(), now_ns=lambda: now, speech_output=Output(),
                        relay_delay_ns=1_000_000_000, output_buffer_ms=500)
    controller = NativeController(str(uuid4()), mixer, Client())
    try:
        await controller.initialize()
        mixer.push_speech(np.full(960, 600, dtype='<i2').tobytes(), 960)
        mixer.tick(music_active=False, speech_active=True, duck_gain=.2, elapsed=.02)
        assert len(packets) == 1 and packets[0].presentation_ns == now+600_000_000
        assert packets[0].session == ZERO_UUID and packets[0].pcm == bytes(3840)
        assert packets[0].presentation_ns-mixer.output_buffer_ms*1_000_000-now == 100_000_000
        assert controller.actor.snapshot()['owner'] is None and len(requests) == 1
    finally:
        await controller.close()


@pytest.mark.parametrize('fault', [None, 'wrong_room', 'prefix', 'close', 'observer', 'warm_stop', 'warm_item', 'warm_owner', 'warm_units', 'warm_selected', 'warm_health_with_stop', 'warm_source_epoch', 'warm_stop_after_release', 'warm_cold_stop_rpc_error'])
async def test_real_measure_coroutine_keeps_original_observer_and_retains_failure_bytes(
    monkeypatch, tmp_path, fault
):
    """Real helper/Opus; HTTP, runtime and final capture facts simulated."""
    import aiortc
    from aiortc.codecs.opus import OpusDecoder
    from aiortc.jitterbuffer import JitterFrame
    from av import AudioResampler
    original_track = finite.packet_track
    monkeypatch.setattr(finite, 'BODY_FRAMES', 8*960)
    monkeypatch.setattr(finite, 'packet_track', lambda session: original_track(session, body_frames=8*960))
    capture = SimpleNamespace(chunks=[], captured_at=[], absolute={}, buffer_metadata={}, expected_base=1, clock_offset_ns=0, rate=48000, channels=2,
                              format='S16LE', poll=lambda: None)
    units = {'owntone': SimpleNamespace(identity=lambda: {'invocation': 'unchanged-original'})}
    calls, peers = [], []
    class FakePeer:
        def __init__(self, configuration):
            assert configuration.iceServers == []
            self.connectionState = 'new'
            self.localDescription = SimpleNamespace(sdp='synthetic-external-sdp')
            self.task = None
            peers.append(self)
        def addTransceiver(self, track, direction):
            assert direction == 'sendonly'
            self.track = track
            return SimpleNamespace(setCodecPreferences=lambda codecs: None)
        async def createOffer(self):
            return None
        async def setLocalDescription(self, description):
            pass
        async def setRemoteDescription(self, description):
            self.connectionState = 'connected'
            self.task = asyncio.create_task(self.send())
        async def send(self):
            decoder = OpusDecoder()
            resampler = AudioResampler(format='s16', layout='mono', rate=48000)
            while True:
                packet = await self.track.recv()
                frame = decoder.decode(JitterFrame(bytes(packet), packet.pts))[0]
                mono = bytes(resampler.resample(frame)[0].planes[0])[:1920]
                values = np.frombuffer(mono, dtype='<i2').copy()
                marker = self.track.triggers[-1]
                if fault == 'prefix' and packet.pts-marker['rtp_frame_index'] < 960:
                    values[:] = 0
                data = np.repeat(values[:, None], 2, axis=1).astype('<i2').tobytes()
                pts = marker['first_source_frame_to_encoder_monotonic_ns']+500_000_000
                pts += (packet.pts-marker['rtp_frame_index'])*1_000_000_000//48000
                at = __import__('time').monotonic()
                capture.chunks.append(data)
                capture.captured_at.append(at)
                capture.absolute[at] = pts
                capture.buffer_metadata[at] = {'offset': packet.pts, 'offset_end': packet.pts+960, 'pts': pts-1, 'duration': 20_000_000, 'discont': False}
        async def close(self):
            self.connectionState = 'closed'
            if self.task:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
    monkeypatch.setattr(aiortc, 'RTCPeerConnection', FakePeer)
    class Api:
        async def request(self, method, path, *, json):
            calls.append((method, path, deepcopy(json)))
            if json['action'] == 'offer':
                now = __import__('time').monotonic_ns()
                startup.update(identity={key: json[key] for key in ('session_id', 'request_id')}, nonce=uuid4().hex, started=now)
                return {'admitted_room_id': 'wrong' if fault == 'wrong_room' else target,
                        'sdp': 'synthetic-external-sdp', 'type': 'answer'}
            if fault == 'close':
                raise RuntimeFailure('synthetic exact API close failure')
            return {}
    class Observer:
        checks = 0
        async def check(self):
            self.checks += 1
            if fault == 'observer' and self.checks >= 5:
                raise RuntimeFailure('original untouched guard failed')
    async def observed_wait(predicate, observer, seconds, message):
        deadline = __import__('time').monotonic()+seconds
        while __import__('time').monotonic() < deadline:
            await observer.check()
            value = await predicate()
            if value:
                return value
            await asyncio.sleep(.01)
        raise RuntimeFailure(message)
    player_calls, health_calls = [], []
    target, launch = str(uuid4()), uuid4().hex
    source_incarnation = str(uuid4())
    startup = {}
    async def player(method, path):
        player_calls.append((method, path))
        warm = bool(peers and len(peers[0].track.triggers) == 2) or len(player_calls) >= 5
        if fault == 'warm_units' and warm:
            units['owntone'] = SimpleNamespace(identity=lambda: {'invocation': 'replacement'})
        return {'state': 'stop' if fault == 'warm_cold_stop_rpc_error' and len(player_calls) >= 4 or
                warm and fault in {'warm_stop', 'warm_health_with_stop'} or
                fault == 'warm_stop_after_release' and peers and len(peers[0].track.triggers) == 2 else
                'play' if peers and peers[0].connectionState == 'connected' else 'stop',
                'item_id': 2 if warm and fault == 'warm_item' else 1}
    async def health_rpc(*args, **kwargs):
        health_calls.append(args)
        if len(player_calls) >= 4 and fault == 'warm_cold_stop_rpc_error':
            raise RuntimeFailure('Known stopped cold baseline must not be masked by later RPC error')
        if len(player_calls) >= 5 and fault == 'warm_health_with_stop':
            raise RuntimeFailure('This later diagnostic error must not mask stopped driver')
        health = {'ready': True, 'error': None, 'source_operation_generation': 7, 'native_generation': None,
            'source': {'ready': True, 'error': None, 'zone_id': target,
            'incarnation': source_incarnation, 'epoch': 1 if len(player_calls) >= 5 and fault == 'warm_source_epoch' else 0,
            'owner': {'session_id': 'replacement'} if len(player_calls) >= 5 and fault == 'warm_owner' else None}}
        if startup:
            started = startup['started']
            ack = {'incarnation': source_incarnation.replace('-', ''), 'session_id': None, 'epoch': 0, 'generation': 1,
                'operation_generation': 7, 'room_id': target.replace('-', ''), 'launch_generation': launch,
                'speech_id': startup['nonce'], 'action': 'ready', 'connected': True, 'ready': True,
                'prepared_monotonic_ns': started, 'mixed_monotonic_ns': started+1, 'output_count': 1}
            health.update(speech_session_id=startup['identity']['session_id'],
                speech_startup_identity={'speech_id': startup['nonce'], **startup['identity']},
                speech_startup_authenticated_ready_ack=ack, speech_startup_phase='ready', speech_startup_error=None,
                speech_startup_started_monotonic_ns=started, speech_startup_prepared_monotonic_ns=started,
                speech_startup_first_mix_monotonic_ns=started+1, speech_startup_released_monotonic_ns=started+2,
                speech_startup_pending_frames=0, speech_startup_setup_budget_ns=5_000_000_000,
                speech_startup_performance_qualified=False)
        return health
    monkeypatch.setattr(finite, 'call_rpc', health_rpc)
    async def outputs(excluded):
        return [{'id': '0', 'protocol': 'alsa', 'selected': True,
                 'offset_ms': 1 if len(player_calls) >= 5 and fault == 'warm_selected' else 0}]
    state = SimpleNamespace(processes=units, launch_generation=launch, client=SimpleNamespace(request=player, outputs=outputs),
        selected_ids=['0'], local_pin=SimpleNamespace(manifest={'device': 0, 'subdevice': 7}, validate=lambda: None))
    guard = SimpleNamespace(capture=capture)
    context = SimpleNamespace(group=SimpleNamespace(NATIVE_LAB='simulated-external-fact'),
        phase=SimpleNamespace(phase='idle', offset_ms=0), report={'native_lab': {'simulated_external': True},
            'rootless_api': {'simulated_external': True},
            'independent_capture_baselines': {target: {'negotiated_period_frames': 960, 'period_uncertainty_ms': 20}}}, target=target, states={target: state},
        captures={target: capture}, pcm_guards={target: guard}, api=Api(), temporary=tmp_path,
        broker=SimpleNamespace(_worker_socket=lambda state: '/private/exact-worker.sock'),
        frozen_plan={'output_buffers_ms': {target: 500}}, latency=SimpleNamespace(observed_wait=observed_wait))
    observer = Observer()
    if fault:
        with pytest.raises(RuntimeFailure, match='already playing OwnTone path' if fault in {'warm_stop', 'warm_health_with_stop', 'warm_cold_stop_rpc_error'} else 'during utterance' if fault == 'warm_stop_after_release' else None):
            await finite.measure_session(context, observer, role='idle')
        if fault.startswith('warm_'):
            assert len(context.report['finite_speech_diagnostics'][0]['rows']) == 2
            assert len(peers[0].track.triggers) == (2 if fault == 'warm_stop_after_release' else 1)  # Original prefix/tail completed; second utterance never emitted.
            assert context.report['finite_speech_diagnostics'][0]['rows'][0]['cold_utterance_completeness_passed']
        if fault == 'warm_health_with_stop':
            assert len(health_calls) == 4
        if fault == 'warm_cold_stop_rpc_error':
            assert len(health_calls) == 3
    else:
        result = await finite.measure_session(context, observer, role='idle')
        assert result['cold_utterance_completeness_passed'] and len(result['rows']) == 2
        assert result['player_before_offer']['state'] == 'stop'
        assert result['player_before_payload_release']['state'] == 'play'
        assert result['timer_ready_ack']['speech_id'] == startup['nonce']
        assert result['offer_scope'] == finite.READY_SCOPE
        assert observer.checks > 20
        for row in result['rows']:
            assert row['source_to_final_reference_origin_ms'] == 500
            assert row['offer_request_to_encoder_ms'] >= 0
    record = context.report['finite_speech_diagnostics'][0]
    assert context.pcm_guards[target] is guard and context.captures[target] is capture
    assert state.processes is units and observer.checks >= 0
    assert peers[0].connectionState == 'closed' and peers[0].track.reference.closed
    assert calls[-1][2]['action'] == 'close' and calls[-1][1] == f'/api/v1/rooms/{target}/speech'
    if fault == 'prefix':
        assert record['rows'][0]['cold_utterance_completeness_passed'] is False
        assert await asyncio.to_thread(lambda: all(Path(item['path']).is_file() for item in record['rows'][0]['artifacts'].values()))
        assert record['reference_evidence']['packets'] > 0
    if fault == 'close':
        assert record['cold_utterance_completeness_passed'] is False and record['cleanup_errors']
