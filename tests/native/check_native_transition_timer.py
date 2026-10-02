from pathlib import Path
import argparse
import tempfile
import shutil
import re
import subprocess
import json
import hashlib

parser = argparse.ArgumentParser()
parser.add_argument("--source", required=True, type=Path)
parser.add_argument("--compiler")
parser.add_argument("--output", type=Path)
args = parser.parse_args()
base = (
    args.output.resolve() if args.output else Path(tempfile.mkdtemp(prefix="shiri-native-transition-check-"))
)
base.mkdir(parents=True, exist_ok=True)
tree = args.source.resolve(strict=True)
fixture_root = Path(__file__).resolve().parent
VERSION = "29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-resample1-framed1-alsa1-speech1-ready1-anchor1-jitter1-owner1-balance1-transition1"
if re.findall(r"^AC_INIT\(\[owntone\], \[([^]]+)\]", (tree / "configure.ac").read_text(), re.M) != [VERSION]:
    raise SystemExit("Native transition check requires exact transition1 source marker")


def f(source, name):
    m = re.search(r"(?m)^" + name + r"\([^;{]*?\)\n\{", source)
    assert m, name
    start = source.rfind("\n", 0, m.start() - 1) + 1
    op = source.index("{", m.start())
    dep = 1
    end = op + 1
    while dep:
        dep += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end] + "\n"


pre = (fixture_root / "native_transition_prelude.c.inc").read_text()
player = (tree / "src/player.c").read_text()
out = (tree / "src/outputs.c").read_text()
cmd = (tree / "src/commands.c").read_text()
param = re.search(r"struct shiri_source_param \{.*?\n\};", player, re.S).group()
wait = player[
    player.index("#define SHIRI_SOURCE_SETUP_NS") : player.index("/* Speech preparation owns neither")
]
# actual globals and registry; no copied implementation of token admission.
registry = (
    re.search(r"struct outputs_callback_register\n\{.*?\n\};", out, re.S).group()
    + "\nstatic struct outputs_callback_register outputs_cb_register[OUTPUTS_MAX_CALLBACKS];\nstatic uint32_t outputs_callback_token;\n"
)
now = r"""static uint64_t shiri_speech_ready_now(void){struct timespec t;if(clock_gettime(CLOCK_MONOTONIC,&t)<0)return 0;return (uint64_t)t.tv_sec*UINT64_C(1000000000)+(uint64_t)t.tv_nsec;}
"""
funcnames = [
    "pb_timer_native_reject",
    "pb_timer_native_prepare",
    "pb_timer_start",
    "pb_timer_stop",
    "pb_abort",
    "device_shiri_flush_cb",
    "device_shiri_start_cb",
    "device_shiri_ready_cb",
    "device_shiri_cleanup_cb",
    "shiri_source_wait_owned",
    "shiri_source_wait_clear",
    "shiri_source_wait_fail",
    "shiri_source_wait_timeout",
    "shiri_source_wait_begin",
    "shiri_source_callback_current",
    "shiri_source_callback_complete",
    "shiri_source_seal_flush",
    "shiri_source_restart",
    "shiri_source_arm",
    "shiri_source_continue",
    "shiri_source_transition_begin",
]
outnames = [
    "callback_remove",
    "callback_add",
    "outputs_cb",
    "deferred_cb",
    "outputs_shiri_flush",
    "outputs_shiri_start",
    "outputs_shiri_ready",
    "outputs_shiri_cancel_callbacks",
    "outputs_shiri_release",
]
# Declarations needed by extracted source order.
proto = "static void shiri_source_wait_timeout(int,short,void*);\n"
pipe_disarm = f((tree / "src/inputs/pipe.c").read_text(), "input_pipe_shiri_disarm")
main = (fixture_root / "test_native_transition_timer.c").read_text()
source = (
    pre
    + "\n"
    + pipe_disarm
    + "\nstatic struct shiri_source_state shiri_source_state;\nstatic int shiri_source_flush_failed,shiri_source_start_failed;\n"
    + param
    + "\n"
    + wait
    + "\n"
    + registry
    + "\n"
    + now
    + "\n"
    + proto
    + "\n"
    + f(cmd, "commands_exec_end_if")
    + "\n"
    + "\n".join(f(out, n) for n in outnames)
    + "\n"
    + "\n".join(f(player, n) for n in funcnames)
    + "\n"
    + main
)
outputs = []
for portable in (False, True):
    c = base / ("deadline-regression-portable.c" if portable else "deadline-regression.c")
    c.write_text(source)
    binary = c.with_suffix("")
    flags = [
        args.compiler or shutil.which("cc") or "cc",
        "-std=c11",
        "-D_POSIX_C_SOURCE=200809L",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-Wno-unused-function",
        "-Wno-unused-variable",
        "-Wno-unused-parameter",
        "-Wno-sign-compare",
        "-fsanitize=address,undefined",
        "-fno-sanitize-recover=all",
        "-I",
        str(tree / "src"),
    ]
    if portable:
        flags += ["-DFIXTURE_PORTABLE_TIMER=1"]
    compile_ = subprocess.run(flags + [str(c), "-o", str(binary)], capture_output=True, text=True)
    if compile_.returncode:
        print(compile_.stderr)
        raise SystemExit(compile_.returncode)
    run = subprocess.run([str(binary)], capture_output=True, text=True)
    if run.returncode:
        print(run.stdout, run.stderr)
        raise SystemExit(run.returncode)
    print(run.stdout)
    outputs.append(
        {
            "timer_api": "timer_settime" if portable else "timerfd_settime",
            "source": str(c),
            "sha256": hashlib.sha256(c.read_bytes()).hexdigest(),
            "compile_exit": compile_.returncode,
            "run_exit": run.returncode,
            "stdout": run.stdout,
        }
    )
(base / "regression-result.json").write_text(
    json.dumps(
        {
            "passed": True,
            "actual_player_functions": funcnames,
            "actual_outputs_functions": outnames,
            "actual_command_helper": True,
            "actual_input_disarm": True,
            "address_undefined_sanitizers": True,
            "controlled_backend_clock_event_seams": True,
            "automatic_retry_proven": False,
            "physical_network_proven": False,
            "cases": outputs,
        },
        indent=2,
    )
    + "\n"
)
print(base / "regression-result.json")
