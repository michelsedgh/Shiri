"""Opt-in abrupt room-failure proof; importing creates no kernel resources.

The grouped fixture supplies its exact owned candidate. Only the target room's
admitted OwnTone/audio MainPID can be killed, through a held pidfd and frozen
cgroup. The other room retains its original continuous observer and program.
"""
from __future__ import annotations

# Bounded manual evidence, proc identity and kernel ownership reads.
# ruff: noqa: ASYNC240

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import os
from pathlib import Path
import select
import signal
import sys
import time
from uuid import uuid4

from shiri.rpc import call_rpc
from shiri.runtime.system import RuntimeFailure, boot_id, process_birth, root_directory

PRODUCER_SECONDS = 180
CAPTURE_BYTES = 48 * 1024 * 1024
FAULT_SECONDS = 60
RECOVERY_SECONDS = 25
CONTROL_SAMPLES = 6000
INNER_SECONDS = 280
EXTERNAL_SECONDS = 400


def require(value, message):
    if not value:
        raise RuntimeFailure(message)


def capture_type(base):
    """Retain actual offset/caps/discontinuity checks with an explicit budget."""
    class FaultCapture(base):
        maximum_bytes = CAPTURE_BYTES

        def poll(self):
            require(self.error is None, self.error or 'Final capture failed')
            require(self.capture_dropped == 0, 'Fault observer dropped final PCM')
            if self.needs_latency:
                self.needs_latency = False
                self.pipeline.recalculate_latency()
            try:
                while self.pending:
                    at, data, rate, channels, audio_format, metadata = self.pending[0]
                    require(bool(data) and len(data) % 4 == 0, 'Fault output contains incomplete stereo frames')
                    require((rate, channels, audio_format) == (48000, 2, 'S16LE'), 'Fault capture changed PCM contract')
                    if self.rate is not None:
                        require((rate, channels, audio_format) == (self.rate, self.channels, self.format),
                                'Fault capture renegotiated')
                    require(self.total + len(data) <= CAPTURE_BYTES, 'Fault capture exceeded48MiB')
                    self.sequence.push(metadata, len(data) // 4, rate)
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
                    'common_clock_base_ns': self.expected_base, 'clock_offset_to_monotonic_ns': self.clock_offset_ns}
    return FaultCapture


@dataclass
class FaultContext:
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
    untouched_health_check: object | None = None
    minimum_timing: object | None = None
    timing_check: object | None = None


def require_minimum_timing(context, identifier, definition, health, *, before_pcm=False):
    if getattr(context, 'minimum_timing', None) is not None:
        require(callable(context.timing_check), 'Minimum fault coverage lost its actual timing verifier')
        context.timing_check(context.minimum_timing, identifier, definition, health, before_pcm=before_pcm)


def cgroup_members(manager, descriptor):
    values = manager.cgroup_read(descriptor, 'cgroup.procs').splitlines()
    require(0 < len(values) <= 128 and all(value.isdecimal() and int(value) > 1 for value in values),
            'Fault target cgroup has an invalid process set; nothing was signalled')
    return {int(value) for value in values}


