#!/usr/bin/env python3
"""Manual two-zone native-clock / API speech check; never runs under pytest.

Run only in an explicitly admitted disposable native lab, supervised by an external
whole-process watchdog. It opens Loopback sub7 in both directions, selects
ONLY the corresponding virtual local0 outputs, and never opens sub0 or sub2.
Synthetic native SOCK_SEQPACKET producers run in exact receiver-UID units.
This proves the candidate software route and instrumented final-PCM timing;
it does not prove stock-phone groups, physical acoustics, Cast or Bluetooth.

  sudo systemd-run --wait --pipe --collect --unit=shiri-v2-native-group-check \
    --property=RuntimeMaxSec=400 --property=TimeoutStopSec=15 \
    --property=KillMode=control-group \
    /opt/shiri-v2/.venv/bin/python \
    /opt/shiri-v2/tests/linux/run_native_grouping.py

A killed parent may leave independent owned daemon units: preserve and inspect
ownership.json, then use the broker's exact recovery before retrying. Producers
also self-exit after90s. No source/PCM truth is inferred from a play flag.
"""

from __future__ import annotations

# Manual, bounded kernel/config observations occur between awaits.
# ruff: noqa: ASYNC240

import argparse
import asyncio
from contextlib import suppress
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from functools import lru_cache
import grp
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import pwd
import re
import secrets
import signal
import socket
import stat
import statistics
import sys
import tempfile
import time
from types import FunctionType, SimpleNamespace
from typing import NamedTuple
from uuid import UUID, uuid4

import httpx
import numpy as np
from aiortc import RTCSessionDescription

from shiri.domain import RoomCreate
from shiri.rpc import call_rpc
from shiri.runtime.broker import Broker
from shiri.runtime.layout import directory, file_owner
from shiri.runtime.system import RuntimeFailure, atomic_json, process_birth, root_directory
from shiri.runtime.latency import latency_plan
from shiri.runtime.timing import Clock, FLAG_AIRPLAY2, FLAG_GROUP_LEADER, Kind, Packet, RATE, RELAY_DELAY_NS
from shiri.runtime.units import Bind, VIEW
from shiri.settings import Settings
from shiri.store import Store

HERE = Path(__file__).resolve()
PROJECT = HERE.parents[2]
STATE = Path('/var/lib/shiri-v2-test-runtime')
RUN = Path('/run/shiri-v2-test')
WORK = Path('/var/lib/shiri-v2-native-group-review')
BINARIES = Path('/opt/shiri-v2-next9-deps')
IDENTITIES = Path('/etc/shiri-v2-test/daemon-identities.json')
RESULT = Path('/tmp/shiri-v2-native-grouping-result.json')
API_BASE = 'http://127.0.0.1:18084'
A = 'b6786543-7eb2-443d-83b1-65b984123a76'
B = '5356832c-c514-4472-bb4b-4f34ed86dd07'
ZONES = {A: {'slot': 6, 'device': 1, 'name': 'Shiri validation A', 'nobly': 'native-probe-a'},
         B: {'slot': 7, 'device': 0, 'name': 'Shiri validation B', 'nobly': 'native-probe-b'}}
MAX_DURATION = 90
CODE_SECONDS = 20
CODE_CHIP_FRAMES = RATE * 120 // 1000
CLOCK_SAMPLE_ATTEMPTS = 4
CLOCK_SAMPLE_BUDGET_NS = 5_000_000
CLOCK_BRACKET_LIMIT_NS = 1_000_000
CLOCK_RECOVERY_BATCHES = 4
CLOCK_RECOVERY_BUDGET_NS = 20_000_000
SEND_LATE_NS = 150_000_000
GROUP = '894a68d5-3cd8-44bd-9667-34f558249b84'
FLAGS = FLAG_AIRPLAY2
_probe_spec = importlib.util.spec_from_file_location('native_group_kernel_fixture', HERE.with_name('loopback_capture_probe.py'))
kernel_probe = importlib.util.module_from_spec(_probe_spec)
_probe_spec.loader.exec_module(kernel_probe)
_lan_spec = importlib.util.spec_from_file_location('native_group_isolated_lan', HERE.with_name('isolated_group_lan.py'))
isolated_lan = importlib.util.module_from_spec(_lan_spec)
_lan_spec.loader.exec_module(isolated_lan)
_spec = importlib.util.spec_from_file_location('native_group_observation', HERE.with_name('native_lab_observation.py'))
observation = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(observation)
_evidence_spec = importlib.util.spec_from_file_location('native_group_failure_evidence', HERE.with_name('group_failure_evidence.py'))
failure_evidence = importlib.util.module_from_spec(_evidence_spec)
_evidence_spec.loader.exec_module(failure_evidence)
require = observation.require
NATIVE_LAB = observation.base.NATIVE_LAB
if NATIVE_LAB is not None:
    STATE, RUN, WORK = NATIVE_LAB.state, NATIVE_LAB.run, NATIVE_LAB.work
    BINARIES, IDENTITIES = NATIVE_LAB.binaries, NATIVE_LAB.identities
    RESULT = WORK/RESULT.name


def native_lab_admission(manifest, mode, *, original_netns_fd=None):
    require(NATIVE_LAB is not None, 'Explicit native lab profile is required for clean-VM admission')
    return NATIVE_LAB.admit(manifest, mode=mode, original_netns_fd=original_netns_fd, project=PROJECT)


class FrozenWorkerTiming(NamedTuple):
    """Actual pre-program worker plan; observations cannot revise its calendar."""
    horizon_ns: int
    buffers_ms: tuple[tuple[str, int], ...]

    def receipt(self):
        return {'common_horizon_ns': self.horizon_ns, 'output_buffers_ms': dict(self.buffers_ms),
                'scope': 'Actual enabled worker health agrees with saved zero-offset candidate plan before first PCM'}


