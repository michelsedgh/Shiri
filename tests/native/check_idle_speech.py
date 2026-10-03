#!/usr/bin/env python3
"""Prove idle speech on exact player C bodies and reproduce the event1 failure.

The controlled output callbacks do not prove hardware emission. The fixture
does prove that paused/empty idle preparation gets a fresh output-only mix,
without filling or reading a program FIFO or altering original music timing.
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

ROOT = Path(__file__).resolve().parents[2]
NATIVE = Path(__file__).resolve().parent
PATCH = ROOT / "install/patches/owntone-29.3-idle-speech.patch"
PATCH_SHA = "19161c472ceb6ab80d88d3e22202ea16c37a7829ed5d44db027cdcd3b73ac29c"
PLAYER_SHA = "ac30806a6bab2032b2ece07fdfbbe36d5b2ecdc7dd4b1376a341cf5b74be202e"
PREIMAGE_PLAYER_SHA = "8d85e2010e6b0402ebddbdb61d965663e8cc333bf5accf587edcb4d9b1585613"
PREIMAGE_VERSION = "29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-resample1-framed1-alsa1-speech1-ready1-anchor1-jitter1-owner1-balance1-transition1-bed1-event1"


def builder():
    spec = importlib.util.spec_from_file_location("idle_paused_fixture", NATIVE / "check_paused_speech.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_source(source: Path):
    """Undo only idle1 privately and retain all exact historical validators."""
    if hashlib.sha256(PATCH.read_bytes()).hexdigest() != PATCH_SHA:
        raise ValueError("Unreviewed idle1 composition patch")
    if hashlib.sha256((source / "src/player.c").read_bytes()).hexdigest() != PLAYER_SHA:
        raise ValueError("Unreviewed idle1 player source")
    with tempfile.TemporaryDirectory(prefix="shiri-idle-strict-backout-") as temporary:
        inverse = Path(temporary) / "inverse"
        shutil.copytree(source / "src", inverse / "src", symlinks=True,
                        ignore=shutil.ignore_patterns("*.o", "*.lo", ".libs", ".deps"))
        shutil.copyfile(source / "configure.ac", inverse / "configure.ac")
        for options in (["--check"], []):
            subprocess.run(["git", "apply", "--reverse", *options, str(PATCH)], cwd=inverse,
                           check=True, capture_output=True, text=True, timeout=20)
        spec = importlib.util.spec_from_file_location("idle_historical_events", NATIVE / "check_airplay_events.py")
        assert spec and spec.loader
        event = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(event)
        prior = event.verify_source(inverse)
    return {"strict_inverse_idle1": True, "unchanged_event_bed_transition_owner_guards": prior}


def check(source: Path, *, compiler=None, preimage=False):
    source = source.resolve(strict=True)
    player = source / "src/player.c"
    if preimage and hashlib.sha256(player.read_bytes()).hexdigest() != PREIMAGE_PLAYER_SHA:
        raise ValueError("The failure must be reproduced on the exact event1 player source")
    version = PREIMAGE_VERSION + ("" if preimage else "-idle1")
    if re.findall(r"^AC_INIT\(\[owntone\], \[([^]]+)\]", (source / "configure.ac").read_text(), re.M) != [version]:
        raise ValueError("Exact event1/idle1 source marker required")
    historical = {} if preimage else verify_source(source)
    compiler = compiler or shutil.which("clang") or shutil.which("cc")
    if not compiler:
        raise ValueError("A C compiler is required")
    with tempfile.TemporaryDirectory(prefix="shiri-idle-real-c-") as temporary:
        unit = Path(temporary) / "idle.c"
        if not builder().assemble(source, unit):
            raise ValueError("The idle fixture requires the reviewed paused-speech bed")
        text = unit.read_text()
        if text.count("int main(void)") != 1:
            raise ValueError("The inherited paused-speech fixture changed its entry point")
        unit.write_text(text.replace("int main(void)", "static int paused_fixture_main(void)")
                        + "\n" + (NATIVE / "test_idle_speech_lifecycle.c").read_text())
        binary = unit.with_suffix("")
        flags = [compiler, "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter",
                 "-Wno-unused-variable", "-Wno-unused-function", "-Wno-sign-compare", "-O1",
                 "-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-I" + str(source / "src"),
                 '-DSHIRI_SPEECH_SOURCE="' + str(source / "src/shiri_speech.c") + '"', str(unit), "-lm", "-o", str(binary)]
        subprocess.run(flags, capture_output=True, text=True, check=True)
        run = subprocess.run([str(binary)], capture_output=True, text=True,
                             env=dict(os.environ, ASAN_OPTIONS="detect_leaks=0:halt_on_error=1",
                                      UBSAN_OPTIONS="halt_on_error=1"))
        expected = "idle owner preparation did not arm an output-only clock"
        if preimage:
            if run.returncode == 0 or expected not in run.stderr:
                raise ValueError("Exact idle failure did not reproduce: " + run.stdout + run.stderr)
        elif run.returncode:
            raise ValueError("Idle lifecycle failed: " + run.stdout + run.stderr)
        return {"ok": True, "sanitized": True, "preimage_failure_reproduced": preimage,
                "player_sha256": hashlib.sha256(player.read_bytes()).hexdigest(),
                "patch_sha256": PATCH_SHA,
                **historical,
                "kernel_or_physical_output_proof": False,
                "results": [expected] if preimage else run.stdout.strip().splitlines()}


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
