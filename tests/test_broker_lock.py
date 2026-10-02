"""Actual-file singleton admission; unsafe files and lock races preserve state."""
import fcntl
import os
import stat

import pytest

from shiri.runtime.broker import Broker
from shiri.runtime.system import RuntimeFailure


def existing(tmp_path, mode=0o600):
    path = tmp_path/'broker.lock'
    path.write_bytes(b'retained-operator-state\n')
    path.chmod(mode)
    return path


def identity(path):
    info = path.lstat()
    return info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink


def test_created_lock_is_private_cloexec_and_actually_excludes_another_owner(tmp_path):
    path = tmp_path/'broker.lock'
    with Broker._singleton_lock(path, os.geteuid()) as handle:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert not os.get_inheritable(handle.fileno())
        assert os.get_blocking(handle.fileno()) is False
        with pytest.raises(RuntimeFailure, match='Another Shiri runtime'):
            Broker._singleton_lock(path, os.geteuid())
    with Broker._singleton_lock(path, os.geteuid()):
        pass
    assert path.read_bytes() == b''


@pytest.mark.parametrize('mode', [0o644, 0o660, 0o666, 0o700, 0o400, 0o000, 0o4600, 0o2600, 0o1600])
def test_unsafe_existing_modes_are_refused_without_metadata_or_content_changes(tmp_path, mode):
    path = existing(tmp_path, mode)
    before = identity(path)
    with pytest.raises(RuntimeFailure, match='0600 regular file'):
        Broker._singleton_lock(path, os.geteuid())
    assert identity(path) == before
    path.chmod(0o600)  # Test-owned repair only, after comparing all original metadata.
    assert path.read_bytes() == b'retained-operator-state\n'


def test_foreign_owner_is_refused_without_chown_or_file_replacement(tmp_path):
    path = existing(tmp_path)
    before = identity(path), path.read_bytes()
    with pytest.raises(RuntimeFailure, match='0600 regular file'):
        Broker._singleton_lock(path, os.geteuid()+1)
    assert (identity(path), path.read_bytes()) == before


def test_hardlink_is_refused_and_both_existing_names_remain(tmp_path):
    path = existing(tmp_path)
    alias = tmp_path/'operator-retained.lock'
    os.link(path, alias)
    before = identity(path), path.read_bytes()
    with pytest.raises(RuntimeFailure, match='one link'):
        Broker._singleton_lock(path, os.geteuid())
    assert (identity(path), path.read_bytes()) == before
    assert identity(alias) == identity(path)


def test_symlink_is_refused_without_opening_or_altering_its_target(tmp_path, monkeypatch):
    target = existing(tmp_path)
    link = tmp_path/'unsafe.lock'
    link.symlink_to(target)
    before = identity(link), identity(target), target.read_bytes()
    monkeypatch.setattr(os, 'open', lambda *_: pytest.fail('An unsafe symlink was opened'))
    with pytest.raises(RuntimeFailure, match='regular file'):
        Broker._singleton_lock(link, os.geteuid())
    assert (identity(link), identity(target), target.read_bytes()) == before


def test_fifo_is_rejected_before_any_potentially_blocking_open(tmp_path, monkeypatch):
    path = tmp_path/'broker.lock'
    os.mkfifo(path, 0o600)
    before = identity(path)
    monkeypatch.setattr(os, 'open', lambda *_: pytest.fail('A special file was opened'))
    with pytest.raises(RuntimeFailure, match='regular file'):
        Broker._singleton_lock(path, os.geteuid())
    assert identity(path) == before


@pytest.mark.parametrize('stage', ['before-open', 'before-flock', 'after-flock'])
def test_named_inode_replacement_is_rejected_and_both_files_are_preserved(tmp_path, monkeypatch, stage):
    path = existing(tmp_path)
    retained = tmp_path/'retained.lock'
    opening, locking = os.open, fcntl.flock
    opened = []

    def replace():
        path.rename(retained)
        path.write_bytes(b'new-root-owned-inode\n')
        path.chmod(0o600)

    def open_then_replace(*args):
        if stage == 'before-open':
            replace()
        descriptor = opening(*args)
        opened.append(descriptor)
        if stage == 'before-flock':
            replace()
        return descriptor

    def lock_then_replace(descriptor, operation):
        locking(descriptor, operation)
        if stage == 'after-flock':
            replace()

    monkeypatch.setattr(os, 'open', open_then_replace)
    monkeypatch.setattr(fcntl, 'flock', lock_then_replace)
    with pytest.raises(RuntimeFailure, match='was replaced'):
        Broker._singleton_lock(path, os.geteuid())
    assert path.read_bytes() == b'new-root-owned-inode\n'
    assert retained.read_bytes() == b'retained-operator-state\n'
    assert path.stat().st_ino != retained.stat().st_ino
    with pytest.raises(OSError):
        os.fstat(opened[0])
    with retained.open('a') as handle:
        locking(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)  # Failed admission released its held-inode lock.


def test_metadata_changed_during_exclusive_lock_is_refused_and_preserved(tmp_path, monkeypatch):
    path = existing(tmp_path)
    locking = fcntl.flock

    def changing(descriptor, operation):
        locking(descriptor, operation)
        path.chmod(0o644)

    monkeypatch.setattr(fcntl, 'flock', changing)
    with pytest.raises(RuntimeFailure, match='0600 regular file'):
        Broker._singleton_lock(path, os.geteuid())
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert path.read_bytes() == b'retained-operator-state\n'


def test_regular_path_swapped_to_fifo_at_open_cannot_block(tmp_path, monkeypatch):
    path = existing(tmp_path)
    opening = os.open
    retained = tmp_path/'retained.lock'

    def race(*args):
        assert args[1] & os.O_NONBLOCK and args[1] & os.O_NOFOLLOW and args[1] & os.O_CLOEXEC
        path.rename(retained)
        os.mkfifo(path, 0o600)
        return opening(*args)

    monkeypatch.setattr(os, 'open', race)
    with pytest.raises(RuntimeFailure, match='cannot be admitted safely'):
        Broker._singleton_lock(path, os.geteuid())
    assert stat.S_ISFIFO(path.lstat().st_mode)
    assert retained.read_bytes() == b'retained-operator-state\n'
