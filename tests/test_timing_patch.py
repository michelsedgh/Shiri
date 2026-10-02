"""Compile the actual pinned native transport/timer functions under sanitizers."""
import importlib.util
from pathlib import Path
import shutil

import pytest


def test_native_timing_callbacks_pipe_markers_and_both_player_timers_with_sanitizers():
    compiler = shutil.which("clang") or shutil.which("cc")
    if compiler is None:
        pytest.skip("A C compiler is required for the actual native timing seam")
    path = Path(__file__).parent / "native/check_timing_patch.py"
    spec = importlib.util.spec_from_file_location("timing_c_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.run_tests(compiler=compiler)
    assert result["ok"] and result["sanitized"]
    assert len(result["results"]) == 4
    assert "1.347960ms bracket sent invalid PCM" in result["clock_preimage"]
    assert "4-attempt/5ms clock retry" in result["results"][1]
