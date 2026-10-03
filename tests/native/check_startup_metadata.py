#!/usr/bin/env python3
"""Prove initial DMAP metadata is acknowledged before Shiri output startup.

All sequence bodies/definitions, payload and DMAP serialization are extracted
from exact reviewed OwnTone source. RTSP replies are held by a controlled local
transport; no network or audio device is opened. The old missing request is
reproduced on its exact drain1 preimage.
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
PATCH = ROOT / "install/patches/owntone-29.3-startup-metadata.patch"
PATCH_SHA = "11cd0aec9232b716d2ea3c28a9ac2d321ca24779937063178ad40982d45df525"
AIRPLAY_SHA = "9b598a3fe18042476af9e6a535f3201c74276eeee86da84c95c7fc51438e27b9"
PREIMAGE_AIRPLAY_SHA = "395d6f7b12c937327f145399107cee4354d5df09a022a82282255b7d7365475b"
VERSION = "29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-resample1-framed1-alsa1-speech1-ready1-anchor1-jitter1-owner1-balance1-transition1-bed1-event1-idle1-drain1-startupmeta1"


def module(name):
    spec = importlib.util.spec_from_file_location("metadata_" + name, HERE / (name + ".py"))
    assert spec and spec.loader
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def verify_source(source: Path):
    if hashlib.sha256(PATCH.read_bytes()).hexdigest() != PATCH_SHA:
        raise ValueError("Unreviewed startup metadata patch")
    if hashlib.sha256((source / "src/outputs/airplay.c").read_bytes()).hexdigest() != AIRPLAY_SHA:
        raise ValueError("Unreviewed startup metadata AirPlay source")
    if f"AC_INIT([owntone], [{VERSION}])" not in (source / "configure.ac").read_text():
        raise ValueError("Exact startupmeta1 source marker required")
    with tempfile.TemporaryDirectory(prefix="shiri-metadata-strict-backout-") as temporary:
        inverse = Path(temporary) / "inverse"
        shutil.copytree(source / "src", inverse / "src", symlinks=True,
                        ignore=shutil.ignore_patterns("*.o", "*.lo", ".libs", ".deps"))
        shutil.copyfile(source / "configure.ac", inverse / "configure.ac")
        for options in (["--check"], []):
            subprocess.run(["git", "apply", "--reverse", *options, str(PATCH)], cwd=inverse,
                           check=True, capture_output=True, text=True, timeout=20)
        prior = module("check_speech_drain").verify_source(inverse)
    return {"strict_inverse_startupmeta1": True, "unchanged_drain_idle_event_timing_guards": prior}


def check(source: Path, *, compiler=None, preimage=False):
    source = source.resolve(strict=True)
    expected_sha = PREIMAGE_AIRPLAY_SHA if preimage else AIRPLAY_SHA
    if hashlib.sha256((source / "src/outputs/airplay.c").read_bytes()).hexdigest() != expected_sha:
        raise ValueError("Exact startupmeta1/drain1 AirPlay source required")
    version = VERSION.removesuffix("-startupmeta1") if preimage else VERSION
    if f"AC_INIT([owntone], [{version}])" not in (source / "configure.ac").read_text():
        raise ValueError("Exact startupmeta1/drain1 source marker required")
    historical = {} if preimage else verify_source(source)
    bodies = module("check_paused_speech").body
    airplay = (source / "src/outputs/airplay.c").read_text()
    dmap = (source / "src/dmap_common.c").read_text()
    requests = re.search(r"  \{\n#if AIRPLAY_USE_AUTH_SETUP\n(.*?)\n  \},\n  \{\n    \{ AIRPLAY_SEQ_PROBE", airplay, re.S)
    if not requests:
        raise ValueError("Exact START_PLAYBACK request block required")
    request_body = "#if AIRPLAY_USE_AUTH_SETUP\n" + requests.group(1)
    # Keep actual conditional auth plus every request field and its ordering.
    definition = re.search(r"^  (\{ AIRPLAY_SEQ_START_PLAYBACK,.*?\}),$", airplay, re.M)
    if not definition:
        raise ValueError("Exact START_PLAYBACK success definition required")
    structs = "\n".join(re.search(r"struct " + name + r"\n\{.*?\n\};", airplay, re.S).group(0)
                        for name in ["airplay_seq_definition", "airplay_seq_request", "airplay_seq_ctx"])
    fixture = (HERE / "test_startup_metadata.c").read_text()
    replacements = {
        "ACTUAL_DMAP_HELPERS": "\n".join(bodies(dmap, name) for name in
                                         ["dmap_add_container", "dmap_add_char", "dmap_add_string", "dmap_add_short"]),
        "ACTUAL_STARTUP_PAYLOAD": ("static int payload_make_shiri_startup_metadata(struct evrtsp_request *r,struct airplay_session *s,void *a){(void)r;(void)s;(void)a;return 1;}"
                                   if preimage else bodies(airplay, "payload_make_shiri_startup_metadata")),
        "ACTUAL_SEQUENCE_TYPES": structs,
        "ACTUAL_SESSION_CONNECTED": bodies(airplay, "session_connected"),
        "ACTUAL_START_FAILURE": bodies(airplay, "start_failure"),
        "ACTUAL_STARTUP_DEFINITION": "[AIRPLAY_SEQ_START_PLAYBACK] = " + definition.group(1) + ",",
        "ACTUAL_STARTUP_REQUESTS": request_body,
        "ACTUAL_SEQUENCE_FUNCTIONS": "\n".join(bodies(airplay, name) for name in
                                                (["response_handler_shiri_startup_metadata"] if not preimage else []) + ["sequence_continue_cb", "sequence_continue", "sequence_start"]),
    }
    for marker, value in replacements.items():
        fixture = fixture.replace("/* @" + marker + "@ */", value)
    compiler = compiler or shutil.which("clang") or shutil.which("cc")
    if not compiler:
        raise ValueError("A C compiler is required")
    with tempfile.TemporaryDirectory(prefix="shiri-startup-metadata-c-") as temporary:
        unit = Path(temporary) / "metadata.c"
        unit.write_text(fixture)
        binary = unit.with_suffix("")
        command = [compiler, "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter",
                   "-Wno-unused-function", "-Wno-unused-variable", "-Wno-sign-compare", "-O1",
                   "-fsanitize=address,undefined", "-fno-sanitize-recover=all", str(unit), "-o", str(binary)]
        built = subprocess.run(command, capture_output=True, text=True, timeout=60)
        if built.returncode:
            raise RuntimeError(built.stderr)
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, ASAN_OPTIONS="detect_leaks=0:halt_on_error=1",
                                         UBSAN_OPTIONS="halt_on_error=1"))
        failure = "Shiri startup acknowledged CONNECTED without initial DMAP metadata"
        if preimage:
            if result.returncode == 0 or failure not in result.stderr:
                raise RuntimeError("Exact missing metadata preimage did not reproduce: " + result.stdout + result.stderr)
        elif result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
    return {"ok": True, "sanitized": True, "preimage_failure_reproduced": preimage,
            "airplay_sha256": expected_sha, "patch_sha256": PATCH_SHA, **historical,
            "captured_metadata_body_bytes": 71, "initial_rtp_info_from_exact_session": True,
            "metadata_ack_before_connected": not preimage, "physical_playback_proof": False,
            "results": [failure] if preimage else result.stdout.strip().splitlines()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--preimage", type=Path)
    parser.add_argument("--compiler")
    args = parser.parse_args()
    result = check(args.source, compiler=args.compiler)
    if args.preimage:
        result["preimage"] = check(args.preimage, compiler=args.compiler, preimage=True)
    print(json.dumps(result, indent=2))
