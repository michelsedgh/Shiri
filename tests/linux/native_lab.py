"""Explicit, boot-bound admission for the independently installed clean Linux lab.

The default imports nothing from the lab and reads no privileged files. A root
supervisor opts in with SHIRI_NATIVE_LAB_PROFILE; nonroot synthetic producers
receive no such variable and retain the existing harmless import path. The
external VM controller proves transport isolation and stages this profile.
This module never creates resources, stops services, or repairs installation
state. Ordinary legacy harness admission remains unchanged in its callers.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
from uuid import UUID

from shiri.runtime.bind_policy import trusted_file
from shiri.runtime.identities import DaemonIdentities
from shiri.runtime.system import RuntimeFailure

ENVIRONMENT = 'SHIRI_NATIVE_LAB_PROFILE'
PROFILE_ROOT = Path('/etc/shiri-rehearsal')
MARKER = Path('/etc/shiri-rehearsal-identity.json')
MODES = {'grouping', 'zone_faults', 'speech_stress', 'latency_probe', 'bluetooth_route', 'finite_speech', 'music_minimum', 'music_soak'}
REQUIRED_SOURCE = {
    'tests/linux/native_lab.py', 'tests/linux/native_lab_audio.py',
    'tests/linux/native_lab_observation.py', 'tests/linux/check_native_grouping.py',
    'tests/linux/run_native_grouping.py', 'tests/linux/run_native_zone_faults.py',
    'tests/linux/run_native_speech_stress.py', 'tests/linux/run_native_latency_probe.py',
    'tests/linux/run_native_bluetooth_route.py', 'tests/linux/check_native_bluetooth_route.py',
    'tests/linux/native_bluetooth_route.py', 'tests/linux/check_bluealsa_private_bus.py',
    'tests/linux/native_zone_faults.py', 'tests/linux/native_latency_probe.py',
    'tests/linux/native_speech_stress.py', 'tests/linux/native_speech_reference.py',
    'tests/linux/loopback_capture_probe.py', 'tests/linux/isolated_group_lan.py',
    'tests/linux/group_failure_evidence.py', 'tests/native/private_bluealsa_exec.c',
    'shiri/__init__.py', 'shiri/runtime/broker.py', 'shiri/runtime/system.py',
}
REQUIRED_BINARIES = {
    'bin/nqptp', 'bin/shairport-sync', 'bin/bluealsad', 'sbin/airptpd',
    'sbin/avahi-daemon', 'sbin/owntone', 'libexec/shiri-bind-policy',
    'libexec/shiri-pcm-exec', 'share/shiri/backends.json',
    'share/shiri/bluealsa.json', 'share/shiri/runtime-helpers.json',
}
MAX_FILE = 32 * 1024 * 1024


def require(value, message):
    if not value:
        raise RuntimeFailure(message)


def signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_uid, info.st_mode, info.st_nlink)


def read_file(path, *, private=False, maximum=MAX_FILE):
    """Inspect actual held root-owned bytes and reject replacement while reading."""
    path = trusted_file(Path(path))
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        require(stat.S_ISREG(before.st_mode) and before.st_uid == 0 and before.st_nlink == 1
                and not before.st_mode & (0o077 if private else 0o6022)
                and 0 < before.st_size <= maximum, 'Lab file has unsafe ownership, permissions or size')
        chunks, size = [], 0
        while chunk := os.read(descriptor, 65536):
            size += len(chunk)
            require(size <= maximum, 'Lab file exceeded its admitted size while reading')
            chunks.append(chunk)
        require(signature(before) == signature(os.fstat(descriptor)) == signature(path.lstat())
                and size == before.st_size, 'Lab file changed while its actual bytes were inspected')
        return b''.join(chunks), signature(before)
    finally:
        os.close(descriptor)


def json_object(data):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'Lab JSON contains a duplicate field')
            result[key] = value
        return result
    try:
        value = json.loads(data, object_pairs_hook=pairs)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise RuntimeFailure('Lab JSON is invalid') from exc
    require(isinstance(value, dict), 'Lab JSON must be an object')
    return value


def exact(value, names, label):
    require(isinstance(value, dict) and set(value) == set(names), f'Invalid lab {label} fields')


def canonical(value, *, below=None):
    require(isinstance(value, str) and value.startswith('/') and '//' not in value
            and not any(part in {'.', '..'} for part in value.split('/'))
            and not any(ord(char) < 32 for char in value), 'Lab path is not canonical')
    path = Path(value)
    require(str(path) == value and path != Path('/'), 'Lab path is not canonical')
    if below is not None:
        require(path.is_relative_to(below) and path != below, 'Lab path is outside its private admitted root')
    return path


def hashes(value, label):
    require(isinstance(value, dict) and 1 <= len(value) <= 10000, f'Invalid lab {label} file manifest')
    for name, digest in value.items():
        require(isinstance(name, str) and name and not name.startswith('/') and '\\' not in name
                and not any(part in {'', '.', '..'} for part in name.split('/'))
                and not name.endswith(('.pyc', '.pyo')) and '__pycache__' not in name.split('/')
                and isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest),
                f'Invalid lab {label} file identity')


def schema(data):
    exact(data, {'version', 'scope', 'vm', 'installation', 'source', 'installed', 'binaries', 'work_dir'}, 'profile')
    require(type(data['version']) is int and data['version'] == 1
            and data['scope'] == 'isolated-clean-vm', 'Unsupported native lab profile')
    vm = data['vm']
    exact(vm, {'uuid', 'hostname', 'mac', 'machine_id', 'boot_id', 'marker_sha256', 'original_netns'}, 'VM')
    for name in ('uuid', 'boot_id'):
        try:
            require(isinstance(vm[name], str) and str(UUID(vm[name])) == vm[name], 'Lab VM UUID is not canonical')
        except (ValueError, TypeError, AttributeError) as exc:
            raise RuntimeFailure('Lab VM UUID is not canonical') from exc
    require(isinstance(vm['hostname'], str) and re.fullmatch(r'shiri-rehearsal-[0-9a-f]{8}', vm['hostname'])
            and vm['hostname'] == 'shiri-rehearsal-'+vm['uuid'][:8], 'Lab hostname is not bound to its VM UUID')
    require(isinstance(vm['mac'], str) and re.fullmatch(r'02(?::[0-9a-f]{2}){5}', vm['mac']), 'Lab MAC is invalid')
    for name, pattern in (('machine_id', r'[0-9a-f]{32}'), ('marker_sha256', r'[0-9a-f]{64}')):
        require(isinstance(vm[name], str) and re.fullmatch(pattern, vm[name]), 'Lab machine/marker identity is invalid')
    exact(vm['original_netns'], {'st_dev', 'st_ino'}, 'original network namespace')
    require(all(type(x) is int and x > 0 for x in vm['original_netns'].values()), 'Lab namespace identity is invalid')
    installation = data['installation']
    exact(installation, {'id', 'state_dir', 'run_dir', 'identities', 'identities_sha256'}, 'installation')
    try:
        require(isinstance(installation['id'], str) and str(UUID(installation['id'])) == installation['id'],
                'Lab installation UUID is invalid')
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuntimeFailure('Lab installation UUID is invalid') from exc
    require(installation['state_dir'] == '/var/lib/shiri-runtime' and installation['run_dir'] == '/run/shiri'
            and installation['identities'] == '/etc/shiri/daemon-identities.json',
            'Lab must use the real installed daemon ownership paths')
    hashes({'identities': installation['identities_sha256']}, 'daemon identities')
    for name in ('source', 'installed', 'binaries'):
        exact(data[name], {'root', 'files'}, name)
        canonical(data[name]['root'])
        hashes(data[name]['files'], name)
    canonical(data['source']['root'], below=Path('/opt/shiri-rehearsal-lab'))
    installed = canonical(data['installed']['root'], below=Path('/opt/shiri/venv/lib'))
    require(installed.name == 'site-packages', 'Lab installed Python root is not the installed virtual environment')
    require(data['binaries']['root'] == '/opt/shiri', 'Lab binaries must be the real installed prefix')
    require(REQUIRED_SOURCE <= set(data['source']['files']), 'Lab whole-tree manifest omits a harness dependency')
    require(REQUIRED_BINARIES <= set(data['binaries']['files']), 'Lab binary manifest omits a maintained backend/helper')
    package = {name: digest for name, digest in data['source']['files'].items() if name.startswith('shiri/')}
    require(package and data['installed']['files'] == package, 'Staged production bytes differ from the installed package manifest')
    require(data['work_dir'] == '/var/lib/shiri-rehearsal-native-group-review', 'Lab work directory is not private')


def source_inventory(root):
    files, directories = set(), set()
    for path in root.rglob('*'):
        info = path.lstat()
        require(info.st_uid == 0 and not info.st_mode & 0o022
                and (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)),
                'Lab source tree contains a linked, writable or foreign entry')
        target = directories if stat.S_ISDIR(info.st_mode) else files
        target.add(str(path.relative_to(root)))
    return files, directories


def boot_identity():
    machine, _ = read_file('/etc/machine-id', maximum=128)
    instance, _ = read_file('/var/lib/cloud/data/instance-id', maximum=128)
    return {'hostname': socket.gethostname(), 'machine_id': machine.decode().strip(),
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'uuid': instance.decode().strip()}


def namespace_identity(descriptor=None):
    info = os.fstat(descriptor) if descriptor is not None else Path('/proc/self/ns/net').stat()
    return {'st_dev': info.st_dev, 'st_ino': info.st_ino}


def installed_services_stopped():
    status = subprocess.run(['/usr/bin/systemctl', 'show', 'shiri-api.service', 'shiri-runtime.service',
                             '--property=LoadState', '--property=ActiveState', '--property=SubState',
                             '--property=MainPID', '--property=ControlPID'],
                            capture_output=True, text=True, timeout=10, check=False,
                            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
    require(status.returncode == 0 and len(status.stdout) <= 16384 and not status.stderr,
            'Installed service quiescence could not be inspected')
    paragraphs = [part for part in status.stdout.strip().split('\n\n') if part.strip()]
    require(len(paragraphs) == 2, 'Installed service quiescence lacks both actual units')
    for paragraph in paragraphs:
        try:
            fields = dict(line.split('=', 1) for line in paragraph.splitlines())
        except ValueError as exc:
            raise RuntimeFailure('Installed service quiescence is malformed') from exc
        require(fields == {'LoadState': 'loaded', 'ActiveState': 'inactive', 'SubState': 'dead',
                           'MainPID': '0', 'ControlPID': '0'}, 'Installed services must be stopped before the lab run')


def marker_mac_present(mac):
    status = subprocess.run(['/usr/sbin/ip', '-j', 'link', 'show'], capture_output=True, text=True,
                            timeout=5, check=False, env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
    require(status.returncode == 0 and len(status.stdout) <= 16384 and not status.stderr,
            'Clean VM NIC identity could not be inspected')
    try:
        links = json.loads(status.stdout)
    except (ValueError, TypeError) as exc:
        raise RuntimeFailure('Clean VM NIC identity is invalid') from exc
    require(isinstance(links, list) and sum(isinstance(link, dict) and link.get('address') == mac for link in links) == 1,
            'Seeded clean VM NIC MAC is absent or ambiguous')


class NativeLab:
    def __init__(self, profile):
        require(sys.platform == 'linux' and os.geteuid() == 0, 'Native lab profile requires explicit Linux root admission')
        self.profile = canonical(str(profile), below=PROFILE_ROOT)
        self.bytes, self.file_identity = read_file(self.profile, private=True, maximum=1024*1024)
        self.data = json_object(self.bytes)
        schema(self.data)
        self.digest = hashlib.sha256(self.bytes).hexdigest()
        self.state = Path(self.data['installation']['state_dir'])
        self.run = Path(self.data['installation']['run_dir'])
        self.identities = Path(self.data['installation']['identities'])
        self.binaries = Path(self.data['binaries']['root'])
        self.project = Path(self.data['source']['root'])
        self.work = Path(self.data['work_dir'])

    def receipt(self):
        return {'scope': 'isolated-clean-vm', 'profile': str(self.profile), 'profile_sha256': self.digest,
                'vm_uuid': self.data['vm']['uuid'], 'boot_id': self.data['vm']['boot_id'],
                'installation_id': self.data['installation']['id'],
                'legacy_process_verification': 'not_applicable_clean_vm',
                'transport_isolation_scope': 'External owned VM controller; inner disconnected namespace separately verified'}

    def verify(self, *, original_netns_fd=None, project=None):
        require(sys.platform == 'linux' and os.geteuid() == 0, 'Native lab admission requires Linux root')
        require(sys.dont_write_bytecode, 'Clean lab requires PYTHONDONTWRITEBYTECODE=1 before importing its staged tree')
        current, identity = read_file(self.profile, private=True, maximum=1024*1024)
        require(current == self.bytes and identity == self.file_identity, 'Exact native lab profile changed')
        vm = self.data['vm']
        marker, _ = read_file(MARKER, private=True, maximum=16384)
        require(hashlib.sha256(marker).hexdigest() == vm['marker_sha256'], 'Lab VM marker bytes changed')
        actual = json_object(marker)
        require(actual.get('vm_uuid') == vm['uuid'] and actual.get('hostname') == vm['hostname']
                and actual.get('mac') == vm['mac'],
                'Lab VM marker/hostname does not match actual admission')
        require(boot_identity() == {name: vm[name] for name in ('hostname', 'machine_id', 'boot_id', 'uuid')},
                'Lab boot, machine or seeded VM instance changed')
        require(namespace_identity(original_netns_fd) == vm['original_netns'],
                'Lab original network namespace differs from the admitted boot')
        if original_netns_fd is None:
            marker_mac_present(vm['mac'])
        if project is not None:
            require(Path(project) == self.project, 'Harness imported a different staged source tree')
        require(Path(sys.executable) == self.binaries/'venv/bin/python', 'Lab must run the actual installed Python environment')
        total = 0
        for tree in ('source', 'installed', 'binaries'):
            root = Path(self.data[tree]['root'])
            for name, digest in self.data[tree]['files'].items():
                contents, _ = read_file(root/name)
                total += len(contents)
                require(total <= 512*1024*1024, 'Lab manifest exceeds its bounded complete byte inspection')
                require(hashlib.sha256(contents).hexdigest() == digest, f'Lab {tree} actual bytes changed: {name}')
            if tree == 'source':
                actual_files, actual_dirs = source_inventory(root)
                expected_dirs = {str(parent) for name in self.data[tree]['files']
                                 for parent in Path(name).parents if parent != Path('.')}
                require(actual_files == set(self.data[tree]['files']) and actual_dirs == expected_dirs,
                        'Lab whole source tree has unexpected or missing files/directories')
        saved, _ = read_file(self.identities, private=True, maximum=65536)
        require(hashlib.sha256(saved).hexdigest() == self.data['installation']['identities_sha256'],
                'Installed daemon identity map bytes changed')
        identities = DaemonIdentities(self.identities).load()
        require(identities.installation_id == self.data['installation']['id']
                and identities.runtime_state_dir == self.state and identities.runtime_dir == self.run,
                'Lab daemon identities belong to a different installation or owning paths')
        installed_services_stopped()

    def admit(self, manifest, *, mode, original_netns_fd=None, project=None):
        require(mode in MODES, 'This standalone harness has no admitted clean-VM network fixture')
        self.verify(original_netns_fd=original_netns_fd, project=project)
        exact(manifest, {'version', 'installation_id', 'networks', 'processes'}, 'runtime ownership')
        require(type(manifest['version']) is int and manifest['version'] == 1
                and manifest['installation_id'] == self.data['installation']['id']
                and manifest['networks'] == {} and manifest['processes'] == {},
                'Real installed runtime ownership must be intact and empty')
        return self.receipt()

    def protected_snapshot(self):
        """Protect real unused endpoints without inventing a legacy process."""
        status = {f'{direction}/sub{slot}': Path(f'/proc/asound/Loopback/{direction}/sub{slot}/status').read_text().strip()
                  for direction in ('pcm0p', 'pcm0c', 'pcm1p', 'pcm1c') for slot in (0, 2)}
        require(all(value == 'closed' for value in status.values()), 'Clean lab protected Loopback0/2 is in use')
        require(Path('/proc/sys/kernel/random/boot_id').read_text().strip() == self.data['vm']['boot_id'],
                'Protected lab PCM belongs to a different boot')
        return {**self.receipt(), 'protected_slot0_2_state': status}


def from_environment():
    selected = os.environ.get(ENVIRONMENT)
    return NativeLab(Path(selected)) if selected is not None else None
