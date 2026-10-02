"""Prepared IPC credentials must survive Linux's silent mode-bit stripping."""
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from shiri.runtime import layout
from shiri.runtime.system import RuntimeFailure


@pytest.fixture
def metadata(monkeypatch):
    # Real directories/inodes/mode updates; replace only privileged chown on
    # portable hosts. The admission must inspect the actual resulting mode.
    original = Path.lstat
    owners = {}

    def observed(path):
        info = original(path)
        uid, gid = owners.get(path, (0, 0))
        return SimpleNamespace(st_mode=info.st_mode, st_dev=info.st_dev, st_ino=info.st_ino,
                               st_uid=uid, st_gid=gid)

    def changed(path, uid, gid, *, follow_symlinks):
        assert follow_symlinks is False
        owners[Path(path)] = (uid, gid)

    monkeypatch.setattr(Path, 'lstat', observed)
    monkeypatch.setattr(layout.os, 'chown', changed)
    return owners


@pytest.mark.parametrize('mode', [0o700, 0o750, 0o2750])
def test_directory_accepts_only_the_actual_requested_credentials_and_mode(tmp_path, metadata, mode):
    path = tmp_path/'input'
    assert layout.directory(path, {'uid': 1001, 'gid': 2002}, mode=mode) == path
    actual = path.lstat()
    assert (actual.st_uid, actual.st_gid, stat.S_IMODE(actual.st_mode)) == (1001, 2002, mode)


def test_silently_stripped_setgid_never_admits_wrong_socket_group(tmp_path, metadata, monkeypatch):
    original = Path.chmod
    monkeypatch.setattr(Path, 'chmod', lambda path, mode: original(path, mode & ~stat.S_ISGID))
    path = tmp_path/'input'
    with pytest.raises(RuntimeFailure, match='exact credentials and mode'):
        layout.directory(path, {'uid': 1001, 'gid': 2002}, mode=0o2750)
    assert path.is_dir() and stat.S_IMODE(path.lstat().st_mode) == 0o750
    assert metadata[path] == (1001, 2002)


@pytest.mark.parametrize('wrong', [(0, 2002), (1001, 0)])
def test_silent_owner_or_group_change_failure_cannot_admit_a_directory(tmp_path, metadata, monkeypatch, wrong):
    path = tmp_path/'input'
    monkeypatch.setattr(layout.os, 'chown', lambda *_args, **_kwargs: metadata.update({path: wrong}))
    with pytest.raises(RuntimeFailure, match='exact credentials and mode'):
        layout.directory(path, {'uid': 1001, 'gid': 2002}, mode=0o750)
    assert path.is_dir()


def test_equally_safe_directory_replacement_is_not_the_admitted_inode(tmp_path, metadata, monkeypatch):
    path, old = tmp_path/'input', tmp_path/'retired'
    original = Path.chmod

    def replaced(current, mode):
        original(current, mode)
        current.rename(old)
        current.mkdir(mode=mode)

    monkeypatch.setattr(Path, 'chmod', replaced)
    with pytest.raises(RuntimeFailure, match='exact credentials and mode'):
        layout.directory(path, {'uid': 1001, 'gid': 2002}, mode=0o750)
    assert path.is_dir() and old.is_dir() and path.lstat().st_ino != old.lstat().st_ino
