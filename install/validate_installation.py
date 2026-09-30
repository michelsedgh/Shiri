#!/usr/bin/python3
"""Reject executable trees another user could replace before privileged use."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import stat
import sys

ROOT_UID = 0


class UnsafeInstallation(RuntimeError):
    pass


def trusted_metadata(path: Path, info):
    if info.st_uid != ROOT_UID:
        raise UnsafeInstallation(f"Installation path is not root-owned: {path}")
    if not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022:
        raise UnsafeInstallation(f"Installation path is writable by another user/group: {path}")
    if stat.S_ISREG(info.st_mode) and info.st_mode & (stat.S_ISUID | stat.S_ISGID):
        raise UnsafeInstallation(f"Unexpected set-ID installation file: {path}")


def trusted_target(path: Path, *, seen=frozenset()) -> tuple[Path, os.stat_result]:
    """Validate every symlink and ancestor, including normal system Python links."""
    path = Path(path)
    if not path.is_absolute():
        path = Path.cwd() / path
    if path in seen or len(seen) >= 40:
        raise UnsafeInstallation(f"Installation symlink cycle or excessive depth: {path}")
    current = Path(path.anchor)
    trusted_metadata(current, current.lstat())
    for index, component in enumerate(path.parts[1:]):
        current = current.parent if component == ".." else current / component
        info = current.lstat()
        trusted_metadata(current, info)
        if stat.S_ISLNK(info.st_mode):
            target = Path(os.readlink(current))
            if not target.is_absolute():
                target = current.parent / target
            remaining = path.parts[index + 2:]
            return trusted_target(target.joinpath(*remaining), seen=seen | {path})
        if index < len(path.parts) - 2 and not stat.S_ISDIR(info.st_mode):
            raise UnsafeInstallation(f"Installation ancestor is not a directory: {current}")
    info = current.lstat()
    trusted_metadata(current, info)
    return current, info


def validate_prefix(value: str | Path, *, create=False):
    text = str(value)
    if (
        not re.fullmatch(r"/[A-Za-z0-9_./-]+", text)
        or text == "/"
        or any(component in {".", ".."} for component in text.split("/"))
    ):
        raise UnsafeInstallation("Use a canonical safe absolute installation prefix")
    prefix = Path(text)
    current = Path(prefix.anchor)
    trusted_metadata(current, current.lstat())
    for component in prefix.parts[1:]:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            if not create:
                raise UnsafeInstallation(f"Installation directory does not exist: {current}") from None
            current.mkdir(mode=0o755)
            info = current.lstat()
        trusted_metadata(current, info)
        if not stat.S_ISDIR(info.st_mode):
            raise UnsafeInstallation(f"Installation prefix/ancestor must be a real directory: {current}")

    pending = [prefix]
    visited = set()
    while pending:
        directory = pending.pop()
        identity = (directory.stat().st_dev, directory.stat().st_ino)
        if identity in visited:
            continue
        visited.add(identity)
        for entry in directory.iterdir():
            info = entry.lstat()
            trusted_metadata(entry, info)
            if stat.S_ISLNK(info.st_mode):
                target, info = trusted_target(entry)
                if stat.S_ISDIR(info.st_mode):
                    if not target.is_relative_to(prefix):
                        raise UnsafeInstallation(f"External installation directory symlink: {entry}")
                    pending.append(target)
                elif not stat.S_ISREG(info.st_mode):
                    raise UnsafeInstallation(f"Installation symlink target is not a regular file: {entry}")
            elif stat.S_ISDIR(info.st_mode):
                pending.append(entry)
            elif not stat.S_ISREG(info.st_mode):
                raise UnsafeInstallation(f"Unexpected special installation file: {entry}")
    return prefix


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prefix", type=Path)
    parser.add_argument("--create", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0 or sys.platform != "linux":
        parser.error("Run installation validation as root on Linux")
    try:
        validate_prefix(args.prefix, create=args.create)
    except (UnsafeInstallation, OSError) as exc:
        print(f"Unsafe Shiri installation: {exc}. Preserve the tree; choose a trusted root-owned prefix.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
