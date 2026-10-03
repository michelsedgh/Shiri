"""The installed marker must include the verified natural EOF drain layer."""
import hashlib
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def checker():
    spec = importlib.util.spec_from_file_location("drain1_pin", ROOT / "tests/native/check_speech_drain.py")
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_drain_layer_build_and_runtime_contract_are_coherent():
    from shiri.runtime.broker import REQUIRED_OWNTONE_VERSION, _OWNTONE_VERSION_PATTERN
    value = checker()
    assert hashlib.sha256(value.PATCH.read_bytes()).hexdigest() == value.PATCH_SHA
    assert REQUIRED_OWNTONE_VERSION == value.VERSION + "-startupmeta1-coldmusic1-outputclock1"
    assert _OWNTONE_VERSION_PATTERN.search("OwnTone " + REQUIRED_OWNTONE_VERSION)
    for stale in (value.VERSION.removesuffix("-drain1"), value.VERSION + "0", value.VERSION + "-other"):
        assert not _OWNTONE_VERSION_PATTERN.search("OwnTone " + stale)
    builder = (ROOT / "install/build_backends.sh").read_text()
    assert "OWNTONE_DRAIN_SHA=" + value.PATCH_SHA in builder
    assert '"$OWNTONE_DRAIN_PATCH:$OWNTONE_DRAIN_SHA"' in builder
    assert 'manifest["owntone_speech_drain_patch"] = sys.argv[33]' in builder
    assert builder.index("check_idle_speech.py") < builder.index('apply "$OWNTONE_DRAIN_PATCH"')
    assert builder.index('apply "$OWNTONE_DRAIN_PATCH"') < builder.index("check_speech_drain.py")
    own_build = builder.index("make -j", builder.index("check_startup_metadata.py"))
    own_install = builder.index('(cd "$BUILD/owntone" && make install)')
    assert own_build < builder.index("check_speech_packetizer.py") < own_install


def test_forged_drain_marker_does_not_replace_historical_source(tmp_path):
    value = checker()
    (tmp_path / "src").mkdir()
    (tmp_path / "src/player.c").write_text("/* unrelated timing/source implementation */\n")
    (tmp_path / "configure.ac").write_text("AC_INIT([owntone], [" + value.VERSION + "])\n")
    with pytest.raises(ValueError, match="Unreviewed speech drain player source"):
        value.verify_source(tmp_path)
