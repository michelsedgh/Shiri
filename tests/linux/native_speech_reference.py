"""Bounded independent Opus/native-mixer references for the manual stress test.

Import constructs no peer, socket, decoder or media resource. The opt-in stress
candidate uses this model to admit one mapping and verify each whole session;
real grouped acceptance remains a separate Linux check.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from fractions import Fraction
import hashlib
import math
import threading
import time
from uuid import UUID

import numpy as np

from shiri.runtime.system import RuntimeFailure

RATE = 48000
FRAMES = 960
REFERENCE_BYTES = 8*1024*1024
MAX_PACKET_BYTES = 2048
PERIOD = 1200  # Exact sample period of the declared440Hz/48k native fixture.
MAX_CARRIER_FRAMES = PERIOD*4
FOREIGN_LIMIT = 4.
RESIDUAL_RMS_LIMIT = 4.
MAX_ONSET_FRAMES = RATE//4
MAX_DUCK_SEARCH = 4096
MAX_VOICE_SEARCH = 8192
ADMISSION_SECONDS = 1.
MAX_RESTORE_FRAMES = RATE*3//4
MAX_SESSION_CAPTURE_BYTES = 8*1024*1024


def require(value, message):
    if not value:
        raise RuntimeFailure(message)


class DecodedReference:
    """Decode the exact outgoing packet sequence, continuously, once/session."""
    def __init__(self, session_id):
        from aiortc.codecs.opus import OpusDecoder
        from av import AudioResampler
        require(type(session_id) is str and str(UUID(session_id)) == session_id, 'Reference session must be a canonical UUID')
        self.session_id = session_id
        self.decoder = OpusDecoder()  # Explicit libopus/s16/stereo/48k, same receiver decoder.
        self.resampler = AudioResampler(format='s16', layout='mono', rate=RATE)
        self.parts, self.total, self.next_pts = bytearray(REFERENCE_BYTES), 0, 0
        self.lock = threading.RLock()
        self.payload_hash = hashlib.sha256()
        self.error, self.closed = None, False
        self.packet_count = 0

    def feed(self, payload, pts):
        with self.lock:
            return self._feed(payload, pts)

    def _feed(self, payload, pts):
        from aiortc.jitterbuffer import JitterFrame
        require(self.error is None and not self.closed, 'Decoded reference is permanently fenced')
        try:
            require(type(payload) is bytes and 0 < len(payload) <= MAX_PACKET_BYTES, 'Reference Opus packet size is invalid')
            require(type(pts) is int and pts == self.next_pts and pts <= 2**63-1-FRAMES,
                    'Reference Opus packet was dropped, repeated or reordered')
            decoded = self.decoder.decode(JitterFrame(payload, pts))
            require(len(decoded) == 1 and decoded[0].samples == FRAMES and decoded[0].sample_rate == RATE,
                    'Reference decoder changed its fixed20ms/48k contract')
            converted = self.resampler.resample(decoded[0])
            require(len(converted) == 1 and converted[0].samples == FRAMES
                    and converted[0].format.name == 's16' and converted[0].layout.name == 'mono',
                    'Reference mono resampler changed its PCM contract')
            data = bytes(converted[0].planes[0])[:FRAMES*2]
            require(self.total+len(data) <= REFERENCE_BYTES, 'Decoded reference exceeded its8MiB per-active-peer bound')
            self.parts[self.total:self.total+len(data)] = data
            self.total += len(data)
            self.packet_count += 1
            self.next_pts += FRAMES
            self.payload_hash.update(pts.to_bytes(8, 'big')+len(payload).to_bytes(2, 'big')+payload)
            return data
        except BaseException as exc:
            self.error = type(exc).__name__
            raise

    def snapshot(self):
        with self.lock:
            require(self.error is None and not self.closed and self.packet_count > 0,
                    'Cannot admit a failed or cleared reference')
            # One immutable copy per pair, never b''.join/copy per PCM buffer.
            # Preallocated storage avoids bytearray capacity/temporary joins.
            pcm = np.frombuffer(bytes(memoryview(self.parts)[:self.total]), dtype='<i2')
            return Waveform(self.session_id, self.packet_count, self.payload_hash.hexdigest(), pcm)

    def clear(self):
        with self.lock:
            self.parts.clear()
            self.decoder = self.resampler = None
            self.total = 0
            self.closed = True

    def evidence(self):
        return {'session_id': self.session_id, 'decoder': 'libopus,s16,stereo,48000; continuous',
                'resampler': 's16,mono,48000; continuous', 'packets': self.packet_count,
                'decoded_bytes': self.total, 'storage_byte_limit': REFERENCE_BYTES,
                'snapshot_byte_limit': REFERENCE_BYTES, 'peak_pcm_reference_bytes': REFERENCE_BYTES*2,
                'payload_calendar_sha256': self.payload_hash.hexdigest(), 'error': self.error,
                'scope': 'Exact produced public AVPacket payloads; arrival and audible prefix require independent verification'}


@dataclass(frozen=True)
class Waveform:
    session_id: str
    packet_count: int
    payload_sha256: str
    pcm: np.ndarray

    def __len__(self):
        return len(self.pcm)

    def __getitem__(self, key):
        return self.pcm[key]

    def tobytes(self):
        return self.pcm.tobytes()


def packet_track(frequency, session_id):
    """Publish exactly the existing sine PCM through aiortc's AVPacket path."""
    from aiortc import AudioStreamTrack
    from aiortc.codecs.opus import OpusEncoder
    from av import AudioFrame, Packet
    require(frequency in {880, 1320}, 'Undeclared reference speech marker')
    class PacketTone(AudioStreamTrack):
        def __init__(self):
            super().__init__()
            self.reference = DecodedReference(session_id)
            self.encoder = OpusEncoder()
            self.samples, self.started = 0, None
            self.error = None
            self.receiving = False

        async def recv(self):
            require(self.error is None and self.readyState == 'live' and not self.receiving,
                    'Reference publisher is stopped, failed or concurrently read')
            self.receiving = True
            try:
                if self.started is None:
                    self.started = time.monotonic()
                await asyncio.sleep(max(0, self.started+self.samples/RATE-time.monotonic()))
                values = (600*np.sin(2*np.pi*frequency*(self.samples+np.arange(FRAMES))/RATE)).astype('<i2')
                frame = AudioFrame(format='s16', layout='mono', samples=FRAMES)
                frame.planes[0].update(values.tobytes())
                frame.sample_rate, frame.pts, frame.time_base = RATE, self.samples, Fraction(1, RATE)
                # No private sender hook or second encode: RTCRtpSender packs
                # this exact public AVPacket into its audio RTP payload.
                payloads, pts = await asyncio.to_thread(self.encoder.encode, frame)
                require(len(payloads) == 1 and pts == self.samples, 'Reference publisher lost its fixed packet calendar')
                self.reference.feed(payloads[0], pts)
                packet = Packet(payloads[0])
                packet.pts, packet.time_base = pts, Fraction(1, RATE)
                self.samples += FRAMES
                return packet
            except BaseException as exc:
                self.error = type(exc).__name__
                self.stop()
                raise
            finally:
                self.receiving = False
    return PacketTone()


