"""Adversarial final-PCM stress observers; no GI, devices, VM or namespaces."""
import asyncio
from collections import deque
from fractions import Fraction
import importlib.util
import hashlib
import json
from pathlib import Path
import threading
import tempfile
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from synthetic_speech import SyntheticSpeechWaveform

np = pytest.importorskip('numpy', reason='Speech stress requires the audio extra')
pytest.importorskip('aiortc', reason='Manual grouped fixture imports the audio extra')
ROOT = Path(__file__).parent/'linux'


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT/filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stress = load('speech_stress_observer_tests', 'native_speech_stress.py')
group = load('speech_stress_group_tests', 'check_native_grouping.py')


def pcm(frame=0, *, gain=1., a=0., b=0.):
    times = (frame+np.arange(960))/48000
    mono = (8192*gain*np.sin(2*np.pi*440*times)+a*np.sin(2*np.pi*880*times+.3)
            +b*np.sin(2*np.pi*1320*times-.7)).astype('<i2')
    return np.repeat(mono[:, None], 2, axis=1).tobytes()


def capture():
    cls = stress.capture_type(group.Capture)
    value = cls.__new__(cls)  # No GI constructor or media device is executed.
    value.__dict__.update(error=None, capture_dropped=0, needs_latency=False,
        pending=deque(), rate=None, channels=None, format=None, total=0,
        chunks=[], captured_at=[], absolute={}, buffer_metadata={}, next_frame=0, device='virtual-observer-only',
        sequence=group.observation.PcmSequence(), discontinuities=1, warning=None,
        expected_base=1_000_000_000, clock_offset_ns=0, max_packet_gap=0, last_packet_at=None)
    return value


def append(value, data=None, *, at=None, metadata=None):
    frame = value.next_frame
    data = pcm(frame) if data is None else data
    at = 100+frame/48000 if at is None else at
    facts = {'offset': frame, 'offset_end': frame+len(data)//4,
             'pts': frame*1_000_000_000//48000, 'duration': len(data)//4*1_000_000_000//48000,
             'discont': frame == 0}
    if metadata:
        facts.update(metadata)
    value.absolute[at] = value.expected_base+facts['pts']
    value.buffer_metadata[at] = dict(facts)
    value.pending.append((at, data, 48000, 2, 'S16LE', facts))
    value.next_frame += len(data)//4
    value.last_packet_at = stress.time.monotonic()


def observer():
    captures = {'A': capture(), 'B': capture()}
    for value in captures.values():
        append(value)
        value.poll()
    references = {room: stress.spectrum(value.chunks[-1]) for room, value in captures.items()}
    return stress.PcmGuard(captures, references, {'A': 880, 'B': 1320})


@pytest.mark.parametrize('frequency', [880, 1320])
def test_independent_marker_projection_retains_voice_and_music_at_arbitrary_phase(frequency):
    measured = stress.spectrum(pcm(1793, gain=.2, a=423 if frequency == 880 else 0,
                                  b=423 if frequency == 1320 else 0))
    assert measured['music'] == pytest.approx(8192*.2, abs=1)
    assert measured['voice'][frequency] == pytest.approx(423, abs=1)
    assert measured['voice'][1320 if frequency == 880 else 880] < 1


@pytest.mark.parametrize('frames', [480, 960])
@pytest.mark.parametrize('direction', ['duck', 'restore'])
def test_synthetic_smooth_gain_ramps_do_not_invent_a_peer_voice_marker(frames, direction):
    times = np.arange(frames)/48000
    gain = .2+.8*np.exp(-times/.06)
    if direction == 'restore':
        gain = 1.2-gain
    signal = (8192*gain*np.sin(2*np.pi*440*times+.8)
              +423*(1-gain)*np.sin(2*np.pi*880*times+.3)).astype('<i2')
    measured = stress.spectrum(np.repeat(signal[:, None], 2, axis=1).tobytes())
    assert .15 < measured['music']/8192 < 1.05
    assert measured['voice'][1320] < 1
    # The same local envelope fit still exposes a real wrong-room marker.
    leaked = (signal.astype(float)+40*np.sin(2*np.pi*1320*times-.7)).astype('<i2')
    assert stress.spectrum(np.repeat(leaked[:, None], 2, axis=1).tobytes())['voice'][1320] > 38


def test_simultaneous_peer_markers_cannot_be_hidden_by_successful_own_voice():
    guard = observer()
    guard.set_mode('A', 'active')
    append(guard.captures['A'], pcm(960, gain=.2, a=423, b=40))
    with pytest.raises(stress.RuntimeFailure, match='Peer-zone speech'):
        guard.check()
    before = guard.index['A']
    append(guard.captures['A'], pcm(1920, gain=.2, a=423))
    with pytest.raises(stress.RuntimeFailure, match='Peer-zone speech'):
        guard.check()
    assert guard.index['A'] == before and guard.evidence()['failed_buffer']['room_id'] == 'A'


