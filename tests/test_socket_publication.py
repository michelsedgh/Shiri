"""Socket inode publication and cleanup keep producer path swaps fenced."""
import os
from pathlib import Path
import socket
import stat
import tempfile
from types import SimpleNamespace
from uuid import uuid4

import pytest

from shiri.runtime import socket_publication as publication
from shiri.runtime.system import RuntimeFailure


@pytest.fixture
def listener(monkeypatch):
    # Short Unix path works on macOS too. Mock only root/worker credentials and
    # Linux O_PATH admission; rename, held directories and accept are real.
    with tempfile.TemporaryDirectory(prefix="shiri-publish-", dir="/tmp") as temporary:
        root = Path(temporary)
        source = root / "input.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(source))
        server.listen(1)
        source.chmod(0o600)
        parent = root / "bridge-published"
        parent.mkdir(mode=0o700)
        target = parent / uuid4().hex
        pin_file = root / "held"
        pin_file.write_bytes(b"")
        raw_open, raw_close, raw_fstat, raw_stat = os.open, os.close, os.fstat, os.stat
        fds, credentials = set(), {"uid": 1001, "gid": 1001, "mode": 0o600}
        captured = raw_stat(source)

        def info(value, **updates):
            return SimpleNamespace(**{key: updates.get(key, getattr(value, key))
                                      for key in dir(value) if key.startswith("st_")})

        def normalized(value):
            if stat.S_ISDIR(value.st_mode):
                return info(value, st_uid=0)
            if stat.S_ISSOCK(value.st_mode):
                return info(value, st_uid=credentials["uid"], st_gid=credentials["gid"],
                            st_mode=stat.S_IFSOCK | credentials["mode"])
            return value

        def opened(path, flags, *args, **kwargs):
            if Path(path) == source:
                fd = raw_open(pin_file, os.O_RDONLY)
                fds.add(fd)
                return fd
            return raw_open(path, flags, *args, **kwargs)

        def close(fd):
            fds.discard(fd)
            raw_close(fd)

        def changed_owner(path, uid, gid):
            assert path.startswith("/proc/self/fd/") and int(path.rsplit("/", 1)[1]) in fds
            credentials.update(uid=uid, gid=gid)

        def changed_mode(path, mode):
            assert path.startswith("/proc/self/fd/") and int(path.rsplit("/", 1)[1]) in fds
            credentials["mode"] = mode

        monkeypatch.setattr(publication, "trusted_ancestors", lambda _path: None)
        monkeypatch.setattr(os, "O_PATH", getattr(os, "O_PATH", 0), raising=False)
        monkeypatch.setattr(os, "open", opened)
        monkeypatch.setattr(os, "close", close)
        monkeypatch.setattr(os, "fstat", lambda fd: normalized(captured if fd in fds else raw_fstat(fd)))
        monkeypatch.setattr(os, "stat", lambda *args, **kwargs: normalized(raw_stat(*args, **kwargs)))
        monkeypatch.setattr(Path, "lstat", lambda path: normalized(raw_stat(path, follow_symlinks=False)))
        monkeypatch.setattr(os, "chown", changed_owner)
        monkeypatch.setattr(os, "chmod", changed_mode)
        try:
            yield SimpleNamespace(source=source, directory=target, server=server, credentials=credentials, fds=fds)
        finally:
            for fd in list(fds):
                close(fd)
            server.close()


def prepared(listener):
    return publication.prepare(listener.source, listener.directory, uid=1001, initial_gid=1001, gid=2002)


def test_renamed_published_listener_still_accepts_and_cleanup_keeps_exact_identity(listener):
    record, source_fd, directory_fd = prepared(listener)
    try:
        destination = publication.publish(listener.source, record, source_fd, directory_fd)
        assert not listener.source.exists()
        assert record["socket_inode"] == destination.lstat().st_ino
        assert listener.credentials == {"uid": 1001, "gid": 2002, "mode": 0o660}
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.connect(str(destination))
            peer, _ = listener.server.accept()
            peer.close()
        finally:
            client.close()
        publication.discard(record)
        assert not destination.exists() and not listener.directory.exists()
    finally:
        os.close(source_fd)
        os.close(directory_fd)


