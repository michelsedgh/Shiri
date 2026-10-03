#!/usr/bin/env python3
"""Check the additive natural-EOF drain on exact native player C bodies.

The pending receive queue controls only the OS transport seam; the admission,
mix, command, timer and output-bed bodies are compiled from reviewed sources.
Captured PCM ends at outputs_write, so this is not physical playback proof.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
NATIVE = Path(__file__).resolve().parent
PATCH = ROOT / "install/patches/owntone-29.3-speech-drain.patch"
PATCH_SHA = "2e240eecaad804b805d886b961116e142f0dff94d6c437f134b96f80b8408563"
PLAYER_SHA = "9d11e31ca558861951a6f5285aa4b933b58cecc6aeb59ea806420aa3258eef6c"
PREIMAGE_PLAYER_SHA = "ac30806a6bab2032b2ece07fdfbbe36d5b2ecdc7dd4b1376a341cf5b74be202e"
VERSION = "29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-resample1-framed1-alsa1-speech1-ready1-anchor1-jitter1-owner1-balance1-transition1-bed1-event1-idle1-drain1"


def module(name):
    spec = importlib.util.spec_from_file_location("drain_" + name, NATIVE / (name + ".py"))
    assert spec and spec.loader
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def verify_source(source: Path):
    if hashlib.sha256(PATCH.read_bytes()).hexdigest() != PATCH_SHA:
        raise ValueError("Unreviewed speech drain patch")
    if hashlib.sha256((source / "src/player.c").read_bytes()).hexdigest() != PLAYER_SHA:
        raise ValueError("Unreviewed speech drain player source")
    with tempfile.TemporaryDirectory(prefix="shiri-drain-strict-backout-") as temporary:
        inverse = Path(temporary) / "inverse"
        shutil.copytree(source / "src", inverse / "src", symlinks=True,
                        ignore=shutil.ignore_patterns("*.o", "*.lo", ".libs", ".deps"))
        shutil.copyfile(source / "configure.ac", inverse / "configure.ac")
        for options in (["--check"], []):
            subprocess.run(["git", "apply", "--reverse", *options, str(PATCH)], cwd=inverse,
                           check=True, capture_output=True, text=True, timeout=20)
        prior = module("check_idle_speech").verify_source(inverse)
    return {"strict_inverse_drain1": True, "unchanged_idle_event_bed_transition_owner_guards": prior}


def assemble(source: Path, unit: Path):
    module("check_paused_speech").assemble(source, unit)
    text = unit.read_text()
    assert text.count("#include SHIRI_SPEECH_SOURCE") == 1
    text = text.replace("#include SHIRI_SPEECH_SOURCE", """#define shiri_speech_poll underlying_socket_poll
