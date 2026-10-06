#!/usr/bin/env python3
"""Execute exact buffered AP2 mapping/configuration bodies with scripted clocks.

The installer supplies its composed pinned source. Offline tests use the same
hash-pinned excerpt; no network, devices, acoustic assertions, or model clocks.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
PATCH = HERE.parents[1] / "install/patches/shairport-5.5.2-buffered-timing.patch"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def provenance():
    result = json.loads((HERE / "shairport_buffered_timing-origin.json").read_text())
    if digest(PATCH.read_bytes()) != result["patch_sha256"]:
        raise ValueError("Buffered timing patch differs from its reviewed digest")
    return result


def function(text, signature):
    start = text.index(signature)
    return text[start:text.index("\n}\n", start) + 3]


def extract(rtp, audio):
    bodies = [function(rtp, signature) for signature in (
        "void set_ptp_anchor_info(", "int get_ptp_anchor_local_time_info(",
        "int frame_to_ptp_local_time(", "int local_ptp_time_to_frame(",
    )]
    start = rtp.index("int32_t stream_specified_latency =")
    realtime = rtp[start:rtp.index("              set_ptp_anchor_info(conn,", start)]
    start = rtp.index("                int32_t latency_offset =")
    ap1 = rtp[start:rtp.index("                if (la != conn->latency)", start)]
    result = ("\n".join(bodies)
            + "\nstatic void realtime_window(rtsp_conn_info *conn,uint32_t frame_1,uint32_t frame_2){\n"
            + realtime + "}\nstatic uint32_t ap1_window(rtsp_conn_info *conn,uint32_t la){\n"
            + ap1 + "return la;}\n" + function(audio, "static int init("))
    # Keep the checked-in excerpt whitespace-clean. Full source byte hashes
    # above still pin the actual functions, including upstream whitespace.
    return "\n".join(line.rstrip() for line in result.splitlines()) + "\n"


def preimage_files(source):
    """Remove only this exact overlay before unchanged historical source guards.

    Returning the four verified preimages avoids copying or mutating the build
    tree. A partial/new/unreviewed overlay is rejected, never silently removed.
    """
    source = Path(source)
    data = provenance()
    with tempfile.TemporaryDirectory(prefix="shiri-phone1-inverse-") as temporary:
        target = Path(temporary)
        for name, expected in data["postimage_sha256"].items():
            value = (source / name).read_bytes()
            if digest(value) != expected:
                raise ValueError("Buffered timing postimage changed: " + name)
            (target / name).write_bytes(value)
        subprocess.run(["git", "apply", "--reverse", str(PATCH)], cwd=target,
                       check=True, capture_output=True, text=True, timeout=10)
        result = {}
        for name, expected in data["preimage_sha256"].items():
            value = (target / name).read_bytes()
            if digest(value) != expected:
                raise ValueError("Buffered timing exact inverse changed: " + name)
            result[name] = value
    return result


def run_tests(*, source=None, compiler=None, sanitize=True):
    data = provenance()
    excerpt = (HERE / "shairport_buffered_timing.inc").read_bytes()
    if digest(excerpt) != data["excerpt_sha256"]:
        raise ValueError("Reviewed buffered timing native excerpt changed")
    if source is not None:
        source = Path(source)
        preimage_files(source)
        actual = extract((source / "rtp.c").read_text(), (source / "audio_shiri.c").read_text())
        retained = excerpt.decode()
        if retained[retained.index("void set_ptp_anchor_info("):] != actual:
            raise ValueError("Actual buffered timing functions differ from the reviewed excerpt")
    compiler = compiler or shutil.which("clang") or shutil.which("cc")
    if not compiler:
        raise RuntimeError("A C compiler is required for buffered timing checks")
    flags = ["-std=c11", "-D_POSIX_C_SOURCE=200809L", "-O1", "-Wall", "-Wextra", "-Werror"]
    if sanitize:
        flags += ["-fsanitize=address,undefined", "-fno-sanitize-recover=all"]
    environment = {**os.environ, "ASAN_OPTIONS": "detect_leaks=0:halt_on_error=1",
                   "UBSAN_OPTIONS": "halt_on_error=1"}
    results = {}
    with tempfile.TemporaryDirectory(prefix="shiri-buffered-timing-c-") as temporary:
        for name, extra in (("shiri", []), ("without_shiri", ["-DTEST_WITHOUT_SHIRI=1"])):
            binary = Path(temporary) / name
            subprocess.run([compiler, *flags, *extra,
                            str(HERE / "test_shairport_buffered_timing.c"), "-o", str(binary)],
                           check=True, capture_output=True, text=True, timeout=30)
            completed = subprocess.run([str(binary)], env=environment, check=True,
                                       capture_output=True, text=True, timeout=10)
            results[name] = json.loads(completed.stdout)
    return {"ok": True, "sanitized": sanitize, "actual_source": source is not None,
            "patch_sha256": data["patch_sha256"], "results": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--compiler")
    parser.add_argument("--no-sanitizers", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(run_tests(source=args.source, compiler=args.compiler,
                                   sanitize=not args.no_sanitizers), indent=2))
    except subprocess.CalledProcessError as error:
        raise SystemExit("Native buffered timing check failed: " + (error.stderr or error.stdout)) from None


if __name__ == "__main__":
    main()
