#!/usr/bin/env python3
"""Compile the exact added AP2 module with protocol-library seams and real sockets."""
import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile

PATCH_SHA = "26003aa1b8df99c5de6eecc21074bf259158e64baefd41a35c704145d5957bbd"
BACKEND_HASHES = {'Makefile.am': '9bd009b115beb6c4d26688759b0de2e2029ba6009538bf1b988d40d2e11d5c00', 'ap2_event_message_handler.c': '0e5f13006a7f34af53f1b9d60c60e31e3db978baa2b0ce310f97ae039b57fd7c', 'audio.h': 'cbc32c1bbdd7c48b16fdce68f7b7a1216354c3a8e2fc7872a9806abcbb0b62b9', 'audio_shiri.c': '199ea0575b5d83f11901758b043d36fc8b31e6b7d21c50564da49cef4dd4f1a9', 'common.c': 'af137155cbfc30c7cdbe6d7922870f896cb1e8ce7545438951c991e91575b3b0', 'player.c': '7d65933ac8a99f62984daa5a67ea9a12943a2f569c60b7a81029c91366ddcb93', 'player.h': '4e4a36d9265b555f3e96e293250ffb22e4b5e4af4ceeefdc3b9652184778ebf7', 'shiri_ap2_volume.c': 'cbe19623dabb91cb4d3f27f90ce48e8800a6927860777c7c0c6a4baffa66af36', 'shiri_volume_control.h': 'c0760aa336bebda5ab157a43e4a23731f234558af8db25efdfea26faf31674d8', 'shiri_volume_io.h': 'e69445ee32c33b1df3fab7bada327e62b51bfe2b71319cfe9600ea135e9bf62b', 'shiri_volume_lane.inc': '85db01fdac5f57de20b32a2b679fe8835eaadef85f267dc89cf76529083d30f6'}


def verify_source(source):
    patch = Path(__file__).resolve().parents[2] / "install/patches/shairport-5.5.2-receiver-volume.patch"
    if hashlib.sha256(patch.read_bytes()).hexdigest() != PATCH_SHA:
        raise ValueError("Receiver volume patch digest differs from the reviewed layer")
    for name, digest in BACKEND_HASHES.items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != digest:
            raise ValueError("Actual receiver volume source differs: " + name)
    return patch


def preimage_audio(source):
    # Keep the existing exact timed3 tests on their unchanged PCM callback.
    # The complete added control source is independently sanitized below.
    # Backout succeeds only for the verified complete overlay and exact files.
    patch = verify_source(source)
    with tempfile.TemporaryDirectory(prefix="shiri-volume-clock-preimage-") as directory:
        build = Path(directory)
        for name in BACKEND_HASHES:
            shutil.copyfile(source / name, build / name)
        subprocess.run(["git", "apply", "--reverse", "--check", str(patch)], cwd=build, check=True,
                       capture_output=True, text=True)
        subprocess.run(["git", "apply", "--reverse", str(patch)], cwd=build, check=True,
                       capture_output=True, text=True)
        return (build / "audio_shiri.c").read_text()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--compiler", default="cc")
    parser.add_argument("--sanitize", action="store_true")
    args = parser.parse_args()
    verify_source(args.source)
    fixture = Path(__file__).parent
    with tempfile.TemporaryDirectory(prefix="shiri-volume-native-") as directory:
        build = Path(directory)
        for name in ("shiri_ap2_volume.c", "shiri_volume_io.h", "shiri_volume_control.h", "shiri_pcm.h", "shiri_volume_lane.inc"):
            shutil.copyfile(args.source / name, build / name)
        for name in ("test_receiver_volume.c", "test_receiver_volume_lane.c", "receiver_volume_stubs.h"):
            shutil.copyfile(fixture / name, build / name)
        for name in ("common.h", "player.h", "rtsp.h"):
            (build / name).write_text('#include "receiver_volume_stubs.h"\n')
        (build / "config.h").write_text("#define CONFIG_AIRPLAY_2 1\n")
        command = [args.compiler, "-std=c11", "-D_GNU_SOURCE", "-DMSG_NOSIGNAL=0", "-g", "-O1", "-Wall", "-Wextra", "-Werror", "-Wno-unused-function", "-pthread", "-I", str(build)]
        if args.sanitize:
            command += ["-fsanitize=address,undefined", "-fno-omit-frame-pointer"]
        for name in ("test_receiver_volume.c", "test_receiver_volume_lane.c"):
            subprocess.run([*command, str(build / name), "-o", str(build / "test")], check=True)
            subprocess.run([str(build / "test")], check=True, timeout=10)


if __name__ == "__main__":
    main()
