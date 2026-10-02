#!/usr/bin/env python3
"""Compile unchanged audited sender/receiver C excerpts; no network or devices.

Provenance hashes for the exact composed/pinned sources accompany the excerpt.
The input buffers must be calculated by the Python policy under review. This
check deliberately does not implement another copy of that policy in C.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent


def run_tests(buffers: list[int], *, compiler: str | None = None) -> dict:
    if (type(buffers) is not list or len(buffers) != 4001
            or any(type(buffer) is not int or not 500 <= buffer <= 4250 for buffer in buffers)):
        raise ValueError("Exactly4001 bounded integer buffer decisions are required")
    compiler = compiler or shutil.which("clang") or shutil.which("cc")
    if not compiler:
        raise RuntimeError("Actual sender/receiver C seam requires a compiler")
    provenance = json.loads((HERE / "airplay_receiver_lead-origin.json").read_text())
    excerpt = HERE / "airplay_receiver_lead.inc"
    if hashlib.sha256(excerpt.read_bytes()).hexdigest() != provenance["excerpt_sha256"]:
        raise RuntimeError("Audited receiver source excerpt changed")
    flags = ["-std=c11", "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined", "-fno-sanitize-recover=all"]
    environment = {**os.environ, "ASAN_OPTIONS": "detect_leaks=0:halt_on_error=1", "UBSAN_OPTIONS": "halt_on_error=1"}
    with tempfile.TemporaryDirectory(prefix="shiri-receiver-lead-") as name:
        target = Path(name) / "receiver-lead"
        command = [compiler, *flags, str(HERE / "test_airplay_receiver_lead.c"), "-o", str(target)]
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
        decisions = "".join(f"{offset} {buffer}\n" for offset, buffer in zip(range(-2000, 2001), buffers, strict=True))
        completed = subprocess.run([str(target)], input=decisions, env=environment,
                                   capture_output=True, text=True, timeout=10)
        if completed.returncode not in (0, 1):
            raise RuntimeError(completed.stderr or "Actual receiver C seam failed unexpectedly")
        report = json.loads(completed.stdout)
        if completed.returncode != int(not report["ok"]):
            raise RuntimeError("Receiver C result differs from its actual exit code")
    return {**report, "sanitized": True, "source": "exact audited source excerpts", "excerpt_sha256": provenance["excerpt_sha256"]}
