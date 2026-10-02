"""Portable actual filter checks; Linux kernel/ALSA proof remains separate."""
import importlib.util
from pathlib import Path
import shutil

import pytest


def test_exact_pcm_ioctl_filter_and_bounded_launch_arguments():
    compiler = shutil.which("clang") or shutil.which("cc")
    if not compiler:
        pytest.skip("A C compiler is required for the native PCM exec check")
    path = Path(__file__).parent / "native/check_pcm_exec.py"
    spec = importlib.util.spec_from_file_location("pcm_exec_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.run_tests(compiler)
    assert result["ok"] and result["sanitized"] and not result["kernel_tested"]
    assert "x32/compat/io_uring fenced" in result["output"]
