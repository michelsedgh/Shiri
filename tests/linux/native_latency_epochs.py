"""Explicit fresh-lab latency epochs; importing opens no resources.

Six complete fixtures retain original actors inside each frozen plan. Each
native row includes cold/warm speech on advancing music. Actual software
evidence never claims physical output or phone interoperability.
"""
from __future__ import annotations

# Manual bounded fixture/kernel observations; no production mutations.
# ruff: noqa: ASYNC240

import asyncio
from copy import deepcopy
import hashlib
import json
import math
import time
from typing import NamedTuple
from uuid import UUID, uuid4

from aiortc import RTCSessionDescription

from shiri.domain import Room
from shiri.rpc import call_rpc
from shiri.runtime.latency import latency_plan
from shiri.runtime.system import RuntimeFailure, root_directory

A = 'b6786543-7eb2-443d-83b1-65b984123a76'
B = '5356832c-c514-4472-bb4b-4f34ed86dd07'
OFFSETS = (-2000, 0, 2000)
KINDS = ('cold_idle', 'warm_idle', 'native_calendar')
INNER_SECONDS = 480
MATRIX_SECONDS = 3000


def require(value, message):
    if not value:
        raise RuntimeFailure(message)


class Phase(NamedTuple):
    epoch_id: str
    offset_ms: int
    phase: str

    def receipt(self):
        require(type(self.epoch_id) is str and str(UUID(self.epoch_id)) == self.epoch_id
                and type(self.offset_ms) is int and self.offset_ms in OFFSETS
                and self.phase in {'idle', 'native'}, 'Invalid explicit latency epoch')
        return {'version': 1, 'id': self.epoch_id, 'offset_ms': self.offset_ms, 'phase': self.phase}


def phases():
    return tuple(Phase(str(uuid4()), offset, role) for offset in OFFSETS for role in ('idle', 'native'))


def saved_intent(definitions):
    return [Room.model_validate(room).model_dump(mode='json')
            for room in sorted(definitions, key=lambda room: room.id)]


