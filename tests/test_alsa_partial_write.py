"""Run the maintained final ALSA queue layer through real C function seams."""
import importlib.util
from pathlib import Path
import shutil

import pytest


def test_actual_alsa_tail_admission_gain_drain_and_exact_source_flush_with_sanitizers():
    compiler = shutil.which('clang') or shutil.which('cc')
    if compiler is None:
        pytest.skip('The actual ALSA queue seam requires a C compiler')
    spec = importlib.util.spec_from_file_location('alsa_partial_c_check', Path(__file__).parent/'native/check_alsa_partial_write.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.run_tests(compiler=compiler)
    assert result['ok'] and result['sanitized'] and len(result['results']) == 1
