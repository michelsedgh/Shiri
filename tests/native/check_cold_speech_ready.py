"""Actual composed C preparation/player callback and strict JSON checks."""

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
PATCH = ROOT / "install/patches/owntone-29.3-cold-speech-ready.patch"
CALLBACK_PREIMAGE = ROOT / "tests/native/cold_speech_callbacks_preimage.c"
CALLBACK_PREIMAGE_SHA256 = "639baa9e4ad0a349e9400227dd53695c099ad2d01dfa3ee43f7e73b19c05d832"


def callback_preimage():
    data = CALLBACK_PREIMAGE.read_bytes()
    if hashlib.sha256(data).hexdigest() != CALLBACK_PREIMAGE_SHA256:
        raise ValueError("Pinned actual callback-registry preimage changed")
    return data.decode()


def postimage(patch, name):
    marker = f"diff --git a/{name} b/{name}\n"
    if patch.count(marker) != 1:
        raise ValueError("Missing exact maintained readiness postimage")
    section = patch.split(marker, 1)[1].split("\ndiff --git ", 1)[0]
    return (
        "\n".join(
            line[1:]
            for line in section.splitlines()
            if line.startswith(" ") or line.startswith("+") and not line.startswith("+++")
        )
        + "\n"
    )


def patch_source(directory, patch, extractor):
    spec = importlib.util.spec_from_file_location("late_extract", ROOT / "tests/native/check_late_speech.py")
    late = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(late)
    src = directory / "src"
    (src / "outputs").mkdir(parents=True)
    (src / "inputs").mkdir()
    (src / "inputs/shiri_pcm.h").write_text(
        late.added_file(
            (ROOT / "install/patches/owntone-29.3-timed-pcm.patch").read_text(), "src/inputs/shiri_pcm.h"
        )
    )
    for name in ("shiri_source.h", "shiri_source_json.h"):
        (src / name).write_text(
            late.added_file(
                (ROOT / "install/patches/owntone-29.3-source-transition.patch").read_text(), f"src/{name}"
            )
        )
    (src / "outputs/cast_json.h").write_text(
        late.added_file(
            (ROOT / "install/patches/owntone-29.3-transport-control.patch").read_text(),
            "src/outputs/cast_json.h",
        )
    )
    for name in ("shiri_speech_ready.h", "shiri_speech_ready_json.h"):
        (src / name).write_text(late.added_file(patch, f"src/{name}"))
    previous = (ROOT / "install/patches/owntone-29.3-late-speech.patch").read_text()
    module = late.added_file(previous, "src/shiri_speech.c")
    extra = extractor.function(postimage(patch, "src/shiri_speech.c"), "shiri_speech_ids_match")
    module = module.replace(
        "\nvoid\nshiri_speech_deinit(void)", "\n" + extra + "\nvoid\nshiri_speech_deinit(void)"
    )
    (src / "shiri_speech.c").write_text(module)
    header = late.added_file(previous, "src/shiri_speech.h")
    header = header.replace(
        "void shiri_speech_deinit(void);",
        "int shiri_speech_ids_match(const uint8_t room[16], const uint8_t launch[16]);\nvoid shiri_speech_deinit(void);",
    )
    (src / "shiri_speech.h").write_text(header)
    (src / "player.c").write_text(postimage(patch, "src/player.c"))
    callbacks = callback_preimage()
    (src / "outputs.c").write_text(callbacks + "\n" + postimage(patch, "src/outputs.c"))
    (directory / "configure.ac").write_text(postimage(patch, "configure.ac"))
    return directory