def test_real_five_ms_peer_tone_cannot_hide_in_a_low_midpoint_of_its_fitted_envelope():
    guard = observer()
    guard.set_mode('A', 'active')
    values = np.frombuffer(pcm(960, gain=.2, a=423), dtype='<i2').reshape(-1, 2).astype(float)
    times = np.arange(960)/48000
    leaked = 600*np.sin(2*np.pi*1320*times+.12693303650867852)*(np.arange(960) < 240)
    data = (values+leaked[:, None]).astype('<i2').tobytes()
    matrix, _powers = stress.projection(960)
    coefficients = matrix @ np.frombuffer(data, dtype='<i2').reshape(-1, 2)[:, 0].astype(float)
    # The exact observer preimage accepted this real5ms/600-count wrong-room
    # tone because it measured only these midpoint coefficients (<4counts).
    assert np.hypot(coefficients[16], coefficients[17]) < 4
    assert stress.spectrum(data)['voice'][1320] > 100
    append(guard.captures['A'], data)
    with pytest.raises(stress.RuntimeFailure, match='Peer-zone speech'):
        guard.check()
    assert guard.index['A'] == 1


@pytest.mark.parametrize('mode,gain,voice', [('idle', .2, 0), ('idle', 1, 30),
                                          ('active', 1, 423), ('active', .2, 0)])
def test_every_buffer_enforces_idle_restore_or_continuing_active_peer(mode, gain, voice):
    guard = observer()
    guard.set_mode('A', mode)
    append(guard.captures['A'], pcm(960, gain=gain, a=voice))
    with pytest.raises(stress.RuntimeFailure, match='speech|music'):
        guard.check()
    assert guard.checked['A'] == 0


def test_transition_gain_allowance_cannot_hide_the_real_eight_ms_truncation_pattern():
    guard = observer()
    guard.set_mode('A', 'ending')
    data = bytearray(pcm(960))
    data[576*4:] = bytes(384*4)
    # Its fitted amplitude falls inside legitimate gain-ramp bounds, so the
    # independent per-frame silence guard is necessary to catch this gap.
    assert .15 < stress.spectrum(data)['music']/guard.references['A']['music'] < 1.05
    append(guard.captures['A'], bytes(data))
    with pytest.raises(stress.RuntimeFailure, match='four consecutive silent'):
        guard.check()
    assert guard.index['A'] == 1


def test_four_frame_silence_crossing_buffer_boundaries_is_sticky():
    guard = observer()
    guard.set_mode('A', 'starting')
    # Place the four missing samples around a real carrier zero crossing so
    # the independent peer-tone test does not mask this continuity test.
    samples = (8192*np.sin(2*np.pi*440*(np.arange(-960, 960)+1.5)/48000)).astype('<i2')
    data = np.repeat(samples[:, None], 2, axis=1).tobytes()
    first, second = bytearray(data[:3840]), bytearray(data[3840:])
    first[-8:] = bytes(8)
    second[:8] = bytes(8)
    append(guard.captures['A'], bytes(first))
    guard.check()
    append(guard.captures['A'], bytes(second))
    with pytest.raises(stress.RuntimeFailure, match='four consecutive silent'):
        guard.check()


@pytest.mark.parametrize('kind,bad', [('active', pcm(gain=1)), ('active', pcm(gain=.2)),
                                    ('idle', pcm(gain=.2, a=423))])
def test_qualifying_prefix_cannot_accept_a_current_wrong_buffered_tail(kind, bad):
    guard = observer()
    guard.set_mode('A', 'starting' if kind == 'active' else 'ending')
    gate = stress.Gate('A', kind, 100, guard)
    for _ in range(22):
        frame = guard.captures['A'].next_frame
        append(guard.captures['A'], pcm(frame, gain=.2, a=423) if kind == 'active' else pcm(frame))
    for _ in range(5):
        append(guard.captures['A'], bad)
    guard.check()
    assert not gate.check() and not gate.passed and gate.frames == 0
    assert gate.index == len(guard.captures['A'].chunks)
    for _ in range(22):
        frame = guard.captures['A'].next_frame
        append(guard.captures['A'], pcm(frame, gain=.2, a=423) if kind == 'active' else pcm(frame))
    guard.check()
    assert gate.check() and gate.frames >= 19200


def test_capture_byte_budget_does_not_disable_actual_offset_or_discontinuity_fences():
    value = capture()
    append(value)
    value.poll()
    append(value, metadata={'offset': 1920, 'offset_end': 2880})
    with pytest.raises(stress.RuntimeFailure, match='frame offsets'):
        value.poll()
    before = value.total
    append(value)
    with pytest.raises(stress.RuntimeFailure, match='frame offsets'):
        value.poll()
    assert value.total == before and len(value.chunks) == 1


def test_explicit_128_mib_budget_accepts_its_boundary_and_retains_unconsumed_overflow():
    value = capture()
    value.total = stress.CAPTURE_BYTES-3840
    append(value)
    assert value.poll()['observed_bytes'] == stress.CAPTURE_BYTES
    append(value)
    with pytest.raises(stress.RuntimeFailure, match='128MiB'):
        value.poll()
    assert value.total == stress.CAPTURE_BYTES and len(value.pending) == 1


def test_cumulative_npt_cannot_stall_through_many_individually_small_steps():
    anchors = {}
    stress.observe_progress(anchors, 'A', 10000, 100)
    for step in range(1, 15):
        stress.observe_progress(anchors, 'A', 10000, 100+step/10)
    with pytest.raises(stress.RuntimeFailure, match='stalled'):
        stress.observe_progress(anchors, 'A', 10000, 101.5)


@pytest.mark.parametrize('progress', [9999, 14000, True, None])
def test_npt_rewind_jump_or_bad_readback_cannot_pass_a_continuous_stress_program(progress):
    anchors = {}
    stress.observe_progress(anchors, 'A', 10000, 100)
    with pytest.raises(stress.RuntimeFailure, match='Stress NPT'):
        stress.observe_progress(anchors, 'A', progress, 100.1)


