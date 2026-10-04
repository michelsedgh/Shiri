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
CODEC_SOURCE_SHA = {
    "src/outputs.c": "08df3c6e3e451bc9e5190dd367e6c58402945a5827495ffd9dfbe5e596a1cb9d",
    "src/transcode.c": "68441cfdab2e0862349d499cafa8fdf419d93562534e90c0955e92ed31398d13",
    "src/outputs/airplay.c": "405243e11ea3f24301f449346b359d81134ae20ff75c5389c491e776f1dcc046",
}


def run(source: Path, capture: Path, *, compiler=None, preimage=False):
    source = source.resolve(strict=True)
    capture = capture.resolve(strict=True)
    configure = (source / "configure.ac").read_text()
    warm_lease = "-coldmusic1-outputclock1-duck1-warm1])" in configure
    duck_envelope = warm_lease or "-coldmusic1-outputclock1-duck1])" in configure
    output_clock = duck_envelope or "-coldmusic1-outputclock1])" in configure
    guard_name = "check_startup_metadata.py" if preimage else (
        "check_warm_lease.py" if warm_lease else
        "check_duck_envelope.py" if duck_envelope else
        "check_output_clock.py" if output_clock else "check_cold_music.py"
    )
    guard_spec = importlib.util.spec_from_file_location("packetizer_composed_source", HERE / guard_name)
    assert guard_spec and guard_spec.loader
    guard = importlib.util.module_from_spec(guard_spec)
    guard_spec.loader.exec_module(guard)
    historical = guard.verify_source(source)
    for name, expected in CODEC_SOURCE_SHA.items():
        if preimage and name == "src/outputs/airplay.c":
            expected = guard.AIRPLAY_SHA
        elif warm_lease and name in {"src/outputs.c", "src/outputs/airplay.c"}:
            expected = guard.SOURCE_SHA[name]
        elif output_clock and name == "src/outputs/airplay.c":
            # Additive envelope/config layers are strictly inverted by their
            # guards before the immutable coldmusic1 codec guard is evaluated.
            expected = guard.SOURCE_SHA[name]
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != expected:
            raise ValueError("Unreviewed packetizer codec source: " + name)
    spec = importlib.util.spec_from_file_location("packetizer_bodies", HERE / "check_paused_speech.py")
    assert spec and spec.loader
    extract = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(extract)
    airplay = (source / "src/outputs/airplay.c").read_text()
    bodies = "\n".join(extract.body(airplay, name) for name in
                       ["alac_encode", "packets_send", "timestamp_set", *([] if preimage else ["airplay_shiri_input_timestamp"]), "packets_sync_send", "airplay_write"])
    rtp = (source / "src/outputs/rtp_common.c").read_text()
    cadence = "\n".join(extract.body(rtp, name) for name in ["rtp_packet_commit", "rtp_sync_is_time"])
    misc = (source / "src/misc.c").read_text()
    timing = "\n".join(extract.body(misc, name) for name in ["timespec_add", "timespec_cmp"])
    compiler = compiler or shutil.which("cc")
    if not compiler:
        raise ValueError("A compiler is required")
    flags = subprocess.run(["pkg-config", "--cflags", "--libs", *PACKAGES], check=True,
                           capture_output=True, text=True, timeout=10)
    with tempfile.TemporaryDirectory(prefix="shiri-speech-alac-") as temporary:
        directory = Path(temporary)
        (directory / "actual_airplay.inc").write_text(bodies)
        (directory / "actual_rtp.inc").write_text(cadence)
        (directory / "actual_timing.inc").write_text(timing)
        program = directory / "packetizer"
        command = [compiler, "-std=gnu11", "-D_GNU_SOURCE", "-DHAVE_CONFIG_H", "-ffunction-sections", "-fdata-sections",
                   "-O1", "-g", *(["-DPREIMAGE=1"] if preimage else []), "-I", str(directory), "-I", str(source), "-I", str(source / "src"),
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
            "preimage_gap_failure_reproduced": preimage,
            "actual_full_outputs_transcode": True, "actual_airplay_packetizer_bodies": True,
            "real_ffmpeg_alac_encode_decode": True, "physical_playback_proof": False,
            "composed_source_guards": historical,
            "source_hashes": {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in names},
            "captured_player_pcm_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
            "results": result.stdout.strip().splitlines()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--compiler")
    parser.add_argument("--preimage", type=Path, help="Exact startupmeta1 source to reproduce the missing resumed sync")
    args = parser.parse_args()
    result = run(args.source, args.capture, compiler=args.compiler)
    if args.preimage:
        result["preimage"] = run(args.preimage, args.capture, compiler=args.compiler, preimage=True)
    print(json.dumps(result, indent=2))
