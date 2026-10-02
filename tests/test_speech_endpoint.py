"""Retained datagram endpoints are recoverable only under exact ownership."""

import os
import errno
from pathlib import Path
import socket
import stat
import tempfile
from types import SimpleNamespace

import pytest

from shiri.runtime import speech_endpoint as endpoint
from shiri.runtime.system import RuntimeFailure


def changed(info, **fields):
    return SimpleNamespace(**{name: fields.get(name, getattr(info, name))
                              for name in dir(info) if name.startswith("st_")})


@pytest.fixture
def room(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="shiri-speech-retire-", dir="/tmp") as temporary:
        root = Path(temporary)
        parent = root / "overlay"
        parent.mkdir()
        os.chown(parent, -1, os.getgid())
        parent.chmod(0o2710)
        path = parent / endpoint.NAME
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        listener.bind(str(path))
        os.chown(path, -1, os.getgid())
        path.chmod(0o660)
        listener.close()  # SIGKILL likewise leaves the filesystem entry behind.

        actual_uid, actual_gid = os.getuid(), os.getgid()
        output_uid, audio_gid = actual_uid or 1001, actual_gid or 1002
        if (output_uid, audio_gid) != (actual_uid, actual_gid):
            # The Linux suite may itself run as root. Normalize only observed
            # credentials to mapped daemon identities; do not spoof process
            # UID, relax the helper, or mock filesystem mutation/pinning.
            credential_fstat, credential_stat, credential_lstat = os.fstat, os.stat, Path.lstat

            def credentials(info):
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISSOCK(info.st_mode)):
                    return info
                return changed(info, st_uid=output_uid if info.st_uid == actual_uid else info.st_uid,
                               st_gid=audio_gid if info.st_gid == actual_gid else info.st_gid)

            monkeypatch.setattr(os, "fstat", lambda descriptor: credentials(credential_fstat(descriptor)))
            monkeypatch.setattr(os, "stat", lambda *args, **kwargs: credentials(credential_stat(*args, **kwargs)))
            monkeypatch.setattr(Path, "lstat", lambda path: credentials(credential_lstat(path)))

        # Linux exercises a genuine held O_PATH inode. Darwin cannot open a
        # filesystem socket as a path FD: mock only that missing kernel seam,
        # keeping named socket/directory identities, replacements and unlink real.
        pins = {}
        if not hasattr(os, "O_PATH"):
            raw_open, raw_close, raw_fstat, raw_stat = os.open, os.close, os.fstat, os.stat
            monkeypatch.setattr(os, "O_PATH", 1 << 29, raising=False)

            def opened(name, flags, *args, **kwargs):
                if name == endpoint.NAME and flags & os.O_PATH:
                    info = raw_stat(name, dir_fd=kwargs["dir_fd"], follow_symlinks=False)
                    descriptor = os.dup(kwargs["dir_fd"])
                    pins[descriptor] = info
                    return descriptor
                return raw_open(name, flags, *args, **kwargs)

            def closed(descriptor):
                pins.pop(descriptor, None)
                raw_close(descriptor)

            monkeypatch.setattr(os, "open", opened)
            monkeypatch.setattr(os, "close", closed)
            monkeypatch.setattr(os, "fstat", lambda descriptor: pins.get(descriptor) or raw_fstat(descriptor))
        sockets = []

        def replacement(destination=path):
            channel = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            channel.bind(str(destination))
            os.chown(destination, -1, os.getgid())
            destination.chmod(0o660)
            sockets.append(channel)
            return destination.lstat()

        value = SimpleNamespace(root=root, parent=parent, path=path, uid=output_uid, gid=audio_gid,
                                replacement=replacement, pins=pins)
        try:
            yield value
            assert not pins, "A held endpoint descriptor leaked"
        finally:
            for channel in sockets:
                channel.close()
            for descriptor in list(pins):
                os.close(descriptor)


def retire(room):
    return endpoint.retire(room.parent, room.uid, room.gid)


def test_actual_closed_datagram_socket_retirement_allows_clean_same_path_recovery(room):
    original = room.path.lstat()
    assert stat.S_ISSOCK(original.st_mode)
    assert retire(room) is True
    assert not room.path.exists() and room.parent.exists()
    assert retire(room) is False
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        listener.bind(str(room.path))
        os.chown(room.path, -1, os.getgid())
        room.path.chmod(0o660)
        listener.settimeout(1)
        sender.sendto(b"recovered-room", str(room.path))
        assert listener.recv(64) == b"recovered-room"
    finally:
        sender.close()
        listener.close()
    assert retire(room) is True