def check(source=None, compiler=None, patch_path=PATCH):
    compiler = compiler or shutil.which("clang") or shutil.which("cc")
    spec = importlib.util.spec_from_file_location(
        "extract_actual", ROOT / "tests/native/check_source_transition.py"
    )
    extractor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(extractor)
    if source is None:
        with tempfile.TemporaryDirectory(prefix="shiri-ready-postimages-") as temporary:
            assembled = patch_source(Path(temporary), patch_path.read_text(), extractor)
            result = check(assembled, compiler, patch_path)
            result["source"] = "maintained patch postimages plus exact callback preimage"
            return result
    source = source.resolve()
    if not any(marker in (source / "configure.ac").read_text() for marker in ("-speech1-ready1])", "-speech1-ready1-anchor1])", "-speech1-ready1-anchor1-jitter1])")):
        raise ValueError("Readiness requires the exact reviewed-ready1 backend")
    player = (source / "src/player.c").read_text()
    outputs = (source / "src/outputs.c").read_text()
    preimage = callback_preimage()
    for name in ("callback_remove", "callback_add", "outputs_cb"):
        if extractor.function(outputs, name).strip() != extractor.function(preimage, name).strip():
            raise ValueError("Actual output registry differs from the pinned callback preimage")
    state = extractor.between(player, "/* Speech preparation owns", "\nstruct event_base *evbase_player;")
    names = (
        "shiri_speech_ready_now",
        "shiri_speech_source_matches",
        "shiri_speech_request_matches",
        "shiri_speech_outputs_snapshot",
        "shiri_speech_reap_start_callbacks",
        "shiri_speech_schedule_idle_stop",
        "shiri_speech_bind_session",
        "shiri_speech_setup_event_clear",
        "shiri_speech_setup_timeout",
        "device_shiri_speech_start_cb",
        "shiri_speech_prepare",
        "shiri_speech_prepare_bh",
        "shiri_speech_last_outputs",
        "shiri_speech_mix_ready",
        "shiri_speech_ready_check",
        "player_shiri_speech_ready",
    )
    # Only unchanged upstream64slot signed-loop warnings are scoped out;
    # every new readiness/parser function retains full-Werror validation.
    functions = '#pragma GCC diagnostic push\n#pragma GCC diagnostic ignored "-Wsign-compare"\n'
    functions += "\n".join(
        extractor.function(outputs, name) for name in ("callback_remove", "callback_add", "outputs_cb")
    )
    functions += "\n#pragma GCC diagnostic pop\n"
    functions += extractor.function(outputs, "outputs_shiri_callback_pending") + "\n"
    functions += extractor.function(outputs, "outputs_shiri_stop_delayed_cancel") + "\n"
    functions += "\n".join(extractor.function(player, name) for name in names)
    flags = [
        "-std=gnu11",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-O1",
        "-fsanitize=address,undefined",
        "-fno-sanitize-recover=all",
        "-I" + str(source / "src"),
    ]
    result = []
    with tempfile.TemporaryDirectory(prefix="shiri-cold-speech-c-") as temp:
        directory = Path(temp)
        text = (
            (ROOT / "tests/native/test_cold_speech_ready.c")
            .read_text()
            .replace("/* @ACTUAL_STATE@ */", state)
            .replace("/* @ACTUAL_FUNCTIONS@ */", functions)
        )
        scaffold = directory / "test.c"
        scaffold.write_text(text)
        binary = directory / "player"
        subprocess.run(
            [
                compiler,
                *flags,
                '-DSHIRI_SPEECH_SOURCE="' + str(source / "src/shiri_speech.c") + '"',
                str(scaffold),
                "-lm",
                "-o",
                str(binary),
            ],
            check=True,
            timeout=60,
        )
        result.append(
            subprocess.run(
                [str(binary)], check=True, capture_output=True, text=True, timeout=15
            ).stdout.strip()
        )
        json_flags = shlex.split(
            subprocess.run(
                ["pkg-config", "--cflags", "--libs", "json-c"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout
        )
        binary = directory / "json"
        subprocess.run(
            [
                compiler,
                *flags,
                *json_flags,
                str(ROOT / "tests/native/test_cold_speech_ready_json.c"),
                "-o",
                str(binary),
            ],
            check=True,
            timeout=60,
        )
        result.append(
            subprocess.run(
                [str(binary)], check=True, capture_output=True, text=True, timeout=15
            ).stdout.strip()
        )
    return {
        "passed": True,
        "source": str(source),
        "compiler": compiler,
        "sanitizers": True,
        "patch_sha256": hashlib.sha256(patch_path.read_bytes()).hexdigest(),
        "callback_preimage_sha256": CALLBACK_PREIMAGE_SHA256,
        "kernel_or_physical_output_proof": False,
        "checks": result,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path)
    parser.add_argument("--compiler")
    args = parser.parse_args()
    print(json.dumps(check(args.source, args.compiler), indent=2))
