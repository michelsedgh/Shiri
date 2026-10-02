#!/usr/bin/python3
"""Compile actual output gain/selection functions and reject the old master algorithm."""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "install/patches/owntone-29.3-speaker-balance.patch"
spec = importlib.util.spec_from_file_location("volume_source_extract", ROOT / "tests/native/check_source_transition.py")
extract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(extract)

def run_tests(source, compiler=None):
    source = Path(source)
    compiler = compiler or shutil.which("clang") or shutil.which("cc")
    if not compiler:
        raise RuntimeError("Volume settings checks require a C compiler")
    text = (source / "src/outputs.c").read_text()
    names = ("rel_to_vol", "vol_to_rel", "shiri_device_balance_restore", "vol_adjust", "outputs_device_select", "outputs_device_deselect",
             "outputs_device_volume_register", "outputs_volume_set", "outputs_shiri_volume_settings_set")
    functions = "\n\n".join(extract.function(text, name) for name in names)
    fixture = (ROOT / "tests/native/test_volume_settings.c").read_text().replace("/* @ACTUAL_OUTPUT_VOLUME_FUNCTIONS@ */", functions)
    flags = ["-std=c11", "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined", "-fno-sanitize-recover=all"]
    pkg = subprocess.run(["pkg-config", "--cflags", "--libs", "json-c"], capture_output=True, text=True, check=True, timeout=10)
    environment = dict(os.environ, ASAN_OPTIONS="detect_leaks=0:halt_on_error=1", UBSAN_OPTIONS="halt_on_error=1")
    results = []
    with tempfile.TemporaryDirectory(prefix="shiri-native-volume-settings-") as temporary:
        directory = Path(temporary)
        for name in ("shiri_volume_settings.h", "shiri_volume_settings_json.h"):
            (directory / name).write_bytes((source / "src" / name).read_bytes())
        path = directory / "fixture.c"
        binary = directory / "fixture"
        for preimage in (False, True):
            body = fixture
            if preimage:
                old = extract.function(extract.patch_image(PATCH, "src/outputs.c", after=False), "vol_adjust")
                body = body.replace(extract.function(text, "vol_adjust"), old)
                body = body.replace(extract.function(text, "shiri_device_balance_restore"), "")
            path.write_text(body)
            built = subprocess.run([compiler, *flags, "-I" + str(directory), str(path), *shlex.split(pkg.stdout), "-o", str(binary)], capture_output=True, text=True, timeout=30)
            if built.returncode:
                raise RuntimeError(built.stderr)
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10, env=environment)
            if (run.returncode == 0) == preimage:
                raise RuntimeError("Old master algorithm unexpectedly passed" if preimage else run.stderr)
            results.append("preimage rejected: trimmed speakers changed room master" if preimage else run.stdout.strip())
    return {"ok": True, "sanitized": True, "results": results}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--compiler")
    args = parser.parse_args()
    print(json.dumps(run_tests(args.source, args.compiler)))

if __name__ == "__main__":
    main()