def intent_digest(intent):
    return hashlib.sha256(json.dumps(intent, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def plan_receipt(phase, definitions, healths, *, before_pcm):
    phase.receipt()
    intent = saved_intent(definitions)
    enabled = [Room.model_validate(room) for room in intent if room['enabled']]
    require({room.id for room in enabled} == {A, B}, 'Epoch requires exactly the two enabled disposable rooms')
    for room in enabled:
        expected = phase.offset_ms if room.id == A else 0
        require(len(room.speakers) == 1 and room.speakers[0].id == '0'
                and room.speakers[0].protocol == 'alsa' and room.speakers[0].offset_ms == expected,
                'Epoch saved local0 offset/protocol differs from its declared configuration')
    plan = latency_plan([Room.model_validate(room) for room in intent])
    require(set(healths) == {A, B}, 'Epoch worker set differs from all enabled rooms')
    for decision in plan.rooms:
        health = healths[decision.room_id]
        source = health.get('source') if type(health) is dict else None
        require(type(health) is dict and health.get('ready') is True
                and 'error' in health and health['error'] is None
                and type(source) is dict and source.get('ready') is True and 'owner' in source,
                'Epoch worker/source is not exactly healthy')
        require(type(health.get('timing_relay_delay_ms')) is int
                and health['timing_relay_delay_ms']*1_000_000 == plan.common_horizon_ns
                and type(health.get('output_buffer_ms')) is int
                and health['output_buffer_ms'] == decision.output_buffer_ms,
                'Actual epoch worker H/B differs from reviewed saved plan')
        if before_pcm:
            require(source['owner'] is None and type(health.get('native_blocks')) is int
                    and health['native_blocks'] == 0, 'Epoch plan freeze must precede owner/producer PCM')
    return {'common_horizon_ns': plan.common_horizon_ns,
            'output_buffers_ms': {item.room_id: item.output_buffer_ms for item in plan.rooms},
            'saved_intent': intent, 'intent_sha256': intent_digest(intent),
            'scope': 'Actual all-enabled worker H/B agrees with exact saved local0 definitions before producer PCM'}


async def worker_health(context):
    enabled = {identifier: state for identifier, state in context.broker.rooms.items() if state.desired.enabled}
    require(set(enabled) == {A, B}, 'Another enabled room entered the isolated epoch')
    return {identifier: await call_rpc(context.broker._worker_socket(state), 'health', {}, timeout=2)
            for identifier, state in enabled.items()}


async def prepare(context):
    """All plan-driven restarts finish before source or capture admission."""
    phase = context.phase
    phase.receipt()
    require(context.group.NATIVE_LAB is not None and not context.producers and not context.captures,
            'Epoch preparation requires explicit fresh lab and no old source/capture actors')
    old_pin = deepcopy(context.states[A].local_pin.manifest)
    await context.api.patch(A, {'enabled': False})
    async def stopped():
        state = context.broker.rooms[A]
        room = await context.api.room(A)
        return (room['runtime']['status'] == 'stopped' and not state.processes and state.local_pin is None
                and not any(key.startswith(A+':') for key in context.broker.network.manifest['processes']))
    await context.group.observation.base.eventually(stopped, 'exact unmeasured A stopped for final offset preparation', timeout=30)
    context.latency.playback_closed(old_pin)
    room = await context.api.room(A)
    reply = await context.api.request('PATCH', f'/api/v1/rooms/{A}/speakers/0/offset',
        json={'expected_revision': room['revision'], 'offset_ms': phase.offset_ms})
    require(reply.get('runtime_accepted') is True, 'Final epoch offset lacked actual broker acceptance')
    await context.api.patch(A, {'enabled': True})
    async def settled():
        healths = {}
        refreshed = {}
        for identifier in (A, B):
            state = context.broker.rooms[identifier]
            room = await context.api.room(identifier)
            require(room['runtime']['status'] not in {'error', 'degraded'}, 'Epoch setup faulted before PCM')
            if state.status != 'running' or state.local_pin is None:
                return None
            actual = await state.client.outputs(set())
            selected = [item for item in actual if item['selected']]
            expected = phase.offset_ms if identifier == A else 0
            if state.selected_ids != ['0'] or len(selected) != 1 or selected[0]['id'] != '0':
                return None
            require(type(selected[0].get('offset_ms')) is int and selected[0]['offset_ms'] == expected,
                    'Actual epoch local output offset differs from its saved declaration')
            state.local_pin.validate()
            pin = state.local_pin.manifest
            require(pin['device'] == context.group.ZONES[identifier]['device'] and pin['subdevice'] == 7,
                    'Epoch output escaped its exact virtual endpoint')
            healths[identifier] = await call_rpc(context.broker._worker_socket(state), 'health', {}, timeout=2)
            if healths[identifier].get('ready') is not True:
                return None
            refreshed[identifier] = state
        frozen = plan_receipt(phase, [state.desired for state in context.broker.rooms.values()], healths, before_pcm=True)
        context.states.clear()
        context.states.update(refreshed)
        return frozen
    frozen = await context.group.observation.base.eventually(settled, 'all final plan workers after coordinated startup', timeout=65)
    context.frozen_plan = frozen
    context.declared_horizon_ns = frozen['common_horizon_ns']
    context.evidence = {**phase.receipt(), 'passed': False, 'frozen_plan': deepcopy(frozen),
                        'measured_signal_timeline_cleanup_passed': False,
                        'speech_latency_performance_passed': False,
                        'speech_latency_performance_status': 'not_completed',
                        'cold_utterance_completeness_passed': False,
                        'cold_utterance_completeness_status': 'not_completed',
                        'rows': [{'kind': kind, 'offset_ms': phase.offset_ms, 'epoch_id': phase.epoch_id,
                                  'status': 'pending', 'passed': False}
                                 for kind in (('cold_idle', 'warm_idle') if phase.phase == 'idle' else ('native_calendar',))],
                        'scope': 'Independently calibrated, freshly prepared digital epoch; no acoustic or phone claim'}
    context.evidence['speech_latency_budget'] = {
        'encoder_worker_overhead_ms': None, 'idle_startup_overhead_ms': None,
        'scope': 'No reviewed performance budget has been declared;12s bounds collection only'}
    context.report['latency_epoch'] = context.evidence
    context.report['latency_probe'] = {'preparations': []}  # Existing protected producer helpers retain evidence here.
    context.evidence['independent_capture_baselines'] = deepcopy(context.report['independent_capture_baselines'])
    return frozen


class EpochObserver:
    """Original B observer plus frozen plan and exact target music incarnation."""
    def __init__(self, context):
        self.context = context
        self.untouched = context.latency.UntouchedObserver(context, context.evidence)
        self.units = {identifier: {name: unit.identity() for name, unit in state.processes.items()}
                      for identifier, state in context.states.items()}
        require(self.units == context.evidence['initial_unit_identities'],
                'An epoch unit changed between first PCM and original observer admission')
        self.target_owner = None
        self.target_player = None
        self.target_progress = None
        self.previous_progress = None
        self.error = None
        self.watcher = None
        self.done = asyncio.Event()
        self.stopped = False

    async def initialize(self):
        if self.context.phase.phase == 'native':
            health = await call_rpc(self.context.broker._worker_socket(self.context.states[A]), 'health', {}, timeout=2)
            self.target_owner = deepcopy(health['source']['owner'])
            self.target_player = await self.context.states[A].client.request('GET', '/api/player')
            require(self.target_owner is not None and self.target_player['state'] == 'play', 'Native target has no original music')
            self.context.evidence['target_original'] = {'source_owner': deepcopy(self.target_owner),
                'player': deepcopy(self.target_player), 'unit_identities': deepcopy(self.units[A])}
        await self.untouched.initialize()
        await self.sample()
        self.watcher = asyncio.create_task(self.watch(), name='native-epoch-frozen-plan-observer')

    def pcm(self):
        require(self.error is None, self.error or 'Epoch observation failed permanently')
        for guard in self.context.pcm_guards.values():
            guard.check()

    async def sample(self):
        try:
            self.pcm()
            await self.untouched.check()
            context = self.context
            healths = await worker_health(context)
            observed = plan_receipt(context.phase, [state.desired for state in context.broker.rooms.values()], healths, before_pcm=False)
            require(observed == context.frozen_plan, 'Plan or saved intent changed within the original audio epoch')
            for identifier, state in context.states.items():
                require(context.broker.rooms.get(identifier) is state and state.status == 'running'
                        and {name: unit.identity() for name, unit in state.processes.items()} == self.units[identifier]
                        and all(unit.alive for unit in state.processes.values()), 'Epoch original room/unit changed')
                health = healths[identifier]
                require(type(health.get('dropped_bytes')) is int and health['dropped_bytes'] == 0
                        and type(health.get('speech_dropped_frames')) is int and health['speech_dropped_frames'] == 0
                        and health.get('speech_output_error') is None and health.get('speech_output_errno') is None,
                        'Epoch worker dropped or failed final speech/music delivery')
            health = healths[A]
            if context.phase.phase == 'idle':
                require(health['source']['owner'] is None, 'Idle target acquired an unrelated native source')
            else:
                require(health['source']['owner'] == self.target_owner, 'TTS changed original target native ownership')
                player = await context.states[A].client.request('GET', '/api/player')
                require(player['state'] == 'play' and player['item_id'] == self.target_player['item_id']
                        and player.get('volume') == context.states[A].current_volume == 100,
                        'TTS paused/replaced the original target program or volume')
                progress, now = player.get('item_progress_ms'), time.monotonic()
                require(type(progress) is int and progress >= 0, 'Target native NPT is invalid')
                require(self.previous_progress is None or progress >= self.previous_progress,
                        'Target native NPT moved backwards during TTS')
                self.previous_progress = progress
                if self.target_progress is None:
                    self.target_progress = now, progress
                else:
                    at, first = self.target_progress
                    require(abs(progress-first-(now-at)*1000) < 1500, 'Target native NPT stalled/jumped during TTS')
            self.pcm()
        except Exception as exc:
            self.error = str(exc)[:2000]
            raise

    async def watch(self):
        while not self.done.is_set():
            await self.sample()
            try:
                await asyncio.wait_for(self.done.wait(), .06)
            except asyncio.TimeoutError:
                pass

    async def check(self):
        self.pcm()
        await self.untouched.check()
        if self.watcher is not None and self.watcher.done():
            self.watcher.result()
            raise RuntimeFailure('Epoch plan watcher ended unexpectedly')

    async def close(self):
        if self.stopped:
            return
        try:
            await self.sample()
        except Exception as exc:
            self.error = str(exc)[:2000]
        self.done.set()
        if self.watcher:
            self.watcher.cancel()
            results = await asyncio.gather(self.watcher, return_exceptions=True)
            if results and isinstance(results[0], Exception):
                self.error = str(results[0])[:2000]
        try:
            await self.untouched.close()
        except Exception as exc:
            self.error = str(exc)[:2000]
        require(self.error is None, self.error or 'Epoch observer failed before retirement')
        requested = time.monotonic_ns()
        self.context.evidence['stop_boundary'] = {'requested_monotonic_ns': requested,
            'scope': 'Strict active source/control/content through fixture stop request; later PCM is bounded teardown'}
        for guard in self.context.pcm_guards.values():
            guard.end_at(requested)
        self.context.evidence['untouched'] = self.untouched.receipt()
        self.stopped = True


async def observed_wait(context, observer, predicate, message, timeout):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        if observer:
            await observer.check()
        value = await asyncio.wait_for(predicate(), max(.001, deadline-time.monotonic()))
        if value:
            return value
        await asyncio.sleep(.02)
    raise RuntimeFailure(message)


async def exercise(context):
    """A complete phase with no subsequent enabled/offset mutation."""
    group, latency = context.group, context.latency
    before = await worker_health(context)
    require(plan_receipt(context.phase, [s.desired for s in context.broker.rooms.values()], before, before_pcm=True)
            == context.frozen_plan, 'Epoch plan changed immediately before original receiver preparation')
    monitor = context.broker._monitor
    require(monitor is not None and not monitor.done(), 'Production health monitor is absent before epoch preparation')
    try:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        for identifier in ((B,) if context.phase.phase == 'idle' else (A, B)):
            state = context.states[identifier]
            root = state.directory/'native-validation-epoch'/context.phase.epoch_id
            root_directory(root)
            context.producers[identifier] = await group.launch_producer(context.broker, state, root, 0, duration_seconds=480)
    finally:
        context.broker._monitor = asyncio.create_task(context.broker._health_monitor(), name='native-epoch-runtime-health')
    async def granted():
        values = {identifier: group.producer_status(handle) for identifier, handle in context.producers.items()}
        return values if all(value and value['stage'] == 'granted' for value in values.values()) else None
    grants = await group.observation.base.eventually(granted, 'all exact epoch receiver grants without PCM', timeout=8)
    context.evidence['producer_grants'] = deepcopy(grants)
    units = {identifier: {name: unit.identity() for name, unit in state.processes.items()}
             for identifier, state in context.states.items()}
    context.evidence['initial_unit_identities'] = deepcopy(units)
    initial_players = {}
    for identifier in context.producers:
        pin = context.states[identifier].local_pin.manifest
        capture = context.capture_factory(f"hw:{pin['card_index']},{1-pin['device']},7", context.clock, context.base_time, context.clock_offset)
        context.captures[identifier] = capture
        context.pcm_guards[identifier] = group.FinalPcmGuard(capture)
        capture.start()
    start = time.monotonic_ns()+4_000_000_000
    context.evidence['common_presentation_ns'] = start
    for handle in context.producers.values():
        group.publish_command(handle['command'], {'generation': 2, 'action': 'run', 'common_start_ns': start}, handle['account'])
    indexes = {}
    async def initial_pcm():
        healths = await worker_health(context)
        require(plan_receipt(context.phase, [s.desired for s in context.broker.rooms.values()], healths, before_pcm=False)
                == context.frozen_plan, 'Actual worker plan changed after first PCM')
        for identifier, state in context.states.items():
            require(context.broker.rooms.get(identifier) is state and state.status == 'running'
                    and {name: unit.identity() for name, unit in state.processes.items()} == units[identifier]
                    and all(unit.alive for unit in state.processes.values()),
                    'An original epoch unit changed during the initial coded program')
            if identifier in grants:
                owner = healths[identifier]['source']['owner']
                grant = group.producer_status(context.producers[identifier])
                require(type(owner) is dict and all(owner.get(key) == grants[identifier][key]
                        for key in ('session_id', 'epoch', 'incarnation'))
                        and grant['session_id'] == grants[identifier]['session_id'],
                        'Initial final PCM lost its exact admitted native source')
                if healths[identifier]['native_blocks']:
                    require(healths[identifier]['native_generation'] == grants[identifier]['generation']
                            and healths[identifier]['native_group_id'] == group.GROUP,
                            'Initial final PCM changed its original native generation/group clock')
            else:
                require(healths[identifier]['source']['owner'] is None, 'Cold idle target acquired a native owner')
        for identifier, capture in context.captures.items():
            context.pcm_guards[identifier].check()
            if identifier not in indexes:
                index = group.music_onset_index(capture)
                if index is not None:
                    indexes[identifier] = index
                    context.pcm_guards[identifier].begin(index)
            if identifier in indexes:
                player = await context.states[identifier].client.request('GET', '/api/player')
                require(player['state'] == 'play' and player.get('volume') == context.states[identifier].current_volume == 100,
                        'Initial coded program stopped or changed its output volume')
                if identifier not in initial_players:
                    initial_players[identifier] = deepcopy(player)
                else:
                    require(player['item_id'] == initial_players[identifier]['item_id'],
                            'Initial coded program changed its original OwnTone item')
        return len(indexes) == len(context.producers)
    await observed_wait(context, None, initial_pcm, 'Epoch did not establish all actual final native PCM', 20)
    # Every buffer remains checked from actual onset throughout the code.
    end = start+context.declared_horizon_ns+max(0, context.phase.offset_ms)*1_000_000+21_000_000_000
    while time.monotonic_ns() < end:
        await initial_pcm()
        await asyncio.sleep(.03)
    calendars = {}
    series = {}
    for identifier, capture in context.captures.items():
        output_offset = context.phase.offset_ms if identifier == A else 0
        origin = start+context.declared_horizon_ns+output_offset*1_000_000
        first, last = origin+2_000_000_000, origin+18_000_000_000
        snapshot = group.capture_snapshot(capture)
        actual = await asyncio.to_thread(group.modulation, snapshot, first, last)
        reference = group.declared_capture(origin, group.RATE*2, group.RATE*18)
        declared = await asyncio.to_thread(group.modulation, reference, first, last)
        displacement = await asyncio.to_thread(group.align_series, declared, actual, maximum_delay_ms=None, search_ms=1000)
        calendars[identifier] = {'declared_final_origin_ns': origin, 'alignment': displacement,
            'horizon': group.validate_horizon(displacement, context.report['independent_capture_baselines'][identifier]),
            'frame_continuity': snapshot.frame_continuity}
        series[identifier] = actual
    context.evidence['native_calendars'] = calendars
    context.evidence['initial_players'] = deepcopy(initial_players)
    if context.phase.phase == 'native':
        raw = await asyncio.to_thread(group.measure_alignment, series[A], series[B], search_ms=2100)
        normalized = [(times-(context.phase.offset_ms if identifier == A else 0)/1000, values)
                      for identifier, (times, values) in ((A, series[A]), (B, series[B]))]
        alignment = {}
        await asyncio.to_thread(group.record_group_alignment, alignment, normalized,
            frame_continuity={key: calendars[key]['frame_continuity'] for key in (A, B)})
        context.evidence['configured_offset_alignment'] = {
            'raw_b_minus_a': raw, 'declared_offsets_ms': {A: context.phase.offset_ms, B: 0},
            'residual': alignment['group_alignment'],
            'scope': 'Only saved known output offsets normalize measured sample times; unchanged full/early/late2ms and drift gate'}
    baseline_start = len(context.captures[B].chunks)
    deadline = time.monotonic()+1.2
    while time.monotonic() < deadline:
        for guard in context.pcm_guards.values():
            guard.check()
        await asyncio.sleep(.02)
    baseline = group.observation.spectrum(context.captures[B].chunks[baseline_start:], group.RATE)
    group.observation.require_music(baseline, 1)
    context.pcm_guards[B].preserve_reference(baseline)
    observer = EpochObserver(context)
    context.observer = observer
    await observer.initialize()
    configuration = {'pin': deepcopy(context.states[A].local_pin.manifest), 'saved_offset_ms': context.phase.offset_ms,
                     'units': {name: unit.identity() for name, unit in context.states[A].processes.items()}}
    if getattr(context, 'finite_speech', False):
        # This exact prepared cold target has never received a speech marker.
        # Reading its final endpoint does not start playback or prime a peer.
        require(context.report.get('mode') == 'finite_speech', 'Finite fixture lost its explicit diagnostic mode')
        if context.phase.phase == 'idle':
            require(A not in context.captures and A not in context.producers,
                    'Finite cold target already has a source/capture actor')
            latency.start_capture(context, configuration['pin'])
        import importlib.util
        import sys
        from pathlib import Path
        spec = importlib.util.spec_from_file_location('native_epoch_finite_speech', Path(__file__).with_name('native_speech_finite.py'))
        finite = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = finite
        spec.loader.exec_module(finite)
        context.evidence.update(diagnostic='finite_emitted_opus', rows=[],
                                scope='Separate fresh finite utterance fixture; no repeating marker or matrix completeness promotion')
        context.evidence['finite_speech'] = await finite.measure_session(context, observer, role=context.phase.phase)
        await observer.sample()
        context.evidence.update(passed=True, cold_utterance_completeness_passed=True,
            cold_utterance_completeness_status='finite_emitted_opus_prefix_body_tail_verified',
            speech_latency_performance_passed=False,
            speech_latency_performance_status='pending_declared_and_characterized_software_budget')
        return observer
    if context.phase.phase == 'idle':
        result = await latency.idle_rows(context, observer, configuration, context.evidence['rows'])
        context.evidence['idle_peer'] = result
        for row in context.evidence['rows']:
            row['session_id'] = result['session_id']
    else:
        row = context.evidence['rows'][0]
        row.update(status='running', calendar=calendars[A], configured_offset_alignment=context.evidence['configured_offset_alignment'])
        row['markers'] = await music_markers(context, observer, configuration)
        row.update(status='complete', passed=True)
    await observer.sample()
    require(all(row['status'] == 'complete' and row['passed'] is True for row in context.evidence['rows']), 'Epoch has incomplete normal rows')
    context.evidence['passed'] = True
    context.evidence.update(measured_signal_timeline_cleanup_passed=False,
        speech_latency_performance_passed=False,
        speech_latency_performance_status='pending_declared_and_characterized_software_budget',
        cold_utterance_completeness_passed=False,
        cold_utterance_completeness_status='pending_finite_opus_prefix_tail_reference')
    context.evidence['speech_latency_measurements'] = latency_measurements(context.evidence)
    return observer


async def music_markers(context, observer, configuration):
    """Cold/warm gated Opus through actual API; original native program survives."""
    group, latency = context.group, context.latency
    peer, tone = latency.make_peer()
    identity = {'session_id': str(uuid4()), 'request_id': str(uuid4())}
    capture = context.captures[A]
    reference = group.observation.spectrum(capture.chunks[-60:], group.RATE)
    group.observation.require_music(reference, 1)
    markers = {}
    primary = None
    try:
        await peer.setLocalDescription(await peer.createOffer())
        answer = await context.api.request('POST', f'/api/v1/rooms/{A}/speech',
            json={**identity, 'action': 'offer', 'sdp': peer.localDescription.sdp, 'type': 'offer'})
        require(answer.get('admitted_room_id') == A, 'Music speech targeted the wrong room')
        await peer.setRemoteDescription(RTCSessionDescription(sdp=answer['sdp'], type=answer['type']))
        async def connected():
            require(peer.connectionState not in {'failed', 'closed'}, 'Music speech peer failed before explicit RTP')
            return peer.connectionState == 'connected'
        await observed_wait(context, observer, connected, 'Music speech peer did not connect', 8)
        require(tone.samples == 0, 'Cold music peer emitted RTP before explicit release')
        for kind in ('cold_music', 'warm_music'):
            record = {'session_id': identity['session_id'], 'same_session_for_cold_and_warm': True}
            markers[kind] = record
            index = len(capture.chunks)
            trigger = time.monotonic_ns()
            tone.release()
            async def emitted():
                return tone.triggers[-1] if tone.triggers and tone.triggers[-1]['utterance'] == tone.utterance else None
            marker = await observed_wait(context, observer, emitted, 'Music Opus marker was not emitted', 2)
            record['encoder_marker'] = marker
            gate = latency.MarkerGate(audible=True, trigger_ns=marker['first_emitted_monotonic_ns'])
            transition = group.observation.SpectrumTransition('duck_voice', trigger/1e9,
                reference['music_440_amplitude'], max(1., reference['speech_880_amplitude']))
            first_voice = transition_passed = False
            async def audible(gate=gate, transition=transition):
                nonlocal index, first_voice, transition_passed
                capture.poll()
                while index < len(capture.chunks):
                    data, at = capture.chunks[index], capture.captured_at[index]
                    first_voice = gate.push(data, capture.absolute[at], at) or first_voice
                    transition_passed = transition.push(data, at, group.RATE) or transition_passed
                    index += 1
                    if first_voice and transition_passed:
                        return True
                return False
            await observed_wait(context, observer, audible, 'Music speech did not establish real coded voice and duck', 12)
            record.update(gate.receipt())
            record['duck_and_voice'] = transition.evidence(time.monotonic())
            tone.audible = False
            restore_trigger = time.monotonic()
            restore = group.observation.SpectrumTransition('restore_no_voice', restore_trigger,
                reference['music_440_amplitude'], max(1., reference['speech_880_amplitude']),
                previous_voice=transition.last_measured['speech_880_amplitude'])
            async def restored(restore=restore):
                nonlocal index
                capture.poll()
                while index < len(capture.chunks):
                    data, at = capture.chunks[index], capture.captured_at[index]
                    index += 1
                    if restore.push(data, at, group.RATE):
                        return True
                return False
            await observed_wait(context, observer, restored, 'Music did not restore after quiet connected peer', 12)
            record['following_restore'] = restore.evidence(time.monotonic())
            await observer.sample()
            record.update(passed=True, status='complete', unchanged_native_owner=deepcopy(observer.target_owner),
                          unchanged_item_id=observer.target_player['item_id'])
        return markers
    except BaseException as exc:
        primary = exc
        raise
    finally:
        tone.stop()
        errors = []
        try:
            await context.api.request('POST', f'/api/v1/rooms/{A}/speech',
                json={**identity, 'action': 'close', 'request_id': str(uuid4())})
        except Exception as exc:
            errors.append(f'API peer close:{type(exc).__name__}')
        try:
            await asyncio.wait_for(peer.close(), 4)
            require(peer.connectionState == 'closed', 'Music peer survived owned close')
        except Exception as exc:
            errors.append(f'RTC peer close:{type(exc).__name__}')
        context.evidence['music_peer_cleanup_errors'] = errors
        if primary is None:
            require(not errors, 'Music latency peer cleanup failed')


def finalize_capture(context, identifier, capture):
    """After NULL, validate all pre-request PCM and every queued frame anchor."""
    require(context.observer is not None and context.observer.stopped, 'Epoch lacks exact active observation stop boundary')
    capture.poll()
    guard = context.pcm_guards[identifier]
    guard.check(live=False)
    evidence = capture.poll()
    require(evidence['capture_dropped_bytes'] == 0, 'Epoch teardown capture dropped PCM')
    context.evidence.setdefault('tail', {}).setdefault('captures', {})[identifier] = {
        'null_verified_monotonic_ns': time.monotonic_ns(), 'capture': evidence, 'pcm': guard.evidence(),
        'scope': 'Original caps/sample-frame/PTS/queue fences through NULL; only post-stop PCM is labeled teardown'}
    artifact = context.group.retain_failed_capture(capture, context.temporary, f'epoch-{context.phase.epoch_id}-{identifier}')
    context.evidence['tail']['captures'][identifier]['artifact'] = artifact
    if set(context.evidence['tail']['captures']) == {A, B}:
        context.evidence['tail'].update(both_captures_null=True, pre_stop_pcm_verified=True,
                                      frame_sequences_verified=True, teardown_labeled=True)


def aggregate_epoch_reports(reports, expected_phases):
    expected = [phase.receipt() if isinstance(phase, Phase) else phase for phase in expected_phases]
    require(len(reports) == len(expected) == 6, 'Latency matrix requires all six independently cleaned fixtures')
    require({(phase['offset_ms'], phase['phase']) for phase in expected}
            == {(offset, role) for offset in OFFSETS for role in ('idle', 'native')}
            and len({phase['id'] for phase in expected}) == 6, 'Controller phase set is incomplete/reused')
    rows, units, owners, paths = [], set(), set(), set()
    admission = reports[0].get('native_lab')
    require(type(admission) is dict, 'Matrix requires actual explicit clean-lab admission')
    for report, phase in zip(reports, expected, strict=True):
        epoch = report.get('latency_epoch', {})
        require(report.get('mode') == 'latency_probe' and report.get('passed') is True
                and report.get('cleanup_errors') == [] and report.get('cleanup')
                and all(value is True for value in report['cleanup'].values())
                and report.get('native_lab') == admission, 'Fixture cleanup/admission did not pass')
        require({key: epoch.get(key) for key in ('version', 'id', 'offset_ms', 'phase')} == phase
                and epoch.get('passed') is True, 'Fixture receipt relabels its exact prepared epoch')
        require(epoch.get('measured_signal_timeline_cleanup_passed') is True
                and epoch.get('speech_latency_performance_passed') is False
                and epoch.get('speech_latency_performance_status') == 'pending_declared_and_characterized_software_budget',
                'Measurement integrity must not disguise unqualified speech latency performance')
        require(epoch.get('cold_utterance_completeness_passed') is False
                and epoch.get('cold_utterance_completeness_status') == 'pending_finite_opus_prefix_tail_reference',
                'Qualifying coded run cannot disguise pending finite utterance prefix/tail qualification')
        require(epoch.get('speech_latency_measurements') == latency_measurements(epoch),
                'Marker latency measurements relabel actual B/offset/capture observations')
        frozen = epoch.get('frozen_plan', {})
        definitions = [Room.model_validate(room) for room in frozen.get('saved_intent', [])]
        require(intent_digest(frozen['saved_intent']) == frozen.get('intent_sha256'), 'Saved epoch intent proof changed')
        plan = latency_plan(definitions)
        require({room.id for room in definitions if room.enabled} == {A, B}
                and frozen.get('common_horizon_ns') == plan.common_horizon_ns
                and frozen.get('output_buffers_ms') == {item.room_id: item.output_buffer_ms for item in plan.rooms},
                'Matrix H/B disagrees with its actual reviewed policy and saved intent')
        for room in definitions:
            if room.enabled:
                require(len(room.speakers) == 1 and room.speakers[0].id == '0' and room.speakers[0].protocol == 'alsa'
                        and room.speakers[0].offset_ms == (phase['offset_ms'] if room.id == A else 0),
                        'Matrix does not preserve exact declared room offsets')
        require(type(epoch.get('common_presentation_ns')) is int and epoch['common_presentation_ns'] > 0,
                'Epoch has no independently declared native calendar')
        baselines = epoch.get('independent_capture_baselines', {})
        require(set(baselines) == {A, B}, 'Epoch lacks both pre-backend independent capture calibrations')
        calendars = epoch.get('native_calendars', {})
        require(set(calendars) == ({B} if phase['phase'] == 'idle' else {A, B}),
                'Epoch calendar set differs from its actual program actors')
        for identifier, calendar in calendars.items():
            offset = phase['offset_ms'] if identifier == A else 0
            require(calendar.get('declared_final_origin_ns')
                    == epoch['common_presentation_ns']+plan.common_horizon_ns+offset*1_000_000,
                    'Native calendar uses a guessed or relabeled actual final origin')
            validate_alignment_receipt(calendar.get('alignment', {}), tight=False)
            baseline = baselines[identifier]
            require(all(baseline.get('cleanup', {}).get(key) is True
                        for key in ('exact_output_unit_stopped', 'capture_null', 'held_pin_closed')),
                    'Independent calibration retains an owned producer/capture')
            horizon = calendar.get('horizon', {})
            require(numeric(baseline.get('capture_offset_ms')) and numeric(baseline.get('horizon_half_width_ms'))
                    and baseline['horizon_half_width_ms'] > 0
                    and horizon.get('raw_displacement_ms') == calendar['alignment']['relative_offset_ms']
                    and horizon.get('independent_capture_offset_ms') == baseline['capture_offset_ms']
                    and horizon.get('predeclared_half_width_ms') == baseline['horizon_half_width_ms']
                    and numeric(horizon.get('corrected_horizon_error_ms'))
                    and abs(horizon['corrected_horizon_error_ms']-(horizon['raw_displacement_ms']-baseline['capture_offset_ms'])) < 1e-9
                    and abs(horizon['corrected_horizon_error_ms']) <= baseline['horizon_half_width_ms'],
                    'Native calendar fails its retained independent capture timing bracket')
            sequence = calendar.get('frame_continuity', {})
            require(sequence.get('mode') == 'sample_offsets' and type(sequence.get('verified_blocks')) is int
                    and sequence['verified_blocks'] >= 100 and type(sequence.get('verified_frames')) is int
                    and sequence['verified_frames'] > 0, 'Native calendar lacks actual sample-frame continuity')
        untouched = epoch.get('untouched', {})
        require(untouched.get('failure') is None and untouched.get('original_capture_preserved') is True
                and len(untouched.get('control_samples', [])) >= 3 and untouched.get('pcm', {}).get('failed_block') is None
                and untouched.get('pcm', {}).get('untouched_reference_blocks', 0) > 0, 'Original untouched proof is incomplete')
        invocation_ids = {item['invocation_id'] for item in untouched.get('original_unit_identities', {}).values()}
        require(invocation_ids and not units.intersection(invocation_ids), 'Unit actor reused across prepared epochs')
        units.update(invocation_ids)
        source = untouched.get('source_owner', {})
        identity = source.get('session_id'), source.get('epoch'), source.get('incarnation')
        require(all(value is not None for value in identity) and identity not in owners, 'Source actor reused across prepared epochs')
        owners.add(identity)
        tail = epoch.get('tail', {})
        require(type(epoch.get('stop_boundary', {}).get('requested_monotonic_ns')) is int
                and all(tail.get(key) is True for key in ('both_captures_null', 'pre_stop_pcm_verified',
                                                        'frame_sequences_verified', 'teardown_labeled'))
                and set(tail.get('captures', {})) == {A, B}, 'Original capture tail/stop boundary incomplete')
        stopped = epoch['stop_boundary']['requested_monotonic_ns']
        retirements = epoch.get('producer_retirement', {})
        require(set(retirements) == ({B} if phase['phase'] == 'idle' else {A, B}),
                'Epoch did not retire the exact original native producers')
        for retirement in retirements.values():
            require(retirement.get('retired') is True and retirement.get('identity', {}).get('invocation_id')
                    and type(retirement.get('requested_monotonic_ns')) is int
                    and type(retirement.get('verified_monotonic_ns')) is int
                    and stopped <= retirement['requested_monotonic_ns'] <= retirement['verified_monotonic_ns'],
                    'Owned producer retirement lacks its exact stop-request/identity proof')
        daemon = epoch.get('daemon_retirement', {})
        require(daemon.get('retired') is True and daemon.get('empty_ownership_verified') is True
                and type(daemon.get('requested_monotonic_ns')) is int and type(daemon.get('verified_monotonic_ns')) is int
                and daemon['requested_monotonic_ns'] <= daemon['verified_monotonic_ns'] and set(daemon.get('units', {})) == {A, B},
                'Epoch daemon retirement/empty ownership is incomplete')
        for final in tail['captures'].values():
            require(final['pcm'].get('failed_block') is None and final['capture']['capture_dropped_bytes'] == 0,
                    'Final tail hides a previously failed music/frame observation')
            sequence = final['capture'].get('frame_continuity', {})
            require(type(final.get('null_verified_monotonic_ns')) is int and final['null_verified_monotonic_ns'] >= stopped
                    and final['pcm'].get('stop_requested_monotonic_ns') == stopped
                    and sequence.get('mode') == 'sample_offsets'
                    and sequence.get('verified_frames')*4 == final['capture']['observed_bytes'],
                    'Final queue/frame evidence differs from its exact recorded capture')
            path = final['artifact']['path']
            require(path not in paths, 'Final capture artifact reused across epochs')
            paths.add(path)
        expected_kinds = {'cold_idle', 'warm_idle'} if phase['phase'] == 'idle' else {'native_calendar'}
        actual_rows = epoch.get('rows', [])
        require(len(actual_rows) == len(expected_kinds) and {row.get('kind') for row in actual_rows} == expected_kinds,
                'Fixture has missing/duplicate/unrelated normal rows')
        for row in actual_rows:
            require(row.get('epoch_id') == phase['id'] and row.get('offset_ms') == phase['offset_ms']
                    and row.get('passed') is True and row.get('status') == 'complete', 'Latency row is pending/failed/relabeled')
            if row['kind'] == 'native_calendar':
                require(row.get('calendar') == calendars[A]
                        and row.get('configured_offset_alignment') == epoch.get('configured_offset_alignment'),
                        'Native row differs from its measured epoch calendar/alignment')
                alignment = epoch.get('configured_offset_alignment', {})
                require(alignment.get('declared_offsets_ms') == {A: phase['offset_ms'], B: 0},
                        'Group alignment uses a measured or invented output correction')
                validate_alignment_receipt(alignment.get('raw_b_minus_a', {}), tight=False)
                require(abs(alignment['raw_b_minus_a']['relative_offset_ms']+phase['offset_ms']) <= 2,
                        'Raw group timing misses its known configured relative offset')
                residual = alignment.get('residual', {})
                for window in (residual, residual.get('early', {}), residual.get('late', {})):
                    validate_alignment_receipt(window, tight=True)
                require(numeric(residual.get('drift_ms')) and abs(residual['drift_ms']) <= 2
                        and abs(residual['drift_ms']-(residual['late']['relative_offset_ms']-residual['early']['relative_offset_ms'])) < 1e-9,
                        'Actual normalized group timing drifts across the coded program')
                markers = row.get('markers', {})
                require(set(markers) == {'cold_music', 'warm_music'} and not epoch.get('music_peer_cleanup_errors'),
                        'Native row lacks both required original-peer musical subcases')
                sessions = set()
                for marker in markers.values():
                    require(marker.get('passed') is True and marker.get('status') == 'complete'
                            and marker.get('same_session_for_cold_and_warm') is True
                            and marker.get('duck_and_voice', {}).get('passed') is True
                            and marker.get('following_restore', {}).get('passed') is True
                            and marker.get('first_matching_absolute_pts_ns') is not None,
                            'Music marker lacks real coded voice/duck/restore latency evidence')
                    validate_marker_receipt(marker)
                    original = epoch.get('target_original', {})
                    require(marker.get('unchanged_native_owner') == original.get('source_owner')
                            and marker.get('unchanged_item_id') == original.get('player', {}).get('item_id')
                            and original.get('source_owner'), 'Music speech changed its original target source/item')
                    sessions.add(marker['session_id'])
                require(len(sessions) == 1, 'Warm musical marker reconnected its peer')
            else:
                require(row.get('same_session_for_cold_and_warm') is True
                        and row.get('first_matching_absolute_pts_ns') is not None
                        and row.get('following_silence', {}).get('passed') is True,
                        'Idle marker lacks original-peer onset/following-silence evidence')
                validate_marker_receipt(row)
                require(row.get('session_id') == epoch.get('idle_peer', {}).get('session_id'),
                        'Warm idle marker reconnected its actual peer')
            rows.append({**deepcopy(row), 'actual_horizon_ns': plan.common_horizon_ns,
                         'actual_buffers_ms': frozen['output_buffers_ms']})
    require(len(rows) == 9 and {(row['kind'], row['offset_ms']) for row in rows}
            == {(kind, offset) for kind in KINDS for offset in OFFSETS}, 'Full nine-row matrix remains incomplete')
    return {'passed': True, 'rows': rows, 'epochs': reports,
            'measured_signal_timeline_cleanup_passed': True,
            'speech_latency_performance_passed': False,
            'speech_latency_performance_status': 'pending_declared_and_characterized_software_budget',
            'cold_utterance_completeness_passed': False,
            'cold_utterance_completeness_status': 'pending_finite_opus_prefix_tail_reference',
            'scope': 'Six independently prepared digital epochs; ordinary H1 policy plus actual offset plans; no physical/phone/Cast/Bluetooth latency claim',
            'stock_phone_verified': False, 'physical_speakers_verified': False}


def latency_measurements(epoch):
    """Independent integrity measurements; no guessed minimum or release SLA."""
    budget = epoch.get('speech_latency_budget', {})
    require('encoder_worker_overhead_ms' in budget and budget['encoder_worker_overhead_ms'] is None
            and 'idle_startup_overhead_ms' in budget and budget['idle_startup_overhead_ms'] is None,
            'Pending characterization cannot manufacture a source-derived performance budget')
    frozen = epoch['frozen_plan']
    base_ms = frozen['output_buffers_ms'][A]+epoch['offset_ms']
    calibration = epoch['independent_capture_baselines'][A]
    result = []
    for row in epoch['rows']:
        markers = row['markers'] if row['kind'] == 'native_calendar' else {row['kind']: row}
        for kind, marker in markers.items():
            validate_marker_receipt(marker)
            result.append({'kind': kind, 'epoch_id': epoch['id'], 'offset_ms': epoch['offset_ms'],
                'actual_output_buffer_ms': frozen['output_buffers_ms'][A],
                'known_buffer_plus_offset_ms': base_ms,
                'independent_capture_half_width_ms': calibration['horizon_half_width_ms'],
                'encoder_to_qualifying_run_pts_ms': marker['encoder_to_final_pts_seconds']*1000,
                'encoder_to_qualifying_run_callback_ms': marker['encoder_to_final_callback_seconds']*1000,
                'encoder_worker_overhead_budget_ms': None, 'idle_startup_overhead_budget_ms': None,
                'performance_maximum_ms': None, 'performance_passed': False,
                'performance_status': 'pending_declared_and_characterized_software_budget',
                'scope': 'First qualifying coded run vs source frame handed to Opus encoder; not first nonzero utterance or acoustic latency'})
    return result


def numeric(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate_alignment_receipt(alignment, *, tight):
    require(all(numeric(alignment.get(key)) for key in ('relative_offset_ms', 'correlation', 'peak_prominence'))
            and alignment['correlation'] >= .985 and alignment['peak_prominence'] >= .015
            and (not tight or abs(alignment['relative_offset_ms']) <= 2),
            'Retained final PCM fails the unchanged coded signal/2ms timing gate')


def validate_marker_receipt(marker):
    trigger = marker.get('encoder_marker', {}).get('first_emitted_monotonic_ns')
    first = marker.get('first_matching_absolute_pts_ns')
    require(marker.get('passed') is True and type(trigger) is int and type(first) is int and first >= trigger
            and marker.get('trigger_monotonic_ns') == trigger
            and numeric(marker.get('encoder_to_final_pts_seconds'))
            and abs(marker['encoder_to_final_pts_seconds']-(first-trigger)/1e9) < 1e-9
            and numeric(marker.get('encoder_to_final_callback_seconds'))
            and 0 <= marker['encoder_to_final_callback_seconds'] <= 12
            and type(marker.get('consecutive_frames')) is int and marker['consecutive_frames'] >= 19200
            and len(marker.get('coded_120ms_levels', [])) >= 3,
            'Marker onset/code/latency receipt does not identify its actual qualifying run')
