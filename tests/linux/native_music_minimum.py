"""Opt-in H140/B40 cold MUSIC-only experiment; no physical claim.

The private broker adapter changes only the experiment's pure plan binding.
Actual generated config, worker health, and saved output selection must agree.
P is fixed before either BEGIN at nominal first availability +150 ms. Admission
cannot rebase P, skip frames, or widen the maintained 150 ms delivery guard.
Importing this module opens no sockets, files, units or devices.
"""
from __future__ import annotations

# Explicit manual bounded source and artifact observations.
# ruff: noqa: ASYNC240
import asyncio
from copy import deepcopy
import importlib.util
import hashlib
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import signal
import socket
import sys
import time
from types import FunctionType
from uuid import UUID, uuid4

from shiri.domain import Room
from shiri.runtime.broker import Broker
from shiri.runtime.latency import LatencyPlan, latency_plan, room_buffer_ms
from shiri.runtime.system import RuntimeFailure, atomic_json, root_directory
from shiri.runtime.timing import Kind, Packet, RATE

A = 'b6786543-7eb2-443d-83b1-65b984123a76'
B = '5356832c-c514-4472-bb4b-4f34ed86dd07'
HORIZON_MS = 140
BUFFER_MS = 40
NATIVE_LEAD_NS = 150_000_000
SEND_LATE_NS = 150_000_000
PRODUCER_PROFILE = {'version': 2, 'horizon_ms': HORIZON_MS, 'buffer_ms': BUFFER_MS}
CAPTURE_BYTES = 48 * 1024 * 1024


def require(value, message):
    if not value:
        raise RuntimeFailure(message)


