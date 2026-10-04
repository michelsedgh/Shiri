#!/usr/bin/env python3
"""Supervise the private Bluetooth production route inside a disconnected parent namespace.

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
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import stat
from uuid import uuid4

from shiri.runtime.system import Runner, RuntimeFailure, atomic_json, boot_id, root_directory
from shiri.runtime.bind_policy import trusted_file

HERE = Path(__file__).resolve()
spec = importlib.util.spec_from_file_location('supervised_private_bluetooth_group', HERE.with_name('check_native_grouping.py'))
group = importlib.util.module_from_spec(spec)
spec.loader.exec_module(group)
fixture_spec = importlib.util.spec_from_file_location('supervised_private_bluetooth_fixture', HERE.with_name('check_native_bluetooth_route.py'))
fixture = importlib.util.module_from_spec(fixture_spec)
sys.modules[fixture_spec.name] = fixture
fixture_spec.loader.exec_module(fixture)
RESULT = Path('/tmp/shiri-v2-native-bluetooth-route-supervisor-result.json')
if group.NATIVE_LAB is not None:
    RESULT = group.WORK/RESULT.name
INNER_RESULT = fixture.RESULT

SOURCE_FILES = (
    'tests/linux/native_lab.py', 'tests/linux/native_lab_audio.py', 'tests/linux/native_lab_observation.py',
    'tests/linux/run_native_bluetooth_route.py', 'tests/linux/native_bluetooth_route.py',
    'tests/linux/check_native_bluetooth_route.py', 'tests/linux/check_bluealsa_private_bus.py',
    'tests/native/private_bluealsa_exec.c', 'tests/linux/check_native_grouping.py',
    'tests/linux/loopback_capture_probe.py', 'tests/linux/native_zone_faults.py',
    'shiri/runtime/bluealsa.py', 'shiri/runtime/bluetooth_output.py', 'shiri/runtime/pcm_transport.py',
    'shiri/runtime/broker.py', 'shiri/runtime/units.py',
    'shiri/runtime/audio.py', 'shiri/runtime/native.py', 'shiri/runtime/timing.py',
    'shiri/runtime/configuration.py',
)


def source_receipts(project):
    """Name source bytes, not cached bytecode; external staging pins the tree."""
    result = {}
    for relative in SOURCE_FILES:
        path = trusted_file(Path(project)/relative)
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                    or info.st_mode & 0o022 or not 0 < info.st_size <= 1024*1024):
                raise RuntimeFailure('Bluetooth source is not a bounded immutable root-owned file')
            chunks, size = [], 0
            while data := os.read(descriptor, 65536):
                size += len(data)
                if size > 1024*1024:
                    raise RuntimeFailure('Bluetooth source changed beyond its admitted size bound')
                chunks.append(data)
            after, current = os.fstat(descriptor), path.lstat()
            def signature(value):
                return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns
            if signature(info) != signature(after) or signature(info) != signature(current) or size != info.st_size:
                raise RuntimeFailure('Bluetooth source changed while its proof bytes were read')
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


def valid_inner(inner, digest, exit_code):
    """Require route evidence, not merely a process exit or generic pass flag."""
    transitions = inner.get('transitions', {})
    labels = {'final_volume50', 'final_volume100', 'duck_and_voice', 'silent_restore', 'resumed_voice', 'closed_restore'}
    cleanup = inner.get('cleanup', {})
    required_cleanup = {f'producer_{identifier}_stopped' for identifier in (group.A, group.B)} | {
        'b_capture_null', 'disposable_rooms_deleted', 'api_stopped', 'api_client_closed', 'broker_closed',
        'bluetooth_lease_released', 'private_daemon_closed', 'private_directory_removed',
        'isolated_lan_closed', 'empty_manifest', 'slot7_closed', 'host_and_legacy_preserved'}
    sbc = inner.get('sbc_capture', {})
    bluetooth = inner.get('per_buffer_evidence', {}).get('bluetooth', {})
    untouched = inner.get('per_buffer_evidence', {}).get('untouched', {})
    private_daemon = inner.get('private_daemon', {})
    encoder = private_daemon.get('encoder_stop_boundary', {})
    transport = private_daemon.get('transport_stop_boundary', {}) or {}
    eof = transport.get('eof', {})
    boundaries = [encoder.get('requested_monotonic_ns'), encoder.get('verified_monotonic_ns'),
                  transport.get('requested_monotonic_ns'), transport.get('producer_descriptor_closed_monotonic_ns'),
                  eof.get('observed_monotonic_ns'), transport.get('retired_monotonic_ns'),
                  transport.get('receiver_descriptor_closed_monotonic_ns')]
    stopped = (type(encoder.get('pid')) is int and encoder['pid'] > 0
               and type(encoder.get('returncode')) is int
               and all(type(value) is int and value > 0 for value in boundaries)
               and boundaries == sorted(boundaries)
               and eof.get('packets') == sbc.get('packets')
               and eof.get('decoded_frames') == sbc.get('decoded_frames'))
    return (exit_code == 0 and inner.get('passed') is True
            and inner.get('minimum_policy') is True
            and group.load_minimum_coverage_module().valid_receipt(inner.get('frozen_worker_timing'))
            and inner.get('same_mac_lease_denied') is True
            and inner.get('private_daemon', {}).get('binary_sha256') == digest
            and inner.get('private_daemon', {}).get('host_bus_used') is False
            and inner.get('private_daemon', {}).get('physical_adapter_used') is False
            and stopped
            and labels <= set(transitions)
            and all(transitions[label].get('passed') is True for label in labels)
            and inner.get('speech', {}).get('passed') is True
            and inner.get('takeover', {}).get('passed') is True
            and inner.get('end', {}).get('passed') is True
            and 'error' in sbc and sbc['error'] is None
            and type(sbc.get('packets')) is int and 0 < sbc['packets'] <= fixture.route.MAX_PACKETS
            and type(sbc.get('decoded_frames')) is int and sbc['decoded_frames'] > 0
            and 'error' in bluetooth and bluetooth['error'] is None
            and type(bluetooth.get('checked_blocks')) is int and bluetooth['checked_blocks'] > 0
            and 'failed_block' in untouched and untouched['failed_block'] is None
            and type(untouched.get('untouched_reference_blocks')) is int and untouched['untouched_reference_blocks'] > 0
            and required_cleanup <= set(cleanup)
            and not inner.get('cleanup_errors') and not inner.get('observation_cleanup_errors')
            and all(value is True for value in cleanup.values()))


async def run(binary, digest):
    result = {'started_at': datetime.now(timezone.utc).isoformat(), 'passed': False,
              'scope': 'Actual private BlueALSA/SBC production route; no RF/host bus, original zone/host preserved', 'cleanup': {}}
    runner, process, log, descriptor, original_descriptor, inode, baseline = Runner(), None, None, None, None, None, None
    identifier = uuid4().hex
    namespace = f'shiri_group_run_{identifier[:8]}'
    root, node = group.WORK/f'bluetooth-supervisor-{identifier}', Path('/run/netns')/namespace
    errors, timed_out = [], False
    try:
        if sys.platform != 'linux' or os.geteuid() != 0 or not boot_id():
            raise RuntimeFailure('Run supervised native grouping as Linux root with a known boot')
        if os.environ.get('SHIRI_PRIVATE_BLUETOOTH_ROUTE_TEST') != '1':
            raise RuntimeFailure('Explicit SHIRI_PRIVATE_BLUETOOTH_ROUTE_TEST=1 is required')
        binary, digest = fixture.route.private.validated_binary(Path(binary), digest)
        result['binary_sha256'] = digest
        result['source_files'] = source_receipts(group.PROJECT)
        result['source_proof_scope'] = 'Exact named source bytes; reviewed whole-tree admission belongs to the external immutable staging manifest'
        if group.NATIVE_LAB is None:
            raise RuntimeFailure('An explicit native lab profile is required')
        manifest = json.loads((group.STATE/'ownership.json').read_text())
        result['native_lab'] = group.native_lab_admission(manifest, 'bluetooth_route')
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
            sys.executable, str(HERE.with_name('check_native_bluetooth_route.py')), '--parent-namespace', namespace,
            '--original-netns-fd', str(original_descriptor), '--binary', str(binary), '--expected-sha256', digest,
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
        result['harness_passed'] = valid_inner(inner, digest, process.returncode)
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
                if result.get('native_lab') and group.native_lab_admission(manifest, 'bluetooth_route') != result['native_lab']:
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
        if result.get('source_files'):
            try:
                if source_receipts(group.PROJECT) != result['source_files']:
                    raise RuntimeFailure('Bluetooth source proof changed during the isolated experiment')
                result['cleanup']['source_files_preserved'] = True
            except Exception as exc:
                errors.append(f'source proof: {type(exc).__name__}')
        if result.get('native_lab'):
            try:
                manifest = json.loads((group.STATE/'ownership.json').read_text())
                if group.native_lab_admission(manifest, 'bluetooth_route') != result['native_lab']:
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
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--expected-sha256', required=True)
    options = parser.parse_args()
    raise SystemExit(asyncio.run(run(options.binary, options.expected_sha256)))