@pytest.mark.asyncio
async def test_client_cancellation_is_exercised_only_after_exact_pending_broker_operation():
    room, session = str(uuid4()), str(uuid4())
    broker = SimpleNamespace(pending_sessions=set(), sessions={session: room})
    completion = asyncio.Future()
    class Api:
        async def request(self, _method, path, *, json):
            assert path == f'/api/v1/rooms/{room}/speech' and json['session_id'] == session
            broker.pending_sessions.add(session)
            return await asyncio.shield(completion)
    checks = []
    async def check():
        checks.append(True)
    result = await stress.canceled_close(Api(), broker, room, {'session_id': session, 'request_id': 'offer'}, check)
    assert result == {'client_canceled_after_exact_broker_admission': True}
    assert not completion.cancelled() and checks
    completion.set_result({'ok': True})


@pytest.mark.asyncio
async def test_a_finished_or_unsubmitted_close_cannot_be_counted_as_client_cancellation():
    broker = SimpleNamespace(pending_sessions=set(), sessions={'session': 'room'})
    class Api:
        async def request(self, *_args, **_kwargs):
            return {'ok': True}
    async def check():
        pass
    with pytest.raises(stress.RuntimeFailure, match='actual admitted in-flight'):
        await stress.canceled_close(Api(), broker, 'room', {'session_id': 'session'}, check)


@pytest.mark.asyncio
@pytest.mark.parametrize('frequency', [880, 1320])
async def test_real_media_track_retains_declared_marker_pcm_and_paced_frame_indices(frequency):
    peer, tone = stress.make_peer(frequency)
    try:
        first, second = await tone.recv(), await tone.recv()
        assert (first.pts, second.pts, first.time_base) == (0, 960, Fraction(1, 48000))
        from aiortc.codecs.opus import OpusEncoder
        assert OpusEncoder().pack(first) == ([bytes(first)], 0)
        decoded = tone.reference.snapshot()
        assert len(decoded) == 1920 and decoded.packet_count == 2
        assert float(np.sqrt(np.mean(decoded.pcm.astype(float)**2))) > 200
    finally:
        tone.stop()
        await peer.close()
        tone.reference.clear()


