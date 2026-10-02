"""Optional, bounded, read-only observations of the actual jitter1 endpoint.

This helper belongs to the disposable native fixture, not the runtime. Its
failures never qualify or reject playback. Counters describe a room launch's
lifetime; the source fields identify only the instant at which they were read.
"""
from __future__ import annotations

import asyncio
import json
import time
from uuid import UUID

PATH = '/api/player/shiri-speech-status'
SCOPE = 'room_launch_lifetime; current_source_at_read_only_snapshot'
MAX_BYTES = 4096
MAX_OBSERVATIONS = 12
ROOM_SECONDS = .25
HTTP_SECONDS = .15
RPC_SECONDS = .1
SIGNED64_MAX = 2**63-1
BOOL_FIELDS = ('configured', 'source_initialized', 'source_faulted', 'active', 'running', 'priming')
INTEGER_FIELDS = (
    'observed_monotonic_ns', 'sequence', 'queued_frames', 'admitted_packets', 'refused_packets',
    'expired_frames', 'pcm_packets', 'admitted_frames', 'mixed_frames', 'priming_events',
    'priming_frames', 'empty_frames', 'underflow_events', 'underflow_frames',
    'priming_until_monotonic_ns', 'current_run_first_pcm_admitted_monotonic_ns',
    'last_pcm_admitted_monotonic_ns', 'last_pcm_emitted_monotonic_ns', 'last_mix_monotonic_ns',
    'last_underflow_monotonic_ns', 'last_resume_monotonic_ns', 'max_pcm_admission_age_ns',
    'max_pcm_interarrival_ns', 'epoch', 'generation', 'operation_generation',
)
IDENTITY_FIELDS = ('room_id', 'launch_generation', 'incarnation', 'session_id',
                   'epoch', 'generation', 'operation_generation')
SCHEMA_FIELDS = frozenset(('version', 'scope', 'reserve_ns', *BOOL_FIELDS,
                          *INTEGER_FIELDS, *IDENTITY_FIELDS))


class DiagnosticFailure(ValueError):
    """A fixed reason code, without backend data or credentials."""


def _expect(condition, code):
    if not condition:
        raise DiagnosticFailure(code)


def _hex(value):
    _expect(type(value) is str and len(value) in (32, 36), 'identity_type')
    try:
        return UUID(value).hex
    except ValueError as exc:
        raise DiagnosticFailure('identity_uuid') from exc


def source_binding(identifier, launch, health):
    """Bind an instant to a ready worker's complete current music operation."""
    _expect(type(health) is dict and type(health.get('source')) is dict, 'worker_source_shape')
    source = health['source']
    _expect(health.get('ready') is True and not health.get('error')
            and source.get('ready') is True and not source.get('error')
            and source.get('transitioning') is False, 'worker_not_current')
    _expect(_hex(source.get('zone_id')) == _hex(identifier), 'worker_room')
    owner = source.get('owner')
    if owner is None:
        session, generation = None, 1  # NativeController's reviewed idle transition.
    else:
        _expect(type(owner) is dict and _hex(owner.get('zone_id')) == _hex(identifier)
                and _hex(owner.get('incarnation')) == _hex(source.get('incarnation'))
                and owner.get('epoch') == source.get('epoch'), 'worker_owner')
        session, generation = _hex(owner.get('session_id')), health.get('native_generation')
    result = {'room_id': _hex(identifier), 'launch_generation': _hex(launch),
              'incarnation': _hex(source.get('incarnation')), 'session_id': session,
              'epoch': source.get('epoch'), 'generation': generation,
              'operation_generation': health.get('source_operation_generation')}
    _expect(all(type(result[key]) is int and 0 <= result[key] <= SIGNED64_MAX
                for key in ('epoch', 'generation', 'operation_generation'))
            and result['generation'] > 0 and result['operation_generation'] > 0, 'worker_operation')
    return result


def validate_status(value, expected):
    """Validate exact version1 types and source scope without changing playback."""
    _expect(type(value) is dict and set(value) == SCHEMA_FIELDS, 'status_schema')
    _expect(type(value['version']) is int and value['version'] == 1
            and value['scope'] == SCOPE and type(value['reserve_ns']) is int
            and value['reserve_ns'] == 20_000_000, 'status_version_scope')
    _expect(all(type(value[key]) is bool for key in BOOL_FIELDS), 'status_boolean')
    _expect(all(type(value[key]) is int and 0 <= value[key] <= SIGNED64_MAX
                for key in INTEGER_FIELDS), 'status_integer')
    _expect(value['configured'] and value['source_initialized'] and not value['source_faulted']
            and value['observed_monotonic_ns'] > 0 and value['queued_frames'] <= 12_000,
            'status_not_current')
    _expect(all(type(value[key]) is str and len(value[key]) == 32
                and value[key] == _hex(value[key]) for key in ('room_id', 'launch_generation', 'incarnation'))
            and (value['session_id'] is None or type(value['session_id']) is str
                 and len(value['session_id']) == 32 and value['session_id'] == _hex(value['session_id'])),
            'status_identity')
    _expect(all(type(value[key]) is type(expected[key]) and value[key] == expected[key]
                for key in IDENTITY_FIELDS), 'status_wrong_binding')
    # Values are bounded primitives; no undeclared response fields are retained.
    return dict(value)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        _expect(key not in result, 'status_duplicate_key')
        result[key] = value
    return result


