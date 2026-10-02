"""Retire a stopped room's private late-speech endpoint.

The caller MUST first prove the exact OwnTone, audio and reserved room units'
cgroups empty, and serialize retirement against another room launch. The
enclosing root-owned room directory is mode 0700: foreign host UIDs cannot
reach this endpoint, and no stopped unit retains its private writable bind.
Those lifecycle/permission facts exclude a writer between the final identity
check and unlink. Linux has no atomic conditional unlink-by-inode operation;
the held descriptors and checks below do not create such an operation and do
not authorize deleting a live endpoint.
"""

from __future__ import annotations

import os
from pathlib import Path
import stat

from .system import RuntimeFailure

NAME = "speech.sock"


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid, info.st_nlink)


def _parent(info, output_uid, audio_gid):
    return (stat.S_ISDIR(info.st_mode) and info.st_uid == output_uid
            and info.st_gid == audio_gid and stat.S_IMODE(info.st_mode) == 0o2710)


def _socket(info, output_uid, audio_gid):
    return (stat.S_ISSOCK(info.st_mode) and info.st_nlink == 1
            and info.st_uid == output_uid and info.st_gid == audio_gid
            and stat.S_IMODE(info.st_mode) == 0o660)


def retire(parent: Path, output_uid: int, audio_gid: int) -> bool:
    """Remove only the unchanged admitted socket AFTER exact unit retirement.

    Return False for an initially absent directory or endpoint, True after
    unlink and directory fsync. Every unexpected entry/identity fails closed.
    O_PATH pins the filesystem socket inode, not an active socket connection.
    """
    if (not isinstance(parent, Path) or not parent.is_absolute() or ".." in parent.parts
            or any(type(value) is not int or not 0 < value <= 2**32 - 1
                   for value in (output_uid, audio_gid))):
        raise RuntimeFailure("Speech endpoint retirement requires an exact private path and non-root credentials")

    try:
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RuntimeFailure("Speech endpoint retirement cannot admit its private directory") from exc

    socket_fd = None
    try:
        directory = os.fstat(directory_fd)
        if not _parent(directory, output_uid, audio_gid):
            raise RuntimeFailure("Speech endpoint directory credentials or mode changed")
        try:
            original = os.stat(NAME, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not _socket(original, output_uid, audio_gid):
            raise RuntimeFailure("Speech endpoint type, credentials or mode changed")
        if not hasattr(os, "O_PATH"):
            raise RuntimeFailure("Speech endpoint inode retirement requires Linux O_PATH support")
        socket_fd = os.open(NAME, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory_fd)
        held = os.fstat(socket_fd)
        if not _socket(held, output_uid, audio_gid) or _identity(held) != _identity(original):
            raise RuntimeFailure("Speech endpoint inode changed before retirement")

        current_directory = os.fstat(directory_fd)
        named_directory = parent.lstat()
        if (not _parent(current_directory, output_uid, audio_gid)
                or not _parent(named_directory, output_uid, audio_gid)
                or _identity(current_directory) != _identity(directory)
                or _identity(named_directory) != _identity(directory)):
            raise RuntimeFailure("Speech endpoint directory changed before retirement")
        current = os.stat(NAME, dir_fd=directory_fd, follow_symlinks=False)
        if (not _socket(current, output_uid, audio_gid)
                or _identity(current) != _identity(held)
                or _identity(os.fstat(socket_fd)) != _identity(held)):
            raise RuntimeFailure("Speech endpoint inode changed before retirement")

        # The caller's stopped-unit proof/root-private serialized directory is
        # required here. A check/unlink pair alone cannot exclude a live writer.
        os.unlink(NAME, dir_fd=directory_fd)
        os.fsync(directory_fd)
        return True
    except OSError as exc:
        raise RuntimeFailure("Speech endpoint changed or could not finish retirement") from exc
    finally:
        try:
            if socket_fd is not None:
                os.close(socket_fd)
        finally:
            os.close(directory_fd)
