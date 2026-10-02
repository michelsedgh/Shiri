#!/usr/bin/env python3
"""Compile full configured OwnTone outputs/transcode + framed output with real FFmpeg.

Manual Linux check. The caller supplies an isolated, configured pinned source
checkout; nothing is installed and no PCM/network device is opened. --observe
records failures explicitly for diagnosis and is not an acceptance test.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ["libavcodec", "libavformat", "libavfilter", "libavutil", "libswresample", "libevent", "libconfuse", "libcurl"]


def run(source: Path, *, observe=False, compiler=None, sanitize=True):
    source = source.resolve(strict=True)
    names = ["config.h", "src/player.c", "src/outputs.c", "src/transcode.c", "src/outputs/shiri_pcm_output.c",
             "src/outputs/shiri_out_wire.h", "src/outputs/pcm_volume.h"]
    hashes = {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in names}
    compiler = compiler or shutil.which("cc")
    if not compiler:
        raise RuntimeError("A C compiler is required")
    flags = subprocess.run(["pkg-config", "--cflags", "--libs", *PACKAGES], capture_output=True, text=True, timeout=10)
    if flags.returncode:
        raise RuntimeError("Actual FFmpeg/libevent/libconfuse development packages are required: " + flags.stderr)
    with tempfile.TemporaryDirectory(prefix="shiri-real-resampler-") as temporary:
        directory = Path(temporary)
        spec = importlib.util.spec_from_file_location("full_resample_extract", ROOT / "tests/native/check_source_transition.py")
        extract = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(extract)
        player = (source / "src/player.c").read_text()
        param = extract.between(player, "struct shiri_source_param {", "\nstruct event_base *evbase_player;")
        (directory / "actual_seal_param.inc").write_text(param)
        (directory / "actual_seal_function.inc").write_text(extract.function(player, "shiri_source_seal_flush"))
        program = directory / "actual"
        command = [compiler, "-std=gnu11", "-D_GNU_SOURCE", "-DHAVE_CONFIG_H", "-ffunction-sections", "-fdata-sections",
                   "-O1", "-g", "-I", str(directory), "-I", str(source), "-I", str(source / "src"),
                   str(ROOT / "tests/native/test_framed_resampler.c"), str(source / "src/transcode.c"),
                   "-Wl,--gc-sections", "-o", str(program), *shlex.split(flags.stdout), "-lm"]
        if sanitize:
            command += ["-fsanitize=address,undefined", "-fno-sanitize-recover=all"]
        built = subprocess.run(command, capture_output=True, text=True, timeout=60)
        if built.returncode:
            raise RuntimeError("Full actual source compilation failed:\n" + built.stderr)
        environment = dict(os.environ, ASAN_OPTIONS="detect_leaks=1:halt_on_error=1", UBSAN_OPTIONS="halt_on_error=1")
        measured = subprocess.run([str(program), *(["--observe"] if observe else [])], env=environment,
                                  capture_output=True, text=True, timeout=30)
        try:
            report = json.loads(measured.stdout)
        except ValueError as exc:
            raise RuntimeError("Actual source fixture failed before its bounded report:\n" + measured.stderr) from exc
        return {"ok": measured.returncode == 0 and report["failures"] == 0, "observe_only": observe,
                "source_hashes": hashes, "sanitized": sanitize, "returncode": measured.returncode,
                "result": report, "diagnostics": measured.stderr[-12000:]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--observe", action="store_true")
    parser.add_argument("--compiler")
    parser.add_argument("--no-sanitize", action="store_true")
    args = parser.parse_args()
    result = run(args.source, observe=args.observe, compiler=args.compiler, sanitize=not args.no_sanitize)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["ok"] or args.observe else 1)
