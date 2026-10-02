"""Filesystem identity seam restricted to the two portable mock-unit modules."""

import os
from pathlib import Path

import pytest

from shiri.runtime import namespace_policy


PORTABLE_POLICY_MODULES = {"test_bind_policy.py", "test_namespace_policy.py"}
TEST_DIRECTORY = Path(__file__).resolve().parent


@pytest.fixture(autouse=True)
def isolated_policy_root(request, tmp_path, monkeypatch):
    module = Path(request.node.path).resolve()
    # Actual opt-in Linux lifecycle/kernel/managed-unit tests use production
    # /run/systemd/system and uid/gid0. Unknown future modules also stay real.
    if module.parent != TEST_DIRECTORY or module.name not in PORTABLE_POLICY_MODULES:
        return None
    root = (tmp_path / "systemd").resolve()
    root.mkdir(mode=0o755)
    monkeypatch.setattr(namespace_policy, "ROOT", root)
    owner = (os.getuid(), os.getgid())
    monkeypatch.setattr(
        namespace_policy,
        "_root_owned",
        lambda info: (info.st_uid, getattr(info, "st_gid", 0)) in {(0, 0), owner},
    )
    return root
