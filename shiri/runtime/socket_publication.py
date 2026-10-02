"""Publish one admitted daemon socket from a root-protected directory.

The unit reservation receives the exact inode before rename. Consumers bind
only the published socket, never its producer-writable parent directory.
"""
from __future__ import annotations

from contextlib import suppress
import os
from pathlib import Path
import re
import stat

from .system import RuntimeFailure


def validate(record):
    if (not isinstance(record, dict) or set(record) != {
            "directory", "directory_dev", "directory_inode", "socket_dev", "socket_inode", "uid", "gid", "initial_gid"}
            or not isinstance(record["directory"], str)
            or not re.fullmatch(r"/[A-Za-z0-9_./-]+/bridge-published/[0-9a-f]{32}", record["directory"])
            or ".." in Path(record["directory"]).parts
            or any(type(record[key]) is not int or record[key] <= 0 for key in set(record) - {"directory"})):
        raise RuntimeFailure("Invalid admitted bridge socket publication")
    return record


def open_directory(record):
    validate(record)
    path = Path(record["directory"])
    trusted_ancestors(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    info = os.fstat(descriptor)
    if (info.st_uid != 0 or info.st_mode & 0o077
            or (info.st_dev, info.st_ino) != (record["directory_dev"], record["directory_inode"])):
        os.close(descriptor)
        raise RuntimeFailure("Bridge publication directory was replaced")
    return descriptor


def trusted_ancestors(path):
    for parent in reversed([path.parent, *path.parent.parents]):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeFailure("Bridge publication has an unsafe ancestor")


def socket_matches(info, record):
    return (stat.S_ISSOCK(info.st_mode) and info.st_nlink == 1
            and (info.st_dev, info.st_ino) == (record["socket_dev"], record["socket_inode"])
            and info.st_uid == record["uid"] and info.st_gid in {record["gid"], record["initial_gid"]}
            and info.st_mode & 0o777 in {0o600, 0o660})


def prepare(source: Path, directory: Path, *, uid: int, initial_gid: int, gid: int):
    """Return an intent and held descriptors; caller persists before publish()."""
    if any(type(value) is not int or value <= 0 for value in [uid, initial_gid, gid]):
        raise RuntimeFailure("Bridge socket needs exact non-root credentials")
    validate({"directory": str(directory), "directory_dev": 1, "directory_inode": 1,
              "socket_dev": 1, "socket_inode": 1, "uid": uid, "initial_gid": initial_gid, "gid": gid})
    trusted_ancestors(directory)
    source_fd = os.open(source, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
    created = None
    try:
        info = os.fstat(source_fd)
        if (not stat.S_ISSOCK(info.st_mode) or info.st_nlink != 1 or info.st_uid != uid
                or info.st_gid != initial_gid or info.st_mode & 0o777 != 0o600):
            raise RuntimeFailure("Bridge listener does not match its private launch identity")
        directory.mkdir(mode=0o700)
        directory_info = directory.lstat()
        created = directory_info
        record = {"directory": str(directory), "directory_dev": directory_info.st_dev,
                  "directory_inode": directory_info.st_ino, "socket_dev": info.st_dev, "socket_inode": info.st_ino,
                  "uid": uid, "initial_gid": initial_gid, "gid": gid}
        directory_fd = open_directory(record)
        return record, source_fd, directory_fd
    except BaseException:
        # Only this freshly created, unchanged, empty root directory is ours
        # before reservation. A changed entry remains available for inspection.
        try:
            if created is not None and created.st_uid == 0:
                with suppress(FileNotFoundError):
                    current = directory.lstat()
                    if ((current.st_dev, current.st_ino) == (created.st_dev, created.st_ino)
                            and stat.S_ISDIR(current.st_mode) and not list(directory.iterdir())):
                        directory.rmdir()
        finally:
            os.close(source_fd)
        raise


def publish(source: Path, record, source_fd: int, directory_fd: int):
    """Rename only after durable reservation, then verify the held listener."""
    validate(record)
    held, directory = os.fstat(source_fd), os.fstat(directory_fd)
    named_directory = Path(record["directory"]).lstat()
    if (not socket_matches(held, record)
            or (directory.st_dev, directory.st_ino) != (record["directory_dev"], record["directory_inode"])
            or (named_directory.st_dev, named_directory.st_ino) != (directory.st_dev, directory.st_ino)
            or directory.st_uid != 0 or directory.st_mode & 0o077 or os.listdir(directory_fd)):
        raise RuntimeFailure("Bridge socket publication changed before commit")
    os.rename(source, "final-pcm.sock", dst_dir_fd=directory_fd)
    named = os.stat("final-pcm.sock", dir_fd=directory_fd, follow_symlinks=False)
    if not socket_matches(named, record) or (named.st_dev, named.st_ino) != (held.st_dev, held.st_ino):
        raise RuntimeFailure("Bridge listener was replaced during publication; reservation retained")
    # O_PATH cannot be fchowned/fchmodded on Linux5.15. Its /proc descriptor
    # magic link refers to the admitted inode even after the producer's rename.
    reference = f"/proc/self/fd/{source_fd}"
    os.chown(reference, record["uid"], record["gid"])
    os.chmod(reference, 0o660)
    final = os.fstat(source_fd)
    if (not socket_matches(final, record) or final.st_gid != record["gid"]
            or final.st_mode & 0o777 != 0o660):
        raise RuntimeFailure("Bridge listener credentials changed during publication")
    os.fsync(directory_fd)
    return Path(record["directory"]) / "final-pcm.sock"


def discard(record):
    """Caller has proved the exact producer unit empty; refuse replacements."""
    try:
        descriptor = open_directory(record)
    except FileNotFoundError:
        return
    try:
        names = os.listdir(descriptor)
        if set(names) - {"final-pcm.sock"}:
            raise RuntimeFailure("Unexpected files in bridge publication; reservation retained")
        if names:
            info = os.stat("final-pcm.sock", dir_fd=descriptor, follow_symlinks=False)
            if not socket_matches(info, record):
                raise RuntimeFailure("Published bridge listener changed; reservation retained")
            os.unlink("final-pcm.sock", dir_fd=descriptor)
        Path(record["directory"]).rmdir()
    finally:
        os.close(descriptor)
