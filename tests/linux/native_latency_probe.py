"""Shared signal and observation helpers for isolated digital latency epochs.

Idle speech measures encoder-to-final-output latency, not a guessed mixer PTS.
The epoch runner owns room preparation and its measured timing plan. Importing
these marker, capture and untouched-room guards opens no devices or processes.
"""
from __future__ import annotations

# Bounded manual proc/config observations, separate from production code.
# ruff: noqa: ASYNC240

import asyncio
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
import time
from uuid import uuid4

import numpy as np

from shiri.rpc import call_rpc
from shiri.runtime.system import RuntimeFailure

RATE = 48000
PRODUCER_SECONDS = 480
CAPTURE_BYTES = 128 * 1024 * 1024
CONTROL_SAMPLES = 10000


def require(value, message):
    if not value:
        raise RuntimeFailure(message)


def capture_type(base):
    """Use the same real offset/caps fences with an explicit larger budget."""
    class LatencyCapture(base):
        maximum_bytes = CAPTURE_BYTES

        def poll(self):
            require(self.error is None, self.error or 'Latency final capture failed')
            require(self.capture_dropped == 0, 'Latency final capture dropped PCM')
            if self.needs_latency:
                self.needs_latency = False
                self.pipeline.recalculate_latency()
            try:
                while self.pending:
                    at, data, rate, channels, audio_format, metadata = self.pending[0]
                    require(bool(data) and len(data) % 4 == 0, 'Latency output has incomplete stereo PCM')
                    require((rate, channels, audio_format) == (RATE, 2, 'S16LE'), 'Latency output changed caps')
                    require(self.total + len(data) <= CAPTURE_BYTES, 'Latency capture exceeded128MiB')
                    self.sequence.push(metadata, len(data)//4, rate)
                    self.rate, self.channels, self.format = rate, channels, audio_format
                    self.total += len(data)
                    self.chunks.append(data)
                    self.captured_at.append(at)
                    self.pending.popleft()
            except RuntimeFailure as exc:
                self.error = str(exc)[:2000]
                raise
            return {'capture': self.device, 'format': self.format, 'rate': self.rate, 'channels': self.channels,
                    'observed_bytes': self.total, 'maximum_bytes': CAPTURE_BYTES,
                    'capture_dropped_bytes': self.capture_dropped, 'discontinuities': self.discontinuities,
                    'max_packet_gap_seconds': self.max_packet_gap, 'frame_continuity': self.sequence.evidence(),
                    'common_clock_base_ns': self.expected_base,
                    'clock_offset_to_monotonic_ns': self.clock_offset_ns}
    return LatencyCapture


class UntouchedObserver:
    """Sticky original-B source/item/volume/PCM/NPT guard, including the tail."""
    def __init__(self, context, evidence):
        self.context, self.evidence = context, evidence
        self.room = context.untouched
        self.state = context.states[self.room]
        self.capture = context.captures[self.room]
        self.guard = context.pcm_guards[self.room]
        require(self.guard.reference is not None, 'Latency requires an audible original untouched reference')
        self.units = {name: deepcopy(unit.identity()) for name, unit in self.state.processes.items()}
        self.sender_units = {name: deepcopy(unit.identity()) for name, unit in context.broker.sender_processes.items()}
        self.intent = self.state.desired.model_dump(mode='json')
        self.health = self.owner = self.player = self.origin = self.last_progress = None
        self.samples, self.error = [], None
        self.done, self.watcher = asyncio.Event(), None

    async def initialize(self):
        self.health = await call_rpc(self.context.broker._worker_socket(self.state), 'health', {}, timeout=2)
        self.owner = deepcopy(self.health['source']['owner'])
        self.player = await self.state.client.request('GET', '/api/player')
        require(self.owner is not None and self.player['state'] == 'play', 'Untouched original program is absent')
        await self._sample()
        self.watcher = asyncio.create_task(self._watch(), name='native-latency-untouched-observer')
        await asyncio.sleep(0)
        await self.check()

    def check_pcm(self):
        require(self.error is None, self.error or 'Untouched latency observer failed permanently')
        try:
            require(self.context.captures.get(self.room) is self.capture
                    and self.context.pcm_guards.get(self.room) is self.guard,
                    'Latency replaced the original untouched PCM observer')
            self.guard.check()
        except Exception as exc:
            self.error = str(exc)[:2000]
            raise

    async def _sample(self):
        try:
            self.check_pcm()
            context, state = self.context, self.state
            require(context.broker.rooms.get(self.room) is state and context.broker.ready and state.status == 'running',
                    'Untouched runtime readiness changed during latency measurement')
            require(state.desired.model_dump(mode='json') == self.intent, 'Latency changed untouched intent')
            require({name: unit.identity() for name, unit in state.processes.items()} == self.units
                    and all(unit.alive for unit in state.processes.values()), 'Latency changed an untouched unit invocation')
            require({name: unit.identity() for name, unit in context.broker.sender_processes.items()} == self.sender_units
                    and all(unit.alive for unit in context.broker.sender_processes.values()), 'Latency changed shared sender units')
            outputs = await state.client.outputs(set())
            require(state.selected_ids == ['0'] and [v['id'] for v in outputs if v['selected']] == ['0'],
                    'Latency changed actual untouched output selection')
            health = await call_rpc(context.broker._worker_socket(state), 'health', {}, timeout=2)
            require(health['ready'] and not health.get('error') and health['source']['ready']
                    and health['source']['owner'] == self.owner
                    and health['native_generation'] == self.health['native_generation']
                    and health['native_group_id'] == self.health['native_group_id'],
                    'Latency changed the untouched exact source/timeline')
            if health.get('speech_mix') == 'owntone_player':
                check = getattr(context.group, 'require_untouched_music_health', None)
                require(callable(check), 'Late-mix latency lacks its original final PCM health observer')
                check(health, self.guard)
            else:
                require(health['music_gain'] == 1 and health['speech_session_id'] is None
                        and health['dropped_bytes'] == health['speech_dropped_frames'] == 0,
                        'Latency ducked/injected/dropped untouched music')
            player = await state.client.request('GET', '/api/player')
            require(player['state'] == 'play' and player['item_id'] == self.player['item_id']
                    and player.get('volume') == state.current_volume == self.intent['volume'],
                    'Latency paused/changed untouched program or volume')
            progress, at = player.get('item_progress_ms'), time.monotonic()
            require(type(progress) is int and progress >= 0, 'Untouched NPT is invalid')
            if self.origin is None:
                self.origin = (at, progress)
            else:
                origin_at, origin_progress = self.origin
                require(abs(progress-origin_progress-(at-origin_at)*1000) < 1500,
                        'Untouched NPT stalled/jumped from its original timeline')
            if self.last_progress is not None:
                previous_at, previous = self.last_progress
                require(progress >= previous and (progress > previous or at-previous_at <= 2),
                        'Untouched NPT stopped or went backwards')
            if self.last_progress is None or progress > self.last_progress[1]:
                self.last_progress = at, progress
            require(len(self.samples) < CONTROL_SAMPLES, 'Latency controls exceeded10000records')
            self.samples.append({'at': at, 'progress_ms': progress, 'native_blocks': health['native_blocks']})
            self.check_pcm()
        except Exception as exc:
            self.error = str(exc)[:2000]
            raise

    async def _watch(self):
        while not self.done.is_set():
            await self._sample()
            try:
                await asyncio.wait_for(self.done.wait(), .06)
            except asyncio.TimeoutError:
                pass

    async def check(self):
        self.check_pcm()
        if self.watcher is not None and self.watcher.done():
            self.watcher.result()
            raise RuntimeFailure('Untouched latency observer ended unexpectedly')

    def receipt(self):
        return {'source_owner': self.owner, 'original_unit_identities': self.units,
                'shared_sender_identities': self.sender_units, 'control_samples': list(self.samples),
                'original_capture_preserved': self.context.captures.get(self.room) is self.capture,
                'pcm': self.guard.evidence(), 'failure': self.error}

    async def close(self):
        self.done.set()
        if self.watcher is not None:
            self.watcher.cancel()
            result = await asyncio.gather(self.watcher, return_exceptions=True)
            if isinstance(result[0], Exception):
                self.error = str(result[0])[:2000]
        try:
            self.check_pcm()
        except Exception as exc:
            self.error = str(exc)[:2000]
        self.evidence['untouched'] = self.receipt()
        if self.error is not None:
            self.evidence['passed'] = False
        require(self.error is None, self.error or 'Untouched latency observer failed on close')


async def observed_wait(predicate, observer, seconds, message):
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        await observer.check()
        value = await asyncio.wait_for(predicate(), max(0, deadline-time.monotonic()))
        await observer.check()
        if value:
            return value
        await asyncio.sleep(.03)
    raise RuntimeFailure(message)


def playback_closed(manifest):
    """Only the admitted target playback endpoint; never peer nodes/sub0/sub2."""
    require(manifest['device'] in (0, 1) and manifest['subdevice'] == 7, 'Latency node is outside virtual sub7')
    path = Path(f"/proc/asound/card{manifest['card_index']}/pcm{manifest['device']}p/sub7/status")
    require(path.read_text().strip() == 'closed', 'Target playback PCM is open before cold/calibration setup')


def fit_marker(data):
    require(bool(data) and len(data) % 4 == 0, 'Latency marker contains malformed stereo frames')
    stereo = np.frombuffer(data, dtype='<i2').reshape(-1, 2).astype(float)
    mono = stereo[:, 0]
    require(RATE//100 <= len(mono) <= 960, 'Latency marker block is outside10..20ms')
    t = np.arange(len(mono))/RATE
    basis = np.column_stack([part for hz in (440, 880, 1320)
                             for part in (np.sin(2*np.pi*hz*t), np.cos(2*np.pi*hz*t))]+[np.ones(len(mono))])
    fitted = np.linalg.lstsq(basis, mono, rcond=None)[0]
    return {'music': float(np.hypot(*fitted[:2])), 'voice': float(np.hypot(*fitted[2:4])),
            'foreign': float(np.hypot(*fitted[4:6])), 'peak': float(np.max(np.abs(stereo))),
            'frames': len(mono)}


class MarkerGate:
    """Consecutive real voice/silence, retaining first actual clock observations."""
    def __init__(self, *, audible, trigger_ns):
        self.audible, self.trigger_ns = audible, trigger_ns
        self.frames = self.blocks = self.examined = 0
        self.first_absolute = self.first_callback = self.last_absolute = None
        self.last = None
        self.passed = False
        self.voice_maximum = 0.
        self.code_levels = []

    def push(self, data, absolute_ns, callback_at):
        require(type(absolute_ns) is int and absolute_ns > 0
                and (self.last_absolute is None or absolute_ns > self.last_absolute), 'Marker clock went backwards')
        self.last_absolute = absolute_ns
        self.last = fit_marker(data)
        self.examined += 1
        self.passed = False
        # Silence/capture data from before the emitted marker is not onset.
        if absolute_ns < self.trigger_ns:
            return False
        value = self.last
        require(value['peak'] < 32760, 'Actual idle speech marker clipped')
        matches = value['voice'] > 100 if self.audible else value['peak'] <= 4
        if not matches:
            self.frames = self.blocks = 0
            self.first_absolute = self.first_callback = None
            self.code_levels = []
            self.voice_maximum = 0.
            return False
        if self.first_absolute is None:
            self.first_absolute, self.first_callback = absolute_ns, callback_at
        self.frames += value['frames']
        self.blocks += 1
        if self.audible:
            self.voice_maximum = max(self.voice_maximum, value['voice'])
            level = ('high' if value['voice'] >= self.voice_maximum*.85 else
                     'low' if value['voice'] <= self.voice_maximum*.75 else None)
            if level is not None and (not self.code_levels or self.code_levels[-1] != level):
                self.code_levels.append(level)
            require(len(self.code_levels) <= 200, 'Idle marker exceeded its bounded code observation')
        self.passed = (self.blocks >= 3 and self.frames >= RATE*.4
                       and (not self.audible or len(self.code_levels) >= 3))
        return self.passed

    def receipt(self):
        return {'passed': self.passed, 'audible': self.audible, 'trigger_monotonic_ns': self.trigger_ns,
                'first_matching_absolute_pts_ns': self.first_absolute,
                'first_matching_callback_monotonic_ns': round(self.first_callback*1e9) if self.first_callback else None,
                'encoder_to_final_pts_seconds': (self.first_absolute-self.trigger_ns)/1e9 if self.first_absolute else None,
                'encoder_to_final_callback_seconds': self.first_callback-self.trigger_ns/1e9 if self.first_callback else None,
                'consecutive_frames': self.frames, 'examined_blocks': self.examined, 'last_spectrum': self.last,
                'coded_120ms_levels': self.code_levels[:], 'voice_maximum': self.voice_maximum,
                'cold_utterance_completeness_passed': False,
                'cold_utterance_completeness_status': 'pending_finite_opus_prefix_tail_reference',
                'scope': 'First qualifying coded run in final digital capture vs source frame handed to Opus encoder; includes encode/network/decode/tick, not first nonzero utterance or acoustic latency'}


def make_peer():
    """No RTP before explicit release; warm markers retain the same Opus peer."""
    from aiortc import AudioStreamTrack, RTCConfiguration, RTCPeerConnection, RTCRtpSender
    from av import AudioFrame

    class MarkerTrack(AudioStreamTrack):
        def __init__(self):
            super().__init__()
            self.gate = asyncio.Event()
            self.samples = 0
            self.started = None
            self.audible = False
            self.utterance = 0
            self.pending_trigger = False
            self.triggers = []
            self.marker_start_frame = 0

        def release(self):
            self.utterance += 1
            self.pending_trigger = True
            self.audible = True
            self.gate.set()

        async def recv(self):
            await self.gate.wait()
            if self.started is None:
                self.started = time.monotonic()
            await asyncio.sleep(max(0, self.started+self.samples/RATE-time.monotonic()))
            if self.pending_trigger:
                self.triggers.append({'utterance': self.utterance, 'first_emitted_monotonic_ns': time.monotonic_ns(),
                                      'rtp_frame_index': self.samples})
                self.marker_start_frame = self.samples
                self.pending_trigger = False
            positions = self.samples+np.arange(960)
            gains = np.where(((positions-self.marker_start_frame)//5760) % 2, .65, 1.)
            values = (600*gains*np.sin(2*np.pi*880*positions/RATE)).astype('<i2') if self.audible else np.zeros(960, dtype='<i2')
            frame = AudioFrame(format='s16', layout='mono', samples=960)
            frame.planes[0].update(values.tobytes())
            frame.sample_rate, frame.pts, frame.time_base = RATE, self.samples, Fraction(1, RATE)
            self.samples += 960
            return frame
    peer, tone = RTCPeerConnection(RTCConfiguration(iceServers=[])), MarkerTrack()
    transceiver = peer.addTransceiver(tone, direction='sendonly')
    opus = [codec for codec in RTCRtpSender.getCapabilities('audio').codecs if codec.mimeType.lower() == 'audio/opus']
    require(bool(opus), 'Latency sender lacks real Opus')
    transceiver.setCodecPreferences(opus)
    return peer, tone


def start_capture(context, manifest):
    capture = context.capture_factory(f"hw:{manifest['card_index']},{1-manifest['device']},7",
                                      context.clock, context.base_time, context.clock_offset)
    context.captures[context.target] = capture
    context.pcm_guards[context.target] = context.guard_factory(capture)
    capture.validation_rows = []
    capture.start()
    return capture


async def marker_gate(context, observer, gate, record, *, index, seconds=12):
    capture = context.captures[context.target]
    async def ready():
        nonlocal index
        capture.poll()
        while index < len(capture.chunks):
            at = capture.captured_at[index]
            complete = gate.push(capture.chunks[index], capture.absolute[at], at)
            index += 1
            if complete:
                return index
        return None
    try:
        return await observed_wait(ready, observer, seconds, 'Actual idle final PCM did not establish its marker/silence window')
    finally:
        record.update(gate.receipt())


WARM_DIAGNOSTIC_SECONDS = 2


async def retain_warm_transition(context, state, peer, tone, row, player):
    """Bounded diagnostics cannot replace the original observed player failure."""
    row['post_silence_player'] = deepcopy(player)
    row['post_silence_observed_monotonic_ns'] = time.monotonic_ns()
    row['post_silence_final_pcm_guard'] = context.pcm_guards[context.target].evidence()
    row['post_silence_sender'] = {
        'samples': getattr(tone, 'samples', None), 'utterance': getattr(tone, 'utterance', None),
        'audible': getattr(tone, 'audible', None), 'triggers': deepcopy(getattr(tone, 'triggers', [])),
        'track_ready_state': getattr(tone, 'readyState', None),
        'peer_connection_state': peer.connectionState}
    async def health():
        try:
            row['post_silence_worker_health'] = await asyncio.wait_for(call_rpc(
                context.broker._worker_socket(state), 'health', {}, timeout=WARM_DIAGNOSTIC_SECONDS),
                WARM_DIAGNOSTIC_SECONDS)
        except Exception as exc:
            row['post_silence_worker_health_error'] = {'type': type(exc).__name__, 'message': str(exc)[:1000]}
    async def sender_stats():
        try:
            stats = await asyncio.wait_for(peer.getStats(), WARM_DIAGNOSTIC_SECONDS)
            row['post_silence_outbound_rtp_stats'] = [
                {key: (value.isoformat() if hasattr(value, 'isoformat') else value)
                 for key in ('id', 'type', 'timestamp', 'ssrc', 'kind', 'transportId', 'packetsSent', 'bytesSent')
                 if (value := getattr(item, key, None)) is not None}
                for item in stats.values() if getattr(item, 'type', None) == 'outbound-rtp']
        except Exception as exc:
            row['post_silence_outbound_rtp_stats_error'] = {'type': type(exc).__name__, 'message': str(exc)[:1000]}
    await asyncio.gather(health(), sender_stats())
    row['post_silence_diagnostics_finished_monotonic_ns'] = time.monotonic_ns()


async def idle_rows(context, observer, configuration, rows):
    from aiortc import RTCSessionDescription
    peer, tone = make_peer()
    identity = {'session_id': str(uuid4()), 'request_id': str(uuid4())}
    state = context.states[context.target]
    capture = start_capture(context, configuration['pin'])
    primary = None
    try:
        await peer.setLocalDescription(await peer.createOffer())
        answer = await context.api.request('POST', f'/api/v1/rooms/{context.target}/speech',
            json={**identity, 'action': 'offer', 'sdp': peer.localDescription.sdp, 'type': 'offer'})
        require(answer.get('admitted_room_id') == context.target, 'Idle speech was admitted to a different room')
        await peer.setRemoteDescription(RTCSessionDescription(sdp=answer['sdp'], type=answer['type']))
        async def connected():
            require(peer.connectionState not in {'failed', 'closed'}, 'Idle peer failed before marker release')
            return peer.connectionState == 'connected'
        await observed_wait(connected, observer, 8, 'Real Opus peer did not connect before explicit marker')
        playback_closed(configuration['pin'])
        require(tone.samples == 0, 'Cold peer emitted RTP before explicit marker release')
        for mode in ('cold_idle', 'warm_idle'):
            row = matrix_row(rows, mode, configuration['saved_offset_ms'])
            capture.validation_rows.append(row)
            row['same_session_for_cold_and_warm'] = True
            before = len(capture.chunks)
            tone.release()
            async def emitted():
                return tone.triggers[-1] if tone.triggers and tone.triggers[-1]['utterance'] == tone.utterance else None
            emitted_marker = await observed_wait(emitted, observer, 2, 'Opus marker was not emitted')
            row['encoder_marker'] = emitted_marker
            await marker_gate(context, observer, MarkerGate(audible=True,
                trigger_ns=emitted_marker['first_emitted_monotonic_ns']), row, index=before)
            require({name: unit.identity() for name, unit in state.processes.items()} == configuration['units'],
                    'Idle speech reconnected a target service')
            tone.audible = False
            silence = {}
            await marker_gate(context, observer, MarkerGate(audible=False, trigger_ns=time.monotonic_ns()),
                              silence, index=len(capture.chunks))
            row['following_silence'] = silence
            player = await state.client.request('GET', '/api/player')
            # Retain the observed failed transition before the unchanged gate.
            await retain_warm_transition(context, state, peer, tone, row, player)
            require(player['state'] == 'play', 'Silent connected peer lost the warm OwnTone path')
            if mode == 'warm_idle':
                require(player['item_id'] == configuration['warm_player']['item_id'],
                        'Warm marker changed the existing OwnTone program item')
            configuration['warm_player'] = player
            row.update(passed=True, status='complete')
        return {'session_id': identity['session_id'], 'capture': capture.poll()}
    except BaseException as exc:
        primary = exc
        raise
    finally:
        tone.stop()
        close_errors = []
        try:
            await context.api.request('POST', f'/api/v1/rooms/{context.target}/speech',
                json={**identity, 'action': 'close', 'request_id': str(uuid4())})
        except Exception as exc:
            close_errors.append(f'API close:{type(exc).__name__}')
        try:
            await asyncio.wait_for(peer.close(), 4)
            require(peer.connectionState == 'closed', 'Idle peer survived close')
        except Exception as exc:
            close_errors.append(f'RTC close:{type(exc).__name__}')
        configuration['idle_cleanup_errors'] = close_errors
        if primary is None:
            require(not close_errors, 'Idle latency peer cleanup failed')


def matrix_row(rows, kind, offset_ms):
    matches = [row for row in rows if row['kind'] == kind and row['offset_ms'] == offset_ms]
    require(len(matches) == 1 and matches[0]['status'] == 'pending', 'Latency row identity was repeated or changed')
    matches[0]['status'] = 'running'
    return matches[0]
