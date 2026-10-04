#!/usr/bin/env python3
"""Supervise the per-zone latency matrix inside a disconnected parent namespace.

Run explicitly as Linux root under a 3180-second external process watchdog.
The matrix admits six separate 480-second fixtures, each with a
70-second cleanup deadline, inside a 3000-second collection deadline. The
external margin permits current-epoch cancellation and direct-child reaping.
This creates no interface on the original host. The inner test records and
cleans its virtual LAN; the supervisor checks the original host before/after
and removes the exact parent only with empty candidate ownership and no PIDs.
"""
from __future__ import annotations

# Manual bounded host evidence and durable fixture manifests.
# ruff: noqa: ASYNC240

import asyncio
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
import stat
from uuid import UUID, uuid4

from shiri.runtime.system import Runner, RuntimeFailure, atomic_json, boot_id, root_directory
from shiri.runtime.bind_policy import trusted_file

HERE = Path(__file__).resolve()
spec = importlib.util.spec_from_file_location('supervised_native_group', HERE.with_name('check_native_grouping.py'))
group = importlib.util.module_from_spec(spec)
spec.loader.exec_module(group)
RESULT = Path('/tmp/shiri-v2-native-latency-probe-supervisor-result.json')
if group.NATIVE_LAB is not None:
    RESULT = group.WORK/RESULT.name

SOURCE_FILES = (
    'tests/linux/native_lab.py', 'tests/linux/native_lab_audio.py', 'tests/linux/native_lab_observation.py',
    'tests/linux/run_native_latency_probe.py', 'tests/linux/native_latency_probe.py',
    'tests/linux/native_latency_epochs.py',
    'tests/linux/check_native_grouping.py', 'tests/linux/loopback_capture_probe.py',
    'shiri/runtime/audio.py', 'shiri/runtime/native.py', 'shiri/runtime/timing.py',
    'shiri/runtime/configuration.py', 'shiri/runtime/latency.py', 'shiri/runtime/speech_output.py',
)


def source_receipts(project, *, finite_speech=False):
    """Name source bytes, not cached bytecode; external staging pins the tree."""
    result = {}
    if type(finite_speech) is not bool:
        raise RuntimeFailure('Finite source admission must be explicit boolean')
    extra = ('tests/linux/native_speech_finite.py', 'tests/linux/run_native_speech_finite.py',
             'tests/linux/group_failure_evidence.py') if finite_speech else ()
    for relative in (*SOURCE_FILES, *extra):
        path = trusted_file(Path(project)/relative)
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                    or info.st_mode & 0o022 or not 0 < info.st_size <= 1024*1024):
                raise RuntimeFailure('Latency source is not a bounded immutable root-owned file')
            chunks, size = [], 0
            while data := os.read(descriptor, 65536):
                size += len(data)
                if size > 1024*1024:
                    raise RuntimeFailure('Latency source changed beyond its admitted size bound')
                chunks.append(data)
            after, current = os.fstat(descriptor), path.lstat()
            def signature(value):
                return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns
            if signature(info) != signature(after) or signature(info) != signature(current) or size != info.st_size:
                raise RuntimeFailure('Latency source changed while its proof bytes were read')
            result[relative] = {'sha256': hashlib.sha256(b''.join(chunks)).hexdigest(),
                                'bytes': size, 'st_dev': info.st_dev, 'st_ino': info.st_ino}
        finally:
            os.close(descriptor)
    return result


async def stop_child(process, *, grace=15, kill_timeout=5):
    """Reap only our unreaped direct child, even if it ignores SIGTERM."""
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), grace)
        return
    except asyncio.TimeoutError:
        pass
    # An unreaped direct child cannot be PID-reused. Do not kill arbitrary PIDs
    # reported by a namespace; those remain an exact ownership/recovery gate.
    try:
        process.kill()
    except ProcessLookupError:
        pass
    await asyncio.wait_for(process.wait(), kill_timeout)


NETNS_DIRECTORY = Path('/run/netns')
ORIGINAL_NAMESPACE = Path('/proc/self/ns/net')
REPORT_LIMIT = 8 * 1024 * 1024
CHILD_SECONDS = 480
EPOCH_CLEANUP_SECONDS = 70
MATRIX_SECONDS = 3000
EXTERNAL_MATRIX_SECONDS = 3180


def exact_json(first, second):
    """JSON identity includes types, key presence and finite values."""
    return json.dumps(first, sort_keys=True, allow_nan=False, separators=(',', ':')) == json.dumps(
        second, sort_keys=True, allow_nan=False, separators=(',', ':'))


