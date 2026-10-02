#!/usr/bin/python3
"""Sanitize the actual additive timed3 receiver callback with scripted clocks."""
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

REPOSITORY = Path(__file__).resolve().parents[2]
PATCH = REPOSITORY / "install/patches/shairport-5.5.2-clock-recovery.patch"
SAMPLER_SHA = "73116e82bd29b6a0a8e8c3072686a121ffaebe1528c9a94a643f11cb639fd917"
INNER_CASES_SHA = "82fe35468648273de39fb8d166b2dd55e1a4eaa4ea77f58453d83d97e1263eab"


def timing_tools():
    path = REPOSITORY / "tests/native/check_timing_patch.py"
    spec = importlib.util.spec_from_file_location("timed3_original_timing_tools", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def function_body(text, name):
    start = text.index("static int " + name + "(")
    opening = text.index("{", start)
    depth, end = 1, opening + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


def patched_audio(tools):
    """Apply the actual owned file's complete unified hunks, verifying preimages."""
    original = tools.postimage(tools.SHA_PATCH, "audio_shiri.c").lstrip("\n")
    section = PATCH.read_text().split("diff --git a/audio_shiri.c b/audio_shiri.c\n", 1)[1]
    section = section.split("\ndiff --git ", 1)[0]
    if not section.endswith("\n"):
        section += "\n"
    source, output, consumed = original.splitlines(keepends=True), [], 0
    hunks = list(re.finditer(r"(?m)^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@[^\n]*\n", section))
    if not hunks:
        raise ValueError("Recovery patch omitted actual callback hunks")
    for index, hunk in enumerate(hunks):
        start = int(hunk.group(1)) - 1
        if start < consumed:
            raise ValueError("Recovery patch has overlapping hunks")
        output.extend(source[consumed:start])
        consumed = start
        body = section[hunk.end():hunks[index + 1].start() if index + 1 < len(hunks) else len(section)]
        for line in body.splitlines(keepends=True):
            if line.startswith((" ", "-")):
                if consumed >= len(source) or source[consumed] != line[1:]:
                    raise ValueError("Recovery patch callback preimage differs")
                consumed += 1
            if line.startswith((" ", "+")):
                output.append(line[1:])
    output.extend(source[consumed:])
    return "".join(output)


def run_tests(*, shairport_source=None, compiler=None, sanitize=True):
    tools = timing_tools()
    compiler = compiler or shutil.which("clang") or shutil.which("cc")
    if not compiler:
        raise RuntimeError("A C compiler is required for actual timed3 callback checks")
    expected = patched_audio(tools)
    actual = ((shairport_source / "audio_shiri.c").read_text()
              if shairport_source else expected)
    actual_sha = hashlib.sha256(actual.encode()).hexdigest()
    if actual != expected and shairport_source is not None:
        path = REPOSITORY / "tests/native/check_receiver_volume.py"
        spec = importlib.util.spec_from_file_location("receiver_volume_exact_overlay", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        actual = module.preimage_audio(shairport_source)
    if actual != expected:
        raise ValueError("Actual timed3 callback differs from exact additive patch")
    if hashlib.sha256(function_body(actual, "sample_clocks").encode()).hexdigest() != SAMPLER_SHA:
        raise ValueError("Original four-attempt/five-millisecond sampler changed")
    if shairport_source:
        version = (shairport_source / "common.c").read_text()
        if 'strcat(version_string, "-shiri-timed3")' not in version or '"-shiri-timed2"' in version:
            raise ValueError("Recovery backend requires the exact shiri-timed3 marker")
    unit = REPOSITORY / "tests/native/test_shairport_clock_recovery.c"
    original = (REPOSITORY / "tests/native/test_shairport_timing.c").read_text()
    cases = original.split("static void clock_retries(void){", 1)[1].split("  /* Unsampled lead-in", 1)[0]
    retained = unit.read_text().split("static void original_inner_boundaries(void){\n", 1)[1].split("}\nstatic void callback_result", 1)[0]
    if retained != cases or hashlib.sha256(cases.encode()).hexdigest() != INNER_CASES_SHA:
        raise ValueError("Original inner clock boundary cases changed")
    flags = ["-std=c99", "-D_POSIX_C_SOURCE=200809L", "-O1", "-Wall", "-Wextra", "-Werror"]
    if sys.platform == "darwin":
        flags += ["-D_DARWIN_C_SOURCE=1"]
    if sanitize:
        flags += ["-fsanitize=address,undefined", "-fno-sanitize-recover=all"]
    environment = {**os.environ, "ASAN_OPTIONS": "detect_leaks=0:halt_on_error=1",
                   "UBSAN_OPTIONS": "halt_on_error=1"}
    with tempfile.TemporaryDirectory(prefix="shiri-timed3-c-") as temporary:
        directory = Path(temporary)
        (directory / "audio_shiri.inc").write_text(actual)
        for name in ("audio.h", "shiri_pcm.h", "shiri_timing.h"):
            text = ((shairport_source / name).read_text() if shairport_source
                    else tools.postimage(tools.SHA_PATCH, name))
            (directory / name).write_text(text)
        (directory / "common.h").write_text(
            (REPOSITORY / "tests/native/shairport_timing_common.h").read_text())
        (directory / "libconfig.h").write_text("/* Supplied by the unchanged native test seam. */\n")
        target = directory / "clock_recovery"
        subprocess.run([compiler, *flags, "-I", str(directory), str(unit), "-lm", "-pthread", "-o", str(target)],
                       check=True, capture_output=True, text=True, timeout=30)
        result = subprocess.run([str(target)], env=environment, check=True,
                                capture_output=True, text=True, timeout=30)
    return {"ok": True, "sanitized": sanitize, "result": result.stdout.strip(),
            "callback_sha256": hashlib.sha256(actual.encode()).hexdigest(),
            "composed_callback_sha256": actual_sha,
            "original_sampler_sha256": SAMPLER_SHA, "original_inner_cases_sha256": INNER_CASES_SHA,
            "patch_sha256": hashlib.sha256(PATCH.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shairport-source", type=Path)
    parser.add_argument("--compiler")
    parser.add_argument("--no-sanitizers", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(run_tests(shairport_source=args.shairport_source, compiler=args.compiler,
                                   sanitize=not args.no_sanitizers), indent=2))
    except subprocess.CalledProcessError as error:
        raise SystemExit("Native clock recovery check failed: " + (error.stderr or error.stdout)) from None


if __name__ == "__main__":
    main()
