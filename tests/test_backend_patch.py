"""Run the exact patch's portable PCM scaler and final-write seam under sanitizers."""

import importlib.util
from pathlib import Path
import shutil

import pytest


def test_pinned_pcm_patch_with_address_and_undefined_behavior_sanitizers():
    compiler = shutil.which("clang") or shutil.which("cc")
    if not compiler:
        pytest.skip("C compiler required for the standalone backend sanitizer check")
    path = Path(__file__).parent / "native/check_owntone_patch.py"
    spec = importlib.util.spec_from_file_location("backend_c_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.run_tests(compiler=compiler)
    assert result["ok"] and result["sanitized"]
    assert len(result["results"]) == 2


def test_actual_source_transition_callbacks_and_partial_reads_with_sanitizers():
    compiler = shutil.which("clang") or shutil.which("cc")
    if not compiler:
        pytest.skip("C compiler required for the native source-transition check")
    path = Path(__file__).parent / "native/check_source_transition.py"
    spec = importlib.util.spec_from_file_location("source_c_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.run_tests(compiler=compiler)
    assert result["ok"] and result["sanitized"]
    assert len(result["results"]) == 5 + int(result["json_parser_tested"])
