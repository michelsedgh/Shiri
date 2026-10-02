#!/usr/bin/env python3
"""Compile the actual isolated OwnTone framed output, without devices/network."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "install/patches/owntone-29.3-framed-output.patch"
spec = importlib.util.spec_from_file_location("source_extract", ROOT / "tests/native/check_source_transition.py")
extract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(extract)


def run_tests(*, source: Path | None = None, compiler=None, sanitize=True, vectors=()):
    compiler = compiler or shutil.which("clang") or shutil.which("cc")
    if not compiler:
        raise RuntimeError("A C compiler is required for actual framed-output tests")

    def native(name):
        return (source / name).read_text() if source else extract.postimage(PATCH, name)

    module = native("src/outputs/shiri_pcm_output.c")
    # Keep the actual shared player event-base declaration. Stripping the full
    # prefix previously hid a missing extern in an otherwise passing mock TU.
    module = module[module.index("extern struct event_base *evbase_player;"):]
    fixture = (ROOT / "tests/native/test_framed_output.c").read_text().replace("/* @ACTUAL_MODULE@ */", module)
    flags = ["-std=gnu11", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter", "-Wno-unused-variable", "-O1", "-g"]
    if sanitize:
        flags += ["-fsanitize=address,undefined", "-fno-sanitize-recover=all"]
    environment = dict(os.environ, ASAN_OPTIONS="detect_leaks=0:halt_on_error=1", UBSAN_OPTIONS="halt_on_error=1")
    with tempfile.TemporaryDirectory(prefix="shiri-framed-native-") as temp:
        directory = Path(temp)
        for name in ["pcm_volume.h", "shiri_out_wire.h"]:
            if name == "pcm_volume.h" and not source:
                header = extract.postimage(ROOT / "install/patches/owntone-29.3-software-volume.patch", f"src/outputs/{name}")
            else:
                header = native(f"src/outputs/{name}")
            (directory / name).write_text(header)
        unit, program = directory / "actual.c", directory / "actual"
        unit.write_text(fixture)
        built = subprocess.run([compiler, *flags, str(unit), "-o", str(program)], capture_output=True, text=True, timeout=30)
        if built.returncode:
            raise RuntimeError(built.stderr)
        result = subprocess.run([str(program)], env=environment, capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        for packet, accepted in vectors:
            checked = subprocess.run([str(program), packet.hex()], env=environment, capture_output=True, text=True, timeout=10)
            if checked.returncode != (0 if accepted else 2):
                raise RuntimeError("C/Python wire boundary disagrees: " + checked.stdout + checked.stderr)
        lines = result.stdout.strip().splitlines()
        vector = next(line.split("=", 1)[1] for line in lines if line.startswith("WIRE_VECTOR="))
        return {"ok": True, "sanitized": sanitize, "actual_source": source is not None,
                "wire_vector": vector, "result": "\n".join(line for line in lines if not line.startswith("WIRE_VECTOR="))}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--compiler")
    parser.add_argument("--no-sanitize", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run_tests(source=args.source, compiler=args.compiler, sanitize=not args.no_sanitize), indent=2))