class Driver:
    """Run the real stress loop with captured PCM and exact fake control I/O.

    Synthetic PCM and fake HTTP/RPC/media negotiation drive the actual
    capture metadata, every-buffer observers, transition gates, round counting,
    cancellation, rejection and cleanup paths in the measurement harness.
    Passing this driver does not qualify the production native mixer.
    """
    def __init__(self, monkeypatch, *, audible=True):
        from shiri.domain import Room
        self.now, self.audible = 100., audible
        self.rooms = (str(uuid4()), str(uuid4()))
        self.freq = dict(zip(self.rooms, (880, 1320), strict=True))
        self.broker = SimpleNamespace(pending_sessions=set(), sessions={}, _worker_socket=lambda state: state.desired.id)
        self.captures = {room: capture() for room in self.rooms}
        self.states = {room: SimpleNamespace(desired=Room(id=room, slot=index, name=f'Zone{index}',
            airplay_name=f'Zone{index}', nobly_room_id=f'nobly{index}', interface='eth0', volume=100, duck_gain=.2),
            client=SimpleNamespace(request=lambda method, path, room=room: self.player(room)))
            for index, room in enumerate(self.rooms)}
        self.guards = {room: SimpleNamespace(reference={'original': room}) for room in self.rooms}
        self.references = {room: self.guards[room].reference for room in self.rooms}
        self.owner = {room: {'incarnation': str(uuid4()), 'epoch': 1, 'session_id': str(uuid4()),
                            'zone_id': room, 'protocol': 'airplay2'} for room in self.rooms}
        self.preparations = {}
        self.current, self.peers, self.tasks, self.requests = {}, {}, [], []
        self.artifact_root = Path(tempfile.mkdtemp(prefix='stress-reference-test-'))
        self.report, self.phases, self.checks = {'artifacts': {'private_directory': str(self.artifact_root)}}, [], 0
        self.transform_pcm = lambda _room, data: data
        self.waveforms = {room: SyntheticSpeechWaveform() for room in self.rooms}
        monkeypatch.setattr(stress, 'time', SimpleNamespace(monotonic=lambda: self.now))
        async def yield_now(_seconds=0):
            await asyncio.sleep(0)
        async def bounded_direct_work(function, *args):
            # The actual finite matcher runs, without thread scheduling moving
            # this deliberately accelerated synthetic control clock. Real local
            # RTC/worker-thread ownership has separate reference regressions.
            return function(*args)
        monkeypatch.setattr(stress, 'asyncio', SimpleNamespace(Event=asyncio.Event,
            create_task=asyncio.create_task, gather=asyncio.gather, wait_for=asyncio.wait_for,
            wait=asyncio.wait, shield=asyncio.shield, to_thread=bounded_direct_work,
            CancelledError=asyncio.CancelledError, sleep=yield_now))
        monkeypatch.setattr(stress, 'ROUNDS', 4)  # Includes both EOF and admitted client-cancel rounds.
        monkeypatch.setattr(stress, 'make_peer', self.peer)
        monkeypatch.setattr(stress, 'call_rpc', self.health)
        for value in self.captures.values():
            append(value, at=self.now)
            value.poll()

    async def player(self, room):
        assert room in self.rooms
        return {'state': 'play', 'item_id': 'unchanged-native-program',
                'item_progress_ms': 10000+round((self.now-100)*1000)}

    async def room(self, room):
        return self.states[room].desired.model_dump(mode='json')

    async def health(self, room, operation, arguments, timeout):
        assert operation == 'health' and arguments == {} and timeout == 2
        preparation = self.preparations.get(room, {})
        return {'source': {'owner': self.owner[room]}, **preparation,
                'speech_session_id': self.current[room].session if room in self.current else None,
                'speech_cleanup_pending': 0, 'speech_ready': True, 'speech_cleanup_error': None}

    def end(self, peer):
        # Closing a retired negotiation never closes its successor.
        if self.current.get(peer.room) is peer:
            self.current.pop(peer.room)
            self.broker.sessions.pop(peer.session)
            self.broker.pending_sessions.discard(peer.session)
            prepared = self.preparations[peer.room]
            prepared['speech_startup_authenticated_retirement_ack'] = {
                **prepared['speech_startup_authenticated_begin_ack'], 'action': 'cancel', 'connected': False,
                'ready': False, 'prepared_monotonic_ns': 0, 'mixed_monotonic_ns': 0, 'output_count': 0}

    def peer(self, frequency, session_id=None):
        driver = self
        class Tone:
            stopped = False
            readyState = 'live'
            samples = 0
            def __init__(self):
                from aiortc.codecs.opus import OpusEncoder
                self.reference = stress.reference.DecodedReference(session_id or str(uuid4()))
                self.encoder = OpusEncoder()
            def stop(self):
                self.stopped = True
                self.readyState = 'ended'
        class Peer:
            room = session = None
            connectionState = 'new'
            def __init__(self):
                self.key, self.tone = str(uuid4()), Tone()
                self.localDescription = SimpleNamespace(sdp=f'private-offer-{self.key}', type='offer')
                driver.peers[self.localDescription.sdp] = self
            async def createOffer(self):
                return self.localDescription
            async def setLocalDescription(self, offer):
                assert offer is self.localDescription
            async def setRemoteDescription(self, answer):
                assert answer.sdp == f'private-answer-{self.room}' and answer.type == 'answer'
                self.connectionState = 'connected'
            async def close(self):
                driver.end(self)
                self.connectionState = 'closed'
            async def getStats(self):
                return {'audio': SimpleNamespace(type='outbound-rtp', kind='audio',
                    packetsSent=self.tone.reference.packet_count)}
        peer = Peer()
        peer.frequency = frequency
        return peer, peer.tone

    async def request(self, method, path, *, json, expected=200):
        assert method == 'POST'
        room = next((room for room in self.rooms if f'/{room}/' in path
                     or f'/{self.states[room].desired.nobly_room_id}/' in path), None)
        assert room is not None
        action, session = json['action'], json['session_id']
        self.requests.append((action, room, expected))
        if expected == 409:
            assert ((action == 'offer' and room in self.current)
                    or (action == 'close' and (room not in self.current or self.current[room].session != session)))
            return {'detail': 'Exact owner conflict'}
        if action == 'offer':
            assert room not in self.current
            peer = self.peers[json['sdp']]
            assert peer.frequency == self.freq[room]
            peer.room, peer.session = room, session
            owner = self.owner[room]
            speech_id = uuid4().hex
            begin = {'room_id': room.replace('-', ''), 'launch_generation': uuid4().hex,
                'incarnation': owner['incarnation'].replace('-', ''), 'session_id': owner['session_id'].replace('-', ''),
                'epoch': owner['epoch'], 'generation': 1, 'operation_generation': 2, 'speech_id': speech_id,
                'action': 'begin', 'connected': True, 'ready': True, 'prepared_monotonic_ns': 0,
                'mixed_monotonic_ns': 100_000_000_000, 'output_count': 1}
            self.preparations[room] = {'speech_startup_authenticated_begin_ack': begin,
                'speech_startup_authenticated_retirement_ack': None,
                'speech_startup_identity': {'session_id': session, 'request_id': json['request_id'], 'speech_id': speech_id}}
            self.current[room] = peer
            self.broker.sessions[session] = room
            return {'sdp': f'private-answer-{room}', 'type': 'answer', 'admitted_room_id': room}
        assert action == 'close' and self.current[room].session == session
        peer = self.current[room]
        self.broker.pending_sessions.add(session)
        completion = asyncio.Future()
        async def server_close():
            # Let the actual canceled_close helper observe exact admission.
            # Canceling its client does not cancel the server operation.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.end(peer)
            completion.set_result({'ok': True})
        self.tasks.append(asyncio.create_task(server_close()))
        return await asyncio.shield(completion)

    async def check(self):
        self.checks += 1
        self.now += .02
        for room, value in self.captures.items():
            speaking = room in self.current and self.current[room].connectionState == 'connected'
            active = speaking and self.audible
            waveform = self.waveforms[room]
            if active:
                from av import AudioFrame
                tone = self.current[room].tone
                values = (600*np.sin(2*np.pi*self.freq[room]*(tone.samples+np.arange(960))/48000)).astype('<i2')
                frame = AudioFrame(format='s16', layout='mono', samples=960)
                frame.planes[0].update(values.tobytes())
                frame.sample_rate, frame.pts, frame.time_base = 48000, tone.samples, Fraction(1, 48000)
                packets, at = tone.encoder.encode(frame)
                data = tone.reference.feed(packets[0], at)
                tone.samples += 960
                waveform.push(data)
            mixed = waveform.render(pcm(value.next_frame), 960, target_gain=.2 if active else 1.)
            append(value, self.transform_pcm(room, mixed), at=self.now)
        await asyncio.sleep(0)

    async def run(self):
        try:
            await stress.exercise(self, self.broker, self.states, self.captures, self.guards,
                                  self.check, self.report, phase_change=self.phases.append)
        finally:
            await asyncio.gather(*self.tasks)


