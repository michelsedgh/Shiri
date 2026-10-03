#!/usr/bin/env python3
"""Silent Linux test: actual OwnTone conversion and AirPlay ALAC packetizer.

Input is the captured exact-player output-bed corpus from check_speech_drain.
No socket/audio device is opened. Only RTP/session send boundaries are local;
the output conversion, ALAC encoding, packetization and FFmpeg decoding are real.
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

HERE = Path(__file__).resolve().parent
PACKAGES = ["libavcodec", "libavformat", "libavfilter", "libavutil", "libswresample", "libevent", "libconfuse", "libcurl"]


def run(source: Path, capture: Path, *, compiler=None):
    source = source.resolve(strict=True)
    capture = capture.resolve(strict=True)
    spec = importlib.util.spec_from_file_location("packetizer_bodies", HERE / "check_paused_speech.py")
    assert spec and spec.loader
    extract = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(extract)
    airplay = (source / "src/outputs/airplay.c").read_text()
    bodies = "\n".join(extract.body(airplay, name) for name in
                       ["alac_encode", "packets_send", "timestamp_set", "packets_sync_send", "airplay_write"])
    compiler = compiler or shutil.which("cc")
    if not compiler:
        raise ValueError("A compiler is required")
    flags = subprocess.run(["pkg-config", "--cflags", "--libs", *PACKAGES], check=True,
                           capture_output=True, text=True, timeout=10)
    with tempfile.TemporaryDirectory(prefix="shiri-speech-alac-") as temporary:
        directory = Path(temporary)
        (directory / "actual_airplay.inc").write_text(bodies)
        program = directory / "packetizer"
        command = [compiler, "-std=gnu11", "-D_GNU_SOURCE", "-DHAVE_CONFIG_H", "-ffunction-sections", "-fdata-sections",
                   "-O1", "-g", "-I", str(directory), "-I", str(source), "-I", str(source / "src"),
                   str(HERE / "test_speech_packetizer.c"), str(source / "src/transcode.c"),
                   "-Wl,--gc-sections", "-o", str(program), *shlex.split(flags.stdout), "-lm",
                   "-fsanitize=address,undefined", "-fno-sanitize-recover=all"]
        built = subprocess.run(command, capture_output=True, text=True, timeout=60)
        if built.returncode:
            raise RuntimeError(built.stderr)
        result = subprocess.run([str(program), str(capture)], capture_output=True, text=True, timeout=30,
                                env=dict(os.environ, ASAN_OPTIONS="detect_leaks=0:halt_on_error=1",
                                         UBSAN_OPTIONS="halt_on_error=1"))
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
    names = ["src/outputs.c", "src/transcode.c", "src/outputs/airplay.c"]
    return {"ok": True, "sanitized": True, "no_socket_or_audio_device": True,
            "actual_full_outputs_transcode": True, "actual_airplay_packetizer_bodies": True,
            "real_ffmpeg_alac_encode_decode": True, "physical_playback_proof": False,
            "source_hashes": {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in names},
            "captured_player_pcm_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
            "results": result.stdout.strip().splitlines()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--compiler")
    args = parser.parse_args()
    print(json.dumps(run(args.source, args.capture, compiler=args.compiler), indent=2))
