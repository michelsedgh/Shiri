#!/usr/bin/env python3
"""Exercise exact OwnTone timer, speech admission, media and output-bed C bodies."""

from pathlib import Path
import argparse
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
NATIVE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("extract", NATIVE / "check_source_transition.py")
x = importlib.util.module_from_spec(spec)
spec.loader.exec_module(x)


def body(source, name):
    found = re.search(r"(?m)^" + name + r"\([^;{}]*?\)\n\{", source)
    if not found:
        raise ValueError(name)
    start = source.rfind("\n", 0, found.start() - 1) + 1
    opening = source.index("{", found.start())
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end] + "\n"


x.function = body
oldfixture = (
    (NATIVE / "test_cold_speech_ready.c")
    .read_text()
    .split("static struct shiri_speech_request fresh(void)")[0]
)
ready = (
    "shiri_speech_ready_now",
    "shiri_speech_source_matches",
    "shiri_speech_request_matches",
    "shiri_speech_outputs_snapshot",
    "shiri_speech_reap_start_callbacks",
    "shiri_speech_schedule_idle_stop",
    "shiri_speech_bind_session",
    "shiri_speech_setup_event_clear",
    "shiri_speech_setup_timeout",
    "device_shiri_speech_start_cb",
    "shiri_speech_prepare",
    "shiri_speech_prepare_bh",
    "shiri_speech_last_outputs",
    "shiri_speech_mix_ready",
    "shiri_speech_ready_check",
)
voice = ("shiri_speech_voice_matches", "shiri_speech_voice_command", "player_shiri_speech_ready")