@dataclass(frozen=True)
class Alignment:
    """One immutable sample mapping for this exact negotiation incarnation."""
    session_id: str
    start_frame: int
    stop_frame: int | None = None

    def __post_init__(self):
        require(type(self.session_id) is str and str(UUID(self.session_id)) == self.session_id,
                'Wave alignment requires an exact canonical session UUID')
        require(type(self.start_frame) is int and 0 <= self.start_frame <= 2**63-1,
                'Wave alignment start frame is invalid')
        require(self.stop_frame is None or type(self.stop_frame) is int
                and self.start_frame <= self.stop_frame <= 2**63-1, 'Wave alignment stop frame is invalid')


@dataclass(frozen=True)
class GainPlan:
    duck_frame: int
    restore_frame: int | None = None
    target: float = .2

    def __post_init__(self):
        require(type(self.duck_frame) is int and 0 <= self.duck_frame <= 2**63-1, 'Native duck frame is invalid')
        require(self.restore_frame is None or type(self.restore_frame) is int
                and self.duck_frame <= self.restore_frame <= 2**63-1, 'Native restore frame is invalid')
        require(type(self.target) is float and math.isfinite(self.target) and self.target == .2,
                'Reference authority is the declared.2 duck fixture only')

    def values(self, frames):
        gain = np.clip(1-(frames-self.duck_frame+1)/1920, self.target, 1.)
        if self.restore_frame is not None:
            before = max(self.target, min(1., 1-(self.restore_frame-self.duck_frame)/1920))
            restored = np.clip(before+(frames-self.restore_frame+1)/12000, self.target, 1.)
            gain = np.where(frames >= self.restore_frame, restored, gain)
        return gain


