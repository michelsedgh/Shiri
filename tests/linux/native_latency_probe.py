"""Opt-in digital latency matrix. Importing creates no devices or processes.

This fixture changes only a disabled, exact disposable room's output offset.
Its other room retains the original source, final-PCM and control observer.
Idle speech measures encoder-to-final-output latency, not a guessed mixer PTS.
Native rows use their declared calendar and a separate kernel capture baseline.
"""
from __future__ import annotations

# Bounded manual proc/config observations, separate from production code.
# ruff: noqa: ASYNC240

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
import importlib.util
import json
from pathlib import Path
import time
from uuid import uuid4

import numpy as np

from shiri.rpc import call_rpc
from shiri.runtime.system import RuntimeFailure, root_directory

RATE = 48000
PRODUCER_SECONDS = 480
CAPTURE_BYTES = 128 * 1024 * 1024
PHASE_SECONDS = 300
CONTROL_SAMPLES = 10000
INNER_SECONDS = 480
EXTERNAL_SECONDS = 600
OFFSETS = (-2000, 0, 2000)
ADVERSE_LEAD_NS = -1_950_000_000


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


@dataclass
class LatencyContext:
    api: object
    broker: object
    states: dict
    captures: dict
    producers: dict
    pcm_guards: dict
    report: dict
    temporary: Path
    clock: object
    base_time: int
    clock_offset: int
    capture_factory: object
    guard_factory: object
    launch_producer: object
    publish_command: object
    producer_status: object
    onset_index: object
    retain_capture: object
    handoff: object
    target: str
    untouched: str
    group: object
    binding: str
    declared_horizon_ns: int
    frozen_timing: object | None = None


def require_candidate_timing(context):
    frozen = getattr(context, 'frozen_timing', None)
    require(context.declared_horizon_ns == 1_000_000_000
            and type(frozen) is getattr(getattr(context, 'group', None), 'FrozenWorkerTiming', None)
            and frozen.horizon_ns == context.declared_horizon_ns
            and frozen.buffers_ms == tuple((identifier, 500) for identifier in sorted(context.states)),
            'H1 latency requires the exact immutable measured H1000/B500 context plan')
    return frozen


@lru_cache(maxsize=1)
def legacy_group_profile():
    spec = importlib.util.spec_from_file_location('native_group_legacy_forecast', Path(__file__).with_name('native_group_legacy_profile.py'))
    profile = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(profile)
    return profile


def require_offset_plan(context, offset_ms):
    """Refuse a predicted plan transition before touching either live room."""
    frozen = require_candidate_timing(context)
    require(type(offset_ms) is int and offset_ms in OFFSETS, 'Latency offset is outside its explicit fixture matrix')
    definitions = []
    for identifier, state in context.states.items():
        desired = state.desired
        if identifier == context.target:
            require(len(desired.speakers) == 1 and desired.speakers[0].id == '0',
                    'Candidate offset forecast requires the exact selected local0 intent')
            desired = desired.model_copy(update={'enabled': True, 'speakers': [
                desired.speakers[0].model_copy(update={'offset_ms': offset_ms})]})
        definitions.append(desired)
    try:
        plan = legacy_group_profile().plan(definitions)
    except ValueError as exc:
        raise RuntimeFailure('Candidate offset forecast has invalid saved intent') from exc
    require(plan.common_horizon_ns == frozen.horizon_ns
            and tuple((room.room_id, room.output_buffer_ms) for room in plan.rooms) == frozen.buffers_ms,
            'Offset matrix would change the frozen common plan and restart the untouched room; separate epoch redesign is pending')


