#!/usr/bin/python3
"""Actual OwnTone first-anchor admission: mutex-stall preimage and fresh clock."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

REPOSITORY = Path(__file__).resolve().parents[2]
PATCH = REPOSITORY / "install/patches/owntone-29.3-native-anchor.patch"
PREIMAGE = REPOSITORY / "tests/native/native_anchor_preimage.inc"
PREIMAGE_SHA = "f799b641eb579dad7e4cd94bca64131f8ccb6d43349969b7dd080a4bb52c4b95"
VERSION = "29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-resample1-framed1-alsa1-speech1-ready1-anchor1"
BEFORE = """  ret = input_peek_sync(&anchor);
  if (ret < 0 || timespec_cmp(now, pb_timer_native_wait_until) >= 0)
"""
AFTER = """  ret = input_peek_sync(&anchor);
  /* The input mutex can wait across the native deadline. Admit the original
   * anchor against a fresh clock after peek, including a missing marker. */
  if (ret < 0 || clock_gettime(CLOCK_MONOTONIC, &now) < 0
      || timespec_cmp(now, pb_timer_native_wait_until) >= 0)
"""


def _extractor():
    spec = importlib.util.spec_from_file_location("shiri_anchor_extraction", REPOSITORY / "tests/native/check_timing_patch.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_layer(source: Path | None = None):
    extraction = _extractor()
    preimage = PREIMAGE.read_text()
    if hashlib.sha256(PREIMAGE.read_bytes()).hexdigest() != PREIMAGE_SHA:
        raise ValueError("Native anchor preimage changed from the reviewed ready1 function")
    if preimage.count(BEFORE) != 1:
        raise ValueError("Native anchor preimage no longer has its single stale-clock seam")
    patched = extraction.function(extraction.postimage(PATCH, "src/player.c"), "pb_timer_native_prepare")
    if patched != preimage.replace(BEFORE, AFTER):
        raise ValueError("Anchor layer must contain only the reviewed fresh post-peek clock repair")
    configure = (source / "configure.ac").read_text() if source else extraction.postimage(PATCH, "configure.ac")
    versions = re.findall(r"^AC_INIT\(\[owntone\], \[([^]]+)\]", configure, re.MULTILINE)
    if versions not in ([VERSION], [VERSION + "-jitter1"]):
        raise ValueError("First-anchor deadline repair requires the exact reviewed anchor1 marker")
    player = (source / "src/player.c").read_text() if source else extraction.postimage(extraction.OWN_PATCH, "src/player.c")
    if source and extraction.function(player, "pb_timer_native_prepare") != patched:
        raise ValueError("Actual composed player does not contain the exact reviewed anchor repair")
    bodies = {}
    for name in ("pb_timer_start", "pb_timer_stop", "source_read", "playback_cb"):
        bodies[name] = extraction.function(player, name)
    return patched, preimage, bodies


def instrumented_scaffold(bodies: str) -> tuple[str, list[str]]:
    scaffold = (REPOSITORY / "tests/native/test_native_timer.c").read_text()
    old_clock = "static int fixture_clock(clockid_t id,struct timespec*t){assert(id==CLOCK_MONOTONIC);*t=now;return 0;}"
    new_clock = """static bool peek_advances;