@dataclass(frozen=True)
class Terminal:
    """An exact worker-authenticated terminal for one admitted voice owner."""
    action: str
    session_id: str
    speech_id: str
    scope: tuple

    def __post_init__(self):
        require(self.action in {'cancel', 'finish'} and canonical_uuid(self.session_id),
                'Reference terminal requires a canonical voice session')
        keys = ('room_id', 'launch_generation', 'incarnation', 'session_id',
                'generation', 'epoch', 'operation_generation', 'speech_id')
        require(type(self.scope) is tuple and len(self.scope) == len(keys)
                and tuple(key for key, _ in self.scope) == keys
                and dict(self.scope)['speech_id'] == self.speech_id,
                'Reference terminal lacks its exact native scope')


def canonical_uuid(value):
    try:
        return type(value) is str and len(value) == 36 and str(UUID(value)) == value
    except ValueError:
        return False


def admit_terminal(health, room, identity, owner, admitted_begin):
    """Require the real native echo; API close success/EOF labels grant nothing."""
    require(type(health) is dict and type(owner) is dict and canonical_uuid(room)
            and canonical_uuid(owner.get('incarnation')) and canonical_uuid(owner.get('session_id')),
            'Stress terminal lacks its canonical source/room scope')
    scope = ('room_id', 'launch_generation', 'incarnation', 'session_id',
             'generation', 'epoch', 'operation_generation', 'speech_id')
    counters = ('prepared_monotonic_ns', 'mixed_monotonic_ns', 'output_count')
    fields = set(scope) | set(counters) | {'action', 'connected', 'ready'}
    begin = health.get('speech_startup_authenticated_begin_ack')
    terminal = health.get('speech_startup_authenticated_retirement_ack')
    api_identity = health.get('speech_startup_identity')
    require(type(identity) is dict and set(identity) == {'session_id', 'request_id'}
            and all(canonical_uuid(value) for value in identity.values()),
            'Stress terminal lacks its canonical API negotiation')
    require(type(begin) is dict and type(terminal) is dict and set(begin) == set(terminal) == fields,
            'Stress terminal lacks its exact authenticated native schema')
    require(begin == admitted_begin and begin['action'] == 'begin'
            and begin['connected'] is True and begin['ready'] is True,
            'Stress terminal changed its admitted native BEGIN')
    require(terminal['action'] in {'cancel', 'finish'} and terminal['connected'] is False
            and terminal['ready'] is False and all(type(terminal[key]) is int and terminal[key] == 0 for key in counters),
            'Stress terminal is not a definitive retired CANCEL/FINISH')
    require(all(type(begin[key]) is int and 0 <= begin[key] <= 2**63-1 for key in counters)
            and begin['mixed_monotonic_ns'] > 0 and begin['output_count'] > 0,
            'Stress BEGIN lacks its exact observed readiness')
    require(all(type(begin[key]) is int and 0 < begin[key] <= 2**63-1
                for key in ('generation', 'epoch', 'operation_generation')),
            'Stress terminal has invalid native generations')
    for key in ('room_id', 'launch_generation', 'incarnation', 'session_id', 'speech_id'):
        value = begin[key]
        require(type(value) is str and len(value) == 32 and all(char in '0123456789abcdef' for char in value)
                and int(value, 16) > 0, 'Stress terminal has a noncanonical native identity')
    require(all(type(terminal[key]) is type(begin[key]) and terminal[key] == begin[key] for key in scope),
            'Stress terminal changed its exact native request scope')
    require(type(api_identity) is dict and set(api_identity) == {'session_id', 'request_id', 'speech_id'}
            and api_identity == {**identity, 'speech_id': begin['speech_id']},
            'Stress terminal changed its API speech incarnation')
    require(health.get('source', {}).get('owner') == owner and owner.get('zone_id') == room
            and begin['room_id'] == UUID(room).hex and begin['incarnation'] == UUID(owner['incarnation']).hex
            and begin['session_id'] == UUID(owner['session_id']).hex
            and type(owner['epoch']) is int and begin['epoch'] == owner['epoch'],
            'Stress terminal changed its current music/source owner')
    require(health.get('speech_session_id') is None and health.get('speech_cleanup_pending') == 0
            and health.get('speech_ready') is True and not health.get('speech_cleanup_error'),
            'Stress terminal has not retired exact voice ownership')
    return Terminal(terminal['action'], identity['session_id'], begin['speech_id'],
                    tuple((key, begin[key]) for key in scope))


