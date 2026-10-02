"""Bounded, permanently unprivileged observation of one receiver's kernel sockets.

Executed directly with system Python -I -S; imports are standard-library only.
The parent owns the unit/cgroup admission. This observer gets held pidfd/netns
handles and reads the receiver's own proc view without CAP_SYS_PTRACE.
"""
from __future__ import annotations

import argparse
import ctypes
import ipaddress
import json
import os
from pathlib import Path
import re
import select
import signal
import sys
import time

MAX_PROC_BYTES = 2 * 1024 * 1024
MAX_FDS = 4096


class ProbeFailure(RuntimeError):
    pass


def bounded_text(path):
    with Path(path).open('r', encoding='ascii') as stream:
        result = stream.read(MAX_PROC_BYTES + 1)
    if len(result) > MAX_PROC_BYTES:
        raise ProbeFailure('Receiver proc observation exceeded its bound')
    return result


def birth(pid, *, root=Path('/proc')):
    try:
        return bounded_text(root/str(pid)/'stat').rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError) as exc:
        raise ProbeFailure('Receiver exited or its process identity became unavailable') from exc


def sockets(pid, *, root=Path('/proc')):
    result = set()
    with os.scandir(root/str(pid)/'fd') as entries:
        for count, entry in enumerate(entries, 1):
            if count > MAX_FDS:
                raise ProbeFailure('Receiver descriptor observation exceeded its bound')
            if not entry.name.isdecimal():
                raise ProbeFailure('Receiver descriptor table is malformed')
            try:
                target = os.readlink(entry.path)
            except FileNotFoundError:
                continue  # A concurrently closed descriptor is not evidence.
            match = re.fullmatch(r'socket:\[([1-9][0-9]*)\]', target)
            if match:
                result.add(int(match[1]))
    return result


def listeners(text, family, *, port=7000):
    rows = []
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 10:
            raise ProbeFailure('Receiver kernel TCP table is malformed')
        try:
            address, encoded_port = fields[1].split(':')
            remote, remote_port = fields[2].split(':')
            local_port = int(encoded_port, 16)
            if local_port != port or fields[3] != '0A':
                continue
            if len(address) != (8 if family == 'ipv4' else 32) or not re.fullmatch(r'[0-9A-Fa-f]+', address):
                raise ValueError('Invalid address')
            # proc prints native-endian u32 words, including each IPv6 word.
            packed = b''.join(int(address[i:i+8], 16).to_bytes(4, sys.byteorder) for i in range(0, len(address), 8))
            decoded = str(ipaddress.ip_address(packed))
            if int(remote, 16) != 0 or int(remote_port, 16) != 0:
                raise ValueError('Connected LISTEN row')
            uid, inode = int(fields[7]), int(fields[9])
            if uid < 0 or inode <= 0:
                raise ValueError('Invalid socket identity')
        except (ValueError, IndexError) as exc:
            raise ProbeFailure('Receiver listening socket identity is malformed') from exc
        rows.append({'family': family, 'address': decoded, 'port': port, 'uid': uid, 'inode': inode})
    return rows


def verify_process(pid, expected_birth, namespace, *, root=Path('/proc')):
    if birth(pid, root=root) != expected_birth:
        raise ProbeFailure('Receiver MainPID was replaced')
    info = (root/str(pid)/'ns/net').stat()
    if (info.st_dev, info.st_ino) != namespace:
        raise ProbeFailure('Receiver left its admitted network namespace')


def sample(pid, expected_birth, namespace, uid, address, interface, *, root=Path('/proc')):
    verify_process(pid, expected_birth, namespace, root=root)
    devices = bounded_text(root/str(pid)/'net/dev').splitlines()[2:]
    names = {line.split(':', 1)[0].strip() for line in devices if ':' in line}
    if interface not in names or names - {'lo', interface}:
        raise ProbeFailure('Receiver interface view does not match its admitted LAN namespace')
    before = sockets(pid, root=root)
    rows = listeners(bounded_text(root/str(pid)/'net/tcp'), 'ipv4')
    try:
        tcp6 = bounded_text(root/str(pid)/'net/tcp6')
    except FileNotFoundError:
        tcp6 = None  # IPv6 may be disabled in this namespace/kernel.
    if tcp6 is not None:
        rows += listeners(tcp6, 'ipv6')
    after = sockets(pid, root=root)
    verify_process(pid, expected_birth, namespace, root=root)
    owned = [row for row in rows if row['inode'] in before & after]
    for row in owned:
        allowed = {'0.0.0.0', address} if row['family'] == 'ipv4' else {'::'}
        if row['uid'] != uid or row['address'] not in allowed:
            raise ProbeFailure('Receiver listener has unexpected credentials or bind address')
    # The receiver's advertised LAN address is IPv4. An IPv6-only or loopback
    # listener cannot establish readiness for that endpoint.
    return owned if any(row['family'] == 'ipv4' for row in owned) else []


