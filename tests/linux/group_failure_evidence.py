"""Private, bounded diagnostics for the manual native-group proof.

Capture before stopping producers or deleting rooms. Historical journal reads
use an exact admitted unit, invocation and boot, regardless of whether that
daemon is still alive. Configurations, command payloads and unrelated services
are never evidence sources. This helper is not part of the production runtime.
"""
from __future__ import annotations

# All filesystem reads below have a fixed size and held, validated descriptors.
# ruff: noqa: ASYNC240

import asyncio
import base64
from contextlib import suppress
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
from uuid import UUID

from shiri.runtime.bind_policy import trusted_file
from shiri.runtime.system import RuntimeFailure, atomic_json, boot_id
from shiri.runtime.units import UNIT_RE, UnitManager

FILE_BYTES = 64 * 1024
ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_UNITS = 15
DEADLINE_SECONDS = 6.
JOURNAL_LINES = 256
_REAPERS = set()
SENSITIVE = re.compile(r'authorization|cookie|password|secret|bearer|\btoken\b|'
                       r'v=0|a=ice-|a=fingerprint:|ice[_-](?:pwd|ufrag)|\bsdp\b', re.I)


def same_json(left, right):
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(right, sort_keys=True, allow_nan=False)


class Sanitizer:
    def __init__(self, private=()):
        self.private = tuple(sorted({value for value in private if isinstance(value, str) and value},
                                    key=len, reverse=True))

    def text(self, value, *, limit=FILE_BYTES):
        lines, omitted = [], 0
        for line in str(value).splitlines():
            for private in self.private:
                line = line.replace(private, '[redacted]')
            if SENSITIVE.search(line):
                omitted += 1
            else:
                lines.append(line)
        encoded = '\n'.join(lines).encode('utf-8')
        return {'text': encoded[:limit].decode('utf-8', errors='ignore'),
                'omitted_sensitive_lines': omitted, 'truncated': len(encoded) > limit}


def _open_file(path, *, root, owner, group=None, writable_parent=False):
    """Walk beneath a protected root with NOFOLLOW on every component.

    A producer may replace its own status file in its exact writable status
    directory. All earlier parents remain root-owned and non-writable.
    """
    path, root = Path(path), Path(root)
    if not path.is_absolute() or '..' in path.parts or not path.is_relative_to(root):
        raise RuntimeFailure('Evidence path is outside its exact admitted root')
    relative = path.relative_to(root)
    if not relative.parts:
        raise RuntimeFailure('Evidence source must be an ordinary file')
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        parts = path.parts[1:-1]
        for index, component in enumerate(parts):
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=directory)
            info = os.fstat(child)
            final_status = writable_parent and index == len(parts)-1
            wanted = owner if final_status else 0
            sticky_ancestor = not final_status and Path('/', *parts[:index+1]) in root.parents and info.st_mode & stat.S_ISVTX
            if (info.st_uid != wanted or (info.st_mode & 0o022 and not sticky_ancestor)
                    or (final_status and group is not None and info.st_gid != group)):
                os.close(child)
                raise RuntimeFailure('Evidence directory ownership or write boundary changed')
            os.close(directory)
            directory = child
        descriptor = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                             dir_fd=directory)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != owner
                or info.st_mode & 0o022 or (group is not None and info.st_gid != group)):
            os.close(descriptor)
            raise RuntimeFailure('Evidence source is linked, writable or has a different owner')
        return descriptor
    finally:
        os.close(directory)


def read_source(path, *, root, owner, group=None, writable_parent=False, tail=False, limit=FILE_BYTES):
    descriptor = _open_file(path, root=root, owner=owner, group=group, writable_parent=writable_parent)
    try:
        before = os.fstat(descriptor)
        offset = max(0, before.st_size-limit) if tail else 0
        data = os.pread(descriptor, limit if tail else limit+1, offset)
        if not tail and len(data) > limit:
            raise RuntimeFailure('Evidence source exceeds its fixed byte bound')
        if tail and offset:
            # Do not preserve a partial line which could omit a credential's
            # leading marker. Keep only complete log records after that line.
            data = data.partition(b'\n')[2]
        return data, {'bytes_read': len(data), 'source_bytes': before.st_size,
                      'truncated': bool(offset), 'inode': before.st_ino}
    finally:
        os.close(descriptor)


