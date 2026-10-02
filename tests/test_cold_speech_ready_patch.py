"""Execute actual maintained callback/player code and reject sensitive preimages."""

import importlib.util
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "cold_speech_checker", ROOT / "tests/native/check_cold_speech_ready.py"
)
assert SPEC and SPEC.loader
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)


@pytest.fixture(autouse=True)
def compiler():
    if not (shutil.which("clang") or shutil.which("cc")) or not shutil.which("pkg-config"):
        pytest.skip("Actual C proof requires compiler and json-c development package")
    probe = subprocess.run(["pkg-config", "--exists", "json-c"], check=False, timeout=10)
    if probe.returncode:
        pytest.skip("Actual parser proof requires json-c development package")


def test_actual_player_deadline_output_callbacks_first_mix_and_json():
    result = CHECKER.check()
    assert result["passed"] and result["sanitizers"]
    assert result["checks"] == [
        "2456 exact player readiness checks passed",
        "8 strict readiness JSON checks passed",
    ]
    assert not result["kernel_or_physical_output_proof"]
    assert result["callback_preimage_sha256"] == CHECKER.CALLBACK_PREIMAGE_SHA256


@pytest.mark.parametrize(
    "guard,replacement",
    [
        ("evtimer_add(shiri_speech_setup_event, &deadline) < 0", "1"),
        ("outputs_shiri_stop_delayed_cancel(device)", "(outputs_stop_delayed_cancel(), 0)"),
        (
            "outputs_shiri_callback_pending(shiri_speech_start.outputs[index].id, device_shiri_speech_start_cb)",
            "true",
        ),
        ("now - param->reply.mixed_ns < SHIRI_SPEECH_MIX_AGE_NS", "1"),
        (
            "return COMMAND_END; /* Always release the ordinary player command lane. */",
            "return COMMAND_PENDING;",
        ),
    ],
)
def test_missing_backend_deadline_retirement_freshness_or_command_release_fails_actual_c(
    tmp_path, guard, replacement
):
    patch = CHECKER.PATCH.read_text()
    assert patch.count(guard) == 1
    broken = tmp_path / "sensitive-preimage.patch"
    broken.write_text(patch.replace(guard, replacement, 1))
    with pytest.raises(subprocess.CalledProcessError):
        CHECKER.check(patch_path=broken)


def test_callback_fixture_cannot_be_substituted(tmp_path, monkeypatch):
    path = tmp_path / "callbacks.c"
    path.write_bytes(CHECKER.CALLBACK_PREIMAGE.read_bytes() + b"\n")
    monkeypatch.setattr(CHECKER, "CALLBACK_PREIMAGE", path)
    with pytest.raises(ValueError, match="preimage changed"):
        CHECKER.check()


def test_old_binary_marker_rejected_before_native_proof(tmp_path):
    broken = tmp_path / "stock-marker.patch"
    broken.write_text(CHECKER.PATCH.read_text().replace("-speech1-ready1])", "-speech1])"))
    with pytest.raises(ValueError, match="reviewed-ready1"):
        CHECKER.check(patch_path=broken)
