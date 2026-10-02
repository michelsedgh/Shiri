#!/usr/bin/python3
"""Compile the actual Avahi runtime-directory seam, without daemon or host changes."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "install/patches/avahi-0.8-private-user.patch"


def patched_context():
    block = PATCH.read_text().split("diff --git a/avahi-daemon/main.c b/avahi-daemon/main.c\n", 1)[1]
    return "\n".join(line[1:] for line in block.splitlines() if line[:1] in {" ", "+"} and not line.startswith("+++"))


def extract_runtime(source):
    match = re.search(r"static int make_runtime_dir\(void\)\s*\{", source)
    if not match:
        raise ValueError("Actual runtime-directory function is missing")
    start = source.index("{", match.start())
    depth, end = 1, start + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[match.start():end] + "\n"


def run_tests(*, source=None, compiler=None):
    compiler = compiler or shutil.which("clang") or shutil.which("cc")
    if not compiler:
        raise RuntimeError("A C compiler is required")
    if source:
        actual = (source / "avahi-daemon/main.c").read_text()
        if "[0.8-shiri-user1]" not in (source / "configure.ac").read_text():
            raise ValueError("Supplied Avahi source lacks its expected version marker")
    else:
        actual = patched_context()
    with tempfile.TemporaryDirectory(prefix="shiri-avahi-native-") as temporary:
        directory = Path(temporary)
        (directory / "avahi_runtime.inc").write_text(extract_runtime(actual))
        target = directory / "runtime-check"
        subprocess.run([
            compiler, "-std=c99", "-D_DEFAULT_SOURCE", "-O1", "-Wall", "-Wextra", "-Werror",
            "-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-I", str(directory),
            str(ROOT / "tests/native/test_avahi_runtime.c"), "-o", str(target),
        ], check=True, capture_output=True, text=True, timeout=30)
        environment = dict(os.environ, ASAN_OPTIONS="detect_leaks=0:halt_on_error=1", UBSAN_OPTIONS="halt_on_error=1")
        result = subprocess.run([str(target)], check=True, capture_output=True, text=True,
                                env=environment, timeout=10)
        return {"ok": True, "sanitized": True, "actual_source": source is not None,
                "result": result.stdout.strip()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path)
    parser.add_argument("--compiler")
    args = parser.parse_args()
    try:
        print(json.dumps(run_tests(source=args.source, compiler=args.compiler)))
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"Avahi runtime seam failed: {exc.stderr or exc.stdout}") from None


if __name__ == "__main__":
    main()
