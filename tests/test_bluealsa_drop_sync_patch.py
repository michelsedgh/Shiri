"""The maintained synchronous barrier compiles actual source seams and preimages."""
import importlib.util
from pathlib import Path
import shutil

import pytest


def test_actual_bluealsa_drop_sync_and_stock_preimage_with_sanitizers():
    compiler = shutil.which("clang") or shutil.which("cc")
    if not compiler:
        pytest.skip("A C compiler is required for the maintained BlueALSA DropSync checks")
    path = Path(__file__).parent / "native/check_bluealsa_drop_sync.py"
    spec = importlib.util.spec_from_file_location("bluealsa_c_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.run_tests(compiler=compiler)
    assert result["ok"] and result["sanitized"]
    assert "41 actual request/poll/codec/controller cases" in result["results"][0]
    assert "stock Drop OK precedes" in result["results"][1]