def test_initial_absence_is_harmless_and_creates_nothing(room):
    room.path.unlink()
    assert retire(room) is False
    room.parent.rmdir()
    assert retire(room) is False
    assert not room.parent.exists()


@pytest.mark.parametrize("kind", ["file", "symlink", "dangling_symlink"])
def test_hostile_endpoint_entries_are_never_removed_or_followed(room, kind):
    room.path.unlink()
    target = room.root / "foreign"
    target.write_bytes(b"preserve-source")
    if kind == "file":
        room.path.write_bytes(b"unexpected-file")
    else:
        room.path.symlink_to(target if kind == "symlink" else room.root / "missing")
    before = room.path.lstat()
    with pytest.raises(RuntimeFailure, match="endpoint type"):
        retire(room)
    assert room.path.lstat().st_ino == before.st_ino
    assert target.read_bytes() == b"preserve-source"


@pytest.mark.parametrize("mode", [0o700, 0o710, 0o2750, 0o2770, 0o777])
def test_wrong_parent_mode_cannot_authorize_retirement(room, mode):
    room.parent.chmod(mode)
    before = room.path.lstat()
    with pytest.raises(RuntimeFailure, match="directory credentials"):
        retire(room)
    assert room.path.lstat().st_ino == before.st_ino


@pytest.mark.parametrize("mode", [0o600, 0o666, 0o660 | stat.S_ISGID])
def test_wrong_socket_mode_is_preserved(room, mode):
    room.path.chmod(mode)
    before = room.path.lstat()
    with pytest.raises(RuntimeFailure, match="endpoint type"):
        retire(room)
    assert room.path.lstat().st_ino == before.st_ino


@pytest.mark.parametrize("field", ["st_uid", "st_gid"])
def test_wrong_parent_credentials_are_preserved(room, monkeypatch, field):
    raw_fstat = os.fstat
    monkeypatch.setattr(os, "fstat", lambda descriptor: changed(raw_fstat(descriptor), **{
        field: getattr(raw_fstat(descriptor), field) + 1,
    }))
    with pytest.raises(RuntimeFailure, match="directory credentials"):
        retire(room)
    assert room.path.exists()


@pytest.mark.parametrize("field", ["st_uid", "st_gid", "st_nlink"])
def test_wrong_socket_credentials_or_aliases_cannot_authorize_retirement(room, monkeypatch, field):
    raw_stat = os.stat

    def observed(*args, **kwargs):
        info = raw_stat(*args, **kwargs)
        return changed(info, **{field: getattr(info, field) + 1}) if stat.S_ISSOCK(info.st_mode) else info

    monkeypatch.setattr(os, "stat", observed)
    with pytest.raises(RuntimeFailure, match="endpoint type"):
        retire(room)
    assert room.path.exists()


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_non_directory_parent_is_never_followed(room, kind):
    actual = room.root / "held-overlay"
    room.parent.rename(actual)
    if kind == "file":
        room.parent.write_bytes(b"preserve-parent")
    else:
        room.parent.symlink_to(actual, target_is_directory=True)
    with pytest.raises(RuntimeFailure):
        retire(room)
    assert (actual / endpoint.NAME).exists()
    assert room.parent.lstat().st_ino != actual.lstat().st_ino


