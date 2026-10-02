"""Compile actual backend arithmetic seams and their failing preimages."""
import importlib.util
from pathlib import Path
import shutil

import pytest


def test_actual_offset_arithmetic_with_address_and_undefined_behavior_sanitizers():
    compiler = shutil.which("clang") or shutil.which("cc")
    if not compiler:
        pytest.skip("C compiler required for the maintained backend arithmetic patch")
    path = Path(__file__).parent / "native/check_offset_arithmetic.py"
    spec = importlib.util.spec_from_file_location("offset_c_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.run_tests(compiler=compiler)
    assert result["ok"] and result["sanitized"]
    assert len(result["results"]) == 8
    assert "preimage" in result["results"][1]
    assert "21 request/admission cases" in result["results"][5]
    assert "INT_MAX request sequence increment" in result["results"][-1]
