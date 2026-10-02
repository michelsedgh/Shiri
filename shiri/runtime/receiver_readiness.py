"""Admit receiver readiness only from exact unit and kernel listener ownership."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import select

from .bind_policy import trusted_file
from .system import RuntimeFailure, boot_id, process_birth


async def wait_receiver_ready(unit, receiver, account, *, timeout=10):
    """The short same-UID observer avoids granting broker CAP_SYS_PTRACE."""
    try:
        return await asyncio.wait_for(_wait_receiver_ready(unit, receiver, account, timeout=timeout), timeout=timeout+1)
    except asyncio.TimeoutError as exc:
        raise RuntimeFailure('Receiver startup ownership verification exceeded its total deadline') from exc


async def _wait_receiver_ready(unit, receiver, account, *, timeout):
    namespace_fd = pidfd = process = None
    try:
        expected = deepcopy(unit.identity())
        pid = unit.process.pid
        birth = process_birth(pid)
        namespace = Path('/run/netns')/receiver['namespace']
        if (not unit.alive or not birth or expected.get('name') != 'shairport'
                or expected.get('boot_id') != boot_id() or receiver.get('boot_id') != boot_id()
                or not re.fullmatch(r'shiri_rx_[0-9a-f]{8}_[0-9a-f]{12}', receiver['namespace'])
                or expected.get('namespace') != str(namespace)
                or expected.get('user') != account['name'] or expected.get('group') != account['name']
                or not 0 < timeout <= 15):
            raise RuntimeFailure('Receiver startup admission lost its exact process or namespace authority')
        namespace_fd = os.open(namespace, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        held = os.fstat(namespace_fd)
        if held.st_ino != receiver.get('inode') or not os.path.ismount(namespace):
            raise RuntimeFailure('Receiver namespace changed before startup readiness')
        pidfd = os.pidfd_open(pid)
        poll = select.poll()
        poll.register(pidfd, select.POLLIN)

        async def verify():
            actual = await unit.manager.inspect(expected['unit'])
            if actual is None:
                raise RuntimeFailure('Receiver unit disappeared during startup readiness')
            unit.manager.verify(expected, actual)
            current = namespace.stat()
            if (actual.get('MainPID') != pid or actual.get('ActiveState') not in {'active', 'activating'}
                    or process_birth(pid) != birth or poll.poll(0)
                    or (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino)):
                raise RuntimeFailure('Receiver process or namespace changed during startup readiness')
            group = unit.manager.open_cgroup(expected)
            try:
                if os.fstat(group).st_ino != expected.get('cgroup_inode'):
                    raise RuntimeFailure('Receiver cgroup changed during startup readiness')
            finally:
                os.close(group)

        await verify()
        script = trusted_file(Path(__file__).with_name('receiver_probe.py'))
        python = trusted_file(Path('/usr/bin/python3').resolve(strict=True), executable=True)
        process = await asyncio.create_subprocess_exec(
            str(python), '-I', '-S', str(script), '--pid', str(pid), '--birth', birth,
            '--namespace-fd', str(namespace_fd), '--pidfd', str(pidfd),
            '--uid', str(account['uid']), '--gid', str(account['gid']), '--parent', str(os.getpid()),
            '--address', receiver['ip'], '--interface', receiver['interface'], '--timeout', str(timeout),
            pass_fds=(namespace_fd, pidfd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C.UTF-8'},
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout+1)
        except asyncio.TimeoutError as exc:
            raise RuntimeFailure('Receiver readiness observer exceeded its bounded deadline') from exc
        if len(output) > 8192:
            raise RuntimeFailure('Receiver readiness observer returned oversized evidence')
        try:
            observation = json.loads(output)
        except (ValueError, UnicodeError) as exc:
            raise RuntimeFailure('Receiver readiness observer returned malformed evidence') from exc
        if process.returncode or not isinstance(observation, dict) or observation.get('ready') is not True:
            detail = observation.get('error', 'No listener ownership proof') if isinstance(observation, dict) else 'Malformed listener proof'
            raise RuntimeFailure('AirPlay receiver failed startup readiness: '+str(detail))
        if (observation.get('pid') != pid or observation.get('birth') != birth
                or observation.get('uid') != account['uid'] or observation.get('gid') != account['gid']
                or observation.get('namespace_dev') != held.st_dev or observation.get('namespace_inode') != held.st_ino
                or not observation.get('listeners')):
            raise RuntimeFailure('Receiver listener observation belongs to another process or namespace')
        for row in observation['listeners']:
            if (not isinstance(row, dict) or set(row) != {'family', 'address', 'port', 'uid', 'inode'}
                    or type(row['inode']) is not int or row['inode'] <= 0
                    or type(row['port']) is not int or row['port'] != 7000
                    or type(row['uid']) is not int or row['uid'] != account['uid']
                    or row['family'] not in {'ipv4', 'ipv6'}
                    or row['address'] not in ({'0.0.0.0', receiver['ip']} if row['family'] == 'ipv4' else {'::'})):
                raise RuntimeFailure('Receiver readiness did not prove an admitted LAN listener')
        if not any(row['family'] == 'ipv4' for row in observation['listeners']):
            raise RuntimeFailure('Receiver readiness did not prove its advertised IPv4 endpoint')
        await verify()
        return observation
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeFailure('Receiver startup ownership could not be verified') from exc
    finally:
        if process is not None and process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
        for descriptor in (pidfd, namespace_fd):
            if descriptor is not None:
                os.close(descriptor)
