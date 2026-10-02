"""Execute the maintained late-mix C parser/queue/gain, without audio devices.

With --source, checks the exact composed OwnTone source. Otherwise extracts
the added module bytes from the patch. Linux credentials require the separate
kernel fixture; this portable test never claims a socket-authority result.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "install/patches/owntone-29.3-late-speech.patch"


def added_file(patch: str, name: str) -> str:
    marker = f"diff --git a/{name} b/{name}\n"
    if patch.count(marker) != 1:
        raise ValueError(f"Missing exact added module: {name}")
    section = patch.split(marker, 1)[1].split("\ndiff --git ", 1)[0]
    if "--- /dev/null\n" not in section:
        raise ValueError("The overlay module must be independently added")
    return "\n".join(line[1:] for line in section.splitlines()
                     if line.startswith("+") and not line.startswith("+++")) + "\n"


def validate_integration(patch: str, source: Path | None) -> None:
    if source is None:
        # Check the maintained call ordering from the actual diff. Pure C
        # execution below checks the real module instead of a Python mirror.
        if patch.count("+  shiri_speech_poll();") != 1:
            raise ValueError("Overlay receives must run once per playback tick")
        seam = patch.split("+      shiri_speech_mix(", 1)
        if len(seam) != 2 or "outputs_write(pb_session.buffer" not in seam[1].split("@@", 1)[0]:
            raise ValueError("Late speech must precede the existing output dispatch")
        if "cfg_getbool(cfg_getsec(cfg, \"library\"), \"pipe_framed\")" not in patch:
            raise ValueError("Enabled speech must require the framed profile")
        if "framed1-alsa1-speech1])" not in patch:
            raise ValueError("The maintained backend needs an explicit speech marker")
        return
    player = (source / "src/player.c").read_text()
    callback = player.split("playback_cb(int fd, short what, void *arg)", 1)[1]
    callback = callback.split("/* ----------------", 1)[0]
    if callback.count("shiri_speech_poll();") != 1:
        raise ValueError("Overlay receives must run once per playback tick")
    receive = callback.index("shiri_speech_poll();")
    read = callback.index("source_read(&nbytes")
    mix = callback.index("shiri_speech_mix(pb_session.buffer")
    output = callback.index("outputs_write(pb_session.buffer")
    if not receive < read < mix < output:
        raise ValueError("The common mix must run after the read and before outputs")
    if callback.count("shiri_speech_mix(") != 1:
        raise ValueError("Do not duplicate the overlay into an output or timer")
    deinit = player.split("player_deinit(void)", 1)[1]
    if deinit.index("pthread_join(tid_player") > deinit.index("shiri_speech_deinit();"):
        raise ValueError("Socket disposal must follow player thread quiescence")
    if "shiri_speech.c shiri_speech.h" not in (source / "src/Makefile.am").read_text():
        raise ValueError("The new module must be distributed and built")
    marker = (source / "configure.ac").read_text()
    if not any(exact in marker for exact in ("framed1-alsa1-speech1])", "framed1-alsa1-speech1-ready1])", "framed1-alsa1-speech1-ready1-anchor1])", "framed1-alsa1-speech1-ready1-anchor1-jitter1])")):
        raise ValueError("The composed source lacks the explicit speech marker")


def check(source: Path | None, patch_path: Path, compiler: str | None, sanitize: bool,
          credentials: bool = False) -> dict:
    if source and "-speech1-ready1-anchor1-jitter1])" in (source / "configure.ac").read_text():
        validate_integration(patch_path.read_text(), source)
        # Preserve execution of the immutable immediate-mix patch proof. The
        # deliberate reserve is separately tested against its exact module;
        # do not pretend that speech1's immediate-consumption vector changed.
        original = check(None, patch_path, compiler, sanitize, False)
        import importlib.util
        spec = importlib.util.spec_from_file_location("late_jitter", ROOT / "tests/native/check_speech_jitter.py")
        jitter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(jitter)
        reserve = jitter.check(source, compiler, sanitize=sanitize)
        socket_output = None
        chosen = reserve["compiler"]
        if credentials:
            if sys.platform != "linux" or os.geteuid() != 0:
                raise RuntimeError("Credential proof requires Linux root and two disposable synthetic UIDs")
            with tempfile.TemporaryDirectory(prefix="shiri-jitter-speech-socket-") as temporary:
                binary = Path(temporary) / "socket"
                command = [chosen, "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-D_GNU_SOURCE",
                           f'-DSHIRI_SPEECH_SOURCE="{source / "src/shiri_speech.c"}"',
                           str(ROOT / "tests/native/test_late_speech_socket.c"), "-lm", "-o", str(binary)]
                if sanitize:
                    command[1:1] = ["-fsanitize=address,undefined", "-fno-sanitize-recover=all"]
                subprocess.run(command, check=True, timeout=90)
                result = subprocess.run([str(binary)], check=True, capture_output=True, text=True, timeout=15)
                socket_output = result.stdout.strip()
        return {"passed": True, "compiler": chosen, "sanitizers": sanitize,
                "patch_sha256": original["patch_sha256"],
                "module_sha256": reserve["module_sha256"], "actual_source": str(source),
                "original_speech1_patch_proof": original,
                "deliberate_20ms_reserve_proof": reserve,
                "kernel_credential_proof": credentials, "socket_output": socket_output,
                "output": original["output"] + "; " + "; ".join(reserve["output"])}
    patch = patch_path.read_text()
    validate_integration(patch, source)
    chosen = compiler or shutil.which("clang") or shutil.which("cc")
    if not chosen:
        raise RuntimeError("A C compiler is required for the actual-source proof")
    with tempfile.TemporaryDirectory(prefix="shiri-late-speech-c-") as temporary:
        directory = Path(temporary)
        if source is None:
            module = directory / "shiri_speech.c"
            module.write_text(added_file(patch, "src/shiri_speech.c"))
            (directory / "shiri_speech.h").write_text(added_file(patch, "src/shiri_speech.h"))
        else:
            module = source / "src/shiri_speech.c"
            for name in ("shiri_speech.c", "shiri_speech.h"):
                expected = added_file(patch, f"src/{name}")
                if any(marker in (source / "configure.ac").read_text() for marker in ("-speech1-ready1])", "-speech1-ready1-anchor1])")):
                    import importlib.util
                    spec = importlib.util.spec_from_file_location("ready_extract", ROOT / "tests/native/check_cold_speech_ready.py")
                    ready = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(ready)
                    spec = importlib.util.spec_from_file_location("source_extract", ROOT / "tests/native/check_source_transition.py")
                    extractor = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(extractor)
                    if name.endswith(".c"):
                        function = extractor.function(ready.postimage(ready.PATCH.read_text(), "src/shiri_speech.c"), "shiri_speech_ids_match")
                        expected = expected.replace("\nvoid\nshiri_speech_deinit(void)", "\n" + function + "\nvoid\nshiri_speech_deinit(void)")
                    else:
                        expected = expected.replace("void shiri_speech_deinit(void);", "int shiri_speech_ids_match(const uint8_t room[16], const uint8_t launch[16]);\nvoid shiri_speech_deinit(void);")
                if (source / "src" / name).read_text() != expected:
                    raise ValueError("The tested module differs from the maintained patch")
        binary = directory / "check"
        command = [chosen, "-std=gnu11", "-Wall", "-Wextra", "-Werror",
                   "-D_GNU_SOURCE", f'-DSHIRI_SPEECH_SOURCE="{module}"',
                   str(ROOT / "tests/native/test_late_speech.c"), "-lm", "-o", str(binary)]
        if sanitize:
            command[1:1] = ["-fsanitize=address,undefined", "-fno-sanitize-recover=all"]
        subprocess.run(command, check=True, timeout=90)
        result = subprocess.run([str(binary)], check=True, capture_output=True, text=True, timeout=15)
        socket_output = None
        if credentials:
            if sys.platform != "linux" or os.geteuid() != 0:
                raise RuntimeError("Credential proof requires Linux root and two disposable synthetic UIDs")
            socket_binary = directory / "check-socket"
            socket_command = [arg if arg != str(ROOT / "tests/native/test_late_speech.c")
                              else str(ROOT / "tests/native/test_late_speech_socket.c")
                              for arg in command]
            socket_command[-1] = str(socket_binary)
            subprocess.run(socket_command, check=True, timeout=90)
            socket_result = subprocess.run([str(socket_binary)], check=True,
                                           capture_output=True, text=True, timeout=15)
            socket_output = socket_result.stdout.strip()
        return {"passed": True, "compiler": chosen, "sanitizers": sanitize,
                "patch_sha256": hashlib.sha256(patch_path.read_bytes()).hexdigest(),
                "module_sha256": hashlib.sha256(module.read_bytes()).hexdigest(),
                "actual_source": str(source) if source else "exact added patch module",
                "kernel_credential_proof": credentials, "socket_output": socket_output,
                "output": result.stdout.strip()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--patch", type=Path, default=PATCH)
    parser.add_argument("--compiler")
    parser.add_argument("--no-sanitize", action="store_true")
    parser.add_argument("--require-credentials", action="store_true")
    args = parser.parse_args()
    print(json.dumps(check(args.source, args.patch, args.compiler, not args.no_sanitize,
                           args.require_credentials), indent=2))


if __name__ == "__main__":
    main()