async def kill_owned_main(broker, state, role, evidence, *, expected_room_id):
    """One abrupt signal; no unit stop, reservation removal, or intent edits.

    Every descriptor is held through signal admission. A cancellation or failed
    identity check thaws that exact cgroup and closes all owned descriptors.
    """
    require(sys.platform == 'linux' and os.geteuid() == 0 and hasattr(os, 'pidfd_open')
            and hasattr(signal, 'pidfd_send_signal'), 'Abrupt fault injection requires Linux root and pidfds')
    require(state.desired.id == expected_room_id and role in {'owntone', 'audio'}
            and broker.rooms.get(expected_room_id) is state,
            'Refuse a fault outside the exact target room/role')
    unit = state.processes.get(role)
    require(unit is not None, 'Exact fault target unit is absent')
    entry = deepcopy(await unit.coherent_identity())
    key = f'{state.desired.id}:{role}'
    manager = unit.manager

    def reservation():
        require(broker.rooms.get(state.desired.id) is state and state.processes.get(role) is unit
                and unit.identity() == entry and broker.network.manifest['processes'].get(key) == entry,
                'Fault target handle/reservation changed; nothing was signalled')
        require(entry.get('name') == role and entry.get('boot_id') == boot_id(),
                'Fault target role or boot changed; nothing was signalled')

    reservation()
    group_fd = pid_fd = None
    thaw_needed = False
    primary = None
    try:
        actual = await manager.inspect(entry['unit'])
        reservation()
        require(actual is not None, 'Fault target disappeared before admission')
        manager.verify(entry, actual)
        pid = actual.get('MainPID')
        require(type(pid) is int and pid > 1 and pid == unit.process.pid
                and actual.get('ActiveState') == 'active', 'Fault target is not the exact active MainPID')
        birth = process_birth(pid)
        require(birth is not None, 'Fault target has no live process birth')
        group_fd = manager.open_cgroup(entry)
        held = os.fstat(group_fd)
        require(held.st_ino == entry.get('cgroup_inode'), 'Fault target kernel cgroup was replaced')
        require(pid in cgroup_members(manager, group_fd), 'Fault MainPID is outside its exact owned cgroup')
        pid_fd = os.pidfd_open(pid, 0)
        # pidfd_open binds the actual process. Check birth again after capturing
        # it, before any await or signal; a same-number successor is rejected.
        require(process_birth(pid) == birth, 'Fault MainPID was reused before pidfd admission')
        evidence.update(role=role, unit=entry['unit'], invocation_id=entry['invocation_id'],
                        boot_id=entry['boot_id'], main_pid=pid, process_birth=birth,
                        cgroup={'st_dev': held.st_dev, 'st_ino': held.st_ino}, signal_sent=False)
        thaw_needed = True
        require(manager.write_control(group_fd, 'cgroup.freeze', b'1'), 'Fault target vanished before freeze')
        freeze_deadline = time.monotonic()+1.5
        for _ in range(150):
            require(time.monotonic() < freeze_deadline, 'Fault cgroup freeze exceeded its1.5s bound')
            if 'frozen 1' in manager.cgroup_events(group_fd):
                break
            await asyncio.sleep(.01)
        else:
            raise RuntimeFailure('Fault cgroup did not freeze within its1.5s bound; nothing was signalled')
        # Inspect the invocation and held cgroup again around awaited manager
        # calls. We never signal a PID obtained from a replacement invocation.
        reservation()
        actual = await manager.inspect(entry['unit'])
        reservation()
        require(actual is not None and actual.get('MainPID') == pid and actual.get('ActiveState') == 'active',
                'Fault MainPID/invocation changed while frozen')
        manager.verify(entry, actual)
        proof = manager.open_cgroup(entry)
        try:
            checked = os.fstat(proof)
            require((checked.st_dev, checked.st_ino) == (held.st_dev, held.st_ino),
                    'Fault cgroup name no longer identifies the held kernel instance')
        finally:
            os.close(proof)
        require(pid in cgroup_members(manager, group_fd) and process_birth(pid) == birth,
                'Frozen fault MainPID/birth no longer belongs to its admitted cgroup')
        poll = select.poll()
        poll.register(pid_fd, select.POLLIN)
        require(not poll.poll(0), 'Fault MainPID already exited; no injected crash can be claimed')
        signal.pidfd_send_signal(pid_fd, signal.SIGKILL)
        evidence.update(signal_sent=True, signal='SIGKILL',
                        signalled_monotonic_ns=time.monotonic_ns(), reservation_retained=True)
    except BaseException as exc:
        primary = exc
        raise
    finally:
        cleanup_error = None
        if thaw_needed and group_fd is not None:
            try:
                require(manager.write_control(group_fd, 'cgroup.freeze', b'0')
                        or 'populated 0' in manager.cgroup_events(group_fd), 'Exact fault cgroup could not be thawed')
                evidence['held_cgroup_thawed'] = True
            except Exception as exc:
                evidence['thaw_error'] = type(exc).__name__
                cleanup_error = exc
        for descriptor in (pid_fd, group_fd):
            if descriptor is not None:
                os.close(descriptor)
        evidence['owned_descriptors_closed'] = True
        if cleanup_error is not None and primary is None:
            raise cleanup_error


