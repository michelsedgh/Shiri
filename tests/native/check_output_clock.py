#!/usr/bin/env python3
"""Execute reviewed OwnTone timing config, discovery and serialization bodies.

No service, socket, model or audio device is opened. The optional Linux parser
fixture uses real libconfuse. Historical source layers are inverted exactly.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PATCH = ROOT / "install/patches/owntone-29.3-output-clock.patch"
PATCH_SHA = "842b21bb969af4cf839e0254679cd57266fc85d9ef8c2d77e1a9cfe44ec73d94"
SOURCE_SHA = {
    "configure.ac": "434c8dc415859470f3797ab60379ed92df614380bb111db1c7d894d47754801a",
    "src/conffile.c": "1c695931993f735ba804c831ccec030879e1c2d24009a61ffe448b437e057723",
    "src/outputs/airplay.c": "e6cd3a395548b9c6cf431424ae2fa76c58b347e83626041c982b418a4b84dac2",
    "src/outputs.h": "44632caeffd43cb98741464f5a1dd5c501d5c080717f0b3c2a81a687c1518336",
    "src/player.h": "6a9b93308ab2251eb54cf257fcfb3f85ec612a593fb53f860e732f5287d9ff0f",
    "src/player.c": "ff5b250602d61e0ec2b837b3a3019c6c2547bd0c9fb86a31eaafac6691f26f53",
    "src/httpd_jsonapi.c": "14d5207bb8e1651915e6c4dd5ecd837c03e0fb06e650a5c5c8565c2df79d4e65",
}
VERSION = "29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-resample1-framed1-alsa1-speech1-ready1-anchor1-jitter1-owner1-balance1-transition1-bed1-event1-idle1-drain1-startupmeta1-coldmusic1-outputclock1"


def module(name):
    spec = importlib.util.spec_from_file_location("output_clock_" + name, HERE / (name + ".py"))
    assert spec and spec.loader
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def verify_source(source: Path):
    if hashlib.sha256(PATCH.read_bytes()).hexdigest() != PATCH_SHA:
        raise ValueError("Unreviewed output timing patch")
    for name, expected in SOURCE_SHA.items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != expected:
            raise ValueError("Unreviewed output timing source: " + name)
    if f"AC_INIT([owntone], [{VERSION}])" not in (source / "configure.ac").read_text():
        raise ValueError("Exact outputclock1 marker required")
    with tempfile.TemporaryDirectory(prefix="shiri-output-clock-inverse-") as temporary:
        inverse = Path(temporary) / "inverse"
        shutil.copytree(source / "src", inverse / "src", symlinks=True,
                        ignore=shutil.ignore_patterns("*.o", "*.lo", ".libs", ".deps"))
        shutil.copyfile(source / "configure.ac", inverse / "configure.ac")
        for options in (["--check"], []):
            subprocess.run(["git", "apply", "--reverse", *options, str(PATCH)], cwd=inverse,
                           check=True, capture_output=True, text=True, timeout=20)
        historical = module("check_cold_music").verify_source(inverse)
    return {"strict_inverse_outputclock1": True, "unchanged_coldmusic_and_historical_guards": historical}


def declarations(text, name, kind="struct"):
    value = re.search(r"(?:static const )?" + kind + " " + name + r"\s*\{.*?\n\};", text, re.S)
    if not value:
        raise ValueError("Missing exact declaration: " + name)
    return value.group(0)


def check(source: Path, *, compiler=None, real_confuse=False):
    source = source.resolve(strict=True)
    historical = verify_source(source)
    extract = module("check_paused_speech").body
    config = (source / "src/conffile.c").read_text()
    airplay = (source / "src/outputs/airplay.c").read_text()
    schema = re.search(r"static cfg_opt_t sec_shiri_airplay_timing\[\].*?\n  \};", config, re.S)
    section = re.search(r'    CFG_SEC\("shiri_airplay_timing",.*?CFGF_NO_TITLE_DUPES\),', config, re.S)
    features = re.search(r"struct features_type_map.*?static const struct features_type_map features_map\[\].*?\n  \};", airplay, re.S)
    if not schema or not section or not features:
        raise ValueError("Exact timing schema and duplicate-title guard required")
    load = extract(config, "conffile_load")
    if load.index("shiri_airplay_timing_validate(cfg)") > load.index("getpwnam(runas)"):
        raise ValueError("Timing validation must precede runtime setup")
    if "session->master_session = master_session_make(&device->quality, extra->use_ptp);" not in airplay:
        raise ValueError("Effective timing must reach the original master session")
    replacements = {
        "ACTUAL_AIRPLAY_ENUM": declarations(airplay, "airplay_devtype", "enum"),
        "ACTUAL_AIRPLAY_EXTRA": declarations(airplay, "airplay_extra"),
        "ACTUAL_OUTPUT_DEVICE": declarations((source / "src/outputs.h").read_text(), "output_device"),
        "ACTUAL_SPEAKER_INFO": declarations((source / "src/player.h").read_text(), "player_speaker_info"),
        "ACTUAL_FEATURES_MAP": features.group(0),
        "ACTUAL_CONFIG_VALIDATOR": extract(config, "shiri_airplay_timing_validate"),
        "ACTUAL_DEVICE_ID_PARSE": extract(airplay, "device_id_colon_parse"),
        "ACTUAL_FEATURES_PARSE": extract(airplay, "features_parse"),
        "ACTUAL_TIMING_SELECTOR": extract(airplay, "airplay_shiri_timing_select"),
        "ACTUAL_DISCOVERY_CALLBACK": extract(airplay, "airplay_device_cb"),
        "ACTUAL_DEVICE_INFO_COPY": extract((source / "src/player.c").read_text(), "device_to_speaker_info"),
        "ACTUAL_JSON_SERIALIZATION": extract((source / "src/httpd_jsonapi.c").read_text(), "speaker_to_json"),
        "ACTUAL_SETUP_DISPATCH": extract(airplay, "payload_make_setup_session"),
        "ACTUAL_TIMING_SCHEMA": schema.group(0),
        "ACTUAL_TIMING_SECTION": section.group(0),
    }
    compiler = compiler or shutil.which("clang") or shutil.which("cc")
    if not compiler:
        raise ValueError("A C compiler is required")
    results = []
    with tempfile.TemporaryDirectory(prefix="shiri-output-clock-fixture-") as temporary:
        for name in ["test_output_clock"] + (["test_output_clock_confuse"] if real_confuse else []):
            text = (HERE / (name + ".c")).read_text()
            for key, value in replacements.items():
                text = text.replace("/* @" + key + "@ */", value)
            if "/* @ACTUAL_" in text:
                raise ValueError("Unexpanded actual source body")
            directory = Path(temporary)
            unit = directory / (name + ".c")
            unit.write_text(text)
            binary = directory / name
            flags = []
            if name.endswith("_confuse"):
                configured = subprocess.run(["pkg-config", "--cflags", "--libs", "libconfuse"], check=True,
                                            capture_output=True, text=True, timeout=10)
                flags = shlex.split(configured.stdout)
            command = [compiler, "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter",
                       "-Wno-unused-function", "-Wno-unused-variable", "-Wno-sign-compare", "-O1", "-g",
                       "-fsanitize=address,undefined", "-fno-sanitize-recover=all", str(unit), "-o", str(binary), *flags]
            built = subprocess.run(command, capture_output=True, text=True, timeout=60)
            if built.returncode:
                raise RuntimeError(built.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                                    env=dict(os.environ, ASAN_OPTIONS="detect_leaks=0:halt_on_error=1",
                                             UBSAN_OPTIONS="halt_on_error=1"))
            if result.returncode:
                raise RuntimeError(result.stdout + result.stderr)
            results.append({"fixture": name, "stdout": result.stdout.strip().splitlines(),
                            "stderr": result.stderr.strip().splitlines()})
    return {"ok": True, "sanitized": True, "no_socket_or_audio_device": True,
            "real_libconfuse_parser": real_confuse, "stable_uint64_selector": True,
            "actual_discovery_features_and_json": True, "physical_playback_proof": False,
            "patch_sha256": PATCH_SHA, "source_hashes": SOURCE_SHA,
            "composed_source_guards": historical, "results": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--compiler")
    parser.add_argument("--real-confuse", action="store_true")
    args = parser.parse_args()
    print(json.dumps(check(args.source, compiler=args.compiler, real_confuse=args.real_confuse), indent=2))