@lru_cache(maxsize=1)
def load_minimum_coverage_module():
    spec = importlib.util.spec_from_file_location('native_group_minimum_coverage', HERE.with_name('native_minimum_coverage.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def candidate_worker_timing(definitions, healths):
    # Freeze the production route policy before any source or PCM exists.
    try:
        plan = latency_plan(definitions)
    except ValueError as exc:
        raise RuntimeFailure('Candidate worker plan has invalid saved definitions') from exc
    enabled = [room for room in definitions if room.enabled]
    require(len(enabled) == 2 and set(healths) == {room.id for room in enabled}
            and all(room.speakers and all(speaker.offset_ms == 0 for speaker in room.speakers)
                    for room in enabled), 'Candidate first baseline requires exactly two selected zero-offset rooms')
    require(plan.common_horizon_ms == 140 and all(room.output_buffer_ms == 40 for room in plan.rooms),
            'Candidate first baseline requires the production H140/B40 plan')
    for room in plan.rooms:
        health = healths[room.room_id]
        require(isinstance(health, dict) and {'ready', 'error', 'source'} <= health.keys()
                and health['ready'] is True and health['error'] is None
                and isinstance(health['source'], dict) and {'ready', 'owner'} <= health['source'].keys()
                and health['source']['ready'] is True
                and type(health.get('timing_relay_delay_ms')) is int
                and health['timing_relay_delay_ms'] == plan.common_horizon_ms
                and type(health.get('output_buffer_ms')) is int
                and health['output_buffer_ms'] == room.output_buffer_ms,
                'Actual worker timing differs from the saved candidate plan')
    return FrozenWorkerTiming(plan.common_horizon_ns,
                             tuple((room.room_id, room.output_buffer_ms) for room in plan.rooms))


def freeze_worker_timing(definitions, healths):
    result = candidate_worker_timing(definitions, healths)
    require(all(health['source'].get('owner') is None
                and type(health.get('native_blocks')) is int and health['native_blocks'] == 0
                for health in healths.values()), 'Worker timing must be frozen before any native PCM or owner')
    return result


def require_worker_timing(frozen, definitions, healths):
    require(type(frozen) is FrozenWorkerTiming and candidate_worker_timing(definitions, healths) == frozen,
            'Measured program changed its frozen worker plan')


def require_untouched_music_health(health, guard):
    """Late OwnTone gain is proved by the original per-buffer final PCM guard."""
    keys = {'ready', 'error', 'speech_mix', 'music_gain', 'music_target_gain', 'speech_session_id',
            'dropped_bytes', 'speech_dropped_frames', 'speech_output_error', 'speech_output_errno'}
    require(keys <= health.keys() and health['speech_mix'] == 'owntone_player'
            and health['ready'] is True and health['error'] is None and health['music_gain'] is None
            and type(health['music_target_gain']) in (int, float) and health['music_target_gain'] == 1
            and health['speech_session_id'] is None
            and type(health['dropped_bytes']) is int and type(health['speech_dropped_frames']) is int
            and health['dropped_bytes'] == health['speech_dropped_frames'] == 0
            and health['speech_output_error'] is None and health['speech_output_errno'] is None,
            'Untouched late OwnTone music has invalid gain, session, drops or speech output health')
    require(isinstance(guard, FinalPcmGuard) and guard.index is not None and guard.reference is not None,
            'Late OwnTone music requires its original armed final PCM reference')
    guard.check()
    require(guard.reference_blocks > 0, 'Late OwnTone music lacks actual per-buffer unchanged gain evidence')


class IsolatedBroker(Broker):
    """Place namespace-default test units beside the synthetic speech peer.

    The system manager does not inherit its caller's network namespace. This
    adapter changes only that explicit test plumbing; generated room/sender
    namespaces and all production launch, ownership and audio gates remain.
    """
    def __init__(self, settings, parent_namespace):
        require(isinstance(parent_namespace, str) and
                re.fullmatch(r'shiri_group_run_[0-9a-f]{8}', parent_namespace),
                'A tracked isolated parent namespace is required')
        parent = (Path('/run/netns')/parent_namespace).stat()
        current = Path('/proc/self/ns/net').stat()
        require((parent.st_dev, parent.st_ino) == (current.st_dev, current.st_ino),
                'Harness is outside its exact pinned parent network namespace')
        self.test_parent_namespace = parent_namespace
        super().__init__(settings)

    async def _start_process(self, *args, namespace=None, **kwargs):
        return await super()._start_process(*args, namespace=namespace or self.test_parent_namespace, **kwargs)


def envelope(frame):
    """Nonrepeating known binary amplitude code before a constant TTS probe."""
    if frame >= RATE * CODE_SECONDS:
        return 1.
    chip = frame // CODE_CHIP_FRAMES
    # A fixed bit mixer is deterministic across separate processes; no hash().
    value = (chip + 1) * 0x9E3779B1 & 0xFFFFFFFF
    value ^= value >> 16
    value = value * 0x85EBCA6B & 0xFFFFFFFF
    value ^= value >> 13
    return 1. if value & 1 else .65


def program_pcm(frame_index, frames=960, frequency=440):
    positions = np.arange(frame_index, frame_index + frames)
    gains = np.array([envelope(int(frame)) for frame in positions])
    mono = (8192 * gains * np.sin(2 * np.pi * frequency * positions / RATE)).astype('<i2')
    return np.repeat(mono[:, None], 2, axis=1).tobytes()


@lru_cache(maxsize=8)
def coded_880_bound(frames):
    """Exact worst projection of one declared amplitude edge, any phase.

    The baseline's 120 ms chips can cross a short capture buffer. Even a pure
    440 Hz carrier then projects onto the 880 Hz fit. Derive that bound from
    the fixed stimulus and analysis basis before examining captured values.
    Constant music during the speech test retains its stricter measured floor.
    """
    require(type(frames) is int and RATE//100 <= frames <= CODE_CHIP_FRAMES,
            'Coded baseline has an unsupported analysis window')
    t = np.arange(frames)/RATE
    carrier = np.column_stack((np.sin(2*np.pi*440*t), np.cos(2*np.pi*440*t)))
    basis = np.column_stack((carrier, np.sin(2*np.pi*880*t), np.cos(2*np.pi*880*t), np.ones(frames)))
    projection = np.linalg.pinv(basis)[2:4]
    tails = np.cumsum((projection.T[:, :, None]*carrier[:, None, :])[::-1], axis=0)[::-1]
    edge = 8192*.35*float(np.linalg.norm(tails, ord=2, axis=(1, 2)).max())
    quantization = float(np.hypot(*np.abs(projection).sum(axis=1)))
    return edge+quantization+1.


def coherent_clock_sample(*, wide=False, stats=None):
    """Retry a complete mapping sample; never widen the ingress1ms gate.

    Four attempts and5ms of total elapsed monotonic time are hard admission
    limits. OS preemption can exceed that wall time while a read is pending;
    the returned sample then fails explicitly instead of admitting stale data.
    Each stress attempt still surrounds RAW with75us on each side.
    """
    started, previous_after, previous_raw = None, None, None
    for attempt in range(CLOCK_SAMPLE_ATTEMPTS):
        before = time.monotonic_ns()
        if wide:
            until = before + 75_000
            while time.monotonic_ns() < until:
                pass
        raw = time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)
        if wide:
            until = time.monotonic_ns() + 75_000
            while time.monotonic_ns() < until:
                pass
        after = time.monotonic_ns()
        require(all(type(value) is int and value > 0 for value in (before, raw, after))
                and after >= before and (previous_after is None or before >= previous_after)
                and (previous_raw is None or raw >= previous_raw),
                'Synthetic native clock sampling returned invalid or backwards clocks')
        if started is None:
            started = before
        bracket = after-before
        if stats is not None:
            stats['clock_sample_attempts'] = stats.get('clock_sample_attempts', 0)+1
            stats['clock_sample_retries'] = stats.get('clock_sample_retries', 0)+(attempt > 0)
            stats['max_attempt_clock_bracket_ns'] = max(stats.get('max_attempt_clock_bracket_ns', 0), bracket)
        elapsed = after-started
        if stats is not None and (elapsed >= CLOCK_SAMPLE_BUDGET_NS
                                  or attempt+1 == CLOCK_SAMPLE_ATTEMPTS and bracket > CLOCK_BRACKET_LIMIT_NS):
            # Evidence precedes the existing refusal; never send this sample.
            stats['clock_sample_failure'] = {'kind': 'elapsed_budget' if elapsed >= CLOCK_SAMPLE_BUDGET_NS else 'attempts',
                'attempt': attempt+1, 'before_ns': before, 'raw_ns': raw, 'after_ns': after,
                'started_ns': started, 'bracket_ns': bracket, 'elapsed_ns': elapsed,
                'budget_ns': CLOCK_SAMPLE_BUDGET_NS, 'bracket_limit_ns': CLOCK_BRACKET_LIMIT_NS}
        require(elapsed < CLOCK_SAMPLE_BUDGET_NS,
                'Synthetic native clock sampling exhausted its5ms elapsed budget')
        if bracket <= CLOCK_BRACKET_LIMIT_NS:
            return before, raw, after
        previous_after, previous_raw = after, raw
    raise RuntimeFailure('Synthetic native clock sampling exhausted its4 coherent-sample attempts')


class ClockSamplingTimeout(RuntimeFailure):
    """A complete original sampler batch refused its finite time/attempt budget."""


class RecoveryClock:
    """Per-packet ordered reads, including every original stress-spin read."""
    def __init__(self, source):
        self.source = source
        self.last_monotonic = 0
        self.last_raw = 0

    def __getattr__(self, name):
        return getattr(self.source, name)

    def monotonic_ns(self):
        value = self.source.monotonic_ns()
        require(type(value) is int and value > 0 and value >= self.last_monotonic,
                'Synthetic native clock sampling returned invalid or backwards clocks')
        self.last_monotonic = value
        return value

    def clock_gettime_ns(self, clock):
        value = self.source.clock_gettime_ns(clock)
        require(clock == self.source.CLOCK_MONOTONIC_RAW and type(value) is int
                and value > 0 and value >= self.last_raw,
                'Synthetic native clock sampling returned invalid or backwards clocks')
        self.last_raw = value
        return value


def recovered_clock_sample(*, wide=False, stats=None):
    """Fresh timeout-only batches under the timed3 original-entry20ms cap."""
    evidence = stats if stats is not None else {}
    evidence.pop('clock_recovery_failure', None)
    trace = RecoveryClock(time)
    def typed_require(value, message):
        if not value and message == 'Synthetic native clock sampling exhausted its5ms elapsed budget':
            raise ClockSamplingTimeout(message)
        require(value, message)
    namespace = dict(coherent_clock_sample.__globals__, time=trace, require=typed_require,
                     RuntimeFailure=ClockSamplingTimeout)
    inner = FunctionType(coherent_clock_sample.__code__, namespace, coherent_clock_sample.__name__,
                         coherent_clock_sample.__defaults__, coherent_clock_sample.__closure__)
    inner.__kwdefaults__ = coherent_clock_sample.__kwdefaults__
    started = trace.monotonic_ns()
    row = {'started_ns': started, 'checked_ns': started, 'elapsed_ns': 0,
           'batches': 0, 'refused': 0, 'passed': False}
    evidence['clock_recovery'] = row
    def deadline():
        checked = trace.monotonic_ns()
        row.update(checked_ns=checked, elapsed_ns=checked-started)
        if row['elapsed_ns'] >= CLOCK_RECOVERY_BUDGET_NS:
            raise ClockSamplingTimeout('Synthetic native clock recovery exhausted its20ms elapsed budget')
        return checked
    try:
        for _batch in range(CLOCK_RECOVERY_BATCHES):
            entered = deadline()
            row['batches'] += 1
            evidence['clock_recovery_total_batches'] = evidence.get('clock_recovery_total_batches', 0)+1
            try:
                sampled = inner(wide=wide, stats=evidence)
            except ClockSamplingTimeout:
                row['refused'] += 1
                evidence['clock_recovery_refused_batches'] = evidence.get('clock_recovery_refused_batches', 0)+1
                rejected = evidence.pop('clock_sample_failure', None)
                if rejected is not None:
                    evidence['last_clock_recovery_rejection'] = rejected
                deadline()
                continue
            checked = deadline()
            require(sampled[0] >= entered and sampled[2] <= checked,
                    'Synthetic native clock sampling returned invalid or backwards clocks')
            row['passed'] = True
            evidence.pop('clock_sample_failure', None)
            if row['refused']:
                evidence['clock_recovery_recovered_packets'] = evidence.get('clock_recovery_recovered_packets', 0)+1
            return sampled
        raise ClockSamplingTimeout('Synthetic native clock recovery exhausted its4 fresh batches')
    except BaseException as error:
        evidence['clock_recovery_failure'] = {'type': type(error).__name__, 'message': str(error)[:2000], **row}
        raise


def check_clock_recovery_deadline(*, stats):
    """Packet encoding cannot refresh the original timed3 sampling entry."""
    row = stats['clock_recovery']
    now = time.monotonic_ns()
    require(row['passed'] is True and type(now) is int and now > 0 and now >= row['checked_ns'],
            'Synthetic native clock sampling returned invalid or backwards clocks')
    row.update(checked_ns=now, elapsed_ns=now-row['started_ns'])
    if row['elapsed_ns'] >= CLOCK_RECOVERY_BUDGET_NS:
        row['passed'] = False
        raise ClockSamplingTimeout('Synthetic native clock recovery exhausted its20ms elapsed budget')


async def send_native_packet(loop, connection, payload, target_ns, *, stats):
    """One owned PCM send retains its original target+150ms absolute deadline."""
    absolute = target_ns+SEND_LATE_NS
    checked = time.monotonic_ns()
    require(type(checked) is int and checked >= target_ns and checked < absolute,
            'Synthetic producer missed bounded native delivery cadence')
    admitted = time.monotonic_ns()
    require(admitted >= checked and admitted < absolute,
            'Synthetic producer missed bounded native delivery cadence')
    async def transmit():
        entered = time.monotonic_ns()
        require(entered >= admitted and entered < absolute,
                'Synthetic producer missed bounded native delivery cadence')
        check_clock_recovery_deadline(stats=stats)
        sending = time.monotonic_ns()
        require(sending >= stats['clock_recovery']['checked_ns'] >= entered and sending < absolute,
                'Synthetic producer missed bounded native delivery cadence')
        await loop.sock_sendall(connection, payload)
        return sending
    task = asyncio.create_task(transmit(), name='native-owned-pcm-send')
    primary, cancellation, completed = None, None, None
    try:
        done, _pending = await asyncio.wait({task}, timeout=(absolute-admitted)/1e9)
        finished = time.monotonic_ns()
        require(bool(done), 'Synthetic producer missed bounded native delivery cadence')
        sending = task.result()
        require(finished >= sending and finished < absolute,
                'Synthetic producer missed bounded native delivery cadence')
        completed = finished
    except BaseException as error:
        primary = error
    finally:
        if not task.done():
            task.cancel()
        async def consume():
            return await asyncio.gather(task, return_exceptions=True)
        join = asyncio.create_task(consume(), name='native-owned-pcm-send-join')
        while not join.done():
            try:
                await asyncio.shield(join)
            except asyncio.CancelledError as error:
                cancellation = cancellation or error
        join.result()
    if cancellation is not None and not isinstance(primary, asyncio.CancelledError):
        if primary is not None:
            stats['native_send_failure_before_cancellation'] = {
                'type': type(primary).__name__, 'message': str(primary)[:512]}
            raise cancellation from primary
        raise cancellation
    if primary is not None:
        raise primary
    return completed-target_ns


def retire_producer(connections, path, state, primary, *, publish=None):
    """Close each exact owned socket and retain the first producer failure."""
    errors = []
    for connection in connections:
        try:
            connection.close()
        except BaseException as error:
            errors.append(error)
    state['finished'] = True
    if errors:
        state['producer_cleanup_errors'] = [type(error).__name__ for error in errors]
    try:
        (publish or atomic_json)(path, state)
    except BaseException as error:
        errors.append(error)
    if errors:
        if primary is not None:
            raise primary from errors[0]
        raise errors[0]


def bracketed_packet(grant, group, frame, sequence, common_start_ns, wide=False, frequency=440, *, stats=None):
    before, raw, after = recovered_clock_sample(wide=wide, stats=stats)
    midpoint = before + (after - before)//2
    presentation = common_start_ns + frame * 1_000_000_000 // RATE
    return Packet(Kind.PCM, grant.session, incarnation=grant.incarnation, group=group,
                  epoch=grant.epoch, generation=grant.generation, sequence=sequence,
                  frame_index=frame, frames=960, pcm=program_pcm(frame, frequency=frequency), flags=grant.flags,
                  clock=Clock.RAW, presentation_ns=raw + presentation - midpoint,
                  clock_sample_ns=raw, monotonic_before_ns=before, monotonic_after_ns=after)


async def hold_bluetooth_receiver_after_end(config, connection, path, state, stop):
    """Hold only the declared BT fixture receiver after acknowledged source END."""
    profile = config.get('bluetooth_receiver_idle')
    if profile is None:
        return
    require(isinstance(profile, dict) and set(profile) == {
        'version', 'room_id', 'started_monotonic_ns', 'deadline_monotonic_ns'
    } and type(profile['version']) is int and profile['version'] == 1
            and profile['room_id'] == A
            and type(config.get('duration_seconds')) is int and config['duration_seconds'] == 180
            and config.get('leader') is True
            and not any(key in config for key in ('music_startup', 'music_minimum', 'music_soak', 'candidate_timing')),
            'Idle receiver lifetime is restricted to the declared Bluetooth A fixture')
    started, deadline = profile['started_monotonic_ns'], profile['deadline_monotonic_ns']
    now = time.monotonic_ns()
    require(type(started) is int and type(deadline) is int
            and 0 < started <= now < deadline and deadline-started == 180_000_000_000,
            'Bluetooth fixture receiver exceeded or altered its original180s lifetime')
    require(not state.get('error') and state.get('commands')
            and state['commands'][-1] == {'generation': 4, 'action': 'end'}
            and state.get('stage') == 'streaming' and state.get('finished') is False
            and type(state.get('frames')) is int and state['frames'] > 0,
            'Idle receiver hold requires the exact successful final native END')
    connection.close()
    state.update(stage='ended_idle', native_end_idle={
        'generation': 4, 'source_retired': True, 'descriptor_closed': True,
        'receiver_unit_held': True, 'observed_monotonic_ns': now,
        'deadline_monotonic_ns': deadline,
    })
    atomic_json(path, state)
    try:
        await asyncio.wait_for(stop.wait(), (deadline-time.monotonic_ns())/1e9)
    except asyncio.TimeoutError as exc:
        raise RuntimeFailure('Bluetooth fixture idle receiver reached its declared lifetime') from exc


async def producer(config_path):
    """Receiver credentials are set by the owned unit, never by a root socket."""
    config = json.loads(Path(config_path).read_text())
    if 'music_minimum' in config:
        return await load_minimum_music_module().producer(config, globals())
    if 'music_soak' in config:
        return await load_soak_module().producer(config, globals())
    duration = config.get('duration_seconds', MAX_DURATION)
    require(type(duration) is int and duration in {MAX_DURATION, 180, 420, 480},
            'Producer duration must be the explicit90s baseline,180s fault,420s stress or480s latency bound')
    require(os.geteuid() == config['uid'] != 0 and os.getegid() == config['gid'], 'Producer credential mismatch')
    expected_lead = 220_000_000 if config.get('leader') is True else 80_000_000
    require(type(config.get('leader')) is bool and type(config.get('arrival_lead_ns')) is int
            and config['arrival_lead_ns'] == expected_lead,
            'Producer arrival lead must be its declared route timing')
    path = Path(config['status'])
    command = Path(config['command'])
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(name, stop.set)
    connections = []
    session = uuid4().bytes
    group = UUID(config['group']).bytes
    flags = FLAGS | (FLAG_GROUP_LEADER if config['leader'] else 0)
    state = {'uid': os.geteuid(), 'gid': os.getegid(), 'pid': os.getpid(), 'stage': 'connecting',
             'frames': 0, 'session_id': str(UUID(bytes=session)), 'max_send_lateness_ns': 0,
             'max_clock_bracket_ns': 0, 'clock_sample_attempts': 0, 'clock_sample_retries': 0,
             'max_attempt_clock_bracket_ns': 0, 'commands': [], 'finished': False}
    async def connect(identity):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        connection.setblocking(False)
        connections.append(connection)
        await asyncio.wait_for(loop.sock_connect(connection, config['socket']), 3)
        begin = Packet(Kind.BEGIN, identity, group=group, flags=flags)
        await asyncio.wait_for(loop.sock_sendall(connection, begin.encode()), .3)
        reply = Packet.decode(await asyncio.wait_for(loop.sock_recv(connection, 4096), 5))
        require(reply.kind is Kind.GRANT and reply.session == identity and reply.group == group,
                'Native ingress did not return the exact producer grant')
        require(reply.epoch > 0 and reply.incarnation != bytes(16), 'Grant omitted source ownership identity')
        return connection, reply
    connection = None
    primary = None
    try:
        connection, grant = await connect(session)
        state.update(stage='granted', epoch=grant.epoch, incarnation=str(UUID(bytes=grant.incarnation)), generation=grant.generation)
        atomic_json(path, state)
        start = config['common_start_ns']
        frame, sequence, seen, next_status, frequency = 0, 0, 0, 0, 440
        while not stop.is_set() and frame < RATE * duration:
            now = time.monotonic_ns()
            action = json.loads(command.read_text())
            if action['generation'] > seen:
                seen = action['generation']
                if action['action'] == 'takeover':
                    old_connection, old_grant = connection, grant
                    connection, grant = await connect(uuid4().bytes)
                    stale = replace(old_grant, kind=Kind.VOLUME, frames=77, pcm=b'')
                    try:
                        await asyncio.wait_for(loop.sock_sendall(old_connection, stale.encode()), .3)
                        stale_result = 'send_completed; successor health must independently remain unchanged'
                    except (OSError, asyncio.TimeoutError):
                        stale_result = 'retired_socket_rejected'
                    old_connection.close()
                    state.update(session_id=grant.session_id, epoch=grant.epoch,
                                 incarnation=str(UUID(bytes=grant.incarnation)), generation=grant.generation)
                    frequency = 660
                    state['commands'].append({'generation': seen, 'action': 'takeover', 'stale_volume': stale_result})
                elif action['action'] == 'end':
                    await asyncio.wait_for(loop.sock_sendall(connection, replace(grant, kind=Kind.END).encode()), .3)
                    require(await asyncio.wait_for(loop.sock_recv(connection, 4096), 3) == b'',
                            'Native END did not close its exact retired ingress')
                    state['commands'].append({'generation': seen, 'action': 'end'})
                    await hold_bluetooth_receiver_after_end(config, connection, path, state, stop)
                    break
                else:
                    require(action['action'] in {'run', 'wait'}, 'Unsupported producer command')
                    if action['action'] == 'run':
                        start = action['common_start_ns']
                        require(type(start) is int and start > 0, 'Producer needs one explicit shared calendar origin')
                atomic_json(path, state)
            if not start:
                try:
                    await asyncio.wait_for(stop.wait(), .03)
                except asyncio.TimeoutError:
                    pass
                continue
            target = start + frame * 1_000_000_000 // RATE - config['arrival_lead_ns']
            if now < target:
                try:
                    await asyncio.wait_for(stop.wait(), (target-now)/1e9)
                except asyncio.TimeoutError:
                    pass
                continue
            lateness = time.monotonic_ns()-target
            require(lateness < 150_000_000, 'Synthetic producer missed bounded native delivery cadence')
            packet = bracketed_packet(grant, group, frame, sequence, start, config['wide_bracket'], frequency, stats=state)
            lateness = await send_native_packet(loop, connection, packet.encode(), target, stats=state)
            state['max_send_lateness_ns'] = max(state['max_send_lateness_ns'], lateness)
            state['max_clock_bracket_ns'] = max(state['max_clock_bracket_ns'], packet.monotonic_after_ns-packet.monotonic_before_ns)
            frame += packet.frames
            sequence += 1
            state.update(stage='streaming', frames=frame, presentation_ns=packet.presentation_ns)
            if now >= next_status:
                atomic_json(path, state)
                next_status = now + 250_000_000
        state.update(stage='finished', finished=True)
    except BaseException as exc:
        primary = exc
        state['error'] = observation.redact_exception(exc)
        raise
    finally:
        retire_producer(connections, path, state, primary)


class Api(observation.Api):
    def __init__(self):
        self.client = httpx.AsyncClient(base_url=API_BASE, trust_env=False, timeout=httpx.Timeout(25, connect=2),
                                        headers={'Origin': API_BASE}, limits=httpx.Limits(max_connections=4))
    async def room(self, identifier):
        view = await self.request('GET', '/api/v1/state')
        require(not view['runtime']['simulation'], 'Candidate API unexpectedly uses simulation')
        found = [room for room in view['rooms'] if room['id'] == identifier]
        require(len(found) == 1, 'Exact disposable room is absent')
        return found[0]
    async def patch(self, identifier, changes):
        for _ in range(5):
            room = await self.room(identifier)
            response = await self.client.patch(f'/api/v1/rooms/{identifier}',
                json={'expected_revision': room['revision'], 'changes': changes})
            if response.status_code == 409:
                await asyncio.sleep(.1)
                continue
            require(response.status_code == 200, f'Room patch returned HTTP{response.status_code}')
            reply = response.json()
            require(reply.get('runtime_accepted') is True, 'Room intent was not accepted by actual broker')
            return reply['room']
        raise RuntimeFailure('Room revision did not settle')


def seed(database, interface='enp0s1'):
    with Store(database) as store:
        with store._transaction() as connection:
            for slot in range(6):
                room = store._insert_room(connection, RoomCreate(name=f'Native probe disabled {slot}', interface=interface))
                require(room.slot == slot and not room.enabled, 'Placeholder seed did not remain disabled')
            for identifier, zone in ZONES.items():
                room = store._insert_room(connection, RoomCreate(name=zone['name'], airplay_name=zone['name'],
                    nobly_room_id=zone['nobly'], interface=interface), room_id=identifier)
                require(room.slot == zone['slot'] and not room.enabled, 'Two-zone seed slot mismatch')


async def launch_api(path, settings, account, group, *, interface='enp0s1'):
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 18084))
    state = path/'api'
    require(settings.state_dir == state and settings.api_token_file == path/'token',
            'Broker and API must protect the same disposable state and credential paths')
    state.mkdir(mode=0o700)
    seed(state/'shiri.sqlite3', interface)
    for item in state.iterdir():
        require(stat.S_ISREG(item.lstat().st_mode), 'Unexpected API seed file')
        item.chmod(0o600)
        os.chown(item, account.pw_uid, group.gr_gid)
    os.chown(state, account.pw_uid, group.gr_gid)
    token = secrets.token_urlsafe(48)
    token_path = path/'token'
    descriptor = os.open(token_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o640)
    os.fchmod(descriptor, 0o640)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write(token+'\n')
    os.chown(token_path, 0, group.gr_gid)
    env = {key: value for key, value in os.environ.items() if not key.startswith('SHIRI_')}
    env.update(SHIRI_STATE_DIR=str(state), SHIRI_RUNTIME_STATE_DIR=str(STATE), SHIRI_RUNTIME_DIR=str(RUN),
        SHIRI_RUNTIME_SOCKET=str(settings.runtime_socket), SHIRI_API_TOKEN_FILE=str(token_path),
        SHIRI_DAEMON_IDENTITY_FILE=str(IDENTITIES), SHIRI_SIMULATION='0', SHIRI_HOST='127.0.0.1',
        SHIRI_PORT='18084', SHIRI_BINARY_DIR=str(BINARIES))
    log_path = path/'api.log'
    log = log_path.open('xb')
    log_path.chmod(0o600)
    process = None
    try:
        process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'shiri.cli', '--log-level', 'WARNING', 'serve',
            cwd=PROJECT, env=env, user=account.pw_uid, group=group.gr_gid, extra_groups=[], start_new_session=True,
            stdout=log, stderr=asyncio.subprocess.STDOUT)
        owned = observation.ApiProcess(process, account.pw_uid, group.gr_gid, log)
        return owned, token
    except BaseException:
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                process.kill()
                await asyncio.wait_for(process.wait(), 3)
        log.close()
        raise


