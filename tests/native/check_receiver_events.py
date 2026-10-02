#!/usr/bin/env python3
"""Verify additive bounded metadata layer without retiring original28 guards."""

import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

PATCH_SHA = "a7ffecbe2fe0da846b12ec34b2303a6279d5ad4f7ba4c2312234db0c634d7ecd"
BACKEND_HASHES = {
    "Makefile.am": "ab6205e210684f7b9bd93f531efb25862213eb637c2cd0180d5423e604532d8d",
    "ap2_event_message_handler.c": "c560a21af39fded6f6d94d1d9d09fecee4a361711c424ed857041a296f2d56eb",
    "audio.h": "cbc32c1bbdd7c48b16fdce68f7b7a1216354c3a8e2fc7872a9806abcbb0b62b9",
    "audio_shiri.c": "199ea0575b5d83f11901758b043d36fc8b31e6b7d21c50564da49cef4dd4f1a9",
    "common.c": "f70fe84343403c33a5b73a4e11326000ec31739e94eb1255c5a5448bc6811df7",
    "player.c": "7d65933ac8a99f62984daa5a67ea9a12943a2f569c60b7a81029c91366ddcb93",
    "player.h": "4e4a36d9265b555f3e96e293250ffb22e4b5e4af4ceeefdc3b9652184778ebf7",
    "shiri_ap2_volume.c": "509d12f4fec6935319a1c439e10493930e9a89e3eb83fa158c1ba55aac82479a",
    "shiri_event_control.h": "1bacf2c166e856e16417dce90016b3f9794e2495688e1a24b6dae54510e3e493",
    "shiri_volume_control.h": "c0760aa336bebda5ab157a43e4a23731f234558af8db25efdfea26faf31674d8",
    "shiri_volume_io.h": "f8470c52b1f3cf037555d75668f85cc95fe38cfaf5d871eb92d4cf5922542428",
    "shiri_volume_lane.inc": "85db01fdac5f57de20b32a2b679fe8835eaadef85f267dc89cf76529083d30f6",
}
BACKEND_HASHES["pair_ap/pair_homekit.c"] = "d2cd0c8af14581fab36098074db779e50c73882c023eb014e1b5c92cac3636a8"
REPOSITORY = Path(__file__).resolve().parents[2]


def original():
    spec = importlib.util.spec_from_file_location(
        "receiver_volume_immutable28", REPOSITORY / "tests/native/check_receiver_volume.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_source(source):
    patch = REPOSITORY / "install/patches/shairport-5.5.2-bounded-events.patch"
    if hashlib.sha256(patch.read_bytes()).hexdigest() != PATCH_SHA:
        raise ValueError("Bounded metadata patch digest differs from the reviewed additive layer")
    for name, digest in BACKEND_HASHES.items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != digest:
            raise ValueError("Actual bounded metadata source differs: " + name)
    return patch


@contextmanager
def preimage_volume1(source):
    patch = verify_source(source)
    with tempfile.TemporaryDirectory(prefix="shiri-event30-volume28-preimage-") as directory:
        build = Path(directory)
        for name in BACKEND_HASHES:
            (build / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, build / name)
        subprocess.run(
            ["git", "apply", "--reverse", "--check", str(patch)],
            cwd=build,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "apply", "--reverse", str(patch)], cwd=build, check=True, capture_output=True, text=True
        )
        original().verify_source(build)
        yield build


def preimage_audio(source):
    # Exact30 -> exact28 -> the existing original timed3 callback check.
    with preimage_volume1(source) as build:
        return original().preimage_audio(build)


def compiler_command(compiler, build, *, sanitize=False, platform=None):
    """Keep GNU socket enums intact; only Darwin lacks MSG_NOSIGNAL."""
    platform = sys.platform if platform is None else platform
    command = [
        compiler,
        "-std=c11",
        "-D_GNU_SOURCE",
        "-g",
        "-O1",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-Wno-unused-function",
        "-pthread",
        "-I",
        str(build),
    ]
    if platform == "darwin":
        command += ["-DMSG_NOSIGNAL=0"]
    if sanitize:
        command += ["-fsanitize=address,undefined", "-fno-omit-frame-pointer"]
    return command


def run_tests(source, compiler="cc", sanitize=False):
    verify_source(source)
    fixture = Path(__file__).parent
    with tempfile.TemporaryDirectory(prefix="shiri-bounded-event-native-") as directory:
        build = Path(directory)
        for name in (
            "shiri_ap2_volume.c",
            "shiri_volume_io.h",
            "shiri_volume_control.h",
            "shiri_pcm.h",
            "shiri_volume_lane.inc",
            "shiri_event_control.h",
        ):
            shutil.copyfile(source / name, build / name)
        for name in (
            "test_receiver_volume.c",
            "test_receiver_volume_records.c",
            "test_receiver_volume_lane.c",
            "receiver_volume_stubs.h",
            "test_receiver_events.c",
        ):
            shutil.copyfile(fixture / name, build / name)
        for name in ("common.h", "player.h", "rtsp.h"):
            (build / name).write_text('#include "receiver_volume_stubs.h"\n')
        (build / "config.h").write_text("#define CONFIG_AIRPLAY_2 1\n")
        command = compiler_command(compiler, build, sanitize=sanitize)
        for name in ("test_receiver_events.c", "test_receiver_volume_lane.c"):
            subprocess.run([*command, str(build / name), "-o", str(build / "test")], check=True)
            subprocess.run([str(build / "test")], check=True, timeout=15)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--compiler", default="cc")
    parser.add_argument("--sanitize", action="store_true")
    args = parser.parse_args()
    with preimage_volume1(args.source):
        pass
    run_tests(args.source, args.compiler, args.sanitize)


if __name__ == "__main__":
    main()
