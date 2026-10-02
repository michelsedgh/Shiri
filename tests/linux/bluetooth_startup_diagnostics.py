"""Test-only error/log receipts for the isolated Bluetooth route fixture.

This observes the existing scoped request. It never calls Open/OpenRestricted,
changes admission, returns a rejected reply, or owns received descriptors.
Production DescriptorBus remains responsible for identity checks and FD cleanup.
"""
from __future__ import annotations

from contextvars import ContextVar
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import stat
import time

from dbus_next import MessageType

from shiri.runtime.bluealsa import DescriptorBus
from shiri.runtime.system import RuntimeFailure, atomic_json

MAX_ERRORS = 16
MAX_ERROR_TEXT_BYTES = 1024
MAX_LOG_TAIL_BYTES = 64 * 1024
MAX_SOURCE_LOG_BYTES = 4 * 1024 * 1024


def _text(value, maximum):
    if not isinstance(value, str):
        return None
    return value[:maximum].encode('utf-8', errors='replace')[:maximum].decode('utf-8', errors='ignore')


class _Pending(dict):
    """Attach method metadata at the exact production pending insertion."""
    def __init__(self, context, methods):
        super().__init__()
        self.context, self.methods = context, methods

    def __setitem__(self, serial, value):
        super().__setitem__(serial, value)
        self.methods[serial] = self.context.get()

    def pop(self, serial, default=None):
        self.methods.pop(serial, None)
        return super().pop(serial, default)

    def clear(self):
        self.methods.clear()
        super().clear()


class DiagnosticBus(DescriptorBus):
    """Observe only a live exact-serial reply from its expected unique owner."""
    def __init__(self, bus, records):
        self.records, self.methods = records, {}
        self.context = ContextVar('private_bluetooth_method', default=None)
        super().__init__(bus)
        self.pending = _Pending(self.context, self.methods)

    async def request(self, destination, path, interface, member, **kwargs):
        token = self.context.set((destination, interface, member))
        try:
            return await super().request(destination, path, interface, member, **kwargs)
        finally:
            self.context.reset(token)

    def _received(self, reply):
        expected = self.pending.get(reply.reply_serial)
        method = self.methods.get(reply.reply_serial)
        if (reply.message_type == MessageType.ERROR and not self.closed and expected is not None
                and not expected[0].done() and reply.sender == expected[1] and method is not None
                and method[0] == reply.sender):
            if len(self.records['errors']) < MAX_ERRORS:
                self.records['errors'].append({
                    'observed_monotonic_ns': time.monotonic_ns(),
                    'member': _text(method[2], 128), 'interface': _text(method[1], 128),
                    'expected_reply_sender': _text(expected[1], 32),
                    'reply_sender': _text(reply.sender, 32), 'reply_serial': reply.reply_serial,
                    'error_name': _text(reply.error_name, 256), 'signature': _text(reply.signature, 64),
                    'body_items': len(reply.body),
                    'body_text': (_text(reply.body[0], MAX_ERROR_TEXT_BYTES)
                                  if reply.signature == 's' and len(reply.body) == 1 else None),
                    'received_fd_count': len(reply.unix_fds),
                })
            else:
                self.records['omitted_errors'] += 1
        # Delegate every reply unchanged, including unknown/foreign/error FDs.
        return super()._received(reply)


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode, info.st_nlink)


def retain(directory, daemon):
    """Copy a bounded private log tail before exact daemon teardown/removal.

    The root-created held fixture directory and no-follow regular log are the
    authority. Append-only growth is permitted; replacement/type/ownership is
    refused. No arbitrary reported path or daemon-provided filename is opened.
    Diagnostics never serve as successful media or cleanup evidence.
    """
    result = {'phase': 'before_private_daemon_teardown', 'captured_monotonic_ns': time.monotonic_ns(),
              'dbus': {'errors': [dict(value) for value in daemon.startup_diagnostics['errors']],
                       'omitted_errors': daemon.startup_diagnostics['omitted_errors']},
              'log': {'status': 'not_created'}}
    descriptor = None
    if daemon.directory_fd is not None:
        held = os.fstat(daemon.directory_fd)
        if daemon.directory_identity != (held.st_dev, held.st_ino, held.st_uid):
            raise RuntimeFailure('Private diagnostic directory changed identity')
        current = daemon.directory.lstat()
        if not stat.S_ISDIR(current.st_mode) or _identity(current) != _identity(held):
            raise RuntimeFailure('Private diagnostic pathname changed identity')
        try:
            descriptor = os.open('bluealsa.log', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                                 dir_fd=daemon.directory_fd)
        except FileNotFoundError:
            pass
        if descriptor is not None:
            try:
                before = os.fstat(descriptor)
                if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                        or before.st_uid != held.st_uid or stat.S_IMODE(before.st_mode) != 0o600):
                    raise RuntimeFailure('Private diagnostic log is not the exact protected regular file')
                if before.st_size > MAX_SOURCE_LOG_BYTES:
                    raise RuntimeFailure('Private diagnostic log exceeded its existing file-size bound')
                offset = max(0, before.st_size-MAX_LOG_TAIL_BYTES)
                payload = os.pread(descriptor, min(before.st_size, MAX_LOG_TAIL_BYTES), offset)
                after = os.fstat(descriptor)
                named = os.stat('bluealsa.log', dir_fd=daemon.directory_fd, follow_symlinks=False)
                if (_identity(before) != _identity(after) or _identity(before) != _identity(named)
                        or after.st_size < before.st_size or len(payload) != min(before.st_size, MAX_LOG_TAIL_BYTES)):
                    raise RuntimeFailure('Private diagnostic log changed identity during capture')
                destination = Path(directory)/'private-bluealsa-before-teardown.log'
                output = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(output, 'wb') as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                result['log'] = {'status': 'retained', 'path': str(destination),
                                 'observed_source_bytes': before.st_size, 'tail_offset': offset,
                                 'retained_bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest(),
                                 'tail_text': payload.decode('utf-8', errors='replace')}
            finally:
                os.close(descriptor)
    result['mock_bluez_calls'] = [list(value) for value in (daemon.mock.calls[-32:] if daemon.mock else [])]
    result['mock_bluez_total_calls'] = len(daemon.mock.calls) if daemon.mock else 0
    result['daemon_pid'] = daemon.daemon.pid if daemon.daemon else None
    result['daemon_returncode'] = daemon.daemon.returncode if daemon.daemon else None
    result['broker_startup'] = deepcopy(getattr(daemon, 'broker_startup_diagnostics', None))
    path = Path(directory)/'private-bluetooth-startup-diagnostics.json'
    atomic_json(path, result)
    content = path.read_bytes()
    return {'path': str(path), 'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest(),
            'error_records': len(result['dbus']['errors']), 'dbus': result['dbus'], 'log': result['log'],
            'broker_startup': result['broker_startup']}