async def read_status(control):
    """Reuse the exact authenticated private client's stream, capped at4KiB."""
    async def exchange():
        async with control.client.stream('GET', PATH) as response:
            _expect(response.status_code == 200, 'status_http')
            content = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=MAX_BYTES):
                _expect(len(content)+len(chunk) <= MAX_BYTES, 'status_bytes')
                content.extend(chunk)
            try:
                return json.loads(content, object_pairs_hook=_unique_object)
            except (ValueError, UnicodeDecodeError) as exc:
                if isinstance(exc, DiagnosticFailure):
                    raise
                raise DiagnosticFailure('status_json') from exc
    return await asyncio.wait_for(exchange(), HTTP_SECONDS)


class SpeechDiagnostics:
    """Retain three speech boundaries and a first failure for two exact zones."""
    def __init__(self, states, health, report):
        self.states, self.health = states, health
        self.failure_started = False
        self.failure_complete = asyncio.Event()
        self.lock = asyncio.Lock()
        self.evidence = report['speech_status_diagnostics'] = {
            'path': PATH, 'optional': True, 'read_only': True,
            'counter_scope': 'room_launch_lifetime',
            'source_scope': 'current_source_at_read_only_snapshot',
            'utterance_identity_available': False,
            'limits': {'response_bytes': MAX_BYTES, 'observations': MAX_OBSERVATIONS,
                       'per_room_seconds': ROOM_SECONDS, 'http_seconds': HTTP_SECONDS,
                       'worker_rpc_seconds': RPC_SECONDS},
            'observations': [], 'omitted_observations': 0,
        }

    async def _one(self, identifier, state, label):
        rows = self.evidence['observations']
        if len(rows) >= MAX_OBSERVATIONS:
            self.evidence['omitted_observations'] += 1
            return
        row = {'room_id': str(identifier)[:36], 'phase': str(label)[:64],
               'started_monotonic_ns': time.monotonic_ns(), 'binding_verified': False}
        rows.append(row)  # Retain even a timeout or cancellation before cleanup.
        async def observe():
            try:
                before = source_binding(identifier, state.launch_generation,
                                        await self.health(state, RPC_SECONDS))
                row['expected_binding'] = before
                value = await read_status(state.client)
                snapshot = validate_status(value, before)
                after = source_binding(identifier, state.launch_generation,
                                       await self.health(state, RPC_SECONDS))
                _expect(after == before, 'worker_operation_changed_during_read')
                row.update(binding_verified=True, snapshot=snapshot)
            except asyncio.CancelledError as exc:
                # A dependency can cancel itself without cancelling the
                # caller. Convert that internal outcome inside this child;
                # outer CancelledError then always means caller cancellation.
                raise DiagnosticFailure('diagnostic_dependency_cancelled') from exc
        observation = asyncio.create_task(observe(), name='private-bluetooth-speech-status')
        try:
            # asyncio.wait_for's Python3.10 fut.done() cancellation branch
            # can swallow a caller cancellation. wait() leaves cancellation
            # with this caller and keeps this exact child explicitly owned.
            completed, _pending = await asyncio.wait({observation}, timeout=ROOM_SECONDS)
            if not completed:
                raise asyncio.TimeoutError()
            observation.result()
        except asyncio.CancelledError:
            row['diagnostic_error'] = {'type': 'CancelledError', 'code': 'diagnostic_cancelled'}
            raise
        except Exception as exc:
            row['diagnostic_error'] = {'type': type(exc).__name__[:80],
                                       'code': str(exc) if isinstance(exc, DiagnosticFailure) else 'diagnostic_unavailable'}
        finally:
            if not observation.done():
                observation.cancel()
            await asyncio.gather(observation, return_exceptions=True)
            row['finished_monotonic_ns'] = time.monotonic_ns()

    async def capture(self, label):
        # Fixed two-room fixture; don't iterate arbitrary endpoint inventories.
        async with self.lock:
            for identifier, state in tuple(self.states.items())[:2]:
                await self._one(identifier, state, label)

    async def failure(self, primary, label):
        if isinstance(primary, asyncio.CancelledError):
            return
        if self.failure_started:
            # The control watcher sets the primary error before this GET.
            # A concurrent exercise failure must join that first observation
            # before its own finally block can start retiring the endpoints.
            await self.failure_complete.wait()
            return
        self.failure_started = True
        self.evidence['first_failure_capture_started_monotonic_ns'] = time.monotonic_ns()
        try:
            await self.capture('failure:'+str(label)[:56])
        except Exception as exc:
            self.evidence['failure_capture_error'] = type(exc).__name__[:80]
        finally:
            self.failure_complete.set()
