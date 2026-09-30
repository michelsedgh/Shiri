#!/usr/bin/python3
"""Compile portable scaler tests and real patched ALSA seams without devices."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

REPOSITORY = Path(__file__).resolve().parents[2]
PATCH = REPOSITORY / "install/patches/owntone-29.3-software-volume.patch"


def additions(path):
    patch = PATCH.read_text()
    block = patch.split(f"diff --git a/{path} b/{path}\n", 1)[1].split("\ndiff --git ", 1)[0]
    return "\n".join(line[1:] for line in block.splitlines() if line.startswith("+") and not line.startswith("+++")) + "\n"


def function(source, name):
    match = re.search(rf"(?m)^{name}\([^\n]*\)\n\{{", source)
    if not match:
        raise ValueError(f"Missing actual patched function: {name}")
    start = source.rfind("\nstatic ", 0, match.start()) + 1
    if start == 0 and not source.startswith("static "):
        raise ValueError(f"Missing function declaration: {name}")
    opening = source.index("{", match.start())
    depth = 1
    index = opening + 1
    while depth:
        depth += (source[index] == "{") - (source[index] == "}")
        index += 1
    return source[start:index] + "\n"


def run_tests(*, source=None, compiler=None, sanitize=True):
    compiler = compiler or shutil.which("clang") or shutil.which("cc")
    if not compiler:
        raise RuntimeError("A C compiler is required for the patched-backend checks")
    flags = ["-std=c99", "-D_POSIX_C_SOURCE=200809L", "-O1", "-Wall", "-Wextra", "-Werror"]
    if sanitize:
        flags += ["-fsanitize=address,undefined", "-fno-sanitize-recover=all"]
    environment = os.environ.copy()
    environment["ASAN_OPTIONS"] = "detect_leaks=0:halt_on_error=1"
    environment["UBSAN_OPTIONS"] = "halt_on_error=1"
    results = []
    with tempfile.TemporaryDirectory(prefix="shiri-backend-c-check-") as temporary:
        directory = Path(temporary)
        if source:
            header = (source / "src/outputs/pcm_volume.h").read_text()
            alsa = (source / "src/outputs/alsa.c").read_text()
            if "[29.3-shiri-swvol1]" not in (source / "configure.ac").read_text():
                raise ValueError("The supplied source does not carry the expected patch marker")
        else:
            header = additions("src/outputs/pcm_volume.h")
            alsa = additions("src/outputs/alsa.c")
        (directory / "pcm_volume.h").write_text(header)
        (directory / "pcm_write.inc").write_text(function(alsa, "pcm_write"))
        if source:
            names = ("alsa_session_free", "alsa_session_make", "alsa_device_start", "alsa_device_volume_set")
            (directory / "alsa_control.inc").write_text("\n".join(function(alsa, name) for name in names))

        for name in ("test_owntone_pcm_volume", "test_owntone_alsa_seam"):
            target = directory / name
            extra = ["-DTEST_ALSA_CONTROL=1"] if source and name.endswith("alsa_seam") else []
            subprocess.run([
                compiler, *flags, *extra, "-I", str(directory),
                str(REPOSITORY / "tests/native" / (name + ".c")), "-lm", "-o", str(target),
            ], check=True, capture_output=True, text=True, timeout=30)
            completed = subprocess.run([str(target)], check=True, capture_output=True, text=True, env=environment, timeout=30)
            results.append(completed.stdout.strip())
    return {"ok": True, "sanitized": sanitize, "actual_control_functions": source is not None, "results": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="Optional patched, pinned OwnTone checkout for hardware-control seam mocks")
    parser.add_argument("--compiler", help="Explicit C compiler")
    parser.add_argument("--no-sanitizers", action="store_true", help="Explicit fallback for a compiler without ASan/UBSan")
    args = parser.parse_args()
    try:
        print(json.dumps(run_tests(source=args.source, compiler=args.compiler, sanitize=not args.no_sanitizers), indent=2))
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"Patched-backend C check failed: {exc.stderr or exc.stdout}") from None


if __name__ == "__main__":
    main()
