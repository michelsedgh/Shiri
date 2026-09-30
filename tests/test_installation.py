"""Trust-boundary regressions without privileged host changes."""

from contextlib import contextmanager
import importlib.util
import os
from pathlib import Path
import sqlite3
import stat
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def load_script(relative):
    source = Path(__file__).resolve().parents[1] / relative
    spec = importlib.util.spec_from_file_location(source.stem, source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def guard(tmp_path, monkeypatch):
    module = load_script("install/validate_installation.py")
    metadata = module.trusted_metadata
    monkeypatch.setattr(module, "ROOT_UID", os.getuid())

    def fixture_metadata(path, info):
        # Fixtures exercise actual file/link modes. System ancestors outside
        # this private fixture stand in for the root-owned Linux /opt path.
        if path.is_relative_to(tmp_path):
            metadata(path, info)

    monkeypatch.setattr(module, "trusted_metadata", fixture_metadata)
    return module


def test_custom_prefix_and_normal_venv_interpreter_links_are_allowed(guard, tmp_path):
    prefix = tmp_path / "custom" / "shiri"
    guard.validate_prefix(prefix, create=True)
    (prefix / "bin").mkdir()
    (prefix / "lib").mkdir()
    (prefix / "lib64").symlink_to("lib", target_is_directory=True)
    interpreter = tmp_path / "system-python"
    interpreter.write_text("trusted executable")
    interpreter.chmod(0o755)
    (prefix / "bin" / "python").symlink_to(interpreter)
    assert guard.validate_prefix(prefix) == prefix


@pytest.mark.parametrize("target", ["ancestor", "prefix", "file"])
def test_writable_installation_paths_are_rejected_without_repair(guard, tmp_path, target):
    ancestor = tmp_path / "parent"
    prefix = ancestor / "shiri"
    guard.validate_prefix(prefix, create=True)
    program = prefix / "program"
    program.write_bytes(b"unchanged")
    bad = {"ancestor": ancestor, "prefix": prefix, "file": program}[target]
    bad.chmod(0o777 if target != "file" else 0o666)
    before = bad.stat().st_mode
    with pytest.raises(guard.UnsafeInstallation, match="writable"):
        guard.validate_prefix(prefix)
    assert bad.stat().st_mode == before
    assert program.read_bytes() == b"unchanged"


def test_foreign_owner_and_set_id_metadata_are_rejected():
    guard = load_script("install/validate_installation.py")
    with pytest.raises(guard.UnsafeInstallation, match="root-owned"):
        guard.trusted_metadata(Path("/opt/shiri"), SimpleNamespace(st_uid=12345, st_mode=stat.S_IFREG | 0o755))
    with pytest.raises(guard.UnsafeInstallation, match="set-ID"):
        guard.trusted_metadata(Path("/opt/shiri/program"), SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o4755))