def adverse_parameters(context, configuration):
    """Isolate a missed initial anchor while the candidate final deadline is future."""
    if getattr(context, 'frozen_timing', None) is not None:
        require_candidate_timing(context)
    if context.declared_horizon_ns == 1_000_000_000:
        frozen = require_candidate_timing(context)
        health = configuration.get('cold_health', {})
        buffer_ms = dict(frozen.buffers_ms)[context.target]
        require(health.get('ready') is True and health.get('source', {}).get('owner') is None
                and health.get('source', {}).get('ready') is True
                and type(health.get('output_buffer_ms')) is int and health['output_buffer_ms'] == buffer_ms
                and type(health.get('timing_relay_delay_ms')) is int
                and health['timing_relay_delay_ms']*1_000_000 == frozen.horizon_ns,
                'Candidate adverse anchor requires actual cold worker B/H and its immutable plan')
        lead_ns = -(frozen.horizon_ns-buffer_ms*1_000_000+250_000_000)
        require(frozen.horizon_ns+lead_ns > 0, 'Adverse arrival must precede the actual final presentation')
        return buffer_ms, lead_ns, 'actual cold worker health and frozen candidate plan'
    require(context.declared_horizon_ns in (3_000_000_000, 4_000_000_000),
            'Adverse legacy experiment requires its explicit3s or4s declaration')
    return 2250, ADVERSE_LEAD_NS, 'legacy explicitly pinned2250ms buffer experiment'


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


async def close_capture(context, label):
    capture = context.captures[context.target]
    guard = context.pcm_guards[context.target]
    require(guard.capture is capture, 'Target capture lost its exact content guard before shutdown')
    try:
        capture.poll()
        guard.check()  # Idle/adverse guards remain deliberately unarmed.
        stop_requested = time.monotonic_ns()
        await asyncio.wait_for(asyncio.to_thread(capture.close), 3)
        require(capture.pipeline.get_state(0).state == capture.Gst.State.NULL, 'Target capture failed to close')
        null_verified = time.monotonic_ns()
        capture.observation_stop = {'requested_monotonic_ns': stop_requested,
                                    'null_verified_monotonic_ns': null_verified,
                                    'transition_seconds': (null_verified-stop_requested)/1e9}
        for row in getattr(capture, 'validation_rows', ()):
            row['capture_stop_boundary'] = dict(capture.observation_stop)
        # set_state(NULL) joins streaming callbacks. Validate their final queue
        # with both the SAME caps/offset and any armed native-music fence.
        capture.poll()
        guard.check(live=False)
    except Exception as exc:
        for row in getattr(capture, 'validation_rows', ()):
            row.update(passed=False, status='failed', capture_tail_error=type(exc).__name__)
        raise
    artifact = context.retain_capture(capture, context.temporary, label)
    artifact['capture_stop_boundary'] = dict(capture.observation_stop)
    return artifact


async def disable_target(context, observer, record):
    state = context.states[context.target]
    await context.api.patch(context.target, {'enabled': False})
    async def stopped():
        room = await context.api.room(context.target)
        if room['runtime']['status'] != 'stopped' or state.processes or state.local_pin is not None:
            return False
        require(not state.desired.enabled and not any(key.startswith(context.target+':')
                for key in context.broker.network.manifest['processes']), 'Stopped target retains an owned reservation')
        return True
    await observed_wait(stopped, observer, 30, 'Target did not release all owned units before offset change')
    context.producers.pop(context.target, None)  # Only after actual owned shutdown.
    playback_closed(record['pin'])
    record['disabled_node_closed'] = True


async def set_offset(context, observer, offset_ms, record):
    require(offset_ms in OFFSETS and not context.states[context.target].desired.enabled, 'Offset mutation requires disabled exact target')
    room = await context.api.room(context.target)
    require(room['enabled'] is False, 'Durable target is enabled before offset mutation')
    reply = await context.api.request('PATCH', f'/api/v1/rooms/{context.target}/speakers/0/offset',
        json={'expected_revision': room['revision'], 'offset_ms': offset_ms})
    require(reply.get('runtime_accepted') is True, 'Offset intent lacks actual broker acceptance')
    await observer.check()
    saved = await context.api.room(context.target)
    require(len(saved['speakers']) == 1 and saved['speakers'][0]['id'] == '0'
            and saved['speakers'][0]['offset_ms'] == offset_ms, 'Saved local0 offset differs from exact API mutation')
    record['saved_offset_ms'] = offset_ms