def replay_retained(artifacts):
    """Reconstruct only from held bytes/clock mapping after decoder cleanup."""
    def held(key):
        value = artifacts[key]
        raw = Path(value['path']).read_bytes()
        assert len(raw) == value['bytes'] and hashlib.sha256(raw).hexdigest() == value['sha256']
        return raw
    mapping = json.loads(held('mapping'))
    decoded = stress.reference.Waveform(mapping['session_id'], mapping['emitted_reference']['packets'],
        mapping['emitted_reference']['payload_calendar_sha256'], np.frombuffer(held('decoded_mono'), dtype='<i2'))
    period = np.frombuffer(held('carrier_mono'), dtype='<i2')
    pure = np.repeat(np.tile(period, 2)[:, None], 2, axis=1).astype('<i2').tobytes()
    carrier = stress.reference.Carrier(pure, next_frame=mapping['carrier_origin'])
    final = held('final_stereo')
    blocks = tuple(final[row['byte_offset']:row['byte_offset']+row['frames']*4] for row in mapping['buffers'])
    health = mapping['terminal_observation']
    room = mapping['guards']['source_before']['zone_id']
    terminal = stress.reference.admit_terminal(health, room, mapping['api_identity'],
        mapping['guards']['source_before'], mapping['admitted_begin'])
    directory = Path(artifacts['directory'])/'replay'
    directory.mkdir(mode=0o700)
    return stress.verify_captured_session(blocks, mapping['first_frame'], carrier, decoded,
        mapping['close_index'], mapping['baseline_music_amplitude'], (terminal, directory),
        mapping['frequency_hz'], threading.Event())


@pytest.mark.asyncio
async def test_actual_round_driver_requires_final_pcm_overlap_rejections_eof_and_admitted_cancel(monkeypatch):
    driver = Driver(monkeypatch)
    await driver.run()
    receipt = driver.report['speech_stress']
    assert receipt['passed'] is True
    assert receipt['audible_sessions'] == dict.fromkeys(driver.rooms, 4)
    assert receipt['rejected_controls'] == 22  # Eight wrong-room/competing plus six stale pairs.
    assert all(row['passed'] and all(zone['active']['passed'] and zone['idle']['passed'] and zone['closed']
                                   for zone in row['zones'].values()) for row in receipt['rounds'])
    assert sorted(zone['end_mode'] for row in receipt['rounds'] for zone in row['zones'].values()) == ['cancel']+['close']*6+['eof']
    assert sum(zone.get('cancellation', {}).get('client_canceled_after_exact_broker_admission', False)
               for row in receipt['rounds'] for zone in row['zones'].values()) == 1
    assert not driver.current and not driver.broker.sessions and not driver.broker.pending_sessions
    assert all(peer.connectionState == 'closed' and peer.tone.stopped for peer in driver.peers.values())
    assert {room: guard.reference for room, guard in driver.guards.items()} == driver.references
    assert 'private-offer-' not in repr(receipt) and 'private-answer-' not in repr(receipt)


@pytest.mark.asyncio
async def test_http_success_and_connected_peers_never_count_a_session_without_actual_final_pcm(monkeypatch):
    driver = Driver(monkeypatch, audible=False)
    with pytest.raises(stress.RuntimeFailure, match='Final PCM stress active transition'):
        await driver.run()
    receipt = driver.report['speech_stress']
    assert receipt['passed'] is False
    assert receipt['audible_sessions'] == dict.fromkeys(driver.rooms, 0)
    assert all(row['passed'] is False for row in receipt['rounds'])
    assert not driver.current and not driver.broker.sessions
    assert all(peer.connectionState == 'closed' and peer.tone.stopped for peer in driver.peers.values())
    assert {room: guard.reference for room, guard in driver.guards.items()} == driver.references


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', [0., np.pi/4, np.pi/2, 3*np.pi/4])
async def test_actual_round_driver_defers_both_counts_until_all_pcm_rejects_five_ms_peer_voice(monkeypatch, phase):
    driver = Driver(monkeypatch)
    contaminated = []
    def inject(room, data):
        rows = driver.report.get('speech_stress', {}).get('rounds', [])
        if (not contaminated and room == driver.rooms[1] and rows
                and all(zone.get('active', {}).get('passed') for zone in rows[-1]['zones'].values())):
            samples = np.frombuffer(data, dtype='<i2').reshape(-1, 2).copy()
            samples[720:] += (600*np.sin(2*np.pi*880*np.arange(240)/48000+phase)).astype('<i2')[:, None]
            contaminated.append(room)
            return samples.tobytes()
        return data
    driver.transform_pcm = inject
    with pytest.raises(stress.RuntimeFailure, match='immutable|wrong-room'):
        await driver.run()
    receipt = driver.report['speech_stress']
    assert contaminated == [driver.rooms[1]] and receipt['audible_sessions'] == dict.fromkeys(driver.rooms, 0)
    assert all(not row['passed'] for row in receipt['rounds'])
    assert all(peer.connectionState == 'closed' and peer.tone.stopped and peer.tone.reference.closed
               for peer in driver.peers.values())
    # The contaminated B survived lightweight own-audibility/music checks but
    # never the complete immutable waveform authority; A cannot count alone.
    assert receipt['pcm_guard']['failed_buffer'] is None
    assert not driver.current and not driver.broker.sessions
    failed_artifacts = receipt['rounds'][0]['zones'][driver.rooms[1]]['replay_artifacts']
    assert failed_artifacts['retained_before_analysis'] is True
    # The exact defective voice bytes remain an independently reproducible
    # rejection after both original decoders and all snapshots were cleared.
    with pytest.raises(stress.RuntimeFailure, match='immutable|wrong-room'):
        replay_retained(failed_artifacts)


