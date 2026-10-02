"""Exercise retained native SETUP source and receive-thread ownership guards."""
import importlib.util
from pathlib import Path
import shutil

import pytest


def test_synchronous_native_setup_and_exact_retirement_reference_with_sanitizers():
    compiler = shutil.which("clang") or shutil.which("cc")
    if compiler is None:
        pytest.skip("A C compiler is required for native startup lifecycle checks")
    path = Path(__file__).parent / "native/check_shairport_startup.py"
    spec = importlib.util.spec_from_file_location("native_startup_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.run_tests(compiler=compiler)
    assert result["ok"] and result["sanitized"] and result["startup_cases"] == 14
