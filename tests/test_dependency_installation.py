"""Dependency installs preserve host service policy and forbid restart hooks."""

from contextlib import contextmanager
import importlib.util
import os
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def installer(monkeypatch):
    source = Path(__file__).resolve().parents[1] / "install/apt_dependencies.py"
    spec = importlib.util.spec_from_file_location("apt_dependencies", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Exercise real private file identities and mode changes without root.
    original = module.root_metadata

    def fixture_metadata(info, **kwargs):
        metadata = SimpleNamespace(st_mode=info.st_mode, st_uid=0, st_nlink=info.st_nlink)
        original(metadata, **kwargs)

    monkeypatch.setattr(module, "root_metadata", fixture_metadata)
    monkeypatch.setattr(module, "trusted_directory", lambda path: None)
    return module


@pytest.mark.parametrize("has_original", [True, False])
@pytest.mark.parametrize("fail", [True, False])
def test_temporary_policy_denies_all_and_restores_original_inode(installer, tmp_path, has_original, fail):
    policy = tmp_path / "policy-rc.d"
    content = b"#!/bin/sh\n# Existing operator policy\nexit 0\n"
    if has_original:
        policy.write_bytes(content)
        policy.chmod(0o755)
        inode = policy.stat().st_ino
    with pytest.raises(ValueError) if fail else passthrough():
        with installer.deny_service_actions(policy):
            installed = policy.read_bytes()
            assert installer.MARKER in installed
            assert installed.endswith(b"exit 101\n")
            assert policy.stat().st_mode & 0o777 == 0o755
            assert subprocess.run([str(policy), "avahi-daemon", "restart"], check=False).returncode == 101
            if fail:
                raise ValueError("apt failed")
    if has_original:
        assert policy.read_bytes() == content
        assert policy.stat().st_ino == inode
        assert policy.stat().st_nlink == 1
    else:
        assert not policy.exists()
    assert not list(tmp_path.glob(".shiri-policy-*"))


@contextmanager
def passthrough():
    yield


def test_concurrent_policy_replacement_is_preserved_with_recovery_backup(installer, tmp_path):
    policy = tmp_path / "policy-rc.d"
    policy.write_bytes(b"original bytes")
    policy.chmod(0o755)
    with pytest.raises(RuntimeError, match="changed concurrently"):
        with installer.deny_service_actions(policy):
            replacement = tmp_path / "new-policy"
            replacement.write_bytes(b"new operator policy")
            replacement.replace(policy)
    assert policy.read_bytes() == b"new operator policy"
    backups = list(tmp_path.glob(".shiri-policy-*/original"))
    assert len(backups) == 1 and backups[0].read_bytes() == b"original bytes"


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "writable", "interrupted"])
def test_unsafe_or_interrupted_policy_never_runs_apt_or_replaces_bytes(installer, tmp_path, kind):
    policy = tmp_path / "policy-rc.d"
    original = tmp_path / "other"
    original.write_bytes(b"operator policy")
    if kind == "symlink":
        policy.symlink_to(original)
    else:
        policy.write_bytes(installer.MARKER + b"saved backup" if kind == "interrupted" else b"operator policy")
        policy.chmod(0o666 if kind == "writable" else 0o755)
        if kind == "hardlink":
            os.link(policy, tmp_path / "linked")
    content = policy.read_bytes()
    with pytest.raises(RuntimeError):
        with installer.deny_service_actions(policy):
            pytest.fail("Unsafe policy admitted")
    assert policy.read_bytes() == content


def test_apt_list_only_environment_and_explicit_package_names(installer, monkeypatch):
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    lock, policy = Mock(side_effect=passthrough), Mock(side_effect=passthrough)
    monkeypatch.setattr(installer, "installation_lock", lock)
    monkeypatch.setattr(installer, "deny_service_actions", policy)
    run = Mock()
    monkeypatch.setattr(installer.subprocess, "run", run)
    installer.install(["autopoint", "intltool", "libexpat1-dev"])
    assert run.call_count == 2
    assert run.call_args_list[0].args[0] == ["/usr/bin/apt-get", "update"]
    assert run.call_args_list[1].args[0] == ["/usr/bin/apt-get", "install", "-y", "autopoint", "intltool", "libexpat1-dev"]
    environment = run.call_args.kwargs["env"]
    assert environment["NEEDRESTART_MODE"] == "l"
    assert environment["DEBIAN_FRONTEND"] == "noninteractive"
    assert "LD_PRELOAD" not in environment
    for packages in ([], ["--reinstall"], ["pkg; touch /tmp/unowned"], ["pkg=1"]):
        with pytest.raises(RuntimeError, match="package names"):
            installer.install(packages)
    assert run.call_count == 2


def test_foreign_owner_policy_rejected():
    source = Path(__file__).resolve().parents[1] / "install/apt_dependencies.py"
    spec = importlib.util.spec_from_file_location("apt_policy_metadata", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(RuntimeError, match="root-owned"):
        module.root_metadata(SimpleNamespace(st_uid=1001, st_mode=stat.S_IFREG | 0o755, st_nlink=1))
