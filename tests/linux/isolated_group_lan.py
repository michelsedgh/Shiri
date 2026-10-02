"""Owned, disconnected kernel LAN for the manual native-group audio proof.

Importing this file creates no resources. The bridge has only one test veth,
whose peer is an independently pinned gateway namespace. It never includes a
physical interface, changes a host default route, or forwards to the house LAN.
The foreground DHCP process remains a child of the supervised test harness.
"""
from __future__ import annotations

import asyncio
import ctypes
import fcntl
import ipaddress
import os
from pathlib import Path
import re
import shutil
import sys
from uuid import uuid4

from shiri.runtime.system import Runner, RuntimeFailure, atomic_json, boot_id, process_birth

SUBNET = ipaddress.IPv4Network('198.18.254.0/24')
NSFS_MAGIC = 0x6E736673
CLONE_NEWNET = 0x40000000
# Linux UAPI linux/nsfs.h: _IO(NSIO=0xb7, 0x3). The nsfs handler
# returns the held namespace's type; it does not require CAP_SYS_PTRACE.
NS_GET_NSTYPE = 0xB703


def namespace_identity(descriptor):
    """Return the genuine held Linux network namespace's device/inode."""
    if sys.platform != 'linux' or type(descriptor) is not int or descriptor < 3:
        raise RuntimeFailure('Expected an inherited network namespace descriptor')
    try:
        info = os.fstat(descriptor)
        function = ctypes.CDLL(None, use_errno=True).fstatfs
        function.argtypes, function.restype = [ctypes.c_int, ctypes.c_void_p], ctypes.c_int
        result = ctypes.create_string_buffer(256)
        if function(descriptor, result):
            raise OSError(ctypes.get_errno(), 'Cannot identify held namespace filesystem')
        if (ctypes.c_long.from_buffer(result).value != NSFS_MAGIC
                or fcntl.ioctl(descriptor, NS_GET_NSTYPE) != CLONE_NEWNET
                or info.st_dev <= 0 or info.st_ino <= 0):
            raise RuntimeFailure('Descriptor is not a genuine held network namespace')
        return info.st_dev, info.st_ino
    except OSError as exc:
        raise RuntimeFailure('Cannot verify inherited network namespace descriptor') from exc