def test_symlink_prefix_cannot_redirect_creation_or_validation(guard, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(guard.UnsafeInstallation, match="real directory"):
        guard.validate_prefix(linked / "candidate", create=True)
    assert not (real / "candidate").exists()


@pytest.mark.parametrize("kind", ["external-directory", "fifo", "broken", "cycle"])
def test_untrusted_tree_members_are_preserved_and_rejected(guard, tmp_path, kind):
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    member = prefix / "member"
    if kind == "external-directory":
        external = tmp_path / "external"
        external.mkdir()
        member.symlink_to(external, target_is_directory=True)
    elif kind == "fifo":
        os.mkfifo(member, 0o600)
    elif kind == "broken":
        member.symlink_to("missing")
    else:
        member.symlink_to("other")
        (prefix / "other").symlink_to("member")
    before = member.lstat()
    with pytest.raises((guard.UnsafeInstallation, OSError)):
        guard.validate_prefix(prefix)
    assert member.lstat().st_ino == before.st_ino


def test_symlink_dotdot_resolution_checks_the_actual_target(guard, tmp_path):
    prefix, external, trap = (tmp_path / name for name in ("prefix", "external", "trap"))
    for directory in (prefix, external, trap, trap / "inner"):
        directory.mkdir()
    (external / "branch").symlink_to(trap / "inner", target_is_directory=True)
    (external / "python").write_text("safe decoy")
    (trap / "python").write_text("unsafe actual target")
    (trap / "python").chmod(0o666)
    (prefix / "python").symlink_to(str(external / "branch") + "/../python")
    with pytest.raises(guard.UnsafeInstallation, match="writable"):
        guard.trusted_target(prefix / "python")


@pytest.fixture
def state(tmp_path, monkeypatch):
    module = load_script("deploy/adopt_state.py")
    account = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
    database = tmp_path / "shiri.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA user_version=1")
    connection.execute("CREATE TABLE rooms (id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()
    database.chmod(0o600)
    monkeypatch.setattr(module.os, "fchown", Mock())
    monkeypatch.setattr(module.os, "fchmod", Mock())
    monkeypatch.setattr(module, "require_no_other_openers", Mock())

    @contextmanager
    def lock(descriptor):
        yield

    monkeypatch.setattr(module, "database_lock", lock)
    connect = module.sqlite3.connect

    def portable_connect(database_uri, **kwargs):
        assert database_uri.startswith("file:/proc/self/fd/") and database_uri.endswith("?mode=ro&immutable=1")
        assert kwargs == {"uri": True}
        return connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)

    monkeypatch.setattr(module.sqlite3, "connect", portable_connect)
    return module, database, account


def test_quiescent_valid_database_adopts_only_held_descriptor(state):
    module, database, account = state
    original = database.read_bytes()
    stopped = Mock()
    module.adopt_database(database, account, stopped_check=stopped)
    stopped.assert_called_once()
    descriptor = module.os.fchown.call_args.args[0]
    assert module.os.fchown.call_args.args == (descriptor, account.pw_uid, account.pw_gid)
    module.os.fchmod.assert_called_once_with(descriptor, 0o600)
    assert database.read_bytes() == original
    assert not list(database.parent.glob("*-wal")) and not list(database.parent.glob("*-shm"))


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
@pytest.mark.parametrize("kind", ["empty", "data", "dangling-link"])
def test_every_retained_sqlite_companion_refuses_adoption_and_preserves_bytes(state, suffix, kind):
    module, database, account = state
    companion = Path(str(database) + suffix)
    if kind == "dangling-link":
        companion.symlink_to("missing")
    else:
        companion.write_bytes(b"committed bytes" if kind == "data" else b"")
    original, inode = database.read_bytes(), companion.lstat().st_ino
    with pytest.raises(module.UnsafeState, match="checkpoint"):
        module.adopt_database(database, account)
    assert database.read_bytes() == original and companion.lstat().st_ino == inode
    module.os.fchown.assert_not_called()
    module.os.fchmod.assert_not_called()


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
@pytest.mark.parametrize("main", ["missing", "empty"])
def test_orphaned_companions_prevent_creation_or_adoption_of_a_database(state, suffix, main):
    module, database, account = state
    if main == "missing":
        database.unlink()
    else:
        database.write_bytes(b"")
    companion = Path(str(database) + suffix)
    companion.write_bytes(b"possibly committed data")
    with pytest.raises(module.UnsafeState, match="preserve"):
        module.adopt_database(database, account)
    assert companion.read_bytes() == b"possibly committed data"
    assert database.exists() is (main == "empty")
    module.os.fchown.assert_not_called()


@pytest.mark.parametrize("change", ["replace-main", "new-companion", "writable-main", "service-starts"])
def test_validation_races_cannot_chown_replaced_or_live_state(state, change):
    module, database, account = state
    connect = module.sqlite3.connect

    def changing_connect(database_uri, **kwargs):
        connection = connect(database_uri, **kwargs)
        if change == "replace-main":
            database.rename(database.with_suffix(".preserved"))
            database.write_bytes(b"foreign replacement")
        elif change == "new-companion":
            Path(str(database) + "-wal").write_bytes(b"retained committed frames")
        elif change == "writable-main":
            database.chmod(0o666)
        return connection

    module.sqlite3.connect = changing_connect

    def stopped():
        if change == "service-starts":
            raise module.UnsafeState("service started")

    with pytest.raises(module.UnsafeState):
        module.adopt_database(database, account, stopped_check=stopped)
    module.os.fchown.assert_not_called()
    module.os.fchmod.assert_not_called()