def assert_retained_hashes(artifacts):
    for key in ('decoded_mono', 'final_stereo'):
        assert hashlib.sha256(Path(artifacts[key]['path']).read_bytes()).hexdigest() == artifacts[key]['sha256']


def assert_retained_success(evidence, produced):
    artifacts = evidence['replay_artifacts']
    assert artifacts['retained_before_analysis'] is True
    assert Path(artifacts['directory']).stat().st_mode & 0o777 == 0o700
    for key in ('decoded_mono', 'final_stereo', 'carrier_mono', 'mapping'):
        retained = Path(artifacts[key]['path'])
        assert retained.stat().st_mode & 0o777 == 0o400
        assert retained.stat().st_size == artifacts[key]['bytes']
        assert hashlib.sha256(retained.read_bytes()).hexdigest() == artifacts[key]['sha256']
    mapping = json.loads(Path(artifacts['mapping']['path']).read_text())
    assert mapping['emitted_reference'] == produced and mapping['emission'] == evidence['emission']
    assert mapping['buffers'][0]['first_global_frame'] == mapping['first_frame']
    assert mapping['buffers'][-1]['byte_offset']+mapping['buffers'][-1]['frames']*4 == artifacts['final_stereo']['bytes']
    assert mapping['carrier_frozen_before_offer'] is True
    assert Path(artifacts['directory'], 'onset.json').is_file()
    assert Path(artifacts['directory'], 'restore-proposal.json').is_file()
    assert json.loads(Path(artifacts['directory'], 'verification.json').read_text())['verified'] is True
    assert replay_retained(artifacts)['verified'] is True


@pytest.mark.asyncio
async def test_actual_round_driver_retires_reference_after_evidence_and_joins_before_snapshot(monkeypatch):
    driver = Driver(monkeypatch)
    await driver.run()
    receipt = driver.report['speech_stress']
    assert receipt['source_sha256'] == {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                       for path in (Path(stress.__file__), Path(stress.reference.__file__))}
    for row in receipt['rounds']:
        for room, evidence in row['zones'].items():
            produced, verified = evidence['emitted_reference'], evidence['reference_verification']
            assert verified['verified'] and verified['session_id'] == produced['session_id'] == evidence['session_id']
            assert verified['payload_calendar_sha256'] == produced['payload_calendar_sha256']
            assert verified['produced_packets'] == produced['packets'] > 0
            assert verified['audible_verified_frames'] >= 19200 and verified['total_work_limit_seconds'] == 3
            assert verified['maximum_foreign_rms'] <= 4 and verified['maximum_residual_rms'] <= 4
            assert verified['analysis_worker_joined'] is True
            assert evidence['emission']['outbound_audio_packets'] == produced['packets']
            assert room in driver.rooms
            assert evidence['authenticated_terminal'] == 'cancel'
            assert_retained_success(evidence, produced)
    assert all(peer.tone.reference.closed and not peer.tone.reference.parts and peer.tone.encoder is None
               for peer in driver.peers.values())


@pytest.mark.asyncio
async def test_exact_replay_inputs_are_retained_before_failing_analysis_and_reference_clear(monkeypatch):
    driver = Driver(monkeypatch)
    monkeypatch.setattr(stress, 'ROUNDS', 1)
    original = stress.reference.admit_restore
    entered = []
    def failing_restore(*args, **kwargs):
        original(*args, **kwargs)  # Commits one exact proposal before rejection.
        entered.append(True)
        raise stress.RuntimeFailure('Retained restore rejection')
    monkeypatch.setattr(stress.reference, 'admit_restore', failing_restore)
    with pytest.raises(stress.RuntimeFailure, match='Retained restore rejection'):
        await driver.run()
    receipt = driver.report['speech_stress']
    assert entered == [True] and receipt['audible_sessions'] == dict.fromkeys(driver.rooms, 0)
    for zone in receipt['rounds'][0]['zones'].values():
        artifacts = zone['replay_artifacts']
        assert artifacts['retained_before_analysis'] is True
        assert_retained_hashes(artifacts)
    first = receipt['rounds'][0]['zones'][driver.rooms[0]]['replay_artifacts']
    directory = Path(first['directory'])
    assert json.loads((directory/'failure.json').read_text())['message'] == 'Retained restore rejection'
    assert (directory/'onset.json').is_file() and (directory/'restore-proposal.json').is_file()
    assert all(peer.tone.reference.closed and peer.connectionState == 'closed' for peer in driver.peers.values())
    assert not driver.current and not driver.broker.sessions and receipt['reference_worker_cleanup_pending'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['decoded-file', 'mapping-file', 'missing-clock', 'stale-terminal'])
