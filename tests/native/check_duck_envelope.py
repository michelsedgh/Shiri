#!/usr/bin/env python3
"""Qualify optional authenticated duck envelopes on exact native speech code.

The old source is recovered by strict inverse patching and checked through the
unchanged outputclock1/history guards. Fixtures open no socket or audio device.
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
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PATCH = ROOT / "install/patches/owntone-29.3-duck-envelope.patch"
PATCH_SHA = "34790726bf9e14085bcde2519f6bb736c36e951f771f78d0282134cf3f669bab"
VERSION = "29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-resample1-framed1-alsa1-speech1-ready1-anchor1-jitter1-owner1-balance1-transition1-bed1-event1-idle1-drain1-startupmeta1-coldmusic1-outputclock1-duck1"
SOURCE_SHA = {
    "configure.ac": "ff48b20c46cf2d2e1f629ddfe309e52693c1f26cdcdcc43d7c630a60008eb64d",
    "src/player.c": "82165cab13dcc5c11850c975e7148d25f770309950a80abf97268fbfaf677300",
    "src/shiri_speech.c": "fee49970c0af0e6a5c53a35ecb935894cae67ab51a30f3fd322c6e0a0411c4b3",
    "src/shiri_speech.h": "185c28e728786b07a6ee0ddc39f9e7e0bb7014471fbe7c34932da7f3e3e25519",
    "src/outputs/airplay.c": "e6cd3a395548b9c6cf431424ae2fa76c58b347e83626041c982b418a4b84dac2",
}
PREIMAGE_SHA = {
    "configure.ac": "434c8dc415859470f3797ab60379ed92df614380bb111db1c7d894d47754801a",
    "src/player.c": "ff5b250602d61e0ec2b837b3a3019c6c2547bd0c9fb86a31eaafac6691f26f53",
    "src/shiri_speech.c": "a73e4e85494e1461f50339aa65f932fc7f233b8b8c903921992f8a1d5b862eff",
    "src/shiri_speech.h": "45a031ece6e6683fbd9f8ea530144249c337ced603975371b997c051e3ceb24d",
}


def module(name):
    spec = importlib.util.spec_from_file_location("duck_envelope_" + name, HERE / (name + ".py"))
    assert spec and spec.loader
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def inverse_source(source: Path, destination: Path):
    if hashlib.sha256(PATCH.read_bytes()).hexdigest() != PATCH_SHA:
        raise ValueError("Unreviewed duck envelope patch")
    files = re.findall(r"^diff --git a/(\S+) b/\S+$", PATCH.read_text(), re.MULTILINE)
    if files != ["configure.ac", "src/player.c", "src/shiri_speech.c", "src/shiri_speech.h"]:
        raise ValueError("Duck envelope cannot change input, clock or output transport code")
    for name, expected in SOURCE_SHA.items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != expected:
            raise ValueError("Unreviewed duck envelope source: " + name)
    if f"AC_INIT([owntone], [{VERSION}])" not in (source / "configure.ac").read_text():
        raise ValueError("Exact duck1 marker required")
    shutil.copytree(source / "src", destination / "src", symlinks=True,
                    ignore=shutil.ignore_patterns("*.o", "*.lo", ".libs", ".deps"))
    shutil.copyfile(source / "configure.ac", destination / "configure.ac")
    for options in (["--check"], []):
        subprocess.run(["git", "apply", "--reverse", *options, str(PATCH)], cwd=destination,
                       check=True, capture_output=True, text=True, timeout=20)
    for name, expected in PREIMAGE_SHA.items():
        if hashlib.sha256((destination / name).read_bytes()).hexdigest() != expected:
            raise ValueError("Duck envelope inverse differs from frozen outputclock1: " + name)
    historical = module("check_output_clock").verify_source(destination)
    return {"strict_inverse_duck1": True, "unchanged_outputclock_and_historical_guards": historical}


def verify_source(source: Path):
    with tempfile.TemporaryDirectory(prefix="shiri-duck-envelope-inverse-") as temporary:
        return inverse_source(source, Path(temporary) / "inverse")


def check(source: Path, *, compiler=None):
    source = source.resolve(strict=True)
    compiler = compiler or shutil.which("clang") or shutil.which("cc")
    if not compiler:
        raise ValueError("A C compiler is required")
    environment = dict(os.environ, ASAN_OPTIONS="detect_leaks=0:halt_on_error=1",
                       UBSAN_OPTIONS="halt_on_error=1")
    with tempfile.TemporaryDirectory(prefix="shiri-duck-envelope-fixture-") as temporary:
        directory = Path(temporary)
        inverse = directory / "inverse"
        historical = inverse_source(source, inverse)
        results = []

        def fixture(name, tree, suffix=""):
            binary = directory / (name + suffix)
            unit = HERE / (name + ".c")
            if name == "test_duck_source_boundary":
                player = (tree / "src/player.c").read_text()
                extract = module("check_paused_speech").body
                param = re.search(r"struct shiri_source_param \{.*?\n\};", player, re.S)
                if not param:
                    raise ValueError("Missing actual source command state")
                text = unit.read_text().replace("/* @ACTUAL_SOURCE_PARAM@ */", param.group(0))
                for tag, body in (("SEAL_FLUSH", "shiri_source_seal_flush"), ("ARM", "shiri_source_arm")):
                    text = text.replace("/* @ACTUAL_SOURCE_" + tag + "@ */", extract(player, body))
                if "/* @ACTUAL_" in text:
                    raise ValueError("Unexpanded actual source boundary")
                unit = directory / (name + ".c")
                unit.write_text(text)
            command = [compiler, "-std=gnu11", "-Wall", "-Wextra", "-Werror",
                       "-Wno-unused-function", "-Wno-unused-variable", "-O1", "-g",
                       "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                       "-I", str(tree / "src"),
                       "-DSHIRI_SPEECH_SOURCE=" + json.dumps(str(tree / "src/shiri_speech.c")),
                       str(unit), "-lm", "-o", str(binary)]
            built = subprocess.run(command, capture_output=True, text=True, timeout=60)
            if built.returncode:
                raise RuntimeError(built.stderr)
            executed = subprocess.run([str(binary)], capture_output=True, timeout=20, env=environment)
            if executed.returncode:
                raise RuntimeError(executed.stderr.decode(errors="replace"))
            return executed.stdout

        for name in ("test_duck_envelope", "test_duck_source_boundary", "test_speech_owner"):
            result = fixture(name, source)
            results.append({"fixture": name, "stdout": result.decode().strip().splitlines()})
        before = fixture("test_duck_envelope_legacy", inverse, "-before")
        after = fixture("test_duck_envelope_legacy", source, "-after")
        if len(before) != 80 * 480 * 4 or before != after:
            raise ValueError("Legacy zero-word PCM differs from the actual frozen preimage")
        return {"ok": True, "sanitized": True, "no_socket_or_audio_device": True,
                "private_policy_bounds_ms": {"attack": [40, 2000], "release": [40, 5000]},
                "policy_locked_per_admitted_owner": True, "full_actual_target_durations": True,
                "idle_terminal_fresh_music_prefix_unchanged": True,
                "legacy_zero_pcm_byte_exact": True, "legacy_pcm_bytes": len(before),
                "legacy_pcm_sha256": hashlib.sha256(before).hexdigest(),
                "physical_playback_proof": False, "patch_sha256": PATCH_SHA,
                "source_hashes": SOURCE_SHA, "composed_source_guards": historical,
                "results": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--compiler")
    args = parser.parse_args()
    print(json.dumps(check(args.source, compiler=args.compiler), indent=2))
