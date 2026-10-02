#!/usr/bin/env python3
"""Validate exact bed bodies and undo only pinned reviewed layers to old owner guards.

No source checkout is changed. Immutable historical owner validation remains
strict: the reviewed bed and transition patches are reversed privately, then
all original readiness/retirement/media/auth guards must match byte for byte.
"""

from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "install/patches/owntone-29.3-paused-speech.patch"
TRANSITION = ROOT / "install/patches/owntone-29.3-native-transition-timer.patch"
PATCH_SHA = "eb2f9ceb0e58f3c92d82c682cd177b98d3b0a48d4848aa5b430f3761703da928"
TRANSITION_SHA = "912922fb7853d25fb031d0258effeb33f3332a971b84f01e01c79014a194e8a7"
VERSION = "29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-resample1-framed1-alsa1-speech1-ready1-anchor1-jitter1-owner1-balance1-transition1-bed1"
READY_JSON_SHA = "adc4b3ffb250d14c652ae4de9cb1872220a6cbcc6437663793f5dd466cd50140"
FUNCTION_SHA = {
    "playback_cb": "ea4865c41ec4d6b3aeef5966370427ffb69df8ecb2cbfe33547c7f43e67f8d49",
    "pb_timer_native_prepare": "12f185dd08ebb3293ba5955070323d98bc2f4467c713773292daa5ac1a852407",
    "pb_timer_start": "f7e628d3e31cfd37e6f1100a4ad0b8f3a4150abeb8213cef74335fe5b0c7a684",
    "pb_timer_stop": "eaa9c169b641ee619e5b092854cb5f5cbdaaec14777023bd04759bea8a6de2d4",
    "shiri_speech_schedule_idle_stop": "899aea2a2364776921e5c77a9786ff191a7923ad4bb8b75c4252d2310f71f9c9",
    "shiri_speech_prepare": "b566550a4ee00741e0f615caf617d5dacf106fbfa5546a9789da97aab46d0993",
    "shiri_speech_prepare_bh": "80b24bc8616ce09f0fa5f2076fdfd5f32ffea8e3c3dccb14a4b3ac5b3744edee",
    "shiri_speech_mix_ready": "54dde5761e1c774918cbf3253e2941e0ebc1659ea98cceeb4bfc153f96bfac34",
    "shiri_speech_ready_check": "2ff363422edace8da83b3e564bb2903a5aa1827aca10dfc885627cb87abbf06e",
    "shiri_speech_bed_wanted": "6580d02bfcfdede1b0df3de1b1c38c83ee87708c141eb1e34718610b90572071",
    "shiri_speech_bed_retire_outputs": "1f84ff97c13bf73ab52a64ac2ee233fd9280a7eb2a46f73e19e397fbf930405d",
    "shiri_speech_bed_arm": "95ec4ce781d0cd0a8c8418ed043bf0d0c1e0d4b07a4cc6bbe79217a56a6ae6dc",
    "shiri_speech_bed_tick": "17e2eb44f9e3b6163aba7b32fad2f92ba1e914b611a6233de07e5e3db3b01640",
    "shiri_speech_voice_command": "14d4e7464f10148f0adcf369f80d5dfdab6830a8e5871806341af2c9c5d0b7cd",
    "shiri_source_seal_flush": "bf4d340cc73224d52b9812269ff5f987fa891116562164353290299974d397e2",
    "shiri_source_arm": "562ed3965770ebf667d92d808ac0d8996ab673b49756d1c4e355cc96c88d5892",
}


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tests/native" / filename)
    assert spec and spec.loader
    out = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(out)
    return out


def check(source):
    source = source.resolve(strict=True)
    for patch, pin in ((PATCH, PATCH_SHA), (TRANSITION, TRANSITION_SHA)):
        if hashlib.sha256(patch.read_bytes()).hexdigest() != pin:
            raise ValueError("Unreviewed composition patch: " + patch.name)
    if re.findall(r"^AC_INIT\(\[owntone\], \[([^]]+)\]", (source / "configure.ac").read_text(), re.M) != [
        VERSION
    ]:
        raise ValueError("Exact transition1-bed1 source marker required")
    extract = module("bed_guard_extract", "check_source_transition.py")
    player = (source / "src/player.c").read_text()
    for name, pin in FUNCTION_SHA.items():
        if hashlib.sha256(extract.function(player, name).encode()).hexdigest() != pin:
            raise ValueError("Unreviewed composed player body: " + name)
    if hashlib.sha256((source / "src/shiri_speech_ready_json.h").read_bytes()).hexdigest() != READY_JSON_SHA:
        raise ValueError("Unreviewed composed speech JSON schema")
    owner = module("bed_owner_guards", "check_speech_owner.py")
    owner.validate_patch()
    with tempfile.TemporaryDirectory(prefix="shiri-bed-strict-backout-") as temporary:
        directory = Path(temporary)
        inverse = directory / "inverse"
        inverse.mkdir()
        shutil.copytree(
            source / "src",
            inverse / "src",
            symlinks=True,
            ignore=shutil.ignore_patterns("*.o", "*.lo", ".libs", ".deps"),
        )
        shutil.copy2(source / "configure.ac", inverse / "configure.ac")
        for patch in (PATCH, TRANSITION):
            for options in (["--check"], []):
                subprocess.run(
                    ["git", "apply", "--reverse", *options, str(patch)],
                    cwd=inverse,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
        assembled = owner.assemble(directory / "historical-owner")
        owner.validate_source(inverse, assembled)
    return {
        "ok": True,
        "exact_player_bodies": len(FUNCTION_SHA),
        "exact_speech_json": True,
        "strict_inverse_bed_transition": True,
        "immutable_historical_owner_ready_media_auth_guards": True,
        "patch_sha256": PATCH_SHA,
        "transition_sha256": TRANSITION_SHA,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(check(args.source), indent=2))
