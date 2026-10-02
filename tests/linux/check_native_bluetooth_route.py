#!/usr/bin/env python3
"""Opt-in actual broker/framed/FD-only/BlueALSA SBC path on a private bus.

Run only through run_native_bluetooth_route.py and a400s whole-cgroup watchdog.
No adapter, host system bus, phone or physical output is used. A's decoded SBC
transport is observed before radio; B retains its actual original Loopback PCM.
"""
from __future__ import annotations

# Manual bounded proc/config/artifact observations between asynchronous edges.
# ruff: noqa: ASYNC240

import argparse
import asyncio
from contextlib import suppress
from copy import deepcopy
from datetime import datetime, timezone
import grp
import importlib.util
import json
import os
from pathlib import Path
import pwd
import signal
import sys
import tempfile
import time
from uuid import uuid4

import httpx
from aiortc import RTCSessionDescription

from shiri.rpc import call_rpc
from shiri.runtime.broker import Broker
from shiri.runtime.units import UnitManager
from shiri.runtime.system import RuntimeFailure, atomic_json, root_directory
from shiri.settings import Settings

HERE = Path(__file__).resolve()
def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE.with_name(filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

group = load('bluetooth_route_group', 'check_native_grouping.py')
route = load('bluetooth_route_observation', 'native_bluetooth_route.py')
faults = load('bluetooth_untouched_zone', 'native_zone_faults.py')
speech_status = load('bluetooth_speech_status', 'native_speech_diagnostics.py')
RESULT = Path('/tmp/shiri-v2-native-bluetooth-route-result.json')
if group.NATIVE_LAB is not None:
    RESULT = group.WORK/RESULT.name
A, B, ZONES = group.A, group.B, group.ZONES
require = route.require


class IsolatedBluetoothBroker(group.IsolatedBroker):
    """Observe startup failures before the unchanged production loop cleans up.

    This is diagnostic only: every production operation is awaited once and
    every exception is re-raised unchanged. Later retry failures cannot replace
    the first failure, including a failure after a successful PCM admission.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.startup_diagnostics = {'first_failure': None, 'attempts': [], 'omitted_attempts': 0}
        self._startup_current = {}
        self.startup_api_token = None

    def _startup_stage(self, identifier, stage):
        # Observations are armed only by _start_room. Pure unit-adapter tests
        # may construct an uninitialized instance without starting a broker.
        attempt = getattr(self, '_startup_current', {}).get(identifier)
        if attempt is not None:
            attempt['stage'] = stage
            if len(attempt['stages']) < 32:
                attempt['stages'].append({'stage': stage, 'monotonic_ns': time.monotonic_ns()})
            else:
                attempt['omitted_stages'] += 1

    async def _start_room(self, room):
        identifier = room.desired.id
        attempt = None
        if len(self.startup_diagnostics['attempts']) < 8:
            attempt = {'room_id': identifier, 'revision': room.desired.revision,
                       'started_monotonic_ns': time.monotonic_ns(), 'outcome': 'pending',
                       'stage': 'room_start', 'stages': [], 'omitted_stages': 0}
            self.startup_diagnostics['attempts'].append(attempt)
            self._startup_current[identifier] = attempt
        else:
            self.startup_diagnostics['omitted_attempts'] += 1
        try:
            result = await super()._start_room(room)
            if attempt is not None:
                attempt['outcome'] = 'returned'
            return result
        except BaseException as exc:
            if attempt is not None:
                message = group.observation.redact_exception(exc, self.startup_api_token,
                    self._password, self._room_password(identifier))
                attempt.update(outcome='raised', failure={
                    'observed_monotonic_ns': time.monotonic_ns(),
                    'type': type(exc).__name__[:80],
                    'message': route.diagnostics._text(message, 1024),
                    'causes': group.failure_cause(exc),
                    'captured_before_cleanup': True,
                    'bluetooth_admission_present': room.bluetooth_admission is not None,
                    'bluetooth_handoff_present': room.bluetooth_handoff is not None,
                    'started_roles': sorted(room.processes)[:16],
                    'own_control_client_present': room.client is not None,
                    'launch_generation': room.launch_generation,
                })
                if self.startup_diagnostics['first_failure'] is None:
                    self.startup_diagnostics['first_failure'] = deepcopy(attempt)
            raise
        finally:
            if attempt is not None:
                attempt['finished_monotonic_ns'] = time.monotonic_ns()
                if self._startup_current.get(identifier) is attempt:
                    self._startup_current.pop(identifier)

    async def _start_bluetooth(self, room, *args, **kwargs):
        self._startup_stage(room.desired.id, 'bluetooth_admission_and_bridge')
        result = await super()._start_bluetooth(room, *args, **kwargs)
        self._startup_stage(room.desired.id, 'bluetooth_bridge_ready')
        return result

    async def _wait_worker(self, room, name, socket):
        self._startup_stage(room.desired.id, 'worker_health:'+name)
        result = await super()._wait_worker(room, name, socket)
        self._startup_stage(room.desired.id, 'worker_ready:'+name)
        return result

    async def _remember_process(self, key, process):
        self._startup_stage(key.partition(':')[0], 'persist_process:'+process.name)
        return await super()._remember_process(key, process)

    async def _start_process(self, *args, namespace=None, **kwargs):
        role = args[1] if len(args) > 1 else kwargs.get('role')
        key = args[0] if args else kwargs.get('key', '')
        self._startup_stage(key.partition(':')[0], 'launch_process:'+str(role))
        if role == 'bluetooth-output':
            # The real production bridge profile forbids a network namespace.
            # Its AF_UNIX-only worker receives only admitted descriptors.
            require(namespace is None, 'Bluetooth worker must retain its production no-namespace profile')
            return await Broker._start_process(self, *args, namespace=None, **kwargs)
        return await super()._start_process(*args, namespace=namespace, **kwargs)


def endpoint_evidence(state, daemon, report):
    admission = state.bluetooth_admission
    require(admission is not None and admission.manager is daemon.manager and admission.worker_pid > 0,
            'Bluetooth output was not admitted by the actual private daemon boundary')
    endpoint = admission.endpoint
    bridge, output = state.processes['bluetooth-output'], state.processes['owntone']
    receipt = bridge.identity()
    # Preserve the original observation before any compatibility/authority
    # assertion; a rejection never promotes it to verified admission.
    report['bluetooth_endpoint_last_observed'] = deepcopy({
        'endpoint': admission.envelope()['endpoint'], 'bridge': receipt,
        'output': output.identity(), 'admitted_worker_pid': admission.worker_pid,
        'observed_bridge_pid': bridge.process.pid, 'authority_verified': False})
    require(endpoint.mac == route.private.MAC and endpoint.codec == 'SBC'
            and (endpoint.rate, endpoint.channels, endpoint.format_code) == (48000, 2, 0x8210)
            and endpoint.synchronous_drop is True and endpoint.restricted_controller is True,
            'Actual maintained daemon changed its admitted SBC/capability contract')
    properties = receipt['properties']
    require(type(receipt.get('namespace')) is str and receipt['namespace'] == ''
            and 'NetworkNamespacePath' not in properties,
            'Bridge must retain the canonical empty namespace and no namespace property')
    UnitManager.validate_saved(receipt)
    require(type(receipt['policy_version']) is int and receipt['policy_version'] == 5
            and properties.get('CapabilityBoundingSet') == '' and properties.get('AmbientCapabilities') == ''
            and properties.get('NoNewPrivileges') == 'yes' and properties.get('PrivateDevices') == 'yes'
            and properties.get('DevicePolicy') == 'strict'
            and properties.get('DeviceAllow') == '/dev/null rw /dev/zero rw /dev/random r /dev/urandom r'
            and properties['RestrictAddressFamilies'] == 'AF_UNIX'
            and not properties.get('SupplementaryGroups')
            and '/run/dbus/system_bus_socket' in properties['InaccessiblePaths']
            and '/dev/snd' not in properties['DeviceAllow']
            and not any(value.startswith(('DBUS_', 'ALSA_')) for value in properties.get('Environment', '').split())
            and admission.worker_pid == bridge.process.pid,
            'Bridge did not retain exact descriptor-only production authority')
    publication = receipt.get('socket_publication')
    require(isinstance(publication, dict), 'Published final-PCM socket lacks durable held-inode receipt')
    require(bridge.entry['user'] != output.entry['user'], 'Bridge and OwnTone must retain distinct static identities')
    report['bluetooth_endpoint_last_observed']['authority_verified'] = True
    return {'endpoint': admission.envelope()['endpoint'], 'bridge': receipt, 'output': output.identity(),
            'scope': 'Actual broker admission, FD handoff, durable publication and productionv5 bridge unit'}


async def stop_producers(broker, states, producers, cleanup):
    """A retired handle cannot erase a fresh canonical reservation after await."""
    errors = []
    for identifier, handle in producers.items():
        try:
            identity = deepcopy(handle['unit'].identity())
            await asyncio.wait_for(handle['unit'].stop(), 10)
            require(not handle['unit'].alive, 'Exact Bluetooth-route producer survived stop')
            if broker.network.manifest['processes'].get(handle['key']) == identity:
                broker.network.forget_process(handle['key'])
            if states[identifier].processes.get('shairport') is handle['unit']:
                states[identifier].processes.pop('shairport')
            cleanup[f'producer_{identifier}_stopped'] = True
        except Exception as exc:
            errors.append('producer: '+type(exc).__name__)
    return errors


async def finish_observers(monitor, done, capture, b_guard, a_guard, report):
    """Join controls, verify all buffered content, and always attempt NULL."""
    failure = None
    done.set()
    if not monitor.done():
        monitor.cancel()
    results = await asyncio.gather(monitor, return_exceptions=True)
    if results and isinstance(results[0], BaseException) and not isinstance(results[0], asyncio.CancelledError):
        failure = results[0]
    control_stopped = time.monotonic_ns()
    try:
        b_guard.check()
        a_guard.check()
    except BaseException as exc:
        failure = failure or exc
    stop_requested = time.monotonic_ns()
    try:
        await asyncio.wait_for(asyncio.to_thread(capture.close), 3)
        require(capture.pipeline.get_state(0).state == capture.Gst.State.NULL,
                'Original B capture did not prove NULL at its deliberate stop boundary')
        null_verified = time.monotonic_ns()
        report['original_b_stop_boundary'] = {'control_observer_joined_monotonic_ns': control_stopped,
                                              'requested_monotonic_ns': stop_requested,
                                              'null_verified_monotonic_ns': null_verified}
        # Stop permission excludes only callback age, after actual NULL proof.
        # Existing historical gaps and all queued content/caps/offsets remain.
        b_guard.check(live=False)
    except BaseException as exc:
        failure = failure or exc
    report['per_buffer_evidence'] = {'untouched': b_guard.evidence(), 'bluetooth': a_guard.evidence()}
    if failure is not None:
        raise failure


async def finish_private_bluetooth(daemon, decoded_guard, temporary, report, errors):
    """Stop exact producers/drain before final checks and complete retention."""
    require(daemon.manager is None or not daemon.manager.leases, 'Root exactMAC admission remains owned')
    report['cleanup']['bluetooth_lease_released'] = True
    # Capture the one existing admission failure and the actual bounded private
    # log before stopping its exact producers/bus or removing their directory.
    if temporary:
        try:
            report.setdefault('artifacts', {})['private_bluetooth_startup_diagnostics'] = route.diagnostics.retain(
                temporary, daemon)
        except Exception as exc:
            report.setdefault('artifact_errors', {})['private_bluetooth_startup_diagnostics'] = type(exc).__name__
            errors.append('private startup diagnostics: '+type(exc).__name__)
    closed = False
    try:
        await daemon.close(leases_released=True)
        report['cleanup']['private_daemon_closed'] = closed = True
    except BaseException as exc:
        report['cleanup']['private_daemon_closed'] = False
        errors.append('private daemon close: '+type(exc).__name__)
    if daemon.capture and temporary:
        try:
            daemon.capture.check(live=False)
            if decoded_guard is not None:
                decoded_guard.check(live=False)
        except BaseException as exc:
            errors.append('SBC observation: '+type(exc).__name__)
        finally:
            if decoded_guard is not None:
                report['per_buffer_evidence']['bluetooth'] = decoded_guard.evidence()
        try:
            report['artifacts']['a_sbc_transport'] = daemon.capture.retain(temporary)
        except BaseException as exc:
            report.setdefault('artifact_errors', {})['a_sbc_transport'] = type(exc).__name__
            errors.append('SBC retention: '+type(exc).__name__)
        report['sbc_capture'] = daemon.capture.evidence()
    if closed:
        # Delete this exact parent only after encoder/bus and transport retire.
        daemon.remove_directory()
        report['cleanup']['private_directory_removed'] = not daemon.directory.exists()


class NativeSourceGenerationGuard:
    """One-way admission from exact granted/pre-PCM startup to full PCM proof."""
    def __init__(self):
        self.sealed = set()

    def check(self, identifier, health, expected, phase, report, *, require_generation=False):
        # Keep only two latest, bounded identity/counter observations, before
        # rejecting. No producer command history, credentials or PCM payloads.
        source = health.get('source')
        owner = source.get('owner') if isinstance(source, dict) else None
        def bounded(value):
            if isinstance(value, str):
                return value[:512]
            if type(value) is int:
                return value if -(2**63) <= value < 2**63 else '<integer outside signed64range>'
            if type(value) is bool or value is None:
                return value
            return '<'+type(value).__name__[:80]+'>'
        observed = {
            'phase': phase, 'observed_monotonic_ns': time.monotonic_ns(),
            'generation_required': require_generation or identifier in self.sealed or phase != 'startup',
            'expected': {key: bounded(expected.get(key)) for key in
                         ('session_id', 'epoch', 'incarnation', 'generation', 'stage', 'frames')},
            'health': {key: bounded(health.get(key)) for key in
                       ('ready', 'error', 'native_generation', 'native_blocks', 'dropped_bytes', 'speech_dropped_frames')},
            'source_ready': bounded(source.get('ready')) if isinstance(source, dict) else None,
            'source_owner': {key: bounded(owner.get(key)) for key in
                             ('zone_id', 'session_id', 'epoch', 'incarnation', 'protocol')} if isinstance(owner, dict) else None,
            'accepted': False,
        }
        report.setdefault('native_source_last_observed', {})[identifier] = observed
        def expect(condition, message):
            if not condition:
                observed['failure'] = {'type': 'RuntimeFailure', 'message': message}
                report.setdefault('native_source_first_rejected', {}).setdefault(identifier, deepcopy(observed))
            require(condition, message)
        expect(isinstance(source, dict) and source.get('ready') is True and isinstance(owner, dict)
                and owner.get('zone_id') == identifier and owner.get('protocol') == 'airplay2'
                and all(type(owner.get(key)) is type(expected.get(key)) and owner.get(key) == expected.get(key)
                        for key in ('session_id', 'epoch', 'incarnation')),
                'Native source incarnation changed during TTS')
        blocks, generation = health.get('native_blocks'), health.get('native_generation')
        expect(type(blocks) is int and 0 <= blocks < 2**63 and type(expected.get('generation')) is int
                and 0 < expected['generation'] < 2**63, 'Native source PCM generation evidence is malformed')
        if blocks or generation is not None or require_generation or phase != 'startup':
            self.sealed.add(identifier)
        observed['generation_required'] = identifier in self.sealed
        if generation is None:
            expect(not require_generation and phase == 'startup' and identifier not in self.sealed
                    and blocks == 0 and expected.get('stage') == 'granted'
                    and expected['generation'] == 1
                    and type(expected.get('frames')) is int and expected['frames'] == 0,
                    'Native source lacks its required accepted-PCM generation')
            observed['scope'] = 'exact_granted_source_before_first_pcm_only'
        else:
            expect(type(generation) is int and generation == expected['generation'] and blocks > 0,
                    'Native source incarnation changed during TTS')
            observed['scope'] = 'exact_accepted_pcm_generation'
        observed['accepted'] = True


class EndedTransportSilence:
    """Measure idle final PCM/RTP without requiring the selected socket to close."""
    def __init__(self, before_flush, receiver):
        require(type(before_flush) is int and 0 <= before_flush < 2**63 and isinstance(receiver, dict),
                'END idle proof lacks its original flush/receiver observation')
        self.before_flush = before_flush
        self.receiver = deepcopy(receiver)
        self.previous = self.stable = None
        self.since = None
        self.last_observed_at = None

    def observe(self, bridge, health, receiver, packets, chunks, now):
        source, ended = health.get('source'), receiver.get('native_end_idle')
        require(health.get('ready') is True and not health.get('error')
                and isinstance(source, dict) and source.get('ready') is True and source.get('owner') is None
                and receiver.get('stage') == 'ended_idle' and receiver.get('finished') is False
                and not receiver.get('error') and isinstance(ended, dict)
                and type(ended.get('generation')) is int and ended['generation'] == 4
                and all(ended.get(key) is True for key in ('source_retired', 'descriptor_closed', 'receiver_unit_held'))
                and ended == self.receiver.get('native_end_idle')
                and all(type(receiver.get(key)) is type(self.receiver.get(key))
                        and receiver.get(key) == self.receiver.get(key)
                        for key in ('session_id', 'epoch', 'incarnation', 'generation')),
                'END idle proof lost its exact retired source or held receiver')
        require(bridge.get('ready') is True and not bridge.get('error')
                and bridge.get('peer_authorized') is True and type(bridge.get('active')) is bool
                and bridge.get('uses_system_bus') is False and bridge.get('uses_alsa_devices') is False,
                'END idle proof lost its exact descriptor-only worker')
        counter_keys = ('flushes', 'frames_forwarded', 'frames_discarded', 'queue_bytes')
        require(all(type(bridge.get(key)) is int and 0 <= bridge[key] < 2**63 for key in counter_keys)
                and type(packets) is int and 0 < packets < route.MAX_PACKETS
                and type(chunks) is int and 0 < chunks < route.MAX_PACKETS,
                'END idle proof counters are malformed')
        output = bridge.get('last_output_at')
        require(type(now) in {int, float} and 0 < now < 2**63
                and type(output) in {int, float} and 0 < output <= now,
                'END idle proof output clock is malformed or in the future')
        require(self.last_observed_at is None or now >= self.last_observed_at,
                'END idle proof observation clock moved backwards')
        self.last_observed_at = now
        observed = (bridge['flushes'], bridge['frames_forwarded'], bridge['frames_discarded'], output, packets, chunks)
        if self.previous is not None:
            require(all(current >= previous for current, previous in zip(observed, self.previous, strict=True)),
                    'END idle proof counters or output clock moved backwards')
        self.previous = observed
        if bridge['flushes'] <= self.before_flush or bridge['queue_bytes'] != 0:
            self.stable = self.since = None
            return None
        if self.stable != observed:
            self.stable, self.since = observed, now
            return None
        require(now >= self.since, 'END idle proof observation clock moved backwards')
        elapsed = now-self.since
        if elapsed < .5:
            return None
        return {'kind': 'actual_framed_session_idle_after_completed_drop' if bridge['active']
                        else 'actual_framed_session_ended_after_completed_drop',
                'last_packet': packets-1, 'first_block': chunks,
                'no_new_transport_seconds': elapsed, 'unchanged_frames_forwarded': bridge['frames_forwarded'],
                'unchanged_frames_discarded': bridge['frames_discarded'], 'completed_flushes': bridge['flushes'],
                'queue_bytes': 0, 'unchanged_last_output_at': output, 'socket_retained': bridge['active'],
                'receiver_end': deepcopy(ended), 'source_owner_idle': True,
                'scope': 'Unchanged final PCM counters and decoded/RTP callback counts over a measured interval'}


async def exercise(api, api_process, broker, daemon, states, capture, producers, report, *, frozen_timing):
    """Own one uninterrupted B observer through music, speech and A retirement."""
    done = asyncio.Event()
    control_error = None
    phase = 'startup'
    expected_units = {key: {name: proc.identity() for name, proc in state.processes.items()}
                      for key, state in states.items()}
    players, anchors, controls = {}, {}, []
    b_guard = group.FinalPcmGuard(capture)
    a_guard = route.DecodedGuard(daemon.capture)
    peer = tone = speech = None
    primary = None
    initial_flushes = None
    a_native_expected = None
    volume_expected = 100
    pending_volume = None
    report['transitions'] = {}
    generation_guard = NativeSourceGenerationGuard()

    async def diagnostic_health(state, timeout):
        return await call_rpc(broker._worker_socket(state), 'health', {}, timeout=timeout)

    speech_diagnostics = speech_status.SpeechDiagnostics(states, diagnostic_health, report)

    async def healthy(*, require_generation=False):
        nonlocal initial_flushes
        daemon.healthy()
        a_guard.check()
        b_guard.check()
        require(api_process.alive, 'Rootless Bluetooth-route API exited')
        for identifier, state in states.items():
            require({name: proc.identity() for name, proc in state.processes.items()} == expected_units[identifier]
                    and all(proc.alive for proc in state.processes.values()), 'A Bluetooth-route daemon identity changed')
            actual = await state.client.outputs(set())
            require(state.selected_ids == ['0'] and [value['id'] for value in actual if value['selected']] == ['0'],
                    'Actual selected output changed or a house output became selected')
            expected = group.producer_status(producers[identifier])
            health = await call_rpc(broker._worker_socket(state), 'health', {}, timeout=2)
            group.load_minimum_coverage_module().require_room_timing(frozen_timing, identifier, state.desired, health)
            # Capture/check the exact source evidence before the broader worker
            # health assertion, so startup generation failures stay attributable.
            if identifier == B or phase not in {'takeover', 'end'}:
                generation_guard.check(identifier, health, expected, phase, report,
                                       require_generation=require_generation)
            if identifier == A and a_native_expected is not None:
                generation_guard.check(identifier, health, a_native_expected, phase, report,
                                       require_generation=True)
            require(health['ready'] and not health.get('error') and health['dropped_bytes'] == 0
                    and health['speech_dropped_frames'] == 0, 'Actual native worker failed or dropped PCM')
            owner = health['source']['owner']
            player = await state.client.request('GET', '/api/player')
            if identifier in players and (identifier == B or phase not in {'takeover', 'end'}):
                require(player['state'] == 'play' and player['item_id'] == players[identifier]['item_id'],
                        'OwnTone music paused, sought or changed item during speech')
                value, now = player.get('item_progress_ms'), time.monotonic()
                require(type(value) is int and value >= 0, 'Actual NPT progress is missing')
                if identifier not in anchors:
                    anchors[identifier] = {'value': value, 'at': now, 'last': value, 'last_at': now}
                anchor = anchors[identifier]
                require(value >= anchor['last'] and abs((value-anchor['value'])-(now-anchor['at'])*1000) < 1500,
                        'Original music progress no longer follows its cumulative clock')
                if value > anchor['last']:
                    anchor['last'], anchor['last_at'] = value, now
                require(now-anchor['last_at'] < 2, 'Original music NPT stopped advancing')
            wanted = pending_volume if identifier == A and pending_volume is not None else {volume_expected if identifier == A else 100}
            require(player.get('volume') in wanted and state.current_volume in wanted,
                    'Room volume changed outside its explicitly tested mutation')
            controls.append({'at': time.monotonic(), 'phase': phase, 'room_id': identifier,
                             'progress_ms': player.get('item_progress_ms'), 'music_gain': health['music_gain'],
                             'speech_session_id': health['speech_session_id'], 'source_owner': owner})
            require(len(controls) < route.MAX_CONTROLS, 'Control receipt count exceeded its bound')
        bridge = await broker._worker_rpc(states[A], 'bluetooth-output', 'health', {}, timeout=2)
        require(bridge['ready'] and not bridge['error'] and bridge['peer_authorized']
                and (bridge['active'] or phase in {'startup', 'end'})
                and bridge['uses_system_bus'] is False and bridge['uses_alsa_devices'] is False,
                'Descriptor-only final output failed or changed its authority')
        report['bridge_last_observed'] = bridge
        if initial_flushes is None and phase != 'startup':
            initial_flushes = bridge['flushes']
        if initial_flushes is not None and phase not in {'takeover', 'end'}:
            require(bridge['flushes'] == initial_flushes, 'TTS or volume change flushed/reconnected the music output')

    async def watch():
        nonlocal control_error
        try:
            while not done.is_set():
                await healthy()
                await asyncio.sleep(.06)
        except BaseException as exc:
            control_error = exc
            await speech_diagnostics.failure(exc, phase)
            raise

    monitor = asyncio.create_task(watch(), name='private-bluetooth-route-original-control')
    async def guard():
        if control_error is not None:
            raise control_error
        require(not monitor.done(), 'Original control observer ended unexpectedly')
        daemon.healthy()
        a_guard.check()
        b_guard.check()

    async def wait(predicate, description, seconds=8):
        return await group.observed_wait(predicate, guard, description, timeout=seconds)

    async def stage(label, seconds=.8):
        nonlocal phase
        phase = label
        first = len(daemon.capture.chunks)
        end = time.monotonic()+seconds
        while time.monotonic() < end:
            await guard()
            await asyncio.sleep(.02)
        require(len(daemon.capture.chunks) > first, 'Actual SBC stage has no decoded data')
        measured = route.fit_tones(b''.join(daemon.capture.chunks[first:]))
        report.setdefault('stages', {})[label] = measured
        return measured

    async def transition(label, ratio, voice, baseline, boundary, previous_voice=0):
        nonlocal phase
        phase = label
        first = boundary['first_block']
        trigger = boundary['trigger_monotonic_ns']/1e9
        gate = route.Plateau(baseline['amplitude_440'], ratio, voice=voice,
                             voice_floor=baseline['amplitude_880'], previous_voice=previous_voice,
                             first_block=first, previous_ratio=boundary['previous_ratio'],
                             previous_voice_active=boundary['previous_voice'])
        async def observed():
            nonlocal first
            while first < len(daemon.capture.chunks):
                result = gate.push(daemon.capture.chunks[first], daemon.capture.at[first])
                first += 1
                if result:
                    a_guard.qualify(440, baseline['amplitude_440'], ratio, voice,
                                    gate.qualifying_first, label)
                    return True
            return None
        try:
            await wait(observed, 'actual decodedSBC '+label, 8)
        finally:
            report['transitions'][label] = {**gate.evidence(), 'trigger_monotonic_ns': round(trigger*1e9),
                'observed_callback_seconds': time.monotonic()-trigger,
                'latency_scope': 'Actual RTP receive/decode callbacks; includes software path, not device/air latency'}

    try:
        async def onset():
            capture.poll()
            b_index = group.music_onset_index(capture)
            if b_index is not None and b_guard.index is None:
                b_guard.begin(b_index)
            if len(daemon.capture.chunks) < 5 or b_guard.index is None:
                return None
            measured = route.fit_tones(b''.join(daemon.capture.chunks[-5:]))
            if measured['amplitude_440'] > 1000:
                await healthy(require_generation=True)
                return measured
            return None
        initial = await wait(onset, 'actual privateSBC and originalB music onset', 20)
        a_guard.arm(440, initial['amplitude_440'])
        players.update({key: await state.client.request('GET', '/api/player') for key, state in states.items()})
        require(all(player['state'] == 'play' for player in players.values()), 'Actual source programs are not playing')
        report['initial_players'] = deepcopy(players)
        # Keep B's original coded program. Constant carrier starts only at its
        # existing declared20s point, not a fixture-specific stimulus change.
        until = report['common_program_start_monotonic_ns']/1e9+frozen_timing.horizon_ns/1e9+group.CODE_SECONDS+1
        while time.monotonic() < until:
            await guard()
            await asyncio.sleep(.03)
        baseline_first = len(daemon.capture.chunks)
        await stage('baseline')
        baseline = route.pure_baseline(daemon.capture.chunks[baseline_first:])
        require(baseline['amplitude_440'] > 1000, 'Decoded SBC music baseline is not independently audible')
        a_guard.qualify(440, baseline['amplitude_440'], 1., False, baseline_first, 'baseline')
        b_reference = group.observation.spectrum(capture.chunks[-50:], group.RATE)
        b_guard.preserve_reference(b_reference)
        report['codec_baseline'] = baseline
        report['untouched_baseline'] = b_reference
        # Live final cubic volume applies without changing native input gain.
        for wanted, ratio, label in ((50, .125, 'final_volume50'), (100, 1., 'final_volume100')):
            pending_volume = {volume_expected, wanted}
            phase = 'volume_transition'
            boundary = a_guard.begin_transition()
            await api.patch(A, {'volume': wanted})
            volume_expected, pending_volume = wanted, None
            await transition(label, ratio, False, baseline, boundary)
        await speech_diagnostics.capture('before_tts')
        peer, tone = group.observation.base.make_speech_peer()
        speech = {'session_id': str(uuid4()), 'request_id': str(uuid4())}
        await asyncio.wait_for(peer.setLocalDescription(await peer.createOffer()), 8)
        boundary = a_guard.begin_transition()
        answer = await api.request('POST', f'/api/v1/nobly/rooms/{ZONES[A]["nobly"]}/speech',
            json={**speech, 'action': 'offer', 'sdp': peer.localDescription.sdp, 'type': 'offer'})
        require(answer['admitted_room_id'] == A, 'Nobly speech admitted to the wrong exact zone')
        await asyncio.wait_for(peer.setRemoteDescription(RTCSessionDescription(sdp=answer['sdp'], type=answer['type'])), 4)
        await api.request('POST', f'/api/v1/rooms/{B}/speech',
                          json={**speech, 'action': 'close', 'request_id': str(uuid4())}, expected=409)
        await transition('duck_and_voice', .2, True, baseline, boundary)
        voice = (await stage('speech_active'))['amplitude_880']
        await speech_diagnostics.capture('during_tts')
        boundary = a_guard.begin_transition()
        tone.silent = True
        await transition('silent_restore', 1., False, baseline, boundary, voice)
        require(peer.connectionState == 'connected', 'Idle Opus speech peer disconnected')
        boundary = a_guard.begin_transition()
        tone.silent = False
        await transition('resumed_voice', .2, True, baseline, boundary)
        boundary = a_guard.begin_transition()
        await api.request('POST', f'/api/v1/rooms/{A}/speech',
                          json={**speech, 'action': 'close', 'request_id': str(uuid4())})
        await transition('closed_restore', 1., False, baseline, boundary, voice)
        await speech_diagnostics.capture('after_tts_restore')
        tone.stop()
        await asyncio.wait_for(peer.close(), 4)
        require(peer.connectionState == 'closed' and speech['session_id'] not in broker.sessions,
                'Closed exact speech session still owns resources')
        report['speech'] = {'admitted_room_id': A, 'passed': True, 'flushes_before': initial_flushes,
                           'flushes_after': report['bridge_last_observed']['flushes']}
        phase = 'takeover'
        before = group.producer_status(producers[A])['session_id']
        takeover_boundary = a_guard.begin_transition(clear_carrier=True)
        report['takeover_boundary'] = takeover_boundary
        daemon.capture.begin_barrier('takeover')
        command_at = time.monotonic()
        group.publish_command(producers[A]['command'], {'generation': 3, 'action': 'takeover'}, producers[A]['account'])
        async def changed():
            state = group.producer_status(producers[A])
            return state if state['session_id'] != before else None
        successor = await wait(changed, 'actual successor source grant', 5)
        a_native_expected = deepcopy(successor)
        index = len(daemon.capture.chunks)
        observed = None
        async def successor_pcm():
            nonlocal index, observed
            if len(daemon.capture.chunks)-index < 20:
                return None
            observed = route.fit_tones(b''.join(daemon.capture.chunks[-20:]))
            # Same known source level after intentional carrier replacement;
            # each block is replayed below so averaging cannot hide old440.
            ratio = observed['amplitude_660']/baseline['amplitude_440']
            minimum = observed['minimum_channel_amplitude_660']/baseline['amplitude_440']
            return observed if .95 < minimum <= ratio < 1.05 and observed['amplitude_440'] <= route.CODEC_TONE_FLOOR else None
        await wait(successor_pcm, 'decodedSBC successor replaces retired carrier', 8)
        daemon.capture.complete_barrier()
        a_guard.qualify(660, baseline['amplitude_440'], 1., False,
                        len(daemon.capture.chunks)-20, 'takeover_successor')
        report['takeover'] = {'passed': True, 'previous_session_id': before, 'successor': successor,
                             'spectrum': observed, 'callback_seconds_after_command': time.monotonic()-command_at,
                             'scope': 'Intentional source-only cutover; already transmitted packets are not revoked'}
        require(report['bridge_last_observed']['flushes'] > initial_flushes, 'Actual source takeover did not complete BlueALSA DropSync')
        phase, a_native_expected = 'end', None
        report['end_boundary'] = a_guard.begin_transition(clear_carrier=True)
        daemon.capture.begin_barrier('end')
        before_end = report['bridge_last_observed']['flushes']
        group.publish_command(producers[A]['command'], {'generation': 4, 'action': 'end'}, producers[A]['account'])
        async def idle():
            health = await call_rpc(broker._worker_socket(states[A]), 'health', {}, timeout=2)
            receiver = group.producer_status(producers[A])
            ended = receiver.get('native_end_idle', {})
            return health if (health['source']['owner'] is None and health['source']['ready']
                and receiver.get('stage') == 'ended_idle' and receiver.get('finished') is False
                and ended.get('generation') == 4 and ended.get('source_retired') is True
                and ended.get('descriptor_closed') is True and ended.get('receiver_unit_held') is True) else None
        await wait(idle, 'actual A END releases native source', 5)
        end_idle = EndedTransportSilence(before_end, group.producer_status(producers[A]))
        async def retired():
            health = await call_rpc(broker._worker_socket(states[A]), 'health', {}, timeout=2)
            receiver = group.producer_status(producers[A])
            bridge = await broker._worker_rpc(states[A], 'bluetooth-output', 'health', {}, timeout=2)
            require(bridge['ready'] and not bridge['error'], 'END faulted the descriptor worker')
            silent = end_idle.observe(bridge, health, receiver, len(daemon.capture.records),
                                      len(daemon.capture.chunks), time.monotonic())
            if bridge['flushes'] <= before_end:
                return None
            measured = route.fit_tones(b''.join(daemon.capture.chunks[-20:]))
            quiet_first = len(daemon.capture.chunks)-20
            quiet = [route.fit_tones(data) for data in daemon.capture.chunks[quiet_first:]]
            if all(item['rms'] < baseline['amplitude_440']*.01 and item['dc'] <= route.CODEC_TONE_FLOOR
                   and max(item[f'amplitude_{frequency}'] for frequency in (440, 660, 880, 1320))
                   <= route.CODEC_TONE_FLOOR for item in quiet):
                return {'kind': 'decoded_carrier_retired', 'spectrum': measured, 'first_block': quiet_first}
            return silent
        end_spectrum = await wait(retired, 'actual decoded endpoint retires old A program', 8)
        await healthy()
        require(report['bridge_last_observed']['flushes'] > before_end, 'Actual END did not complete selected PCM DropSync')
        report['end'] = {'passed': True, 'a_owner_idle': True, 'retirement': end_spectrum,
                         'receiver_idle': deepcopy(group.producer_status(producers[A])['native_end_idle']),
                         'scope': 'Selected pipe/codec and observed future transport retire; no remote-radio/speaker queue claim'}
        a_guard.retire(baseline['amplitude_440']*.01, first_block=end_spectrum['first_block'])
        report['original_progress'] = deepcopy(anchors)
        require(all(anchor['last'] > anchor['value']+10000 for anchor in anchors.values()),
                'Music NPT did not advance through speech')
    except BaseException as exc:
        primary = exc
        await speech_diagnostics.failure(exc, phase)
        raise
    finally:
        if tone:
            tone.stop()
        if peer:
            try:
                await asyncio.wait_for(peer.close(), 4)
            except BaseException as exc:
                report.setdefault('observation_cleanup_errors', []).append('speech-peer: '+type(exc).__name__)
                primary = primary or exc
        try:
            await finish_observers(monitor, done, capture, b_guard, a_guard, report)
        except BaseException as exc:
            report.setdefault('observation_cleanup_errors', []).append('final-original-B: '+type(exc).__name__)
            primary = primary or exc
        report['control_samples'] = controls
        if primary is not None:
            raise primary
    return a_guard


async def run_check(binary, digest, parent_namespace, original_netns_fd):
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'passed': False, 'cleanup': {},
        'scope': 'Actual authenticatedAPI/broker/OwnTone framed output/FD-only bridge/maintained BlueALSA/SBC/AV decode, pre-radio',
        'physical_speakers_verified': False, 'native_phone_grouping_verified': False, 'cast_input_verified': False,
        'radio_delivery_verified': False, 'acoustic_sync_verified': False,
        'limits': {'phase_seconds': route.PHASE_SECONDS, 'producer_seconds': route.PRODUCER_SECONDS,
                   'raw_rtp_bytes': route.MAX_RTP_BYTES, 'decoded_bytes': route.MAX_PCM_BYTES,
                   'original_b_bytes': faults.CAPTURE_BYTES, 'private_daemon_log_bytes': route.MAX_LOG_BYTES,
                   'packet_records': route.MAX_PACKETS, 'control_records': route.MAX_CONTROLS}}
    broker = api = api_process = daemon = lan = capture = decoded_guard = None
    token = temporary = baseline = legacy = None
    states, producers, errors = {}, {}, []
    completed = False
    try:
        require(sys.platform == 'linux' and os.geteuid() == 0
                and os.environ.get('SHIRI_PRIVATE_BLUETOOTH_ROUTE_TEST') == '1', 'Explicit Linux root private-route opt-in is required')
        require(type(original_netns_fd) is int and original_netns_fd >= 3, 'Run through the isolated supervisor')
        manifest = json.loads((group.STATE/'ownership.json').read_text())
        if group.NATIVE_LAB is not None:
            report['native_lab'] = group.native_lab_admission(manifest, 'bluetooth_route', original_netns_fd=original_netns_fd)
        else:
            require(manifest['installation_id'].startswith('b265') and not manifest['networks'] and not manifest['processes'],
                    'Known candidate must be idle and exact; no concurrent fixture')
        route.private.validated_binary(binary, digest)
        group.observation.base.closed_slot()
        legacy, baseline = group.legacy_snapshot(), await group.observation.base.host_snapshot()
        account, account_group = pwd.getpwnam('shiri'), grp.getgrnam('shiri')
        root_directory(group.WORK, mode=0o755)
        temporary = Path(tempfile.mkdtemp(prefix='native-bluetooth-', dir=group.WORK))
        os.chown(temporary, 0, account_group.gr_gid)
        temporary.chmod(0o750)
        report['artifacts'] = {'private_directory': str(temporary)}
        # The daemon can traverse its private sibling under /tmp; the rootless
        # API and descriptor workers never receive its path or bus.
        daemon = route.PrivateDaemon(Path('/tmp')/f'shiri-private-bluealsa-route-{uuid4().hex}', binary, digest)
        manager = await daemon.start()
        report['private_daemon'] = daemon.receipt
        lan = group.isolated_lan.IsolatedLan(temporary/'isolated-lan')
        await lan.start(original_netns_fd=original_netns_fd, parent_namespace=parent_namespace)
        report['network_fixture'] = lan.evidence()
        settings = Settings(state_dir=temporary/'api', api_token_file=temporary/'token', runtime_state_dir=group.STATE,
            runtime_dir=group.RUN, runtime_socket=group.RUN/'runtime.sock', binary_dir=group.BINARIES,
            daemon_identity_file=group.IDENTITIES)
        api_process, token = await group.launch_api(temporary, settings, account, account_group, interface=lan.interface)
        broker = IsolatedBluetoothBroker(settings, parent_namespace)
        broker.startup_api_token = token
        report['broker_startup'] = broker.startup_diagnostics
        daemon.broker_startup_diagnostics = broker.startup_diagnostics
        broker.bluealsa = manager  # Sole codec/control dependency boundary.
        health = await broker.start(serve=True)
        require(health['ready'], health.get('error') or 'Actual broker preflight failed')
        require(broker.network.installation_id == manifest['installation_id'],
                'Actual broker candidate installation identity changed during startup')
        report.update(installation_id=manifest['installation_id'], versions=health['versions'])
        api = group.Api()
        async def api_ready():
            require(api_process.alive, 'Actual rootless API exited')
            try:
                return (await api.client.get('/api/v1/health/live')).status_code == 200
            except httpx.HTTPError:
                return False
        await group.observation.base.eventually(api_ready, 'rootless private-routeAPI', timeout=20)
        await api.request('GET', '/api/v1/state', expected=401)
        await api.request('POST', '/api/v1/session', json={'token': token})
        report['rootless_api'] = api_process.evidence()
        async def stopped():
            rooms = (await api.request('GET', '/api/v1/state'))['rooms']
            return len(rooms) == 8 and all(not room['enabled'] and room['runtime']['status'] == 'stopped' for room in rooms)
        await group.observation.base.eventually(stopped, 'all disposable rooms initially disabled', timeout=8)
        import gi
        gi.require_version('Gst', '1.0')
        from gi.repository import Gst
        Gst.init(None)
        clock = Gst.SystemClock.obtain()
        require(clock.get_property('clock-type').value_nick == 'monotonic', 'Actual B capture clock must be monotonic')
        before, sampled, after = time.monotonic_ns(), clock.get_time(), time.monotonic_ns()
        require(after-before <= 1_000_000, 'B capture clock mapping bracket exceeds1ms')
        clock_offset, base_time = sampled-(before+(after-before)//2), clock.get_time()
        binding = await group.enrollment(api, ZONES[B]['device'])
        root_directory(temporary/'capture-baseline-B')
        report['independent_b_capture_baseline'] = await group.calibrate_capture(broker, B, binding,
            temporary/'capture-baseline-B', clock, base_time, clock_offset)
        for identifier, device in ((A, route.DEVICE), (B, binding)):
            await api.patch(identifier, {'local_audio_device': device, 'volume': 100, 'duck_gain': .2, 'enabled': True})
            async def ready(identifier=identifier):
                room = await api.room(identifier)
                require(room['runtime']['status'] not in {'error', 'degraded'}, room['runtime'].get('error') or 'Room failed')
                return room if room['runtime']['status'] == 'running' else None
            await group.observation.base.eventually(ready, 'actual enabled private-route zone', timeout=65)
            states[identifier] = broker.rooms[identifier]
            room = await api.room(identifier)
            await api.request('PUT', f'/api/v1/rooms/{identifier}/speakers',
                              json={'expected_revision': room['revision'], 'speaker_ids': ['0']})
            async def selected(identifier=identifier):
                outputs = await states[identifier].client.outputs(set())
                room = await api.room(identifier)
                return ([speaker['id'] for speaker in room['speakers']] == ['0']
                        and states[identifier].selected_ids == ['0']
                        and [output['id'] for output in outputs if output['selected']] == ['0'])
            await group.observation.base.eventually(selected, 'actual exactlocal0 selection', timeout=12)
        report['bluetooth_admission'] = endpoint_evidence(states[A], daemon, report)
        frozen_timing = group.load_minimum_coverage_module().freeze(
            [state.desired for state in broker.rooms.values()],
            {identifier: await call_rpc(broker._worker_socket(state), 'health', {}, timeout=2)
             for identifier, state in states.items()})
        report['minimum_policy'] = True
        report['frozen_worker_timing'] = frozen_timing.receipt()
        require(states[A].local_pin is None and states[B].local_pin is not None,
                'Bluetooth route must not reserve a shared ALSA node')
        # Admission conflict is checked through the real manager while its
        # exact descriptor-backed worker remains alive; no alternate output.
        try:
            extra = await manager.admit(route.DEVICE, B, uuid4().hex)
        except RuntimeFailure:
            report['same_mac_lease_denied'] = True
        else:
            await extra.close()
            raise RuntimeFailure('Same exactMAC was leased to a second room')
        broker._monitor.cancel()
        await asyncio.gather(broker._monitor, return_exceptions=True)
        try:
            for identifier, state in states.items():
                root = state.directory/'native-bluetooth-validation'
                root_directory(root)
                producers[identifier] = await group.launch_producer(broker, state, root, 0,
                                                                    duration_seconds=route.PRODUCER_SECONDS,
                                                                    bluetooth_receiver_idle=identifier == A)
        finally:
            broker._monitor = asyncio.create_task(broker._health_monitor(), name='private-bt-production-health')
        async def granted():
            values = {key: group.producer_status(handle) for key, handle in producers.items()}
            return values if all(value and value['stage'] in {'granted', 'streaming'} for value in values.values()) else None
        report['producer_grants'] = await group.observation.base.eventually(granted, 'exact receiverUID grants', timeout=8)
        pin = states[B].local_pin
        pin.validate()
        capture = faults.capture_type(group.Capture)(f'hw:{pin.manifest["card_index"]},1,7', clock, base_time, clock_offset)
        capture.start()
        common_start = time.monotonic_ns()+4_000_000_000
        report['common_program_start_monotonic_ns'] = common_start
        for handle in producers.values():
            group.publish_command(handle['command'], {'generation': 2, 'action': 'run', 'common_start_ns': common_start}, handle['account'])
        decoded_guard = await asyncio.wait_for(
            exercise(api, api_process, broker, daemon, states, capture, producers, report, frozen_timing=frozen_timing), route.PHASE_SECONDS)
        report['producer_final'] = {key: group.producer_status(value) for key, value in producers.items()}
        completed = True
    except BaseException as exc:
        report['failure'] = {'type': type(exc).__name__, 'message': group.observation.redact_exception(exc, token,
            broker._password if broker else None), 'causes': group.failure_cause(exc)}
    finally:
        async def cleanup(label, operation, seconds=10):
            try:
                await asyncio.wait_for(operation, seconds)
                report['cleanup'][label] = True
            except BaseException as exc:
                report['cleanup'][label] = False
                errors.append(label+': '+type(exc).__name__)
        if not completed and broker and temporary:
            try:
                sources, rejected = group.failure_evidence.owned_sources(broker, states, producers)
                report['artifacts']['failure_diagnostics'] = await group.failure_evidence.capture(temporary/'failure-evidence',
                    sources, private=(token, broker._password, *(broker._room_password(key) for key in ZONES)),
                    admission_errors=rejected)
            except BaseException as exc:
                report.setdefault('artifact_errors', {})['failure_diagnostics'] = type(exc).__name__
        if broker:
            errors.extend(await stop_producers(broker, states, producers, report['cleanup']))
        if capture:
            try:
                # NULL is attempted even when an earlier framing/content
                # observation has already failed permanently.
                await asyncio.wait_for(asyncio.to_thread(capture.close), 3)
                require(capture.pipeline.get_state(0).state == capture.Gst.State.NULL, 'Original B observer did not prove NULL')
                capture.poll()
                report['cleanup']['b_capture_null'] = True
                if temporary:
                    report['artifacts']['b_final_pcm'] = group.retain_failed_capture(capture, temporary, B)
            except Exception as exc:
                errors.append('B capture: '+type(exc).__name__)
        if api and api_process and api_process.alive:
            async def delete_rooms():
                for identifier in ZONES:
                    await api.patch(identifier, {'enabled': False})
                for room in (await api.request('GET', '/api/v1/state'))['rooms']:
                    await api.request('DELETE', f'/api/v1/rooms/{room["id"]}?expected_revision={room["revision"]}')
                require(not (await api.request('GET', '/api/v1/state'))['rooms'], 'Disposable API rooms remain')
            await cleanup('disposable_rooms_deleted', delete_rooms(), 30)
        if api_process:
            await cleanup('api_stopped', api_process.stop())
        if api:
            await cleanup('api_client_closed', api.close())
        if broker:
            await cleanup('broker_closed', broker.close(), 50)
        if daemon:
            try:
                await finish_private_bluetooth(daemon, decoded_guard, temporary, report, errors)
            except BaseException as exc:
                errors.append('private daemon: '+type(exc).__name__)
        if lan:
            await cleanup('isolated_lan_closed', lan.close(), 20)
        if baseline is not None:
            try:
                manifest = json.loads((group.STATE/'ownership.json').read_text())
                require(manifest['installation_id'] == report['installation_id'] and not manifest['processes'] and not manifest['networks'],
                        'Actual candidate ownership remains after cleanup')
                if group.NATIVE_LAB is not None:
                    require(group.native_lab_admission(manifest, 'bluetooth_route', original_netns_fd=original_netns_fd)
                            == report['native_lab'], 'Clean lab admission changed during cleanup')
                group.observation.base.closed_slot()
                require(await group.observation.base.host_snapshot() == baseline and group.legacy_snapshot() == legacy,
                        'Host, legacyPID or protectedPCM baseline changed')
                report['cleanup'].update(empty_manifest=True, slot7_closed=True, host_and_legacy_preserved=True)
            except Exception as exc:
                errors.append('host cleanup: '+type(exc).__name__)
        if type(original_netns_fd) is int and original_netns_fd >= 3:
            with suppress(OSError):
                os.close(original_netns_fd)
        report['cleanup_errors'] = errors
        report['passed'] = completed and not errors and all(report['cleanup'].values())
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        atomic_json(RESULT, report)
        if temporary:
            route.retain_receipt(temporary, report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--expected-sha256', required=True)
    parser.add_argument('--parent-namespace', required=True)
    parser.add_argument('--original-netns-fd', type=int, required=True)
    options = parser.parse_args()
    async def supervised():
        task, loop = asyncio.current_task(), asyncio.get_running_loop()
        for name in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(name, task.cancel)
        return await run_check(options.binary, options.expected_sha256, options.parent_namespace, options.original_netns_fd)
    result = asyncio.run(supervised())
    raise SystemExit(0 if result['passed'] else 1)
