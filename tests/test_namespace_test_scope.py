"""Portable fixture scope cannot mask the real Linux policy authority."""

from pathlib import Path
from types import SimpleNamespace

import pytest

import conftest
from shiri.runtime import namespace_policy as policy


@pytest.mark.parametrize("module", ["test_linux_lifecycle.py",
                                   "test_native_lab_work_permissions.py", "test_future_native_units.py"])
def test_actual_linux_and_unknown_modules_keep_real_root_and_uid_enforcement(module, tmp_path, monkeypatch):
    original_root, original_owner = policy.ROOT, policy._root_owned
    assert original_root == Path("/run/systemd/system")
    request = SimpleNamespace(node=SimpleNamespace(path=conftest.TEST_DIRECTORY / module))
    assert conftest.isolated_policy_root.__wrapped__(request, tmp_path, monkeypatch) is None
    assert policy.ROOT == original_root and policy._root_owned is original_owner
    assert policy._root_owned(SimpleNamespace(st_uid=0, st_gid=0)) is True
    assert policy._root_owned(SimpleNamespace(st_uid=501, st_gid=20)) is False
    assert not (tmp_path / "systemd").exists()


def test_similarly_named_actual_module_in_another_directory_cannot_enable_seam(tmp_path, monkeypatch):
    original_root, original_owner = policy.ROOT, policy._root_owned
    request = SimpleNamespace(node=SimpleNamespace(path=conftest.TEST_DIRECTORY / "linux/test_bind_policy.py"))
    assert conftest.isolated_policy_root.__wrapped__(request, tmp_path, monkeypatch) is None
    assert policy.ROOT == original_root and policy._root_owned is original_owner


@pytest.mark.parametrize("module", sorted(conftest.PORTABLE_POLICY_MODULES))
def test_only_declared_mock_unit_modules_receive_private_real_file_root(module, tmp_path, monkeypatch):
    request = SimpleNamespace(node=SimpleNamespace(path=conftest.TEST_DIRECTORY / module))
    result = conftest.isolated_policy_root.__wrapped__(request, tmp_path, monkeypatch)
    assert result == (tmp_path / "systemd").resolve() and policy.ROOT == result
    assert result.is_dir() and result.stat().st_mode & 0o777 == 0o755