def disconnected_parent(original_netns_fd, parent_namespace):
    """Verify the supervisor's held host and the exact named current parent."""
    if not isinstance(parent_namespace, str) or not re.fullmatch(r'shiri_group_run_[0-9a-f]{8}', parent_namespace):
        raise RuntimeFailure('Test LAN requires an exact supervised parent namespace')
    original = namespace_identity(original_netns_fd)
    current_fd = named_fd = None
    try:
        # /proc/self/ns/net is a namespace magic link; follow only our own
        # process, never another PID. The named mount must be an exact inode.
        current_fd = os.open('/proc/self/ns/net', os.O_RDONLY | os.O_CLOEXEC)
        named_fd = os.open(Path('/run/netns')/parent_namespace, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        current, named = namespace_identity(current_fd), namespace_identity(named_fd)
        if current == original or current != named:
            raise RuntimeFailure('Test LAN requires a fresh disconnected parent network namespace')
        return {'original': {'st_dev': original[0], 'st_ino': original[1]},
                'parent': {'name': parent_namespace, 'st_dev': current[0], 'st_ino': current[1]}}
    finally:
        for descriptor in (named_fd, current_fd):
            if descriptor is not None:
                os.close(descriptor)


class IsolatedLan:
    def __init__(self, root: Path):
        self.root, self.runner = root, Runner()
        self.identifier = uuid4().hex
        tag = self.identifier[:8]
        self.interface, self.host_veth = f'sgp{tag}', f'sgh{tag}'
        self.peer_veth, self.namespace = f'sgn{tag}', f'shiri_group_gateway_{tag}'
        self.alias = f'shiri-native-group:{self.identifier}'
        self.mac = '02:'+':'.join(self.identifier[index:index+2] for index in range(0, 10, 2))
        self.process = self.log = None
        self.record = {'version': 1, 'boot_id': boot_id(), 'identifier': self.identifier,
                       'subnet': str(SUBNET), 'links': {}, 'namespace': None, 'process': None}

    def save(self):
        atomic_json(self.root/'isolated-lan.json', self.record)

    async def command(self, *args):
        return await self.runner.run(list(args), timeout=5)

    async def links(self, namespace=None):
        prefix = ['ip', 'netns', 'exec', namespace] if namespace else []
        return await self.runner.json(prefix+['ip', '-j', '-d', 'link', 'show'])

    async def remember_link(self, name, kind, namespace=None, *, tagged=True):
        candidates = [item for item in await self.links(namespace) if item['ifname'] == name]
        if len(candidates) != 1 or candidates[0].get('linkinfo', {}).get('info_kind') != kind:
            raise RuntimeFailure('Test LAN created an unexpected kernel link')
        item = candidates[0]
        if tagged and item.get('ifalias') != self.alias:
            raise RuntimeFailure('Test LAN link lost its unique creation alias')
        self.record['links'][name] = {'ifindex': item['ifindex'], 'kind': kind,
                                     'address': item['address'], 'namespace': namespace, 'tagged': tagged}
        self.save()

    async def verify_link(self, name):
        expected = self.record['links'][name]
        candidates = [item for item in await self.links(expected['namespace']) if item['ifname'] == name]
        if len(candidates) != 1:
            raise RuntimeFailure('Test LAN link disappeared; preserve its identity for inspection')
        item = candidates[0]
        if (item.get('ifindex') != expected['ifindex'] or item.get('address') != expected['address']
                or item.get('ifalias', '') not in ({self.alias} if expected['tagged'] else {'', self.alias})
                or item.get('linkinfo', {}).get('info_kind') != expected['kind']):
            raise RuntimeFailure('Test LAN link identity changed; refusing cleanup')
        return item

    async def start(self, *, original_netns_fd, parent_namespace):
        if os.geteuid() != 0 or not shutil.which('dnsmasq'):
            raise RuntimeFailure('Isolated kernel LAN requires root and installed dnsmasq-base')
        self.root.mkdir(mode=0o700)
        self.save()
        links = await self.links()
        proof = disconnected_parent(original_netns_fd, parent_namespace)
        if not self.record['boot_id'] or any(item['ifname'] != 'lo' for item in links):
            raise RuntimeFailure('Test LAN requires a fresh disconnected parent network namespace')
        self.record['namespace_admission'] = proof
        self.save()
        if any(item['ifname'] in {self.interface, self.host_veth, self.peer_veth} for item in links):
            raise RuntimeFailure('Test LAN names already exist; refusing adoption')
        if (Path('/run/netns')/self.namespace).exists():
            raise RuntimeFailure('Test gateway namespace already exists; refusing adoption')
        routes = await self.runner.json(['ip', '-j', '-4', 'route', 'show', 'table', 'all'])
        for route in routes:
            if route.get('dst', 'default') == 'default':
                continue
            if ipaddress.IPv4Network(route['dst'], strict=False).overlaps(SUBNET):
                raise RuntimeFailure('Test subnet overlaps a current host route')
        await self.command('ip', 'link', 'add', self.interface, 'address', self.mac,
                           'alias', self.alias, 'type', 'bridge')
        await self.remember_link(self.interface, 'bridge', tagged=False)
        await self.command('ip', 'link', 'set', self.interface, 'alias', self.alias)
        await self.remember_link(self.interface, 'bridge')
        await self.command('ip', 'link', 'set', self.interface, 'up')
        await self.command('ip', 'addr', 'add', '198.18.254.2/24', 'dev', self.interface)
        await self.command('ip', 'netns', 'add', self.namespace)
        node = (Path('/run/netns')/self.namespace).stat()
        self.record['namespace'] = {'name': self.namespace, 'st_dev': node.st_dev, 'st_ino': node.st_ino}
        self.save()
        await self.command('ip', 'link', 'add', self.host_veth, 'alias', self.alias,
                           'type', 'veth', 'peer', 'name', self.peer_veth)
        await self.remember_link(self.host_veth, 'veth', tagged=False)
        await self.command('ip', 'link', 'set', self.host_veth, 'alias', self.alias)
        await self.remember_link(self.host_veth, 'veth')
        await self.command('ip', 'link', 'set', self.peer_veth, 'alias', self.alias)
        await self.command('ip', 'link', 'set', self.peer_veth, 'netns', self.namespace)
        await self.remember_link(self.peer_veth, 'veth', self.namespace)
        await self.command('ip', 'link', 'set', self.host_veth, 'master', self.interface)
        await self.command('ip', 'link', 'set', self.host_veth, 'up')
        for name in ('lo', self.peer_veth):
            await self.command('ip', 'netns', 'exec', self.namespace, 'ip', 'link', 'set', name, 'up')
        await self.command('ip', 'netns', 'exec', self.namespace, 'ip', 'addr', 'add',
                           '198.18.254.1/24', 'dev', self.peer_veth)
        self.log = (self.root/'dnsmasq.log').open('xb')
        args = ['ip', 'netns', 'exec', self.namespace, '/usr/sbin/dnsmasq',
                '--no-daemon', '--conf-file=/dev/null', '--port=0', '--no-hosts', '--no-resolv',
                '--user=root', '--group=root', '--bind-interfaces', f'--interface={self.peer_veth}',
                '--dhcp-authoritative', '--dhcp-range=198.18.254.40,198.18.254.90,255.255.255.0,5m',
                '--dhcp-option=option:router,198.18.254.1', '--dhcp-option=option:dns-server,198.18.254.1',
                f'--dhcp-leasefile={self.root}/leases', '--log-dhcp', '--log-facility=-']
        self.process = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.DEVNULL,
            stdout=self.log, stderr=asyncio.subprocess.STDOUT, env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL':'C'})
        self.record['process'] = {'pid': self.process.pid, 'birth': process_birth(self.process.pid), 'argv': args}
        self.save()
        await asyncio.sleep(.1)
        if self.process.returncode is not None:
            raise RuntimeFailure('Isolated DHCP server exited during startup')
        return self

    def evidence(self):
        return dict(self.record, scope='Disconnected bridge/veth DHCP/ARP kernel fixture; no house network connectivity')

    async def close(self):
        if not self.record['boot_id'] or boot_id() != self.record['boot_id']:
            raise RuntimeFailure('Test LAN boot changed; refusing cleanup')
        if self.process and self.process.returncode is None:
            # An unreaped direct child cannot be PID-reused. Check its birth as
            # an additional fence and wait before touching its namespace.
            if process_birth(self.process.pid) != self.record['process']['birth']:
                raise RuntimeFailure('Test DHCP child identity changed')
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 3)
            except asyncio.TimeoutError:
                self.process.kill()
                await asyncio.wait_for(self.process.wait(), 3)
        if self.log:
            self.log.close()
        namespace = self.record['namespace']
        if namespace is not None:
            node = (Path('/run/netns')/self.namespace).stat()
            if (node.st_dev, node.st_ino) != (namespace['st_dev'], namespace['st_ino']):
                raise RuntimeFailure('Test gateway namespace identity changed')
            pids = await self.runner.run(['ip', 'netns', 'pids', self.namespace], timeout=5)
            if pids.stdout.strip():
                raise RuntimeFailure('Unexpected processes remain in test gateway namespace')
        for name in tuple(self.record['links']):
            await self.verify_link(name)
        if self.interface in self.record['links']:
            children = [item for item in await self.links() if item.get('link_index') ==
                        self.record['links'][self.interface]['ifindex'] or
                        (item.get('master') in {self.interface, self.record['links'][self.interface]['ifindex']}
                         and item['ifname'] != self.host_veth)]
            if children:
                raise RuntimeFailure('Test bridge still has macvlan children; preserve owned fixture')
        if self.host_veth in self.record['links']:
            await self.command('ip', 'link', 'delete', self.host_veth)
            self.record['links'].pop(self.host_veth)
            self.record['links'].pop(self.peer_veth, None)
            self.save()
        if namespace is not None:
            await self.command('ip', 'netns', 'delete', self.namespace)
            self.record['namespace'] = None
            self.save()
        if self.interface in self.record['links']:
            await self.command('ip', 'link', 'delete', self.interface)
            self.record['links'].pop(self.interface)
            self.save()
        self.record['cleaned'] = True
        self.save()
