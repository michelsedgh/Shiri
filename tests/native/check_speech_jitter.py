"""Execute the exact additive20ms reserve and authenticated status C seams.

No device is opened. --source tests the exact composed backend; the default
reconstructs the maintained late/ready module, then applies the additive patch.
The preimage proof uses the unmodified ready1 media module, not a queue mirror.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "install/patches/owntone-29.3-speech-jitter.patch"
PREIMAGE_C_SHA256 = "80509119243368e7cab0c3fad1c0269a8e4d2f44e051e9916eebf0273c4347c0"
PREIMAGE_H_SHA256 = "ddae9c87e423c3a4d628c648edff97fd2c987f6c57ef75be3f2ac2d0af4ac553"
MARKER = "-speech1-ready1-anchor1-jitter1])"


def module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tests/native" / filename)
    assert spec and spec.loader
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def baseline(directory: Path) -> Path:
    ready = module("jitter_ready", "check_cold_speech_ready.py")
    extractor = module("jitter_extract_baseline", "check_source_transition.py")
    ready.patch_source(directory, ready.PATCH.read_text(), extractor)
    for name, digest in (("shiri_speech.c", PREIMAGE_C_SHA256), ("shiri_speech.h", PREIMAGE_H_SHA256)):
        if hashlib.sha256((directory / "src" / name).read_bytes()).hexdigest() != digest:
            raise ValueError("Frozen actual ready1 media preimage changed")
    return directory


def patch_module(directory: Path, patch: Path) -> Path:
    baseline(directory)
    subprocess.run(
        ["git", "apply", "--check", "--include=src/shiri_speech.c", "--include=src/shiri_speech.h", str(patch)],
        cwd=directory, check=True, capture_output=True, timeout=10,
    )
    subprocess.run(
        ["git", "apply", "--include=src/shiri_speech.c", "--include=src/shiri_speech.h", str(patch)],
        cwd=directory, check=True, capture_output=True, timeout=10,
    )
    return directory


def postimage_section(patch: str, filename: str) -> str:
    ready = module("jitter_postimage", "check_cold_speech_ready.py")
    return ready.postimage(patch, filename)


def validate_source(source: Path, assembled: Path, patch: str) -> None:
    extract = module("jitter_validate_extract", "check_source_transition.py")
    original = module("jitter_original_integration", "check_late_speech.py")
    original.validate_integration(original.PATCH.read_text(), source)
    for name in ("shiri_speech.c", "shiri_speech.h"):
        if (source / "src" / name).read_bytes() != (assembled / "src" / name).read_bytes():
            raise ValueError("Actual media module differs from the exact maintained patch")
    for file, names in (
        ("src/player.c", ("shiri_speech_status_get", "player_shiri_speech_status")),
        ("src/httpd_jsonapi.c", ("shiri_speech_status_hex", "shiri_speech_status_integer", "jsonapi_reply_player_shiri_speech_status")),
    ):
        actual = (source / file).read_text()
        expected = postimage_section(patch, file)
        for name in names:
            if extract.function(actual, name) != extract.function(expected, name):
                raise ValueError("Actual diagnostic seam differs from the maintained patch")
    player = (source / "src/player.c").read_text()
    playback = extract.function(player, "playback_cb")
    if not (playback.index("shiri_speech_poll();") < playback.index("source_read(&nbytes")
            < playback.index("shiri_speech_mix(pb_session.buffer") < playback.index("outputs_write(pb_session.buffer")):
        raise ValueError("Speech reserve must keep the existing common player dispatch")
    if MARKER not in (source / "configure.ac").read_text():
        raise ValueError("The additive reserve requires the exact jitter1 marker")
    http = (source / "src/httpd_jsonapi.c").read_text()
    if '{ HTTPD_METHOD_GET,    "^/api/player/shiri-speech-status$",            jsonapi_reply_player_shiri_speech_status }' not in http:
        raise ValueError("Diagnostic status must use the existing GET JSON router")
    authorized = extract.function(http, "jsonapi_request")
    if authorized.index("httpd_request_is_authorized(hreq)") > authorized.index("hreq->handler(hreq)"):
        raise ValueError("The status route cannot bypass existing OwnTone authentication")


def check(source: Path | None = None, compiler: str | None = None,
          patch_path: Path = PATCH, sanitize: bool = True) -> dict:
    compiler = compiler or shutil.which("clang") or shutil.which("cc")
    if not compiler:
        raise RuntimeError("Actual C proof requires a compiler")
    extract = module("jitter_actual_extract", "check_source_transition.py")
    patch = patch_path.read_text()
    if MARKER not in postimage_section(patch, "configure.ac"):
        raise ValueError("The additive reserve requires the exact jitter1 marker")
    flags = ["-std=gnu11", "-Wall", "-Wextra", "-Werror", "-D_GNU_SOURCE", "-O1"]
    if sanitize:
        flags += ["-fsanitize=address,undefined", "-fno-sanitize-recover=all"]
    outputs = []
    with tempfile.TemporaryDirectory(prefix="shiri-speech-jitter-c-") as temporary:
        directory = Path(temporary)
        previous = baseline(directory / "before")
        assembled = patch_module(directory / "after", patch_path)
        actual = source.resolve() if source else assembled
        if source:
            validate_source(actual, assembled, patch)
        test = ROOT / "tests/native/test_speech_jitter.c"
        for label, chosen, extra in (("preimage", previous, ["-DJITTER_PREIMAGE"]), ("reserve", actual, [])):
            binary = directory / label
            subprocess.run([compiler, *flags, *extra,
                            '-DSHIRI_SPEECH_SOURCE="' + str(chosen / "src/shiri_speech.c") + '"',
                            str(test), "-lm", "-o", str(binary)], check=True, timeout=90)
            result = subprocess.run([str(binary)], check=True, capture_output=True, text=True, timeout=20)
            outputs.append(result.stdout.strip())

        # All status functions are taken directly from the additive C patch,
        # or validated equal to those bytes before extraction from --source.
        player = (actual / "src/player.c").read_text() if source else postimage_section(patch, "src/player.c")
        header = (actual / "src/player.h").read_text() if source else postimage_section(patch, "src/player.h")
        http = (actual / "src/httpd_jsonapi.c").read_text() if source else postimage_section(patch, "src/httpd_jsonapi.c")
        status_type = extract.between(header, "struct player_shiri_speech_status {", "\nint player_shiri_speech_status")
        player_functions = "\n".join(extract.function(player, name) for name in ("shiri_speech_status_get", "player_shiri_speech_status"))
        http_functions = "\n".join(extract.function(http, name) for name in ("shiri_speech_status_hex", "shiri_speech_status_integer", "jsonapi_reply_player_shiri_speech_status"))
        scaffold = directory / "status.c"
        scaffold.write_text((ROOT / "tests/native/test_speech_jitter_status.c").read_text()
                            .replace("/* @ACTUAL_STATUS_TYPE@ */", status_type)
                            .replace("/* @ACTUAL_PLAYER_FUNCTIONS@ */", player_functions)
                            .replace("/* @ACTUAL_HTTP_FUNCTIONS@ */", http_functions))
        json_flags = shlex.split(subprocess.run(["pkg-config", "--cflags", "--libs", "json-c"],
                                               check=True, capture_output=True, text=True, timeout=10).stdout)
        binary = directory / "status"
        subprocess.run([compiler, *flags, '-I' + str(actual / "src"),
                        '-I' + str(ROOT / "tests/native"),
                        '-DSHIRI_SPEECH_SOURCE="' + str(actual / "src/shiri_speech.c") + '"',
                        str(scaffold), *json_flags, "-lm", "-o", str(binary)], check=True, timeout=90)
        result = subprocess.run([str(binary)], check=True, capture_output=True, text=True, timeout=20)
        outputs.append(result.stdout.strip())
        return {"passed": True, "compiler": compiler, "sanitizers": sanitize,
                "source": str(source) if source else "exact composed patch modules/extracted actual C seams",
                "patch_sha256": hashlib.sha256(patch_path.read_bytes()).hexdigest(),
                "module_sha256": hashlib.sha256((actual / "src/shiri_speech.c").read_bytes()).hexdigest(),
                "reserve_ns": 20000000, "packet_age_ns": 250000000,
                "kernel_credential_proof": False, "output": outputs}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--patch", type=Path, default=PATCH)
    parser.add_argument("--compiler")
    parser.add_argument("--no-sanitize", action="store_true")
    args = parser.parse_args()
    print(json.dumps(check(args.source, args.compiler, args.patch, not args.no_sanitize), indent=2))


if __name__ == "__main__":
    main()