class FaultObserver:
    """One sticky uninterrupted B guard, spanning the monitor handoff/tail."""
    def __init__(self, context, evidence):
        self.context, self.evidence = context, evidence
        self.room = context.untouched
        self.state = context.states[self.room]
        self.capture = context.captures[self.room]
        self.guard = context.pcm_guards[self.room]
        require(self.guard.reference is not None, 'Fault isolation requires an established untouched-zone reference')
        self.units = {name: deepcopy(unit.identity()) for name, unit in self.state.processes.items()}
        self.sender_units = {name: deepcopy(unit.identity()) for name, unit in context.broker.sender_processes.items()}
        self.intent = self.state.desired.model_dump(mode='json')
        self.owner = self.player = self.health = None
        self.samples = []
        self.progress = self.progress_at = self.progress_origin = None
        self.error = None
        self.done = asyncio.Event()
        self.watcher = None

    async def initialize(self):
        self.health = await call_rpc(self.context.broker._worker_socket(self.state), 'health', {}, timeout=2)
        self.owner = deepcopy(self.health['source']['owner'])
        if self.health.get('speech_mix') == 'owntone_player':
            require(self.owner is not None and callable(getattr(self.context, 'untouched_health_check', None)),
                    'Untouched late-mix room lacks its exact music owner or final PCM health observer')
            self.context.untouched_health_check(self.health, self.guard)
        else:
            require(self.owner is not None and self.health['music_gain'] == 1, 'Untouched room is not carrying its exact music program')
        self.player = await self.state.client.request('GET', '/api/player')
        require(self.player['state'] == 'play', 'Untouched room must be playing before fault admission')
        await self._sample()
        self.watcher = asyncio.create_task(self._watch(), name='native-zone-fault-untouched-observer')
        await asyncio.sleep(0)
        await self.check()

    def check_pcm(self):
        require(self.error is None, self.error or 'Untouched-zone isolation failed permanently')
        try:
            require(self.context.captures.get(self.room) is self.capture and self.context.pcm_guards.get(self.room) is self.guard,
                    'Fault test replaced the original untouched-zone observer')
            self.guard.check()
        except Exception as exc:
            self.error = str(exc)
            raise

    async def _sample(self):
        # Readiness waits cannot erase an observed control violation by retrying
        # after the bad value changes back. PCM and controls share one fence.
        try:
            await self._sample_once()
        except Exception as exc:
            self.error = str(exc)[:2000]
            raise

    async def _sample_once(self):
        self.check_pcm()
        broker, state = self.context.broker, self.state
        require(broker.rooms.get(self.room) is state and broker.ready and state.status == 'running',
                'Untouched-zone runtime/room readiness changed during a peer fault')
        require(state.desired.model_dump(mode='json') == self.intent, 'Peer fault changed untouched saved intent')
        require({name: unit.identity() for name, unit in state.processes.items()} == self.units
                and all(unit.alive for unit in state.processes.values()), 'Peer fault changed an untouched unit invocation')
        require({name: unit.identity() for name, unit in broker.sender_processes.items()} == self.sender_units
                and all(unit.alive for unit in broker.sender_processes.values()), 'Peer fault changed shared sender units')
        outputs = await state.client.outputs(set())
        require(state.selected_ids == ['0'] and [value['id'] for value in outputs if value['selected']] == ['0'],
                'Peer fault changed actual untouched output selection')
        health = await call_rpc(broker._worker_socket(state), 'health', {}, timeout=2)
        require_minimum_timing(self.context, self.room, state.desired, health)
        require(health['ready'] and not health.get('error') and health['source']['ready']
                and health['source']['owner'] == self.owner and health['native_generation'] == self.health['native_generation']
                and health['native_group_id'] == self.health['native_group_id'], 'Peer fault changed the untouched exact source/timeline')
        if health.get('speech_mix') == 'owntone_player':
            require(callable(getattr(self.context, 'untouched_health_check', None)),
                    'Late-mix fault observer lacks its original final PCM health check')
            self.context.untouched_health_check(health, self.guard)
        else:
            require(health['music_gain'] == 1 and health['speech_session_id'] is None
                    and health['dropped_bytes'] == health['speech_dropped_frames'] == 0,
                    'Peer fault ducked, injected speech, or dropped untouched music')
        player = await state.client.request('GET', '/api/player')
        require(player['state'] == 'play' and player['item_id'] == self.player['item_id']
                and player.get('volume') == state.current_volume == self.intent['volume'],
                'Peer fault paused/changed the untouched program/volume')
        progress, at = player.get('item_progress_ms'), time.monotonic()
        require(type(progress) is int and progress >= 0, 'Untouched NPT is invalid')
        if self.progress_origin is None:
            self.progress_origin = (at, progress)
        else:
            origin_at, origin_progress = self.progress_origin
            require(abs(progress-origin_progress-(at-origin_at)*1000) < 1500,
                    'Untouched NPT stalled or jumped from its original program timeline')
        if self.progress is None or progress > self.progress:
            self.progress, self.progress_at = progress, at
        else:
            require(progress == self.progress and at-self.progress_at <= 2,
                    'Untouched NPT moved backwards or stopped advancing')
        require(len(self.samples) < CONTROL_SAMPLES, 'Fault control evidence exceeded its6000sample bound')
        self.samples.append({'at': at, 'progress_ms': progress, 'native_blocks': health['native_blocks'],
                             'native_generation': health['native_generation'], 'source_epoch': self.owner['epoch']})
        self.check_pcm()

    async def _watch(self):
        try:
            while not self.done.is_set():
                await self._sample()
                try:
                    await asyncio.wait_for(self.done.wait(), .06)
                except asyncio.TimeoutError:
                    pass
        except Exception as exc:
            self.error = str(exc)
            raise

    async def check(self):
        self.check_pcm()
        if self.watcher is not None and self.watcher.done():
            self.watcher.result()
            raise RuntimeFailure('Untouched fault observer ended unexpectedly')

    def receipt(self):
        return {'source_owner': self.owner, 'original_unit_identities': self.units,
                'shared_sender_identities': self.sender_units, 'control_samples': list(self.samples),
                'original_capture_preserved': self.context.captures.get(self.room) is self.capture,
                'pcm': self.guard.evidence(), 'failure': self.error,
                'scope': 'Original shared-clock final digital PCM/source/item/NPT; no physical speaker claim'}

    async def close(self):
        self.done.set()
        if self.watcher is not None:
            self.watcher.cancel()
            result = await asyncio.gather(self.watcher, return_exceptions=True)
            if isinstance(result[0], Exception) and not isinstance(result[0], asyncio.CancelledError):
                self.error = str(result[0])[:2000]
        try:
            self.check_pcm()
        except Exception as exc:
            self.error = str(exc)[:2000]
        if self.error is not None:
            self.evidence['passed'] = False
        self.evidence['untouched'] = self.receipt()
        require(self.error is None, self.error or 'Untouched-zone observer failed during close')


