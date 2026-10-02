#!/usr/bin/python3
"""Opt-in root Linux BPF proof in one new cgroup and a child-only network namespace.

Does not join or mutate Shiri/host audio namespaces, units, links, or devices.
The only child joins our just-created cgroup before dropping to UID/GID 65534.
Exact created cgroup identity and a held child pidfd fence cleanup.
Usage: sudo python3 -I tests/linux/check_socket_policy.py --helper /opt/shiri/libexec/shiri-bind-policy
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import json
import os
from pathlib import Path
import select
import signal
import socket
import stat
import subprocess
import sys
import time
from uuid import uuid4


def command(helper, operation, descriptor, info, boot, port, proof=None):
    args = [str(helper), operation, str(descriptor), str(info.st_dev), str(info.st_ino), boot, str(port)]
    if proof:
        args += [str(proof['inet4']['id']), proof['inet4']['tag'], str(proof['inet6']['id']), proof['inet6']['tag']]
    result = subprocess.run(args, pass_fds=(descriptor,), check=True, capture_output=True, text=True, timeout=5)
    response = json.loads(result.stdout)
    if (response['port'] != port or response['cgroup_dev'] != info.st_dev
            or response['cgroup_inode'] != info.st_ino or response['boot_id'] != boot
            or response['instructions_verified'] is not True or response['effective_verified'] is not True):
        raise RuntimeError('Helper did not return the exact verified cgroup identity')
    return response


def probe(family, kind, port, allowed):
    with socket.socket(family, kind) as connection:
        address = ('0.0.0.0' if family == socket.AF_INET else '::', port)
        try:
            connection.bind(address)
        except OSError as error:
            if allowed or error.errno != errno.EPERM:
                raise RuntimeError(f'Unexpected bind outcome: family={family} kind={kind} port={port}') from error
            return 'denied'
        if not allowed:
            raise RuntimeError(f'Unauthorized bind succeeded: family={family} kind={kind} port={port}')
        if kind == socket.SOCK_STREAM:
            connection.listen(1)
        return 'allowed'


def child(group, descriptor, helper, info, boot, port, proof):
    library = ctypes.CDLL(None, use_errno=True)
    if library.unshare(0x40000000):  # CLONE_NEWNET, only this newly-forked child.
        raise OSError(ctypes.get_errno(), 'Cannot create isolated socket-test network namespace')
    (group / 'cgroup.procs').write_text(str(os.getpid()))
    low, high = map(int, Path('/proc/sys/net/ipv4/ip_local_port_range').read_text().split())
    if not (high < 3869 or low > 3939):
        raise RuntimeError('Isolated namespace ephemeral range overlaps peer room control ports')
    os.setgroups([])
    os.setresgid(65534, 65534, 65534)
    os.setresuid(65534, 65534, 65534)
    observed = []
    for candidate in range(3869, 3940, 10):
        observed.append(probe(socket.AF_INET, socket.SOCK_STREAM, candidate, candidate == port))
        observed.append(probe(socket.AF_INET, socket.SOCK_DGRAM, candidate, True))
        observed.append(probe(socket.AF_INET6, socket.SOCK_STREAM, candidate, False))
        observed.append(probe(socket.AF_INET6, socket.SOCK_DGRAM, candidate, False))
    observed.append(probe(socket.AF_INET, socket.SOCK_STREAM, 0, False))
    observed.append(probe(socket.AF_INET, socket.SOCK_DGRAM, 0, True))
    observed.append(probe(socket.AF_INET6, socket.SOCK_STREAM, 0, False))
    observed.append(probe(socket.AF_INET6, socket.SOCK_DGRAM, 0, False))
    # Bind hooks deliberately do not govern listen() implicit autobind. The
    # actual namespace range must keep this path away from all peer HTTP ports.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.listen(1)
        assigned = connection.getsockname()[1]
        if not low <= assigned <= high or 3869 <= assigned <= 3939:
            raise RuntimeError('Implicit listener captured a fixed room control port')
    args = [str(helper), 'verify', str(descriptor), str(info.st_dev), str(info.st_ino), boot, str(port),
            str(proof['inet4']['id']), proof['inet4']['tag'], str(proof['inet6']['id']), proof['inet6']['tag']]
    denied = subprocess.run(args, pass_fds=(descriptor,), capture_output=True, text=True, timeout=5)
    if not denied.returncode or denied.stdout:
        raise RuntimeError('Unprivileged output UID acquired root helper authority')
    return {'ok': True, 'uid': os.geteuid(), 'bind_checks': len(observed), 'ephemeral_range': [low, high],
            'implicit_listener_port': assigned, 'nonroot_helper_denied': True}


def check(helper, parent, port):
    if sys.platform != 'linux' or os.geteuid() != 0 or not hasattr(os, 'pidfd_open'):
        raise RuntimeError('Root Linux with cgroup2 and pidfds is required')
    parent_info = parent.stat()
    if not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid or parent_info.st_mode & 0o022:
        raise RuntimeError('Use an existing root-owned non-writable cgroup parent')
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    group = parent / ('shiri-bind-test-' + uuid4().hex)
    descriptor = pidfd = read_fd = write_fd = None
    pid = None
    reaped = False
    group.mkdir(mode=0o700)
    try:
        descriptor = os.open(group, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        proof = command(helper, 'attach', descriptor, info, boot, port)
        before = command(helper, 'verify', descriptor, info, boot, port, proof)
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            try:
                response = child(group, descriptor, helper, info, boot, port, proof)
            except BaseException as error:
                response = {'ok': False, 'error': str(error)}
            payload = json.dumps(response).encode()
            os.write(write_fd, payload)
            os.close(write_fd)
            os._exit(0 if response['ok'] else 1)
        os.close(write_fd)
        write_fd = None
        pidfd = os.pidfd_open(pid)
        readable, _, _ = select.select([read_fd], [], [], 10)
        if not readable:
            raise RuntimeError('Isolated policy child exceeded its bounded test deadline')
        payload = os.read(read_fd, 4097)
        if len(payload) > 4096:
            raise RuntimeError('Isolated policy result exceeded its bound')
        response = json.loads(payload)
        _, status = os.waitpid(pid, 0)
        reaped = True
        if status or not response.get('ok'):
            raise RuntimeError(response.get('error', 'Isolated policy child failed'))
        after = command(helper, 'verify', descriptor, info, boot, port, proof)
        if before != after:
            raise RuntimeError('Policy did not persist through the unprivileged child lifetime')
        return {'ok': True, 'kernel_tested': True, 'proof': proof, 'child': response,
                'policy_persisted': True, 'cleanup': True}
    finally:
        if pidfd is not None and not reaped:
            try:
                signal.pidfd_send_signal(pidfd, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
        for handle in (pidfd, read_fd, write_fd):
            if handle is not None:
                os.close(handle)
        if descriptor is not None:
            current = group.stat()
            held = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
                os.close(descriptor)
                raise RuntimeError('Created test cgroup identity changed; no replacement was removed')
            deadline = time.monotonic() + 2
            while 'populated 0' not in (group / 'cgroup.events').read_text():
                if time.monotonic() >= deadline:
                    os.close(descriptor)
                    raise RuntimeError('Created test cgroup did not become empty; it stays reserved')
                time.sleep(.02)
            group.rmdir()
            os.close(descriptor)
        else:
            group.rmdir()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--helper', type=Path, required=True)
    parser.add_argument('--parent', type=Path, default=Path('/sys/fs/cgroup'))
    parser.add_argument('--port', type=int, choices=range(3869, 3940, 10), default=3939)
    options = parser.parse_args()
    print(json.dumps(check(options.helper, options.parent, options.port)))
