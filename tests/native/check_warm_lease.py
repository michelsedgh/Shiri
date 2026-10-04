#!/usr/bin/env python3
"""Qualify finite connection-only leases on exact reviewed OwnTone source.

Fixtures open no socket, service or audio device. Strict inversion checks the
unchanged duck envelope, clock-selection and historical source boundaries.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PATCH = ROOT / "install/patches/owntone-29.3-warm-lease.patch"
PATCH_SHA = "37cc3a7a6ab6c6fcb57dbc93c7e4e9208193aa6c219559a12930f2b07574959e"
VERSION = "29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-resample1-framed1-alsa1-speech1-ready1-anchor1-jitter1-owner1-balance1-transition1-bed1-event1-idle1-drain1-startupmeta1-coldmusic1-outputclock1-duck1-warm1"
SOURCE_SHA = {
    "configure.ac": "18ad07ea8d35944ef19835bd455d25ae9c117d808bd800af886e978abccc33c9",
    "src/httpd_jsonapi.c": "aa825dd9eafe9678fa58d86d7d7acbffa82f2d78ceb1db0b136fea74d4a6cb90",
    "src/outputs.c": "1803d5040fa699c9469a1a87360ceab104e3343cfa368786fc19fe8995901b59",
    "src/outputs.h": "79965413c0d2c0dbc2d2cd2e97ee0fd4863118b31ea164bc129b1aa1c7224bfc",
    "src/outputs/airplay.c": "213c2fcb5fc6cca452d22b8c19b1a70fd70a6844c5599be675d6531553ed2219",
    "src/player.c": "44bdff90575678420d4131e0377150823e38c91c30c4dee53f61942f83907969",
    "src/shiri_warm.h": "c6b95c2e24d3c08f59617a3a4e4fa91eb604b66c7f460413aa2722544f3c5dbc",
    "src/shiri_warm_json.h": "bb43658f6cd1173a5df143b7e094ab52c1aabf453ec43a475983712593184380"
}
PREIMAGE_SHA = {
    "configure.ac": "ff48b20c46cf2d2e1f629ddfe309e52693c1f26cdcdcc43d7c630a60008eb64d",
    "src/httpd_jsonapi.c": "14d5207bb8e1651915e6c4dd5ecd837c03e0fb06e650a5c5c8565c2df79d4e65",
    "src/outputs.c": "08df3c6e3e451bc9e5190dd367e6c58402945a5827495ffd9dfbe5e596a1cb9d",
    "src/outputs.h": "44632caeffd43cb98741464f5a1dd5c501d5c080717f0b3c2a81a687c1518336",
    "src/outputs/airplay.c": "e6cd3a395548b9c6cf431424ae2fa76c58b347e83626041c982b418a4b84dac2",
    "src/player.c": "82165cab13dcc5c11850c975e7148d25f770309950a80abf97268fbfaf677300"
}


def module(name):
    spec = importlib.util.spec_from_file_location("warm_lease_" + name, HERE / (name + ".py"))
    assert spec and spec.loader
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def inverse_source(source, destination):
    if hashlib.sha256(PATCH.read_bytes()).hexdigest() != PATCH_SHA:
        raise ValueError("Unreviewed connection lease patch")
    if re.findall(r"^diff --git a/(\S+) b/\S+$", PATCH.read_text(), re.M) != list(SOURCE_SHA):
        raise ValueError("Connection lease file boundary changed")
    for name, expected in SOURCE_SHA.items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != expected:
            raise ValueError("Unreviewed connection lease source: " + name)
    shutil.copytree(source / "src", destination / "src", symlinks=True,
                    ignore=shutil.ignore_patterns("*.o", "*.lo", ".libs", ".deps"))
    shutil.copyfile(source / "configure.ac", destination / "configure.ac")
    for options in (["--check"], []):
        subprocess.run(["git", "apply", "--reverse", *options, str(PATCH)], cwd=destination,
                       check=True, capture_output=True, text=True, timeout=20)
    for name, expected in PREIMAGE_SHA.items():
        if hashlib.sha256((destination / name).read_bytes()).hexdigest() != expected:
            raise ValueError("Connection lease inverse differs from frozen duck1: " + name)
    for name in ("src/shiri_warm.h", "src/shiri_warm_json.h"):
        if (destination / name).exists():
            raise ValueError("Connection lease inverse retained its new API")
    return {"strict_inverse_warm1": True,
            "unchanged_duck_clock_and_historical_guards": module("check_duck_envelope").verify_source(destination)}


def verify_source(source):
    with tempfile.TemporaryDirectory(prefix="shiri-warm-lease-inverse-") as temporary:
        return inverse_source(source, Path(temporary) / "inverse")


def check(source, *, compiler=None):
    source = source.resolve(strict=True)
    historical = verify_source(source)
    extract = module("check_paused_speech").body
    player = (source / "src/player.c").read_text()
    outputs = (source / "src/outputs.c").read_text()
    start = player.index("/* Connection-only preparation: separate identity/counter from music and voice. */")
    end = player.index("\nint\nplayer_shiri_warm(", start)
    lifecycle = player[start:end]
    if any(term in lifecycle for term in ("outputs_write(", "shiri_speech_begin(", "shiri_speech_mix(", "shiri_speech_bed_arm(", "pb_timer_start(")):
        raise ValueError("Connection warming cannot admit audio, speech or a playback clock")
    speech_status = module("check_output_clock").declarations((source / "src/shiri_speech.h").read_text(), "shiri_speech_status")
    replacements = {"ACTUAL_WARM_LIFECYCLE": lifecycle, "ACTUAL_SPEECH_STATUS": speech_status}
    for tag, function in (("SESSION_ADD", "outputs_device_session_add"), ("SESSION_REMOVE", "outputs_device_session_remove"),
                          ("REMAINING", "outputs_shiri_warm_remaining"), ("STOP", "outputs_device_stop"),
                          ("DELAYED_STOP", "outputs_device_stop_delayed"), ("STOP_TIMER", "stop_timer_cb"),
                          ("HOLD", "outputs_shiri_warm_hold"), ("RELEASE", "outputs_shiri_warm_release")):
        replacements["ACTUAL_OUTPUT_" + tag] = extract(outputs, function)
    compiler = compiler or shutil.which("clang") or shutil.which("cc")
    if not compiler:
        raise ValueError("A C compiler is required")
    environment = dict(os.environ, ASAN_OPTIONS="detect_leaks=0:halt_on_error=1", UBSAN_OPTIONS="halt_on_error=1")
    results = []
    with tempfile.TemporaryDirectory(prefix="shiri-warm-lease-fixture-") as temporary:
        directory = Path(temporary)
        for name in ("test_warm_lease", "test_warm_lease_json"):
            text = (HERE / (name + ".c")).read_text()
            for tag, value in replacements.items():
                text = text.replace("/* @" + tag + "@ */", value)
            if "/* @ACTUAL_" in text:
                raise ValueError("Unexpanded connection lease source fixture")
            unit, binary = directory / (name + ".c"), directory / name
            unit.write_text(text)
            flags = []
            if name.endswith("_json"):
                flags = shlex.split(subprocess.run(["pkg-config", "--cflags", "--libs", "json-c"],
                    check=True, capture_output=True, text=True, timeout=10).stdout)
            command = [compiler, "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-Wno-unused-function",
                       "-Wno-unused-variable", "-Wno-unused-parameter", "-O1", "-g",
                       "-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-I", str(source / "src"),
                       str(unit), "-o", str(binary), *flags]
            compiled = subprocess.run(command, capture_output=True, text=True, timeout=60)
            if compiled.returncode:
                raise RuntimeError(compiled.stderr)
            executed = subprocess.run([str(binary)], capture_output=True, text=True, env=environment, timeout=20)
            if executed.returncode:
                raise RuntimeError(executed.stderr)
            results.append({"fixture": name, "stdout": executed.stdout.strip().splitlines()})
    return {"ok": True, "sanitized": True, "no_socket_or_audio_device": True,
            "connection_only_no_pcm": True, "observe_read_only": True,
            "max_lease_ns": 300_000_000_000, "max_setup_ns": 3_000_000_000,
            "terminal_generation_fence": True, "exact_session_hold": True,
            "physical_standby_or_playback_proof": False, "patch_sha256": PATCH_SHA,
            "source_hashes": SOURCE_SHA, "composed_source_guards": historical, "results": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--compiler")
    args = parser.parse_args()
    print(json.dumps(check(args.source, compiler=args.compiler), indent=2))
