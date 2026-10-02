"""Require the complete event layer and retain exact historical build guards."""
import hashlib
import importlib.util
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def checker():
    path = ROOT / "tests/native/check_airplay_events.py"
    spec = importlib.util.spec_from_file_location("event1_pin", path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_event_patch_builder_and_required_contract_are_coherent():
    from shiri.runtime.broker import REQUIRED_OWNTONE_VERSION, _OWNTONE_VERSION_PATTERN
    value = checker()
    assert hashlib.sha256(value.PATCH.read_bytes()).hexdigest() == value.PATCH_SHA
    assert REQUIRED_OWNTONE_VERSION == value.VERSION
    assert _OWNTONE_VERSION_PATTERN.search("OwnTone " + value.VERSION)
    for stale in (value.VERSION.removesuffix("-event1"), value.VERSION + "0", value.VERSION + "-other"):
        assert not _OWNTONE_VERSION_PATTERN.search("OwnTone " + stale)
    builder = (ROOT / "install/build_backends.sh").read_text()
    assert "OWNTONE_EVENT_SHA=" + value.PATCH_SHA in builder
    assert '"$OWNTONE_EVENT_PATCH:$OWNTONE_EVENT_SHA"' in builder
    assert 'manifest["owntone_event_ack_patch"] = sys.argv[31]' in builder
    assert 'manifest["shairport_bounded_events_patch"] = sys.argv[30]' in builder
    assert 'manifest["owntone_paused_speech_patch"] = sys.argv[29]' in builder
    assert 'manifest["shairport_receiver_volume_patch"] = sys.argv[28]' in builder
    assert builder.index("check_paused_speech.py") < builder.index('apply "$OWNTONE_EVENT_PATCH"')
    assert builder.index('apply "$OWNTONE_EVENT_PATCH"') < builder.index("check_airplay_events.py")
    assert 'check_airplay_events.py" --source "$BUILD/owntone" --compiler /usr/bin/cc --sanitize' in builder
    subprocess.run(["bash", "-n", ROOT / "install/build_backends.sh"], check=True)


def test_event_patch_cannot_modify_music_or_backend_transport():
    value = checker()
    text = value.PATCH.read_text()
    assert {line[6:] for line in text.splitlines() if line.startswith("+++ b/")} == {
        "configure.ac", "src/outputs/airplay_events.c"
    }
    assert hashlib.sha256((ROOT / "tests/native/airplay_updateInfo128.rtsp").read_bytes()).hexdigest() == value.FIXTURE_SHA
    assert value.PREIMAGE_EVENT_SHA == "38d3dce1d5a7b445c81c4453f063e4e3893e5723b864936ee84ac0ab73a58944"
    assert value.PLAYER_SHA == "8d85e2010e6b0402ebddbdb61d965663e8cc333bf5accf587edcb4d9b1585613"


def test_forged_event_marker_cannot_admit_unreviewed_source(tmp_path):
    value = checker()
    (tmp_path / "configure.ac").write_text("AC_INIT([owntone], [" + value.VERSION + "])\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/player.c").write_text("/* forged prior player */\n")
    with pytest.raises(ValueError, match="Prior complete player/gain/timing source changed"):
        value.verify_source(tmp_path)
