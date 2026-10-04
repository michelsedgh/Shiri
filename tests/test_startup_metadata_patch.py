"""Require the complete acknowledged initial metadata startup layer."""
import hashlib
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def checker():
    spec = importlib.util.spec_from_file_location("startupmeta1_pin", ROOT / "tests/native/check_startup_metadata.py")
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_startup_metadata_builder_and_runtime_contract_are_coherent():
    from shiri.runtime.broker import REQUIRED_OWNTONE_VERSION, _OWNTONE_VERSION_PATTERN
    value = checker()
    assert hashlib.sha256(value.PATCH.read_bytes()).hexdigest() == value.PATCH_SHA
    assert REQUIRED_OWNTONE_VERSION == value.VERSION + "-coldmusic1-outputclock1-duck1-warm1"
    assert _OWNTONE_VERSION_PATTERN.search("OwnTone " + REQUIRED_OWNTONE_VERSION)
    assert not _OWNTONE_VERSION_PATTERN.search("OwnTone " + value.VERSION)
    builder = (ROOT / "install/build_backends.sh").read_text()
    assert "OWNTONE_STARTUP_METADATA_SHA=" + value.PATCH_SHA in builder
    assert '"$OWNTONE_STARTUP_METADATA_PATCH:$OWNTONE_STARTUP_METADATA_SHA"' in builder
    assert 'manifest["owntone_startup_metadata_patch"] = sys.argv[34]' in builder
    assert builder.index("check_speech_drain.py") < builder.index('apply "$OWNTONE_STARTUP_METADATA_PATCH"')
    check = builder.index("check_startup_metadata.py")
    assert builder.index('apply "$OWNTONE_STARTUP_METADATA_PATCH"') < check < builder.index("make -j", check)
    assert {line[6:] for line in value.PATCH.read_text().splitlines() if line.startswith("+++ b/")} == {
        "configure.ac", "src/outputs/airplay.c"
    }


def test_forged_metadata_marker_cannot_change_prior_source(tmp_path):
    value = checker()
    (tmp_path / "src/outputs").mkdir(parents=True)
    (tmp_path / "src/outputs/airplay.c").write_text("/* unreviewed output transport */\n")
    (tmp_path / "configure.ac").write_text("AC_INIT([owntone], [" + value.VERSION + "])\n")
    with pytest.raises(ValueError, match="Unreviewed startup metadata AirPlay source"):
        value.verify_source(tmp_path)
