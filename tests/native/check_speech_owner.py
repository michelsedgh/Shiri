"""Execute the additive single-voice owner using exact maintained C bytes.

No device or network listener is opened. The original fourteen patches remain
immutable; the new layer admits only version2/96byte speech datagrams.
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
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "install/patches/owntone-29.3-speech-owner.patch"
VERSION = "29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-resample1-framed1-alsa1-speech1-ready1-anchor1-jitter1-owner1"
MEDIA_FILES = ("shiri_speech.c", "shiri_speech.h", "shiri_speech_ready.h", "shiri_speech_ready_json.h")
PREIMAGES = {
    "shiri_speech.c": "b763fea0ea130be2ed21cfa7bd0f36db562d24bcda7384a7295227c4d29ffa3f",
    "shiri_speech.h": "fd69cafec1e2802c7c6aa21326631d6e996385a06ab65f1d21ba6b857151c3de",
    "shiri_speech_ready.h": "295abbd9629f85b86499af46c7edd191df985b988f6650afe862114bd56cfc02",
    "shiri_speech_ready_json.h": "c0b94ca2883cfa3c6a54d3837f666f99243b34f7a3c1af6da6e4097715d8f59d",
}
OWNER_FUNCTIONS = ("shiri_speech_voice_matches", "shiri_speech_voice_command", "player_shiri_speech_ready")
READY_FUNCTIONS = (
    "shiri_speech_ready_now", "shiri_speech_source_matches", "shiri_speech_request_matches",
    "shiri_speech_outputs_snapshot", "shiri_speech_reap_start_callbacks", "shiri_speech_schedule_idle_stop",
    "shiri_speech_bind_session", "shiri_speech_setup_event_clear", "shiri_speech_setup_timeout",
    "device_shiri_speech_start_cb", "shiri_speech_prepare", "shiri_speech_prepare_bh",
    "shiri_speech_last_outputs", "shiri_speech_mix_ready", "shiri_speech_ready_check",
)


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tests/native" / filename)
    assert spec and spec.loader
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def validate_patch(patch_path=PATCH):
    extract = module("owner_patch_extract", "check_source_transition.py")
    patch = patch_path.read_text()
    files = re.findall(r"^diff --git a/(\S+) b/\S+$", patch, re.MULTILINE)
    expected = {"configure.ac", "src/player.c", *("src/" + name for name in MEDIA_FILES)}
    if len(files) != len(expected) or set(files) != expected:
        raise ValueError("Voice ownership cannot change a source or output transport")
    configure = extract.postimage(patch_path, "configure.ac")
    if re.findall(r"^AC_INIT\(\[owntone\], \[([^]]+)\]", configure, re.MULTILINE) != [VERSION]:
        raise ValueError("Voice ownership requires the exact owner1 marker")


def assemble(directory, patch_path=PATCH):
    jitter = module("owner_jitter", "check_speech_jitter.py")
    jitter.patch_module(directory, jitter.PATCH)
    for name, digest in PREIMAGES.items():
        if hashlib.sha256((directory / "src" / name).read_bytes()).hexdigest() != digest:
            raise ValueError("Owner layer requires the exact fourteen-layer media preimage")
    includes = ["--include=src/" + name for name in MEDIA_FILES]
    for options in (["--check"], []):
        subprocess.run(["git", "apply", *options, *includes, str(patch_path)], cwd=directory,
                       check=True, capture_output=True, timeout=10)
    return directory


def validate_source(source, assembled, patch_path=PATCH):
    extract = module("owner_validate_extract", "check_source_transition.py")
    patch = patch_path.read_text()
    files = re.findall(r"^diff --git a/(\S+) b/\S+$", patch, re.MULTILINE)
    if set(files) != {"configure.ac", "src/player.c", *("src/" + name for name in MEDIA_FILES)}:
        raise ValueError("Voice ownership cannot change a source or output transport")
    configure = (source / "configure.ac").read_text()
    versions = re.findall(r"^AC_INIT\(\[owntone\], \[([^]]+)\]", configure, re.MULTILINE)
    if versions not in ([VERSION], [VERSION + "-balance1"]):
        raise ValueError("Voice ownership requires the exact owner1 marker")
    for name in MEDIA_FILES:
        if (source / "src" / name).read_bytes() != (assembled / "src" / name).read_bytes():
            raise ValueError("Actual owner module differs from the maintained additive patch")
    player = (source / "src/player.c").read_text()
    expected = extract.postimage(patch_path, "src/player.c")
    for name in OWNER_FUNCTIONS:
        if extract.function(player, name) != extract.function(expected, name):
            raise ValueError("Actual owner command differs from the maintained patch")
    ready = module("owner_validate_ready", "check_cold_speech_ready.py")
    previous = ready.postimage(ready.PATCH.read_text(), "src/player.c")
    for name in READY_FUNCTIONS:
        if extract.function(player, name) != extract.function(previous, name):
            raise ValueError("Owner layer changed an original readiness guard")
    callback = extract.function(player, "playback_cb")
    if not (callback.count("shiri_speech_poll();") == 1 and callback.count("shiri_speech_mix(") == 1
            and callback.index("shiri_speech_poll();") < callback.index("source_read(&nbytes")
            < callback.index("shiri_speech_mix(pb_session.buffer") < callback.index("outputs_write(pb_session.buffer")):
        raise ValueError("Voice ownership must retain the existing common player dispatch")
    http = (source / "src/httpd_jsonapi.c").read_text()
    if extract.function(http, "jsonapi_request").index("httpd_request_is_authorized(hreq)") > extract.function(http, "jsonapi_request").index("hreq->handler(hreq)"):
        raise ValueError("Voice owner actions cannot bypass OwnTone authentication")


def credential_scaffold():
    # The frozen ingress fixture retains every UID/FD/path/TTL/order guard.
    # Adapt only its explicit wire identity and admitted session setup.
    text = (ROOT / "tests/native/test_late_speech_socket.c").read_text()
    text = text.replace("1040", "1056")
    text = text.replace("p[8] = p[9] = 1; p[11] = 80;", "p[8] = 2; p[9] = 1; p[11] = 96; p[80] = 5;")
    text = text.replace("p[80 + i * 2]", "p[96 + i * 2]").replace("p[81 + i * 2]", "p[97 + i * 2]")
    text = text.replace("    ready = 'R'; write_exact(reply[1], &ready, 1);",
                        "    const uint8_t owner[16] = {5}; CHECK(shiri_speech_begin(owner) == 0);\n"
                        "    ready = 'R'; write_exact(reply[1], &ready, 1);")
    text = text.replace("  bytes = packet(data, 2); data[24] ^= 1;", 
                        "  bytes = packet(data, 2); data[80] ^= 1; send_packet(path, audio_uid, data, bytes, 0);\n"
                        "  after = observe(command[1], reply[0]); CHECK(after.sequence == 1 && after.sample == 0);\n"
                        "  bytes = packet(data, 2); data[24] ^= 1;")
    return text


def check(source=None, compiler=None, patch_path=PATCH, sanitize=True, credentials=False):
    validate_patch(patch_path)
    compiler = compiler or shutil.which("clang") or shutil.which("cc")
    if not compiler:
        raise RuntimeError("Actual owner proof requires a C compiler")
    extract = module("owner_extract", "check_source_transition.py")
    ready = module("owner_ready", "check_cold_speech_ready.py")
    flags = ["-std=gnu11", "-Wall", "-Wextra", "-Werror", "-O1"]
    if sanitize:
        flags += ["-fsanitize=address,undefined", "-fno-sanitize-recover=all"]
    outputs = []
    with tempfile.TemporaryDirectory(prefix="shiri-speech-owner-c-") as temporary:
        directory = Path(temporary)
        assembled = assemble(directory / "assembled", patch_path)
        actual = source.resolve() if source else assembled
        if source:
            validate_source(actual, assembled, patch_path)
        postimage = extract.postimage(patch_path, "src/player.c")
        player = (actual / "src/player.c").read_text() if source else ready.postimage(ready.PATCH.read_text(), "src/player.c")
        outputs_source = (actual / "src/outputs.c").read_text()
        state = extract.between(player, "/* Speech preparation owns", "\nstruct event_base *evbase_player;")
        functions = '#pragma GCC diagnostic push\n#pragma GCC diagnostic ignored "-Wsign-compare"\n'
        functions += "\n".join(extract.function(outputs_source, name) for name in ("callback_remove", "callback_add", "outputs_cb"))
        functions += "\n#pragma GCC diagnostic pop\n"
        functions += "\n".join(extract.function(outputs_source, name) for name in ("outputs_shiri_callback_pending", "outputs_shiri_stop_delayed_cancel"))
        functions += "\n".join(extract.function(player, name) for name in READY_FUNCTIONS)
        functions += "\nstatic struct shiri_speech_request shiri_voice_request;\nstatic bool shiri_voice_initialized;\n"
        functions += "\n".join(extract.function(player if source else postimage, name) for name in OWNER_FUNCTIONS)
        scaffold = (ROOT / "tests/native/test_cold_speech_ready.c").read_text()
        scaffold = scaffold.replace("/* @ACTUAL_STATE@ */", state).replace("/* @ACTUAL_FUNCTIONS@ */", functions)
        scaffold = scaffold.split("int main(void) {", 1)[0]
        scaffold = scaffold.replace("  memset(&q,0,sizeof(q));", "  shiri_voice_initialized=false;memset(&shiri_voice_request,0,sizeof(shiri_voice_request));\n  memset(&q,0,sizeof(q));")
        scaffold += (ROOT / "tests/native/test_speech_owner_player.c").read_text()
        (directory / "player.c").write_text(scaffold)
        json_flags = shlex.split(subprocess.run(["pkg-config", "--cflags", "--libs", "json-c"], check=True,
                                               capture_output=True, text=True, timeout=10).stdout)
        for name, test, extra in (
            ("media", ROOT / "tests/native/test_speech_owner.c", []),
            ("player", directory / "player.c", []),
            ("json", ROOT / "tests/native/test_speech_owner_json.c", json_flags),
        ):
            binary = directory / name
            subprocess.run([compiler, *flags, "-I" + str(actual / "src"), "-I" + str(ROOT / "tests/native"),
                            '-DSHIRI_SPEECH_SOURCE="' + str(actual / "src/shiri_speech.c") + '"',
                            str(test), *extra, "-lm", "-o", str(binary)], check=True, timeout=90)
            result = subprocess.run([str(binary)], check=True, capture_output=True, text=True, timeout=20)
            outputs.append(result.stdout.strip())
        if credentials:
            if sys.platform != "linux" or os.geteuid() != 0:
                raise RuntimeError("Credential proof requires Linux root and disposable synthetic UIDs")
            scaffold = directory / "credentials.c"
            scaffold.write_text(credential_scaffold())
            binary = directory / "credentials"
            subprocess.run([compiler, *flags, "-D_GNU_SOURCE",
                            '-DSHIRI_SPEECH_SOURCE="' + str(actual / "src/shiri_speech.c") + '"',
                            str(scaffold), "-lm", "-o", str(binary)], check=True, timeout=90)
            result = subprocess.run([str(binary)], check=True, capture_output=True, text=True, timeout=20)
            outputs.append(result.stdout.strip())
        return {"passed": True, "compiler": compiler, "sanitizers": sanitize,
                "source": str(source) if source else "exact maintained modules/extracted player commands",
                "patch_sha256": hashlib.sha256(patch_path.read_bytes()).hexdigest(),
                "module_sha256": hashlib.sha256((actual / "src/shiri_speech.c").read_bytes()).hexdigest(),
                "version": VERSION, "header_bytes": 96, "packet_age_ns": 250000000, "reserve_ns": 20000000,
                "kernel_credential_proof": credentials, "output": outputs}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--patch", type=Path, default=PATCH)
    parser.add_argument("--compiler")
    parser.add_argument("--no-sanitize", action="store_true")
    parser.add_argument("--require-credentials", action="store_true")
    args = parser.parse_args()
    print(json.dumps(check(args.source, args.compiler, args.patch, not args.no_sanitize, args.require_credentials), indent=2))


if __name__ == "__main__":
    main()