def epoch_producer_root(broker, owner, state, producer, entry, epoch_id):
    """Admit only the current explicit epoch's durable receiver invocation."""
    root = Path(state.directory)/'native-validation-epoch'/epoch_id
    account = broker._account(owner, 'receiver', state.desired.slot)
    if (state.desired.id != owner or type(state.desired.slot) is not int or state.desired.slot not in (6, 7)
            or set(account) != {'name', 'uid', 'gid'}
            or account['name'] != f'shiri-receiver-{state.desired.slot}'
            or any(type(account[k]) is not int or not 0 < account[k] < 2**32 for k in ('uid', 'gid'))
            or not same_json(producer.get('account'), account)
            or entry['user'] != account['name'] or entry['group'] != account['name']
            or producer.get('status') != root/'status/status.json'
            or producer.get('command') != root/'config/command.json'
            or entry['log_path'] != str(root/'logs/shairport.log')):
        raise RuntimeFailure('Epoch producer differs from its exact room/receiver UID/held path')
    properties = entry['properties']
    wanted = [sys.executable, str(Path(__file__).with_name('check_native_grouping.py')),
              '--producer', '/run/shiri-worker/probe-config/producer.json']
    if (entry['argv'] != wanted
            or properties.get('BindPaths') != f'{root}/status:/run/shiri-worker/validation:norbind'
            or properties.get('BindReadOnlyPaths') != (
                f'{state.directory}/input:/run/shiri-worker/input:norbind '
                f'{root}/config:/run/shiri-worker/probe-config:norbind')):
        raise RuntimeFailure('Epoch producer differs from its exact generated command/bind authority')
    return root


def owned_sources(broker, room_states, producers, *, epoch_id=None):
    """Freeze only handle/manifest matches; do not infer ownership from a PID."""
    if epoch_id is not None:
        try:
            if type(epoch_id) is not str or str(UUID(epoch_id)) != epoch_id:
                raise ValueError
        except (ValueError, AttributeError, TypeError):
            raise RuntimeFailure('Evidence epoch must be one explicit canonical UUID') from None
    admitted, errors, seen = [], [], set()
    scopes = [(owner, state.directory, state.processes) for owner, state in room_states.items()]
    scopes.append(('sender', broker.config.runtime_state_dir/'sender', broker.sender_processes))
    for owner, root, processes in scopes:
        for role, handle in tuple(processes.items()):
            key = f'{owner}:{role}'
            try:
                entry = deepcopy(handle.identity())
                UnitManager.validate_saved(entry)
                parsed = UNIT_RE.fullmatch(entry['unit'])
                expected_owner = 'sender' if owner == 'sender' else UUID(owner).hex
                if (entry['boot_id'] != boot_id() or parsed['installation'] != broker.network.installation_tag
                        or parsed['owner'] != expected_owner or parsed['role'] != role
                        or entry['name'] != role or not same_json(entry, broker.network.manifest['processes'].get(key))
                        or not re.fullmatch(r'[0-9a-f]{32}', entry.get('invocation_id') or '')
                        or key in seen or len(admitted) == MAX_UNITS):
                    raise RuntimeFailure('Evidence unit differs from its durable admitted invocation')
                log_path = Path(entry['log_path'])
                expected_log = Path(root)/'logs'/f'{role}.log'
                producer = producers.get(owner)
                if producer is not None and role == 'shairport':
                    producer_root = Path(root)/'native-validation' if epoch_id is None else epoch_producer_root(
                        broker, owner, room_states[owner], producer, entry, epoch_id)
                    expected_log = producer_root/'logs/shairport.log'
                    if producer['unit'] is not handle or producer['key'] != key:
                        raise RuntimeFailure('Producer handle no longer matches the exact receiver unit')
                if log_path != expected_log:
                    raise RuntimeFailure('Evidence log is outside the exact generated daemon path')
                source = {'key': key, 'entry': entry, 'handle': handle, 'root': Path(root), 'log_path': log_path}
                if producer is not None and role == 'shairport':
                    wanted_status = producer_root/'status/status.json'
                    if producer['status'] != wanted_status:
                        raise RuntimeFailure('Producer status is outside the exact generated producer directory')
                    source['producer'] = {'path': wanted_status, 'account': dict(producer['account'])}
                seen.add(key)
                admitted.append(source)
            except Exception as exc:
                errors.append({'key': key, 'error_type': type(exc).__name__})
    return admitted, errors


def identity(source):
    entry = source['entry']
    return {key: entry[key] for key in ('unit', 'name', 'boot_id', 'invocation_id', 'user')}