def load_epoch():
    spec = importlib.util.spec_from_file_location('music_actual_epoch_observation', Path(__file__).with_name('native_latency_epochs.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


epoch = load_epoch()
Phase = epoch.Phase


def bind(function, **names):
    """Bind reviewed functions privately; never monkeypatch production globals."""
    result = FunctionType(function.__code__, {**function.__globals__, **names},
                          function.__name__, function.__defaults__, function.__closure__)
    result.__kwdefaults__ = function.__kwdefaults__
    return result


def candidate_plan(definitions):
    """Allow setup subsets, but never another enabled route or corrected speaker."""
    plan = latency_plan(definitions)
    for definition in definitions:
        room = Room.model_validate(definition)
        if not room.enabled:
            continue
        require(room.id in {A, B} and room.local_audio_device is not None,
                'Music startup allows only its two enrolled local rooms')
        require(len(room.speakers) <= 1 and all(speaker.id == '0' and speaker.protocol == 'alsa'
                and speaker.offset_ms == 0 for speaker in room.speakers),
                'Music startup requires the exact zero-offset local0 route')
    require(all(item.output_buffer_ms == BUFFER_MS for item in plan.rooms),
            'Music startup cannot change its local B40')
    return LatencyPlan(HORIZON_MS, plan.rooms)


def broker_class(base):
    """Own test-only reconcile+selection bindings; all production gates retained."""
    require(issubclass(base, Broker), 'Music startup needs the actual isolated Broker')
    return type('MusicMinimumBroker', (base,), {
        'reconcile': bind(Broker.reconcile, latency_plan=candidate_plan, room_buffer_ms=room_buffer_ms),
        'set_outputs': bind(Broker.set_outputs, latency_plan=candidate_plan, room_buffer_ms=room_buffer_ms),
        '_material': bind(Broker._material, room_buffer_ms=room_buffer_ms),
    })


def require_idle_worker(health):
    source = health.get('source') if type(health) is dict else None
    require(type(health) is dict and health.get('ready') is True and 'error' in health and health['error'] is None
            and type(source) is dict and source.get('ready') is True and 'owner' in source and source['owner'] is None
            and type(health.get('native_blocks')) is int and health['native_blocks'] == 0
            and type(health.get('timing_relay_delay_ms')) is int and health['timing_relay_delay_ms'] == HORIZON_MS
            and type(health.get('output_buffer_ms')) is int and health['output_buffer_ms'] == BUFFER_MS,
            'Music startup worker must be ready idle with exact H140/B40 before BEGIN')


def admit_launch(room, duration, start, lead, frozen, frozen_type, *, lab):
    room = Room.model_validate(room)
    require(lab is not None and duration == 90 and type(duration) is int and type(start) is int and start == 0
            and type(lead) is int and lead == NATIVE_LEAD_NS and type(frozen) is frozen_type
            and frozen.horizon_ns == HORIZON_MS*1_000_000
            and frozen.buffers_ms == tuple((identifier, BUFFER_MS) for identifier in sorted((A, B))),
            'Music startup needs its explicit frozen pre-BEGIN H140/B40 admission')
    require(room.id in {A, B} and room.enabled and len(room.speakers) == 1
            and room.speakers[0].id == '0' and room.speakers[0].protocol == 'alsa'
            and room.speakers[0].offset_ms == 0, 'Music startup receiver route differs from its exact local plan')


_plan_receipt = bind(epoch.plan_receipt, latency_plan=candidate_plan)


def plan_receipt(phase, definitions, healths, *, before_pcm):
    require(type(phase) is Phase and phase.offset_ms == 0 and phase.phase == 'native',
            'Music startup requires its separate zero-offset native phase')
    return _plan_receipt(phase, definitions, healths, before_pcm=before_pcm)


class MusicObserver:
    """Original grant/source/units/selection/NPT throughout analysis and stop."""
    def __init__(self, context, check):
        self.context, self._sample = context, check
        self.lock = asyncio.Lock()
        self.done, self.watcher, self.error, self.stopped = asyncio.Event(), None, None, False

    async def sample(self):
        async with self.lock:
            return await self._sample()

    async def initialize(self):
        await self.check()
        self.watcher = asyncio.create_task(self.watch(), name='music-startup-original-actors')

    async def watch(self):
        try:
            while not self.done.is_set():
                await self.sample()
                try:
                    await asyncio.wait_for(self.done.wait(), .06)
                except asyncio.TimeoutError:
                    pass
        except Exception as exc:
            self.error = str(exc)[:2000]
            raise

    async def check(self):
        require(self.error is None, self.error or 'Music startup watcher failed permanently')
        if self.watcher is not None and self.watcher.done():
            if self.watcher.cancelled():
                self.error = 'Music startup watcher was unexpectedly cancelled'
                raise RuntimeFailure(self.error)
            self.watcher.result()
            raise RuntimeFailure('Music startup watcher stopped unexpectedly')
        try:
            return await self.sample()
        except Exception as exc:
            self.error = str(exc)[:2000]
            raise

    async def close(self):
        if self.stopped:
            return
        failure = None
        try:
            await self.check()
        except Exception as exc:
            failure = exc
        self.done.set()
        if self.watcher is not None:
            self.watcher.cancel()
            results = await asyncio.gather(self.watcher, return_exceptions=True)
            if results and isinstance(results[0], Exception) and failure is None:
                failure = results[0]
        if failure is not None:
            raise failure
        requested = time.monotonic_ns()
        self.context.evidence['stop_boundary'] = {'requested_monotonic_ns': requested,
            'scope': 'Original two-zone music/source/control/content through exact fixture stop; later PCM is teardown'}
        for guard in self.context.pcm_guards.values():
            guard.end_at(requested)
        self.context.evidence['per_buffer_evidence'] = {key: guard.evidence()
            for key, guard in self.context.pcm_guards.items()}
        self.stopped = True


async def prepare(context):
    require(context.group.NATIVE_LAB is not None and context.report.get('mode') == 'music_minimum'
            and not context.producers and not context.captures, 'Music startup is an explicit cold clean-lab mode')
    frozen = plan_receipt(context.phase, [state.desired for state in context.broker.rooms.values()],
                          await epoch.worker_health(context), before_pcm=True)
    for identifier, state in context.states.items():
        state.local_pin.validate()
        require(state.local_pin.manifest['device'] == context.group.ZONES[identifier]['device']
                and state.local_pin.manifest['subdevice'] == 7, 'Music startup output pin differs from its virtual route')
        actual = await state.client.outputs(set())
        selected = [item for item in actual if item['selected']]
        require(state.selected_ids == ['0'] and len(selected) == 1 and selected[0]['id'] == '0'
                and type(selected[0].get('offset_ms')) is int and selected[0]['offset_ms'] == 0,
                'Music startup actual output selection/offset differs from its saved plan')
        player = await state.client.request('GET', '/api/player')
        require(player['state'] == 'stop', 'Music startup cold output was already playing')
        context.latency.playback_closed(state.local_pin.manifest)
    context.frozen_plan = frozen
    context.declared_horizon_ns = frozen['common_horizon_ns']
    context.evidence = {**context.phase.receipt(), 'passed': False, 'frozen_plan': deepcopy(frozen),
        'mode': 'music_minimum', 'speech_exercised': False, 'physical_output_verified': False,
        'native_receiver_callback_verified': False,
        'scope': 'Synthetic exact-UID native ingress with fixed calendar before BEGIN; actual digital outputs only',
        'delivery_envelope': {'native_callback_lead_ns': NATIVE_LEAD_NS,
                              'unchanged_strict_send_lateness_ns': SEND_LATE_NS,
                              'timer_first_anchor_from_availability_ns': NATIVE_LEAD_NS+(HORIZON_MS-BUFFER_MS)*1_000_000}}
    context.report['music_minimum'] = context.evidence
    # Existing exact retirement receipt code expects this reference, not a copy.
    context.report['latency_epoch'] = context.evidence
    context.report['latency_probe'] = {'preparations': []}
    context.evidence['independent_capture_baselines'] = deepcopy(context.report['independent_capture_baselines'])
    return frozen


def calendar(action):
    require(type(action) is dict and set(action) == {'generation', 'action', 'common_start_ns', 'available_ns'}
            and type(action['generation']) is int and action['generation'] == 2 and action['action'] == 'begin'
            and type(action['available_ns']) is int and action['available_ns'] > 0
            and type(action['common_start_ns']) is int
            and action['common_start_ns'] == action['available_ns']+NATIVE_LEAD_NS,
            'Music startup needs one exact immutable pre-BEGIN calendar')
    return action['available_ns'], action['common_start_ns']


async def producer(config, group):
    """Real BEGIN/GRANT/PCM; zero calendar rebasing and zero frame skipping."""
    require(type(config.get('music_minimum')) is dict and config['music_minimum'] == PRODUCER_PROFILE
            and all(type(config['music_minimum'][key]) is int for key in PRODUCER_PROFILE)
            and config.get('common_start_ns') == 0 and type(config.get('common_start_ns')) is int
            and type(config.get('arrival_lead_ns')) is int and config['arrival_lead_ns'] == NATIVE_LEAD_NS
            and config.get('duration_seconds', 90) == 90 and type(config.get('duration_seconds', 90)) is int
            and type(config.get('leader')) is bool and type(config.get('wide_bracket')) is bool
            and os.geteuid() == config['uid'] != 0 and os.getegid() == config['gid'],
            'Music startup producer lost its exact credential/calendar/profile admission')
    loop, stop = asyncio.get_running_loop(), asyncio.Event()
    for name in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(name, stop.set)
    path, command = Path(config['status']), Path(config['command'])
    session, group_id = uuid4().bytes, UUID(config['group']).bytes
    flags = group['FLAGS'] | (group['FLAG_GROUP_LEADER'] if config['leader'] else 0)
    state = {'uid': os.geteuid(), 'gid': os.getegid(), 'pid': os.getpid(),
             'stage': 'ready_before_begin', 'ready_before_begin_ns': time.monotonic_ns(), 'frames': 0,
             'session_id': str(UUID(bytes=session)), 'commands': [], 'finished': False,
             'max_send_lateness_ns': 0, 'max_clock_bracket_ns': 0,
             'clock_sample_attempts': 0, 'clock_sample_retries': 0, 'max_attempt_clock_bracket_ns': 0}
    connection = None
    primary = None
    try:
        atomic_json(path, state)
        deadline = time.monotonic()+8
        action = None
        while not stop.is_set():
            action = json.loads(command.read_text())
            if action == {'generation': 1, 'action': 'wait'}:
                require(time.monotonic() < deadline, 'Music startup calendar was not released within preparation budget')
                await asyncio.sleep(.002)
                continue
            available, start = calendar(action)
            break
        else:
            raise RuntimeFailure('Music startup stopped before BEGIN')
        attempted = time.monotonic_ns()
        require(state['ready_before_begin_ns'] < available <= attempted
                and attempted-available < SEND_LATE_NS,
                'Music startup calendar is stale, reversed or beyond the strict delivery envelope before BEGIN')
        state.update(available_ns=available, common_start_ns=start, calendar_generation=2,
                     begin_attempt_ns=attempted, stage='connecting')
        atomic_json(path, state)
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        connection.setblocking(False)
        await asyncio.wait_for(loop.sock_connect(connection, config['socket']), 3)
        state['begin_sent_ns'] = time.monotonic_ns()
        begin = Packet(Kind.BEGIN, session, group=group_id, flags=flags)
        await asyncio.wait_for(loop.sock_sendall(connection, begin.encode()), .3)
        grant = Packet.decode(await asyncio.wait_for(loop.sock_recv(connection, 4096), 5))
        require(grant.kind is Kind.GRANT and grant.session == session and grant.group == group_id
                and grant.epoch > 0 and grant.incarnation != bytes(16) and grant.generation == 1
                and grant.flags == flags, 'Music startup ingress omitted its exact fresh source grant')
        state.update(stage='granted', grant_received_ns=time.monotonic_ns(), epoch=grant.epoch,
                     incarnation=str(UUID(bytes=grant.incarnation)), generation=grant.generation)
        atomic_json(path, state)
        frame, sequence, next_status = 0, 0, 0
        while not stop.is_set() and frame < RATE*90:
            current = json.loads(command.read_text())
            require(current == action, 'Music startup calendar changed after original BEGIN')
            target = available+frame*1_000_000_000//RATE
            now = time.monotonic_ns()
            if now < target:
                try:
                    await asyncio.wait_for(stop.wait(), (target-now)/1e9)
                except asyncio.TimeoutError:
                    pass
                continue
            lateness = time.monotonic_ns()-target
            if frame == 0:
                state['first_pcm_attempt_ns'] = time.monotonic_ns()
                state['first_pcm_lateness_ns'] = lateness
            require(lateness < SEND_LATE_NS, 'Synthetic producer missed bounded native delivery cadence')
            packet = group['bracketed_packet'](grant, group_id, frame, sequence, start,
                                               config['wide_bracket'], 440, stats=state)
            lateness = await group['send_native_packet'](loop, connection, packet.encode(), target, stats=state)
            if frame == 0:
                state.update(first_pcm_sent_ns=time.monotonic_ns(), first_pcm_frame=packet.frame_index,
                             first_pcm_sequence=packet.sequence, first_pcm_native_presentation_ns=start)
            state['max_send_lateness_ns'] = max(state['max_send_lateness_ns'], lateness)
            state['max_clock_bracket_ns'] = max(state['max_clock_bracket_ns'],
                                              packet.monotonic_after_ns-packet.monotonic_before_ns)
            frame += packet.frames
            sequence += 1
            state.update(stage='streaming', frames=frame, presentation_ns=packet.presentation_ns)
            if now >= next_status:
                atomic_json(path, state)
                next_status = now+250_000_000
        state.update(stage='finished', finished=True)
    except BaseException as exc:
        primary = exc
        state['error'] = group['observation'].redact_exception(exc)
        raise
    finally:
        group['retire_producer']([connection] if connection is not None else [], path, state, primary, publish=atomic_json)


finalize_capture = epoch.finalize_capture


@lru_cache(maxsize=1)
def complete_music_reference(program_pcm):
    """One bounded immutable 21-second lossless local reference per fixture."""
    return program_pcm(0, frames=RATE*21)


def verify_complete_music(snapshot, program_pcm, declared_origin_ns, baseline):
    """Require original coded prefix and constant tail, with no phase/gain fit."""
    require(type(declared_origin_ns) is int and declared_origin_ns > 0,
            'Music prefix has no immutable declared origin')
    require(type(snapshot.chunks) is tuple and type(snapshot.captured_at) is tuple
            and len(snapshot.chunks) == len(snapshot.captured_at)
            and all(type(chunk) is bytes and chunk and len(chunk)%4 == 0 for chunk in snapshot.chunks),
            'Music prefix snapshot lost complete stereo sample frames')
    total = sum(map(len, snapshot.chunks))
    require(total <= CAPTURE_BYTES, 'Music prefix exceeded its predeclared48MiB capture bound')
    raw = b''.join(snapshot.chunks)
    reference = complete_music_reference(program_pcm)
    require(len(reference) == RATE*21*4, 'Music prefix reference has invalid fixed length')
    first = raw.find(reference)
    require(first >= 0 and first%4 == 0 and raw.find(reference, first+4) < 0,
            'Actual final PCM lost, altered or repeated its full original21s music prefix/tail')
    remaining = first
    observed_origin = None
    for chunk, at in zip(snapshot.chunks, snapshot.captured_at, strict=True):
        if remaining < len(chunk):
            anchor = snapshot.absolute.get(at)
            require(type(anchor) is int and anchor > 0, 'Music prefix has no actual buffer presentation anchor')
            observed_origin = anchor+(remaining//4)*1_000_000_000//RATE
            break
        remaining -= len(chunk)
    require(observed_origin is not None, 'Music prefix sample origin is outside its frozen capture')
    from_error = (observed_origin-declared_origin_ns)/1_000_000
    corrected = from_error-baseline['capture_offset_ms']
    require(abs(corrected) <= baseline['horizon_half_width_ms'],
            'Actual full music prefix missed its immutable horizon after independent capture correction')
    return {'passed':True,'verified_frames':RATE*21,'coded_frames':RATE*20,'constant_tail_frames':RATE,
        'sample_start_frame':first//4,'actual_first_sample_pts_ns':observed_origin,
        'declared_final_origin_ns':declared_origin_ns,'corrected_horizon_error_ms':corrected,
        'predeclared_half_width_ms':baseline['horizon_half_width_ms'],
        'reference_sha256':hashlib.sha256(reference).hexdigest(),
        'scope':'Exact unchanged21s local stereoS16PCM, no freely fitted phase/gain or missing prefix; physical output unverified'}


async def freeze_complete_music(context, observer, origin_ns, *, timeout=1):
    """Wait for the admitted capture interval, never for analysis to hide a tail."""
    frozen = {}
    async def captured():
        values = {}
        for identifier,capture in context.captures.items():
            snapshot=context.group.capture_snapshot(capture)
            baseline=context.report['independent_capture_baselines'][identifier]
            offset,width=baseline['capture_offset_ms'],baseline['horizon_half_width_ms']
            require(type(offset) in (int,float) and type(width) in (int,float)
                    and math.isfinite(offset) and math.isfinite(width) and width >= 0,
                    'Music tail has no finite predeclared capture uncertainty')
            if not snapshot.chunks:
                return False
            last=snapshot.chunks[-1]
            anchor=snapshot.absolute.get(snapshot.captured_at[-1])
            require(type(anchor) is int and anchor > 0 and type(last) is bytes and last and len(last)%4 == 0,
                    'Music tail lost its actual final capture anchor or whole frame')
            through=anchor+(len(last)//4)*1_000_000_000//RATE
            required=origin_ns+21_000_000_000+math.ceil((offset+width)*1_000_000)
            if through < required:
                return False
            values[identifier]=snapshot
        frozen.update(values)
        return True
    await epoch.observed_wait(context,observer,captured,
        'Actual final capture did not include its complete original music tail within the bounded observation',timeout)
    # capture_snapshot polls new buffers; validate their content before authority.
    await observer.check()
    return frozen


async def check_original_actors(context, healths, units, grants):
    """Actual backend selection and original admission remain authoritative."""
    group = context.group
    for identifier, state in context.states.items():
        require(context.broker.rooms.get(identifier) is state and state.status == 'running'
                and {name: unit.identity() for name, unit in state.processes.items()} == units[identifier]
                and all(unit.alive for unit in state.processes.values()),
                'An original epoch unit changed during the initial coded program')
        state.local_pin.validate()
        actual_outputs = await state.client.outputs(set())
        selected = [item for item in actual_outputs if item['selected']]
        require(state.selected_ids == ['0'] and len(selected) == 1 and selected[0]['id'] == '0'
                and type(selected[0].get('offset_ms')) is int and selected[0]['offset_ms'] == 0,
                'Original music output selection or correction changed')
        require(type(healths[identifier].get('dropped_bytes')) is int and healths[identifier]['dropped_bytes'] == 0
                and type(healths[identifier].get('speech_dropped_frames')) is int
                and healths[identifier]['speech_dropped_frames'] == 0
                and healths[identifier].get('speech_output_error') is None
                and healths[identifier].get('speech_output_errno') is None
                and healths[identifier].get('speech_session_id') is None,
                'Music-only worker dropped audio or acquired speech')
        if identifier in grants:
            owner = healths[identifier]['source']['owner']
            grant = group.producer_status(context.producers[identifier])
            require(type(owner) is dict and all(owner.get(key) == grants[identifier][key]
                    for key in ('session_id', 'epoch', 'incarnation'))
                    and grant['session_id'] == grants[identifier]['session_id'],
                    'Initial final PCM lost its exact admitted native source')
            require(type(healths[identifier].get('native_blocks')) is int
                    and healths[identifier]['native_blocks'] >= 0, 'Native block count is not an exact counter')
            require(all(grant[key] == grants[identifier][key]
                        for key in ('session_id', 'epoch', 'incarnation', 'generation')),
                    'Original producer grant changed its exact incarnation or generation')
            if healths[identifier]['native_blocks']:
                require(healths[identifier]['native_generation'] == grants[identifier]['generation']
                        and healths[identifier]['native_group_id'] == group.GROUP,
                        'Initial final PCM changed its original native generation/group clock')
        else:
            require(healths[identifier]['source']['owner'] is None, 'Cold idle target acquired a native owner')


async def exercise(context):
    """Only original cold music; no offer/priming/TTS or route mutation."""
    group = context.group
    before = await epoch.worker_health(context)
    require(plan_receipt(context.phase, [s.desired for s in context.broker.rooms.values()], before, before_pcm=True)
            == context.frozen_plan, 'Epoch plan changed immediately before original receiver preparation')
    monitor = context.broker._monitor
    require(monitor is not None and not monitor.done(), 'Production health monitor is absent before epoch preparation')
    try:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        for identifier in (A, B):
            state = context.states[identifier]
            root = state.directory/'native-validation-epoch'/context.phase.epoch_id
            root_directory(root)
            context.producers[identifier] = await group.launch_producer(context.broker, state, root, 0, duration_seconds=90, arrival_lead_ns=NATIVE_LEAD_NS,
                frozen_timing=group.FrozenWorkerTiming(HORIZON_MS*1_000_000,
                    tuple((key, BUFFER_MS) for key in sorted((A, B)))), music_minimum=True)
    finally:
        context.broker._monitor = asyncio.create_task(context.broker._health_monitor(), name='native-epoch-runtime-health')
    async def ready_before_begin():
        values = {identifier: group.producer_status(handle) for identifier, handle in context.producers.items()}
        return values if all(value and value['stage'] == 'ready_before_begin' and value['frames'] == 0
                             and 'begin_attempt_ns' not in value for value in values.values()) else None
    ready = await group.observation.base.eventually(ready_before_begin, 'both exact receiver units before BEGIN', timeout=8)
    context.evidence['producer_ready_before_begin'] = deepcopy(ready)
    units = {identifier: {name: unit.identity() for name, unit in state.processes.items()}
             for identifier, state in context.states.items()}
    context.evidence['initial_unit_identities'] = deepcopy(units)
    initial_players = {}
    for identifier in context.producers:
        state = context.states[identifier]
        require((await state.client.request('GET', '/api/player'))['state'] == 'stop',
                'Cold OwnTone started before the immutable native calendar')
        context.latency.playback_closed(state.local_pin.manifest)
        pin = state.local_pin.manifest
        capture = context.capture_factory(f"hw:{pin['card_index']},{1-pin['device']},7", context.clock, context.base_time, context.clock_offset)
        context.captures[identifier] = capture
        context.pcm_guards[identifier] = group.FinalPcmGuard(capture)
        capture.start()
    require(plan_receipt(context.phase, [s.desired for s in context.broker.rooms.values()],
            await epoch.worker_health(context), before_pcm=True) == context.frozen_plan,
            'Actual plan/source changed before original BEGIN release')
    require(context.broker._monitor is not None and not context.broker._monitor.done(),
            'Production health observer was not restored before calendar release')
    available = time.monotonic_ns()
    start = available+NATIVE_LEAD_NS
    command = {'generation': 2, 'action': 'begin', 'available_ns': available, 'common_start_ns': start}
    context.evidence.update(common_presentation_ns=start, nominal_first_available_ns=available,
                            calendar_frozen_before_begin=True, captures_armed_before_begin=True,
                            production_health_monitor_restored_before_begin=True)
    for handle in context.producers.values():
        group.publish_command(handle['command'], command, handle['account'])
    async def granted():
        values = {identifier: group.producer_status(handle) for identifier, handle in context.producers.items()}
        for value in values.values():
            if value and value.get('begin_attempt_ns'):
                require(value['available_ns'] == available and value['common_start_ns'] == start
                        and value['ready_before_begin_ns'] < available <= value['begin_attempt_ns'],
                        'A producer replaced or preceded its frozen pre-BEGIN calendar')
        return values if all(value and value['stage'] in {'granted', 'streaming'} for value in values.values()) else None
    grants = await group.observation.base.eventually(granted, 'exact original grants and immutable calendar', timeout=8)
    context.evidence['producer_grants'] = deepcopy(grants)
    indexes, progress = {}, {}
    async def initial_pcm():
        healths = await epoch.worker_health(context)
        require(plan_receipt(context.phase, [s.desired for s in context.broker.rooms.values()], healths, before_pcm=False)
                == context.frozen_plan, 'Actual worker plan changed after first PCM')
        await check_original_actors(context, healths, units, grants)
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
                npt, now = player.get('item_progress_ms'), time.monotonic()
                require(type(npt) is int and npt >= 0, 'Music startup NPT is invalid')
                if identifier not in progress:
                    progress[identifier] = [now, npt, npt]
                first_at, first_npt, previous = progress[identifier]
                require(npt >= previous and abs(npt-first_npt-(now-first_at)*1000) < 1500,
                        'Original music NPT stalled, sought or moved backwards')
                progress[identifier][2] = npt
        return len(indexes) == len(context.producers)
    await epoch.observed_wait(context, None, initial_pcm, 'Epoch did not establish all actual final native PCM', 20)
    observer = MusicObserver(context, initial_pcm)
    context.observer = observer
    await observer.initialize()
    # This original-source observer runs through all asynchronous analysis.
    # Every buffer remains checked from actual onset throughout the code.
    end = start+context.declared_horizon_ns+max(0, context.phase.offset_ms)*1_000_000+21_000_000_000
    while time.monotonic_ns() < end:
        await observer.check()
        await asyncio.sleep(.03)
    complete_snapshots = await freeze_complete_music(context,observer,start+context.declared_horizon_ns)
    calendars = {}
    series = {}
    for identifier in context.captures:
        output_offset = context.phase.offset_ms if identifier == A else 0
        origin = start+context.declared_horizon_ns+output_offset*1_000_000
        first, last = origin+2_000_000_000, origin+18_000_000_000
        snapshot = complete_snapshots[identifier]
        actual = await asyncio.to_thread(group.modulation, snapshot, first, last)
        reference = group.declared_capture(origin, group.RATE*2, group.RATE*18)
        declared = await asyncio.to_thread(group.modulation, reference, first, last)
        displacement = await asyncio.to_thread(group.align_series, declared, actual, maximum_delay_ms=None, search_ms=1000)
        calendars[identifier] = {'declared_final_origin_ns': origin, 'alignment': displacement,
            'horizon': group.validate_horizon(displacement, context.report['independent_capture_baselines'][identifier]),
            'frame_continuity': snapshot.frame_continuity}
        series[identifier] = actual
    context.evidence['native_calendars'] = calendars
    context.evidence['complete_music_prefix'] = {}
    for identifier in context.captures:
        snapshot = complete_snapshots[identifier]
        verified = await asyncio.wait_for(asyncio.to_thread(verify_complete_music, snapshot, group.program_pcm,
            start+context.declared_horizon_ns, context.report['independent_capture_baselines'][identifier]), 5)
        await observer.check()
        context.evidence['complete_music_prefix'][identifier] = verified
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
    baseline_start = {key:len(capture.chunks) for key,capture in context.captures.items()}
    deadline = time.monotonic()+1.2
    while time.monotonic() < deadline:
        await observer.check()
        await asyncio.sleep(.02)
    for identifier, capture in context.captures.items():
        baseline = group.observation.spectrum(capture.chunks[baseline_start[identifier]:], group.RATE)
        group.observation.require_music(baseline, 1)
        require(baseline['speech_880_amplitude'] <= 4, 'Music-only tail baseline was contaminated by voice')
        context.pcm_guards[identifier].preserve_reference(baseline)
    await observer.sample()
    final = {identifier: group.producer_status(handle) for identifier, handle in context.producers.items()}
    for value in final.values():
        require(value['available_ns'] == available and value['common_start_ns'] == start
                and value['first_pcm_frame'] == value['first_pcm_sequence'] == 0
                and value['first_pcm_native_presentation_ns'] == start
                and value['first_pcm_lateness_ns'] < SEND_LATE_NS,
                'Music startup lost its original first-frame calendar/source receipt')
    context.evidence.update(producer_final=deepcopy(final), music_calendar_verified=True,
        measured_signal_timeline_cleanup_passed=False, speech_exercised=False,
        speech_latency_performance_passed=False, speech_latency_performance_status='not_exercised',
        cold_utterance_completeness_passed=False, cold_utterance_completeness_status='not_exercised')
    return observer
