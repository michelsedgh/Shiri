#!/usr/bin/python3
"""Validate known stopped service state; never adopt live SQLite companions."""

from __future__ import annotations

import grp
from contextlib import contextmanager
import ctypes
import fcntl
import os
from pathlib import Path
import pwd
import sqlite3
import stat
import subprocess
import sys


class UnsafeState(RuntimeError):
    pass


def api_account():
    account, group = pwd.getpwnam("shiri"), grp.getgrnam("shiri")
    if (
        account.pw_uid == 0 or group.gr_gid == 0 or account.pw_gid != group.gr_gid
        or account.pw_shell not in {"/usr/sbin/nologin", "/sbin/nologin", "/bin/false", "/usr/bin/false"}
    ):
        raise UnsafeState("Existing shiri account must be nonroot, non-login and use the shiri primary group")
    return account


def require_api_stopped():
    result = subprocess.run(
        ["/usr/bin/systemctl", "show", "shiri-api.service", "--property=ActiveState,MainPID"],
        capture_output=True, text=True, check=False, timeout=5,
    )
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if result.returncode and "not found" not in result.stderr.lower() and values.get("ActiveState") != "inactive":
        raise UnsafeState("Cannot prove shiri-api is stopped; inspect systemd before service-state adoption")
    if values.get("ActiveState") in {"active", "activating", "deactivating", "reloading"} or int(values.get("MainPID", 0)):
        raise UnsafeState("Stop shiri-api and every manual API/SQLite process before rerunning service installation")


def validate_directories(account):
    for path in [Path("/var/lib/shiri"), Path("/var/lib/shiri-runtime"), Path("/run/shiri"), Path("/etc/shiri")]:
        for ancestor in reversed(path.parents):
            info = ancestor.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise UnsafeState(f"Untrusted service-directory ancestor: {ancestor}")
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        allowed = {0, account.pw_uid} if path == Path("/var/lib/shiri") else {0}
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in allowed or info.st_mode & 0o022:
            raise UnsafeState(f"Refusing an unowned, linked or writable service directory: {path}")


def companions_absent(path):
    if any(Path(str(path) + suffix).exists() or Path(str(path) + suffix).is_symlink() for suffix in ("-wal", "-shm", "-journal")):
        raise UnsafeState(
            "SQLite WAL/SHM/journal files exist; preserve them. Stop all API/SQLite processes, "
            "checkpoint the database as its current owner, close the connection, and rerun. "
            "Do not delete or chown companions to bypass this check"
        )


def known_descriptor(path, allowed_owners, *, writable=False):
    initial = path.lstat()
    if (not stat.S_ISREG(initial.st_mode) or initial.st_uid not in allowed_owners
            or initial.st_nlink != 1 or initial.st_mode & 0o022):
        raise UnsafeState(f"Refusing to adopt an unowned/nonregular/linked/writable service file: {path}")
    descriptor = os.open(path, (os.O_RDWR if writable else os.O_RDONLY) | os.O_NOFOLLOW | os.O_NONBLOCK)
    info = os.fstat(descriptor)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid not in allowed_owners or info.st_nlink != 1
            or info.st_mode & 0o022 or (initial.st_dev, initial.st_ino) != (info.st_dev, info.st_ino)):
        os.close(descriptor)
        raise UnsafeState(f"Refusing to adopt an unowned/nonregular/linked/writable service file: {path}")
    return descriptor, info


def same_file(path, descriptor, allowed_owners):
    current, held = path.lstat(), os.fstat(descriptor)
    if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1 or held.st_nlink != 1
            or current.st_uid not in allowed_owners or held.st_uid not in allowed_owners
            or current.st_mode & 0o022 or held.st_mode & 0o022
            or (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino)):
        raise UnsafeState(f"Service state changed while it was being validated: {path}")


def adopt_token(path, account, *, stopped_check=None):
    descriptor, _ = known_descriptor(path, {0, account.pw_uid})
    try:
        token = os.read(descriptor, 257).decode().strip()
        if not 32 <= len(token) <= 256 or any(ord(char) < 33 for char in token):
            raise UnsafeState("Existing admin token is invalid; its contents were preserved")
        if stopped_check:
            stopped_check()
        same_file(path, descriptor, {0, account.pw_uid})
        os.fchown(descriptor, 0, account.pw_gid)
        os.fchmod(descriptor, 0o640)
    finally:
        os.close(descriptor)


def require_no_other_openers(info, *, process_root=Path("/proc")):
    """Even an idle connection is unsafe to move to a different service owner."""
    for process in process_root.iterdir():
        if not process.name.isdecimal() or int(process.name) == os.getpid():
            continue
        try:
            for descriptor in (process / "fd").iterdir():
                try:
                    opened = descriptor.stat()
                except FileNotFoundError:
                    continue
                if (opened.st_dev, opened.st_ino) == (info.st_dev, info.st_ino):
                    raise UnsafeState(f"Database is open by PID {process.name}; stop every API/SQLite process")
        except FileNotFoundError:
            continue
        except PermissionError:
            raise UnsafeState(f"Cannot prove database quiescence: process descriptors inaccessible at {process}") from None


