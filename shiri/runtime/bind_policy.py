"""Broker-owned persistent socket policy over an already admitted cgroup FD."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import re
import stat

from .system import RuntimeFailure


def trusted_file(path: Path, *, executable=False):
    """Reject privilege-bearing files beneath replaceable installation paths."""
    if not path.is_absolute() or ".." in path.parts:
        raise RuntimeFailure("Privileged runtime helper must use a canonical absolute installation path")
    try:
        for parent in [*reversed(path.parents), path]:
            info = parent.lstat()
            if (info.st_uid != 0 or info.st_mode & 0o022
                    or (parent == path and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                                           or (executable and not info.st_mode & 0o111)))
                    or (parent != path and not stat.S_ISDIR(info.st_mode))):
                raise RuntimeFailure("Runtime helper installation is writable, linked or not owned by root")
    except OSError as exc:
        raise RuntimeFailure("Required trusted runtime helper is missing or inaccessible; rerun installation") from exc
    return path


def validate_proof(value, *, boot: str, port: int, device: int, inode: int):
    if type(device) is not int or device <= 0 or type(inode) is not int or inode <= 0:
        raise RuntimeFailure("Socket policy has no canonical kernel cgroup identity")
    expected = {"version": 1, "port": port, "cgroup_dev": device,
                "cgroup_inode": inode, "boot_id": boot,
                "instructions_verified": True, "effective_verified": True}
    if not isinstance(value, dict) or set(value) != {*expected, "inet4", "inet6"}:
        raise RuntimeFailure("Socket policy helper returned an invalid ownership proof")
    if any(type(value[key]) is not type(wanted) or value[key] != wanted for key, wanted in expected.items()):
        raise RuntimeFailure("Socket policy helper inspected a different launch boundary")
    for name in ["inet4", "inet6"]:
        record = value[name]
        if (not isinstance(record, dict) or set(record) != {"id", "tag"}
                or type(record["id"]) is not int or not 1 <= record["id"] <= 0xffffffff
                or not isinstance(record["tag"], str) or not re.fullmatch(r"[0-9a-f]{16}", record["tag"])):
            raise RuntimeFailure("Socket policy helper returned an invalid kernel program identity")
    if value["inet4"]["id"] == value["inet6"]["id"]:
        raise RuntimeFailure("Socket policy helper reused one kernel program for different hooks")
    return value


def proof(value, *, descriptor: int, boot: str, port: int):
    info = os.fstat(descriptor)
    return validate_proof(value, boot=boot, port=port, device=info.st_dev, inode=info.st_ino)


class BindPolicy:
    def __init__(self, helper: Path):
        self.helper = helper

    async def run(self, action: str, descriptor: int, boot: str, port: int, expected=None):
        trusted_file(self.helper, executable=True)
        info = os.fstat(descriptor)
        args = [str(self.helper), action, str(descriptor), str(info.st_dev), str(info.st_ino), boot, str(port)]
        if action == "verify":
            if expected is None:
                raise RuntimeFailure("Socket policy verification requires its exact admitted programs")
            proof(expected, descriptor=descriptor, boot=boot, port=port)
            args.extend(str(expected[name][field]) for name in ["inet4", "inet6"] for field in ["id", "tag"])
        elif action != "attach":
            raise RuntimeFailure("Unknown socket policy operation")
        process = await asyncio.create_subprocess_exec(
            *args, pass_fds=(descriptor,), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"},
        )
        try:
            try:
                output, _ = await asyncio.wait_for(process.communicate(), timeout=5)
            except asyncio.TimeoutError as exc:
                raise RuntimeFailure("Kernel socket policy helper exceeded its bounded deadline") from exc
            if process.returncode or len(output) > 4096:
                raise RuntimeFailure("Kernel socket policy could not be attached or verified; daemon remains gated")
            try:
                value = proof(json.loads(output), descriptor=descriptor, boot=boot, port=port)
            except (ValueError, TypeError) as exc:
                raise RuntimeFailure("Kernel socket policy helper returned malformed data") from exc
            if expected is not None and value != expected:
                raise RuntimeFailure("Kernel socket policy identity changed; daemon remains gated")
            return value
        finally:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
                    await process.wait()
