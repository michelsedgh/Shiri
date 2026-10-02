"""Actual C reserve tests and rejected timing/identity preimages."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("speech_jitter_checker", ROOT / "tests/native/check_speech_jitter.py")
assert SPEC and SPEC.loader
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)


@pytest.fixture(autouse=True)
def compiler_required():
    if not (shutil.which("clang") or shutil.which("cc")):
        pytest.skip("The actual maintained C seam requires a compiler")
    if not shutil.which("pkg-config"):
        pytest.skip("The actual HTTP status seam requires pkg-config/json-c")


def test_exact_c_reserve_preimage_eof_freshness_and_status():
    result = CHECKER.check()
    assert result["passed"] and result["reserve_ns"] == 20_000_000
    assert result["packet_age_ns"] == 250_000_000
    assert not result["kernel_credential_proof"]
    assert "480 silent voice frames" in result["output"][0]
    assert "ordered prefix/tail" in result["output"][1]
    assert "current source identity,read-only,bounded,typed" in result["output"][2]


@pytest.mark.parametrize("before,after", [
    ("+      s->running = 1; s->priming_until = now + SPEECH_RESERVE_NS;",
     "+      s->running = 1; s->priming_until = now;"),
    ("+  out->source = shiri_source_state.last;",
     "+  memset(&out->source, 0, sizeof(out->source));"),
])
def test_actual_c_rejects_missing_reserve_and_unbound_status(tmp_path, before, after):
    patch = CHECKER.PATCH.read_text()
    assert patch.count(before) == 1
    broken = tmp_path / "broken.patch"
    broken.write_text(patch.replace(before, after, 1))
    with pytest.raises(subprocess.CalledProcessError):
        CHECKER.check(patch_path=broken)


def test_frozen_preimage_is_required(monkeypatch, tmp_path):
    monkeypatch.setattr(CHECKER, "PREIMAGE_C_SHA256", "0" * 64)
    with pytest.raises(ValueError, match="preimage changed"):
        CHECKER.baseline(tmp_path)


def test_unknown_marker_cannot_admit_the_reserve(tmp_path):
    patch = CHECKER.PATCH.read_text()
    assert patch.count(CHECKER.MARKER) == 1
    broken = tmp_path / "unknown-marker.patch"
    broken.write_text(patch.replace(CHECKER.MARKER, "-speech1-ready1-anchor1-jitter10])", 1))
    with pytest.raises(ValueError, match="exact jitter1 marker"):
        CHECKER.check(patch_path=broken)


def test_new_status_snapshot_does_not_relax_ready_ack_schema():
    patch = CHECKER.PATCH.read_text()
    assert "shiri_speech_ready_json.h" not in patch
    assert "shiri_speech_ready_request_parse" not in patch
    assert '"^/api/player/shiri-speech-status$"' in patch
    assert "room_launch_lifetime; current_source_at_read_only_snapshot" in patch
    assert "current_run_first_pcm_admitted_monotonic_ns" in patch
    changed = "\n".join(line for line in patch.splitlines() if line.startswith(("+", "-")))
    assert "SPEECH_AGE_NS UINT64_C(250000000)" not in changed  # context is unchanged
    assert "source_read" not in changed and "outputs_write" not in changed