def strict_object(items):
    result = {}
    for key, value in items:
        if key in result:
            raise RuntimeFailure('Epoch report contains a duplicate JSON key')
        result[key] = value
    return result


def no_constant(value):
    raise RuntimeFailure(f'Epoch report contains a nonfinite JSON constant: {value}')


def report_signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            info.st_mode, info.st_uid, info.st_gid, info.st_nlink)


def read_epoch_report(path, *, expected_uid=0):
    """Read only the held, bounded single-link report from our private parent."""
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != expected_uid or before.st_nlink != 1
            or before.st_mode & 0o022 or not 0 < before.st_size <= REPORT_LIMIT):
        raise RuntimeFailure('Epoch report is not a bounded protected regular file')
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        initial = os.fstat(descriptor)
        if report_signature(initial) != report_signature(before):
            raise RuntimeFailure('Epoch report was replaced before its held read')
        chunks, size = [], 0
        while data := os.read(descriptor, 65536):
            size += len(data)
            if size > REPORT_LIMIT:
                raise RuntimeFailure('Epoch report exceeded its bounded read')
            chunks.append(data)
        after, current = os.fstat(descriptor), path.lstat()
        if (report_signature(initial) != report_signature(after)
                or report_signature(initial) != report_signature(current) or size != initial.st_size):
            raise RuntimeFailure('Epoch report changed during its held read')
        payload = b''.join(chunks)
        report = json.loads(payload, object_pairs_hook=strict_object, parse_constant=no_constant)
        if type(report) is not dict:
            raise RuntimeFailure('Epoch report must be one JSON object')
        return report, {'path': str(path), 'sha256': hashlib.sha256(payload).hexdigest(),
                        'bytes': size, 'st_dev': initial.st_dev, 'st_ino': initial.st_ino}
    finally:
        os.close(descriptor)


def report_time(value):
    if type(value) is not str:
        raise RuntimeFailure('Epoch report lacks a textual aware timestamp')
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeFailure('Epoch report timestamp is malformed') from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeFailure('Epoch report timestamp has no timezone')
    return parsed.astimezone(timezone.utc)


def validate_epoch_report(report, phase, admission, launch_at, observed_at, *, finite_speech=False):
    """The aggregate never relabels a stale, failed or differently prepared child."""
    started, finished = report_time(report.get('started_at')), report_time(report.get('finished_at'))
    if not launch_at <= started <= finished <= observed_at:
        raise RuntimeFailure('Epoch report falls outside its exact child launch/observation window')
    epoch = report.get('latency_epoch')
    receipt = {key: epoch.get(key) for key in ('version', 'id', 'offset_ms', 'phase')} if type(epoch) is dict else None
    cleanup = report.get('cleanup')
    if type(finite_speech) is not bool:
        raise RuntimeFailure('Finite report mode must be explicit boolean')
    if (report.get('mode') != ('finite_speech' if finite_speech else 'latency_probe') or report.get('passed') is not True
            or not exact_json(receipt, phase.receipt()) or epoch.get('passed') is not True
            or type(cleanup) is not dict or not cleanup or any(value is not True for value in cleanup.values())
            or not exact_json(report.get('cleanup_errors'), [])
            or not exact_json(report.get('native_lab'), admission) or report.get('failure') is not None):
        raise RuntimeFailure('Epoch report failed exact phase, cleanup or clean-lab admission')