def assemble(source, target):
    p = (source / "src/player.c").read_text()
    o = (source / "src/outputs.c").read_text()
    hasbed = "shiri_speech_bed_tick(void)" in p
    f = oldfixture.replace(
        "static int event_del(struct event *e) {",
        "static bool selected_stop_pending;\nstatic int timer_watch;\nstatic struct event *pb_timer_ev;\nstatic int event_add(struct event *e,void *arg){(void)arg;assert(e==pb_timer_ev);timer_watch=1;return 0;}\nstatic int event_del(struct event *e) { if(e==pb_timer_ev){timer_watch=0;return 0;}",
    )
    f = f.replace(
        "static int player_state, pb_timer_native, pb_timer_native_anchored;\nstatic struct { struct { unsigned sample_rate,bits_per_sample,channels; } quality; } pb_session;",
        (NATIVE / "paused_speech_globals.c").read_text(),
    )
    state = x.between(p, "/* Speech preparation owns", "\nstruct event_base *evbase_player;")
    state += x.between(p, "static int shiri_source_flush_failed;", "\n\n/* Speech preparation owns")
    if not hasbed:
        state += "\nstatic struct shiri_speech_request shiri_voice_request;\nstatic bool shiri_voice_initialized;\n"
    functions = "\n".join(
        x.function(o, name)
        for name in (
            "callback_remove",
            "callback_add",
            "outputs_cb",
            "outputs_shiri_callback_pending",
            "outputs_shiri_stop_delayed_cancel",
        )
    )
    if "outputs_shiri_ready(output_status_cb cb)" in o:
        functions += "\n" + x.function(o, "outputs_shiri_ready") + x.function(o, "outputs_shiri_release")
    functions += "\n" + "\n".join(x.function(p, name) for name in ready)
    if hasbed:
        functions += "\n" + "\n".join(
            x.function(p, name)
            for name in (
                "shiri_speech_bed_wanted",
                "shiri_speech_bed_retire_outputs",
                "shiri_speech_bed_arm",
                "shiri_speech_bed_tick",
            )
        )
    if "shiri_source_wait_begin(param)" in p:
        functions += (
            "\nstatic int shiri_source_wait_begin(struct shiri_source_param *param){(void)param;return 0;}\n"
        )
    functions += "\n" + "\n".join(
        x.function(p, name)
        for name in (
            *voice,
            "pb_timer_native_reject",
            "pb_timer_native_prepare",
            "pb_timer_start",
            "pb_timer_stop",
            "playback_cb",
            "shiri_source_seal_flush",
            "shiri_source_arm",
        )
    )
    if hasbed:
        mark = "static void mark_mix_ready(bool bed){shiri_speech_mix_ready(bed);}\n"
    else:
        mark = "static void mark_mix_ready(bool bed){(void)bed;shiri_speech_mix_ready();}\nstatic bool shiri_speech_bed_tick(void){return false;}\n"
    f = f.replace(
        "if(e==&selected_timer)++cancels;", "if(e==&selected_timer){selected_stop_pending=false;++cancels;}"
    )
    f = f.replace(
        "assert(d==&device && cb==device_streaming_cb);++stops;callback_add(d,cb);",
        "assert((d==&device || d==&foreign) && (cb==device_streaming_cb || cb==device_shiri_cleanup_cb));if(d==&device){++stops;selected_stop_pending=true;}else ++foreign_stops;callback_add(d,cb);",
    )
    f = f.replace("/* @ACTUAL_STATE@ */", state).replace("/* @ACTUAL_FUNCTIONS@ */", functions)
    f += "\n" + mark + (NATIVE / "test_paused_speech_lifecycle.c").read_text()
    target.write_text(f)
    return hasbed



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--preimage", type=Path)
    parser.add_argument("--compiler")
    args = parser.parse_args()
    compiler = args.compiler or shutil.which("clang") or shutil.which("cc")
    if not compiler:
        raise SystemExit("A C compiler is required")
    cases = [("candidate", args.source)]
    if args.preimage:
        cases.append(("preimage", args.preimage))
    results = []
    with tempfile.TemporaryDirectory(prefix="shiri-paused-real-c-") as temp:
        for name, source in cases:
            unit = Path(temp) / (name + ".c")
            bed = assemble(source, unit)
            binary = unit.with_suffix("")
            flags = [
                compiler,
                "-std=gnu11",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-Wno-unused-parameter",
                "-Wno-unused-variable",
                "-Wno-unused-function",
                "-Wno-sign-compare",
                "-O1",
                "-fsanitize=address,undefined",
                "-fno-sanitize-recover=all",
                "-I" + str(source / "src"),
                '-DSHIRI_SPEECH_SOURCE="' + str(source / "src/shiri_speech.c") + '"',
                str(unit),
                "-lm",
                "-o",
                str(binary),
            ]
            completed = subprocess.run(flags, capture_output=True, text=True)
            if completed.returncode:
                print(completed.stderr)
                raise SystemExit(1)
            run = subprocess.run(
                [str(binary)],
                capture_output=True,
                text=True,
                env=dict(
                    os.environ, ASAN_OPTIONS="detect_leaks=0:halt_on_error=1", UBSAN_OPTIONS="halt_on_error=1"
                ),
            )
            if bed:
                if run.returncode:
                    print(run.stderr, run.stdout)
                    raise SystemExit(1)
                results.append(run.stdout.strip())
            elif run.returncode == 0 or "paused phone preparation must succeed" not in run.stderr:
                raise SystemExit("Old actual native regression did not reproduce: " + run.stdout + run.stderr)
            else:
                results.append(
                    "Exact pre-bed source preimage reproduced paused-phone setup refusal under the same lifecycle fixture"
                )
            json_flags = shlex.split(
                subprocess.run(
                    ["pkg-config", "--cflags", "--libs", "json-c"], capture_output=True, text=True, check=True
                ).stdout
            )
            json_binary = Path(temp) / (name + "-json")
            compiled = subprocess.run(
                [
                    compiler,
                    "-std=gnu11",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-O1",
                    "-fsanitize=address,undefined",
                    "-fno-sanitize-recover=all",
                    "-I" + str(source / "src"),
                    str(NATIVE / "test_paused_speech_json.c"),
                    *json_flags,
                    "-o",
                    str(json_binary),
                ],
                capture_output=True,
                text=True,
            )
            if compiled.returncode:
                print(compiled.stderr)
                raise SystemExit(1)
            parsed = subprocess.run([str(json_binary)], capture_output=True, text=True)
            if bed:
                if parsed.returncode:
                    print(parsed.stderr, parsed.stdout)
                    raise SystemExit(1)
                results.append(parsed.stdout.strip())
            elif parsed.returncode == 0 or "paused phone JSON prepare must be accepted" not in parsed.stderr:
                raise SystemExit("Actual old JSON refusal not reproduced")
            else:
                results.append("Exact pre-bed JSON parser reproduced actual paused-source preparation refusal")

    print(json.dumps({"ok": True, "sanitized": True, "results": results}, indent=2))


if __name__ == "__main__":
    main()