def producer_status(source, sanitizer):
    producer = source['producer']
    account = producer['account']
    raw, meta = read_source(producer['path'], root=source['root'], owner=account['uid'], group=account['gid'],
                            writable_parent=True)
    value = json.loads(raw)
    if (not isinstance(value, dict) or type(value.get('uid')) is not int or type(value.get('gid')) is not int
            or value['uid'] != account['uid'] or value['gid'] != account['gid']
            or type(value.get('pid')) is not int or value['pid'] <= 1):
        raise RuntimeFailure('Historical producer status does not match its admitted credentials')
    # A dead producer still has useful final status. Its claimed PID is recorded
    # as status, never used to signal or attribute an unrelated journal record.
    result = {key: value[key] for key in ('uid', 'gid', 'pid')}
    for key in ('frames', 'epoch', 'generation', 'presentation_ns', 'max_send_lateness_ns', 'max_clock_bracket_ns',
                'clock_sample_attempts', 'clock_sample_retries', 'max_attempt_clock_bracket_ns'):
        if key in value:
            if type(value[key]) is not int or not 0 <= value[key] <= 2**64-1:
                raise RuntimeFailure('Producer status contains an invalid bounded counter')
            result[key] = value[key]
    if 'clock_sample_failure' in value:
        failure = value['clock_sample_failure']
        keys = {'kind', 'attempt', 'before_ns', 'raw_ns', 'after_ns', 'started_ns', 'bracket_ns',
                'elapsed_ns', 'budget_ns', 'bracket_limit_ns'}
        if (not isinstance(failure, dict) or set(failure) != keys
                or failure['kind'] not in {'elapsed_budget', 'attempts'}
                or any(type(failure[k]) is not int or not 0 <= failure[k] <= 2**64-1 for k in keys-{'kind'})
                or not 1 <= failure['attempt'] <= 4 or failure['budget_ns'] != 5_000_000
                or failure['bracket_limit_ns'] != 1_000_000
                or not 0 < failure['started_ns'] <= failure['before_ns'] <= failure['after_ns']
                or not 0 < failure['raw_ns']
                or failure['bracket_ns'] != failure['after_ns']-failure['before_ns']
                or failure['elapsed_ns'] != failure['after_ns']-failure['started_ns']
                or (failure['kind'] == 'elapsed_budget' and failure['elapsed_ns'] < 5_000_000)
                or (failure['kind'] == 'attempts' and (failure['attempt'] != 4
                    or failure['bracket_ns'] <= 1_000_000 or failure['elapsed_ns'] >= 5_000_000))):
            raise RuntimeFailure('Producer clock refusal telemetry relabels the unchanged source guard')
        result['clock_sample_failure'] = dict(failure)
    if 'delivery_observation' in value:
        delivery = value['delivery_observation']
        keys = {'stage', 'frame', 'target_ns', 'observed_ns', 'lateness_ns'}
        if (type(delivery) is not dict or set(delivery) != keys
                or type(delivery['stage']) is not str
                or delivery['stage'] not in {'command_read', 'preclock', 'clock_sample', 'native_send', 'progress_publish'}
                or any(type(delivery[key]) is not int or not 0 <= delivery[key] <= 2**64-1
                       for key in keys-{'stage', 'lateness_ns'})
                or type(delivery['lateness_ns']) is not int
                or not -2**63 <= delivery['lateness_ns'] <= 2**63-1):
            raise RuntimeFailure('Producer delivery observation has invalid bounded telemetry')
        result['delivery_observation'] = dict(delivery)
    if 'progress_publication' in value:
        progress = value['progress_publication']
        keys = {'started_ns', 'finished_ns', 'duration_ns', 'max_duration_ns', 'count', 'passed'}
        if (type(progress) is not dict or set(progress) != keys or type(progress['passed']) is not bool
                or any(type(progress[key]) is not int or not 0 <= progress[key] <= 2**64-1
                       for key in keys-{'passed'})):
            raise RuntimeFailure('Producer progress publication has invalid bounded telemetry')
        result['progress_publication'] = dict(progress)
    if value.get('stage') in {'connecting', 'granted', 'streaming', 'finished'}:
        result['stage'] = value['stage']
    if type(value.get('finished')) is bool:
        result['finished'] = value['finished']
    for key in ('session_id', 'incarnation'):
        if key in value:
            result[key] = str(UUID(value[key]))
    if 'error' in value:
        result['error'] = sanitizer.text(value['error'], limit=2048)
    if isinstance(value.get('commands'), list):
        result['commands'] = []
        for command in value['commands'][-8:]:
            if (isinstance(command, dict) and type(command.get('generation')) is int
                    and 0 <= command['generation'] <= 2**63-1
                    and command.get('action') in {'run', 'wait', 'takeover', 'end'}):
                recorded = {key: command[key] for key in ('generation', 'action')}
                if command.get('stale_volume') in {'retired_socket_rejected',
                        'send_completed; successor health must independently remain unchanged'}:
                    recorded['stale_volume'] = command['stale_volume']
                result['commands'].append(recorded)
    return {'state': result, 'source': meta}


