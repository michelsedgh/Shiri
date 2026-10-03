#!/usr/bin/env python3
"""Reproduce the framed empty-input refill failure using exact native C bodies.

The input buffer, markers, reads, source callback, suspend and timer bodies are
extracted from reviewed sources. Memory-buffer, event, output and clock seams
are local and controlled. This test opens no network or audio device.
"""
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

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PATCH = ROOT / "install/patches/owntone-29.3-cold-music.patch"
PATCH_SHA = "58f3346db99fadfa448a1c02c91d1ec95656b04fc0100928ef4aa790cada6c80"
PLAYER_SHA = "8b845515d43a4334f90ee4779a800b4dba0c1bb9a5fb2c3891f28ba546a4b9fc"
AIRPLAY_SHA = '405243e11ea3f24301f449346b359d81134ae20ff75c5389c491e776f1dcc046'
PREIMAGE_PLAYER_SHA = "9d11e31ca558861951a6f5285aa4b933b58cecc6aeb59ea806420aa3258eef6c"
VERSION = "29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-resample1-framed1-alsa1-speech1-ready1-anchor1-jitter1-owner1-balance1-transition1-bed1-event1-idle1-drain1-startupmeta1-coldmusic1"


def module(name):
    spec = importlib.util.spec_from_file_location("cold_music_" + name, HERE / (name + ".py"))
    assert spec and spec.loader
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def verify_source(source: Path):
    if hashlib.sha256(PATCH.read_bytes()).hexdigest() != PATCH_SHA:
        raise ValueError("Unreviewed cold music patch")
    if hashlib.sha256((source / "src/player.c").read_bytes()).hexdigest() != PLAYER_SHA:
        raise ValueError("Unreviewed cold music player source")
    if hashlib.sha256((source / "src/outputs/airplay.c").read_bytes()).hexdigest() != AIRPLAY_SHA:
        raise ValueError("Unreviewed cold music AirPlay source")
    if f"AC_INIT([owntone], [{VERSION}])" not in (source / "configure.ac").read_text():
        raise ValueError("Exact coldmusic1 source marker required")
    with tempfile.TemporaryDirectory(prefix="shiri-cold-music-strict-backout-") as temporary:
        inverse = Path(temporary) / "inverse"
        shutil.copytree(source / "src", inverse / "src", symlinks=True,
                        ignore=shutil.ignore_patterns("*.o", "*.lo", ".libs", ".deps"))
        shutil.copyfile(source / "configure.ac", inverse / "configure.ac")
        for options in (["--check"], []):
            subprocess.run(["git", "apply", "--reverse", *options, str(PATCH)], cwd=inverse,
                           check=True, capture_output=True, text=True, timeout=20)
        prior = module("check_startup_metadata").verify_source(inverse)
    return {"strict_inverse_coldmusic1": True, "unchanged_metadata_drain_idle_timing_guards": prior}


def assemble(source: Path, target: Path):
    bodies = module("check_paused_speech").body
    player = (source / "src/player.c").read_text()
    input_source = (source / "src/input.c").read_text()
    input_header = (source / "src/input.h").read_text()
    definitions = "\n".join(re.search(r"struct " + name + r"\n\{.*?\n\};", input_source, re.S).group(0)
                            for name in ["marker", "input_buffer"])
    enum = re.search(r"enum input_flags\n\{.*?\n\};", input_header, re.S).group(0)
    thresholds = "\n".join(line for line in input_source.splitlines()
                           if line.startswith("#define INPUT_BUFFER_THRESHOLD ") or
                           line.startswith("#define INPUT_BUFFER_NATIVE_THRESHOLD "))
    input_functions = "\n".join(bodies(input_source, name) for name in
                                 ["input_buffer_threshold", "marker_add", "markers_set", "buffer_full_cb",
                                  "input_write", "input_peek_sync", "input_read", "input_buffer_full_cb"])
    player_functions = "\n".join(bodies(player, name) for name in
                                  ["source_read", "pb_timer_native_reject", "pb_timer_native_prepare",
                                   "pb_timer_start", "pb_timer_stop", "pb_suspend", "playback_cb"])
    fixture = (HERE / "test_cold_music.c").read_text()
    for name, content in [("INPUT_FLAGS", enum), ("INPUT_DEFINITIONS", definitions),
                          ("INPUT_THRESHOLDS", thresholds), ("INPUT_FUNCTIONS", input_functions),
                          ("PLAYER_FUNCTIONS", player_functions)]:
        marker = "/* @" + name + "@ */"
        if fixture.count(marker) != 1:
            raise ValueError("The exact cold music fixture changed: " + marker)
        fixture = fixture.replace(marker, content)
    target.write_text(fixture)


def check(source: Path, *, compiler=None, preimage=False):
    source = source.resolve(strict=True)
    expected_sha = PREIMAGE_PLAYER_SHA if preimage else PLAYER_SHA
    if hashlib.sha256((source / "src/player.c").read_bytes()).hexdigest() != expected_sha:
        raise ValueError("Exact startupmeta1/coldmusic1 player source required")
    version = VERSION.removesuffix("-coldmusic1") if preimage else VERSION
    if f"AC_INIT([owntone], [{version}])" not in (source / "configure.ac").read_text():
        raise ValueError("Exact startupmeta1/coldmusic1 marker required")
    historical = module("check_startup_metadata").verify_source(source) if preimage else verify_source(source)
    compiler = compiler or shutil.which("clang") or shutil.which("cc")
    if not compiler:
        raise ValueError("A C compiler is required")
    results = []
    with tempfile.TemporaryDirectory(prefix="shiri-cold-music-real-c-") as temporary:
        unit = Path(temporary) / "cold-music.c"
        assemble(source, unit)
        for timer_mode in ("posix", "timerfd"):
            binary = unit.with_name("cold-music-" + timer_mode)
            command = [compiler, "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter",
                       "-Wno-unused-variable", "-Wno-unused-function", "-Wno-sign-compare", "-O1",
                       "-fsanitize=address,undefined", "-fno-sanitize-recover=all", str(unit), "-pthread",
                       *(["-DPREIMAGE=1"] if preimage else []),
                       *(["-DHAVE_TIMERFD=1"] if timer_mode == "timerfd" else []), "-o", str(binary)]
            built = subprocess.run(command, capture_output=True, text=True, timeout=60)
            if built.returncode:
                raise RuntimeError(built.stderr)
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30,
                                 env=dict(os.environ, ASAN_OPTIONS="detect_leaks=" + ("0" if sys.platform == "darwin" else "1") + ":halt_on_error=1",
                                          UBSAN_OPTIONS="halt_on_error=1"))
            if run.returncode:
                raise RuntimeError(run.stdout + run.stderr)
            if preimage and "six-second refill aged an original future marker into strict anchor_late" not in run.stdout:
                raise ValueError("The exact framed empty-input refill failure did not reproduce")
            results.append({"timer_mode": timer_mode, "checks": run.stdout.strip().splitlines()})
    return {"ok": True, "sanitized": True, "preimage_failure_reproduced": preimage,
            "player_sha256": expected_sha, "patch_sha256": PATCH_SHA, **historical,
            "no_socket_or_audio_device": True, "physical_playback_proof": False,
            "results": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--preimage", type=Path)
    parser.add_argument("--preimage-only", action="store_true")
    parser.add_argument("--compiler")
    args = parser.parse_args()
    result = check(args.source, compiler=args.compiler, preimage=args.preimage_only)
    if args.preimage:
        result["preimage"] = check(args.preimage, compiler=args.compiler, preimage=True)
    print(json.dumps(result, indent=2))