@contextmanager
def database_lock(descriptor):
    # Linux open-file-description byte locks conflict with SQLite's POSIX byte
    # locks. Unlike process locks, they survive closing the validation connection.
    if not hasattr(fcntl, "F_OFD_SETLK") or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise UnsafeState("Database adoption requires 64-bit Linux open-file-description locks")

    class FileLock(ctypes.Structure):
        _fields_ = [("type", ctypes.c_short), ("whence", ctypes.c_short),
                    ("start", ctypes.c_longlong), ("length", ctypes.c_longlong), ("pid", ctypes.c_int)]

    try:
        fcntl.fcntl(descriptor, fcntl.F_OFD_SETLK, bytes(FileLock(fcntl.F_WRLCK, os.SEEK_SET, 0, 0, 0)))
    except OSError:
        raise UnsafeState("Database is locked by another SQLite process; stop all writers before adoption") from None
    try:
        yield
    finally:
        fcntl.fcntl(descriptor, fcntl.F_OFD_SETLK, bytes(FileLock(fcntl.F_UNLCK, os.SEEK_SET, 0, 0, 0)))


def adopt_database(path, account, *, stopped_check=None):
    companions_absent(path)
    owners = {0, account.pw_uid}
    descriptor, info = known_descriptor(path, owners, writable=True)
    try:
        if os.read(descriptor, 16) != b"SQLite format 3\0":
            raise UnsafeState("Existing service database is not SQLite; preserved")
        with database_lock(descriptor):
            require_no_other_openers(info)
            companions_absent(path)
            same_file(path, descriptor, owners)
            # This is deliberately NOT WAL-aware. Only a quiescent,
            # companion-free main file is admissible; no journals are created.
            connection = sqlite3.connect(f"file:/proc/self/fd/{descriptor}?mode=ro&immutable=1", uri=True)
            try:
                if (connection.execute("PRAGMA quick_check").fetchone()[0] != "ok"
                        or connection.execute("PRAGMA user_version").fetchone()[0] != 1
                        or not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rooms'").fetchone()):
                    raise UnsafeState("Existing service database failed validation; preserved")
            finally:
                connection.close()
            if stopped_check:
                stopped_check()
            require_no_other_openers(info)
            companions_absent(path)
            same_file(path, descriptor, owners)
            os.fchown(descriptor, account.pw_uid, account.pw_gid)
            os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def adopt_environment(path, account):
    descriptor, _ = known_descriptor(path, {0})
    try:
        same_file(path, descriptor, {0})
        os.fchown(descriptor, 0, account.pw_gid)
        os.fchmod(descriptor, 0o640)
    finally:
        os.close(descriptor)


def validate_unit_targets():
    directory = Path("/etc/systemd/system")
    for ancestor in reversed([directory, *directory.parents]):
        info = ancestor.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise UnsafeState(f"Untrusted systemd unit directory: {ancestor}")
    for name in ("shiri-runtime.service", "shiri-api.service"):
        path = directory / name
        if path.exists() or path.is_symlink():
            descriptor, _ = known_descriptor(path, {0})
            os.close(descriptor)


@contextmanager
def installation_lock(directory=Path("/run/shiri-install")):
    parent = directory.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0 or parent.st_mode & 0o022:
        raise UnsafeState(f"Untrusted installation-lock parent: {directory.parent}")
    if not directory.exists() and not directory.is_symlink():
        directory.mkdir(mode=0o700)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise UnsafeState(f"Untrusted service-installation lock directory: {directory}")
    descriptor = os.open(directory / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o077:
            raise UnsafeState("Untrusted service-installation lock file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UnsafeState("Another Shiri service installation is in progress") from None
        yield
    finally:
        os.close(descriptor)


def main():
    if os.geteuid() != 0 or sys.platform != "linux":
        raise UnsafeState("Service-state adoption requires root Linux installation")
    if len(sys.argv) == 3 and sys.argv[1] == "--run":
        with installation_lock():
            raise SystemExit(subprocess.run(["/bin/bash", sys.argv[2], "--locked"], check=False).returncode)
    account = api_account()
    require_api_stopped()
    validate_directories(account)
    validate_unit_targets()
    if sys.argv[1:] == ["--check-only"]:
        return
    if sys.argv[1:]:
        raise UnsafeState("Usage: adopt_state.py [--check-only]")
    for path, operation in [(Path("/etc/shiri/api-token"), adopt_token), (Path("/var/lib/shiri/shiri.sqlite3"), adopt_database)]:
        if operation is adopt_database:
            companions_absent(path)
        if path.exists() or path.is_symlink():
            operation(path, account, stopped_check=require_api_stopped)
    environment = Path("/etc/shiri/shiri.env")
    if environment.exists() or environment.is_symlink():
        adopt_environment(environment, account)


if __name__ == "__main__":
    try:
        main()
    except (UnsafeState, OSError, ValueError, KeyError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
        print(f"Shiri service state preserved: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