async def bounded_process(argv, *, limit=FILE_BYTES, timeout=2., pass_fds=()):
    """Bound stdout allocation and reap only this direct diagnostic child."""
    process = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        pass_fds=pass_fds, env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C'})
    data, truncated = bytearray(), False
    async def collect():
        nonlocal truncated
        while chunk := await process.stdout.read(min(8192, limit+1-len(data))):
            data.extend(chunk)
            if len(data) > limit:
                truncated = True
                return
        await process.wait()
    try:
        await asyncio.wait_for(collect(), timeout)
        return bytes(data[:limit]), {'truncated': truncated, 'exit_code': process.returncode}
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        # Cancellation of the caller must not leave a journal subprocess. The
        # direct child has no recoverable independent daemon state to preserve.
        async def reap():
            # Drain discarded bytes after SIGKILL so a paused asyncio pipe
            # transport also closes. Never collect them into a growing buffer.
            while await process.stdout.read(8192):
                pass
            await process.wait()
        reaper = asyncio.create_task(reap())
        _REAPERS.add(reaper)
        reaper.add_done_callback(_REAPERS.discard)
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(reaper), .5)


def journal_records(data, source, sanitizer):
    expected = source['entry']
    wanted = {'_SYSTEMD_UNIT': expected['unit'], '_SYSTEMD_INVOCATION_ID': expected['invocation_id'],
              '_BOOT_ID': UUID(expected['boot_id']).hex}
    records, rejected = [], 0
    for line in data.splitlines()[:JOURNAL_LINES]:
        try:
            value = json.loads(line)
            if not isinstance(value, dict) or any(value.get(key) != item for key, item in wanted.items()):
                raise ValueError('Unmatched historical journal identity')
            if not isinstance(value.get('MESSAGE'), str):
                raise ValueError('Journal message is not ordinary text')
            clean = sanitizer.text(value['MESSAGE'], limit=8192)
            record = {**wanted, **clean}
            for key in ('__REALTIME_TIMESTAMP', '__MONOTONIC_TIMESTAMP', 'PRIORITY'):
                if isinstance(value.get(key), str) and re.fullmatch(r'[0-9]{1,20}', value[key]):
                    record[key] = value[key]
            records.append(record)
        except (ValueError, TypeError, UnicodeError):
            rejected += 1
    return {'records': records, 'rejected_records': rejected, 'invocation_scoped': True,
            'scope': 'Exact admitted unit, invocation and boot IDs; every retained row revalidated'}


async def exact_journal(source, sanitizer):
    path = trusted_file(Path('/usr/bin/journalctl'), executable=True)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info, current = os.fstat(descriptor), path.lstat()
        if ((info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)
                or not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o6022
                or info.st_nlink != 1):
            raise RuntimeFailure('Journal executable changed during evidence admission')
        entry = source['entry']
        argv = [f'/proc/self/fd/{descriptor}', '--no-pager', '--reverse', '--output=json',
                f'--lines={JOURNAL_LINES}', f'_SYSTEMD_UNIT={entry["unit"]}',
                f'_SYSTEMD_INVOCATION_ID={entry["invocation_id"]}', f'_BOOT_ID={UUID(entry["boot_id"]).hex}']
        raw, meta = await bounded_process(argv, pass_fds=(descriptor,))
        return {**journal_records(raw, source, sanitizer), **meta}
    finally:
        os.close(descriptor)