def test_source_path_swap_after_admission_cannot_publish_a_foreign_inode(listener):
    record, source_fd, directory_fd = prepared(listener)
    replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.source.unlink()
    replacement.bind(str(listener.source))
    try:
        with pytest.raises(RuntimeFailure, match="replaced during publication"):
            publication.publish(listener.source, record, source_fd, directory_fd)
        foreign = listener.directory / "final-pcm.sock"
        assert foreign.exists() and foreign.lstat().st_ino != record["socket_inode"]
        with pytest.raises(RuntimeFailure, match="listener changed"):
            publication.discard(record)
        assert foreign.exists()
    finally:
        replacement.close()
        os.close(source_fd)
        os.close(directory_fd)


def test_publication_directory_replacement_before_rename_is_preserved(listener):
    record, source_fd, directory_fd = prepared(listener)
    old = listener.directory.with_name("old")
    listener.directory.rename(old)
    listener.directory.mkdir(mode=0o700)
    try:
        with pytest.raises(RuntimeFailure, match="changed before commit"):
            publication.publish(listener.source, record, source_fd, directory_fd)
        assert listener.source.exists() and not list(listener.directory.iterdir())
        with pytest.raises(RuntimeFailure, match="directory was replaced"):
            publication.discard(record)
        assert listener.directory.exists() and old.exists()
    finally:
        os.close(source_fd)
        os.close(directory_fd)


@pytest.mark.parametrize("drift", ["uid", "gid", "mode", "foreign_file"])
def test_cleanup_retains_unproven_published_state(listener, drift):
    record, source_fd, directory_fd = prepared(listener)
    try:
        path = publication.publish(listener.source, record, source_fd, directory_fd)
        if drift == "foreign_file":
            (listener.directory / "unexpected").write_bytes(b"retain")
        else:
            listener.credentials[drift] = {"uid": 3003, "gid": 3003, "mode": 0o666}[drift]
        with pytest.raises(RuntimeFailure, match="reservation retained"):
            publication.discard(record)
        assert path.exists()
    finally:
        os.close(source_fd)
        os.close(directory_fd)


def test_empty_prepublication_intent_is_safe_to_discard(listener):
    record, source_fd, directory_fd = prepared(listener)
    try:
        publication.discard(record)
        assert listener.source.exists() and not listener.directory.exists()
    finally:
        os.close(source_fd)
        os.close(directory_fd)


def test_failed_directory_admission_removes_only_its_new_empty_directory(listener, monkeypatch):
    monkeypatch.setattr(publication, "open_directory", lambda _record: (_ for _ in ()).throw(RuntimeFailure("Admission failed")))
    with pytest.raises(RuntimeFailure, match="Admission failed"):
        prepared(listener)
    assert listener.source.exists() and not listener.directory.exists() and not listener.fds


def test_silent_permission_change_cannot_report_successful_publication(listener, monkeypatch):
    record, source_fd, directory_fd = prepared(listener)
    monkeypatch.setattr(os, "chmod", lambda *_args: None)
    try:
        with pytest.raises(RuntimeFailure, match="credentials changed"):
            publication.publish(listener.source, record, source_fd, directory_fd)
        assert (listener.directory / "final-pcm.sock").exists()
        publication.discard(record)
    finally:
        os.close(source_fd)
        os.close(directory_fd)


@pytest.mark.parametrize("change", [{"uid": True}, {"gid": 0}, {"socket_inode": -1},
                                   {"directory": "/etc/../bridge-published/" + "a" * 32}])
def test_publication_intent_rejects_malformed_authority(listener, change):
    record, source_fd, directory_fd = prepared(listener)
    try:
        with pytest.raises(RuntimeFailure, match="Invalid admitted"):
            publication.validate(record | change)
    finally:
        os.close(source_fd)
        os.close(directory_fd)