class Capture(observation.OutputCapture):
    """Same proven continuity observer, with common absolute Gst clock anchors."""
    def __init__(self, device, shared_clock, base_time_ns, clock_offset_ns):
        super().__init__(device, start=False)
        # This fixed native fixture can start observing before OwnTone opens
        # playback. Pin its declared 48k contract so an otherwise unconstrained
        # capture cannot lock the shared Loopback pair at ALSA's 44.1k default.
        # The general output observer still follows real hardware negotiation.
        self.pipeline.get_by_name('stereo-s16').set_property('caps', self.Gst.Caps.from_string(
            'audio/x-raw,format=S16LE,channels=2,rate=48000,layout=interleaved'))
        self.device = device
        self.absolute = {}
        self.buffer_metadata = {}
        self.clock_offset_ns = clock_offset_ns
        self.pipeline.use_clock(shared_clock)
        self.pipeline.set_start_time(self.Gst.CLOCK_TIME_NONE)
        self.pipeline.set_base_time(base_time_ns)
        self.expected_base = base_time_ns
    def _sample(self, sink):
        sample = sink.emit('pull-sample')
        if sample is None:
            return self.Gst.FlowReturn.EOS
        buffer = sample.get_buffer()
        info = self.GstAudio.AudioInfo.new_from_caps(sample.get_caps())
        if info is None or info.bpf != 4 or info.channels != 2 or info.rate != RATE:
            self.error = 'Final grouping capture negotiated unsupported framing: ' + sample.get_caps().to_string()
            return self.Gst.FlowReturn.ERROR
        at = time.monotonic()
        if self.last_packet_at is not None:
            self.max_packet_gap = max(self.max_packet_gap, at-self.last_packet_at)
        self.last_packet_at = at
        if buffer.has_flags(self.Gst.BufferFlags.DISCONT):
            self.discontinuities += 1
        if len(self.pending) >= 128:
            self.capture_dropped += buffer.get_size()
            return self.Gst.FlowReturn.OK
        def valid(value):
            value = int(value)
            return value if 0 <= value < (1 << 64)-1 else None
        metadata = {key: valid(getattr(buffer, key)) for key in ('offset', 'offset_end', 'pts', 'duration')}
        metadata['discont'] = buffer.has_flags(self.Gst.BufferFlags.DISCONT)
        base_time = self.pipeline.get_base_time()
        if base_time != self.expected_base or metadata['pts'] is None:
            self.error = 'Final grouping capture changed common base time or omitted PTS'
            return self.Gst.FlowReturn.ERROR
        self.absolute[at] = base_time + metadata['pts']-self.clock_offset_ns
        self.buffer_metadata[at] = dict(metadata)
        self.pending.append((at, buffer.extract_dup(0, buffer.get_size()), info.rate, info.channels,
                             sample.get_caps().get_structure(0).get_value('format'), metadata))
        return self.Gst.FlowReturn.OK
    def poll(self):
        result = super().poll()
        result['capture'] = self.device
        result['common_clock_base_ns'] = self.expected_base
        result['clock_offset_to_monotonic_ns'] = self.clock_offset_ns
        return result


