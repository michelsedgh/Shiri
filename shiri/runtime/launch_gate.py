"""Standalone isolated-interpreter gate; no daemon executes before root admission.

This file intentionally uses only the standard library and runs via python -I.
The release directory is bound read-only into the unprivileged service.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

UNIT_RE = re.compile(
    r"shiri-(?P<installation>[0-9a-f]{8})-(?P<owner>sender|[0-9a-f]{32})-"
    r"(?P<role>[a-z0-9-]{1,32})-(?P<nonce>[0-9a-f]{32})\.service"
)


def release(path: Path, *, unit: str, nonce: str, boot: str, invocation: str, cgroup: tuple[int, int]):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                or info.st_mode & 0o022 or info.st_size > 4096):
            raise ValueError("Launch release is not an immutable root-owned record")
        data = os.read(descriptor, 4097)
        if len(data) > 4096:
            raise ValueError("Launch release exceeds its bound")
        value = json.loads(data)
    finally:
        os.close(descriptor)
    if (not isinstance(value, dict)
            or set(value) != {"unit", "nonce", "boot_id", "invocation_id", "cgroup_dev", "cgroup_inode"}
            or value["unit"] != unit or value["nonce"] != nonce or value["boot_id"] != boot
            or not isinstance(value["invocation_id"], str)
            or not re.fullmatch(r"[0-9a-f]{32}", value["invocation_id"])
            or value["invocation_id"] == "0" * 32
            or value["invocation_id"] != invocation
            or type(value["cgroup_dev"]) is not int or type(value["cgroup_inode"]) is not int
            or (value["cgroup_dev"], value["cgroup_inode"]) != cgroup):
        raise ValueError("Launch release belongs to a different daemon incarnation")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit", required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    command = options.command[1:] if options.command[:1] == ["--"] else options.command
    if (not UNIT_RE.fullmatch(options.unit)
            or not re.fullmatch(r"[0-9a-f]{32}", options.nonce)
            or options.release != "/run/shiri-worker/gate/ready.json"
            or not command or not Path(command[0]).is_absolute()):
        raise ValueError("Malformed daemon launch gate")
    groups = Path("/proc/self/cgroup").read_text().splitlines()
    expected_group = f"/system.slice/{options.unit}"
    if groups != [f"0::{expected_group}"]:
        raise ValueError("Launch gate is outside its admitted control group")
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if not re.fullmatch(r"[0-9a-f-]{36}", boot):
        raise ValueError("Launch gate has no kernel boot identity")
    invocation = os.environ.get("INVOCATION_ID", "")
    if not re.fullmatch(r"[0-9a-f]{32}", invocation) or invocation == "0" * 32:
        raise ValueError("Launch gate has no system-manager invocation identity")
    # The mount sandbox exposes the same cgroup inode read-only. No privilege
    # or controller access is needed merely to observe its kernel identity.
    descriptor = os.open(Path("/sys/fs/cgroup") / expected_group.lstrip("/"),
                         os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        cgroup = info.st_dev, info.st_ino
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if release(Path(options.release), unit=options.unit, nonce=options.nonce, boot=boot,
                       invocation=invocation, cgroup=cgroup):
                current = os.fstat(descriptor)
                if (current.st_dev, current.st_ino) != cgroup:
                    raise ValueError("Launch gate control group changed")
                os.execv(command[0], command)
            time.sleep(0.025)
        raise ValueError("Root did not admit the daemon within the launch deadline")
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        print(f"Daemon launch gate refused execution: {error}", file=sys.stderr)
        raise SystemExit(1) from error