class Carrier:
    """Learn only a pure, repeated fixture period; never adapt to speech."""
    def __init__(self, pure_pcm, *, next_frame):
        require(type(next_frame) is int and next_frame >= 0, 'Carrier frame origin is invalid')
        require(type(pure_pcm) is bytes and PERIOD*2*4 <= len(pure_pcm) <= MAX_CARRIER_FRAMES*4 and len(pure_pcm) % 4 == 0,
                'Carrier admission needs two whole observed native periods')
        samples = np.frombuffer(pure_pcm, dtype='<i2').reshape(-1, 2)
        require(np.max(np.abs(samples[:, 0].astype(int)-samples[:, 1])) <= 3, 'Carrier stereo channels differ')
        a, b = samples[-PERIOD*2:-PERIOD, 0], samples[-PERIOD:, 0]
        require(np.max(np.abs(a.astype(int)-b)) <= 3, 'Native fixture period changed before reference admission')
        # The three declared carriers all repeat after1200 samples. Repetition
        # alone must never enroll a contaminated baseline as authority.
        times = np.arange(PERIOD)/RATE
        amplitudes = {frequency: float(2*abs(np.mean(b*np.exp(-2j*np.pi*frequency*times))))
                      for frequency in (440, 880, 1320)}
        require(amplitudes[440] > 1000 and amplitudes[880] <= FOREIGN_LIMIT
                and amplitudes[1320] <= FOREIGN_LIMIT, 'Carrier reference contains speech or lacks pure music')
        self.period, self.origin = b.copy(), next_frame
        self.period.setflags(write=False)

    def values(self, frames):
        return self.period[(frames-self.origin) % PERIOD].astype(float)


def verify_block(data, first_frame, carrier, decoded, alignment, gain, foreign_frequency, measure):
    """Verify a whole captured buffer against the fixed reference mapping.

    Alignment/gain are admitted once, outside this function. This function has
    no fit, offset correction, free gain or authority to mutate either mapping.
    """
    require(type(data) is bytes and len(data) % 4 == 0 and 480 <= len(data)//4 <= FRAMES,
            'Reference verification requires a complete10..20ms captured buffer')
    require(type(first_frame) is int and 0 <= first_frame <= 2**63-1-FRAMES, 'Reference buffer frame is invalid')
    require(foreign_frequency in {880, 1320}, 'Reference foreign marker is undeclared')
    require(isinstance(decoded, Waveform) and decoded.session_id == alignment.session_id
            and decoded.pcm.dtype == np.dtype('<i2') and decoded.pcm.ndim == 1
            and not decoded.pcm.flags.writeable and decoded.pcm.nbytes <= REFERENCE_BYTES,
            'Reference PCM exceeds its exact signed-mono or memory contract')
    samples = np.frombuffer(data, dtype='<i2').reshape(-1, 2).astype(float)
    require(np.max(np.abs(samples[:, 0]-samples[:, 1])) <= 3, 'Reference final stereo channels differ')
    frames = np.arange(first_frame, first_frame+len(samples), dtype=np.int64)
    speech_index = frames-alignment.start_frame
    active = (speech_index >= 0) & (speech_index < len(decoded))
    if alignment.stop_frame is not None:
        active &= frames < alignment.stop_frame
    voice = np.zeros(len(samples))
    voice[active] = decoded[speech_index[active]]
    expected = np.clip(np.rint(carrier.values(frames)*gain.values(frames))+voice, -32768, 32767)
    residual = samples[:, 0]-expected
    rms = float(np.sqrt(np.mean(residual**2)))
    require(rms <= RESIDUAL_RMS_LIMIT, 'Final PCM differs from the immutable native/music/own-Opus reference')
    # Harmonic analysis receives the complete signed residual, not a clipped
    # partial buffer or a free nuisance curve fitted to the observed speech.
    residual_pcm = np.repeat(residual[:, None], 2, axis=1).astype('<i2').tobytes()
    foreign = measure(residual_pcm)['voice'][foreign_frequency]
    require(foreign <= FOREIGN_LIMIT, 'Reference detected wrong-room speech in final PCM')
    return {'first_frame': first_frame, 'frames': len(samples), 'foreign_rms': foreign, 'residual_rms': rms,
            'foreign_limit': FOREIGN_LIMIT, 'residual_limit': RESIDUAL_RMS_LIMIT,
            'session_id': alignment.session_id, 'alignment_start_frame': alignment.start_frame,
            'alignment_stop_frame': alignment.stop_frame}


