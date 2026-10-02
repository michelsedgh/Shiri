"""Held directory aliases for root-only Linux Unix socket operations.

Canonical directory paths remain the durable identity and mount source. A
short /proc/self/fd address is an ephemeral syscall argument, never a manifest
record. Each asynchronous operation owns a duplicate until its exact completion;
closing/reusing the lifecycle FD cannot redirect that operation to a successor.
The caller controls the enclosing private room and the directory's credentials.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import stat

from .system import RuntimeFailure


def identity(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))


class PinnedUnixDirectory:
    def __init__(self, path: Path, *, uid: int, gid: int, mode: int):
        self.path, self.descriptor = Path(path), None
        if (not self.path.is_absolute() or '..' in self.path.parts
                or any(type(value) is not int or value < 0 for value in (uid, gid))
                or type(mode) is not int or not 0 <= mode <= 0o7777 or mode & 0o022):
            raise RuntimeFailure('Unix socket directory needs exact private credentials')
        descriptor = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISDIR(info.st_mode) or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))
                    != (uid, gid, mode) or identity(self.path.lstat()) != identity(info)):
                raise RuntimeFailure('Unix socket directory changed identity or credentials')
            self.expected = identity(info)
            self.descriptor, descriptor = descriptor, None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def verify(self):
        if self.descriptor is None:
            raise RuntimeFailure('Unix socket directory has retired')
        held, named = os.fstat(self.descriptor), self.path.lstat()
        if (not stat.S_ISDIR(held.st_mode) or not stat.S_ISDIR(named.st_mode)
                or identity(held) != self.expected or identity(named) != self.expected):
            raise RuntimeFailure('Unix socket directory was replaced or changed credentials')

    @contextmanager
    def address(self, name: str):
        if not isinstance(name, str) or re.fullmatch(r'[a-z][a-z0-9-]{0,31}\.sock', name) is None:
            raise RuntimeFailure('Unix socket alias requires one fixed socket basename')
        self.verify()
        descriptor = os.dup(self.descriptor)
        try:
            os.set_inheritable(descriptor, False)
            if identity(os.fstat(descriptor)) != self.expected:
                raise RuntimeFailure('Duplicated Unix socket directory changed identity')
            address = Path(f'/proc/self/fd/{descriptor}') / name
            if len(os.fsencode(address)) >= 108:
                raise RuntimeFailure('Unix socket descriptor alias exceeds the Linux address bound')
            yield address
        finally:
            os.close(descriptor)

    def close(self):
        descriptor, self.descriptor = self.descriptor, None
        if descriptor is not None:
            os.close(descriptor)