async def test_replay_retention_and_terminal_admission_failure_reject_both_counts_and_retire_peers(monkeypatch, fault):
    driver = Driver(monkeypatch)
    original = stress.retain_bytes
    def fail(directory, name, parts, limit):
        if (fault == 'decoded-file' and name == 'decoded-mono-s16.pcm'
                or fault == 'mapping-file' and name == 'mapping.json'):
            raise OSError('Exact artifact could not be retained')
        return original(directory, name, parts, limit)
    monkeypatch.setattr(stress, 'retain_bytes', fail)
    if fault == 'missing-clock':
        original_check = driver.check
        async def missing_clock():
            await original_check()
            for cap in driver.captures.values():
                cap.absolute.clear()
        monkeypatch.setattr(driver, 'check', missing_clock)
    elif fault == 'stale-terminal':
        original_health = driver.health
        async def stale_health(*args, **kwargs):
            result = await original_health(*args, **kwargs)
            ack = result.get('speech_startup_authenticated_retirement_ack')
            if ack is not None:
                result['speech_startup_authenticated_retirement_ack'] = {**ack, 'speech_id': uuid4().hex}
            return result
        monkeypatch.setattr(stress, 'call_rpc', stale_health)
    with pytest.raises((OSError, KeyError, stress.RuntimeFailure)):
        await driver.run()
    receipt = driver.report['speech_stress']
    assert receipt['passed'] is False and receipt['audible_sessions'] == dict.fromkeys(driver.rooms, 0)
    assert not driver.current and not driver.broker.sessions
    assert all(peer.tone.reference.closed and peer.connectionState == 'closed' for peer in driver.peers.values())


@pytest.mark.asyncio
async def test_failure_artifact_error_preserves_original_reference_rejection_and_cleanup(monkeypatch):
    driver = Driver(monkeypatch)
    def rejected(*args):
        raise stress.RuntimeFailure('Original immutable reference rejection')
    original = stress.retain_json
    def fail_failure_receipt(directory, name, value):
        if name == 'failure.json':
            raise OSError('Artifact retirement failure')
        return original(directory, name, value)
    monkeypatch.setattr(stress, 'verify_captured_session', rejected)
    monkeypatch.setattr(stress, 'retain_json', fail_failure_receipt)
    with pytest.raises(stress.RuntimeFailure, match='Original immutable reference rejection'):
        await driver.run()
    receipt = driver.report['speech_stress']
    assert receipt['passed'] is False and receipt['audible_sessions'] == dict.fromkeys(driver.rooms, 0)
    assert receipt['reference_worker_cleanup_pending'] == 0 and not driver.current and not driver.broker.sessions
    assert all(peer.connectionState == 'closed' and peer.tone.reference.closed for peer in driver.peers.values())


@pytest.mark.asyncio
@pytest.mark.parametrize('sent', [0, True, -1, 3])
async def test_public_sender_receipt_cannot_invent_packets_beyond_exact_produced_reference(sent):
    async def stats():
        return {'audio': SimpleNamespace(type='outbound-rtp', kind='audio', packetsSent=sent)}
    with pytest.raises(stress.RuntimeFailure, match='transmission evidence'):
        await stress.emission_evidence(SimpleNamespace(getStats=stats), SimpleNamespace(packet_count=2))


@pytest.mark.asyncio
async def test_second_zone_invalid_emission_prefix_cannot_partially_grant_first_zone_count(monkeypatch):
    driver = Driver(monkeypatch)
    original = stress.verify_captured_session
    def verify(*arguments):
        result = original(*arguments)
        if arguments[-2] == 1320:
            result['verified_received_prefix_frames'] = (arguments[3].packet_count+1)*960
        return result
    monkeypatch.setattr(stress, 'verify_captured_session', verify)
    with pytest.raises(stress.RuntimeFailure, match='exceeds exact sender'):
        await driver.run()
    assert driver.report['speech_stress']['audible_sessions'] == dict.fromkeys(driver.rooms, 0)
    assert all(peer.connectionState == 'closed' and peer.tone.reference.closed for peer in driver.peers.values())


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['cancel', 'pcm_gap', None])
async def test_real_analysis_thread_leaves_live_guard_running_and_retires_before_parent_cleanup(monkeypatch, failure):
    driver = Driver(monkeypatch)
    monkeypatch.setattr(stress, 'ROUNDS', 1)
    stress.asyncio.to_thread = asyncio.to_thread
    original_check, original_verify = driver.check, stress.verify_captured_session
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    threads, snapshots = [], []
    inject = [False]
    original_transform = driver.transform_pcm
    async def check_with_real_yield():
        await original_check()
        # Let the real analysis thread and control watcher share execution;
        # only this synthetic fixture's deliberately accelerated clock changes.
        await asyncio.sleep(.001)
    def work(*arguments):
        canceled = arguments[-1]
        snapshots.append(arguments[3])
        assert snapshots[-1].pcm.flags.writeable is False
        threads.append(threading.get_ident())
        loop.call_soon_threadsafe(entered.set)
        deadline = time.monotonic()+1
        while not release.wait(.005):
            if canceled.is_set():
                raise stress.RuntimeFailure('Exact reference worker observed parent cancellation')
            if time.monotonic() >= deadline:
                raise AssertionError('Synthetic worker was never released')
        return original_verify(*arguments)
    def transform(room, data):
        if inject[0] and room == driver.rooms[1]:
            inject[0] = False
            damaged = np.frombuffer(data, dtype='<i2').reshape(-1, 2).copy()
            damaged[576:] = 0  # Same exact8ms missing-music tail as group21.
            return damaged.tobytes()
        return original_transform(room, data)
    monkeypatch.setattr(driver, 'check', check_with_real_yield)
    monkeypatch.setattr(driver, 'transform_pcm', transform)
    monkeypatch.setattr(stress, 'verify_captured_session', work)
    operation = asyncio.create_task(driver.run())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        before = driver.checks
        await asyncio.sleep(.025)
        assert driver.checks >= before+5  # Live every-buffer watcher continued.
        assert driver.report['speech_stress']['audible_sessions'] == dict.fromkeys(driver.rooms, 0)
        if failure == 'cancel':
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(operation, 2)
        elif failure == 'pcm_gap':
            inject[0] = True
            await asyncio.sleep(.01)
            release.set()
            with pytest.raises(stress.RuntimeFailure, match='silent frames|advancing music'):
                await asyncio.wait_for(operation, 2)
        else:
            release.set()
            await asyncio.wait_for(operation, 3)
        receipt = driver.report['speech_stress']
        assert receipt['reference_worker_cleanup_pending'] == 0
        assert threads and all(identity != threading.get_ident() for identity in threads)
        assert all(peer.connectionState == 'closed' and peer.tone.reference.closed for peer in driver.peers.values())
        assert not driver.current and not driver.broker.sessions
        assert receipt['audible_sessions'] == dict.fromkeys(driver.rooms, 0 if failure else 1)
        assert receipt['passed'] is (failure is None)
        if failure == 'pcm_gap':
            assert receipt['pcm_guard']['failed_buffer'] is not None
    finally:
        release.set()
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('room_index,frequency', [(0, 1320), (1, 880), (0, 880), (1, 1320)])
async def test_preexisting_own_or_peer_tone_cannot_be_normalized_into_a_looser_stress_baseline(
        monkeypatch, room_index, frequency):
    driver = Driver(monkeypatch)
    room = driver.rooms[room_index]
    driver.captures[room].chunks[-1] = pcm(a=300 if frequency == 880 else 0,
                                         b=300 if frequency == 1320 else 0)
    with pytest.raises(stress.RuntimeFailure, match='baseline already contains'):
        await driver.run()
    receipt = driver.report['speech_stress']
    assert receipt['passed'] is False and receipt['audible_sessions'] == dict.fromkeys(driver.rooms, 0)
    assert driver.requests == [] and not driver.peers and not driver.broker.sessions
    assert {room: guard.reference for room, guard in driver.guards.items()} == driver.references