def prctl(option, value):
    library = ctypes.CDLL(None, use_errno=True)
    function = library.prctl
    function.restype = ctypes.c_int
    if function(ctypes.c_int(option), ctypes.c_ulong(value), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0)):
        raise OSError(ctypes.get_errno(), 'Cannot confine receiver readiness observer')


def confine(uid, gid, parent):
    if sys.platform != 'linux' or os.geteuid() != 0 or uid <= 0 or gid <= 0:
        raise ProbeFailure('Receiver observer requires root admission and fixed nonroot credentials')
    prctl(38, 1)  # PR_SET_NO_NEW_PRIVS; no later executable may regain capabilities.
    os.setgroups([])
    os.setresgid(gid, gid, gid)
    os.setresuid(uid, uid, uid)
    # The decoder shares this UID. Keep it from writing our stdout pipe or
    # memory through /proc while this trusted observation is in progress.
    prctl(4, 0)  # PR_SET_DUMPABLE
    # A UID/GID transition clears PDEATHSIG; set it after the final transition.
    prctl(1, signal.SIGKILL)
    if os.getppid() != parent:
        raise ProbeFailure('Receiver observer owner exited during credential setup')
    status = dict(line.split(':', 1) for line in bounded_text('/proc/self/status').splitlines() if ':' in line)
    if (os.getresuid() != (uid, uid, uid) or os.getresgid() != (gid, gid, gid) or os.getgroups()
            or status.get('NoNewPrivs', '').strip() != '1'
            or any(int(status[name], 16) != 0 for name in ('CapEff', 'CapPrm', 'CapInh', 'CapAmb'))):
        raise ProbeFailure('Receiver observer did not permanently drop its authority')


def run(args):
    if (args.pid <= 1 or not re.fullmatch(r'[1-9][0-9]*', args.birth)
            or not 0 < args.timeout <= 15 or not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', args.interface)):
        raise ProbeFailure('Invalid receiver readiness admission')
    address = ipaddress.IPv4Address(args.address)
    if address.is_unspecified or address.is_loopback or address.is_multicast:
        raise ProbeFailure('Invalid advertised receiver LAN address')
    pid_info = bounded_text(f'/proc/self/fdinfo/{args.pidfd}')
    if f'Pid:\t{args.pid}\n' not in pid_info:
        raise ProbeFailure('Readiness handle does not identify the admitted MainPID')
    held = os.fstat(args.namespace_fd)
    namespace = (held.st_dev, held.st_ino)
    poll = select.poll()
    poll.register(args.pidfd, select.POLLIN)
    confine(args.uid, args.gid, args.parent)
    deadline = time.monotonic() + args.timeout
    while True:
        if time.monotonic() >= deadline:
            raise ProbeFailure('Receiver did not own its LAN listening socket before the startup deadline')
        if poll.poll(0):
            raise ProbeFailure('Receiver exited during startup readiness')
        rows = sample(args.pid, args.birth, namespace, args.uid, str(address), args.interface)
        if poll.poll(0):
            raise ProbeFailure('Receiver exited during listener observation')
        if time.monotonic() >= deadline:
            raise ProbeFailure('Receiver listener observation missed its startup deadline')
        if rows:
            return {'ready': True, 'pid': args.pid, 'birth': args.birth, 'uid': args.uid, 'gid': args.gid,
                    'namespace_dev': namespace[0], 'namespace_inode': namespace[1], 'listeners': rows}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProbeFailure('Receiver did not own its LAN listening socket before the startup deadline')
        if poll.poll(max(1, min(50, int(remaining * 1000)))):
            raise ProbeFailure('Receiver exited during startup readiness')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ('pid', 'namespace-fd', 'pidfd', 'uid', 'gid', 'parent'):
        parser.add_argument('--'+argument, type=int, required=True)
    for argument in ('birth', 'address', 'interface'):
        parser.add_argument('--'+argument, required=True)
    parser.add_argument('--timeout', type=float, required=True)
    try:
        print(json.dumps(run(parser.parse_args())))
    except (ProbeFailure, OSError, ValueError) as exc:
        print(json.dumps({'ready': False, 'error': str(exc)}))
        raise SystemExit(1) from exc
