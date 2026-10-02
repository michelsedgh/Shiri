#!/usr/bin/env python3
"""Check the exact event1 source; Linux mode uses actual libplist/libevent."""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[1]
PATCH = ROOT / "install/patches/owntone-29.3-event-ack.patch"
PATCH_SHA = "08ead94d976619985445e8177756ee08be82d50a6b03432faadcdd281fbc3f73"
VERSION = "29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-resample1-framed1-alsa1-speech1-ready1-anchor1-jitter1-owner1-balance1-transition1-bed1-event1"
PLAYER_SHA = "8d85e2010e6b0402ebddbdb61d965663e8cc333bf5accf587edcb4d9b1585613"
PAIR_SOURCE_SHA = {
    "src/pair_ap/pair-internal.h": "9f1dafb2792db2c18a1347cf0e8fb64dc3ed6f12037bd8251d4daac5d7219dd1",
    "src/pair_ap/pair-tlv.c": "32c3a77e6244dc44338e866f950a42bbd379ba4479c88b8e1343071db9e47025",
    "src/pair_ap/pair-tlv.h": "b4565d5d0e2e2ad016756d827d2652893602e022889877d229d85373c7357fcd",
    "src/pair_ap/pair.c": "1bf53b7ef14c3c8ab8dbb0a9edef5128bfa8888f1dc7b0db11c0307b8d8d0029",
    "src/pair_ap/pair.h": "74e9b76234cd88d3ac5f81c643ec132032643b6a40b30e00c2fb719fc5e6a607",
    "src/pair_ap/pair_fruit.c": "d37ab36f63015613ea7fa4d82e74a62c4cfd32356d1eb5e0e93e44f23baa73d0",
    "src/pair_ap/pair_homekit.c": "51c94f98620712bfadbcb0cf26b8231fc005c5379bf0cb0134053870b0e9ec46",
}
PREIMAGE_EVENT_SHA = "38d3dce1d5a7b445c81c4453f063e4e3893e5723b864936ee84ac0ab73a58944"
SOURCE_SHA = "ba3dd21cd7ef1eb50ad403d1f04e742c84593043bdcfd4bac35602d54ab64815"
FIXTURE_SHA = "02a899938d974cb1804d6b85f266470ba15e8327a37089e308b953b577718108"


def function(source, name):
    match = re.search(r"\nstatic (?:int|void)\n" + name + r"\(", source)
    if match is None:
        raise ValueError(name)
    begin = source.index("{", match.end())
    level, end = 1, begin + 1
    while level:
        level += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[match.start() + 1 : end]


