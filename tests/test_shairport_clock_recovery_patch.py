"""Run current receiver recovery faults through the real patched C callback."""
import importlib.util
from pathlib import Path
import shutil

import pytest


def test_actual_timed3_callback_recovery_and_original_inner_guards_with_sanitizers():
    compiler = shutil.which("clang") or shutil.which("cc")
    if compiler is None:
        pytest.skip("A C compiler is required for the actual native clock recovery seam")
    path = Path(__file__).parent / "native/check_shairport_clock_recovery.py"
    spec = importlib.util.spec_from_file_location("native_clock_recovery_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.run_tests(compiler=compiler)
    assert result["ok"] and result["sanitized"]
    assert "four/<20ms recovery" in result["result"]
    assert result["original_sampler_sha256"] == "73116e82bd29b6a0a8e8c3072686a121ffaebe1528c9a94a643f11cb639fd917"
