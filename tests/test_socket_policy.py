"""Portable actual-C checks; kernel attach proof remains a Linux gate."""
import importlib.util
from pathlib import Path
import shutil

import pytest


def test_exact_socket_policy_emitter_and_failclosed_lifecycle():
    compiler = shutil.which("clang") or shutil.which("cc")
    if not compiler:
        pytest.skip("A C compiler is required for the native socket-policy check")
    path = Path(__file__).parent / "native/check_socket_policy.py"
    spec = importlib.util.spec_from_file_location("socket_policy_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.run_tests(compiler)
    assert result["ok"] and result["sanitized"] and not result["kernel_tested"]
    assert "every attach fault rolled back" in result["output"]