def admit_onset(data, first_frame, carrier, decoded, session_id, duck_bounds, voice_bounds,
                foreign_frequency, measure, *, canceled=None):
    """Choose one exact sample mapping from a bounded initial observation.

    Only two integers are admitted: native duck start and continuous reference
    start. Gain, phase, decoded samples, carrier, cadence and level are fixed.
    Three seeds and four rounds of integer least-squares search are a bounded
    proposal mechanism; the complete observation must then match that one
    mapping. No later buffer can refit it. A fit cannot grant session evidence.
    Call this in a bounded worker thread while the live PCM guard continues.
    """
    started = time.monotonic()
    def budget():
        require(canceled is None or not canceled.is_set(), 'Reference admission was canceled')
        require(time.monotonic()-started <= ADMISSION_SECONDS, 'Reference admission exceeded its1s work budget')
    budget()
    require(type(data) is bytes and len(data) % 4 == 0 and 1920 <= len(data)//4 <= MAX_ONSET_FRAMES,
            'Reference onset observation exceeds its40..250ms bound')
    require(type(first_frame) is int and 0 <= first_frame <= 2**63-1-MAX_ONSET_FRAMES,
            'Reference onset frame origin is invalid')
    require(isinstance(decoded, Waveform) and decoded.session_id == session_id
            and decoded.pcm.dtype == np.dtype('<i2') and decoded.pcm.ndim == 1 and not decoded.pcm.flags.writeable
            and 1920 <= len(decoded) and decoded.pcm.nbytes <= REFERENCE_BYTES, 'Reference onset PCM is invalid')
    for values, limit in ((duck_bounds, MAX_DUCK_SEARCH), (voice_bounds, MAX_VOICE_SEARCH)):
        require(type(values) is tuple and len(values) == 2 and all(type(item) is int for item in values)
                and first_frame <= values[0] <= values[1] <= first_frame+len(data)//4-960
                and values[1]-values[0] <= limit, 'Reference onset search exceeded its declared integer bounds')
    # A UUID/instance cannot be silently replaced while admitting alignment.
    Alignment(session_id, voice_bounds[0])
    observed = np.frombuffer(data, dtype='<i2').reshape(-1, 2)
    require(np.max(np.abs(observed[:, 0].astype(int)-observed[:, 1])) <= 3, 'Reference onset stereo channels differ')
    samples = observed[:, 0].astype(float)
    frames = np.arange(first_frame, first_frame+len(samples), dtype=np.int64)
    music = carrier.values(frames)
    voice = decoded[:len(samples)].astype(float)
    size = 1 << (len(samples)+len(voice)-1).bit_length()
    require(size <= 32768, 'Reference onset FFT exceeded its fixed32768-point bound')
    voice_fft = np.fft.rfft(voice[::-1], n=size)
    voice_power = np.r_[0., np.cumsum(voice**2)]
    candidates = np.arange(duck_bounds[0], duck_bounds[1]+1, dtype=np.int64)
    local_candidates = candidates-first_frame

    def own_at(start):
        result = np.zeros(len(samples))
        count = min(len(voice), len(samples)-(start-first_frame))
        result[start-first_frame:start-first_frame+count] = voice[:count]
        return result

    def choose_voice(duck):
        budget()
        low = max(voice_bounds[0], duck-1920)
        high = min(voice_bounds[1], duck+1920)
        if low > high:
            return None
        # Exact shift costs, with fixed amplitude. Independent source-calendar
        # bounds are mandatory; no arbitrary received-buffer clock is invented.
        choices = np.arange(low, high+1, dtype=np.int64)
        local = choices-first_frame
        residual = samples-music*GainPlan(int(duck)).values(frames)
        correlation = np.fft.irfft(np.fft.rfft(residual, n=size)*voice_fft, n=size)
        cost = voice_power[np.minimum(len(voice), len(samples)-local)]-2*correlation[len(voice)-1+local]
        return int(choices[np.argmin(cost)])

    def choose_duck(start):
        budget()
        # Prefix sums evaluate EVERY integer duck start without a candidate ×
        # samples allocation. The ramp law is the real native full-scale step.
        residual = samples-own_at(start)
        a = residual-music+music*(np.arange(len(samples))+1)/1920
        b = music/1920
        before = np.r_[0., np.cumsum((residual-music)**2)]
        after = np.r_[0., np.cumsum((residual-.2*music)**2)]
        a2, ab, b2 = (np.r_[0., np.cumsum(value)] for value in (a*a, a*b, b*b))
        left = local_candidates
        right = np.minimum(len(samples), left+1536)
        cost = (before[left]+after[-1]-after[right]+a2[right]-a2[left]
                -2*left*(ab[right]-ab[left])+left**2*(b2[right]-b2[left]))
        valid = np.abs(candidates-start) <= 1920
        require(np.any(valid), 'Reference speech/duck alignment exceeded two native packets')
        cost[~valid] = np.inf
        return int(candidates[np.argmin(cost)])

    proposals = []
    seeds = (duck_bounds[0], sum(duck_bounds)//2, duck_bounds[1])
    for duck in seeds:
        start = None
        for _ in range(4):
            start = choose_voice(duck)
            if start is None:
                break
            duck = choose_duck(start)
        if start is not None:
            start = choose_voice(duck)
            if start is not None:
                gain = GainPlan(duck)
                predicted = np.rint(music*gain.values(frames))+own_at(start)
                cost = float(np.mean((samples-predicted)**2))
                proposals.append((cost, duck, start))
    require(bool(proposals), 'Reference onset has no bounded source-calendar proposal')
    _, duck, start = min(proposals)
    alignment, gain = Alignment(session_id, start), GainPlan(duck)
    # Whole-window verification follows proposal selection; the optimizer's
    # score is never acceptance. Chunking remains complete10..20ms buffers.
    require(len(samples) % FRAMES == 0, 'Reference onset requires complete native20ms blocks')
    receipts = []
    for index in range(0, len(samples), FRAMES):
        budget()
        receipts.append(verify_block(data[index*4:(index+FRAMES)*4], first_frame+index, carrier,
                                    decoded, alignment, gain, foreign_frequency, measure))
    budget()
    return alignment, gain, {'session_id': session_id, 'alignment_start_frame': start, 'duck_frame': duck,
        'checked_frames': len(samples), 'work_seconds': time.monotonic()-started,
        'work_limit_seconds': ADMISSION_SECONDS, 'maximum_fft_points': 32768,
        'duck_integer_candidates': len(candidates), 'maximum_voice_integer_candidates': voice_bounds[1]-voice_bounds[0]+1,
        'maximum_rounds': 15, 'foreign_limit': FOREIGN_LIMIT,
        'maximum_foreign_rms': max(row['foreign_rms'] for row in receipts),
        'maximum_residual_rms': max(row['residual_rms'] for row in receipts)}


def admit_restore(data, first_frame, carrier, decoded, alignment, gain, restore_bounds,
                  foreign_frequency, measure, *, canceled=None, terminal=None, proposed=None):
    """Admit one bounded endpoint/restore law without re-aligning the source.

    Natural FINISH keeps complete decoded20ms endpoints and its entire queued
    tail. An authenticated CANCEL removes the queue between player reads: one
    sample cutoff must also be the gain restoration start. Neither branch
    admits a phase/gain/offset refit of the existing reference.
    """
    started = time.monotonic()
    def budget():
        require(canceled is None or not canceled.is_set(), 'Reference restoration admission was canceled')
        require(time.monotonic()-started <= ADMISSION_SECONDS, 'Reference restoration exceeded its1s work budget')
    budget()
    require(type(data) is bytes and len(data) % (FRAMES*4) == 0
            and 9600 <= len(data)//4 <= MAX_RESTORE_FRAMES, 'Reference restore observation exceeds its200..750ms bound')
    require(type(first_frame) is int and gain.restore_frame is None and alignment.stop_frame is None
            and first_frame >= gain.duck_frame+1536 and first_frame <= 2**63-1-MAX_RESTORE_FRAMES,
            'Reference restore does not continue the admitted fully-ducked session')
    require(isinstance(decoded, Waveform) and decoded.session_id == alignment.session_id
            and len(decoded) == decoded.packet_count*FRAMES and decoded.pcm.nbytes <= REFERENCE_BYTES
            and not decoded.pcm.flags.writeable, 'Reference restore changed its session or packet calendar')
    require(type(restore_bounds) is tuple and len(restore_bounds) == 2
            and all(type(item) is int for item in restore_bounds)
            and first_frame <= restore_bounds[0] <= restore_bounds[1] <= first_frame+len(data)//4-9600,
            'Reference restore bounds cannot contain a complete native gain restoration')
    frames = np.arange(first_frame, first_frame+len(data)//4, dtype=np.int64)
    observed = np.frombuffer(data, dtype='<i2').reshape(-1, 2)
    require(np.max(np.abs(observed[:, 0].astype(int)-observed[:, 1])) <= 3, 'Reference restore stereo channels differ')
    samples, music = observed[:, 0].astype(float), carrier.values(frames)
    candidates = np.arange(restore_bounds[0], restore_bounds[1]+1, dtype=np.int64)
    left = candidates-first_frame
    right = np.minimum(len(samples), left+9600)
    require(terminal is None or isinstance(terminal, Terminal)
            and terminal.action in {'cancel', 'finish'} and terminal.session_id == decoded.session_id,
            'Reference restore lacks its exact admitted voice terminal')
    if terminal is not None and terminal.action == 'cancel':
        candidates = candidates[(candidates > alignment.start_frame)
                                & (candidates <= alignment.start_frame+len(decoded))]
        require(0 < len(candidates) <= MAX_RESTORE_FRAMES,
                'Explicit CANCEL cutoff exceeds its bounded produced voice prefix')
        left, right = candidates-first_frame, candidates-first_frame+9600
        index = frames-alignment.start_frame
        voice = np.zeros(len(samples))
        active = (index >= 0) & (index < len(decoded))
        voice[active] = decoded[index[active]]
        # Evaluate every integer cutoff with prefix sums. Voice removal and
        # the original1/12000 restoration step share ONE endpoint. This cost
        # only proposes a mapping; every original whole-buffer guard follows.
        a = samples-.2*music-music*(np.arange(len(samples))+1)/12000
        b = music/12000
        before = np.r_[0., np.cumsum((samples-voice-.2*music)**2)]
        after = np.r_[0., np.cumsum((samples-music)**2)]
        a2, ab, b2 = (np.r_[0., np.cumsum(value)] for value in (a*a, a*b, b*b))
        cost = (before[left]+after[-1]-after[right]+a2[right]-a2[left]
                +2*left*(ab[right]-ab[left])+left**2*(b2[right]-b2[left]))
        cutoff = int(candidates[np.argmin(cost)])
        ended, restored = Alignment(alignment.session_id, alignment.start_frame, cutoff), GainPlan(gain.duck_frame, cutoff)
        proposal = {'terminal': 'cancel', 'cutoff_integer_candidates': len(candidates)}
    else:
        ended, restored, proposal = _whole_packet_restore(samples, music, frames, candidates, left, right,
            first_frame, decoded, alignment, gain, budget)
    if proposed is not None:
        proposed({'session_id': ended.session_id, 'alignment_start_frame': ended.start_frame,
                  'alignment_stop_frame': ended.stop_frame, 'duck_frame': restored.duck_frame,
                  'restore_frame': restored.restore_frame, **proposal})
    receipts = []
    for index in range(0, len(samples), FRAMES):
        budget()
        receipts.append(verify_block(data[index*4:(index+FRAMES)*4], first_frame+index, carrier,
                                    decoded, ended, restored, foreign_frequency, measure))
    budget()
    return ended, restored, {'session_id': ended.session_id, 'alignment_start_frame': ended.start_frame,
        'alignment_stop_frame': ended.stop_frame, 'restore_frame': restored.restore_frame,
        'checked_frames': len(samples), 'work_seconds': time.monotonic()-started,
        'work_limit_seconds': ADMISSION_SECONDS, **proposal,
        'restore_integer_candidates': len(candidates), 'maximum_foreign_rms': max(row['foreign_rms'] for row in receipts),
        'maximum_residual_rms': max(row['residual_rms'] for row in receipts)}


def _whole_packet_restore(samples, music, frames, candidates, left, right, first_frame,
                          decoded, alignment, gain, budget):
    """Original whole-packet proposal search; natural tails keep this boundary."""
    low_packet = max(1, (first_frame-alignment.start_frame+FRAMES-1)//FRAMES)
    high_packet = min(decoded.packet_count, (first_frame+len(samples)-alignment.start_frame)//FRAMES)
    require(0 <= high_packet-low_packet < 39, 'Reference restore endpoint search exceeds39 complete packet prefixes')
    proposals = []
    for count in range(low_packet, high_packet+1):
        budget()
        cutoff = alignment.start_frame+count*FRAMES
        valid = (candidates-cutoff <= 13920) & (cutoff-candidates <= 9600)
        if not np.any(valid):
            continue
        voice = np.zeros(len(samples))
        index = frames-alignment.start_frame
        active = (index >= 0) & (frames < cutoff)
        voice[active] = decoded[index[active]]
        residual = samples-voice
        a = residual-.2*music-music*(np.arange(len(samples))+1)/12000
        b = music/12000
        before = np.r_[0., np.cumsum((residual-.2*music)**2)]
        after = np.r_[0., np.cumsum((residual-music)**2)]
        a2, ab, b2 = (np.r_[0., np.cumsum(value)] for value in (a*a, a*b, b*b))
        cost = (before[left]+after[-1]-after[right]+a2[right]-a2[left]
                +2*left*(ab[right]-ab[left])+left**2*(b2[right]-b2[left]))
        cost[~valid] = np.inf
        restore = int(candidates[np.argmin(cost)])
        ended = Alignment(alignment.session_id, alignment.start_frame, cutoff)
        restored = GainPlan(gain.duck_frame, restore)
        exact = np.clip(np.rint(music*restored.values(frames))+voice, -32768, 32767)
        proposals.append((float(np.mean((samples-exact)**2)), ended, restored))
    require(bool(proposals), 'Reference restore has no bounded whole-packet endpoint proposal')
    _, ended, restored = min(proposals, key=lambda value: value[0])
    return ended, restored, {'terminal': 'finish_or_legacy_whole_packet',
                            'whole_packet_candidates': high_packet-low_packet+1}


def verify_session(blocks, first_frame, carrier, decoded, alignment, gain, foreign_frequency,
                   measure, *, canceled=None):
    """Authoritative complete-session/tail proof, never a fit or HTTP count.

    Caller freezes a tuple of existing immutable capture buffers after polling
    the live sequence guard. No PCM join or reference copy occurs here. An
    interruption anywhere, including after a qualifying voice prefix, refuses
    evidence. This function creates no peer and grants no production state.
    """
    require(type(blocks) is tuple and 1 <= len(blocks) <= MAX_SESSION_CAPTURE_BYTES//1920,
            'Reference session capture exceeded its fixed buffer bound')
    require(all(type(data) is bytes for data in blocks)
            and sum(map(len, blocks)) <= MAX_SESSION_CAPTURE_BYTES, 'Reference session capture exceeds8MiB')
    require(alignment.stop_frame is not None and gain.restore_frame is not None,
            'Reference session lacks its admitted complete close tail')
    started, at, audible, maximum_foreign, maximum_residual = time.monotonic(), first_frame, 0, 0., 0.
    for data in blocks:
        require(canceled is None or not canceled.is_set(), 'Complete reference verification was canceled')
        require(time.monotonic()-started <= ADMISSION_SECONDS, 'Complete reference verification exceeded its1s work budget')
        result = verify_block(data, at, carrier, decoded, alignment, gain, foreign_frequency, measure)
        frames = np.arange(at, at+result['frames'], dtype=np.int64)
        voice = np.zeros(len(frames))
        active = (frames >= alignment.start_frame) & (frames < alignment.stop_frame)
        index = frames[active]-alignment.start_frame
        require(not len(index) or index[-1] < len(decoded), 'Observed reference outlasted its actual packet prefix')
        voice[active] = decoded[index]
        if float(np.sqrt(np.mean(voice**2))) > 65 and np.max(gain.values(frames)) <= .23:
            audible += len(frames)
        maximum_foreign = max(maximum_foreign, result['foreign_rms'])
        maximum_residual = max(maximum_residual, result['residual_rms'])
        at += len(frames)
    require(time.monotonic()-started <= ADMISSION_SECONDS, 'Complete reference verification exceeded its1s work budget')
    require(first_frame < gain.duck_frame and at >= gain.restore_frame+9600+FRAMES,
            'Reference evidence omitted the initial music or complete restored buffered tail')
    require(audible >= RATE*2//5, 'Reference session lacks400ms of actually verified ducked audible speech')
    return {'verified': True, 'session_id': alignment.session_id,
        'payload_calendar_sha256': decoded.payload_sha256, 'produced_packets': decoded.packet_count,
        'verified_received_prefix_frames': alignment.stop_frame-alignment.start_frame,
        'verified_frames': at-first_frame, 'audible_verified_frames': audible,
        'alignment_start_frame': alignment.start_frame, 'alignment_stop_frame': alignment.stop_frame,
        'duck_frame': gain.duck_frame, 'restore_frame': gain.restore_frame,
        'maximum_foreign_rms': maximum_foreign, 'maximum_residual_rms': maximum_residual,
        'foreign_limit': FOREIGN_LIMIT, 'residual_limit': RESIDUAL_RMS_LIMIT,
        'capture_byte_limit': MAX_SESSION_CAPTURE_BYTES, 'work_limit_seconds': ADMISSION_SECONDS,
        'work_seconds': time.monotonic()-started}
