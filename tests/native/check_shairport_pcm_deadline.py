#!/usr/bin/python3
"""Check exact native publisher expiry admission and retain historical overlays."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
FULL_PREIMAGE = "199ea0575b5d83f11901758b043d36fc8b31e6b7d21c50564da49cef4dd4f1a9"
FULL_POSTIMAGE = "c1b8d3faf39b00626b2d3c9e86971b78563d2d4f642d57ef9b7e407d1a7859d6"
PURE_PREIMAGE = "fb63b57999e1ae53b19f1ed995cf1251c7779a6216b914985d53b0533bfe5e09"
PURE_POSTIMAGE = "d1f12a56ef13a0d6c8530570756da9a3f3c326bebfa9d2b27e5b52c0558b40a5"
CALLBACK_PREIMAGE = "abac2b0c05c81136a7e6c85223b1b70e1b2162976884ff07c586176e2d932cc7"
PCM_HEADER = "38f964f4ef457d92a7e159af45801ef7167d22637a2e306a1b0826042ee3479e"
TIMING_HEADER = "d4a7db096d74ae9999a9ac0545fe24d94e7d79faff48b778c04f34e33f28d054"
PREFIX = (
    "/* Count only samples whose original integer first-sample calendar has elapsed.\n"
)
INCLUDE = '#include "shiri_timing.h"\n'


def digest(value):
    return hashlib.sha256(
        value.encode() if isinstance(value, str) else value
    ).hexdigest()


def module(name):
    path = REPOSITORY / "tests/native" / (name + ".py")
    spec = importlib.util.spec_from_file_location("deadline_" + name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def callback_preimage():
    value = (
        REPOSITORY / "tests/native/shairport_pcm_deadline_preimage.inc"
    ).read_text()
    if digest(value) != CALLBACK_PREIMAGE:
        raise ValueError("Historical native publisher control changed")
    return value.rstrip("\n")


def inverse_audio(value, *, composed):
    before, after = (
        (FULL_PREIMAGE, FULL_POSTIMAGE) if composed else (PURE_PREIMAGE, PURE_POSTIMAGE)
    )
    if digest(value) != after:
        raise ValueError(
            "Receiver publisher differs from exact reviewed deadline postimage"
        )
    function = module("check_shairport_clock_recovery").function_body(
        value, "play_native"
    )
    start = value.index(PREFIX)
    end = value.index(function) + len(function)
    if value.count(INCLUDE) != 1:
        raise ValueError(
            "Native deadline publisher must own its timing header dependency"
        )
    result = value[:start] + callback_preimage() + value[end:]
    result = result.replace(INCLUDE, "", 1)
    if digest(result) != before:
        raise ValueError(
            "Exact deadline inverse did not restore historical native publisher"
        )
    return result


def forward_audio(before, expected):
    if inverse_audio(expected, composed=False) != before:
        raise ValueError(
            "Deadline postimage did not preserve the exact historical callback preimage"
        )
    return expected


@contextmanager
def preimage_source(source):
    """Restore ONLY the exact new publisher; immutable AP2 overlay guards remain."""
    source = Path(source)
    # The buffered-only phone mapping has its own exact native gate. Restore
    # only that verified overlay before checking these unchanged older layers.
    phone = {}
    if '"-phone1"' in (source / "common.c").read_text():
        phone = module("check_shairport_buffered_timing").preimage_files(source)
    audio_bytes = phone.get("audio_shiri.c")
    audio = inverse_audio(
        audio_bytes.decode() if audio_bytes is not None else (source / "audio_shiri.c").read_text(),
        composed=True,
    )
    for name, expected in (
        ("shiri_pcm.h", PCM_HEADER),
        ("shiri_timing.h", TIMING_HEADER),
    ):
        if digest((source / name).read_bytes()) != expected:
            raise ValueError("Native publisher wire/calendar header changed: " + name)
    overlay = (
        "check_receiver_events"
        if (source / "shiri_event_control.h").exists()
        else "check_receiver_volume"
    )
    names = set(module(overlay).BACKEND_HASHES)
    names.update(("shiri_pcm.h", "shiri_timing.h"))
    with tempfile.TemporaryDirectory(
        prefix="shiri-deadline-exact-preimage-"
    ) as temporary:
        target = Path(temporary)
        for name in names:
            (target / name).parent.mkdir(parents=True, exist_ok=True)
            if name in phone:
                (target / name).write_bytes(phone[name])
            else:
                shutil.copyfile(source / name, target / name)
        (target / "audio_shiri.c").write_text(audio)
        yield target


def recovered_audio(source):
    """Return the actual candidate callback after strict existing overlay inverse."""
    with preimage_source(source) as old:
        before = module("check_receiver_events").preimage_audio(old)
    if digest(before) != PURE_PREIMAGE:
        raise ValueError("AP2 overlay inverse changed the historical timed3 source")
    clocks = module("check_shairport_clock_recovery")
    return forward_audio(before, clocks.patched_audio(clocks.timing_tools()))


def check_overlays(source, *, stage, compiler="cc", sanitize=False):
    with preimage_source(source) as old:
        if stage == "volume":
            module("check_receiver_volume").verify_source(old)
        elif stage == "events":
            events = module("check_receiver_events")
            with events.preimage_volume1(old):
                pass
            events.run_tests(old, compiler, sanitize)
        else:
            raise ValueError("Unknown historical overlay stage")
    return {
        "ok": True,
        "stage": stage,
        "publisher_sha256": FULL_POSTIMAGE,
        "historical_publisher_sha256": FULL_PREIMAGE,
        "exact_inverse": True,
    }


def check_native_consumer(directory, binary, environment):
    # Execute the unchanged real consumer against packets emitted by actual C.
    path = REPOSITORY / "shiri/runtime/timing.py"
    spec = importlib.util.spec_from_file_location("deadline_actual_native_timing", path)
    timing = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = timing
    spec.loader.exec_module(timing)
    observed = {}
    for option, gaps, expected in (
        ("--consumer-wire", 0, ((1, 4, 362), (2, 366, 16))),
        ("--gap-wire", 1, ((1, 0, 16), (3, 32, 16))),
    ):
        output = directory / (option[2:] + ".pcm")
        subprocess.run(
            [str(binary), option, str(output)],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        data, packets = output.read_bytes(), []
        while data:
            size = timing.HEADER_BYTES + int.from_bytes(data[16:20], "big")
            packets.append(timing.Packet.decode(data[:size]))
            data = data[size:]
        if (
            len(packets) != 2
            or tuple((x.sequence, x.frame_index, x.frames) for x in packets) != expected
        ):
            raise ValueError(
                "Native consumer received an unexpected actual publisher calendar"
            )
        fence = timing.StreamFence(packets[0].session)
        for packet in packets:
            fence.accept(packet)
        if (
            fence.gaps != gaps
            or fence.next_frame != packets[-1].frame_index + packets[-1].frames
        ):
            raise ValueError(
                "Original native fence changed skip/partial calendar handling"
            )
        observed[option[2:]] = {
            "packets": len(packets),
            "gaps": fence.gaps,
            "next_frame": fence.next_frame,
        }
    return observed


def run_tests(*, shairport_source=None, compiler=None, sanitize=True):
    compiler = compiler or shutil.which("clang") or shutil.which("cc")
    if not compiler:
        raise RuntimeError(
            "A C compiler is required for actual publisher deadline checks"
        )
    clocks = module("check_shairport_clock_recovery")
    tools = clocks.timing_tools()
    expected = clocks.patched_audio(tools)
    before = inverse_audio(expected, composed=False)
    actual = recovered_audio(shairport_source) if shairport_source else expected
    if actual != expected:
        raise ValueError(
            "Actual receiver deadline callback differs from composed patch"
        )
    for name in ("sample_clocks", "sample_clocks_recovered", "clock_recovery_deadline"):
        if clocks.function_body(before, name) != clocks.function_body(actual, name):
            raise ValueError("Historical clock recovery function changed: " + name)
    flags = [
        "-std=c99",
        "-D_POSIX_C_SOURCE=200809L",
        "-O1",
        "-Wall",
        "-Wextra",
        "-Werror",
    ]
    if sys.platform == "darwin":
        flags.append("-D_DARWIN_C_SOURCE=1")
    if sanitize:
        flags += ["-fsanitize=address,undefined", "-fno-sanitize-recover=all"]
    env = {
        **os.environ,
        "ASAN_OPTIONS": "detect_leaks=0:halt_on_error=1",
        "UBSAN_OPTIONS": "halt_on_error=1",
    }
    fixture = REPOSITORY / "tests/native/test_shairport_pcm_deadline.c"
    with tempfile.TemporaryDirectory(
        prefix="shiri-native-publisher-deadline-"
    ) as temporary:
        target = Path(temporary)
        for name in ("audio.h", "shiri_pcm.h", "shiri_timing.h"):
            value = (
                (Path(shairport_source) / name).read_text()
                if shairport_source
                else tools.postimage(tools.SHA_PATCH, name)
            )
            (target / name).write_text(value)
        (target / "common.h").write_text(
            (REPOSITORY / "tests/native/shairport_timing_common.h").read_text()
        )
        (target / "libconfig.h").write_text("/* Native configuration seam only. */\n")
        binaries = {}
        for label, audio in (("original", before), ("candidate", actual)):
            (target / "audio_shiri.inc").write_text(audio)
            binary = target / label
            definitions = ["-DHAS_EXPIRED_PREFIX=1"] if label == "candidate" else []
            subprocess.run(
                [
                    compiler,
                    *flags,
                    *definitions,
                    "-I",
                    str(target),
                    str(fixture),
                    "-lm",
                    "-pthread",
                    "-o",
                    str(binary),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            binaries[label] = binary
        old = subprocess.run(
            [str(binaries["original"])],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if (
            old.returncode != 72
            or "sends==sent+1 && last.presentation>base_raw+delta" not in old.stderr
        ):
            raise ValueError(
                "Historical publisher did not reproduce the actual expired first-sample defect"
            )
        candidate = subprocess.run(
            [str(binaries["candidate"])],
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        wires = []
        for label, binary in binaries.items():
            wire = target / (label + ".pcm")
            subprocess.run(
                [str(binary), "--future-wire", str(wire)],
                env=env,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            wires.append(wire.read_bytes())
        if wires[0] != wires[1]:
            raise ValueError("Future native wire payload/metadata changed")
        consumer = check_native_consumer(target, binaries["candidate"], env)
    return {
        "ok": True,
        "sanitized": sanitize,
        "compiler": str(compiler),
        "result": candidate.stdout.strip(),
        "original_expired_control": old.stderr.strip(),
        "future_wire_byte_identical": True,
        "future_wire_sha256": digest(wires[0]),
        "unchanged_native_consumer": consumer,
        "callback_sha256": digest(actual),
        "composed_callback_sha256": FULL_POSTIMAGE,
        "historical_overlay_inverse_sha256": FULL_PREIMAGE,
        "sampled_admission_only": True,
        "no_dsp_relocation": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument(
        "--stage", choices=("volume", "events", "deadline"), default="deadline"
    )
    parser.add_argument("--compiler")
    parser.add_argument("--sanitize", action="store_true")
    args = parser.parse_args()
    try:
        if args.stage != "deadline":
            if args.source is None:
                parser.error("--source is required for historical overlay checks")
            result = check_overlays(
                args.source,
                stage=args.stage,
                compiler=args.compiler or "cc",
                sanitize=args.sanitize,
            )
        else:
            result = run_tests(
                shairport_source=args.source,
                compiler=args.compiler,
                sanitize=args.sanitize,
            )
        print(json.dumps(result, indent=2))
    except subprocess.CalledProcessError as error:
        raise SystemExit(
            "Native publisher deadline check failed: "
            + (error.stderr or error.stdout or str(error))
        ) from None


if __name__ == "__main__":
    main()
