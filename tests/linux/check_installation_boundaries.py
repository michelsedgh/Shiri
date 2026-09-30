#!/usr/bin/python3
"""Manual root Linux check using only a disposable /opt tree; no apt/services."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import pwd
import select
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace


def load(source):
    specification = importlib.util.spec_from_file_location(source.stem, source)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    if sys.platform != "linux" or os.geteuid() != 0:
        parser.error("Run manually as root on 64-bit Linux")
    guard = load(args.source / "install/validate_installation.py")
    state = load(args.source / "deploy/adopt_state.py")
    state_error = guard.UnsafeInstallation
    account = pwd.getpwnam("nobody")
    owner = SimpleNamespace(pw_uid=account.pw_uid, pw_gid=account.pw_gid)
    base = Path(tempfile.mkdtemp(prefix="shiri-install-review-", dir="/opt"))
    checks = []

    def check(name, operation):
        operation()
        checks.append(name)

    def reject(operation, error):
        try:
            operation()
        except error:
            return
        raise AssertionError("Unsafe fixture unexpectedly accepted")

    def database(name):
        path = base / name
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA user_version=1")
        connection.execute("CREATE TABLE rooms (id TEXT PRIMARY KEY)")
        connection.commit()
        connection.close()
        path.chmod(0o600)
        return path

    try:
        prefix = base / "candidate"
        guard.validate_prefix(prefix, create=True)
        (prefix / "venv" / "bin").mkdir(parents=True)
        (prefix / "venv" / "lib").mkdir()
        (prefix / "venv" / "lib64").symlink_to("lib", target_is_directory=True)
        (prefix / "venv" / "bin" / "python").symlink_to("/usr/bin/python3")
        check("custom prefix accepts trusted venv interpreter links", lambda: guard.validate_prefix(prefix))

        prefix.chmod(0o775)
        check("group-writable prefix refused", lambda: reject(lambda: guard.validate_prefix(prefix), state_error))
        prefix.chmod(0o755)
        unsafe = prefix / "unsafe-program"
        unsafe.write_bytes(b"preserved")
        unsafe.chmod(0o666)
        check("writable executable refused", lambda: reject(lambda: guard.validate_prefix(prefix), state_error))
        assert unsafe.read_bytes() == b"preserved" and unsafe.stat().st_mode & 0o777 == 0o666
        unsafe.chmod(0o644)
        os.chown(unsafe, owner.pw_uid, owner.pw_gid)
        check("nonroot executable refused", lambda: reject(lambda: guard.validate_prefix(prefix), state_error))
        os.chown(unsafe, 0, 0)
        linked = base / "linked-prefix"
        linked.symlink_to(prefix, target_is_directory=True)
        check("symlink prefix refused", lambda: reject(lambda: guard.validate_prefix(linked), state_error))
        (prefix / "external-directory").symlink_to("/usr", target_is_directory=True)
        check("external directory symlink refused", lambda: reject(lambda: guard.validate_prefix(prefix), state_error))
        (prefix / "external-directory").unlink()

        descriptor = os.open(database("ofd.sqlite3"), os.O_RDWR)
        try:
            with state.database_lock(descriptor):
                connection = sqlite3.connect(base / "ofd.sqlite3", timeout=0)
                try:
                    reject(lambda: connection.execute("BEGIN IMMEDIATE"), sqlite3.OperationalError)
                finally:
                    connection.close()
                # Closing another SQLite connection must not release the OFD lock.
                connection = sqlite3.connect(base / "ofd.sqlite3", timeout=0)
                try:
                    check("OFD lock survives SQLite connection close", lambda: reject(
                        lambda: connection.execute("BEGIN IMMEDIATE"), sqlite3.OperationalError,
                    ))
                finally:
                    connection.close()
        finally:
            os.close(descriptor)

        for suffix in ("-wal", "-shm", "-journal"):
            path = database("retained" + suffix + ".sqlite3")
            original = path.read_bytes()
            companion = Path(str(path) + suffix)
            companion.write_bytes(b"retained frames")
            check(f"retained {suffix} refuses adoption", lambda path=path: reject(
                lambda: state.adopt_database(path, owner), state.UnsafeState,
            ))
            assert path.read_bytes() == original and companion.read_bytes() == b"retained frames"
            assert path.stat().st_uid == 0 and companion.stat().st_uid == 0

        path = database("hardlinked.sqlite3")
        os.link(path, base / "hardlink")
        check("hardlinked database refused", lambda: reject(lambda: state.adopt_database(path, owner), state.UnsafeState))
        linked = base / "database-link"
        linked.symlink_to(path)
        check("symlink database refused", lambda: reject(lambda: state.adopt_database(linked, owner), (state.UnsafeState, OSError)))

        path = database("idle.sqlite3")
        child = subprocess.Popen([
            "/usr/bin/python3", "-I", "-c",
            "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
            "c.execute('SELECT count(*) FROM rooms').fetchone(); "
            "print('ready',flush=True); sys.stdin.read(1); c.close()", str(path),
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            readable, _, _ = select.select([child.stdout], [], [], 5)
            assert readable and child.stdout.readline().strip() == "ready", "Idle fixture did not become ready"
            check("idle foreign-process connection refused", lambda: reject(
                lambda: state.adopt_database(path, owner), state.UnsafeState,
            ))
            assert path.stat().st_uid == 0
        finally:
            if child.poll() is None:
                child.stdin.write("x")
                child.stdin.flush()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
            child.stdin.close()
            child.stdout.close()
            child.stderr.close()

        path = database("valid.sqlite3")
        original = path.read_bytes()
        check("quiescent database changes only exact owner and mode", lambda: state.adopt_database(path, owner))
        assert path.read_bytes() == original and path.stat().st_uid == owner.pw_uid
        assert path.stat().st_gid == owner.pw_gid and path.stat().st_mode & 0o777 == 0o600
        assert not Path(str(path) + "-wal").exists() and not Path(str(path) + "-shm").exists()

        lock_dir = base / "locks"
        with state.installation_lock(lock_dir):
            check("concurrent service installer refused", lambda: reject(
                lambda: enter_lock(state, lock_dir), state.UnsafeState,
            ))
        check("installation lock reusable after close", lambda: enter_lock(state, lock_dir))
        print(json.dumps({"ok": True, "checks": checks, "count": len(checks), "scope": "disposable /opt fixtures only"}, indent=2))
    finally:
        shutil.rmtree(base)


def enter_lock(state, directory):
    with state.installation_lock(directory):
        pass


if __name__ == "__main__":
    main()
