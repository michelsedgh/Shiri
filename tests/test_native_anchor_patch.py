"""Exercise the maintained first-anchor repair on actual player functions."""
from pathlib import Path
import importlib.util
import shutil

import pytest

ROOT = Path(__file__).resolve().parents[1]


def checker():
    spec = importlib.util.spec_from_file_location("native_anchor_check", ROOT / "tests/native/check_native_anchor.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_actual_anchor_preimage_and_both_timer_deadline_matrices():
    if not shutil.which("clang") and not shutil.which("cc"):
        pytest.skip("A C compiler is required for actual player admission fixtures")
    result = checker().run_tests()
    assert result["ok"] and result["sanitized"]
    assert len(result["results"]) == 4
    assert [r["timerfd"] for r in result["results"]] == [False,False,True,True]
    assert [r["preimage"] for r in result["results"]] == [True,False,True,False]
    assert all("PREIMAGE reproduced" in r["output"] for r in result["results"] if r["preimage"])
    assert all("actual native anchor cases" in r["output"] for r in result["results"] if not r["preimage"])


@pytest.mark.parametrize("version", [
    "29.3", "29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-resample1-framed1-alsa1-speech1-ready1",
    "29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-resample1-framed1-alsa1-speech1-ready1-anchor10",
    "29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-resample1-framed1-alsa1-speech1-ready1-anchor1-other",
])
def test_composed_marker_must_advertise_exact_anchor_layer(tmp_path, version):
    module = checker()
    (tmp_path / "configure.ac").write_text(f"AC_INIT([owntone], [{version}], [x])\n")
    with pytest.raises(ValueError, match="exact reviewed anchor1 marker"):
        module.validate_layer(tmp_path)


def test_preimage_cannot_be_substituted_to_weaken_regression(tmp_path):
    module = checker()
    original = module.PREIMAGE.read_bytes()
    changed = tmp_path / "preimage.inc"
    changed.write_bytes(original + b"\n")
    module.PREIMAGE = changed
    with pytest.raises(ValueError, match="preimage changed"):
        module.validate_layer()
