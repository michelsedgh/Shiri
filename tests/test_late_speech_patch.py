"""Meaningful native execution and rejected preimages for the late overlay."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("late_speech_checker", ROOT / "tests/native/check_late_speech.py")
assert SPEC and SPEC.loader
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)


def test_actual_c_late_speech_parser_queue_gain_and_retirement():
    if not (shutil.which("clang") or shutil.which("cc")):
        pytest.skip("Actual C proof requires a compiler")
    result = CHECKER.check(None, CHECKER.PATCH, None, True)
    assert result["passed"]
    assert "34715 actual-source checks passed" in result["output"]
    assert not result["kernel_credential_proof"]


def test_stale_pcm_preimage_is_rejected_by_the_actual_c_test(tmp_path):
    if not (shutil.which("clang") or shutil.which("cc")):
        pytest.skip("Actual C proof requires a compiler")
    patch = CHECKER.PATCH.read_text()
    guard = "now - emitted >= SPEECH_AGE_NS"
    assert patch.count(guard) == 1
    broken = tmp_path / "accept-expired.patch"
    broken.write_text(patch.replace(guard, "0", 1))
    import subprocess
    with pytest.raises(subprocess.CalledProcessError):
        CHECKER.check(None, broken, None, True)


def test_missing_common_player_seam_is_rejected():
    patch = CHECKER.PATCH.read_text()
    with pytest.raises(ValueError, match="playback tick"):
        CHECKER.validate_integration(patch.replace("+  shiri_speech_poll();", "", 1), None)


@pytest.mark.skipif(sys.platform != "linux" or os.geteuid() != 0
                    or os.environ.get("SHIRI_TEST_LINUX_SPEECH") != "1",
                    reason="Opt-in Linux/root private credential fixture")
def test_real_linux_audio_uid_ancillary_rights_and_cleanup():
    result = CHECKER.check(None, CHECKER.PATCH, None, True, credentials=True)
    assert result["kernel_credential_proof"]
    assert "real UID admission" in result["socket_output"]