@pytest.mark.parametrize("kind", ["hardlink", "symlink", "writable", "invalid"])
def test_unknown_database_files_are_not_adopted(state, kind):
    module, database, account = state
    if kind == "hardlink":
        os.link(database, database.with_suffix(".hardlink"))
    elif kind == "symlink":
        preserved = database.with_suffix(".preserved")
        database.rename(preserved)
        database.symlink_to(preserved)
    elif kind == "writable":
        database.chmod(0o666)
    else:
        database.write_bytes(b"not a SQLite file")
    before = database.read_bytes()
    with pytest.raises((module.UnsafeState, OSError)):
        module.adopt_database(database, account)
    assert database.read_bytes() == before
    module.os.fchown.assert_not_called()


def test_idle_connection_in_another_process_is_not_quiescent(tmp_path):
    module = load_script("deploy/adopt_state.py")
    database = tmp_path / "database"
    database.write_bytes(b"fixture")
    proc = tmp_path / "proc"
    fd = proc / str(os.getpid() + 100000) / "fd"
    fd.mkdir(parents=True)
    (fd / "4").symlink_to(database)
    with pytest.raises(module.UnsafeState, match="open by PID"):
        module.require_no_other_openers(database.stat(), process_root=proc)


def test_active_service_cannot_be_adopted(monkeypatch):
    module = load_script("deploy/adopt_state.py")
    monkeypatch.setattr(module.subprocess, "run", Mock(return_value=SimpleNamespace(
        returncode=0, stdout="ActiveState=active\nMainPID=42\n", stderr="",
    )))
    with pytest.raises(module.UnsafeState, match="Stop shiri-api"):
        module.require_api_stopped()


def test_existing_token_is_preserved_and_adopted_by_descriptor(tmp_path, monkeypatch):
    module = load_script("deploy/adopt_state.py")
    token = tmp_path / "api-token"
    original = b"private-fixture-token-12345678901234567890\n"
    token.write_bytes(original)
    token.chmod(0o600)
    account = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
    ownership, permissions = Mock(), Mock()
    monkeypatch.setattr(module.os, "fchown", ownership)
    monkeypatch.setattr(module.os, "fchmod", permissions)
    module.adopt_token(token, account)
    descriptor = ownership.call_args.args[0]
    ownership.assert_called_once_with(descriptor, 0, account.pw_gid)
    permissions.assert_called_once_with(descriptor, 0o640)
    assert token.read_bytes() == original


def test_token_replaced_during_read_cannot_be_chowned(tmp_path, monkeypatch):
    module = load_script("deploy/adopt_state.py")
    token = tmp_path / "api-token"
    token.write_text("fixture-token-123456789012345678901234567890")
    token.chmod(0o600)
    account = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
    read = os.read

    def replaced_read(descriptor, count):
        data = read(descriptor, count)
        token.rename(tmp_path / "preserved-token")
        token.write_bytes(b"replacement")
        return data

    monkeypatch.setattr(module.os, "read", replaced_read)
    ownership = Mock()
    monkeypatch.setattr(module.os, "fchown", ownership)
    with pytest.raises(module.UnsafeState, match="changed"):
        module.adopt_token(token, account)
    ownership.assert_not_called()
    assert token.read_bytes() == b"replacement"


def test_fifo_token_is_rejected_before_opening(tmp_path, monkeypatch):
    module = load_script("deploy/adopt_state.py")
    token = tmp_path / "api-token"
    os.mkfifo(token, 0o600)
    opener = Mock()
    monkeypatch.setattr(module.os, "open", opener)
    with pytest.raises(module.UnsafeState, match="nonregular"):
        module.adopt_token(token, SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid()))
    opener.assert_not_called()
