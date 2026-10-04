"""Keep stable AirPlay output timing config tied to the qualified native layer."""
import hashlib
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def checker():
    spec = importlib.util.spec_from_file_location("outputclock1_pin", ROOT / "tests/native/check_output_clock.py")
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_output_clock_builder_and_runtime_contract_are_coherent():
    from shiri.runtime.broker import REQUIRED_OWNTONE_VERSION, _OWNTONE_VERSION_PATTERN
    value = checker()
    assert hashlib.sha256(value.PATCH.read_bytes()).hexdigest() == value.PATCH_SHA
    assert REQUIRED_OWNTONE_VERSION == value.VERSION + "-duck1-warm1"
    assert _OWNTONE_VERSION_PATTERN.search("OwnTone " + REQUIRED_OWNTONE_VERSION)
    assert not _OWNTONE_VERSION_PATTERN.search("OwnTone " + value.VERSION)
    builder = (ROOT / "install/build_backends.sh").read_text()
    assert "OWNTONE_OUTPUT_CLOCK_SHA=" + value.PATCH_SHA in builder
    assert '"$OWNTONE_OUTPUT_CLOCK_PATCH:$OWNTONE_OUTPUT_CLOCK_SHA"' in builder
    assert 'manifest["owntone_output_clock_patch"] = sys.argv[36]' in builder
    assert builder.index("check_cold_music.py") < builder.index('apply "$OWNTONE_OUTPUT_CLOCK_PATCH"')
    assert builder.index('apply "$OWNTONE_OUTPUT_CLOCK_PATCH"') < builder.index("check_output_clock.py")
    assert '--compiler /usr/bin/cc --real-confuse' in builder


def test_output_clock_guard_rejects_a_forged_version_marker(tmp_path):
    value = checker()
    (tmp_path / "configure.ac").write_text("AC_INIT([owntone], [" + value.VERSION + "])\n")
    with pytest.raises(ValueError, match="Unreviewed output timing source: configure.ac"):
        value.verify_source(tmp_path)


def test_output_clock_uses_stable_ids_and_keeps_automatic_discovery():
    value = checker()
    patch = value.PATCH.read_text()
    assert 'CFG_SEC("shiri_airplay_timing", sec_shiri_airplay_timing,' in patch
    assert 'CFGF_MULTI | CFGF_TITLE | CFGF_NO_TITLE_DUPES' in patch
    assert 'snprintf(selector, sizeof(selector), "%" PRIu64, id);' in patch
    assert 'cfg_gettsec(cfg, "shiri_airplay_timing", selector)' in patch
    assert 'supports_ptp && !airplay_ptp_is_disabled' in patch
    assert 'device->type == OUTPUT_TYPE_AIRPLAY' in patch
    assert '"airplay_timing", json_object_new_string(spk->airplay_timing)' in patch
