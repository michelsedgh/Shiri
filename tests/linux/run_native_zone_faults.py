#!/usr/bin/env python3
"""Supervise abrupt per-zone faults inside a disconnected parent namespace.

Run explicitly as Linux root under an external 400-second process watchdog.
This creates no interface on the original host. The inner test records and
cleans its virtual LAN; the supervisor checks the original host before/after
and removes the exact parent only with empty candidate ownership and no PIDs.
"""
from __future__ import annotations

# Manual bounded host evidence and durable fixture manifests.
# ruff: noqa: ASYNC240

import argparse
import asyncio
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

from shiri.runtime.system import Runner, RuntimeFailure, atomic_json, boot_id, root_directory

HERE = Path(__file__).resolve()
spec = importlib.util.spec_from_file_location('supervised_native_group', HERE.with_name('check_native_grouping.py'))
group = importlib.util.module_from_spec(spec)
spec.loader.exec_module(group)
RESULT = Path('/tmp/shiri-v2-native-zone-faults-supervisor-result.json')
if group.NATIVE_LAB is not None:
    RESULT = group.WORK/RESULT.name
INNER_RESULT = group.RESULT.with_name('shiri-v2-native-zone-faults-result.json')


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


async def run(*, minimum_policy=False):
    if minimum_policy is not False:
        group.require(type(minimum_policy) is bool and group.NATIVE_LAB is not None,
                      "Minimum policy requires its explicit admitted clean lab")
    result = {'started_at': datetime.now(timezone.utc).isoformat(), 'passed': False,
              'scope': 'Two exact owned abrupt room crashes; original other room/host preserved', 'cleanup': {}}
    runner, process, log, descriptor, original_descriptor, inode, baseline = Runner(), None, None, None, None, None, None
    identifier = uuid4().hex
    namespace = f'shiri_group_run_{identifier[:8]}'
    root, node = group.WORK/f'zone-faults-supervisor-{identifier}', Path('/run/netns')/namespace
    errors, timed_out = [], False
    try:
        if sys.platform != 'linux' or os.geteuid() != 0 or not boot_id():
            raise RuntimeFailure('Run supervised native grouping as Linux root with a known boot')
        manifest = json.loads((group.STATE/'ownership.json').read_text())
        if group.NATIVE_LAB is not None:
            result['native_lab'] = group.native_lab_admission(manifest, 'zone_faults')
        elif not manifest['installation_id'].startswith('b265') or manifest['networks'] or manifest['processes']:
            raise RuntimeFailure('Known candidate must be idle before creating a test namespace')
        if node.exists():
            raise RuntimeFailure('Supervisor namespace already exists; refusing adoption')
        group.observation.base.closed_slot()
        legacy = group.legacy_snapshot()
        baseline = await group.observation.base.host_snapshot()
        original_descriptor = os.open('/proc/self/ns/net', os.O_RDONLY | os.O_CLOEXEC)
        original = group.isolated_lan.namespace_identity(original_descriptor)
        root_directory(group.WORK, mode=0o755)
        root_directory(root)
        result.update(namespace=namespace, boot_id=boot_id(), private_directory=str(root),
                      installation_id=manifest['installation_id'],
                      original_namespace_identity={'st_dev': original[0], 'st_ino': original[1]})
        atomic_json(root/'supervisor.json', result)
        await runner.run(['ip', 'netns', 'add', namespace], timeout=5)
        descriptor = os.open(node, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        inode = group.isolated_lan.namespace_identity(descriptor)
        if inode == original:
            raise RuntimeFailure('Supervisor created the original host namespace; refusing entry')
        result['namespace_identity'] = {'st_dev': inode[0], 'st_ino': inode[1]}
        atomic_json(root/'supervisor.json', result)
        await runner.run(['ip', 'netns', 'exec', namespace, 'ip', 'link', 'set', 'lo', 'up'], timeout=5)
        log = (root/'harness.log').open('xb')
        environment = {**os.environ, 'PYTHONPATH': str(group.PROJECT)}
        current, held = node.stat(), os.fstat(descriptor)
        if (current.st_dev, current.st_ino) != inode or (held.st_dev, held.st_ino) != inode:
            raise RuntimeFailure('Supervisor parent namespace changed before launch')
        current_host = Path('/proc/self/ns/net').stat()
        if (group.isolated_lan.namespace_identity(original_descriptor) != original
                or (current_host.st_dev, current_host.st_ino) != original):
            raise RuntimeFailure('Supervisor original host namespace changed before launch')
        process = await asyncio.create_subprocess_exec('/usr/bin/nsenter', f'--net=/proc/self/fd/{descriptor}', '--',
            sys.executable, str(HERE.with_name('check_native_grouping.py')), '--parent-namespace', namespace,
            '--original-netns-fd', str(original_descriptor), '--zone-faults',
            *( ['--minimum-policy'] if minimum_policy else []),
            env=environment, stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=asyncio.subprocess.STDOUT,
            pass_fds=(descriptor, original_descriptor))
        try:
            await asyncio.wait_for(process.wait(), 280)
        except asyncio.TimeoutError:
            timed_out = True
            raise RuntimeFailure('Native group harness exceeded its bounded runtime') from None
        result['harness_exit_code'] = process.returncode
        inner = json.loads(INNER_RESULT.read_text())
        if result.get('native_lab') and inner.get('native_lab') != result['native_lab']:
            raise RuntimeFailure('Inner harness did not retain the exact clean lab admission')
        if inner['started_at'] < result['started_at']:
            raise RuntimeFailure('Inner group report is stale')
        result['harness_report'] = str(INNER_RESULT)
        if minimum_policy:
            if (inner.get('minimum_policy') is not True
                    or not group.load_minimum_coverage_module().valid_receipt(inner.get('frozen_worker_timing'))):
                raise RuntimeFailure('Inner gate did not retain actual production minimum timing')
            result['minimum_policy'] = True
            result['frozen_worker_timing'] = inner['frozen_worker_timing']
        faults = inner.get('zone_faults', {})
        phases = faults.get('faults', [])
        result['harness_passed'] = (inner.get('passed') is True and process.returncode == 0
            and inner.get('mode') == 'zone_faults' and faults.get('passed') is True
            and len(phases) == 2 and [phase.get('role') for phase in phases] == ['owntone', 'audio']
            and all(phase.get('passed') is True and phase.get('signal_sent') is True
                    and phase.get('held_cgroup_thawed') is True and phase.get('owned_descriptors_closed') is True
                    and phase.get('original_stream_resumed') is False for phase in phases)
            and faults.get('untouched', {}).get('failure') is None
            and faults.get('untouched', {}).get('original_capture_preserved') is True)
        result['harness_failure'] = inner.get('failure')
        if evidence := inner.get('artifacts', {}).get('failure_diagnostics'):
            result['harness_failure_diagnostics'] = evidence
        if evidence_error := inner.get('artifact_errors', {}).get('failure_diagnostics'):
            result['harness_failure_diagnostics_error'] = evidence_error
    except BaseException as exc:
        result['failure'] = {'type': type(exc).__name__, 'message': str(exc)}
    finally:
        if process and process.returncode is None:
            try:
                await stop_child(process, grace=70 if timed_out else 15)
            except Exception as exc:
                errors.append(f'harness child: {type(exc).__name__}')
        if log:
            log.close()
        if inode is not None:
            try:
                manifest = json.loads((group.STATE/'ownership.json').read_text())
                if not result.get('boot_id') or boot_id() != result['boot_id']:
                    raise RuntimeFailure('Supervisor boot changed; preserve parent for exact recovery')
                if manifest['installation_id'] != result['installation_id'] or manifest['processes'] or manifest['networks']:
                    raise RuntimeFailure('Candidate ownership remains; preserve parent for exact recovery')
                if result.get('native_lab') and group.native_lab_admission(manifest, 'zone_faults') != result['native_lab']:
                    raise RuntimeFailure('Clean lab admission changed before parent cleanup')
                current, held = node.stat(), os.fstat(descriptor)
                if (current.st_dev, current.st_ino) != inode or (held.st_dev, held.st_ino) != inode:
                    raise RuntimeFailure('Parent namespace changed; refusing cleanup')
                pids = await runner.run(['ip', 'netns', 'pids', namespace], timeout=5)
                if pids.stdout.strip():
                    raise RuntimeFailure('Unexpected PIDs remain in the parent namespace')
                links = await runner.json(['ip', 'netns', 'exec', namespace, 'ip', '-j', 'link'])
                if any(item['ifname'] != 'lo' for item in links):
                    raise RuntimeFailure('Inner LAN remains; preserve parent for exact fixture recovery')
                await runner.run(['ip', 'netns', 'delete', namespace], timeout=5)
                result['cleanup']['parent_namespace_deleted'] = True
            except Exception as exc:
                errors.append(f'parent cleanup: {type(exc).__name__}: {exc}')
        for name, handle in [('parent', descriptor), ('original', original_descriptor)]:
            if handle is not None:
                try:
                    os.close(handle)
                except OSError as exc:
                    errors.append(f'{name} namespace descriptor cleanup: {type(exc).__name__}')
        if baseline is not None:
            try:
                if await group.observation.base.host_snapshot() != baseline or group.legacy_snapshot() != legacy:
                    raise RuntimeFailure('Original host or legacy deployment differs from its baseline')
                group.observation.base.closed_slot()
                result['cleanup']['original_host_and_legacy_preserved'] = True
            except Exception as exc:
                errors.append(f'original host verification: {type(exc).__name__}: {exc}')
        if result.get('native_lab'):
            try:
                manifest = json.loads((group.STATE/'ownership.json').read_text())
                if group.native_lab_admission(manifest, 'zone_faults') != result['native_lab']:
                    raise RuntimeFailure('Clean lab admission changed after cleanup')
                result['cleanup']['native_lab_preserved'] = True
            except Exception as exc:
                errors.append(f'clean lab verification: {type(exc).__name__}: {exc}')
        result['cleanup_errors'] = errors
        result['passed'] = (result.get('harness_passed') is True and not errors
                            and result['cleanup'].get('parent_namespace_deleted') is True
                            and result['cleanup'].get('original_host_and_legacy_preserved') is True)
        result['finished_at'] = datetime.now(timezone.utc).isoformat()
        atomic_json(RESULT, result)
        if root.is_dir():
            atomic_json(root/'supervisor.json', result)
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--minimum-policy', action='store_true', help='Run the original gate at actual production H140/B40')
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(minimum_policy=args.minimum_policy)))