def load_matrix_helper():
    spec = importlib.util.spec_from_file_location('supervised_latency_epochs', HERE.with_name('native_latency_epochs.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def matrix_admission(source_files, admission=None, installation=None, *, finite_speech=False):
    """Repeat installed/source/boot/lab admission with empty ownership each time."""
    manifest = json.loads((group.STATE/'ownership.json').read_text())
    if (type(manifest) is not dict or type(manifest.get('installation_id')) is not str
            or not manifest['installation_id'] or manifest.get('networks') != {} or manifest.get('processes') != {}):
        raise RuntimeFailure('Clean-lab candidate ownership must be exactly empty')
    if type(finite_speech) is not bool:
        raise RuntimeFailure('Finite lab admission must be explicit boolean')
    current = group.native_lab_admission(manifest, 'finite_speech' if finite_speech else 'latency_probe')
    if admission is not None and not exact_json(current, admission):
        raise RuntimeFailure('Exact clean-lab admission changed between latency epochs')
    if installation is not None and manifest['installation_id'] != installation:
        raise RuntimeFailure('Latency installation identity changed between epochs')
    current_sources = source_receipts(group.PROJECT, finite_speech=True) if finite_speech else source_receipts(group.PROJECT)
    if current_sources != source_files:
        raise RuntimeFailure('Latency source bytes/identities changed between epochs')
    return manifest['installation_id'], current


def held_namespace(descriptor, node, identity):
    held = group.isolated_lan.namespace_identity(descriptor)
    current = node.lstat()
    if (held != identity or (current.st_dev, current.st_ino) != identity or stat.S_ISLNK(current.st_mode)):
        raise RuntimeFailure('Exact parent namespace identity changed; refusing entry or cleanup')


def held_original(descriptor, identity):
    current = ORIGINAL_NAMESPACE.stat()
    if (group.isolated_lan.namespace_identity(descriptor) != identity
            or (current.st_dev, current.st_ino) != identity):
        raise RuntimeFailure('Original host namespace identity changed')


def private_directory(path):
    """A fresh supervisor root is never adopted, even when it looks safe."""
    # This common ancestor also contains the rootless API's separately owned
    # fixture. Individual supervisor receipts remain root-only below it.
    root_directory(group.WORK, mode=0o755)
    shared = group.WORK.lstat()
    if stat.S_IMODE(shared.st_mode) != 0o755:
        raise RuntimeFailure('Lab work ancestor must be root-owned mode0755 for rootless API traversal')
    if group.WORK.resolve(strict=True) != group.WORK:
        raise RuntimeFailure('Fresh-lab WORK must use a canonical directory without symlink ancestors')
    path.mkdir(mode=0o700)
    current = path.lstat()
    if not stat.S_ISDIR(current.st_mode) or current.st_uid != 0 or stat.S_IMODE(current.st_mode) != 0o700:
        raise RuntimeFailure('New supervisor directory is not exactly root-owned/private')
    return current.st_dev, current.st_ino


def directory_unchanged(root, identity):
    info = root.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700
            or (info.st_dev, info.st_ino) != identity):
        raise RuntimeFailure('Supervisor private directory identity changed')


def failure(exc):
    return {'type': type(exc).__name__, 'message': str(exc)[:2000]}


async def run_epoch(phase, source_files, admission, installation, baseline, protected, *, runner=None, finite_speech=False):
    """Own one child and one held parent; retain failed evidence before return."""
    if type(finite_speech) is not bool:
        raise RuntimeFailure('Finite child mode must be explicit boolean')
    def admitted():
        if finite_speech:
            return matrix_admission(source_files, admission, installation, finite_speech=True)
        return matrix_admission(source_files, admission, installation)
    runner = runner or Runner()
    identifier = uuid4().hex
    namespace = f'shiri_group_run_{identifier[:8]}'
    root, node = group.WORK/f'latency-supervisor-{identifier}', NETNS_DIRECTORY/namespace
    record = {'started_at': datetime.now(timezone.utc).isoformat(), 'phase': phase.receipt(), 'passed': False,
              'namespace': namespace, 'private_directory': str(root), 'cleanup': {}, 'cleanup_errors': []}
    process = launch = log = descriptor = original_descriptor = inode = original = root_identity = None
    primary = None
    epoch_boot = boot_id()
    try:
        admitted()
        if not exact_json(await group.observation.base.host_snapshot(), baseline) or not exact_json(group.legacy_snapshot(), protected):
            raise RuntimeFailure('Original host/protected snapshot changed before an epoch')
        group.observation.base.closed_slot()
        if node.exists() or node.is_symlink():
            raise RuntimeFailure('Epoch parent already exists; refusing adoption')
        root_identity = private_directory(root)
        record.update(boot_id=epoch_boot, installation_id=installation, native_lab=admission)
        atomic_json(root/'supervisor.json', record)
        original_descriptor = os.open(ORIGINAL_NAMESPACE, os.O_RDONLY | os.O_CLOEXEC)
        original = group.isolated_lan.namespace_identity(original_descriptor)
        held_original(original_descriptor, original)
        record['original_namespace_identity'] = {'st_dev': original[0], 'st_ino': original[1]}
        await runner.run(['ip', 'netns', 'add', namespace], timeout=5)
        descriptor = os.open(node, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        inode = group.isolated_lan.namespace_identity(descriptor)
        if inode == original:
            raise RuntimeFailure('New parent equals the original host namespace')
        held_namespace(descriptor, node, inode)
        record['namespace_identity'] = {'st_dev': inode[0], 'st_ino': inode[1]}
        atomic_json(root/'supervisor.json', record)
        await runner.run(['ip', 'netns', 'exec', namespace, 'ip', 'link', 'set', 'lo', 'up'], timeout=5)
        held_namespace(descriptor, node, inode)
        held_original(original_descriptor, original)
        directory_unchanged(root, root_identity)
        if (root/'epoch-result.json').exists() or (root/'epoch-result.json').is_symlink():
            raise RuntimeFailure('Epoch result already exists before child launch')
        log = (root/'harness.log').open('xb')
        os.fchmod(log.fileno(), 0o600)
        record['launch_at'] = datetime.now(timezone.utc).isoformat()
        args = ['/usr/bin/nsenter', f'--net=/proc/self/fd/{descriptor}', '--', sys.executable,
                str(HERE.with_name('check_native_grouping.py')), '--parent-namespace', namespace,
                '--original-netns-fd', str(original_descriptor), '--latency-epoch-id', phase.epoch_id,
                '--latency-offset-ms', str(phase.offset_ms), '--latency-phase', phase.phase,
                '--latency-result', str(root/'epoch-result.json')]
        if finite_speech:
            args.append('--finite-speech')
        # Shield spawn itself: cancellation still obtains/reaps this exact direct child.
        launch = asyncio.create_task(asyncio.create_subprocess_exec(*args,
            env={**os.environ, 'PYTHONPATH': str(group.PROJECT)}, stdin=asyncio.subprocess.DEVNULL,
            stdout=log, stderr=asyncio.subprocess.STDOUT, pass_fds=(descriptor, original_descriptor)))
        try:
            process = await asyncio.shield(launch)
        except asyncio.CancelledError:
            process = await asyncio.shield(launch)
            raise
        try:
            await asyncio.wait_for(process.wait(), CHILD_SECONDS)
        except asyncio.TimeoutError as exc:
            raise RuntimeFailure('Latency epoch child exceeded 480 seconds') from exc
        record['child_exit_code'] = process.returncode
        directory_unchanged(root, root_identity)
        inner, proof = read_epoch_report(root/'epoch-result.json')
        record.update(inner_report=inner, report_proof=proof)
        if finite_speech:
            validate_epoch_report(inner, phase, admission, report_time(record['launch_at']), datetime.now(timezone.utc), finite_speech=True)
        else:
            validate_epoch_report(inner, phase, admission, report_time(record['launch_at']), datetime.now(timezone.utc))
        if process.returncode != 0:
            raise RuntimeFailure('Latency epoch direct child exited unsuccessfully')
        record['child_passed'] = True
    except BaseException as exc:
        primary = exc
        record['failure'] = failure(exc)

    async def cleanup():
        errors = record['cleanup_errors']
        if process is not None:
            try:
                await stop_child(process, grace=45, kill_timeout=5)
                if process.returncode is None:
                    raise RuntimeFailure('Exact direct child remains alive')
                record['cleanup']['direct_child_reaped'] = True
                record['child_exit_code'] = process.returncode
            except Exception as exc:
                errors.append({'stage': 'direct_child', **failure(exc)})
        if log is not None:
            log.close()
        try:
            admitted()
            if boot_id() != epoch_boot or not epoch_boot:
                raise RuntimeFailure('Boot changed; preserve exact parent for recovery')
            record['cleanup']['source_lab_ownership_preserved'] = True
            if inode is not None:
                if process is not None and process.returncode is None:
                    raise RuntimeFailure('Cannot remove parent while direct child remains alive')
                held_original(original_descriptor, original)
                held_namespace(descriptor, node, inode)
                pids = await runner.run(['ip', 'netns', 'pids', namespace], timeout=5)
                record['remaining_namespace_pids'] = pids.stdout.strip()[:1024]
                if pids.stdout.strip():
                    raise RuntimeFailure('Unexpected namespace PIDs remain; preserve parent')
                links = await asyncio.wait_for(runner.json(['ip', 'netns', 'exec', namespace, 'ip', '-j', 'link']), 5)
                if type(links) is not list or len(links) != 1 or links[0].get('ifname') != 'lo':
                    raise RuntimeFailure('Inner interfaces remain; preserve parent')
                held_namespace(descriptor, node, inode)
                await runner.run(['ip', 'netns', 'delete', namespace], timeout=5)
                if node.exists() or node.is_symlink():
                    raise RuntimeFailure('Exact parent namespace name survived deletion')
                record['cleanup']['parent_namespace_deleted'] = True
            elif node.exists() or node.is_symlink():
                raise RuntimeFailure('Unadmitted namespace creation remains; preserve for recovery')
        except Exception as exc:
            errors.append({'stage': 'parent_namespace', **failure(exc)})
        try:
            if not exact_json(await group.observation.base.host_snapshot(), baseline) or not exact_json(group.legacy_snapshot(), protected):
                raise RuntimeFailure('Original host/protected snapshot changed after epoch cleanup')
            group.observation.base.closed_slot()
            admitted()
            record['cleanup']['original_host_protected_preserved'] = True
            record['cleanup']['post_cleanup_admission_preserved'] = True
        except Exception as exc:
            errors.append({'stage': 'host_baseline', **failure(exc)})

    cleanup_task = asyncio.create_task(cleanup())
    try:
        await asyncio.wait_for(asyncio.shield(cleanup_task), EPOCH_CLEANUP_SECONDS)
    except BaseException as exc:
        if primary is None:
            primary = exc
            record['failure'] = failure(exc)
        record['cleanup_errors'].append({'stage': 'cleanup_deadline_or_cancellation', **failure(exc)})
        # Do not detach cancellation finalizers or advance while cleanup is pending.
        cleanup_task.cancel()
        await asyncio.gather(cleanup_task, return_exceptions=True)
        if process is not None and process.returncode is None:
            reaper = asyncio.create_task(stop_child(process, grace=0, kill_timeout=5))
            try:
                await asyncio.wait_for(asyncio.shield(reaper), 6)
            except BaseException as reap_error:
                reaper.cancel()
                await asyncio.gather(reaper, return_exceptions=True)
                record['cleanup_errors'].append({'stage': 'final_direct_child_reap', **failure(reap_error)})
    finally:
        if log is not None:
            log.close()
        for name, handle in (('parent', descriptor), ('original', original_descriptor)):
            if handle is not None:
                try:
                    os.close(handle)
                except OSError as exc:
                    record['cleanup_errors'].append({'stage': name+'_descriptor', **failure(exc)})
    # A timed-out/canceled child can finish its failure report during owned cleanup.
    # Retain it without promoting a failed launch into a successful epoch.
    if root_identity is not None and 'inner_report' not in record:
        try:
            directory_unchanged(root, root_identity)
            path = root/'epoch-result.json'
            if path.exists() or path.is_symlink():
                inner, proof = read_epoch_report(path)
                record.update(inner_report=inner, report_proof=proof)
        except Exception as exc:
            record['report_retention_error'] = failure(exc)
    record['passed'] = (record.get('child_passed') is True and not record['cleanup_errors']
                        and all(record['cleanup'].get(key) is True for key in (
                            'direct_child_reaped', 'source_lab_ownership_preserved',
                            'parent_namespace_deleted', 'original_host_protected_preserved',
                            'post_cleanup_admission_preserved')))
    record['finished_at'] = datetime.now(timezone.utc).isoformat()
    if root_identity is not None:
        try:
            directory_unchanged(root, root_identity)
            atomic_json(root/'supervisor.json', record)
        except Exception as exc:
            record['passed'] = False
            record['cleanup_errors'].append({'stage': 'retained_supervisor_receipt', **failure(exc)})
    if isinstance(primary, asyncio.CancelledError):
        # The controller retains this record, then stops; no next phase is admitted.
        primary.epoch_record = record
        raise primary
    return record


async def run_matrix():
    """Six fresh fixtures; only the strict helper can promote a nine-row result."""
    result = {'started_at': datetime.now(timezone.utc).isoformat(), 'passed': False,
              'measured_signal_timeline_cleanup_passed': False,
              'speech_latency_performance_passed': False,
              'speech_latency_performance_status': 'not_completed',
              'cold_utterance_completeness_passed': False,
              'cold_utterance_completeness_status': 'not_completed',
              'scope': 'Six independently cleaned digital epochs; no minimum speech-latency/phone/hardware/Cast/Bluetooth claim',
              'deadlines': {'child_seconds': CHILD_SECONDS, 'epoch_cleanup_seconds': EPOCH_CLEANUP_SECONDS,
                            'matrix_seconds': MATRIX_SECONDS, 'external_watchdog_seconds': EXTERNAL_MATRIX_SECONDS},
              'epochs': [], 'cleanup': {}, 'cleanup_errors': []}
    baseline = protected = source_files = admission = installation = None
    try:
        if sys.platform != 'linux' or os.geteuid() != 0 or not boot_id() or group.NATIVE_LAB is None:
            raise RuntimeFailure('Six-epoch matrix requires Linux root and an explicit fresh-lab profile')
        source_files = source_receipts(group.PROJECT)
        installation, admission = matrix_admission(source_files)
        result.update(source_files=source_files, native_lab=admission, installation_id=installation, boot_id=boot_id())
        group.observation.base.closed_slot()
        protected = group.legacy_snapshot()
        baseline = await group.observation.base.host_snapshot()
        helper = load_matrix_helper()
        expected = helper.phases()
        result['planned_phases'] = [phase.receipt() for phase in expected]
        plan = result['planned_phases']
        if (len(plan) != 6 or len({phase['id'] for phase in plan}) != 6
                or any(type(phase.get('offset_ms')) is not int
                       or type(phase.get('version')) is not int or phase['version'] != 1
                       or type(phase.get('id')) is not str or str(UUID(phase['id'])) != phase['id'] for phase in plan)
                or {(phase['offset_ms'], phase['phase']) for phase in plan}
                   != {(offset, role) for offset in (-2000, 0, 2000) for role in ('idle', 'native')}):
            raise RuntimeFailure('Controller did not prepare exactly six unique canonical latency phases')
        atomic_json(RESULT, result)

        async def collect():
            reports = []
            for phase in expected:
                try:
                    record = await run_epoch(phase, source_files, admission, installation, baseline, protected)
                except asyncio.CancelledError as exc:
                    if hasattr(exc, 'epoch_record'):
                        result['epochs'].append(exc.epoch_record)
                    raise
                result['epochs'].append(record)
                atomic_json(RESULT, result)
                if record.get('passed') is not True:
                    raise RuntimeFailure('Latency epoch or exact cleanup failed; remaining phases were not launched')
                reports.append(record['inner_report'])
            matrix_admission(source_files, admission, installation)
            result['latency_matrix'] = helper.aggregate_epoch_reports(reports, expected)
            matrix = result['latency_matrix']
            if (matrix.get('passed') is not True or matrix.get('measured_signal_timeline_cleanup_passed') is not True
                    or type(matrix.get('speech_latency_performance_passed')) is not bool
                    or type(matrix.get('speech_latency_performance_status')) is not str
                    or not matrix['speech_latency_performance_status']
                    or type(matrix.get('cold_utterance_completeness_passed')) is not bool
                    or type(matrix.get('cold_utterance_completeness_status')) is not str
                    or not matrix['cold_utterance_completeness_status']):
                raise RuntimeFailure('Strict six-epoch aggregate did not retain explicit measurement/performance outcomes')
            result['all_epochs_passed'] = True
        await asyncio.wait_for(collect(), MATRIX_SECONDS)
    except BaseException as exc:
        result['failure'] = failure(exc)
    finally:
        if source_files is not None:
            try:
                matrix_admission(source_files, admission, installation)
                result['cleanup']['final_source_lab_ownership_preserved'] = True
            except Exception as exc:
                result['cleanup_errors'].append({'stage': 'final_admission', **failure(exc)})
        if baseline is not None:
            try:
                if not exact_json(await group.observation.base.host_snapshot(), baseline) or not exact_json(group.legacy_snapshot(), protected):
                    raise RuntimeFailure('Original host/protected snapshot changed across the full matrix')
                group.observation.base.closed_slot()
                result['cleanup']['original_host_protected_preserved'] = True
            except Exception as exc:
                result['cleanup_errors'].append({'stage': 'final_baseline', **failure(exc)})
        result['passed'] = (result.get('all_epochs_passed') is True and not result['cleanup_errors']
                            and len(result['epochs']) == 6 and all(epoch['passed'] is True for epoch in result['epochs'])
                            and result['cleanup'].get('final_source_lab_ownership_preserved') is True
                            and result['cleanup'].get('original_host_protected_preserved') is True)
        if result['passed']:
            for name in ('measured_signal_timeline_cleanup_passed', 'speech_latency_performance_passed',
                         'speech_latency_performance_status', 'cold_utterance_completeness_passed',
                         'cold_utterance_completeness_status'):
                result[name] = result['latency_matrix'][name]
        result['finished_at'] = datetime.now(timezone.utc).isoformat()
        atomic_json(RESULT, result)
    return 0 if result['passed'] else 1


async def dispatch():
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(name, task.cancel)
    try:
        return await run_matrix()
    finally:
        for name in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(name)


if __name__ == '__main__':
    raise SystemExit(asyncio.run(dispatch()))