def test_contaminated_reference_cannot_raise_the_fixed_peer_voice_floor():
    guard = observer()
    guard.references['A']['voice'][1320] = 300
    assert guard.voice_limit('A', 1320) == 4
    guard.set_mode('A', 'starting')
    append(guard.captures['A'], pcm(960, a=423, b=8))
    with pytest.raises(stress.RuntimeFailure, match='Peer-zone speech'):
        guard.check()


@pytest.mark.asyncio
async def test_cleanup_api_failure_preserves_original_pcm_error_but_always_stops_and_closes_local_peers(monkeypatch):
    driver = Driver(monkeypatch, audible=False)
    original = driver.request
    async def fail_close(method, path, *, json, expected=200):
        if json['action'] == 'close' and expected == 200:
            raise OSError('Private HTTP cleanup failed')
        return await original(method, path, json=json, expected=expected)
    monkeypatch.setattr(driver, 'request', fail_close)
    with pytest.raises(stress.RuntimeFailure, match='Final PCM stress active transition'):
        await driver.run()
    receipt = driver.report['speech_stress']
    assert receipt['passed'] is False and receipt['peer_cleanup_errors'] == ['API close: OSError']*2
    assert all(peer.connectionState == 'closed' and peer.tone.stopped for peer in driver.peers.values())
    assert not driver.current and not driver.broker.sessions
    assert {room: guard.reference for room, guard in driver.guards.items()} == driver.references


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_parent', [False, True])
async def test_failed_or_canceled_paired_admission_retires_and_joins_its_exact_blocked_sibling(monkeypatch, cancel_parent):
    driver = Driver(monkeypatch)
    original = driver.peer
    blocked, never, both = asyncio.Event(), asyncio.Event(), asyncio.Event()
    tasks, retired = [], []
    def paired_peer(frequency, session_id=None):
        peer, tone = original(frequency, session_id)
        async def wait_or_fail(_offer):
            tasks.append(asyncio.current_task())
            try:
                if frequency == 1320:
                    blocked.set()
                    if len(tasks) == 2:
                        both.set()
                    await never.wait()
                else:
                    await blocked.wait()
                    if len(tasks) == 2:
                        both.set()
                    if cancel_parent:
                        await never.wait()
                    raise stress.RuntimeFailure('First exact offer refused')
            finally:
                retired.append(frequency)
        peer.setLocalDescription = wait_or_fail
        return peer, tone
    monkeypatch.setattr(stress, 'make_peer', paired_peer)
    operation = asyncio.create_task(driver.run())
    if cancel_parent:
        await asyncio.wait_for(both.wait(), 1)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(operation, 1)
    else:
        with pytest.raises(stress.RuntimeFailure, match='First exact offer refused'):
            await asyncio.wait_for(operation, 1)
    assert len(tasks) == 2 and all(task.done() for task in tasks)
    assert sorted(retired) == [880, 1320]
    assert all(peer.connectionState == 'closed' and peer.tone.stopped for peer in driver.peers.values())
    assert driver.report['speech_stress']['passed'] is False
    assert driver.report['speech_stress']['audible_sessions'] == dict.fromkeys(driver.rooms, 0)
    assert not driver.current and not driver.broker.sessions