def modulation(capture, start_ns, end_ns):
    continuity = getattr(capture, 'frame_continuity', None)
    if continuity is not None:
        require(continuity['verified_blocks'] == len(capture.chunks)
                and continuity['verified_frames'] == sum(len(data)//4 for data in capture.chunks),
                'Frozen group PCM differs from its actual verified frame continuity receipt')
    observed = [(capture.absolute[at], data)
                for at, data in zip(capture.captured_at, capture.chunks, strict=True)]
    previous_ns, previous_frames = None, None
    # Validate ALL recorded anchors before selection. Otherwise a bad interior
    # timestamp displaced outside the requested interval silently removes PCM
    # from the analysis despite unchanged whole-snapshot continuity counters.
    for anchor_ns, data in observed:
        require(type(anchor_ns) is int and anchor_ns > 0 and len(data) % 4 == 0,
                'Group capture has an invalid absolute timestamp or stereo framing')
        frames = len(data)//4
        require(0 < frames <= 960, 'Group capture exceeds its configured20ms period')
        if previous_ns is not None:
            step = anchor_ns-previous_ns
            # The capture is configured for20ms periods. Its query noise may
            # vary within a period; backwards/unbounded anchors cannot be
            # relabelled as timing noise. Timing noise alone cannot establish
            # sample continuity: PcmSequence checks actual sample offsets and
            # its copied receipt must match every frozen PCM frame/block.
            require(step > 0 and abs(step-previous_frames*1_000_000_000/RATE) < 20_000_000,
                    'Group capture timestamps have a backwards anchor or unbounded span/gap')
        previous_ns, previous_frames = anchor_ns, frames
    chosen = [(anchor, data) for anchor, data in observed if start_ns <= anchor < end_ns]
    require(len(chosen) >= 3, 'Insufficient actual group PCM for independent alignment')
    first_ns = chosen[0][0]
    # alsasrc's driver-derived PTS is measured again for every buffer, even
    # with one shared Gst clock. Extrapolating an entire interval from its
    # first buffer turns that one query's phase noise into a constant offset.
    # Assign every sample its actual buffer anchor plus within-buffer position;
    # an analysis window's time is the mean of ALL of those sample times.
    # Work with residuals around the frame counter to avoid summing large
    # absolute nanosecond timestamps and to preserve the measured clock origin.
    residuals, first_frame = [], 0
    for anchor_ns, data in chosen:
        frames = len(data)//4
        residuals.append(np.full(frames, anchor_ns-first_ns-first_frame*1_000_000_000/RATE))
        first_frame += frames
    mono = np.frombuffer(b''.join(data for _, data in chosen), dtype='<i2').reshape(-1, 2)[:, 0].astype(float)
    require(len(mono) >= RATE*2, 'Group timing window is shorter than2s')
    window, hop = 960, 48
    t = np.arange(window)/RATE
    basis = np.column_stack((np.sin(2*np.pi*440*t), np.cos(2*np.pi*440*t),
                             np.sin(2*np.pi*880*t), np.cos(2*np.pi*880*t), np.ones(window)))
    fits = np.lib.stride_tricks.sliding_window_view(mono, window)[::hop] @ np.linalg.pinv(basis).T
    amplitude = np.hypot(fits[:, 0], fits[:, 1])
    starts = np.arange(len(amplitude))*hop
    prefix = np.concatenate(([0.], np.cumsum(np.concatenate(residuals))))
    mean_residual_ns = (prefix[starts+window]-prefix[starts])/window
    times = first_ns/1e9 + (starts+(window-1)/2)/RATE + mean_residual_ns/1e9
    require(bool(np.isfinite(times).all()) and bool((np.diff(times) > 0).all()),
            'Group analysis window times are not a finite monotonic measured timeline')
    require(float(amplitude.mean()) > 8 and float(amplitude.std()/amplitude.mean()) > .035,
            'Constant/silent output cannot prove a shared coded program')
    return times, amplitude


def capture_snapshot(capture):
    # Freeze aligned lists in the observer's event-loop thread before analysis.
    # Worker threads never drain a shared capture queue or mutate its fences.
    capture.poll()
    continuity = deepcopy(capture.sequence.evidence()) if hasattr(capture, 'sequence') else None
    if continuity is not None:
        require(continuity['verified_blocks'] == len(capture.chunks)
                and continuity['verified_frames'] == sum(len(data)//4 for data in capture.chunks),
                'Group capture differs from its actual verified frame continuity receipt')
    return SimpleNamespace(chunks=tuple(capture.chunks), captured_at=tuple(capture.captured_at),
                           absolute=dict(capture.absolute), frame_continuity=continuity)


def declared_capture(origin_ns, first_frame, last_frame):
    reference = SimpleNamespace(chunks=[], captured_at=[], absolute={})
    for frame in range(first_frame, last_frame, 960):
        at = frame/RATE
        reference.chunks.append(program_pcm(frame))
        reference.captured_at.append(at)
        reference.absolute[at] = origin_ns+frame*1_000_000_000//RATE
    return reference


def kernel_reference(record):
    """Predeclared quality limits and uncertainty budget for a separate fixture."""
    require(record.get('finished') is True and record.get('closed') is True and not record.get('error'),
            'Independent kernel-digital fixture did not finish and close successfully')
    require((record.get('rate'), record.get('channels'), record.get('format')) == (RATE, 2, 'S16LE')
            and record.get('submitted_frames') == kernel_probe.DURATION*RATE,
            'Independent reference has wrong format or incomplete program delivery')
    period = record.get('period_frames')
    buffer = record.get('buffer_frames')
    require(type(period) is int and 0 < period <= 1920 and type(buffer) is int and period < buffer <= RATE,
            'Independent reference has no bounded negotiated period/buffer')
    samples = record.get('anchors')
    require(isinstance(samples, list) and 100 <= len(samples) <= 2000, 'Independent reference lacks bounded kernel observations')
    origins, triggers, brackets = [], [], []
    previous_time, previous_frames = 0, 0
    for sample in samples:
        require(isinstance(sample, dict) and set(sample) == {'before_ns', 'after_ns', 'timestamp_ns',
                'trigger_ns', 'delay_frames', 'submitted_frames', 'origin_ns'}
                and all(type(value) is int for value in sample.values()), 'Malformed kernel queue observation')
        before, after, frames = sample['before_ns'], sample['after_ns'], sample['submitted_frames']
        require(before >= previous_time and after >= before and after-before <= 1_000_000
                and frames > previous_frames and frames <= kernel_probe.DURATION*RATE,
                'Kernel reference query/frame ordering or bracket is invalid')
        expected = kernel_probe.queue_origin(sample['timestamp_ns'], frames, sample['delay_frames'], RATE)
        require(sample['delay_frames'] <= buffer and abs(sample['timestamp_ns']-before) <=
                (after-before)+period*1_000_000_000/RATE, 'Kernel queue timestamp or depth is not a fresh bounded observation')
        require(expected == sample['origin_ns'], 'Cached queue origin differs from its kernel evidence')
        previous_time, previous_frames = after, frames
        if RATE//2 <= frames <= (kernel_probe.DURATION-.5)*RATE:
            origins.append(expected)
            triggers.append(sample['trigger_ns'])
            brackets.append(after-before)
    require(len(origins) >= 100 and len(set(triggers)) == 1 and triggers[0] > 0,
            'Kernel fixture lacks one continuous independently anchored playback interval')
    period_ns = period*1_000_000_000/RATE
    spread = max(origins)-min(origins)
    bracket = max(brackets)
    # No timing failure can manufacture an arbitrarily generous acceptance
    # budget: queue-origin agreement must stay within two measured periods.
    require(spread <= 2*period_ns+2*bracket+1_000_000_000/RATE,
            'Independent queue anchors have excessive dispersion')
    origin = round(statistics.median(origins))
    require(abs(origin-triggers[0]) <= 2*period_ns+bracket,
            'Queue-derived origin disagrees with the actual kernel playback trigger')
    return {'kernel_origin_monotonic_ns': origin, 'kernel_trigger_monotonic_ns': triggers[0],
            'accepted_anchors': len(origins), 'queue_origin_spread_ms': spread/1e6,
            'negotiated_period_frames': period, 'period_uncertainty_ms': period_ns/1e6,
            'max_query_bracket_ms': bracket/1e6, 'analysis_grid_allowance_ms': 2,
            'horizon_half_width_ms': (spread+period_ns+bracket)/1e6+2}


def validate_horizon(displacement, baseline):
    corrected = displacement['relative_offset_ms']-baseline['capture_offset_ms']
    result = {'raw_displacement_ms': displacement['relative_offset_ms'],
              'independent_capture_offset_ms': baseline['capture_offset_ms'],
              'corrected_horizon_error_ms': corrected,
              'predeclared_half_width_ms': baseline['horizon_half_width_ms']}
    require(abs(corrected) <= baseline['horizon_half_width_ms'],
            'Actual final PCM missed its declared horizon after independent capture correction')
    return result


def publish_command(path, value, account):
    """Publish each replacement with its reader permissions already applied."""
    descriptor, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            os.fchown(stream.fileno(), 0, account['gid'])
            os.fchmod(stream.fileno(), 0o640)
            json.dump(value, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


async def calibrate_capture(broker, identifier, binding, root, clock, base_time, clock_offset):
    """Use a disposable canonical output unit before any room backend starts."""
    from shiri.runtime.alsa_configuration import render_pcm_config
    pin = broker.local_devices.resolve(binding)
    account = broker._account(identifier, 'output', ZONES[identifier]['slot'])
    # The fixture is an independently tracked disposable output, not a process
    # of the disabled API room. Normal reconciliation must remain free to stop
    # any processes belonging to a disabled room throughout this measurement.
    key = f'{uuid4()}:owntone'
    unit = capture = None
    result = None
    cleanup_errors = []
    try:
        require(key not in broker.network.manifest['processes'] and pin.fingerprint['binding'] == 'loopback'
                and pin.manifest['device'] == ZONES[identifier]['device'] and pin.manifest['subdevice'] == 7,
                'Independent fixture requires the exact free virtual output reservation')
        pin.validate()
        config_dir = directory(root/'config', {'uid': 0, 'gid': account['gid']}, mode=0o2750)
        status_dir = directory(root/'status', account)
        alsa, config, command = config_dir/'alsa.conf', config_dir/'fixture.json', config_dir/'command.json'
        alsa.write_text(render_pcm_config(pin, conversion=False), encoding='utf-8')
        file_owner(alsa, account)
        publish_command(command, {'action': 'wait'}, account)
        atomic_json(config, {'uid': account['uid'], 'gid': account['gid'], 'manifest': pin.manifest,
            'command': str(VIEW/'probe-config/command.json'), 'status': str(VIEW/'validation/status.json')})
        file_owner(config, account)
        unit = await broker._start_process(key, 'owntone', [str(Path('/usr/bin/python3').resolve(strict=True)),
            str(HERE.with_name('loopback_capture_probe.py')), str(VIEW/'probe-config/fixture.json')], root,
            account=account, devices=[pin.playback_node, f"/dev/snd/controlC{pin.manifest['card_index']}"],
            listen_port=3869+ZONES[identifier]['slot']*10,
            extra_environment=[f'ALSA_CONFIG_PATH={VIEW}/config/alsa.conf'],
            binds=[Bind(str(config_dir), str(VIEW/'probe-config')), Bind(str(alsa), str(VIEW/'config/alsa.conf')),
                   Bind(str(status_dir), str(VIEW/'validation'), True)])
        await broker._remember_process(key, unit)
        def status():
            try:
                value = json.loads((status_dir/'status.json').read_text())
            except FileNotFoundError:
                return None
            require(not value.get('error'), value.get('error') or 'Independent kernel fixture failed')
            require(value['uid'] == account['uid'] and value['gid'] == account['gid'],
                    'Independent fixture lost exact output credentials')
            return value
        async def ready():
            value = status()
            return value if value and value['stage'] == 'ready' else None
        # This stage has no PCM observer yet, so runtime startup retries carry
        # no audio-quality finding. A fixture-reported error is still fatal.
        deadline = time.monotonic()+10
        while not await ready():
            require(time.monotonic() < deadline and unit.alive, 'Independent filtered fixture never became ready')
            await asyncio.sleep(.03)
        pin.validate()
        capture = Capture(f"hw:{pin.manifest['card_index']},{1-pin.manifest['device']},7", clock, base_time, clock_offset)
        capture.start()
        guard = FinalPcmGuard(capture)
        onset = False
        publish_command(command, {'action': 'run'}, account)
        deadline = time.monotonic()+12
        while True:
            guard.check()
            if not onset and len(capture.chunks) >= 5:
                measured = observation.spectrum(capture.chunks[-5:], RATE, minimum_seconds=.05)
                if measured['music_440_amplitude'] > 1000 and measured['median_440_band_energy_fraction'] > .65:
                    onset = True
            value = status()
            if value and value.get('finished') is True and value.get('closed') is True:
                break
            require(time.monotonic() < deadline, 'Independent final PCM calibration did not finish within its bound')
            await asyncio.sleep(.02)
        guard.check()
        require(onset, 'Independent calibration never produced actual coded440 PCM')
        reference = kernel_reference(value)
        origin = reference['kernel_origin_monotonic_ns']
        first, last = origin+500_000_000, origin+5_500_000_000
        frozen = capture_snapshot(capture)
        expected = declared_capture(origin, RATE//2, RATE*11//2)
        expected_series, actual_series = await asyncio.gather(
            asyncio.to_thread(modulation, expected, first, last), asyncio.to_thread(modulation, frozen, first, last))
        aligned = await asyncio.to_thread(align_series, expected_series, actual_series, maximum_delay_ms=None, search_ms=1000)
        observed_first = first+round(aligned['relative_offset_ms']*1e6)
        observed_last = last+round(aligned['relative_offset_ms']*1e6)
        analyzed_blocks = 0
        # A finite fixture legitimately starts/ends in silence. Once its code
        # has identified the recorded measurement interval, every complete
        # buffer inside that interval must independently retain the stimulus.
        for at, data in zip(frozen.captured_at, frozen.chunks, strict=True):
            absolute = frozen.absolute[at]
            if observed_first <= absolute and absolute+len(data)//4*1_000_000_000//RATE <= observed_last:
                measured = observation.music_block(data, RATE, 1000)
                require(measured['speech_880_amplitude'] <= coded_880_bound(len(data)//4),
                        'Independent fixture exceeds its declared coded-carrier880 projection bound')
                analyzed_blocks += 1
        require(analyzed_blocks >= 100, 'Independent stimulus lacks a complete per-buffer measurement interval')
        result = dict(reference, capture_offset_ms=aligned['relative_offset_ms'],
                      code_correlation=aligned['correlation'], code_peak_prominence=aligned['peak_prominence'],
                      individually_verified_music_blocks=analyzed_blocks,
                      scope='Kernel status/queue independently anchors virtual playback; same digital capture pipeline, no physical proof',
                      unit=unit.identity(), capture=capture.poll(), kernel_status_path=str(status_dir/'status.json'))
        raw = root/'captured-s16le-stereo.pcm'
        with raw.open('xb') as stream:
            raw.chmod(0o600)
            for data in capture.chunks:
                stream.write(data)
        timestamps = root/'captured-timestamps.json'
        atomic_json(timestamps, [{'absolute_pts_monotonic_ns': capture.absolute[at], 'frames': len(data)//4}
            for at, data in zip(capture.captured_at, capture.chunks, strict=True)])
        result['artifacts'] = {'pcm': str(raw), 'timestamps': str(timestamps), 'sha256': hashlib.sha256(raw.read_bytes()).hexdigest()}
    finally:
        if unit:
            try:
                await asyncio.wait_for(unit.stop(), 10)
                require(not unit.alive, 'Independent exact output unit survived calibration cleanup')
                broker.network.forget_process(key)
            except Exception as exc:
                cleanup_errors.append(f'output unit: {type(exc).__name__}')
        if capture:
            try:
                await asyncio.wait_for(asyncio.to_thread(capture.close), 3)
                require(capture.pipeline.get_state(0).state == capture.Gst.State.NULL, 'Independent capture did not close')
            except Exception as exc:
                cleanup_errors.append(f'capture: {type(exc).__name__}')
        pin.close()
        require(not cleanup_errors, 'Independent calibration cleanup failed: '+', '.join(cleanup_errors))
    require(result is not None, 'Independent capture calibration produced no usable result')
    result['cleanup'] = {'exact_output_unit_stopped': True, 'capture_null': True, 'held_pin_closed': True}
    return result


def music_onset_index(capture):
    """Confirm a window, then arm continuity at its first audible buffer."""
    if len(capture.chunks) < 5:
        return None
    recent = capture.chunks[-5:]
    measured = observation.spectrum(recent, RATE, minimum_seconds=.05)
    if measured['music_440_amplitude'] <= 1000 or measured['median_440_band_energy_fraction'] <= .65:
        return None
    for index, data in enumerate(capture.chunks):
        block = observation.spectrum([data], RATE, minimum_seconds=.01)
        if block['music_440_amplitude'] > 1000 and block['median_440_band_energy_fraction'] > .65:
            return index
    return None


def retain_failed_capture(capture, directory, identifier):
    """Save bounded, already recorded data after stopping the capture thread."""
    chunks, captured_at, absolute = tuple(capture.chunks), tuple(capture.captured_at), dict(capture.absolute)
    limit = getattr(capture, 'maximum_bytes', 16*1024*1024)
    require(type(limit) is int and limit in {16*1024*1024, 48*1024*1024, 128*1024*1024},
            'Failed capture has an undeclared observation budget')
    require(len(chunks) == len(captured_at) and sum(map(len, chunks)) <= limit,
            'Failed capture is not a bounded complete record')
    timings = [{'absolute_pts_monotonic_ns': absolute[at], 'frames': len(data)//4}
               for at, data in zip(captured_at, chunks, strict=True)]
    pcm_path = directory/f'{identifier}-failed-final-s16le-stereo.pcm'
    timing_path = pcm_path.with_suffix('.timestamps.json')
    digest = hashlib.sha256()
    with os.fdopen(os.open(pcm_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
        for data in chunks:
            digest.update(data)
            stream.write(data)
    atomic_json(timing_path, timings)
    return {'path': str(pcm_path), 'timing_path': str(timing_path), 'sha256': digest.hexdigest(),
            'blocks': len(chunks), 'bytes': sum(map(len, chunks))}


class FinalPcmGuard:
    """A sticky per-buffer fence; aggregate medians cannot hide a bad block."""
    def __init__(self, capture):
        self.capture = capture
        self.index = None
        self.reference = None
        self.error = None
        self.checked_blocks = 0
        self.reference_blocks = 0
        self.failed_block = None
        self.end_requested_ns = None
        self.teardown_blocks = 0

    def end_at(self, requested_ns):
        """Explicit epoch retirement; defaults retain the original full gate."""
        require(self.error is None and self.end_requested_ns is None
                and type(requested_ns) is int and requested_ns > 0,
                'Invalid or repeated final-PCM retirement boundary')
        self.end_requested_ns = requested_ns

    def begin(self, index):
        require(self.index is None and type(index) is int and 0 <= index <= len(self.capture.chunks),
                'Invalid initial final-PCM observation index')
        self.index = index

    def preserve_reference(self, measured):
        require(measured['music_440_amplitude'] > 1000, 'Untouched-zone music baseline is not audible')
        self.reference = dict(measured)

    def check(self, *, live=True):
        require(type(live) is bool, 'Final PCM live observation flag must be boolean')
        require(self.error is None, self.error or 'Final PCM observation failed permanently')
        try:
            capture = self.capture
            capture.poll()
            require(capture.max_packet_gap < .3, 'Observed final PCM callback gap exceeds300ms')
            if live and capture.last_packet_at is not None:
                require(time.monotonic()-capture.last_packet_at < .3, 'Final PCM callback gap exceeds300ms')
            while self.index is not None and self.index < len(capture.chunks):
                if self.end_requested_ns is not None:
                    require(len(capture.captured_at) == len(capture.chunks),
                            'Retirement PCM lost its original callback observations')
                    callback = capture.captured_at[self.index]
                    require(type(callback) in (int, float) and np.isfinite(callback),
                            'Retirement PCM callback time is invalid')
                    if callback*1e9 > self.end_requested_ns:
                        # Capture.poll still validates ALL caps/frame/PTS/queue
                        # fences. Only a callback strictly after the recorded
                        # fixture stop is teardown, never musical success.
                        self.index += 1
                        self.teardown_blocks += 1
                        continue
                measured = observation.music_block(capture.chunks[self.index], RATE, 8)
                if self.reference is not None:
                    ratio = measured['music_440_amplitude']/self.reference['music_440_amplitude']
                    require(.95 < ratio < 1.05, 'An individual untouched-zone PCM block changed music gain')
                    require(measured['speech_880_amplitude'] <= max(4., self.reference['speech_880_amplitude']*4),
                            'An individual untouched-zone PCM block contains target-zone speech')
                    self.reference_blocks += 1
                self.index += 1  # Only a fully verified buffer can be consumed.
                self.checked_blocks += 1
        except RuntimeFailure as exc:
            self.error = str(exc)
            if self.index is not None and self.index < len(self.capture.chunks):
                at = self.capture.captured_at[self.index] if self.index < len(self.capture.captured_at) else None
                self.failed_block = {'index': self.index, 'absolute_pts_monotonic_ns': self.capture.absolute.get(at),
                                     'frames': len(self.capture.chunks[self.index])//4, 'error': self.error}
            raise

    def evidence(self):
        result = {'checked_music_blocks': self.checked_blocks, 'untouched_reference_blocks': self.reference_blocks,
                'untouched_music_ratio_bounds': [.95, 1.05],
                'untouched_voice_floor': max(4., self.reference['speech_880_amplitude']*4) if self.reference else None,
                'failed_block': self.failed_block}
        if self.end_requested_ns is not None:
            result.update(stop_requested_monotonic_ns=self.end_requested_ns,
                          teardown_blocks=self.teardown_blocks,
                          retirement_scope='All callbacks through stop retain original content fence; later callbacks retain frame/caps/PTS/queue fences')
        return result


async def observed_wait(predicate, observation_guard, description, *, timeout):
    """Retry only an explicit not-ready result; observation failures are fatal."""
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        await observation_guard()
        result = await asyncio.wait_for(predicate(), deadline-time.monotonic())
        await observation_guard()
        if result:
            return result
        await asyncio.sleep(.03)
    raise RuntimeFailure(f'Timed out waiting for{description}')


def measure_alignment(first, second, *, search_ms=100):
    """Return the numeric receipt before any synchronization acceptance gate."""
    ta, aa = first
    tb, ab = second
    margin = search_ms/1000+.01
    start, end = max(ta[0], tb[0])+margin, min(ta[-1], tb[-1])-margin
    require(end-start >= 1.5, 'Insufficient common final-PCM timing window')
    grid = np.arange(start, end, .001)
    x = np.interp(grid, ta, aa)
    x = (x-x.mean())/x.std()
    scores = []
    shifts = np.arange(-search_ms, search_ms+1)/1000
    for shift in shifts:
        y = np.interp(grid+shift, tb, ab)
        require(y.std() > 0, 'Group reference modulation is absent')
        y = (y-y.mean())/y.std()
        scores.append(float(np.mean(x*y)))
    index = int(np.argmax(scores))
    outside = [score for position, score in enumerate(scores) if abs(position-index) > 10]
    result = {'relative_offset_ms': round(float(shifts[index]*1000), 6),
              'correlation': scores[index], 'peak_prominence': scores[index]-max(outside),
              'resolution_ms': 1, 'common_observation_seconds': round(end-start, 6)}
    return result


def validate_alignment(result, *, maximum_delay_ms=2):
    require(result['correlation'] >= .985 and result['peak_prominence'] >= .015,
            'Final PCM does not identify the same uniquely coded program')
    if maximum_delay_ms is not None:
        require(abs(result['relative_offset_ms']) <= maximum_delay_ms, 'Actual two-zone final PCM differs by more than2ms')


def align_series(first, second, *, maximum_delay_ms=2, search_ms=100):
    result = measure_alignment(first, second, search_ms=search_ms)
    validate_alignment(result, maximum_delay_ms=maximum_delay_ms)
    return result


def record_group_alignment(report, series, *, frame_continuity=None):
    """Keep measured full/early/late values even when a timing gate fails."""
    report['group_alignment'] = measure_alignment(*series)
    report['group_alignment']['scope'] = (
        'Real digitalLoopbackPCM; every20ms window uses all actual bufferPTS/sample-position anchors '
        'on one shared Gst clock;1ms analysis grid, no physical speaker/acoustic or stock-phone grouping proof')
    if frame_continuity is not None:
        report['group_alignment']['capture_frame_continuity'] = deepcopy(frame_continuity)
    midpoint = (max(series[0][0][0], series[1][0][0])+min(series[0][0][-1], series[1][0][-1]))/2
    earlier = [(times[times < midpoint], values[times < midpoint]) for times, values in series]
    later = [(times[times >= midpoint], values[times >= midpoint]) for times, values in series]
    early, late = measure_alignment(*earlier), measure_alignment(*later)
    report['group_alignment'].update(early=early, late=late,
        drift_ms=late['relative_offset_ms']-early['relative_offset_ms'])
    validate_alignment(report['group_alignment'])
    validate_alignment(early)
    validate_alignment(late)
    require(abs(late['relative_offset_ms']-early['relative_offset_ms']) <= 2,
            'Relative final-PCM timing drifts across the coded program')


def legacy_snapshot():
    require(NATIVE_LAB is not None, 'An explicit native lab profile is required before hardware observation')
    return NATIVE_LAB.protected_snapshot()


async def verify_original_receiver(broker, state, original, expected, pid, birth, held_cgroup, *, stopped=False):
    """A synthetic producer may replace only this proven receiver incarnation.

    Cached ``alive`` is insufficient: a startup failure can precede the unit
    monitor's next sample. Inspect the original unit around every preparatory
    await; retain its durable reservation until its exact cgroup is empty.
    """
    key = f'{state.desired.id}:shairport'

    def reservation():
        require(state.processes.get('shairport') is original
                and original.identity() == expected
                and broker.network.manifest['processes'].get(key) == expected,
                'Original receiver handle or canonical reservation changed during replacement preparation')

    reservation()
    try:
        actual = await asyncio.wait_for(original.manager.inspect(expected['unit']), 5)
    except asyncio.TimeoutError as exc:
        raise RuntimeFailure('Original receiver identity inspection exceeded its deadline') from exc
    reservation()
    if stopped:
        if actual is not None:
            # systemd can clear the completed activation's ID while retaining
            # its inactive unit. Verify every immutable launch property; the
            # held original cgroup below proves termination of the old ID.
            inactive = dict(actual)
            if inactive.get('InvocationID') in ('0'*32, [0]*16):
                inactive['InvocationID'] = expected['invocation_id']
            original.manager.verify(expected, inactive)
        require(not original.alive and (actual is None or (
                    actual.get('MainPID') == 0 and actual.get('ActiveState') in {'inactive', 'failed'}))
                and 'populated 0' in original.manager.cgroup_events(held_cgroup),
                'Original exact receiver termination is not proven; preserve its reservation')
    else:
        if actual is not None:
            original.manager.verify(expected, actual)
        require(actual is not None and original.alive and original.process.pid == pid
                and actual.get('MainPID') == pid and actual.get('ActiveState') in {'active', 'activating'}
                and process_birth(pid) == birth,
                'Original receiver is not the exact live process; refuse synthetic replacement')
        descriptor = original.manager.open_cgroup(expected)
        try:
            saved, current = os.fstat(held_cgroup), os.fstat(descriptor)
            require(saved.st_ino == expected['cgroup_inode']
                    and (saved.st_dev, saved.st_ino) == (current.st_dev, current.st_ino)
                    and 'populated 1' in original.manager.cgroup_events(held_cgroup),
                    'Original receiver cgroup changed during replacement preparation')
        finally:
            os.close(descriptor)


async def launch_producer(broker, state, root, common_start_ns, *, duration_seconds=MAX_DURATION,
                          arrival_lead_ns=None, frozen_timing=None, music_minimum=False, music_soak=False,
                          bluetooth_receiver_idle=False):
    # Preparation only: the original receiver must be idle and its exact unit
    # must stop before its canonical reservation can be replaced. An arbitrary
    # validation role would be rejected by production manifest recovery.
    key = f'{state.desired.id}:shairport'
    require(type(duration_seconds) is int and (duration_seconds in {MAX_DURATION, 180, 420, 480}
            or music_soak is True and duration_seconds == 1830),
            'Refuse an undeclared native producer duration before replacement')
    require(all(type(mode) is bool for mode in (music_minimum, music_soak))
            and sum((music_minimum, music_soak)) <= 1, 'Music experiment admission must be exclusive boolean')
    require(type(bluetooth_receiver_idle) is bool and (not bluetooth_receiver_idle or (
        duration_seconds == 180 and state.desired.id == A and state.bluetooth_admission is not None
        and isinstance(state.desired.local_audio_device, str) and state.desired.local_audio_device.startswith('bluealsa:DEV=')
        and not any((music_minimum, music_soak)) and frozen_timing is None
        and type(common_start_ns) is int and common_start_ns == 0 and arrival_lead_ns is None
    )), 'Idle receiver hold requires only the exact descriptor-backed Bluetooth A fixture')
    if music_soak:
        load_soak_module().admit_launch(state.desired, duration_seconds, common_start_ns, arrival_lead_ns,
                                       frozen_timing, FrozenWorkerTiming, lab=NATIVE_LAB)
    if music_minimum:
        load_minimum_music_module().admit_launch(state.desired, duration_seconds, common_start_ns, arrival_lead_ns,
                                                 frozen_timing, FrozenWorkerTiming, lab=NATIVE_LAB)
    baseline_lead = 220_000_000 if state.desired.id == A else 80_000_000
    if arrival_lead_ns is None:
        arrival_lead_ns = baseline_lead
    require(type(arrival_lead_ns) is int and (arrival_lead_ns == baseline_lead
            or music_minimum or music_soak),
            'Refuse undeclared native arrival lead before replacement')
    original = state.processes.get('shairport')
    require(original is not None, 'Original receiver is missing; refuse synthetic replacement')
    expected = deepcopy(original.identity())
    pid = original.process.pid
    birth = process_birth(pid) if type(pid) is int and pid > 0 else None
    require(birth and expected.get('name') == 'shairport' and expected.get('invocation_id')
            and expected.get('control_group') and expected.get('cgroup_inode'),
            'Original receiver has no admitted process identity; refuse synthetic replacement')
    held_cgroup = original.manager.open_cgroup(expected)
    try:
        await verify_original_receiver(broker, state, original, expected, pid, birth, held_cgroup)
        health = await call_rpc(broker._worker_socket(state), 'health', {}, timeout=2)
        if music_minimum:
            load_minimum_music_module().require_idle_worker(health)
        if music_soak:
            load_soak_module().require_idle_worker(health)
        require(health['source']['owner'] is None, 'Refuse to replace a receiver carrying a live source')
        await verify_original_receiver(broker, state, original, expected, pid, birth, held_cgroup)
        await asyncio.wait_for(original.stop(), 10)
        await verify_original_receiver(broker, state, original, expected, pid, birth, held_cgroup, stopped=True)
    finally:
        os.close(held_cgroup)
    broker.network.forget_process(key)
    state.processes.pop('shairport')
    account = broker._account(state.desired.id, 'receiver', state.desired.slot)
    config_dir = directory(root/'config', {'uid': 0, 'gid': account['gid']}, mode=0o2750)
    status_dir = directory(root/'status', account)
    config = config_dir/'producer.json'
    command = config_dir/'command.json'
    publish_command(command, {'generation': 1, 'action': 'wait'}, account)
    producer_config = {'uid': account['uid'], 'gid': account['gid'], 'socket': str(VIEW/'input/music.sock'),
        'status': str(VIEW/'validation/status.json'), 'command': str(VIEW/'probe-config/command.json'),
        'group': GROUP, 'common_start_ns': common_start_ns,
        'arrival_lead_ns': arrival_lead_ns,
        'wide_bracket': state.desired.id == B, 'leader': state.desired.id == A}
    if music_minimum:
        producer_config['music_minimum'] = load_minimum_music_module().PRODUCER_PROFILE
    if music_soak:
        producer_config['music_soak'] = load_soak_module().producer_profile()
    if duration_seconds != MAX_DURATION:
        producer_config['duration_seconds'] = duration_seconds
    if bluetooth_receiver_idle:
        started = time.monotonic_ns()
        producer_config['bluetooth_receiver_idle'] = {'version': 1, 'room_id': A,
            'started_monotonic_ns': started, 'deadline_monotonic_ns': started+180_000_000_000}
    atomic_json(config, producer_config)
    file_owner(config, account)
    owned = await broker._start_process(key, 'shairport', [sys.executable, str(HERE), '--producer',
        str(VIEW/'probe-config/producer.json')], root, account=account,
        binds=[Bind(str(state.directory/'input'), str(VIEW/'input')),
               Bind(str(config_dir), str(VIEW/'probe-config')),
               Bind(str(status_dir), str(VIEW/'validation'), True)])
    state.processes['shairport'] = owned
    await broker._remember_process(key, owned)
    return {'unit': owned, 'key': key, 'status': status_dir/'status.json', 'command': command,
            'account': account}


def producer_status(handle):
    require(handle['unit'].alive, 'Exact synthetic receiver unit exited')
    try:
        state = json.loads(handle['status'].read_text())
    except FileNotFoundError:
        return None
    require(not state.get('error'), state.get('error') or 'Synthetic receiver failed')
    require(state['uid'] == handle['account']['uid'] and state['gid'] == handle['account']['gid'],
            'Synthetic native receiver did not retain its exact credentials')
    return state


def failure_cause(exception):
    """Bounded transport facts, without rendering causes or partial payloads."""
    result, seen = [], {id(exception)}
    for _ in range(3):
        cause = exception.__cause__
        if cause is None and not exception.__suppress_context__:
            cause = exception.__context__
        if cause is None or id(cause) in seen:
            break
        seen.add(id(cause))
        record = {'type': type(cause).__name__[:80], 'timeout': isinstance(cause, TimeoutError)}
        if isinstance(cause, OSError) and type(cause.errno) is int and -4096 <= cause.errno <= 4096:
            record['errno'] = cause.errno
        if isinstance(cause, asyncio.IncompleteReadError):
            if type(cause.expected) is int and 0 <= cause.expected < (1 << 63):
                record['expected'] = cause.expected
            record['received'] = len(cause.partial)
        result.append(record)
        exception = cause
    return result


async def enrollment(api, device):
    found = await api.request('GET', '/api/v1/local-devices')
    candidates = [entry for entry in found['devices'] if 'loopback' in entry['can_bind_by']
                  and f'PCM {device}/7' in entry['label']]
    require(len(candidates) == 1, f'Exact virtual Loopback DEV{device}/sub7 is not uniquely enrollable')
    reply = await api.request('POST', '/api/v1/local-devices/bind', expected=201,
                             json={'selection_id': candidates[0]['selection_id'], 'binding': 'loopback', 'conversion': True})
    require(reply['binding'] == 'loopback' and reply['device'].startswith('shiri:device='), 'Unexpected virtual enrollment')
    return reply['device']


_epoch_module = None


def load_epoch_module():
    global _epoch_module
    if _epoch_module is None:
        spec = importlib.util.spec_from_file_location('native_group_latency_epochs', HERE.with_name('native_latency_epochs.py'))
        _epoch_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = _epoch_module
        spec.loader.exec_module(_epoch_module)
    return _epoch_module


_minimum_music_module = None


def load_minimum_music_module():
    global _minimum_music_module
    if _minimum_music_module is None:
        spec = importlib.util.spec_from_file_location('native_group_music_minimum', HERE.with_name('native_music_minimum.py'))
        _minimum_music_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = _minimum_music_module
        spec.loader.exec_module(_minimum_music_module)
    return _minimum_music_module


_soak_module = None

def load_soak_module():
    global _soak_module
    if _soak_module is None:
        spec = importlib.util.spec_from_file_location('native_group_music_soak', HERE.with_name('native_music_soak.py'))
        _soak_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = _soak_module
        spec.loader.exec_module(_soak_module)
    return _soak_module


def epoch_result_path(value):
    require(NATIVE_LAB is not None and type(value) is str, 'Epoch report requires an explicit trusted fresh-lab result path')
    path = Path(value)
    require(path.is_absolute() and path.name == 'epoch-result.json' and path.parent.parent == WORK
            and path.parent.name.startswith('latency-supervisor-') and not path.exists(),
            'Epoch result must be fresh inside its exact private supervisor parent')
    require(path.parent.resolve(strict=True) == path.parent and WORK.resolve(strict=True) == WORK,
            'Epoch report path has a symlink or noncanonical parent')
    for parent in (WORK, path.parent):
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
                'Epoch report parent is not root-owned and protected')
    return path


async def run_check(parent_namespace=None, original_netns_fd=None, *, speech_stress=False, zone_faults=False, latency_epoch=None, latency_result=None, finite_speech=False, music_minimum=False, music_soak=False, soak_buffer_ms=None, soak_horizon_ms=None, minimum_policy=False):
    require(all(type(mode) is bool for mode in (speech_stress, zone_faults, finite_speech, music_minimum, music_soak)),
            'Speech stress, zone faults and latency probe must be explicit boolean modes')
    require(sum((speech_stress, zone_faults, latency_epoch is not None, music_minimum, music_soak)) <= 1,
            'Speech stress, zone faults and latency probe are mutually exclusive')
    require(not finite_speech or (latency_epoch is not None and not any((speech_stress, zone_faults))),
            'Finite speech requires its separate explicit prepared epoch')
    require(music_soak or soak_buffer_ms is None and soak_horizon_ms is None, 'Soak timing arguments require explicit soak mode')
    require(type(minimum_policy) is bool and (not minimum_policy or NATIVE_LAB is not None
            and not any((latency_epoch is not None, finite_speech, music_minimum, music_soak))),
            'Minimum coverage is an explicit clean-lab grouping/stress/fault policy only')
    if music_soak:
        load_soak_module().configure(soak_buffer_ms, soak_horizon_ms)
    epoch = (load_soak_module() if music_soak else load_minimum_music_module() if music_minimum
             else load_epoch_module() if latency_epoch is not None else None)
    if music_minimum or music_soak:
        require(NATIVE_LAB is not None, 'Music startup requires the explicit clean lab')
        latency_epoch = epoch.Phase(str(uuid4()), 0, 'native')
    require(latency_epoch is None or type(latency_epoch) is epoch.Phase, 'Latency epoch must be an explicit prepared phase')
    if latency_epoch is not None:
        latency_epoch.receipt()
        require(NATIVE_LAB is not None, 'Prepared latency epochs require the explicit fresh lab profile')
    stress = faults = latency = None
    if speech_stress:
        stress_spec = importlib.util.spec_from_file_location('native_group_speech_stress', HERE.with_name('native_speech_stress.py'))
        stress = importlib.util.module_from_spec(stress_spec)
        stress_spec.loader.exec_module(stress)
    if zone_faults:
        fault_spec = importlib.util.spec_from_file_location('native_group_zone_faults', HERE.with_name('native_zone_faults.py'))
        faults = importlib.util.module_from_spec(fault_spec)
        sys.modules[fault_spec.name] = faults
        fault_spec.loader.exec_module(faults)
    if latency_epoch is not None:
        latency_spec = importlib.util.spec_from_file_location('native_group_latency_probe', HERE.with_name('native_latency_probe.py'))
        latency = importlib.util.module_from_spec(latency_spec)
        sys.modules[latency_spec.name] = latency
        latency_spec.loader.exec_module(latency)
    extended = stress or faults or latency
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'passed': False,
        'scope': 'Synthetic exact-UID native producers; actual rootlessAPI/Nobly/broker/worker/OwnTone/final kernel Loopback PCM',
        'stock_phone_verified': False, 'physical_speakers_verified': False, 'cast_input_verified': False,
        'native_phone_grouping_verified': False, 'cleanup': {}, 'rooms': ZONES, 'group_id': GROUP}
    if music_soak:
        report['mode'] = 'music_soak'
    elif music_minimum:
        report['mode'] = 'music_minimum'
    elif stress:
        report['mode'] = 'speech_stress'
    elif faults:
        report['mode'] = 'zone_faults'
    elif latency:
        report['mode'] = 'finite_speech' if finite_speech else 'latency_probe'
    result_path = (RESULT.with_name('shiri-v2-native-speech-stress-result.json') if stress else
                   RESULT.with_name('shiri-v2-native-zone-faults-result.json') if faults else
                   RESULT.with_name('shiri-v2-native-latency-probe-result.json') if latency else RESULT)
    if music_soak:
        result_path = RESULT.with_name('shiri-v2-native-music-soak-result.json')
    elif music_minimum:
        result_path = RESULT.with_name('shiri-v2-native-music-minimum-result.json')
    elif latency_epoch is not None:
        result_path = epoch_result_path(latency_result)
    epoch_context = None
    broker = api = api_process = peer = tone = lan = None
    captures, producers, room_states, pcm_guards = {}, {}, {}, {}
    identity = token = baseline = legacy = temporary = None
    done = asyncio.Event()
    monitor = fault_observer = None
    complete = False
    errors = []
    frozen_timing = None
    minimum_timing = None
    declared_horizon_ns = RELAY_DELAY_NS
    try:
        require(sys.platform == 'linux' and os.geteuid() == 0, 'Run explicitly as Linux root')
        require(type(original_netns_fd) is int and original_netns_fd >= 3,
                'Run through the supervisor with its inherited original namespace descriptor')
        require(NATIVE_LAB is not None, 'An explicit native lab profile is required')
        manifest = json.loads((STATE/'ownership.json').read_text())
        report['native_lab'] = native_lab_admission(manifest, report.get('mode', 'grouping'), original_netns_fd=original_netns_fd)
        report['installation_id'] = manifest['installation_id']
        require(BINARIES.is_dir() and IDENTITIES.is_file(), 'Pinned next9 candidate and static UID map are required')
        observation.base.closed_slot()
        legacy = legacy_snapshot()
        baseline = await observation.base.host_snapshot()
        account, group = pwd.getpwnam('shiri'), grp.getgrnam('shiri')
        # Daemons retain PrivateTmp. Protected API paths therefore live outside
        # /tmp, where the service mount namespace would hide them before its
        # InaccessiblePaths mounts are installed.
        root_directory(WORK, mode=0o755)
        temporary = Path(tempfile.mkdtemp(prefix='native-group-', dir=WORK))
        os.chown(temporary, 0, group.gr_gid)
        temporary.chmod(0o750)
        report['artifacts'] = {'private_directory': str(temporary), 'final_pcm': {}}
        lan = isolated_lan.IsolatedLan(temporary/'isolated-lan')
        await lan.start(original_netns_fd=original_netns_fd, parent_namespace=parent_namespace)
        report['network_fixture'] = lan.evidence()
        settings = Settings(state_dir=temporary/'api', api_token_file=temporary/'token',
            runtime_state_dir=STATE, runtime_dir=RUN, runtime_socket=RUN/'runtime.sock',
            binary_dir=BINARIES, daemon_identity_file=IDENTITIES)
        # Create the actual protected API paths before the broker launches any
        # service. A nonexistent production default must not stand in for the
        # disposable API's real credential/state boundary.
        api_process, token = await launch_api(temporary, settings, account, group, interface=lan.interface)
        if music_minimum or music_soak:
            broker_class = epoch.broker_class(IsolatedBroker)
        else:
            broker_class = IsolatedBroker
        broker = broker_class(settings, parent_namespace)
        health = await broker.start(serve=True)
        require(health['ready'], health.get('error') or 'Actual broker preflight failed; do not weaken its gates')
        require(broker.network.installation_id == manifest['installation_id'], 'Candidate installation identity changed')
        report['installation_id'], report['versions'] = manifest['installation_id'], health['versions']
        api = Api()
        async def api_ready():
            require(api_process.alive, 'Rootless API exited')
            try:
                return (await api.client.get('/api/v1/health/live')).status_code == 200
            except httpx.HTTPError:
                return False
        await observation.base.eventually(api_ready, 'rootless API', timeout=20)
        await api.request('GET', '/api/v1/state', expected=401)
        await api.request('POST', '/api/v1/session', json={'token': token})
        report['rootless_api'] = api_process.evidence()
        async def disabled_settled():
            rooms = (await api.request('GET', '/api/v1/state'))['rooms']
            return len(rooms) == 8 and all(not room['enabled'] and room['runtime']['status'] == 'stopped' for room in rooms)
        await observation.base.eventually(disabled_settled, 'all disposable rooms stopped before independent fixtures', timeout=8)
        import gi
        gi.require_version('Gst', '1.0')
        from gi.repository import Gst
        Gst.init(None)
        clock = Gst.SystemClock.obtain()
        require(clock.get_property('clock-type').value_nick == 'monotonic', 'Capture requires the actual monotonic Gst system clock')
        before, clock_at, after = time.monotonic_ns(), clock.get_time(), time.monotonic_ns()
        require(after-before <= 1_000_000, 'Capture clock mapping bracket exceeds1ms')
        offset = clock_at-(before+(after-before)//2)
        base_time = clock.get_time()
        report['capture_clock'] = {'gst_offset_to_monotonic_ns': offset, 'mapping_bracket_ns': after-before,
                                  'common_base_time_ns': base_time, 'instrument': 'sharedGstSystemClock/absolutebufferPTS'}
        bindings = {identifier: await enrollment(api, zone['device']) for identifier, zone in ZONES.items()}
        report['independent_capture_baselines'] = {}
        for identifier, binding in bindings.items():
            root = temporary/f'capture-baseline-{identifier}'
            root_directory(root)
            report['independent_capture_baselines'][identifier] = await calibrate_capture(
                broker, identifier, binding, root, clock, base_time, offset)
        observation.base.closed_slot()
        for identifier, zone in ZONES.items():
            await api.patch(identifier, {'local_audio_device': bindings[identifier], 'volume': 100, 'duck_gain': .2, 'enabled': True})
            async def ready(identifier=identifier):
                room = await api.room(identifier)
                require(room['runtime']['status'] not in {'error', 'degraded'}, room['runtime'].get('error') or 'Room failed')
                return room if room['runtime']['status'] == 'running' else None
            await observation.base.eventually(ready, f'exact room{zone["slot"]}', timeout=65)
            state = broker.rooms[identifier]
            pin = state.local_pin
            require(pin is not None and pin.fingerprint['binding'] == 'loopback'
                    and pin.manifest['device'] == zone['device'] and pin.manifest['subdevice'] == 7,
                    'Broker did not pin the exact allowed virtual output')
            pin.validate()
            room_states[identifier] = state
            saved = await api.room(identifier)
            await api.request('PUT', f'/api/v1/rooms/{identifier}/speakers',
                json={'expected_revision': saved['revision'], 'speaker_ids': ['0']})
            async def selected(state=state, identifier=identifier):
                room = await api.room(identifier)
                actual = await state.client.outputs(set())
                report.setdefault('selection_last_observed', {})[identifier] = observation.local_selection_evidence(room, state, actual)
                return observation.exact_local_selected(room, state.selected_ids, actual)
            await observation.base.eventually(selected, 'exact virtual local0 selection', timeout=12)
        if NATIVE_LAB is not None and latency_epoch is None:
            enabled = {identifier: state for identifier, state in broker.rooms.items() if state.desired.enabled}
            require(set(enabled) == set(room_states), 'Candidate baseline contains another enabled room')
            worker_healths = {identifier: await call_rpc(broker._worker_socket(state), 'health', {}, timeout=2)
                             for identifier, state in enabled.items()}
            if minimum_policy:
                minimum_timing = load_minimum_coverage_module().freeze(
                    [state.desired for state in broker.rooms.values()], worker_healths)
                declared_horizon_ns = minimum_timing.horizon_ns
                report['minimum_policy'] = True
                report['frozen_worker_timing'] = minimum_timing.receipt()
                broker.minimum_coverage_timing = minimum_timing
                broker.minimum_coverage_timing_check = load_minimum_coverage_module().require_room_timing
            else:
                frozen_timing = freeze_worker_timing([state.desired for state in broker.rooms.values()], worker_healths)
                declared_horizon_ns = frozen_timing.horizon_ns
                report['frozen_worker_timing'] = frozen_timing.receipt()
        if latency_epoch is not None:
            helpers = SimpleNamespace(NATIVE_LAB=NATIVE_LAB, ZONES=ZONES, RATE=RATE, GROUP=GROUP,
                FrozenWorkerTiming=FrozenWorkerTiming, program_pcm=program_pcm,
                observation=observation, FinalPcmGuard=FinalPcmGuard,
                launch_producer=launch_producer, producer_status=producer_status, publish_command=publish_command,
                music_onset_index=music_onset_index, capture_snapshot=capture_snapshot,
                modulation=modulation, declared_capture=declared_capture, align_series=align_series,
                measure_alignment=measure_alignment, record_group_alignment=record_group_alignment,
                validate_horizon=validate_horizon, retain_failed_capture=retain_failed_capture,
                require_untouched_music_health=require_untouched_music_health)
            epoch_context = SimpleNamespace(api=api, broker=broker, states=room_states,
                captures=captures, producers=producers, pcm_guards=pcm_guards, report=report,
                temporary=temporary, clock=clock, base_time=base_time, clock_offset=offset,
                capture_factory=epoch.capture_type(Capture) if music_soak else latency.capture_type(Capture), guard_factory=FinalPcmGuard,
                target=A, untouched=B, group=helpers, latency=latency, phase=latency_epoch,
                observer=None, frozen_plan=None, declared_horizon_ns=None, finite_speech=finite_speech,
                evidence={**latency_epoch.receipt(), 'passed': False})
            await epoch.prepare(epoch_context)
            fault_observer = await epoch.exercise(epoch_context)
            complete = True
        else:
            card_a, card_b = (room_states[key].local_pin.manifest['card_index'] for key in (A, B))
            require(card_a == card_b, 'The two outputs are not the same constrained virtual Loopback instance')
            # Synthetic source installation happens before any measured program.
            # Suspend only the periodic health observer while swapping the idle
            # receiver units, then restore the actual production observer before
            # source delivery or continuity evidence starts.
            broker._monitor.cancel()
            await asyncio.gather(broker._monitor, return_exceptions=True)
            for identifier, state in room_states.items():
                root = state.directory/'native-validation'
                root_directory(root)
                producers[identifier] = await launch_producer(broker, state, root, 0,
                    duration_seconds=extended.PRODUCER_SECONDS if extended else MAX_DURATION)
            broker._monitor = asyncio.create_task(broker._health_monitor(), name='native-validation-health')
            report['synthetic_receiver_preparation'] = {'canonical_owned_role': 'shairport',
                'original_idle_units_stopped': True, 'production_health_monitor_restored_before_pcm': True}
            async def both_granted():
                values = {key: producer_status(handle) for key, handle in producers.items()}
                return values if all(value and value['stage'] in {'granted', 'streaming'} for value in values.values()) else None
            report['producer_grants'] = await observation.base.eventually(both_granted, 'both exact receiverUID grants', timeout=8)
            common_start = time.monotonic_ns()+4_000_000_000
            report['common_program_start_monotonic_ns'] = common_start
            for handle in producers.values():
                publish_command(handle['command'], {'generation': 2, 'action': 'run', 'common_start_ns': common_start}, handle['account'])
            initial_identities = {key: {name: process.identity() for name, process in state.processes.items()}
                                  for key, state in room_states.items()}
            capture_class = extended.capture_type(Capture) if extended else Capture
            for identifier, zone in ZONES.items():
                captures[identifier] = capture_class(f'hw:{card_a},{1-zone["device"]},7', clock, base_time, offset)
            phase = 'startup'
            pcm_guards = {key: FinalPcmGuard(captures[key]) for key in ZONES}
            control_samples, progress_anchors = [], {}
            def verify_pcm():
                for identifier in room_states:
                    pcm_guards[identifier].check()
            async def healthy():
                verify_pcm()
                worker_healths = {}
                for identifier, state in room_states.items():
                    producer_status(producers[identifier])
                    require(api_process.alive, 'Owned rootless API exited')
                    require({name: process.identity() for name, process in state.processes.items()} == initial_identities[identifier],
                            'A room daemon unit identity changed during group/TTS proof')
                    require(all(process.alive for process in state.processes.values()), 'An owned room daemon exited during group/TTS proof')
                    actual = await state.client.outputs(set())
                    require(state.selected_ids == ['0'] and [entry['id'] for entry in actual if entry['selected']] == ['0'],
                            'Actual selected output changed or a house output became selected')
                    health = await call_rpc(broker._worker_socket(state), 'health', {}, timeout=2)
                    worker_healths[identifier] = health
                    report.setdefault('worker_last_observed', {})[identifier] = {'phase': phase, 'health': health}
                    require(health['ready'] and not health.get('error') and health['dropped_bytes'] == 0
                            and health['speech_dropped_frames'] == 0, 'Actual room worker reports failure/backpressure')
                    owner = health['source']['owner']
                    expected = producer_status(producers[identifier])
                    require(owner and owner['protocol'] == 'airplay2' and owner['session_id'] == expected['session_id'],
                            'Exact native source owner changed during observation')
                    player = await state.client.request('GET', '/api/player')
                    if phase != 'startup':
                        require(player['state'] == 'play' and player['item_id'] == report['initial_players'][identifier]['item_id'],
                                'OwnTone program stopped or its item changed during TTS')
                        if stress:
                            require(owner['epoch'] == expected['epoch'] and owner['incarnation'] == expected['incarnation']
                                    and health['native_generation'] == expected['generation'],
                                    'Speech stress changed exact native source/generation identity')
                            stress.observe_progress(progress_anchors, identifier, player.get('item_progress_ms'), time.monotonic())
                    require(player.get('volume') == 100 and state.current_volume == 100,
                            f'Unexpected room volume effect: room={identifier}, phase={phase}, '
                            f'backend={player.get("volume")}, runtime={state.current_volume}, desired={state.desired.volume}')
                    control_samples.append({'at': time.monotonic(), 'room_id': identifier, 'phase': phase,
                        'progress_ms': player.get('item_progress_ms'), 'gain': health['music_gain'],
                        'source_epoch': owner['epoch'], 'speech_session_id': health['speech_session_id']})
                if frozen_timing is not None:
                    require_worker_timing(frozen_timing, [state.desired for state in broker.rooms.values()], worker_healths)
                if minimum_timing is not None:
                    load_minimum_coverage_module().require_timing(
                        minimum_timing, [state.desired for state in broker.rooms.values()], worker_healths)
                require(len(control_samples) < (extended.CONTROL_SAMPLES if extended else 3000),
                        'Control observation exceeded its bounded record count')
            async def observe_controls():
                while not done.is_set():
                    await healthy()
                    await asyncio.sleep(.06)
            monitor = asyncio.create_task(observe_controls())
            async def guard():
                if monitor.done():
                    monitor.result()  # Preserve the concrete sticky observation failure.
                    raise RuntimeFailure('Group continuity monitor ended unexpectedly')
                verify_pcm()
            for capture in captures.values():
                capture.start()
            end = time.monotonic()+20
            onset_indexes = {}
            while time.monotonic() < end and len(onset_indexes) != 2:
                await guard()
                for identifier, capture in captures.items():
                    if identifier in onset_indexes:
                        continue
                    onset = music_onset_index(capture)
                    if onset is not None:
                        onset_indexes[identifier] = onset
                        pcm_guards[identifier].begin(onset)
                await asyncio.sleep(.02)
            require(len(onset_indexes) == 2, 'Both actual final outputs did not produce the native440 program within20s')
            report['initial_players'] = {key: await state.client.request('GET', '/api/player') for key, state in room_states.items()}
            require(all(player['state'] == 'play' for player in report['initial_players'].values()), 'Actual program is not playing')
            phase = 'group_alignment'
            # Collect the common coded interval while all observers remain active.
            target = (common_start+declared_horizon_ns)/1e9 + 18
            while time.monotonic() < target:
                await guard()
                await asyncio.sleep(.03)
            start_ns = common_start+declared_horizon_ns+2_000_000_000
            end_ns = common_start+declared_horizon_ns+18_000_000_000
            snapshots = {key: capture_snapshot(captures[key]) for key in (A, B)}
            series = await asyncio.gather(*(asyncio.to_thread(modulation, snapshots[key], start_ns, end_ns) for key in (A, B)))
            await asyncio.to_thread(record_group_alignment, report, series,
                frame_continuity={identifier: snapshots[identifier].frame_continuity for identifier in (A, B)})
            reference = declared_capture(common_start+declared_horizon_ns, RATE*2, RATE*18)
            declared = await asyncio.to_thread(modulation, reference, start_ns, end_ns)
            report['calendar_observation'] = {}
            for identifier, observed in zip((A, B), series, strict=True):
                displacement = await asyncio.to_thread(align_series, declared, observed,
                                                        maximum_delay_ms=None, search_ms=1000)
                report['calendar_observation'][identifier] = {
                    'measured_final_pcm_vs_declared_presentation_ms': displacement['relative_offset_ms'],
                    'correlation': displacement['correlation'], 'resolution_ms': displacement['resolution_ms'],
                    'scope': 'Known generated stimulus vs shared-clock digital capturePTS; independent kernel-digital baseline corrects capture offset, no physical delay/profile adjustment',
                    'horizon': validate_horizon(displacement, report['independent_capture_baselines'][identifier])}
            # Code ends before the following constant440 speech ratio measurements.
            while time.monotonic() < (common_start+declared_horizon_ns)/1e9+CODE_SECONDS+1:
                await guard()
                await asyncio.sleep(.03)
            async def stage(label, seconds=1.2):
                nonlocal phase
                phase = label
                first = {key: len(cap.chunks) for key, cap in captures.items()}
                began = time.monotonic()
                while time.monotonic()-began < seconds:
                    await guard()
                    await asyncio.sleep(.03)
                measured = {key: observation.spectrum(cap.chunks[first[key]:], RATE) for key, cap in captures.items()}
                report.setdefault('stages', {})[label] = measured
                return measured
            baseline_spectra = await stage('baseline')
            for value in baseline_spectra.values():
                observation.require_music(value, 1)
            pcm_guards[B].preserve_reference(baseline_spectra[B])
            peer, tone = observation.base.make_speech_peer()
            identity = {'session_id': str(uuid4()), 'request_id': str(uuid4())}
            await peer.setLocalDescription(await peer.createOffer())
            answer = await api.request('POST', f'/api/v1/nobly/rooms/{ZONES[A]["nobly"]}/speech',
                json={**identity, 'action': 'offer', 'sdp': peer.localDescription.sdp, 'type': 'offer'})
            require(answer['admitted_room_id'] == A, 'Nobly binding admitted speech to the wrong stable UUID')
            await peer.setRemoteDescription(RTCSessionDescription(sdp=answer['sdp'], type=answer['type']))
            await api.request('POST', f'/api/v1/rooms/{B}/speech',
                json={**identity, 'action': 'close', 'request_id': str(uuid4())}, expected=409)
            async def transition(label, kind, previous_voice=None):
                nonlocal phase
                phase = label
                trigger = time.monotonic()
                index = len(captures[A].chunks)
                gate = observation.SpectrumTransition(kind, trigger, baseline_spectra[A]['music_440_amplitude'],
                    max(1., baseline_spectra[A]['speech_880_amplitude']), previous_voice=previous_voice)
                deadline = trigger+8
                while time.monotonic() < deadline:
                    await guard()
                    while index < len(captures[A].chunks):
                        passed = gate.push(captures[A].chunks[index], captures[A].captured_at[index], RATE)
                        index += 1
                        if passed:
                            report.setdefault('transitions', {})[label] = gate.evidence(time.monotonic())
                            return
                    await asyncio.sleep(.02)
                report.setdefault('transitions', {})[label] = gate.evidence(time.monotonic())
                raise RuntimeFailure(f'Actual final PCM transition{label} did not pass')
            await transition('duck_and_voice', 'duck_voice')
            ducked = await stage('speech_active')
            require(.17 < ducked[A]['music_440_amplitude']/baseline_spectra[A]['music_440_amplitude'] < .23,
                    'Target A did not retain precise .2 music ducking')
            require(.95 < ducked[B]['music_440_amplitude']/baseline_spectra[B]['music_440_amplitude'] < 1.05,
                    'Room B music was ducked by target A speech')
            require(ducked[B]['speech_880_amplitude'] <= max(4., baseline_spectra[B]['speech_880_amplitude']*4),
                    'Target A speech leaked into room B final PCM')
            tone.silent = True
            await transition('silent_restore', 'restore_no_voice', ducked[A]['speech_880_amplitude'])
            await stage('speech_silent')
            tone.silent = False
            await transition('resumed_voice', 'duck_voice')
            await api.request('POST', f'/api/v1/rooms/{A}/speech',
                              json={**identity, 'action': 'close', 'request_id': str(uuid4())})
            await transition('closed_restore', 'restore_no_voice', ducked[A]['speech_880_amplitude'])
            closed = await stage('speech_closed')
            require(.95 < closed[B]['music_440_amplitude']/baseline_spectra[B]['music_440_amplitude'] < 1.05,
                    'Room B final music changed after target A close')
            require(all(value['speech_880_amplitude'] <= max(4., baseline_spectra[key]['speech_880_amplitude']*4)
                        for key, value in closed.items()), 'Speech retained an audible final tail after close')
            if stress:
                tone.stop()
                await asyncio.wait_for(peer.close(), 4)
                require(peer.connectionState == 'closed', 'Baseline peer must close before explicit stress sessions')
                def stress_phase(value):
                    nonlocal phase
                    phase = value
                await stress.exercise(api, broker, room_states, captures, pcm_guards, guard, report,
                                      phase_change=stress_phase)
            for identifier in ZONES:
                values = [sample for sample in control_samples if sample['room_id'] == identifier and sample['phase'] != 'startup']
                require(values[-1]['progress_ms'] > values[0]['progress_ms']+10000, 'OwnTone NPT did not keep advancing through group/TTS proof')
            report['control_samples'] = control_samples
            report['captures'] = {key: cap.poll() for key, cap in captures.items()}
            report['per_buffer_evidence'] = {key: guard.evidence() for key, guard in pcm_guards.items()}
            report['artifacts'] = {'private_directory': str(temporary), 'final_pcm': {}}
            for key, cap in captures.items():
                pcm_path = temporary/f'{key}-final-s16le-stereo.pcm'
                with pcm_path.open('xb') as stream:
                    pcm_path.chmod(0o600)
                    for data in cap.chunks:
                        stream.write(data)
                timing_path = pcm_path.with_suffix('.timestamps.json')
                atomic_json(timing_path, [{'absolute_pts_monotonic_ns': cap.absolute[at], 'frames': len(data)//4}
                    for at, data in zip(cap.captured_at, cap.chunks, strict=True)])
                report['artifacts']['final_pcm'][key] = {'path': str(pcm_path), 'timing_path': str(timing_path),
                                                        'sha256': hashlib.sha256(pcm_path.read_bytes()).hexdigest()}
            report['producer_final'] = {key: producer_status(handle) for key, handle in producers.items()}
            if faults:
                report['protected_producer_final'] = deepcopy(report['producer_final'])
                async def handoff_controls():
                    # The fault helper starts and checks B's original independent
                    # observer BEFORE joining this all-room monitor. No capture or
                    # source generation is reset during the handoff.
                    done.set()
                    await monitor
                context = faults.FaultContext(api=api, broker=broker, states=room_states,
                    captures=captures, producers=producers, pcm_guards=pcm_guards, report=report,
                    temporary=temporary, clock=clock, base_time=base_time, clock_offset=offset,
                    capture_factory=capture_class, guard_factory=FinalPcmGuard, launch_producer=launch_producer,
                    publish_command=publish_command, producer_status=producer_status,
                    onset_index=music_onset_index, retain_capture=retain_failed_capture,
                    handoff=handoff_controls, target=A, untouched=B,
                    untouched_health_check=require_untouched_music_health,
                    minimum_timing=getattr(broker, 'minimum_coverage_timing', None),
                    timing_check=getattr(broker, 'minimum_coverage_timing_check', None))
                if report.get('minimum_policy') is True:
                    require(context.minimum_timing is not None and callable(context.timing_check),
                            'Minimum fault coverage lost the exact pre-PCM broker plan')
                fault_observer = await faults.exercise(context)
                report['producer_final'] = {key: producer_status(handle) for key, handle in producers.items()}
            # Retirement races happen after the protected uninterrupted proof.
            done.set()
            await monitor
            # Continue B's exact original capture through all explicit A retirement
            # waits. A fresh cutover observer must not reset B's failure history.
            done.clear()
            async def observe_untouched():
                while not done.is_set():
                    if fault_observer is not None:
                        await fault_observer.check()
                    else:
                        pcm_guards[B].check()
                    await asyncio.sleep(.02)
            monitor = asyncio.create_task(observe_untouched())
            async def untouched_guard():
                if monitor.done():
                    monitor.result()
                    raise RuntimeFailure('Untouched-zone observer ended unexpectedly')
                if fault_observer is not None:
                    await fault_observer.check()
                else:
                    pcm_guards[B].check()
            before_owner = report['producer_final'][A]['session_id']
            # A source flush may legitimately clear its PCM buffers. Start a fresh
            # A observer after this explicit cutover; never relabel earlier gaps as
            # continuous. B's original capture remains independently continuous.
            await asyncio.wait_for(asyncio.to_thread(captures[A].close), 3)
            require(captures[A].pipeline.get_state(0).state == captures[A].Gst.State.NULL,
                    'Initial A observer did not release before explicit source cutover')
            report['cleanup']['initial_a_capture_null'] = True
            publish_command(producers[A]['command'], {'generation': 3, 'action': 'takeover'}, producers[A]['account'])
            async def replaced():
                value = producer_status(producers[A])
                return value if value['session_id'] != before_owner else None
            successor = await observed_wait(replaced, untouched_guard, 'new exact native source after takeover', timeout=5)
            health = await call_rpc(broker._worker_socket(room_states[A]), 'health', {}, timeout=2)
            require(health['source']['owner']['session_id'] == successor['session_id'] and health['source']['ready']
                    and room_states[A].current_volume == 100, 'Retired native callback altered successor owner/volume')
            report['takeover'] = {'previous_session_id': before_owner, 'successor': successor,
                                  'continuity_scope': 'Explicit source-only cutover occurs after uninterrupted group/TTS interval'}
            captures[A] = Capture(f'hw:{card_a},0,7', clock, base_time, offset)
            captures[A].start()
            post_started = time.monotonic()
            successor_seen = False
            while time.monotonic()-post_started < 8:
                await untouched_guard()
                captures[A].poll()
                if len(captures[A].chunks) >= 20:
                    mono = np.frombuffer(b''.join(captures[A].chunks[-20:]), dtype='<i2').reshape(-1, 2)[:, 0].astype(float)
                    positions = np.arange(len(mono))/RATE
                    basis = np.column_stack((np.sin(2*np.pi*440*positions), np.cos(2*np.pi*440*positions),
                        np.sin(2*np.pi*660*positions), np.cos(2*np.pi*660*positions), np.ones(len(mono))))
                    fit, *_ = np.linalg.lstsq(basis, mono, rcond=None)
                    previous_amplitude, successor_amplitude = float(np.hypot(*fit[:2])), float(np.hypot(*fit[2:4]))
                    if successor_amplitude > 6000 and previous_amplitude < 8:
                        report['takeover']['actual_final_pcm'] = {'successor_660_amplitude': successor_amplitude,
                            'retired_440_amplitude': previous_amplitude, 'observed_frames': len(mono)}
                        successor_seen = True
                        break
                await asyncio.sleep(.03)
            require(successor_seen, 'New exact source never replaced retired440 with actual final660 PCM')
            health = await call_rpc(broker._worker_socket(room_states[A]), 'health', {}, timeout=2)
            require(health['source']['owner']['session_id'] == successor['session_id'] and health['source']['ready']
                    and room_states[A].current_volume == 100,
                    'A delayed retired callback altered the observed successor owner/volume')
            publish_command(producers[A]['command'], {'generation': 4, 'action': 'end'}, producers[A]['account'])
            async def idle():
                health = await call_rpc(broker._worker_socket(room_states[A]), 'health', {}, timeout=2)
                return health if health['source']['owner'] is None and health['source']['ready'] else None
            await observed_wait(idle, untouched_guard, 'exact A native END releases ownership', timeout=5)
            b_health = await call_rpc(broker._worker_socket(room_states[B]), 'health', {}, timeout=2)
            if b_health.get('speech_mix') == 'owntone_player':
                require(b_health['source']['owner']['session_id'] == producer_status(producers[B])['session_id'],
                        'A source retirement affected B owner')
                require_untouched_music_health(b_health, pcm_guards[B])
            else:
                require(b_health['source']['owner']['session_id'] == producer_status(producers[B])['session_id']
                        and b_health['music_gain'] == 1, 'A source retirement affected B owner or gain')
            await untouched_guard()
            report['end'] = {'a_idle': True, 'b_exact_owner_preserved': True,
                             'untouched_zone_pcm': pcm_guards[B].evidence()}
            report['retirement_captures'] = {key: capture.poll() for key, capture in captures.items()}
            complete = True
    except BaseException as exc:
        report['failure'] = {'type': type(exc).__name__,
                             'message': observation.redact_exception(exc, token, broker._password if broker else None),
                             'causes': failure_cause(exc)}
    finally:
        done.set()
        if monitor is not None:
            monitor.cancel()
            result = await asyncio.gather(monitor, return_exceptions=True)
            if result and isinstance(result[0], Exception) and not isinstance(result[0], asyncio.CancelledError):
                errors.append(f'control monitor: {observation.redact_exception(result[0], token)}')
        if fault_observer is None and epoch_context is not None:
            fault_observer = epoch_context.observer
        if fault_observer is not None:
            try:
                await fault_observer.close()
            except Exception as exc:
                errors.append(f'fault observer: {type(exc).__name__}')
        # Room teardown removes native-validation/status and generated logs.
        # Preserve exact historical invocations before stopping any producer;
        # evidence failures remain separate from the original PCM/test error.
        if not complete and broker is not None and temporary is not None:
            try:
                sources, rejected = failure_evidence.owned_sources(broker, room_states, producers,
                    epoch_id=epoch_context.phase.epoch_id if epoch_context is not None else None)
                private = (token, broker._password, *(broker._room_password(identifier) for identifier in ZONES))
                report['artifacts']['failure_diagnostics'] = await failure_evidence.capture(
                    temporary/'failure-evidence', sources, private=private, admission_errors=rejected)
            except BaseException as exc:
                report.setdefault('artifact_errors', {})['failure_diagnostics'] = type(exc).__name__
        if identity and api:
            with suppress(Exception):
                await api.request('POST', f'/api/v1/rooms/{A}/speech',
                    json={**identity, 'action': 'close', 'request_id': str(uuid4())})
        if tone:
            tone.stop()
        if peer:
            try:
                await asyncio.wait_for(peer.close(), 4)
                require(peer.connectionState == 'closed', 'Speech peer survived close')
                report['cleanup']['speech_peer_closed'] = True
            except Exception as exc:
                errors.append(f'speech peer: {type(exc).__name__}')
        for identifier, handle in producers.items():
            try:
                stopped_identity = deepcopy(handle['unit'].identity())
                if epoch_context is not None:
                    epoch_context.evidence.setdefault('producer_retirement', {})[identifier] = {
                        'identity': stopped_identity, 'requested_monotonic_ns': time.monotonic_ns()}
                await asyncio.wait_for(handle['unit'].stop(), 10)
                require(not handle['unit'].alive, 'Exact native producer unit survived cleanup')
                # A failed reconnect can leave this handle pointing at a retired
                # producer while the broker has already reserved a fresh native
                # receiver under the same canonical key. Compare after stop:
                # ownership may also change during its await.
                if broker.network.manifest['processes'].get(handle['key']) == stopped_identity:
                    broker.network.forget_process(handle['key'])
                state = room_states[identifier]
                if state.processes.get('shairport') is handle['unit']:
                    state.processes.pop('shairport')
                report['cleanup'][f'producer_{identifier}_stopped'] = True
                if epoch_context is not None:
                    epoch_context.evidence['producer_retirement'][identifier].update(
                        retired=True, verified_monotonic_ns=time.monotonic_ns())
            except Exception as exc:
                errors.append(f'producer cleanup: {type(exc).__name__}')
        for identifier, capture in captures.items():
            try:
                await asyncio.wait_for(asyncio.to_thread(capture.close), 3)
                require(capture.pipeline.get_state(0).state == capture.Gst.State.NULL, 'Final capture did not stop')
                report['cleanup'][f'capture_{identifier}_null'] = True
                if epoch_context is not None:
                    epoch.finalize_capture(epoch_context, identifier, capture)
            except Exception as exc:
                errors.append(f'capture cleanup: {type(exc).__name__}')
            if not complete and temporary is not None:
                try:
                    artifact = (epoch.retain_failed_capture(capture, temporary, identifier) if music_soak
                                else retain_failed_capture(capture, temporary, identifier))
                    report['artifacts'].setdefault('failure_final_pcm', {})[identifier] = artifact
                    if identifier in pcm_guards:
                        report.setdefault('per_buffer_evidence', {})[identifier] = pcm_guards[identifier].evidence()
                except Exception as exc:
                    report.setdefault('artifact_errors', {})[identifier] = type(exc).__name__
        if api and api_process and api_process.alive:
            try:
                if epoch_context is not None:
                    epoch_context.evidence['daemon_retirement'] = {'requested_monotonic_ns': time.monotonic_ns(),
                        'units': {identifier: {name: unit.identity() for name, unit in state.processes.items()}
                                  for identifier, state in room_states.items()}}
                for identifier in ZONES:
                    await api.patch(identifier, {'enabled': False})
                for room in (await api.request('GET', '/api/v1/state'))['rooms']:
                    await api.request('DELETE', f'/api/v1/rooms/{room["id"]}?expected_revision={room["revision"]}')
                require(not (await api.request('GET', '/api/v1/state'))['rooms'], 'Disposable rooms remain')
                report['cleanup']['disposable_rooms_deleted'] = True
            except Exception as exc:
                errors.append(f'API room cleanup: {observation.redact_exception(exc, token)}')
        if api_process:
            try:
                await api_process.stop()
                report['cleanup']['api_stopped'] = True
            except Exception as exc:
                errors.append(f'API stop: {type(exc).__name__}')
        if api:
            try:
                await api.close()
                report['cleanup']['api_client_closed'] = True
            except Exception as exc:
                errors.append(f'API client cleanup: {observation.redact_exception(exc, token)}')
        if broker:
            try:
                await asyncio.wait_for(broker.close(), 50)
                report['cleanup']['broker_closed'] = True
                require(not broker.sessions, 'Speech ownership remained after owned runtime shutdown')
                report['cleanup']['speech_ownership_released'] = True
            except Exception as exc:
                errors.append(f'broker cleanup: {observation.redact_exception(exc, token)}')
        if lan:
            try:
                await asyncio.wait_for(lan.close(), 20)
                report['cleanup']['isolated_lan_closed'] = True
            except Exception as exc:
                errors.append(f'isolated LAN cleanup: {type(exc).__name__}: {exc}')
        if baseline is not None:
            try:
                manifest = json.loads((STATE/'ownership.json').read_text())
                require(manifest['installation_id'] == report['installation_id'], 'Candidate installation identity changed during cleanup')
                require(not manifest['networks'] and not manifest['processes'], 'Owned units/networks remain')
                if report.get('native_lab') is not None:
                    require(native_lab_admission(manifest, report.get('mode', 'grouping'), original_netns_fd=original_netns_fd)
                            == report['native_lab'], 'Clean lab admission changed during cleanup')
                observation.base.closed_slot()
                require(await observation.base.host_snapshot() == baseline and legacy_snapshot() == legacy,
                        'Legacy PID/slot0/2 or host network baseline changed')
                report['cleanup'].update(empty_manifest=True, slot7_closed=True, legacy_and_host_preserved=True)
            except Exception as exc:
                errors.append(f'post-cleanup verification: {type(exc).__name__}')
        if type(original_netns_fd) is int and original_netns_fd >= 3:
            try:
                os.close(original_netns_fd)
            except OSError as exc:
                errors.append(f'original namespace descriptor cleanup: {type(exc).__name__}')
        report['cleanup_errors'] = errors
        report['passed'] = complete and not errors and all(report['cleanup'].values())
        if epoch_context is not None:
            epoch_context.evidence['passed'] = report['passed']
            epoch_context.evidence['measured_signal_timeline_cleanup_passed'] = report['passed']
            if report['passed']:
                epoch_context.evidence['daemon_retirement'].update(retired=True,
                    empty_ownership_verified=True, verified_monotonic_ns=time.monotonic_ns())
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        atomic_json(result_path, report)
        print(json.dumps({'passed': report['passed'], 'result': str(result_path), 'failure': report.get('failure'),
                          'cleanup_errors': errors}), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--producer')
    parser.add_argument('--parent-namespace')
    parser.add_argument('--original-netns-fd', type=int)
    parser.add_argument('--minimum-policy', action='store_true', help='Explicit actual production H140/B40 grouping/stress/fault coverage')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--speech-stress', action='store_true', help='Explicit20 audible sessions per zone; run via the stress supervisor')
    modes.add_argument('--zone-faults', action='store_true', help='Explicit exact-room backend failures; run via the fault supervisor')
    modes.add_argument('--music-minimum', action='store_true', help='Explicit cold MUSIC-only H140/B40 route candidate; separate supervisor')
    modes.add_argument('--music-soak', action='store_true', help='Explicit bounded thirty-minute digital music soak')
    parser.add_argument('--soak-buffer-ms', type=int)
    parser.add_argument('--soak-horizon-ms', type=int)
    modes.add_argument('--finite-speech', action='store_true', help='Explicit fresh finite Opus prefix/body/tail diagnostic; run via finite supervisor')
    parser.add_argument('--latency-epoch-id')
    parser.add_argument('--latency-offset-ms', type=int, choices=(-2000, 0, 2000))
    parser.add_argument('--latency-phase', choices=('idle', 'native'))
    parser.add_argument('--latency-result')
    args = parser.parse_args()
    selected_epoch = None
    if any(value is not None for value in (args.latency_epoch_id, args.latency_offset_ms, args.latency_phase, args.latency_result)):
        require(all(value is not None for value in (args.latency_epoch_id, args.latency_offset_ms, args.latency_phase, args.latency_result)),
                'Prepared epoch requires its complete explicit phase/result arguments')
        selected_epoch = load_epoch_module().Phase(args.latency_epoch_id, args.latency_offset_ms, args.latency_phase)
        selected_epoch.receipt()
    logging.basicConfig(level=logging.WARNING)
    async def supervised():
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        for name in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(name, task.cancel)
        return await run_check(args.parent_namespace, args.original_netns_fd,
                               speech_stress=args.speech_stress, zone_faults=args.zone_faults,
                               latency_epoch=selected_epoch, latency_result=args.latency_result, finite_speech=args.finite_speech, music_minimum=args.music_minimum,
                               music_soak=args.music_soak, soak_buffer_ms=args.soak_buffer_ms, soak_horizon_ms=args.soak_horizon_ms,
                               minimum_policy=args.minimum_policy)
    raise SystemExit(asyncio.run(producer(args.producer)) if args.producer else asyncio.run(supervised()))
