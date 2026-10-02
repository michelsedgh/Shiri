"""The new receiver requires bounded metadata; original28 remains pinned."""

import hashlib
import importlib.util
from pathlib import Path
import re
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module():
    spec = importlib.util.spec_from_file_location(
        "receiver_events_pin", ROOT / "tests/native/check_receiver_events.py"
    )
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_builder_pins_both_historical_and_new_layers():
    current = module()
    assert (
        hashlib.sha256(
            (ROOT / "install/patches/shairport-5.5.2-bounded-events.patch").read_bytes()
        ).hexdigest()
        == current.PATCH_SHA
    )
    assert current.original().PATCH_SHA == "26003aa1b8df99c5de6eecc21074bf259158e64baefd41a35c704145d5957bbd"
    builder = (ROOT / "install/build_backends.sh").read_text()
    assert "SHAIRPORT_EVENTS_SHA=" + current.PATCH_SHA in builder
    assert 'manifest["shairport_bounded_events_patch"] = sys.argv[30]' in builder
    assert 'manifest["shairport_receiver_volume_patch"] = sys.argv[28]' in builder
    assert builder.index('apply "$SHAIRPORT_VOLUME_PATCH"') < builder.index('apply "$SHAIRPORT_EVENTS_PATCH"')
    assert builder.index("check_receiver_volume.py") < builder.index('apply "$SHAIRPORT_EVENTS_PATCH"')
    assert builder.index("check_receiver_events.py") < builder.index("check_shairport_startup.py")
    subprocess.run(["bash", "-n", ROOT / "install/build_backends.sh"], check=True)


def test_preflight_cannot_accept_previous_receiver_or_prefix_collision():
    text = (ROOT / "shiri/runtime/broker.py").read_text()
    pattern = re.compile(re.search(r'_SHAIRPORT_TIMED_PATTERN = re.compile\(\s*r"([^"]+)"', text).group(1))
    assert pattern.search("5.5.2-AirPlay2-smi10-shiri-timed3-startup1-volume2-soxr-metadata")
    assert not pattern.search("5.5.2-AirPlay2-smi10-shiri-timed3-startup1-volume1-soxr-metadata")
    assert not pattern.search("5.5.2-AirPlay2-smi10-shiri-timed3-startup1-volume20-soxr-metadata")


def test_forged_new_header_cannot_relax_original28_source_guard(tmp_path):
    (tmp_path / "shiri_event_control.h").write_text("/* fake new feature */\n")
    with pytest.raises((FileNotFoundError, ValueError)):
        module().preimage_audio(tmp_path)


def test_compiler_flags_preserve_linux_socket_enum_and_strict_warnings(tmp_path):
    current = module()
    for platform in ["linux", "darwin"]:
        command = current.compiler_command("cc", tmp_path, sanitize=True, platform=platform)
        assert ("-DMSG_NOSIGNAL=0" in command) is (platform == "darwin")
        assert {"-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined"}.issubset(command)
        assert "-Wno-misleading-indentation" not in command


def test_every_retained_lifecycle_case_executes_in_current_record_fixture():
    fixtures = ROOT / "tests/native"
    historical = (fixtures / "test_receiver_volume.c").read_text().split("int main(void) {", 1)[1]
    current = (fixtures / "test_receiver_volume_records.c").read_text().split("int main(void) {", 1)[1]
    current = current.replace("receiver-volume-records: pinned-format", "receiver-volume:")
    assert current == historical
    events = (fixtures / "test_receiver_events.c").read_text()
    assert "#define main receiver_volume_original_main" in events
    assert '#include "test_receiver_volume_records.c"' in events
    assert "  receiver_volume_original_main();" in events


def test_builder_keeps_historical_source_guard_and_runs_current_full_checker():
    builder = (ROOT / "install/build_backends.sh").read_text()
    assert "module.verify_source(Path(sys.argv[2]))" in builder
    assert (
        '/usr/bin/python3 -I "$SOURCE/tests/native/check_receiver_events.py" --source "$BUILD/shairport" --compiler /usr/bin/cc --sanitize'
        in builder
    )