async def wait_observed(predicate, observer, seconds, message):
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        await observer.check()
        result = await asyncio.wait_for(predicate(), max(0, deadline-time.monotonic()))
        await observer.check()
        if result:
            return result
        await asyncio.sleep(.03)
    raise RuntimeFailure(message)


async def recovered(context, previous, observer, evidence):
    target, state = context.target, context.states[context.target]
    intent = previous['intent']
    seen = []
    async def ready():
        snapshot = state.snapshot()
        require(len(seen) < 1000, 'Recovery observation exceeded its bounded record count')
        seen.append({'at': time.monotonic(), 'status': snapshot['status'],
                     'error': snapshot['error'], 'retry_in_seconds': snapshot['retry_in_seconds']})
        if state.status != 'running' or state.launch_generation == previous['launch_generation']:
            return None
        require(state.desired.model_dump(mode='json') == intent and state.desired.enabled,
                'Room autoheal changed saved intent')
        require(state.launch_generation and all(unit.alive for unit in state.processes.values()),
                'Recovered room is missing live owned units')
        for role in ('owntone', 'audio', 'shairport'):
            require(state.processes[role].identity()['invocation_id'] != previous['units'][role]['invocation_id'],
                    'Room recovery reused a retired service invocation')
        state.local_pin.validate()
        require(state.local_pin.manifest == previous['pcm_pin'] and state.selected_ids == ['0'],
                'Autoheal did not restore the exact saved local output')
        outputs = await state.client.outputs(set())
        require([output['id'] for output in outputs if output['selected']] == ['0'],
                'Recovered room selected a different actual output')
        health = await call_rpc(context.broker._worker_socket(state), 'health', {}, timeout=2)
        require_minimum_timing(context, target, state.desired, health, before_pcm=True)
        require(health['ready'] and health['source']['ready'] and health['source']['owner'] is None
                and health['source']['incarnation'] != previous['source_incarnation'],
                'Autoheal did not produce a fresh ready idle music actor')
        saved = await context.api.room(target)
        from shiri.domain import Room
        require({key: value for key, value in saved.items() if key in Room.model_fields} == intent,
                'Autoheal changed durable room configuration')
        return health
    try:
        health = await wait_observed(ready, observer, RECOVERY_SECONDS, 'Target room did not autoheal within25s')
        evidence.update(recovered_health=health, recovered_units={name: unit.identity() for name, unit in state.processes.items()},
                        recovered_launch_generation=state.launch_generation,
                        original_stream_resumed=False, original_stream_scope='Original sender connection ended; fresh fixture reconnect required')
        return health
    finally:
        evidence['recovery_observations'] = seen