#include SHIRI_SPEECH_SOURCE
#undef shiri_speech_poll
static uint8_t pending_packet[SPEECH_HEADER + 1920];
static size_t pending_bytes;
static void shiri_speech_poll(void) {
 if(pending_bytes) {
   speech_receive(&speech,pending_packet,pending_bytes,now_ns);
   pending_bytes=0;
 }
}
static int captured_voice[230400];
static unsigned capture_count;
static bool capture_enabled;
static FILE *capture_stream;
static void capture_case(uint64_t frames) {
 if(capture_stream) {
   uint32_t marker=0;
   assert(fwrite(&marker,sizeof(marker),1,capture_stream)==1);
   assert(fwrite(&frames,sizeof(frames),1,capture_stream)==1);
 }
}
""")
    assert text.count("static void outputs_write(") == 1
    text = text.replace("static void outputs_write(", "static void original_outputs_write(")
    boundary = "static void pb_abort(void)"
    assert text.count(boundary) == 1
    text = text.replace(boundary, """static void outputs_write(void *pcm,int bytes,int frames,struct media_quality *quality,struct timespec *pts) {
 original_outputs_write(pcm,bytes,frames,quality,pts);
 if(capture_enabled) {
   if(capture_stream) {
     uint32_t samples=(uint32_t)frames;
     uint64_t presentation=(uint64_t)pts->tv_sec*1000000000+(uint64_t)pts->tv_nsec;
     assert(fwrite(&samples,sizeof(samples),1,capture_stream)==1);
     assert(fwrite(&presentation,sizeof(presentation),1,capture_stream)==1);
     assert(fwrite(pcm,bytes,1,capture_stream)==1);
   }
   uint8_t *data=pcm;
   for(int i=0;i<frames;i++) {
     unsigned left=data[4*i] | ((unsigned)data[4*i+1]<<8);
     unsigned right=data[4*i+2] | ((unsigned)data[4*i+3]<<8);
     assert(left==right);
     if(left) {
       assert(capture_count<sizeof(captured_voice)/sizeof(captured_voice[0]));
       captured_voice[capture_count++]=(int)left;
     }
   }
 }
}
""" + boundary)
    assert text.count("int main(void)") == 1
    text = text.replace("int main(void)", "static int paused_fixture_main(void)")
    idle = (NATIVE / "test_idle_speech_lifecycle.c").read_text().split("int main(void)")[0]
    unit.write_text(text + "\n" + idle + "\n" + (NATIVE / "test_speech_drain_lifecycle.c").read_text())


def check(source: Path, *, compiler=None, preimage=False, capture: Path | None = None):
    source = source.resolve(strict=True)
    expected_sha = PREIMAGE_PLAYER_SHA if preimage else PLAYER_SHA
    if hashlib.sha256((source / "src/player.c").read_bytes()).hexdigest() != expected_sha:
        raise ValueError("Exact idle1/drain1 player source required")
    expected_version = VERSION.removesuffix("-drain1") if preimage else VERSION
    if f"AC_INIT([owntone], [{expected_version}])" not in (source / "configure.ac").read_text():
        raise ValueError("Exact idle1/drain1 source marker required")
    historical = {} if preimage else verify_source(source)
    compiler = compiler or shutil.which("clang") or shutil.which("cc")
    if not compiler:
        raise ValueError("A C compiler is required")
    with tempfile.TemporaryDirectory(prefix="shiri-drain-real-c-") as temporary:
        unit = Path(temporary) / "drain.c"
        assemble(source, unit)
        binary = unit.with_suffix("")
        flags = [compiler, "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter",
                 "-Wno-unused-variable", "-Wno-unused-function", "-Wno-sign-compare", "-O1",
                 "-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-I" + str(source / "src"),
                 '-DSHIRI_SPEECH_SOURCE="' + str(source / "src/shiri_speech.c") + '"', str(unit), "-lm", "-o", str(binary)]
        subprocess.run(flags, capture_output=True, text=True, check=True, timeout=60)
        run = subprocess.run([str(binary), *([str(capture)] if capture else [])], capture_output=True, text=True, timeout=30,
                             env=dict(os.environ, ASAN_OPTIONS="detect_leaks=0:halt_on_error=1",
                                      UBSAN_OPTIONS="halt_on_error=1"))
        failure = "FINISH sealed admission before its pending final PCM datagram"
        if preimage:
            if run.returncode == 0 or failure not in run.stderr:
                raise ValueError("Exact FINISH race did not reproduce: " + run.stdout + run.stderr)
        elif run.returncode:
            raise ValueError("Speech drain lifecycle failed: " + run.stdout + run.stderr)
    return {"ok": True, "sanitized": True, "preimage_failure_reproduced": preimage,
            "player_sha256": expected_sha, "patch_sha256": PATCH_SHA, **historical,
            "pcm_capture_boundary": "outputs_write; before conversion and device transport",
            "physical_output_proof": False,
            "results": [failure] if preimage else run.stdout.strip().splitlines()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--preimage", type=Path)
    parser.add_argument("--preimage-only", action="store_true")
    parser.add_argument("--compiler")
    parser.add_argument("--capture", type=Path, help="Optional silent binary bed PCM corpus for the downstream codec check")
    args = parser.parse_args()
    result = check(args.source, compiler=args.compiler, preimage=args.preimage_only, capture=args.capture)
    if args.preimage:
        result["preimage"] = check(args.preimage, compiler=args.compiler, preimage=True)
    print(json.dumps(result, indent=2))