@pytest.mark.parametrize("moment", ["before_pin", "after_pin"])
def test_same_credentials_replaced_socket_inode_is_never_deleted(room, monkeypatch, moment):
    original_inode = room.path.lstat().st_ino
    swapped = False

    def replace():
        nonlocal swapped
        # Keep the old inode allocated, so this is a deterministic replacement
        # rather than relying on whether the filesystem recycles inode numbers.
        room.path.rename(room.root / "original.sock")
        room.replacement()
        swapped = True

    if moment == "before_pin":
        raw_open = os.open

        def opened(name, flags, *args, **kwargs):
            if name == endpoint.NAME and flags & os.O_PATH and not swapped:
                replace()
            return raw_open(name, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", opened)
    else:
        raw_stat, observations = os.stat, 0

        def observed(name, *args, **kwargs):
            nonlocal observations
            if name == endpoint.NAME and kwargs.get("dir_fd") is not None:
                observations += 1
                if observations == 2:
                    replace()
            return raw_stat(name, *args, **kwargs)

        monkeypatch.setattr(os, "stat", observed)
    with pytest.raises(RuntimeFailure, match="inode changed"):
        retire(room)
    assert swapped and room.path.lstat().st_ino != original_inode
    assert (room.root / "original.sock").lstat().st_ino == original_inode


def test_replaced_parent_preserves_both_original_and_successor_endpoints(room, monkeypatch):
    raw_open = os.open
    old = room.root / "original-overlay"

    def opened(name, flags, *args, **kwargs):
        if name == endpoint.NAME and flags & os.O_PATH and not old.exists():
            room.parent.rename(old)
            room.parent.mkdir()
            os.chown(room.parent, -1, os.getgid())
            room.parent.chmod(0o2710)
            room.replacement()
        return raw_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", opened)
    with pytest.raises(RuntimeFailure, match="directory changed"):
        retire(room)
    assert (old / endpoint.NAME).exists() and room.path.exists()


@pytest.mark.parametrize("change", ["mode", "disappeared"])
def test_entry_changes_after_pin_fail_closed(room, monkeypatch, change):
    raw_stat, observations = os.stat, 0
    inode = room.path.lstat().st_ino

    def observed(name, *args, **kwargs):
        nonlocal observations
        if name == endpoint.NAME and kwargs.get("dir_fd") is not None:
            observations += 1
            if observations == 2:
                if change == "mode":
                    room.path.chmod(0o666)
                else:
                    room.path.unlink()
        return raw_stat(name, *args, **kwargs)

    monkeypatch.setattr(os, "stat", observed)
    with pytest.raises(RuntimeFailure):
        retire(room)
    if change == "mode":
        assert room.path.lstat().st_ino == inode


@pytest.mark.parametrize("uid,gid", [(True, 20), (0, 20), (-1, 20), (2**32, 20),
                                    (501, False), (501, 0), (501, -1), (501, 2**32)])
def test_bad_credentials_fail_before_mutation(room, uid, gid):
    with pytest.raises(RuntimeFailure, match="non-root credentials"):
        endpoint.retire(room.parent, uid, gid)
    assert room.path.exists()


@pytest.mark.parametrize("path", ["/tmp/overlay", Path("overlay"), Path("/tmp/../overlay")])
def test_non_exact_parent_path_cannot_authorize_cleanup(path):
    with pytest.raises(RuntimeFailure, match="exact private path"):
        endpoint.retire(path, 501, 20)


@pytest.mark.parametrize("outcome", ["success", "held_inode_failure", "fsync_failure"])
def test_all_held_descriptors_close_on_success_and_guard_or_durability_failure(room, monkeypatch, outcome):
    raw_open, raw_close, raw_fstat = os.open, os.close, os.fstat
    descriptors = set()

    def opened(*args, **kwargs):
        descriptor = raw_open(*args, **kwargs)
        descriptors.add(descriptor)
        return descriptor

    def closed(descriptor):
        descriptors.discard(descriptor)
        raw_close(descriptor)

    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(os, "close", closed)
    if outcome == "held_inode_failure":
        def invalid_pin(descriptor):
            info = raw_fstat(descriptor)
            return changed(info, st_uid=room.uid + 1) if stat.S_ISSOCK(info.st_mode) else info
        monkeypatch.setattr(os, "fstat", invalid_pin)
    if outcome == "fsync_failure":
        def failed_sync(_descriptor):
            raise OSError(errno.EIO, "injected directory sync failure")
        monkeypatch.setattr(os, "fsync", failed_sync)
    if outcome == "success":
        assert retire(room) is True
    else:
        with pytest.raises(RuntimeFailure):
            retire(room)
    assert not descriptors and not room.pins
    if outcome == "held_inode_failure":
        assert room.path.exists()
    elif outcome == "fsync_failure":
        # The unlink happened, but durability was not acknowledged. A later
        # stopped-unit retry is harmless; no false atomic rollback is promised.
        assert not room.path.exists()
        assert retire(room) is False


def test_existing_socket_fails_closed_when_kernel_inode_pinning_is_unavailable(room, monkeypatch):
    monkeypatch.delattr(os, "O_PATH")
    with pytest.raises(RuntimeFailure, match="Linux O_PATH"):
        retire(room)
    assert room.path.exists()
