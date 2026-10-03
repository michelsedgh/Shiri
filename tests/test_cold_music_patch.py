"""Require the framed empty-input fix without changing historical source pins."""
import hashlib
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def checker():
    spec = importlib.util.spec_from_file_location("coldmusic1_pin", ROOT / "tests/native/check_cold_music.py")
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_cold_music_builder_and_runtime_contract_are_coherent():
    from shiri.runtime.broker import REQUIRED_OWNTONE_VERSION, _OWNTONE_VERSION_PATTERN
    value = checker()
    assert hashlib.sha256(value.PATCH.read_bytes()).hexdigest() == value.PATCH_SHA
    assert REQUIRED_OWNTONE_VERSION == value.VERSION + "-outputclock1"
    assert _OWNTONE_VERSION_PATTERN.search("OwnTone " + REQUIRED_OWNTONE_VERSION)
    builder = (ROOT / "install/build_backends.sh").read_text()
    assert "OWNTONE_COLD_MUSIC_SHA=" + value.PATCH_SHA in builder
    assert '"$OWNTONE_COLD_MUSIC_PATCH:$OWNTONE_COLD_MUSIC_SHA"' in builder
    assert 'manifest["owntone_cold_music_patch"] = sys.argv[35]' in builder
    before = builder.index("check_startup_metadata.py")
    apply = builder.index('apply "$OWNTONE_COLD_MUSIC_PATCH"')
    check = builder.index("check_cold_music.py")
    assert before < apply < check < builder.index("make -j", check)
    assert {line[6:] for line in value.PATCH.read_text().splitlines() if line.startswith("+++ b/")} == {
        "configure.ac", "src/player.c", "src/outputs/airplay.c"
    }
    patch = value.PATCH.read_text()
    assert "pb_session.read_deficit = 0;" in patch
    assert "operation == pb_timer_native_operation" in patch
    assert "flag == 0" in patch
    assert "pb_timer_native_anchored = false;" not in patch
    assert "INPUT_BUFFER_NATIVE_THRESHOLD" not in patch


def test_forged_cold_music_marker_cannot_replace_historical_source(tmp_path):
    value = checker()
    (tmp_path / "src").mkdir()
    (tmp_path / "src/player.c").write_text("/* unreviewed player */\n")
    (tmp_path / "configure.ac").write_text("AC_INIT([owntone], [" + value.VERSION + "])\n")
    with pytest.raises(ValueError, match="Unreviewed cold music player source"):
        value.verify_source(tmp_path)


def test_codec_guard_requires_the_new_layer_and_unchanged_encoding_sources():
    spec = importlib.util.spec_from_file_location("coldmusic1_codec", ROOT / "tests/native/check_speech_packetizer.py")
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    assert value.CODEC_SOURCE_SHA == {
        "src/outputs.c": "08df3c6e3e451bc9e5190dd367e6c58402945a5827495ffd9dfbe5e596a1cb9d",
        "src/transcode.c": "68441cfdab2e0862349d499cafa8fdf419d93562534e90c0955e92ed31398d13",
        "src/outputs/airplay.c": "405243e11ea3f24301f449346b359d81134ae20ff75c5389c491e776f1dcc046",
    }
    assert "check_cold_music.py" in Path(value.__file__).read_text()