async def reconnect(context, observer, phase):
    """Synthetic setup only; actual monitor observes the entire crash/heal."""
    state, broker = context.states[context.target], context.broker
    record = {'started_monotonic_ns': time.monotonic_ns(), 'monitor_suspended_only_for_fixture_replacement': True}
    phase['synthetic_reconnect'] = record
    monitor = broker._monitor
    require(monitor is not None and not monitor.done(), 'Actual production health monitor was not active after autoheal')
    try:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        await observer.check()
        root = state.directory/'native-validation-faults'/uuid4().hex
        root_directory(root)
        producer = await context.launch_producer(broker, state, root, 0, duration_seconds=PRODUCER_SECONDS)
        context.producers[context.target] = producer
        record['receiver_unit'] = producer['unit'].identity()
    finally:
        # Scheduling the coroutine creates no media. Its first poll still uses
        # the normal5s production cadence, as in the original fixture setup.
        broker._monitor = asyncio.create_task(broker._health_monitor(), name='native-zone-fault-runtime-health')
        record['monitor_restored_monotonic_ns'] = time.monotonic_ns()
        record['continuous_other_room_observer'] = True
    async def granted():
        status = context.producer_status(producer)
        return status if status and status['stage'] in {'granted', 'streaming'} else None
    grant = await wait_observed(granted, observer, 5, 'Fresh exact receiver did not receive its native grant')
    if getattr(context, 'minimum_timing', None) is not None:
        health = await call_rpc(broker._worker_socket(state), 'health', {}, timeout=2)
        require_minimum_timing(context, context.target, state.desired, health)
        record['frozen_worker_timing'] = context.minimum_timing.receipt()
    start = time.monotonic_ns()+1_000_000_000
    context.publish_command(producer['command'], {'generation': 2, 'action': 'run', 'common_start_ns': start}, producer['account'])
    state.local_pin.validate()
    card, device = state.local_pin.manifest['card_index'], state.local_pin.manifest['device']
    capture = context.capture_factory(f'hw:{card},{1-device},7', context.clock, context.base_time, context.clock_offset)
    context.captures[context.target] = capture
    guard = context.guard_factory(capture)
    context.pcm_guards[context.target] = guard
    capture.start()
    async def audible():
        capture.poll()
        onset = context.onset_index(capture)
        if onset is None:
            return None
        guard.begin(onset)
        guard.check()
        health = await call_rpc(broker._worker_socket(state), 'health', {}, timeout=2)
        require_minimum_timing(context, context.target, state.desired, health)
        require(health['source']['owner']['session_id'] == grant['session_id'] and health['source']['ready'],
                'Fresh reconnect PCM does not belong to its exact admitted source')
        return {'health': health, 'capture': capture.poll(), 'native_grant': grant,
                'common_start_monotonic_ns': start, 'independent_room_stream_reconnect': True}
    phase['fresh_stream'] = await wait_observed(audible, observer, 8, 'Fresh target receiver did not produce actual final440 PCM')


