"""Exercise actual native buffered timing without network or speakers."""
import importlib.util
from pathlib import Path
import shutil

import pytest


def test_buffered_advance_preserves_realtime_sessions_and_source_clock():
    compiler = shutil.which("clang") or shutil.which("cc")
    if compiler is None:
        pytest.skip("A C compiler is required for native buffered timing checks")
    path = Path(__file__).parent / "native/check_shairport_buffered_timing.py"
    spec = importlib.util.spec_from_file_location("buffered_timing_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.run_tests(compiler=compiler)
    assert result["ok"] and result["sanitized"]
    native = result["results"]["shiri"]
    assert native["mapping_cases"] == 10818 and native["realtime_sessions"] == 200
    assert native["configuration_boundaries"] and native["advance_max_ms"] == 600
    assert native["max_quantization_error_ns"] <= 1_000_000_000 // 44100
    assert result["results"]["without_shiri"]["advance_max_ms"] == 0