async def ready_target(context, observer, record):
    if getattr(context, 'frozen_timing', None) is not None:
        require_candidate_timing(context)
    if context.declared_horizon_ns == 1_000_000_000:
        require_offset_plan(context, record['saved_offset_ms'])
    await context.api.patch(context.target, {'enabled': True})
    state = context.states[context.target]
    async def ready():
        room = await context.api.room(context.target)
        require(room['runtime']['status'] not in {'error', 'degraded'}, 'Target startup faulted during latency setup')
        if state.status != 'running':
            return None
        actual = await state.client.outputs(set())
        selected = [value for value in actual if value['selected']]
        if state.selected_ids != ['0'] or len(selected) != 1 or selected[0]['id'] != '0':
            return None
        require(type(selected[0].get('offset_ms')) is int and selected[0]['offset_ms'] == record['saved_offset_ms'],
                'Actual selected local0 does not report the saved timing correction')
        state.local_pin.validate()
        require(state.local_pin.manifest == record['pin'], 'Offset setup changed the exact virtual playback instance')
        health = await call_rpc(context.broker._worker_socket(state), 'health', {}, timeout=2)
        require(health['ready'] and health['source']['ready'] and health['source']['owner'] is None
                and health['timing_relay_delay_ms']*1_000_000 == context.declared_horizon_ns,
                'Cold target does not have the declared ready idle timeline')
        if context.declared_horizon_ns == 1_000_000_000:
            frozen = require_candidate_timing(context)
            require(type(health.get('output_buffer_ms')) is int
                    and health['output_buffer_ms'] == dict(frozen.buffers_ms)[context.target],
                    'Cold target changed its frozen actual output buffer')
        playback_closed(record['pin'])
        record.update(cold_health=health, selected_output=selected[0],
                      units={name: unit.identity() for name, unit in state.processes.items()})
        return health
    return await observed_wait(ready, observer, 65, 'Cold target did not become exactly ready/selected within production startup bound')


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


async def replace_receiver(context, observer, label, *, lead_ns=220_000_000):
    state, broker = context.states[context.target], context.broker
    monitor = broker._monitor
    require(monitor is not None and not monitor.done(), 'Production health monitor is absent before fixture preparation')
    preparation = {'started_monotonic_ns': time.monotonic_ns(), 'only_synthetic_receiver_replacement': True}
    context.report['latency_probe']['preparations'].append(preparation)
    try:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        await observer.check()
        root = state.directory/'native-validation-latency'/f'{label}-{uuid4().hex}'
        root_directory(root)
        timing_arguments = {}
        if lead_ns == -750_000_000:
            timing_arguments['frozen_timing'] = require_candidate_timing(context)
        producer = await context.launch_producer(broker, state, root, 0, duration_seconds=PRODUCER_SECONDS,
                                                 arrival_lead_ns=lead_ns, **timing_arguments)
        context.producers[context.target] = producer
    finally:
        broker._monitor = asyncio.create_task(broker._health_monitor(), name='native-latency-runtime-health')
        preparation['monitor_restored_monotonic_ns'] = time.monotonic_ns()
    async def granted():
        value = context.producer_status(producer)
        return value if value and value['stage'] in {'granted', 'streaming'} else None
    grant = await observed_wait(granted, observer, 5, 'Exact latency synthetic receiver was not granted')
    return producer, grant


def calendar_receipt(displacement, baseline):
    """Record before rejecting; speaker correction already belongs in P+H+O."""
    return {'raw_displacement_ms': displacement['relative_offset_ms'],
            'independent_capture_offset_ms': baseline['capture_offset_ms'],
            'corrected_horizon_error_ms': displacement['relative_offset_ms']-baseline['capture_offset_ms'],
            'independent_half_width_ms': baseline['horizon_half_width_ms'],
            'relative_alignment_scope': 'Initial retained two-zone coded baseline only; this row has an independent absolute calendar'}


