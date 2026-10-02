"""Persist the restrictive namespace policy across systemd transient reloads.

systemd 246/249 serializes its zero namespace allow-mask as an empty directive.
Reloading that directive permits every namespace. A unique root-owned drop-in
keeps the declared ``yes`` policy intact; its receipt never relaxes the daemon
identity or zero-mask checks. Reserved, exact creation phases recover after
the same unit has been proved terminated; unrelated bytes remain reserved.
"""

from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
import hashlib
import os
from pathlib import Path
import stat

from .launch_gate import UNIT_RE
from .system import RuntimeFailure

ROOT = Path("/run/systemd/system")
NAME = "90-shiri-namespace-policy.conf"
BODY = b"[Service]\nRestrictNamespaces=yes\n"
SHA256 = hashlib.sha256(BODY).hexdigest()


def _filesystem(operation):
    @wraps(operation)
    def guarded(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except OSError as exc:
            raise RuntimeFailure(
                "Cannot verify owned namespace policy filesystem; resources stay reserved"
            ) from exc

    return guarded


def _root_owned(info):
    return info.st_uid == 0 and info.st_gid == 0


def _check(info, *, directory, identity=None):
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    mode = 0o755 if directory else 0o644
    if (
        not kind(info.st_mode)
        or not _root_owned(info)
        or stat.S_IMODE(info.st_mode) != mode
        or (not directory and info.st_nlink != 1)
        or (identity is not None and [info.st_dev, info.st_ino] != identity)
    ):
        raise RuntimeFailure("Owned namespace policy artifact changed; resources stay reserved")


@contextmanager
def _root(*, create=False):
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for index, part in enumerate(ROOT.parts[1:]):
            info = os.fstat(descriptor)
            if not _root_owned(info) or info.st_mode & 0o022:
                raise RuntimeFailure("Namespace policy parent is not protected root ownership")
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            except FileNotFoundError:
                if not create or index != len(ROOT.parts) - 2:
                    raise
                os.mkdir(part, mode=0o755, dir_fd=descriptor)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                try:
                    os.fchmod(child, 0o755)
                    os.fsync(descriptor)
                except BaseException:
                    os.close(child)
                    raise
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if not _root_owned(info) or info.st_mode & 0o022:
            raise RuntimeFailure("Namespace policy parent is not protected root ownership")
        yield descriptor
    finally:
        os.close(descriptor)


def validate(record, unit, boot):
    if (
        not isinstance(record, dict)
        or set(record) != {"version", "unit", "boot_id", "root", "phase", "directory", "file", "sha256"}
        or type(record["version"]) is not int
        or record["version"] != 1
        or not isinstance(unit, str)
        or not UNIT_RE.fullmatch(unit)
        or record["unit"] != unit
        or not isinstance(boot, str)
        or not boot
        or record["boot_id"] != boot
        or record["sha256"] != SHA256
        or not isinstance(record["phase"], str)
        or record["phase"] not in {"planned", "directory", "file", "sealed"}
    ):
        raise RuntimeFailure("Invalid durable namespace policy artifact")
    for field in ["root", "directory", "file"]:
        identity = record[field]
        absent = (
            field == "directory"
            and record["phase"] == "planned"
            or field == "file"
            and record["phase"] in {"planned", "directory"}
        )
        if absent:
            if identity is not None:
                raise RuntimeFailure("Unsealed namespace policy has an unexpected identity")
        elif (
            not isinstance(identity, list)
            or len(identity) != 2
            or any(type(value) is not int or value <= 0 for value in identity)
        ):
            raise RuntimeFailure("Namespace policy lacks an exact filesystem identity")


@_filesystem
def plan(unit, boot):
    if not isinstance(unit, str) or not UNIT_RE.fullmatch(unit):
        raise RuntimeFailure("Namespace policy requires a unique owned daemon unit")
    with _root(create=True) as descriptor:
        name = unit + ".d"
        try:
            os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise RuntimeFailure("Namespace policy directory already exists; nothing was replaced")
        info = os.fstat(descriptor)
        record = {
            "version": 1,
            "unit": unit,
            "boot_id": boot,
            "root": [info.st_dev, info.st_ino],
            "phase": "planned",
            "directory": None,
            "file": None,
            "sha256": SHA256,
        }
        validate(record, unit, boot)
        return record


def _match_root(record, descriptor):
    info = os.fstat(descriptor)
    if [info.st_dev, info.st_ino] != record["root"]:
        raise RuntimeFailure("Namespace policy root was replaced; resources stay reserved")


@_filesystem
def create(record, *, persist=lambda _record: None):
    validate(record, record.get("unit", ""), record.get("boot_id"))
    if record["phase"] != "planned":
        raise RuntimeFailure("Namespace policy creation requires its reserved intent")
    name = record["unit"] + ".d"
    with _root() as root:
        _match_root(record, root)
        os.mkdir(name, mode=0o755, dir_fd=root)
        directory = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        try:
            os.fchmod(directory, 0o755)
            info = os.fstat(directory)
            identity = [info.st_dev, info.st_ino]
            _check(info, directory=True)
            result = record | {"phase": "directory", "directory": identity}
            persist(result)
            file = os.open(
                NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=directory
            )
            try:
                os.fchmod(file, 0o644)
                info = os.fstat(file)
                _check(info, directory=False)
                result = result | {"phase": "file", "file": [info.st_dev, info.st_ino]}
                persist(result)
                if os.write(file, BODY) != len(BODY):
                    raise RuntimeFailure("Namespace policy write was incomplete")
                os.fsync(file)
                result = result | {"phase": "sealed"}
            finally:
                os.close(file)
            os.fsync(directory)
            os.fsync(root)
            validate(result, result["unit"], result["boot_id"])
            return result
        finally:
            os.close(directory)


@contextmanager
def _directory(record, *, allow_absent=False):
    validate(record, record.get("unit", ""), record.get("boot_id"))
    with _root() as root:
        _match_root(record, root)
        try:
            directory = os.open(
                record["unit"] + ".d", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root
            )
        except FileNotFoundError:
            if allow_absent:
                yield root, None
                return
            raise RuntimeFailure("Owned namespace policy disappeared; resources stay reserved") from None
        try:
            _check(os.fstat(directory), directory=True, identity=record["directory"])
            yield root, directory
        finally:
            os.close(directory)


def _file(record, directory, *, allow_retired_absence=False):
    names = set(os.listdir(directory))
    if not names and (record["phase"] != "sealed" or allow_retired_absence):
        return False
    if names != {NAME}:
        raise RuntimeFailure("Unexpected namespace policy files; resources stay reserved")
    file = os.open(NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    try:
        _check(os.fstat(file), directory=False, identity=record["file"])
        body = os.read(file, len(BODY) + 1)
        if (
            body != BODY
            and (record["phase"] == "sealed" or not BODY.startswith(body))
            or len(body) > len(BODY)
            or (body == BODY and hashlib.sha256(body).hexdigest() != record["sha256"])
        ):
            raise RuntimeFailure("Owned namespace policy bytes changed; resources stay reserved")
        named = os.stat(NAME, dir_fd=directory, follow_symlinks=False)
        _check(named, directory=False, identity=record["file"])
    finally:
        os.close(file)
    return True


@_filesystem
def verify(record, *, allow_retired_absence=False):
    with _directory(record, allow_absent=record["phase"] == "planned" or allow_retired_absence) as (
        _,
        directory,
    ):
        if directory is not None:
            _file(record, directory, allow_retired_absence=allow_retired_absence)


@_filesystem
def discard(record, *, current_boot):
    """Called only after the exact daemon cgroup has been proven terminated."""
    validate(record, record.get("unit", ""), record.get("boot_id"))
    if record["boot_id"] != current_boot:
        # /run is volatile. Never transfer an old boot's inode authority to
        # a current boot's similarly named artifact, even if bytes agree.
        try:
            with _root() as root:
                try:
                    os.stat(record["unit"] + ".d", dir_fd=root, follow_symlinks=False)
                except FileNotFoundError:
                    return
        except FileNotFoundError:
            return
        raise RuntimeFailure("Namespace policy belongs to another boot; inspection is required")
    with _directory(record, allow_absent=True) as (root, directory):
        if directory is None:
            return
        # Recovery may repeat after our unlink and before our rmdir. The exact
        # original directory inode must still match; foreign/replaced bytes
        # remain refused. This path is called only after proven termination.
        has_file = _file(record, directory, allow_retired_absence=True)
        _check(
            os.stat(record["unit"] + ".d", dir_fd=root, follow_symlinks=False),
            directory=True,
            identity=record["directory"],
        )
        if has_file:
            os.unlink(NAME, dir_fd=directory)
        os.rmdir(record["unit"] + ".d", dir_fd=root)
        os.fsync(root)