static struct timespec peek_returns_at;
static unsigned fixture_clock_reads, clock_failure_read, peek_calls;
static unsigned absolute_timer_successes, timer_failure_call;
static int fixture_clock(clockid_t id,struct timespec*t){
  assert(id==CLOCK_MONOTONIC);fixture_clock_reads++;
  if(fixture_clock_reads==clock_failure_read)return -1;
  *t=now;return 0;
}"""
    old_timer = "  (void)t;(void)o;timer_calls++;flags_seen=flags;timer_seen=*v;return 0;"
    new_timer = """  (void)t;(void)o;timer_calls++;flags_seen=flags;timer_seen=*v;
  if((unsigned)timer_calls==timer_failure_call)return -1;
  if(flags==TIMER_ABSTIME)absolute_timer_successes++;
  return 0;"""
    old_peek = "static int input_peek_sync(struct timespec*t){*t=next_anchor;return peek_status;}"
    new_peek = """static int input_peek_sync(struct timespec*t){
  peek_calls++;*t=next_anchor;
  if(peek_advances)now=peek_returns_at;
  return peek_status;
}"""
    old_reset = "  control_step=reads=writes=aborts=timer_calls=0;now=(struct timespec){5,0};"
    new_reset = old_reset + """
  peek_advances=false;fixture_clock_reads=clock_failure_read=peek_calls=0;
  absolute_timer_successes=timer_failure_call=0;"""
    for before, after in ((old_clock,new_clock), (old_timer,new_timer), (old_peek,new_peek), (old_reset,new_reset)):
        if scaffold.count(before) != 1:
            raise ValueError("Native timer test boundary changed; refuse an incomplete instrumentation")
        scaffold = scaffold.replace(before, after)
    if scaffold.count("/* @ACTUAL_NATIVE_TIMER@ */") != 1:
        raise ValueError("Native timer extraction boundary changed")
    late_calls = [len(re.findall(r"\b" + name + r"\s*\(", bodies)) for name in ("shiri_speech_poll", "shiri_speech_mix")]
    extras = ["-Wno-unused-parameter", "-Wno-sign-compare"]
    if late_calls != [0,0]:
        if late_calls != [1,1]:
            raise ValueError("Actual player must retain exactly one late-speech poll/mix hook")
        extras.append("-DSHIRI_TIMER_SPEECH=1")
    ready_calls = len(re.findall(r"\bshiri_speech_mix_ready\s*\(", bodies))
    if ready_calls:
        if ready_calls != 1 or late_calls != [1,1]:
            raise ValueError("Actual ready hook must occur once after the late-speech output seam")
        extras.append("-DSHIRI_TIMER_READY=1")
    return scaffold.replace("/* @ACTUAL_NATIVE_TIMER@ */", bodies), extras


def run_tests(*, source=None, compiler=None, sanitize=True):
    compiler = compiler or shutil.which("clang") or shutil.which("cc")
    if not compiler:
        raise RuntimeError("A C compiler is required for native anchor admission tests")
    patched, preimage, bodies = validate_layer(source)
    flags = ["-std=c99", "-D_POSIX_C_SOURCE=200809L", "-O1", "-Wall", "-Wextra", "-Werror"]
    if sys.platform == "darwin":
        flags.append("-D_DARWIN_C_SOURCE=1")
    if sanitize:
        flags += ["-fsanitize=address,undefined", "-fno-sanitize-recover=all"]
    environment = {**os.environ, "ASAN_OPTIONS":"detect_leaks=0:halt_on_error=1", "UBSAN_OPTIONS":"halt_on_error=1"}
    results = []
    with tempfile.TemporaryDirectory(prefix="shiri-anchor-c-") as temporary:
        directory = Path(temporary)
        for timerfd in (False, True):
            for prior in (True, False):
                all_bodies = "\n".join([preimage if prior else patched, *bodies.values()])
                scaffold, extras = instrumented_scaffold(all_bodies)
                (directory / "actual_native_timer.c").write_text(scaffold)
                target = directory / "native_anchor"
                command = [compiler, *flags, *extras]
                if timerfd:
                    command.append("-DHAVE_TIMERFD=1")
                command += ["-I", str(directory), str(REPOSITORY / "tests/native/test_native_anchor.c"), "-lm", "-pthread", "-o", str(target)]
                subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
                executed = subprocess.run([str(target), *(["preimage"] if prior else [])], env=environment, check=True, capture_output=True, text=True, timeout=30)
                results.append({"timerfd":timerfd, "preimage":prior, "output":executed.stdout.strip()})
    return {"ok":True, "sanitized":sanitize, "results":results,
            "source":str(source) if source else "maintained patch postimage",
            "patch_sha256":hashlib.sha256(PATCH.read_bytes()).hexdigest(),
            "preimage_sha256":PREIMAGE_SHA,
            "scope":"Actual five player functions; controlled mutex-return/clock boundary, no measured scheduler or device latency claim"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--compiler")
    parser.add_argument("--no-sanitizers", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(run_tests(source=args.source, compiler=args.compiler, sanitize=not args.no_sanitizers), indent=2))
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"Native first-anchor check failed: {exc.stderr or exc.stdout}") from None


if __name__ == "__main__":
    main()