async def exercise(context):
    require(context.target != context.untouched and set(context.states) == {context.target, context.untouched},
            'Fault proof requires exactly two distinct configured zones')
    evidence = {'passed': False, 'target_room_id': context.target, 'untouched_room_id': context.untouched,
                'phase_deadline_seconds': FAULT_SECONDS, 'recovery_deadline_seconds': RECOVERY_SECONDS,
                'max_capture_bytes_per_observer': CAPTURE_BYTES, 'faults': [],
                'scope': 'Exact owned SIGKILL + production per-room autoheal + deliberate synthetic receiver reconnect; no phone auto-resume/physical proof'}
    context.report['zone_faults'] = evidence
    observer = FaultObserver(context, evidence)
    try:
        await observer.initialize()
        # The new independent B observer is already active before the old
        # all-room monitor is joined. No dropped observation interval exists.
        await context.handoff()
        async def faults():
            for role in ('owntone', 'audio'):
                state = context.states[context.target]
                await observer.check()
                health = await call_rpc(context.broker._worker_socket(state), 'health', {}, timeout=2)
                require(state.status == 'running' and health['source']['owner'] is not None
                        and health['source']['ready'], 'Fault target is not carrying an admitted live program')
                phase = {'role': role, 'before': {
                    'launch_generation': state.launch_generation, 'source_incarnation': health['source']['incarnation'],
                    'source_owner': deepcopy(health['source']['owner']),
                    'units': {name: unit.identity() for name, unit in state.processes.items()},
                    'pcm_pin': deepcopy(state.local_pin.manifest), 'intent': state.desired.model_dump(mode='json')}}
                evidence['faults'].append(phase)
                capture = context.captures[context.target]
                phase['historical_final_pcm'] = context.retain_capture(
                    capture, context.temporary, f'{context.target}-before-{role}-fault')
                await asyncio.wait_for(asyncio.to_thread(capture.close), 3)
                require(capture.pipeline.get_state(0).state == capture.Gst.State.NULL,
                        'Old target capture did not release before explicit fault')
                phase['before']['capture_closed_before_explicit_fault'] = True
                require(context.broker._monitor is not None and not context.broker._monitor.done(),
                        'Production health monitor is absent before fault injection')
                await asyncio.wait_for(kill_owned_main(context.broker, state, role, phase,
                                                     expected_room_id=context.target), 5)
                await recovered(context, phase['before'], observer, phase)
                await reconnect(context, observer, phase)
                await observer.check()
                phase['passed'] = True
        await asyncio.wait_for(faults(), FAULT_SECONDS)
        require(observer.samples[-1]['progress_ms'] > observer.samples[0]['progress_ms']+1000,
                'Untouched NPT did not advance through actual peer faults')
        evidence['passed'] = True
        evidence['untouched'] = observer.receipt()
        return observer
    except BaseException:
        # Preserve the original fault/cancellation while still joining our
        # observer. A cleanup failure remains visible in its retained receipt.
        try:
            await observer.close()
        except Exception as exc:
            evidence['observer_cleanup_error'] = type(exc).__name__
        raise
