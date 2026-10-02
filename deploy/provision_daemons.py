#!/usr/bin/env python3
"""Provision fixed non-login worker accounts; never adopt foreign identities.

Stdlib-only so the installer can execute this with isolated system Python.
All accounts are verified before an atomic root-only UID map is published.
"""

import argparse
from contextlib import contextmanager, nullcontext
import fcntl
import grp
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import tempfile
from uuid import UUID

PURPOSE = "Shiri managed daemon v1"


def expected_accounts(version=2):
    names = {"sender.discovery": "shiri-discovery", "sender.timing": "shiri-timing"}
    for slot in range(8):
        for role in ["receiver", "output", "audio", "discovery", "timing"]:
            names[f"slot{slot}.{role}"] = f"shiri-{role}-{slot}"
    if version == 2:
        names.update({f"slot{slot}.bridge": f"shiri-bridge-{slot}" for slot in range(8)})
    return names


def trusted_parent(path):
    if not path.is_absolute() or ".." in path.parts:
        raise RuntimeError("Use a canonical absolute daemon mapping path")
    for item in reversed([path.parent, *path.parent.parents]):
        info = item.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError("Daemon mapping ancestors must be root-owned non-writable directories")


def load_mapping(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0
            or info.st_mode & 0o077 or info.st_size > 65536):
        raise RuntimeError("Refusing unsafe existing daemon UID map")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as stream:
        current = os.fstat(stream.fileno())
        if ((current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
                or current.st_uid != 0 or current.st_mode & 0o077
                or current.st_nlink != 1 or not stat.S_ISREG(current.st_mode)):
            raise RuntimeError("Daemon mapping changed during open")
        data = json.load(stream)
    if (not isinstance(data, dict) or type(data.get("version")) is not int
            or data["version"] not in {1, 2}
            or set(data) != ({"version", "installation_id", "runtime_state_dir", "accounts"}
                             | ({"runtime_dir"} if data.get("version") == 2 else set()))
            or not isinstance(data.get("accounts"), dict)
            or set(data.get("accounts", {})) != set(expected_accounts(data["version"]))
            or (data["version"] == 2 and not isinstance(data.get("runtime_dir"), str))):
        raise RuntimeError("Existing daemon mapping has an unexpected schema; preserve and inspect it")
    for role, name in expected_accounts(data["version"]).items():
        account = data["accounts"][role]
        if (not isinstance(account, dict) or set(account) != {"name", "uid", "gid"}
                or account["name"] != name or type(account["uid"]) is not int
                or type(account["gid"]) is not int or account["uid"] <= 0 or account["gid"] <= 0):
            raise RuntimeError("Existing daemon mapping contains malformed credentials; preserve and inspect it")
    return data


def validate_account(role, name, account, group, installation_id):
    if (account.pw_uid == 0 or account.pw_gid == 0 or account.pw_gid != group.gr_gid
            or account.pw_shell not in {"/usr/sbin/nologin", "/sbin/nologin", "/bin/false"}
            or account.pw_gecos != f"{PURPOSE} {role} {installation_id}"
            or account.pw_dir != f"/nonexistent/shiri/{name}"
            or group.gr_mem
            or any(name in item.gr_mem and item.gr_gid != group.gr_gid for item in grp.getgrall())):
        raise RuntimeError(f"Refusing unexpected preexisting identity {name}; do not reassign an active daemon UID")
    return {"name": name, "uid": account.pw_uid, "gid": group.gr_gid}


def managed_processes_absent(uids, *, proc=Path("/proc")):
    """Refuse migration while any real/effective/saved/fs daemon UID is live."""
    for entry in proc.iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            status = (entry / "status").read_text()
        except FileNotFoundError:
            continue
        values = next((line.split()[1:] for line in status.splitlines() if line.startswith("Uid:")), None)
        if values is None or len(values) != 4 or not all(value.isdecimal() for value in values):
            raise RuntimeError("Cannot prove daemon identity migration is quiescent; inspect /proc before retrying")
        if uids.intersection(int(value) for value in values):
            raise RuntimeError("Managed daemon processes are still live; stop the owning broker and all owned units before UID map migration")


def trusted_json_descriptor(path, *, maximum=65536):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0
                or info.st_mode & 0o077 or info.st_size > maximum):
            raise RuntimeError("An identity migration input changed ownership, permissions or type")
        data = json.loads(os.pread(descriptor, maximum + 1, 0))
        return descriptor, info, data
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def migration_guard(path, previous, runtime_dir):
    """Hold the owning broker lock and old mapping inode until atomic upgrade."""
    mapping_fd, mapping_info, held = trusted_json_descriptor(path)
    lock_fd = None
    try:
        if held != previous:
            raise RuntimeError("Daemon map changed before migration; preserve and inspect it")
        lock_fd = os.open(runtime_dir / "broker.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        lock = os.fstat(lock_fd)
        if (not stat.S_ISREG(lock.st_mode) or lock.st_nlink != 1 or lock.st_uid != 0 or lock.st_mode & 0o077):
            raise RuntimeError("The owning broker lock is unsafe; do not migrate identities")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("The owning broker is active; stop it before UID map migration") from None

        def recheck():
            current = path.lstat()
            if (current.st_dev, current.st_ino) != (mapping_info.st_dev, mapping_info.st_ino):
                raise RuntimeError("Daemon map inode was replaced during migration; existing publication preserved")
            held_mapping = os.fstat(mapping_fd)
            if (not stat.S_ISREG(held_mapping.st_mode) or held_mapping.st_nlink != 1
                    or held_mapping.st_uid != 0 or held_mapping.st_mode & 0o077
                    or held_mapping.st_size > 65536
                    or json.loads(os.pread(mapping_fd, 65537, 0)) != previous):
                raise RuntimeError("Daemon map contents or permissions changed during migration")
            current_lock = (runtime_dir / "broker.lock").lstat()
            held_lock = os.fstat(lock_fd)
            if (not stat.S_ISREG(held_lock.st_mode) or held_lock.st_nlink != 1
                    or held_lock.st_uid != 0 or held_lock.st_mode & 0o077):
                raise RuntimeError("Broker lock permissions changed during migration")
            if (current_lock.st_dev, current_lock.st_ino) != (lock.st_dev, lock.st_ino):
                raise RuntimeError("Broker lock inode was replaced during migration")
            ownership = Path(previous["runtime_state_dir"]) / "ownership.json"
            fd, info, manifest = trusted_json_descriptor(ownership, maximum=1048576)
            try:
                if (not isinstance(manifest, dict) or manifest.get("version") != 1
                        or manifest.get("installation_id") != previous["installation_id"]
                        or manifest.get("networks") != {} or manifest.get("processes") != {}):
                    raise RuntimeError("Owned runtime resources remain; recover and stop them before UID map migration")
                current_ownership = ownership.lstat()
                if (current_ownership.st_dev, current_ownership.st_ino) != (info.st_dev, info.st_ino):
                    raise RuntimeError("Runtime manifest changed during migration; preserve it for inspection")
            finally:
                os.close(fd)
            uids = set()
            for value in previous["accounts"].values():
                if not isinstance(value, dict) or type(value.get("uid")) is not int or value["uid"] <= 0:
                    raise RuntimeError("The existing UID map is malformed; preserve it for inspection")
                uids.add(value["uid"])
            for slot in range(8):
                try:
                    uids.add(pwd.getpwnam(f"shiri-bridge-{slot}").pw_uid)
                except KeyError:
                    pass
            managed_processes_absent(uids)

        recheck()
        yield recheck
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(mapping_fd)


def provision(path, installation_id, runtime_state_dir, runtime_dir=Path("/run/shiri")):
    installation_id = str(UUID(installation_id))
    runtime_state_dir = str(Path(runtime_state_dir).absolute())
    runtime_dir = str(Path(runtime_dir).absolute())
    trusted_parent(Path(runtime_state_dir) / "ownership.json")
    trusted_parent(Path(runtime_dir) / "broker.lock")
    trusted_parent(path)
    previous = load_mapping(path)
    if previous is not None and (previous.get("installation_id") != installation_id
                                 or previous.get("runtime_state_dir") != runtime_state_dir
                                 or (previous["version"] == 2 and previous.get("runtime_dir") != runtime_dir)):
        raise RuntimeError("Daemon accounts belong to another installation; preserve both identities and stop for inspection")
    migrating = previous is not None and previous["version"] == 1
    guard = migration_guard(path, previous, Path(runtime_dir)) if migrating else nullcontext(None)
    with guard as recheck:
        return provision_accounts(path, installation_id, runtime_state_dir, runtime_dir, previous, recheck)


def provision_accounts(path, installation_id, runtime_state_dir, runtime_dir, previous, recheck):
    names = expected_accounts()
    api = pwd.getpwnam("shiri")
    if api.pw_uid == 0 or api.pw_gid == 0:
        raise RuntimeError("The API identity must not share root credentials")
    seen_uids, seen_gids = {api.pw_uid}, {api.pw_gid}
    # Validate every existing identity and published assignment BEFORE mutations.
    for role, name in names.items():
        try:
            account = pwd.getpwnam(name)
        except KeyError:
            if previous is not None and role in previous["accounts"]:
                raise RuntimeError(f"Published daemon account {name} disappeared; repair with all units stopped") from None
            try:
                grp.getgrnam(name)
            except KeyError:
                continue
            raise RuntimeError(f"Refusing preexisting unowned group {name}") from None
        try:
            group = grp.getgrnam(name)
        except KeyError:
            raise RuntimeError(f"Managed daemon group {name} is missing") from None
        current = validate_account(role, name, account, group, installation_id)
        if current["uid"] in seen_uids or current["gid"] in seen_gids:
            raise RuntimeError("Existing daemon credentials overlap another role or the API")
        seen_uids.add(current["uid"])
        seen_gids.add(current["gid"])
        if previous is not None and role in previous["accounts"] and previous["accounts"][role] != current:
            raise RuntimeError(f"Daemon UID/GID changed for {name}; existing mapping preserved")
    if recheck:
        recheck()
    accounts = {}
    for role, name in names.items():
        try:
            pwd.getpwnam(name)
        except KeyError:
            subprocess.run(["/usr/sbin/groupadd", "--system", name], check=True)
            subprocess.run([
                "/usr/sbin/useradd", "--system", "--gid", name, "--no-create-home",
                "--home-dir", f"/nonexistent/shiri/{name}", "--shell", "/usr/sbin/nologin",
                "--comment", f"{PURPOSE} {role} {installation_id}", name,
            ], check=True)
        accounts[role] = validate_account(role, name, pwd.getpwnam(name), grp.getgrnam(name), installation_id)
    api = pwd.getpwnam("shiri")
    uids, gids = [item["uid"] for item in accounts.values()], [item["gid"] for item in accounts.values()]
    if (len(set(uids)) != len(uids) or len(set(gids)) != len(gids)
            or api.pw_uid in uids or api.pw_gid in gids):
        raise RuntimeError("Daemon accounts share credentials with another role or the API")
    result = {"version": 2, "installation_id": installation_id,
              "runtime_state_dir": runtime_state_dir, "runtime_dir": runtime_dir, "accounts": accounts}
    if previous is not None and previous["version"] == 2:
        if previous != result:
            raise RuntimeError("Daemon mapping changed; existing mapping preserved")
        return result
    descriptor, temporary = tempfile.mkstemp(prefix=".daemon-identities.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            os.fchown(stream.fileno(), 0, 0)
            json.dump(result, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        # The outer service installer holds its installation lock. Do not
        # replace a concurrent mapping publication, even with equal content.
        if recheck:
            recheck()
            os.replace(temporary, path)
        else:
            os.link(temporary, path, follow_symlinks=False)
        directory = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("/etc/shiri/daemon-identities.json"))
    parser.add_argument("--installation-id", required=True, help="Existing runtime ownership manifest UUID")
    parser.add_argument("--runtime-state-dir", required=True, type=Path)
    parser.add_argument("--runtime-dir", type=Path, default=Path("/run/shiri"), help="Exact owning broker.lock directory, required for v1 migration")
    parser.add_argument("--locked", action="store_true", help="Called under the service installer installation lock")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Provision daemon identities as root")
    try:
        if args.locked:
            provision(args.output, args.installation_id, args.runtime_state_dir, args.runtime_dir)
        else:
            import importlib.util
            helper = importlib.util.spec_from_file_location("shiri_install_lock", Path(__file__).with_name("adopt_state.py"))
            module = importlib.util.module_from_spec(helper)
            helper.loader.exec_module(module)
            with module.installation_lock():
                provision(args.output, args.installation_id, args.runtime_state_dir, args.runtime_dir)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"Daemon identity provisioning refused: {exc}") from exc


if __name__ == "__main__":
    main()
