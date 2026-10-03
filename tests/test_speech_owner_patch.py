"""Actual additive owner C execution and exact build/marker admission."""
import hashlib
import importlib.util
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("owner_patch_checker", ROOT / "tests/native/check_speech_owner.py")
CHECKER = importlib.util.module_from_spec(spec)
spec.loader.exec_module(CHECKER)


@pytest.mark.skipif(not (shutil.which("clang") or shutil.which("cc")), reason="Actual C requires a compiler")
def test_exact_owner_media_player_and_strict_json_preserve_original_budgets():
    result = CHECKER.check()
    assert result["passed"] and result["sanitizers"] and result["header_bytes"] == 96
    assert result["packet_age_ns"] == 250_000_000 and result["reserve_ns"] == 20_000_000
    assert not result["kernel_credential_proof"] and len(result["output"]) == 3


def test_build_digest_marker_and_required_credential_check_are_coherent():
    from shiri.runtime.broker import REQUIRED_OWNTONE_VERSION
    digest = hashlib.sha256(CHECKER.PATCH.read_bytes()).hexdigest()
    builder = (ROOT / "install/build_backends.sh").read_text()
    assert "OWNTONE_OWNER_SHA=" + digest in builder
    assert '"$OWNTONE_OWNER_PATCH:$OWNTONE_OWNER_SHA"' in builder
    assert builder.index("check_late_speech.py") < builder.index('apply "$OWNTONE_OWNER_PATCH"')
    assert builder.index('apply "$OWNTONE_OWNER_PATCH"') < builder.index("check_speech_owner.py")
    assert 'check_speech_owner.py" --source "$BUILD/owntone" --compiler /usr/bin/cc --require-credentials' in builder
    assert 'manifest["owntone_speech_owner_patch"] = sys.argv[23]' in builder
    assert REQUIRED_OWNTONE_VERSION == CHECKER.VERSION + "-balance1-transition1-bed1-event1-idle1"


def test_owner_marker_is_exact_and_patch_cannot_change_output_transport(tmp_path):
    patch = CHECKER.PATCH.read_text()
    broken = tmp_path / "unknown.patch"
    broken.write_text(patch.replace("-jitter1-owner1])", "-jitter1-owner10])", 1))
    with pytest.raises(ValueError, match="exact owner1 marker"):
        CHECKER.validate_patch(broken)
    broken.write_text(patch + "diff --git a/src/outputs/alsa.c b/src/outputs/alsa.c\n")
    with pytest.raises(ValueError, match="output transport"):
        CHECKER.validate_patch(broken)


@pytest.mark.skipif(not (shutil.which("clang") or shutil.which("cc")), reason="Actual C requires a compiler")
def test_actual_c_proof_rejects_removed_voice_incarnation_guard(tmp_path):
    patch = CHECKER.PATCH.read_text()
    before = "+      !s->accepting || !speech_owner_matches(s, packet + 80) ||"
    assert patch.count(before) == 1
    broken = tmp_path / "no-owner-guard.patch"
    broken.write_text(patch.replace(before, "+      !s->accepting ||", 1))
    with pytest.raises(subprocess.CalledProcessError):
        CHECKER.check(patch_path=broken)


def test_frozen_fourteen_layer_media_preimage_is_required(monkeypatch, tmp_path):
    monkeypatch.setitem(CHECKER.PREIMAGES, "shiri_speech.c", "0" * 64)
    with pytest.raises(ValueError, match="fourteen-layer media preimage"):
        CHECKER.assemble(tmp_path)