async def unit_status(source, sanitizer):
    expected, handle = source['entry'], source['handle']
    actual = await asyncio.wait_for(handle.manager.inspect(expected['unit']), 1.)
    if actual is None:
        return {'loaded': False, 'alive': False}
    observed = actual.get('InvocationID')
    if isinstance(observed, (bytes, list)):
        observed = bytes(observed).hex()
    active = actual.get('ActiveState') in {'active', 'activating'}
    if observed != expected['invocation_id'] and (active or observed):
        raise RuntimeFailure('Observed unit belongs to a different invocation')
    result = {'loaded': True, 'alive': active, 'expected_invocation': expected['invocation_id']}
    for key in ('ActiveState', 'SubState', 'Result'):
        if isinstance(actual.get(key), str):
            result[key] = sanitizer.text(actual[key], limit=128)['text']
    for key in ('MainPID', 'ExecMainPID', 'ExecMainCode', 'ExecMainStatus'):
        if type(actual.get(key)) is int:
            result[key] = actual[key]
    return result


async def capture(directory, sources, *, private=(), admission_errors=(), journal=exact_journal,
                  deadline=DEADLINE_SECONDS):
    """Return an artifact receipt; capture failures never replace test failures."""
    directory = Path(directory)
    directory.mkdir(mode=0o700)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeFailure('Failure evidence requires a new private root-owned directory')
    private = tuple(private) + tuple(base64.b64encode(f'root:{item}'.encode()).decode() for item in private if item)
    sanitizer = Sanitizer(private)
    evidence = {'version': 1, 'captured_at': datetime.now(timezone.utc).isoformat(),
                'captured_monotonic_ns': time.monotonic_ns(),
                'bounds': {'collection_deadline_seconds': deadline, 'child_reap_grace_seconds': .5,
                           'file_bytes': FILE_BYTES,
                           'artifact_bytes': ARTIFACT_BYTES, 'max_units': MAX_UNITS},
                'admission_errors': list(admission_errors)[:MAX_UNITS], 'units': {}, 'deadline_exceeded': False}
    if len(sources) > MAX_UNITS:
        raise RuntimeFailure('Failure evidence exceeds the owned unit bound')
    semaphore = asyncio.Semaphore(3)
    async def collect(source):
        key = source['key']
        record = {'identity': identity(source), 'errors': []}
        evidence['units'][key] = record
        if 'producer' in source:
            try:
                record['producer_status'] = producer_status(source, sanitizer)
            except Exception as exc:
                record['errors'].append({'source': 'producer_status', 'type': type(exc).__name__})
        record['logs'] = []
        for suffix in ('', '.1'):
            try:
                raw, meta = read_source(Path(str(source['log_path'])+suffix), root=source['root'], owner=0,
                                        tail=True, limit=FILE_BYTES//2)
                record['logs'].append({'rotation': suffix or 'current', 'invocation_scoped': False,
                    'scope': 'Historical generated room/sender role logfile; may include earlier invocations; '
                             'current invocation attribution unavailable',
                    **meta, **sanitizer.text(raw.decode('utf-8', 'replace'))})
            except FileNotFoundError:
                continue
            except Exception as exc:
                record['errors'].append({'source': 'log'+suffix, 'type': type(exc).__name__})
        async with semaphore:
            for name, operation in [('status', unit_status), ('journal', journal)]:
                try:
                    record[name] = await operation(source, sanitizer)
                except Exception as exc:
                    record['errors'].append({'source': name, 'type': type(exc).__name__})
    tasks = [asyncio.create_task(collect(source)) for source in sources]
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), deadline)
    except asyncio.TimeoutError:
        evidence['deadline_exceeded'] = True
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        evidence['finished_at'] = datetime.now(timezone.utc).isoformat()
        # JSON escaping can expand even a bounded input. Drop largest text
        # sources, retain identities/status/errors and declare the truncation.
        def encoded():
            return json.dumps(evidence, allow_nan=False, indent=2).encode()
        while len(encoded()) > ARTIFACT_BYTES:
            candidates = [(len(json.dumps(record.get(field, {}))), record, field)
                          for record in evidence['units'].values() for field in ('logs', 'journal') if field in record]
            if not candidates:
                raise RuntimeFailure('Diagnostic metadata exceeded its artifact bound')
            _, record, field = max(candidates, key=lambda item: item[0])
            del record[field]
            record['errors'].append({'source': field, 'type': 'ArtifactByteLimit'})
        artifact = directory/'diagnostics.json'
        atomic_json(artifact, evidence, mode=0o600)
    raw = artifact.read_bytes()
    return {'path': str(artifact), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
            'units': len(evidence['units']), 'deadline_exceeded': evidence['deadline_exceeded']}
