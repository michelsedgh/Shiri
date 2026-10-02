#!/usr/bin/python3
"""Compile the production socket-policy emitter/transaction under sanitizers.

Actual cgroup BPF attach/bind checks require the separate root Linux harness.
This runner never creates a cgroup, calls bpf(), or changes networking.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

REPOSITORY = Path(__file__).resolve().parents[2]


def run_tests(compiler=None):
    compiler = compiler or shutil.which("clang") or shutil.which("cc")
    if not compiler:
        raise RuntimeError("A C compiler is required for the socket-policy check")
    with tempfile.TemporaryDirectory(prefix="shiri-socket-policy-") as directory:
        binary = Path(directory) / "socket-policy-test"
        subprocess.run([compiler, "-std=c11", "-Wall", "-Wextra", "-Werror", "-O1", "-g",
                        "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                        str(REPOSITORY / "tests/native/test_socket_policy.c"), "-o", str(binary)],
                       check=True, capture_output=True, text=True, timeout=30)
        result = subprocess.run([str(binary)], check=True, capture_output=True, text=True, timeout=30,
                                env={**os.environ, "ASAN_OPTIONS": f"detect_leaks={int(sys.platform != 'darwin')}:halt_on_error=1",
                                     "UBSAN_OPTIONS": "halt_on_error=1"})
        return {"ok": True, "sanitized": True, "kernel_tested": False, "output": result.stdout}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiler")
    arguments = parser.parse_args()
    print(json.dumps(run_tests(arguments.compiler)))