def startup_rejection(journal, start_ns):
    """A missing tone is insufficient; require an actual scoped backend event."""
    rejected = [record for record in journal.get('records', [])
                if 'Native input missed or lost its initial presentation anchor' in record.get('text', '')
                and record.get('__MONOTONIC_TIMESTAMP', '').isdigit()
                and int(record['__MONOTONIC_TIMESTAMP'])*1000 >= start_ns]
    require(journal.get('invocation_scoped') is True and rejected,
            'Adverse startup rejection lacks actual current-invocation backend evidence')
    return [record['__MONOTONIC_TIMESTAMP'] for record in rejected]


def matrix_row(rows, kind, offset_ms):
    matches = [row for row in rows if row['kind'] == kind and row['offset_ms'] == offset_ms]
    require(len(matches) == 1 and matches[0]['status'] == 'pending', 'Latency row identity was repeated or changed')
    matches[0]['status'] = 'running'
    return matches[0]


async def native_row(context, observer, configuration, rows):
    row = matrix_row(rows, 'native_calendar', configuration['saved_offset_ms'])
    row['idle_final_pcm'] = await close_capture(context, f'latency-{row["offset_ms"]}-idle')
    setup = {'pin': deepcopy(configuration['pin']), 'saved_offset_ms': configuration['saved_offset_ms']}
    row['cold_native_setup'] = setup
    await disable_target(context, observer, setup)
    await ready_target(context, observer, setup)
    state = context.states[context.target]
    async def idle():
        health = await call_rpc(context.broker._worker_socket(state), 'health', {}, timeout=2)
        return health if health['speech_session_id'] is None and health['source']['owner'] is None else None
    await observed_wait(idle, observer, 8, 'Speech did not release before a deliberate native source')
    producer, grant = await replace_receiver(context, observer, f'offset-{row["offset_ms"]}')
    capture = start_capture(context, configuration['pin'])
    capture.validation_rows.append(row)
    start = time.monotonic_ns()+1_000_000_000
    expected = start+context.declared_horizon_ns+row['offset_ms']*1_000_000
    context.publish_command(producer['command'], {'generation': 2, 'action': 'run', 'common_start_ns': start}, producer['account'])
    row.update(common_presentation_ns=start, declared_final_origin_ns=expected, native_grant=grant)
    deadline = (expected+6_000_000_000)/1e9
    while time.monotonic() < deadline:
        await observer.check()
        capture.poll()
        await asyncio.sleep(.02)
    snapshot = context.group.capture_snapshot(capture)
    row['frame_continuity'] = snapshot.frame_continuity
    first, last = expected+500_000_000, expected+5_500_000_000
    reference = context.group.declared_capture(expected, RATE//2, RATE*11//2)
    series = await asyncio.gather(asyncio.to_thread(context.group.modulation, reference, first, last),
                                  asyncio.to_thread(context.group.modulation, snapshot, first, last))
    displacement = await asyncio.to_thread(context.group.measure_alignment, *series, search_ms=1000)
    row['alignment'] = displacement
    row['horizon'] = calendar_receipt(displacement, configuration['capture_baseline'])
    context.group.validate_alignment(displacement, maximum_delay_ms=None)
    require(abs(row['horizon']['corrected_horizon_error_ms']) <= configuration['capture_baseline']['horizon_half_width_ms'],
            'Native latency row missed its independently bounded absolute calendar')
    # After actual onset, complete blocks may never be lost/zeroed.
    index = context.onset_index(capture)
    require(index is not None, 'Native latency row lacks actual coded440 onset')
    guard = context.pcm_guards[context.target]
    guard.begin(index)
    guard.check()
    health = await call_rpc(context.broker._worker_socket(state), 'health', {}, timeout=2)
    require(health['source']['owner']['session_id'] == grant['session_id'] and health['source']['ready']
            and health['native_blocks'] > 0 and health['dropped_bytes'] == 0,
            'Native calendar PCM does not have the exact admitted owner/clock')
    row.update(passed=True, status='complete', final_health=health, capture=capture.poll())
    return row


async def adverse_row(context, observer, configuration, row):
    """A legal mapping can still miss OwnTone's cold initial scheduling anchor."""
    row['status'] = 'running'
    state = context.states[context.target]
    buffer_ms, lead_ns, buffer_scope = adverse_parameters(context, configuration)
    producer, grant = await replace_receiver(context, observer, 'adverse-lead', lead_ns=lead_ns)
    capture = start_capture(context, configuration['pin'])
    capture.validation_rows.append(row)
    start = time.monotonic_ns()+500_000_000
    context.publish_command(producer['command'], {'generation': 2, 'action': 'run', 'common_start_ns': start}, producer['account'])
    row.update(common_presentation_ns=start, arrival_lead_ns=lead_ns,
               declared_final_origin_ns=start+context.declared_horizon_ns,
               output_buffer_ms=buffer_ms, output_buffer_scope=buffer_scope,
               own_initial_anchor_ns=start+context.declared_horizon_ns-buffer_ms*1_000_000,
               earliest_declared_arrival_ns=start-lead_ns,
               startup_slack_ns=context.declared_horizon_ns-buffer_ms*1_000_000+lead_ns,
               native_grant=grant,
               scope='Strictly admitted RAW mapping vs cold OwnTone startup; no late-source or universal horizon guarantee')
    require(row['startup_slack_ns'] < 0, 'Adverse row no longer describes an initially unschedulable anchor')
    async def accepted():
        health = await call_rpc(context.broker._worker_socket(state), 'health', {}, timeout=2)
        if not health['native_blocks']:
            return None
        require(health['ready'] and health['source']['ready']
                and health['source']['owner']['session_id'] == grant['session_id'],
                'Adverse packet was not admitted to its exact healthy source')
        return health
    row['mapping_admitted_health'] = await observed_wait(accepted, observer, 5, 'Adverse mapping was not actually admitted')
    deadline = time.monotonic()+3
    while time.monotonic() < deadline:
        await observer.check()
        capture.poll()
        for data in capture.chunks:
            require(fit_marker(data)['music'] < 8, 'Initially late adverse native source unexpectedly produced final music')
        await asyncio.sleep(.03)
    # Absence of PCM alone cannot establish a rejection. Retain and inspect
    # the exact admitted OwnTone invocation's bounded sanitized journal.
    evidence = context.group.failure_evidence
    sources, errors = evidence.owned_sources(context.broker, context.states, context.producers)
    matching = [source for source in sources if source['key'] == f'{context.target}:owntone']
    require(len(matching) == 1, 'Cannot admit the exact adverse OwnTone journal')
    receipt = await evidence.capture(context.temporary/'latency-adverse-evidence', matching,
        private=(context.broker._password, context.broker._room_password(context.target)), admission_errors=errors)
    row['runtime_evidence'] = receipt
    saved = json.loads(Path(receipt['path']).read_text())
    journal = saved['units'][f'{context.target}:owntone'].get('journal', {})
    rejection_times = startup_rejection(journal, start)
    row.update(passed=True, status='complete', qualified_outcome='mapping_admitted_startup_rejected',
               actual_rejection_timestamps_us=rejection_times,
               capture=capture.poll())


async def exercise(context):
    require(context.target != context.untouched and set(context.states) == {context.target, context.untouched},
            'Latency matrix requires exactly two distinct rooms')
    require(context.declared_horizon_ns in (1_000_000_000, 3_000_000_000, 4_000_000_000),
            'Latency horizon must be an explicit measured1s candidate or isolated legacy3s/4s experiment')
    if getattr(context, 'frozen_timing', None) is not None:
        require_candidate_timing(context)
    rows = [{'kind': kind, 'offset_ms': offset, 'status': 'pending', 'passed': False}
            for offset in OFFSETS for kind in ('cold_idle', 'warm_idle', 'native_calendar')]
    evidence = {'passed': False, 'declared_horizon_ns': context.declared_horizon_ns, 'rows': rows,
                'configurations': [], 'preparations': [], 'phase_seconds': PHASE_SECONDS,
                'producer_seconds': PRODUCER_SECONDS, 'maximum_capture_bytes': CAPTURE_BYTES,
                'relative_group_alignment_baseline': deepcopy(context.report['group_alignment']),
                'relative_alignment_scope': 'Retained pre-matrix two-zone coded full/early/late2ms gate; B stays its original constant carrier during rows',
                'scope': 'Virtual digital route; idle encoder-to-output observations and exact native calendars. No phone/physical/Cast/Bluetooth or universal minimum-horizon proof.'}
    context.report['latency_probe'] = evidence
    if context.declared_horizon_ns == 1_000_000_000:
        evidence['frozen_worker_timing'] = require_candidate_timing(context).receipt()
        try:
            for offset_ms in OFFSETS:
                require_offset_plan(context, offset_ms)
        except RuntimeFailure:
            evidence['pending_epoch_redesign'] = True
            raise
    observer = UntouchedObserver(context, evidence)
    try:
        await observer.initialize()
        await context.handoff()
        async def matrix():
            for offset_ms in OFFSETS:
                state = context.states[context.target]
                configuration = {'pin': deepcopy(state.local_pin.manifest), 'offset_ms': offset_ms}
                evidence['configurations'].append(configuration)
                configuration['previous_capture'] = await close_capture(context, f'latency-before-{offset_ms}')
                await disable_target(context, observer, configuration)
                # Capture calibration uses only the independently owned closed
                # playback endpoint; configured output offset is NOT involved.
                root = context.temporary/f'latency-capture-baseline-{offset_ms}'
                root_directory(root)
                playback_closed(configuration['pin'])
                configuration['capture_baseline'] = await context.group.calibrate_capture(
                    context.broker, context.target, context.binding, root, context.clock, context.base_time, context.clock_offset)
                playback_closed(configuration['pin'])
                await set_offset(context, observer, offset_ms, configuration)
                await ready_target(context, observer, configuration)
                configuration['idle'] = await idle_rows(context, observer, configuration, evidence['rows'])
                await native_row(context, observer, configuration, evidence['rows'])
            # Restore a ready offset0 room and fresh exact synthetic receiver
            # before the established takeover/END tail resumes.
            state = context.states[context.target]
            restoration = {'pin': deepcopy(state.local_pin.manifest)}
            evidence['restoration'] = restoration
            restoration['last_capture'] = await close_capture(context, 'latency-final-offset-capture')
            await disable_target(context, observer, restoration)
            await set_offset(context, observer, 0, restoration)
            await ready_target(context, observer, restoration)
            adverse = {'kind': 'adverse_cold_native', 'offset_ms': 0, 'status': 'pending', 'passed': False}
            evidence['adverse'] = adverse
            await adverse_row(context, observer, restoration, adverse)
            adverse['final_pcm'] = await close_capture(context, 'latency-adverse')
            await disable_target(context, observer, restoration)
            await ready_target(context, observer, restoration)
            producer, grant = await replace_receiver(context, observer, 'restored-zero')
            capture = start_capture(context, restoration['pin'])
            start = time.monotonic_ns()+1_000_000_000
            context.publish_command(producer['command'], {'generation': 2, 'action': 'run', 'common_start_ns': start}, producer['account'])
            async def audible():
                capture.poll()
                index = context.onset_index(capture)
                if index is None:
                    return None
                guard = context.pcm_guards[context.target]
                guard.begin(index)
                guard.check()
                return True
            await observed_wait(audible, observer, 10, 'Restored zero-offset target did not produce fresh final music')
            restoration.update(passed=True, native_grant=grant, common_presentation_ns=start)
        await asyncio.wait_for(matrix(), PHASE_SECONDS)
        require(len(evidence['rows']) == 9 and all(row['passed'] for row in evidence['rows']), 'Latency matrix has incomplete normal rows')
        evidence.update(passed=True, untouched=observer.receipt())
        return observer
    except BaseException:
        try:
            await observer.close()
        except Exception as exc:
            evidence['observer_cleanup_error'] = type(exc).__name__
        raise
