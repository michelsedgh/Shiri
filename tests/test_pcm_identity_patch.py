"""Check actual opened-hardware identity code with its real JSON-C parser."""
import importlib.util
from pathlib import Path
import shutil
import subprocess

import pytest


def test_actual_opened_pcm_guard_parser_and_wrong_card_preimage_under_sanitizers():
    compiler=shutil.which('clang') or shutil.which('cc')
    if compiler is None or shutil.which('pkg-config') is None:
        pytest.skip('C compiler and pkg-config are required for the PCM guard test')
    if subprocess.run(['pkg-config','--exists','json-c'],timeout=10).returncode:
        pytest.skip('libjson-c development files are required for the actual guard parser')
    path=Path(__file__).parent/'native/check_pcm_identity.py'
    spec=importlib.util.spec_from_file_location('pcm_identity_c_check',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result=module.run_tests(compiler=compiler)
    assert result['ok'] and result['sanitized'] and result['upstream_wrong_pcm_reproduced']
