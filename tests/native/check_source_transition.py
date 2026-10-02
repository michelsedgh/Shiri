#!/usr/bin/python3
"""Compile actual source-transition/callback seams with controlled backend ACKs."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

REPOSITORY = Path(__file__).resolve().parents[2]
SOURCE_PATCH = REPOSITORY / "install/patches/owntone-29.3-source-transition.patch"
TIMING_PATCH = REPOSITORY / "install/patches/owntone-29.3-timed-pcm.patch"


def patch_image(patch: Path, name: str, *, after=True):
    text = patch.read_text()
    block = text.split(f"diff --git a/{name} b/{name}\n", 1)[1].split("\ndiff --git ", 1)[0]
    lines = []
    in_hunk = False
    for line in block.splitlines():
        if line.startswith("@@"):
            in_hunk = True
            lines.append("")
        elif in_hunk and line.startswith(("+" if after else "-", " ")):
            lines.append(line[1:])
    return "\n".join(lines) + "\n"


def postimage(patch: Path, name: str):
    return patch_image(patch, name)


def function(source: str, name: str):
    found = re.search(rf"(?m)^{name}\([^\n]*\)\n\{{", source)
    if found is None:
        raise ValueError(f"Missing actual native source function: {name}")
    start = source.rfind("\n", 0, found.start() - 1) + 1
    opening = source.index("{", found.start())
    depth, end = 1, opening + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end] + "\n"


def between(source, begin, end):
    start = source.index(begin)
    return source[start:source.index(end, start)]


def run_tests(*, source: Path | None = None, compiler=None, sanitize=True, require_json=False):
    compiler = compiler or shutil.which("clang") or shutil.which("cc")
    if not compiler:
        raise RuntimeError("A C compiler is required for native source-transition checks")
    def native(name, patch=SOURCE_PATCH):
        return (source / name).read_text() if source else postimage(patch, name)
    player = native("src/player.c")
    outputs = native("src/outputs.c")
    airplay = native("src/outputs/airplay.c")
    # Keep the exact source command globals. New unrelated ready1 state lies
    # between this existing block and evbase_player and has its own real tests.
    prefix_end = "\n/* Speech preparation owns" if "\n/* Speech preparation owns" in player else "\nstruct event_base *evbase_player;"
    prefix = between(player, "static struct shiri_source_state", prefix_end)
    player_parts = [prefix, function(outputs, "outputs_shiri_flush"), function(outputs, "outputs_shiri_start")]
    if "outputs_resampling_reset();" in function(player, "shiri_source_seal_flush"):
        player_parts += ["static bool outputs_got_new_subscription;\n",
                         function(outputs, "outputs_resampling_reset")]
    player_parts += [function(player, name) for name in (
        "device_shiri_flush_cb", "device_shiri_start_cb", "shiri_source_seal_flush",
        "shiri_source_restart", "shiri_source_arm", "player_shiri_source_transition",
    )]
    player_parts += [between(player, "struct shiri_source_volume_param {", "\nstatic enum command_state\nvolume_setrel_speaker("),
                     function(player, "player_shiri_source_volume")]
    callback_parts = [between(outputs, "struct outputs_callback_register\n", "\nstruct output_quality_subscription"),
                      "static struct outputs_callback_register outputs_cb_register[OUTPUTS_MAX_CALLBACKS];\n",
                      "static uint32_t outputs_callback_token;\n",
                      function(outputs, "callback_remove"), function(outputs, "callback_add"), function(outputs, "outputs_cb")]
    flags = ["-std=c11", "-D_POSIX_C_SOURCE=200809L", "-Wall", "-Wextra", "-Werror", "-Wno-sign-compare", "-O1"]
    if sanitize:
        flags += ["-fsanitize=address,undefined", "-fno-sanitize-recover=all"]
    environment = dict(os.environ, ASAN_OPTIONS="detect_leaks=0:halt_on_error=1", UBSAN_OPTIONS="halt_on_error=1")
    json_flags = None
    pkg_config = shutil.which("pkg-config")
    if pkg_config:
        found = subprocess.run([pkg_config, "--cflags", "--libs", "json-c"], capture_output=True, text=True, timeout=10)
        if found.returncode == 0:
            import shlex
            json_flags = shlex.split(found.stdout)
    if require_json and json_flags is None:
        raise RuntimeError("Actual JSON parser checks require pkg-config and libjson-c development files")
    results = []
    with tempfile.TemporaryDirectory(prefix="shiri-source-native-") as temporary:
        directory = Path(temporary)
        (directory / "inputs").mkdir()
        (directory / "inputs/shiri_pcm.h").write_text(native("src/inputs/shiri_pcm.h", TIMING_PATCH))
        for name in ("shiri_source.h", "shiri_source_json.h"):
            (directory / name).write_text(native("src/" + name))
        cases = [("test_owntone_source_transition", "/* @ACTUAL_SOURCE_TRANSITION@ */", player_parts),
                 ("test_owntone_callbacks", "/* @ACTUAL_CALLBACK_SOURCE@ */", callback_parts),
                 ("test_owntone_source_gate", None, None),
                 ("test_owntone_airplay_ack", "/* @ACTUAL_AIRPLAY_SEQUENCE_CALLBACKS@ */",
                  [function(airplay, "sequence_continue_cb"), function(airplay, "sequence_start")]),
                 ("test_owntone_partial_reads", "/* @ACTUAL_PLAYBACK_TICK@ */", [function(player, "playback_cb")])]
        if json_flags is not None:
            cases.append(("test_owntone_source_json", None, None))
        for name, marker, parts in cases:
            fixture = (REPOSITORY / "tests/native" / (name + ".c")).read_text()
            if name.endswith("airplay_ack"):
                fixture = fixture.replace("/* @ACTUAL_AIRPLAY_SEQUENCE_CONTEXT@ */",
                                          between(airplay, "struct airplay_seq_ctx\n", "\n\n\n/*"))
            unit = directory / (name + ".c")
            unit.write_text(fixture.replace(marker, "\n".join(parts)) if marker else fixture)
            target = directory / name
            extras = json_flags if name.endswith("json") else []
            if name.endswith(("partial_reads", "airplay_ack")):
                extras = [*extras, "-Wno-unused-parameter"]
            if name.endswith("partial_reads"):
                late_calls = [len(re.findall(r"\b"+hook+r"\s*\(", parts[0]))
                              for hook in ("shiri_speech_poll", "shiri_speech_mix")]
                if late_calls != [0, 0]:
                    if late_calls != [1, 1]:
                        raise ValueError("Actual partial-read callback must retain exactly one late-speech poll/mix seam")
                    extras = [*extras, "-DSHIRI_PARTIAL_SPEECH=1"]
                ready_calls = len(re.findall(r"\bshiri_speech_mix_ready\s*\(", parts[0]))
                if ready_calls:
                    if ready_calls != 1 or late_calls != [1, 1]:
                        raise ValueError("Actual ready hook must occur once after the partial-read output seam")
                    extras = [*extras, "-DSHIRI_PARTIAL_READY=1"]
            subprocess.run([compiler, *flags, "-I", str(directory), str(unit), *extras, "-lpthread", "-o", str(target)],
                           check=True, capture_output=True, text=True, timeout=30)
            completed = subprocess.run([str(target)], env=environment, check=True, capture_output=True, text=True, timeout=30)
            results.append(completed.stdout.strip())
            if name.endswith("airplay_ack"):
                # Compile the real removed upstream body from this diff too.
                # A fence regression must detect the original false ACK, not
                # merely pass against a fixture mirroring the new condition.
                before = patch_image(SOURCE_PATCH, "src/outputs/airplay.c", after=False)
                removed = (REPOSITORY / "tests/native" / (name + ".c")).read_text()
                removed = removed.replace("/* @ACTUAL_AIRPLAY_SEQUENCE_CONTEXT@ */",
                                          between(before, "struct airplay_seq_ctx\n", "\n\n\n/*"))
                removed = removed.replace(marker, function(before, "sequence_continue_cb") + function(before, "sequence_start"))
                unit.write_text(removed)
                subprocess.run([compiler, *flags, "-I", str(directory), str(unit), *extras, "-lpthread", "-o", str(target)],
                               check=True, capture_output=True, text=True, timeout=30)
                reproduced = subprocess.run([str(target)], env=environment, capture_output=True, text=True, timeout=30,
                                            cwd=directory)
                if reproduced.returncode == 0 or "!status_calls && !flush_effects" not in reproduced.stderr + reproduced.stdout:
                    raise RuntimeError("The actual upstream stale AirPlay ACK regression was not reproduced")
            if name.endswith("partial_reads"):
                before = patch_image(SOURCE_PATCH, "src/player.c", after=False)
                removed = (REPOSITORY / "tests/native" / (name + ".c")).read_text().replace(marker, function(before, "playback_cb"))
                unit.write_text(removed)
                # The retained upstream preimage predates the overlay. Its
                # exact callback must still reproduce the continuity defect.
                previous_extras = [flag for flag in extras if flag not in ("-DSHIRI_PARTIAL_SPEECH=1", "-DSHIRI_PARTIAL_READY=1")]
                subprocess.run([compiler, *flags, "-I", str(directory), str(unit), *previous_extras, "-lpthread", "-o", str(target)],
                               check=True, capture_output=True, text=True, timeout=30)
                reproduced = subprocess.run([str(target)], env=environment, capture_output=True, text=True, timeout=30,
                                            cwd=directory)
                if reproduced.returncode != 1 or "permanently falls behind the player clock" not in reproduced.stderr:
                    raise RuntimeError("The actual upstream partial-read timing defect was not reproduced")
    return {"ok": True, "sanitized": sanitize, "source": "patched checkout" if source else "actual patch postimages",
            "json_parser_tested": json_flags is not None, "upstream_stale_ack_reproduced": True,
            "upstream_partial_read_deficit_reproduced": True, "results": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--compiler")
    parser.add_argument("--require-json", action="store_true")
    parser.add_argument("--no-sanitizers", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(run_tests(source=args.source, compiler=args.compiler,
                                  sanitize=not args.no_sanitizers, require_json=args.require_json), indent=2))
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"Native source-transition check failed: {exc.stderr or exc.stdout}") from None


if __name__ == "__main__":
    main()
