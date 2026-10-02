"""Finite, nonrepeating emitted-Opus completeness diagnostic.

Import opens no media/network/device. The optional Linux entry point reuses an
already admitted rootless API and its original final-PCM/epoch observers. This
is separate from the immutable nine-row marker matrix and its pending labels.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from fractions import Fraction
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import time
from uuid import UUID, uuid4

import numpy as np

from shiri.rpc import call_rpc
from shiri.runtime.system import RuntimeFailure

RATE, FRAMES = 48000, 960
BODY_FRAMES = 60*FRAMES
CODEC_TAIL_FRAMES = 5*FRAMES
QUIET_FRAMES = 10*FRAMES
MAX_CAPTURE_FRAMES = RATE*14
WINDOW_CORRELATION = .97
WINDOW_ERROR_FRACTION = .16
LEVEL_BOUNDS = (.85, 1.15)
COLLECTION_SECONDS = 12


def require(value, message):
    if not value:
        raise RuntimeFailure(message)


def reference_module():
    name = 'shiri_finite_existing_reference'
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name('native_speech_reference.py'))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def quiet_module():
    name = 'shiri_finite_declared_carrier'
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name('native_finite_quiet.py'))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def waveform(*, body_frames=BODY_FRAMES, seed=1):
    """Unique chirps/envelope; no repeated tone can hide an erased opening."""
    require(type(body_frames) is int and 8*FRAMES <= body_frames <= 100*FRAMES and body_frames % FRAMES == 0,
            'Finite utterance has an undeclared source-frame bound')
    require(type(seed) is int and 0 < seed < 2**32, 'Finite signal requires an explicit deterministic seed')
    t = np.arange(body_frames, dtype=float)/RATE
    duration = body_frames/RATE
    rng = np.random.default_rng(seed)
    levels = rng.uniform(.75, 1., math.ceil(duration/.04)+1)
    envelope = np.interp(t, np.arange(len(levels))*.04, levels)
    phase1 = 2*np.pi*((710+seed % 71)*t+1700*t*t/(2*duration))
    phase2 = 2*np.pi*((3310-seed % 97)*t-1500*t*t/(2*duration))
    result = (1500*envelope*(.72*np.sin(phase1)+.28*np.sin(phase2))).astype('<i2')
    result.flags.writeable = False
    return result


def packet_track(session_id, *, body_frames=BODY_FRAMES, seed=1):
    """Encode once; public AVPackets are exactly the independent decode input."""
    from aiortc import AudioStreamTrack
    from aiortc.codecs.opus import OpusEncoder
    from av import AudioFrame, Packet
    reference = reference_module()
    source = waveform(body_frames=body_frames, seed=seed)

    class FiniteTrack(AudioStreamTrack):
        def __init__(self):
            super().__init__()
            self.reference = reference.DecodedReference(session_id)
            self.encoder = OpusEncoder()
            self.samples, self.started = 0, None
            self.gate, self.triggers = asyncio.Event(), []
            self.pending, self.active_start = False, None
            self.receiving, self.error = False, None

        def release(self):
            require(self.error is None and self.readyState == 'live' and not self.pending,
                    'Finite track is stopped/failed or has a pending utterance')
            require(self.active_start is None or self.samples >= self.active_start+body_frames+CODEC_TAIL_FRAMES+QUIET_FRAMES,
                    'Finite warm utterance overlaps the earlier required codec/quiet tail')
            self.pending = True
            self.gate.set()

        async def recv(self):
            require(self.error is None and self.readyState == 'live' and not self.receiving,
                    'Finite publisher is stopped, failed or concurrently read')
            self.receiving = True
            try:
                await self.gate.wait()
                if self.started is None:
                    self.started = time.monotonic()
                await asyncio.sleep(max(0, self.started+self.samples/RATE-time.monotonic()))
                if self.pending:
                    self.active_start, self.pending = self.samples, False
                    self.triggers.append({'utterance': len(self.triggers)+1, 'first_source_frame_to_encoder_monotonic_ns': time.monotonic_ns(),
                        'rtp_frame_index': self.samples, 'body_frames': body_frames, 'codec_tail_frames': CODEC_TAIL_FRAMES,
                        'source_pcm_sha256': hashlib.sha256(source.tobytes()).hexdigest(), 'signal_seed': seed})
                offset = self.samples-self.active_start
                values = source[offset:offset+FRAMES] if offset < body_frames else np.zeros(FRAMES, dtype='<i2')
                frame = AudioFrame(format='s16', layout='mono', samples=FRAMES)
                frame.planes[0].update(values.tobytes())
                frame.sample_rate, frame.pts, frame.time_base = RATE, self.samples, Fraction(1, RATE)
                payloads, pts = await asyncio.to_thread(self.encoder.encode, frame)
                require(len(payloads) == 1 and pts == self.samples, 'Finite Opus publisher changed the exact20ms packet calendar')
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
    return FiniteTrack()


def frozen_reference(track, trigger):
    require(track.error is None and track.reference.error is None, 'Finite emitted reference is permanently failed')
    start = trigger['rtp_frame_index']
    count = trigger['body_frames']+trigger['codec_tail_frames']
    require(track.samples >= start+count+QUIET_FRAMES, 'Finite utterance/tail has not actually been emitted')
    decoded = track.reference.snapshot()
    pcm = np.array(decoded.pcm[start:start+count], dtype='<i2', copy=True)
    require(len(pcm) == count, 'Finite decoder omitted emitted source prefix or codec tail')
    pcm.flags.writeable = False
    return {'session_id': decoded.session_id, 'payload_calendar_sha256': decoded.payload_sha256,
            'source': deepcopy(trigger), 'pcm': pcm, 'decoded_pcm_sha256': hashlib.sha256(pcm.tobytes()).hexdigest(),
            'reference_start_frame': start, 'reference_frames': count, 'quiet_frames': QUIET_FRAMES}


def remove_music(samples):
    """Remove only the predeclared440Hz native carrier, separately per channel."""
    result = samples.astype(float, copy=True)
    for start in range(0, len(result), FRAMES):
        count = min(FRAMES, len(result)-start)
        t = np.arange(count)/RATE
        basis = np.column_stack((np.sin(2*np.pi*440*t), np.cos(2*np.pi*440*t)))
        block = result[start:start+count]
        result[start:start+count] = block-basis @ (np.linalg.pinv(basis) @ block)
    return result


def verify_complete(data, reference, *, start_bounds, music=False, quiet_carrier=None, capture_first_frame=None):
    """One bounded alignment/level; check EVERY decoded prefix/body/tail window.

    Opus loss is already represented by the independent exact-payload decoder.
    The predeclared16% residual/97% correlation allows mild downstream filtering
    and S16 rounding. No fitted endpoint, omitted prefix, per-window level or
    time-stretching can redefine the finite reference. Collection is not SLA.
    """
    require(type(data) is bytes and 0 < len(data) <= MAX_CAPTURE_FRAMES*4 and len(data) % 4 == 0,
            'Finite capture has invalid stereo framing or exceeds14seconds')
    require(type(music) is bool and type(start_bounds) is tuple and len(start_bounds) == 2
            and all(type(x) is int for x in start_bounds), 'Finite mapping requires explicit integer start bounds')
    expected = reference['pcm']
    source = reference['source']
    known_source = waveform(body_frames=source['body_frames'], seed=source['signal_seed'])
    require(hashlib.sha256(known_source.tobytes()).hexdigest() == source['source_pcm_sha256'],
            'Finite source declaration changed its nonrepeating opening/body/tail waveform')
    require(type(source['rtp_frame_index']) is int and source['rtp_frame_index'] >= 0
            and type(source['first_source_frame_to_encoder_monotonic_ns']) is int
            and source['first_source_frame_to_encoder_monotonic_ns'] > 0,
            'Finite source declaration changed its exact original frame/clock')
    require(expected.dtype == np.dtype('<i2') and len(expected) == reference['reference_frames']
            == source['body_frames']+source['codec_tail_frames']
            and source['codec_tail_frames'] == CODEC_TAIL_FRAMES and reference['quiet_frames'] == QUIET_FRAMES,
            'Finite reference relabeled its emitted opening/body/codec-tail length')
    require(hashlib.sha256(expected.tobytes()).hexdigest() == reference['decoded_pcm_sha256'],
            'Finite reference bytes changed after independent decode')
    samples = np.frombuffer(data, dtype='<i2').reshape(-1, 2)
    require(int(np.max(np.abs(samples.astype(np.int32)))) < 32760, 'Finite final output clipped')
    observed = remove_music(samples) if music else samples.astype(float)
    template = np.repeat(expected.astype(float)[:, None], 2, axis=1)
    if music:
        template = remove_music(template)
    length = len(expected)
    low, high = start_bounds
    require(0 <= low <= high and high+length+QUIET_FRAMES <= len(observed),
            'Finite capture omitted its predeclared complete prefix/tail/quiet coverage')
    voice = observed[:, 0]
    pattern = template[:, 0]
    size = 1 << (len(voice)+length-2).bit_length()
    cross = np.fft.irfft(np.fft.rfft(voice, size)*np.fft.rfft(pattern[::-1], size), size)[length-1:len(voice)]
    sums = np.r_[0., np.cumsum(voice*voice)]
    energy = sums[length:]-sums[:-length]
    scores = cross/np.sqrt(np.maximum(energy*np.sum(pattern*pattern), 1.))
    start = low+int(np.argmax(scores[low:high+1]))
    captured = observed[start:start+length]
    duck_receipt = {}
    if quiet_carrier is not None:
        require(music and type(capture_first_frame) is int and capture_first_frame >= 0,
                'Finite duck carrier requires the original native capture frame calendar')
        # Projection only proposes one voice alignment. Its20ms blocks may
        # straddle the native40ms down-ramp and the first voice sample; such a
        # block is not a stationary440Hz carrier. Remove the frozen carrier
        # with the one onset measured solely BEFORE that proposed voice.
        captured, duck_receipt = quiet_module().subtract_declared_duck(samples, quiet_carrier,
            first_frame=capture_first_frame, voice_start=start, voice_frames=length)
        template = np.repeat(expected.astype(float)[:, None], 2, axis=1)
        whole_correlation = float(np.sum(captured*template)/math.sqrt(max(1., np.sum(captured*captured)*np.sum(template*template))))
    else:
        whole_correlation = float(scores[start])
    denominator = float(np.sum(template*template))
    require(denominator > 100*length, 'Finite emitted reference lacks measurable source energy')
    level = float(np.sum(captured*template)/denominator)
    require(LEVEL_BOUNDS[0] <= level <= LEVEL_BOUNDS[1], 'Finite utterance omitted content or changed its one admitted level')
    records = []
    for at in range(0, length, FRAMES):
        known, actual = template[at:at+FRAMES]*level, captured[at:at+FRAMES]
        rms = float(np.sqrt(np.mean(known*known)))
        residual = float(np.sqrt(np.mean((actual-known)**2)))
        if rms >= 20:
            correlations = [float(np.sum(known[:, ch]*actual[:, ch])/math.sqrt(max(1., np.sum(known[:, ch]**2)*np.sum(actual[:, ch]**2)))) for ch in range(2)]
            require(min(correlations) >= WINDOW_CORRELATION and residual <= max(4., rms*WINDOW_ERROR_FRACTION),
                    f'Finite utterance window omitted or changed at decoded frame{at}')
        else:
            correlations = []
            require(residual <= 8., f'Finite codec-tail window contains unexplained output at decoded frame{at}')
        records.append({'frame': at, 'frames': len(known), 'expected_rms': rms, 'residual_rms': residual,
                        'channel_correlations': correlations})
    quiet = observed[start+length:start+length+QUIET_FRAMES]
    quiet_receipt = {}
    if quiet_carrier is not None:
        require(music and type(capture_first_frame) is int and capture_first_frame >= 0,
                'Finite quiet carrier requires the original native capture frame calendar')
        quiet_receipt = quiet_module().verify_quiet(samples[start+length:], quiet_carrier,
            first_frame=capture_first_frame+start+length)
    else:
        require(capture_first_frame is None, 'Finite stationary oracle cannot relabel its capture frame origin')
        require(float(np.sqrt(np.mean(quiet*quiet))) <= 4., 'Finite utterance retained stale speech after its exact decoded tail')
    require(whole_correlation >= WINDOW_CORRELATION, 'Finite whole-waveform alignment did not retain the complete utterance')
    body = source['body_frames']
    return {'cold_utterance_completeness_passed': True, 'cold_utterance_completeness_status': 'finite_emitted_opus_prefix_body_tail_verified',
            'session_id': reference['session_id'], 'source': deepcopy(source),
            'payload_calendar_sha256': reference['payload_calendar_sha256'], 'decoded_pcm_sha256': reference['decoded_pcm_sha256'],
            'alignment_start_frame': start, 'one_admitted_level': level, 'whole_correlation': whole_correlation,
            'verified_reference_frames': length, 'verified_body_frames': body, 'verified_codec_tail_frames': CODEC_TAIL_FRAMES,
            'verified_quiet_frames': QUIET_FRAMES, 'prefix_windows': records[:min(18, body//FRAMES)],
            'tail_windows': records[max(0, body//FRAMES-12):], 'all_windows': records,
            'speech_latency_performance_passed': False, 'speech_latency_performance_status': 'pending_declared_and_characterized_software_budget',
            'scope': 'Exact finite emitted-payload Opus decode vs both final digital channels; one bounded mapping; no acoustic/phone/minimum-latency claim',
            **duck_receipt, **quiet_receipt}


def retain_diagnostic(directory, reference, data, timings, *, music_baseline=None):
    """Keep failure and success bytes BEFORE a reference/peer can be cleared."""
    require(type(directory) is Path or isinstance(directory, Path), 'Finite artifact directory must be a fixture Path')
    prefix = directory/f'finite-speech-{uuid4()}'
    artifacts = {}
    contents = [('decoded_reference', reference['pcm'].tobytes()), ('final_pcm', data),
                ('final_timestamps', (json.dumps(timings, allow_nan=False)+'\n').encode())]
    if music_baseline is not None:
        require(type(music_baseline) is bytes and len(music_baseline) == QUIET_FRAMES*4,
                'Finite carrier baseline artifact is not its exact pre-offer PCM')
        contents.append(('music_baseline', music_baseline))
    for label, content in contents:
        path = prefix.with_name(prefix.name+'-'+label+('.json' if label == 'final_timestamps' else '.pcm'))
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
        artifacts[label] = {'path': str(path), 'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
    return artifacts


READY_SCOPE = 'Authenticated latest fresh exact-source output dispatch acknowledged before SDP answer; not first-ever tick or physical presentation'
OWNER_BEGIN_KEY = 'speech_startup_authenticated_begin_ack'
READY_HEALTH_KEYS = ('ready', 'error', 'source', 'source_operation_generation', 'native_generation',
    'speech_session_id', 'speech_startup_identity', 'speech_startup_authenticated_ready_ack',
    'speech_startup_phase', 'speech_startup_error', 'speech_startup_started_monotonic_ns',
    'speech_startup_prepared_monotonic_ns', 'speech_startup_first_mix_monotonic_ns',
    'speech_startup_released_monotonic_ns', 'speech_startup_pending_frames', 'speech_startup_setup_budget_ns',
    'speech_startup_performance_qualified')


def validate_ready_route(route, role):
    require(type(route) is dict and set(route) == {'source_owner', 'source_identity', 'source_operation_generation', 'native_generation', 'launch_generation',
            'selected_output', 'selected_ids', 'local_pin', 'units'} and role in {'idle', 'native'}, 'Finite readiness route schema changed')
    source, owner = route['source_identity'], route['source_owner']
    require(type(source) is dict and set(source) == {'zone_id', 'incarnation', 'epoch', 'owner'}
            and type(source['epoch']) is int and source['epoch'] >= 0 and source['owner'] == owner
            and type(route['source_operation_generation']) is int and route['source_operation_generation'] > 0
            and (owner is None) == (role == 'idle'), 'Finite readiness lost its actual source identity')
    try:
        require(all(type(source[key]) is str and str(UUID(source[key])) == source[key] for key in ('zone_id', 'incarnation')),
                'Finite readiness source UUID representation changed')
        if role == 'native':
            require(type(owner) is dict and owner.get('zone_id') == source['zone_id'] and owner.get('incarnation') == source['incarnation']
                    and type(owner.get('epoch')) is int and owner['epoch'] == source['epoch'] and owner.get('protocol') == 'airplay2'
                    and type(owner.get('session_id')) is str and str(UUID(owner['session_id'])) == owner['session_id']
                    and type(route['native_generation']) is int and 0 < route['native_generation'] < 2**32,
                    'Finite readiness native owner/generation changed')
        else:
            require(route['native_generation'] is None, 'Finite idle readiness claims an accepted native generation')
    except (ValueError, AttributeError, TypeError) as exc:
        raise RuntimeFailure('Finite readiness contains malformed actual source UUIDs') from exc
    launch = route['launch_generation']
    require(type(launch) is str and len(launch) == 32 and launch != '0'*32 and all(char in '0123456789abcdef' for char in launch),
            'Finite readiness lacks its actual room launch')


def validate_ready_observation(observation, role, route, identity, *, offer_request_ns, offer_response_ns):
    """Recompute the typed ACK against independently held current worker facts."""
    validate_ready_route(route, role)
    require(type(observation) is dict and set(observation) == {'observed_before_monotonic_ns', 'observed_after_monotonic_ns', 'worker_health'},
            'Finite readiness lacks an exact observed health bracket')
    before, after, health = (observation[key] for key in ('observed_before_monotonic_ns', 'observed_after_monotonic_ns', 'worker_health'))
    require(type(before) is int and type(after) is int and offer_response_ns <= before <= after
            and type(health) is dict and set(health) in (set(READY_HEALTH_KEYS), set(READY_HEALTH_KEYS) | {OWNER_BEGIN_KEY}),
            'Finite readiness health schema or observation clock changed')
    source, startup, ack = health['source'], health['speech_startup_identity'], health['speech_startup_authenticated_ready_ack']
    require(health['ready'] is True and health['error'] is None and type(source) is dict and source.get('ready') is True
            and source.get('error') is None and all(source.get(key) == value for key, value in route['source_identity'].items())
            and type(health['source_operation_generation']) is int
            and health['source_operation_generation'] == route['source_operation_generation']
            and health['native_generation'] == route['native_generation'],
            'Finite readiness replaced its actual current source operation')
    require(health['speech_session_id'] == identity['session_id'] and type(startup) is dict
            and set(startup) == {'speech_id', 'session_id', 'request_id'}
            and all(startup.get(key) == value for key, value in identity.items())
            and type(startup['speech_id']) is str and len(startup['speech_id']) == 32
            and startup['speech_id'] != '0'*32 and all(char in '0123456789abcdef' for char in startup['speech_id'])
            and health['speech_startup_phase'] == 'ready' and health['speech_startup_error'] is None
            and type(health['speech_startup_pending_frames']) is int and health['speech_startup_pending_frames'] == 0
            and health['speech_startup_performance_qualified'] is False,
            'Finite readiness does not own its exact current API session and nonce')
    owner = route['source_owner']
    admitted = OWNER_BEGIN_KEY in health
    if admitted:
        require(type(health[OWNER_BEGIN_KEY]) is dict and health[OWNER_BEGIN_KEY] == ack,
                'Finite voice admission lost its exact authenticated BEGIN echo')
    expected = {'incarnation': UUID(route['source_identity']['incarnation']).hex,
                'session_id': None if role == 'idle' else UUID(owner['session_id']).hex,
                'epoch': route['source_identity']['epoch'],
                'generation': 1 if role == 'idle' else route['native_generation'],
                'operation_generation': route['source_operation_generation'],
                'room_id': UUID(route['source_identity']['zone_id']).hex,
                'launch_generation': route['launch_generation'], 'speech_id': startup['speech_id'],
                'action': 'begin' if admitted else 'ready' if role == 'idle' else 'observe'}
    require(type(ack) is dict and set(ack) == set(expected) | {'connected', 'ready', 'prepared_monotonic_ns', 'mixed_monotonic_ns', 'output_count'}
            and all(type(ack.get(key)) is type(value) and ack[key] == value for key, value in expected.items())
            and ack['connected'] is True and ack['ready'] is True and type(ack['output_count']) is int and ack['output_count'] == 1,
            'Finite authenticated ACK changed a typed source/session/room/launch/nonce echo')
    started, prepared, mixed, released, budget = (health[key] for key in (
        'speech_startup_started_monotonic_ns', 'speech_startup_prepared_monotonic_ns',
        'speech_startup_first_mix_monotonic_ns', 'speech_startup_released_monotonic_ns', 'speech_startup_setup_budget_ns'))
    require(all(type(value) is int for value in (started, prepared, mixed, released, budget))
            and type(ack['prepared_monotonic_ns']) is int and type(ack['mixed_monotonic_ns']) is int
            and ack['prepared_monotonic_ns'] == prepared and ack['mixed_monotonic_ns'] == mixed
            and budget == 5_000_000_000 and offer_request_ns <= started <= released <= offer_response_ns
            and 0 <= released-started < budget and 0 < mixed <= released and released-mixed < 100_000_000
            and (started <= prepared <= mixed if role == 'idle' else prepared == 0),
            'Finite readiness clocks do not prove bounded setup and a fresh acknowledged dispatch at release')
    return {'scope': READY_SCOPE, 'source_operation_generation': expected['operation_generation'],
            'offer_request_to_prepared_ms': (prepared-offer_request_ns)/1e6 if role == 'idle' else None,
            'offer_request_to_acknowledged_mix_ms': (mixed-offer_request_ns)/1e6,
            'offer_request_to_prefix_release_ms': (released-offer_request_ns)/1e6,
            'acknowledged_mix_to_prefix_release_ms': (released-mixed)/1e6}


def validate_final_timestamps(timestamps, byte_count, contract, *, start_frame):
    """Exact frame-offset continuity; preserve bounded hardware PTS jitter."""
    require(type(contract) is dict and set(contract) == {'base_time_ns', 'clock_offset_to_monotonic_ns', 'negotiated_period_frames',
            'period_uncertainty_ms', 'jitter_budget_ns', 'declared_before_offer_monotonic_ns'}
            and all(type(contract[key]) is int for key in ('base_time_ns', 'clock_offset_to_monotonic_ns', 'negotiated_period_frames', 'jitter_budget_ns', 'declared_before_offer_monotonic_ns'))
            and contract['base_time_ns'] > 0 and contract['declared_before_offer_monotonic_ns'] > 0
            and contract['negotiated_period_frames'] == FRAMES
            and contract['period_uncertainty_ms'] == FRAMES*1000/RATE
            and contract['jitter_budget_ns'] == FRAMES*1_000_000_000//RATE,
            'Finite capture lost its predeclared independently calibrated one-period timestamp budget')
    require(type(timestamps) is list and timestamps and type(start_frame) is int and start_frame >= 0,
            'Finite capture lacks its exact original per-buffer sample coordinates')
    frames, previous, origin = 0, None, None
    first_pts = first_offset = None
    adjacent, cumulative = [], []
    for timing in timestamps:
        require(type(timing) is dict and set(timing) == {'absolute_pts_monotonic_ns', 'callback_monotonic_ns', 'frames', 'gst_metadata'}
                and all(type(timing[key]) is int and timing[key] > 0 for key in ('absolute_pts_monotonic_ns', 'callback_monotonic_ns', 'frames')),
                'Finite output timing record is invalid')
        meta = timing['gst_metadata']
        pts, callback, count = (timing[key] for key in ('absolute_pts_monotonic_ns', 'callback_monotonic_ns', 'frames'))
        require(type(meta) is dict and set(meta) == {'offset', 'offset_end', 'pts', 'duration', 'discont'}
                and all(type(meta[key]) is int and meta[key] >= 0 for key in ('offset', 'offset_end', 'pts', 'duration'))
                and type(meta['discont']) is bool and meta['offset_end']-meta['offset'] == count
                and count == contract['negotiated_period_frames']
                and abs(meta['duration']*RATE-count*1_000_000_000) <= 2*RATE
                and pts == contract['base_time_ns']+meta['pts']-contract['clock_offset_to_monotonic_ns'],
                'Finite output metadata disagrees with actual samples, duration or held clock transform')
        if previous is not None:
            require(not meta['discont'] and meta['offset'] == previous['gst_metadata']['offset_end']
                    and callback > previous['callback_monotonic_ns'],
                    'Finite final sample offsets/callbacks show lost, repeated or discontinuous audio')
            error = (pts-previous['absolute_pts_monotonic_ns'])*RATE-previous['frames']*1_000_000_000
            require(abs(error) <= contract['jitter_budget_ns']*RATE,
                    'Finite raw PTS adjacent jitter exceeded its predeclared calibrated period')
            adjacent.append(error/RATE)
        else:
            first_pts, first_offset = pts, meta['offset']
        error = (pts-first_pts)*RATE-(meta['offset']-first_offset)*1_000_000_000
        require(abs(error) <= contract['jitter_budget_ns']*RATE,
                'Finite raw PTS drift exceeded its predeclared calibrated period')
        cumulative.append(error/RATE)
        if frames <= start_frame < frames+count:
            origin = pts+(start_frame-frames)*1_000_000_000//RATE
        frames += count
        previous = timing
    require(frames*4 == byte_count and origin is not None, 'Finite original buffer samples disagree with captured PCM or aligned origin')
    return origin, {'scope': 'Exact original sample offsets/duration/discontinuity; raw hardware PTS jitter retained within independently calibrated one-period uncertainty',
        'verified_frames': frames, 'verified_buffers': len(timestamps), 'jitter_budget_ns': contract['jitter_budget_ns'],
        'adjacent_jitter_min_ns': min(adjacent, default=0), 'adjacent_jitter_max_ns': max(adjacent, default=0),
        'cumulative_jitter_min_ns': min(cumulative), 'cumulative_jitter_max_ns': max(cumulative),
        'initial_discontinuity': timestamps[0]['gst_metadata']['discont']}


async def target_route(context, state, *, with_health=False):
    """Actual held source/output facts; no prepare, playback or route mutation."""
    before = time.monotonic_ns()
    health = await call_rpc(context.broker._worker_socket(state), 'health', {}, timeout=2)
    after = time.monotonic_ns()
    source = health.get('source') if type(health) is dict else None
    require(type(health) is dict and health.get('ready') is True and 'error' in health and health['error'] is None
            and type(source) is dict and source.get('ready') is True and 'owner' in source,
            'Finite target source/worker is not exactly healthy')
    require(source.get('zone_id') == context.target and type(source.get('incarnation')) is str
            and type(source.get('epoch')) is int and source['epoch'] >= 0, 'Finite target lost its durable source incarnation/epoch')
    require((source['owner'] is None) == (context.phase.phase == 'idle'), 'Finite target source differs from its exact role')
    require(type(health.get('source_operation_generation')) is int and health['source_operation_generation'] > 0
            and type(state.launch_generation) is str and len(state.launch_generation) == 32
            and state.launch_generation != '0'*32 and all(char in '0123456789abcdef' for char in state.launch_generation),
            'Finite target lacks its actual source operation or launch incarnation')
    selected = [output for output in await state.client.outputs(set()) if output['selected']]
    require(state.selected_ids == ['0'] and len(selected) == 1 and selected[0]['id'] == '0'
            and selected[0].get('protocol') == 'alsa' and type(selected[0].get('offset_ms')) is int
            and selected[0]['offset_ms'] == context.phase.offset_ms, 'Finite target selection differs from its exact saved local route')
    state.local_pin.validate()
    route = {'source_owner': deepcopy(source['owner']),
            'source_identity': {key: deepcopy(source[key]) for key in ('zone_id', 'incarnation', 'epoch', 'owner')},
            'source_operation_generation': health['source_operation_generation'],
            'native_generation': health.get('native_generation'), 'launch_generation': state.launch_generation,
            'selected_output': {key: selected[0][key] for key in ('id', 'protocol', 'selected', 'offset_ms')},
            'selected_ids': list(state.selected_ids), 'local_pin': deepcopy(state.local_pin.manifest),
            'units': {name: unit.identity() for name, unit in state.processes.items()}}
    validate_ready_route(route, context.phase.phase)
    if with_health:
        require(all(key in health for key in READY_HEALTH_KEYS), 'Finite worker lacks authenticated readiness evidence')
        observation = {'observed_before_monotonic_ns': before, 'observed_after_monotonic_ns': after,
                       'worker_health': {key: deepcopy(health[key]) for key in READY_HEALTH_KEYS}}
        if health.get(OWNER_BEGIN_KEY) is not None:
            observation['worker_health'][OWNER_BEGIN_KEY] = deepcopy(health[OWNER_BEGIN_KEY])
        return route, observation
    return route


async def measure_session(context, observer, *, role):
    """Opt-in existing admitted Linux lab API->broker->worker->OwnTone diagnostic.

    Caller owns fresh fixture admission, independent capture calibration and all
    final producer/daemon/capture cleanup. This helper never stops a music actor
    or replaces/reset its PCM observer. Existing matrix results stay separate.
    """
    from aiortc import RTCConfiguration, RTCPeerConnection, RTCRtpSender, RTCSessionDescription
    require(role in {'idle', 'native'} and context.phase.phase == role and context.group.NATIVE_LAB is not None,
            'Finite speech diagnostic requires the explicit clean lab and role')
    require(context.report.get('native_lab') and context.report.get('rootless_api'),
            'Finite diagnostic lacks actual admitted rootless API evidence')
    target = context.target
    state = context.states[target]
    capture = context.captures[target]
    require(context.pcm_guards[target].capture is capture, 'Finite diagnostic lost its original target final-PCM guard')
    units = {name: unit.identity() for name, unit in state.processes.items()}
    cold_player = await state.client.request('GET', '/api/player')
    original_route = await target_route(context, state)
    baseline = context.report['independent_capture_baselines'][target]
    timing_contract = {'base_time_ns': capture.expected_base, 'clock_offset_to_monotonic_ns': capture.clock_offset_ns,
        'negotiated_period_frames': baseline['negotiated_period_frames'], 'period_uncertainty_ms': baseline['period_uncertainty_ms'],
        'jitter_budget_ns': baseline['negotiated_period_frames']*1_000_000_000//RATE,
        'declared_before_offer_monotonic_ns': time.monotonic_ns()}
    if role == 'idle':
        require(cold_player['state'] == 'stop', 'Finite cold-idle diagnostic requires an actually stopped player before offer')
    music_baseline, quiet_carrier, carrier_contract = None, None, None
    if role == 'native':
        await observer.check()
        require(state.desired.volume == 100 and state.desired.duck_gain == .2
                and capture.rate == RATE and capture.channels == 2 and capture.format == 'S16LE',
                'Finite pre-offer carrier requires its unchanged canonical music/duck/capture route')
        # Copy only the bounded last200ms; preserve their exact original GST
        # offsets/PTS/callbacks. Existing guards remain authoritative too.
        require(len(capture.chunks) >= QUIET_FRAMES//FRAMES,
                'Finite pre-offer carrier lacks its original full200ms baseline')
        baseline_chunks = tuple(capture.chunks[-QUIET_FRAMES//FRAMES:])
        baseline_callbacks = tuple(capture.captured_at[-QUIET_FRAMES//FRAMES:])
        music_baseline = b''.join(baseline_chunks)
        baseline_timings = [{'absolute_pts_monotonic_ns': capture.absolute[at],
            'callback_monotonic_ns': round(at*1e9), 'frames': len(block)//4,
            'gst_metadata': deepcopy(capture.buffer_metadata[at])}
            for at, block in zip(baseline_callbacks, baseline_chunks, strict=True)]
        _, baseline_quality = validate_final_timestamps(baseline_timings, len(music_baseline), timing_contract, start_frame=0)
        quiet_carrier = quiet_module().freeze_carrier(music_baseline,
            first_frame=baseline_timings[0]['gst_metadata']['offset'], duck_gain=state.desired.duck_gain)
        carrier_contract = {'declared_before_offer_monotonic_ns': time.monotonic_ns(),
            'original_target_route': deepcopy(original_route), 'carrier': quiet_carrier.evidence(),
            'timings': baseline_timings, 'timestamp_quality': baseline_quality}
    peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    identity = {'session_id': str(uuid4()), 'request_id': str(uuid4())}
    tone = packet_track(identity['session_id'])
    transceiver = peer.addTransceiver(tone, direction='sendonly')
    codecs = [codec for codec in RTCRtpSender.getCapabilities('audio').codecs if codec.mimeType.lower() == 'audio/opus']
    require(bool(codecs), 'Finite peer lacks actual Opus')
    transceiver.setCodecPreferences(codecs)
    record = {'role': role, **identity, 'cold_utterance_completeness_passed': False,
              'cold_utterance_completeness_status': 'not_completed', 'rows': [], 'cleanup_errors': [],
              'player_before_offer': deepcopy(cold_player), 'original_target_route': original_route,
              'capture_timing_contract': timing_contract}
    if carrier_contract is not None:
        record['music_carrier_contract'] = carrier_contract
    context.report.setdefault('finite_speech_diagnostics', []).append(record)
    primary = None
    try:
        await peer.setLocalDescription(await peer.createOffer())
        record['offer_request_monotonic_ns'] = time.monotonic_ns()
        answer = await context.api.request('POST', f'/api/v1/rooms/{target}/speech',
            json={**identity, 'action': 'offer', 'sdp': peer.localDescription.sdp, 'type': 'offer'})
        record['offer_response_monotonic_ns'] = time.monotonic_ns()
        require(answer.get('admitted_room_id') == target, 'Finite speech was admitted to a different room')
        ready_route, record['timer_ready_observation'] = await target_route(context, state, with_health=True)
        require(ready_route == original_route, 'Finite readiness changed the original held target route')
        record['timer_ready_measurements'] = validate_ready_observation(record['timer_ready_observation'], role, original_route, identity,
            offer_request_ns=record['offer_request_monotonic_ns'], offer_response_ns=record['offer_response_monotonic_ns'])
        record['timer_ready_ack'] = deepcopy(record['timer_ready_observation']['worker_health']['speech_startup_authenticated_ready_ack'])
        record['offer_scope'] = READY_SCOPE
        await peer.setRemoteDescription(RTCSessionDescription(sdp=answer['sdp'], type=answer['type']))
        async def connected():
            require(peer.connectionState not in {'failed', 'closed'}, 'Finite peer failed before gated source release')
            return peer.connectionState == 'connected'
        await context.latency.observed_wait(connected, observer, 8, 'Finite Opus peer did not connect')
        require(tone.samples == 0, 'Finite cold peer emitted payload before explicit release')
        record['player_before_payload_release'] = await state.client.request('GET', '/api/player')
        # A reviewed future idle prearm may start playback during offer; cold
        # means the player was stopped BEFORE offer, not forced to stay stopped.
        require({name: unit.identity() for name, unit in state.processes.items()} == units,
                'Finite offer changed the target unit incarnation')
        warm_player = None
        for kind in ('cold', 'warm'):
            capture.poll()
            first_index = len(capture.chunks)
            capture_first_frame = None
            row = {'kind': f'{kind}_{role}', 'cold_utterance_completeness_passed': False,
                   'cold_utterance_completeness_status': 'not_completed',
                   'utterance_request_monotonic_ns': time.monotonic_ns()}
            record['rows'].append(row)
            # Freeze the known control fact first; later health/route errors
            # cannot mask a stopped or replaced purported warm driver.
            player = await state.client.request('GET', '/api/player')
            row['pre_release_player'] = deepcopy(player)
            if kind == 'warm':
                require(player['state'] == 'play' and warm_player is not None and warm_player['state'] == 'play',
                        'Finite warm utterance lost the already playing OwnTone path')
                require(player.get('item_id') is not None and player['item_id'] == warm_player.get('item_id'),
                        'Finite warm utterance replaced the original OwnTone item')
            elif role == 'native':
                require(player['state'] == 'play' and player.get('item_id') == cold_player.get('item_id'),
                        'Finite cold native utterance changed the original program')
            row['pre_release_target_route'], row['pre_release_readiness'] = await target_route(context, state, with_health=True)
            require(row['pre_release_target_route'] == original_route,
                    'Finite utterance changed its original held source/unit/selected output route')
            validate_ready_observation(row['pre_release_readiness'], role, original_route, identity,
                offer_request_ns=record['offer_request_monotonic_ns'], offer_response_ns=record['offer_response_monotonic_ns'])
            require(row['pre_release_readiness']['worker_health']['speech_startup_authenticated_ready_ack'] == record['timer_ready_ack'],
                    'Finite utterance replaced its originally authenticated speech preparation')
            await observer.check()
            async def warm_running(row=row, expected_player=warm_player):
                current = await state.client.request('GET', '/api/player')
                samples = row.setdefault('warm_player_samples', [])
                require(len(samples) < 1000, 'Finite warm control observation exceeded its explicit bound')
                samples.append({'monotonic_ns': time.monotonic_ns(), 'player': deepcopy(current)})
                require(current['state'] == 'play' and current.get('item_id') == expected_player.get('item_id'),
                        'Finite warm driver stopped or replaced its original item during utterance')
            tone.release()
            async def emitted(kind=kind):
                if kind == 'warm':
                    await warm_running()
                return tone.triggers[-1] if len(tone.triggers) == (1 if kind == 'cold' else 2) else None
            trigger = await context.latency.observed_wait(emitted, observer, 2, 'Finite source was not handed to actual encoder')
            async def collected(trigger=trigger, kind=kind):
                if kind == 'warm':
                    await warm_running()
                capture.poll()
                if tone.samples < trigger['rtp_frame_index']+BODY_FRAMES+CODEC_TAIL_FRAMES+QUIET_FRAMES:
                    return None
                # Collection bound is explicit and includes actual final output lead.
                wanted = trigger['first_source_frame_to_encoder_monotonic_ns']+round((BODY_FRAMES+CODEC_TAIL_FRAMES+QUIET_FRAMES)/RATE*1e9)
                wanted += (context.frozen_plan['output_buffers_ms'][target]+abs(context.phase.offset_ms)+500)*1_000_000
                return bool(capture.captured_at and capture.absolute[capture.captured_at[-1]] >= wanted)
            await context.latency.observed_wait(collected, observer, COLLECTION_SECONDS, 'Finite final prefix/body/tail capture did not finish')
            require(capture.chunks and capture.rate == RATE and capture.channels == 2 and capture.format == 'S16LE',
                    'Finite target final capture changed caps')
            frozen = frozen_reference(tone, trigger)
            chunks = tuple(capture.chunks[first_index:])
            data = b''.join(chunks)
            row['source'] = deepcopy(trigger)
            timings = [{'absolute_pts_monotonic_ns': capture.absolute[capture.captured_at[first_index+i]],
                        'callback_monotonic_ns': round(capture.captured_at[first_index+i]*1e9),
                        'frames': len(block)//4, 'gst_metadata': deepcopy(capture.buffer_metadata[capture.captured_at[first_index+i]])}
                        for i, block in enumerate(chunks)]
            if quiet_carrier is not None:
                capture_first_frame = timings[0]['gst_metadata']['offset']
                row['capture_first_frame'] = capture_first_frame
            row['artifacts'] = await asyncio.to_thread(retain_diagnostic, context.temporary, frozen, data, timings,
                music_baseline=music_baseline)
            high = len(data)//4-frozen['reference_frames']-QUIET_FRAMES
            receipt = await asyncio.to_thread(verify_complete, data, frozen, start_bounds=(0, high), music=role == 'native',
                quiet_carrier=quiet_carrier, capture_first_frame=capture_first_frame)
            start = receipt['alignment_start_frame']
            pts, receipt['capture_timestamp_quality'] = validate_final_timestamps(timings, len(data), timing_contract, start_frame=start)
            receipt['final_reference_origin_monotonic_ns'] = pts
            receipt['source_to_final_reference_origin_ms'] = (pts-trigger['first_source_frame_to_encoder_monotonic_ns'])/1e6
            receipt['kind'] = f'{kind}_{role}'
            receipt['same_session_for_cold_and_warm'] = True
            receipt['offer_request_to_encoder_ms'] = (trigger['first_source_frame_to_encoder_monotonic_ns']-record['offer_request_monotonic_ns'])/1e6
            receipt['utterance_request_to_encoder_ms'] = (trigger['first_source_frame_to_encoder_monotonic_ns']-row['utterance_request_monotonic_ns'])/1e6
            receipt['offer_request_to_final_reference_origin_ms'] = (receipt['final_reference_origin_monotonic_ns']-record['offer_request_monotonic_ns'])/1e6
            mixed = record['timer_ready_ack']['mixed_monotonic_ns']
            receipt['acknowledged_mix_to_encoder_ms'] = (trigger['first_source_frame_to_encoder_monotonic_ns']-mixed)/1e6
            receipt['acknowledged_mix_to_final_reference_origin_ms'] = (receipt['final_reference_origin_monotonic_ns']-mixed)/1e6
            row.update(receipt)
            require({name: unit.identity() for name, unit in state.processes.items()} == units,
                    'Finite utterance restarted an existing target unit')
            await observer.check()
            row['player_after_utterance'] = deepcopy(await state.client.request('GET', '/api/player'))
            if kind == 'warm':
                require(row['player_after_utterance']['state'] == 'play'
                        and row['player_after_utterance'].get('item_id') == warm_player.get('item_id'),
                        'Finite warm driver stopped or replaced its original item after complete capture')
            if kind == 'cold' and row['player_after_utterance']['state'] != 'play':
                # Preserve the known failed warm baseline; the next explicit
                # warm pre-release gate will refuse before any health RPC.
                row['post_utterance_target_route_unobserved_reason'] = 'player_already_stopped_before_warm'
            else:
                row['post_utterance_target_route'], row['post_utterance_readiness'] = await target_route(context, state, with_health=True)
                require(row['post_utterance_target_route'] == original_route,
                        'Finite utterance replaced its original source/unit/output route during delivery')
                validate_ready_observation(row['post_utterance_readiness'], role, original_route, identity,
                    offer_request_ns=record['offer_request_monotonic_ns'], offer_response_ns=record['offer_response_monotonic_ns'])
                require(row['post_utterance_readiness']['worker_health']['speech_startup_authenticated_ready_ack'] == record['timer_ready_ack'],
                        'Finite delivery replaced its originally authenticated speech preparation')
            warm_player = row['player_after_utterance']
        record.update(cold_utterance_completeness_passed=True,
                      cold_utterance_completeness_status='finite_emitted_opus_prefix_body_tail_verified')
    except BaseException as exc:
        primary = exc
        record['failure'] = str(exc)[:2000]
        raise
    finally:
        tone.stop()
        for name, operation in [('API close', context.api.request('POST', f'/api/v1/rooms/{target}/speech',
            json={**identity, 'action': 'close', 'request_id': str(uuid4())})), ('RTC close', peer.close())]:
            try:
                await asyncio.wait_for(operation, 5)
            except Exception as exc:
                record['cleanup_errors'].append(f'{name}:{type(exc).__name__}')
        record['reference_evidence'] = tone.reference.evidence()
        tone.reference.clear()
        if record['cleanup_errors']:
            record['cold_utterance_completeness_passed'] = False
            record['cold_utterance_completeness_status'] = 'not_completed'
        if primary is None:
            require(not record['cleanup_errors'] and peer.connectionState == 'closed', 'Finite peer/API cleanup failed')
    return record