def verify_source(source):
    source = source.resolve(strict=True)
    if hashlib.sha256(PATCH.read_bytes()).hexdigest() != PATCH_SHA:
        raise ValueError("Unreviewed event1 composition patch")
    if re.findall(r"^AC_INIT\(\[owntone\], \[([^]]+)\]", (source / "configure.ac").read_text(), re.M) != [
        VERSION
    ]:
        raise ValueError("Exact bed1-event1 marker required")
    if hashlib.sha256((source / "src/player.c").read_bytes()).hexdigest() != PLAYER_SHA:
        raise ValueError("Prior complete player/gain/timing source changed")
    if hashlib.sha256((source / "src/outputs/airplay_events.c").read_bytes()).hexdigest() != SOURCE_SHA:
        raise ValueError("Exact event1 source hash differs")
    # Undo only the complete reviewed event layer privately. Historical bed,
    # transition and owner readiness/media/auth validators remain unchanged.
    with tempfile.TemporaryDirectory(prefix="shiri-event1-strict-backout-") as temporary:
        inverse = Path(temporary) / "inverse"
        inverse.mkdir()
        shutil.copytree(
            source / "src",
            inverse / "src",
            symlinks=True,
            ignore=shutil.ignore_patterns("*.o", "*.lo", ".libs", ".deps"),
        )
        shutil.copyfile(source / "configure.ac", inverse / "configure.ac")
        for options in (["--check"], []):
            subprocess.run(
                ["git", "apply", "--reverse", *options, str(PATCH)],
                cwd=inverse,
                check=True,
                capture_output=True,
                text=True,
                timeout=20,
            )
        if (
            hashlib.sha256((inverse / "src/outputs/airplay_events.c").read_bytes()).hexdigest()
            != PREIMAGE_EVENT_SHA
        ):
            raise ValueError("Exact historical events preimage changed")
        spec = importlib.util.spec_from_file_location(
            "event1_historical_bed_guard", BASE / "check_paused_speech_guards.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        prior = module.check(inverse)
    return {
        "ok": True,
        "event_patch_sha256": PATCH_SHA,
        "event_source_sha256": SOURCE_SHA,
        "player_source_unchanged": True,
        "strict_inverse_event1": True,
        "unchanged_bed_transition_owner_guards": prior,
    }


def compile_pair_objects(command, build, crypto_cflags):
    """Keep untouched pinned library objects under their production warning policy."""
    production_command = [
        argument
        for argument in command
        if argument not in {"-Wextra", "-Werror", "-Wno-unused-function", "-Wno-unused-parameter"}
    ]
    objects = []
    for name in ("pair.c", "pair_homekit.c", "pair_fruit.c", "pair-tlv.c"):
        source = build / "pair_ap" / name
        output = build / (name + ".o")
        subprocess.run(
            [*production_command, "-DCONFIG_GCRYPT=1", *crypto_cflags, "-c", str(source), "-o", str(output)],
            check=True,
        )
        objects.append(str(output))
    return objects


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--compiler", default="cc")
    parser.add_argument("--sanitize", action="store_true")
    parser.add_argument(
        "--verify-only", action="store_true", help="strict layer/backout validation without compilation"
    )
    parser.add_argument(
        "--framing-only",
        action="store_true",
        help="portable bounded framing only; no plist/socket qualification",
    )
    args = parser.parse_args()
    validation = verify_source(args.source)
    if args.verify_only:
        print(json.dumps(validation, sort_keys=True))
        return
    src = args.source / "src/outputs/airplay_events.c"
    source = src.read_text()
    if hashlib.sha256(src.read_bytes()).hexdigest() != SOURCE_SHA:
        raise ValueError("Exact event1 source hash differs")
    fixture = BASE / "airplay_updateInfo128.rtsp"
    if hashlib.sha256(fixture.read_bytes()).hexdigest() != FIXTURE_SHA:
        raise ValueError("Retained network128 fixture hash differs")
    with tempfile.TemporaryDirectory(prefix="shiri-event1-test-") as directory:
        build = Path(directory)
        command = [
            args.compiler,
            "-std=c11",
            "-D_POSIX_C_SOURCE=200809L",
            "-g",
            "-O1",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-Wno-unused-function",
            "-Wno-unused-parameter",
            "-pthread",
            "-I",
            str(build),
        ]
        if args.sanitize:
            command += ["-fsanitize=address,undefined", "-fno-omit-frame-pointer"]
        if args.framing_only:
            definitions = source[source.index("#define RTSP_VERSION") : source.index("enum airplay_events")]
            structure = source[source.index("struct rtsp_message") : source.index("static pthread_t")]
            prefix = "#include <assert.h>\n#include <stdbool.h>\n#include <stdint.h>\n#include <stdio.h>\n#include <stdlib.h>\n#include <string.h>\n#include <strings.h>\n#include <limits.h>\n"
            native = (
                prefix
                + definitions
                + structure
                + function(source, "rtsp_uint")
                + "\n"
                + function(source, "rtsp_frame_parse")
            )
            (build / "test.c").write_text(native + "\n" + (BASE / "test_event_framing.c").read_text())
            subprocess.run([*command, str(build / "test.c"), "-o", str(build / "test")], check=True)
        else:
            flags = subprocess.run(
                ["pkg-config", "--cflags", "--libs", "libplist-2.0", "libevent"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.split()
            shutil.copyfile(src, build / "airplay_events.c")
            shutil.copyfile(BASE / "test_airplay_events.c", build / "test.c")
            (build / "airplay_events.h").write_text("void airplay_events_deinit(void);\n")
            (build / "logger.h").write_text(
                '#define DPRINTF(...) ((void)0)\n#define DHEXDUMP(...) ((void)0)\n#define PACKAGE_NAME "owntone-test"\n'
            )
            (build / "misc.h").write_text(
                "#define CHECK_NULL(category, expr) do { if (!(expr)) abort(); } while(0)\nstatic void thread_setname(const char *name) {(void)name;}\nstatic int net_connect(const char *a,unsigned short p,int t,const char *n) {(void)a;(void)p;(void)t;(void)n;return -1;}\n"
            )
            (build / "commands.h").write_text(
                "struct commands_base { int unused; };\nstatic struct commands_base *commands_base_new(struct event_base *b,void *p) {(void)b;(void)p;return calloc(1,sizeof(struct commands_base));}\nstatic void commands_base_destroy(struct commands_base *b) {free(b);}\n"
            )
            (build / "player.h").write_text(
                "struct player_status { int status; };\n#define PLAY_PLAYING 4\nvoid player_get_status(struct player_status *s);\nvoid player_playback_pause(void);\nvoid player_playback_start(void);\nvoid player_playback_next(void);\nvoid player_playback_prev(void);\n"
            )
            (build / "pair_ap").mkdir()
            (build / "pair_ap/pair.h").write_text(
                "struct pair_cipher_context { int unused; };\n#define PAIR_CLIENT_HOMEKIT_NORMAL 1\nstruct pair_cipher_context *pair_cipher_new(int,int,const uint8_t *,size_t);\nvoid pair_cipher_free(struct pair_cipher_context *);\nconst char *pair_cipher_errmsg(struct pair_cipher_context *);\nssize_t pair_decrypt(uint8_t **,size_t *,const uint8_t *,size_t,struct pair_cipher_context *);\nint pair_encrypt(uint8_t **,size_t *,const uint8_t *,size_t,struct pair_cipher_context *);\n"
            )
            subprocess.run([*command, str(build / "test.c"), "-o", str(build / "test"), *flags], check=True)
            for name, pin in PAIR_SOURCE_SHA.items():
                if hashlib.sha256((args.source / name).read_bytes()).hexdigest() != pin:
                    raise ValueError("Pinned pairing library bytes changed: " + name)
                shutil.copyfile(args.source / name, build / "pair_ap" / Path(name).name)
            shutil.copyfile(BASE / "test_airplay_events_crypto.c", build / "crypto.c")
            crypto_flags = subprocess.run(
                ["pkg-config", "--cflags", "--libs", "libgcrypt", "libsodium"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.split()
            crypto_cflags = subprocess.run(
                ["pkg-config", "--cflags", "libgcrypt", "libsodium"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.split()
            pair_objects = compile_pair_objects(command, build, crypto_cflags)
            subprocess.run(
                [
                    *command,
                    "-DCONFIG_GCRYPT=1",
                    str(build / "crypto.c"),
                    *pair_objects,
                    "-o",
                    str(build / "crypto"),
                    *flags,
                    *crypto_flags,
                ],
                check=True,
            )
            subprocess.run([str(build / "crypto"), str(fixture)], check=True, timeout=30)

        subprocess.run([str(build / "test"), str(fixture)], check=True, timeout=30)


if __name__ == "__main__":
    main()
