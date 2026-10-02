#!/usr/bin/python3
"""Check actual pinned BlueALSA request, codec-reset, poll and controller seams."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "install/patches/bluealsa-5.0.0-drop-sync.patch"
PIN = "1a84465dd860d1be9dcf62339c6273e9e0632dd2"


def function(text, name):
    match = re.search(rf"(?m)^(?:static )?(?:bool|int|void|ssize_t) {name}\([^{{]+\{{", text)
    if not match:
        raise ValueError(f"Actual source seam {name} is missing")
    opening = text.index("{", match.start())
    depth, end = 1, opening + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[match.start():end] + "\n"


def patch_image(name, after=True):
    block = PATCH.read_text().split(f"diff --git a/{name} b/{name}\n", 1)[1].split("\ndiff --git ", 1)[0]
    lines, hunk = [], False
    for line in block.splitlines():
        if line.startswith("@@"):
            hunk = True
            lines.append("")
        elif hunk and line.startswith(("+" if after else "-", " ")):
            lines.append(line[1:])
    return "\n".join(lines)


def run_tests(source=None, compiler=None, sanitize=True, require_sbc=False):
    compiler = compiler or shutil.which("clang") or shutil.which("cc")
    if not compiler:
        raise RuntimeError("A C compiler is required for BlueALSA DropSync checks")
    def native(name):
        return (Path(source) / name).read_text() if source else patch_image(name)
    pcm, io = native("src/ba-transport-pcm.c"), native("src/io.c")
    header, io_header = native("src/ba-transport-pcm.h"), native("src/io.h")
    dbus, sbc = native("src/bluealsa-dbus.c"), native("src/a2dp-sbc.c")
    request = "\n".join(function(pcm, name) for name in (
        "ba_transport_pcm_drop_sync_supported", "ba_transport_pcm_drop_sync_cancel"))
    request += "\nstruct drop_sync_waiter { struct ba_transport_pcm *pcm; uint64_t token; };\n"
    request += "\n".join(function(pcm, name) for name in (
        "drop_sync_wait_cancel", "ba_transport_pcm_drop_sync_lock", "ba_transport_pcm_drop_sync", "ba_transport_pcm_drop_sync_claim",
        "ba_transport_pcm_drop_sync_complete"))
    io_functions = "\n".join(function(io, name) for name in (
        "io_poll_drain_complete", "io_pcm_drop_sync_flush", "io_poll_drop_sync_complete", "io_poll_and_read_pcm"))
    reset = sbc.split("if (errno == ESTALE) {", 1)[1].split("\n\t\t\t}", 1)[0].rsplit("\t\t\t\tcontinue;", 1)[0]
    fixture = (ROOT / "tests/native/test_bluealsa_drop_sync.c").read_text()
    fixture = fixture.replace("/* @ACTUAL_DROP_FIELDS@ */", header.split("\t/* Exact, bounded SBC DropSync", 1)[1].split("\n\t/* notification PIPE */", 1)[0].split("\n", 1)[1])
    fixture = fixture.replace("/* @ACTUAL_IO_FIELDS@ */", io_header.split("\t/* Exact synchronous drop", 1)[1].split("\n\t/* keep-alive", 1)[0].split("\n", 1)[1])
    fixture = fixture.replace("/* @ACTUAL_REQUEST_FUNCTIONS@ */", request).replace("/* @ACTUAL_IO_FUNCTIONS@ */", io_functions)
    fixture = fixture.replace("/* @ACTUAL_RELEASE@ */", function(pcm, "ba_transport_pcm_release"))
    fixture = fixture.replace("/* @ACTUAL_CONTROLLER@ */", function(dbus, "bluealsa_pcm_controller"))
    fixture = fixture.replace("/* @ACTUAL_OPEN_WRAPPERS@ */", "\n".join(function(dbus, name) for name in (
        "bluealsa_pcm_open", "bluealsa_pcm_open_restricted")))
    fixture = fixture.replace("/* @ACTUAL_SBC_RESET@ */", reset)
    # These cleanup and advertisement checks consume the actual source, not mirrored logic.
    assert "ba_transport_pcm_drop_sync_cancel(pcm);" in function(pcm, "ba_transport_pcm_release")
    assert "ba_transport_pcm_drop_sync_cancel(pcm);" in function(pcm, "ba_transport_pcm_thread_cleanup")
    assert '"SynchronousDrop"' in dbus and 'name="SynchronousDrop" type="b" access="read"' in native("src/dbus/org.bluealsa.xml")
    common_open = function(dbus, "bluealsa_pcm_open_common")
    assert "pcm->controller_restricted = restricted;" in common_open
    assert "restricted && !ba_transport_pcm_drop_sync_supported(pcm)" in common_open
    assert 'name="RestrictedController" type="b" access="read"' in native("src/dbus/org.bluealsa.xml")
    assert '<method name="OpenRestricted">' in native("src/dbus/org.bluealsa.xml")
    assert "pthread_condattr_setclock(&drop_attr, CLOCK_MONOTONIC)" in pcm
    flags = ["-std=c11", "-D_POSIX_C_SOURCE=200809L", "-Wall", "-Wextra", "-Werror", "-Wno-sign-compare", "-O1", "-pthread"]
    if sanitize:
        flags += ["-fsanitize=address,undefined", "-fno-sanitize-recover=all"]
    environment = dict(os.environ, ASAN_OPTIONS="detect_leaks=0:halt_on_error=1", UBSAN_OPTIONS="halt_on_error=1")
    with tempfile.TemporaryDirectory(prefix="shiri-bluealsa-sync-") as temporary:
        path = Path(temporary)
        unit, executable = path / "drop_sync.c", path / "drop_sync"
        unit.write_text(fixture)
        built = subprocess.run([compiler, *flags, str(unit), "-o", str(executable)], capture_output=True, text=True, timeout=30)
        if built.returncode:
            raise RuntimeError(built.stderr)
        result = subprocess.run([str(executable)], capture_output=True, text=True, env=environment, timeout=10)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        def previous(name):
            if source:
                original = subprocess.run(["git", "-C", str(source), "show", f"{PIN}:{name}"],
                                          capture_output=True, text=True, check=True, timeout=10)
                return original.stdout
            return patch_image(name, after=False)
        preimage = (ROOT / "tests/native/test_bluealsa_drop_preimage.c").read_text()
        preimage = preimage.replace("/* @ACTUAL_PREIMAGE_DROP@ */", function(previous("src/ba-transport-pcm.c"), "ba_transport_pcm_drop"))
        preimage = preimage.replace("/* @ACTUAL_PREIMAGE_CONTROLLER@ */", function(previous("src/bluealsa-dbus.c"), "bluealsa_pcm_controller"))
        unit.write_text(preimage)
        built = subprocess.run([compiler, *flags, str(unit), "-o", str(executable)], capture_output=True, text=True, timeout=30)
        if built.returncode:
            raise RuntimeError(built.stderr)
        before = subprocess.run([str(executable)], capture_output=True, text=True, env=environment, timeout=10)
        if before.returncode:
            raise RuntimeError(before.stdout + before.stderr)
        pkg = shutil.which("pkg-config")
        found = subprocess.run([pkg, "--cflags", "--libs", "sbc"], capture_output=True, text=True, timeout=10) if pkg else None
        if found is not None and found.returncode == 0:
            codec = (ROOT / "tests/native/test_bluealsa_sbc_reset.c").read_text().replace("/* @ACTUAL_SBC_RESET@ */", reset)
            unit.write_text(codec)
            built = subprocess.run([compiler, *flags, str(unit), *shlex.split(found.stdout), "-o", str(executable)],
                                   capture_output=True, text=True, timeout=30)
            if built.returncode:
                raise RuntimeError(built.stderr)
            linked = subprocess.run([str(executable)], capture_output=True, text=True, env=environment, timeout=10)
            if linked.returncode:
                raise RuntimeError(linked.stdout + linked.stderr)
            codec_result = linked.stdout.strip()
        elif require_sbc:
            raise RuntimeError("Installed SBC library headers/pkg-config are required for the codec reset proof")
        else:
            codec_result = "linked SBC codec check unavailable: pkg-config sbc required"
    return {"ok": True, "sanitized": sanitize, "pin": PIN,
            "results": [result.stdout.strip(), before.stdout.strip(), codec_result]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--compiler")
    parser.add_argument("--no-sanitize", action="store_true")
    parser.add_argument("--require-sbc", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run_tests(args.source, args.compiler, not args.no_sanitize, args.require_sbc)))
