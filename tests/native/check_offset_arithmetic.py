#!/usr/bin/python3
"""Sanitize the actual pinned OwnTone offset/config seams, without device I/O."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "install/patches/owntone-29.3-offset-arithmetic.patch"
spec = importlib.util.spec_from_file_location("offset_source_extract", ROOT / "tests/native/check_source_transition.py")
extract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(extract)


def build_fixture(native):
    delays, config = [], []
    for name in ("fifo", "cast", "pulse"):
        text = native(f"src/outputs/{name}.c")
        value = "ps->delay_ms" if name == "pulse" else "delay_ms"
        start = f"  {value} = outputs_buffer_duration_ms_get()"
        end = "\n  ps->next" if name == "pulse" else f"\n  {'cs->delay_ts' if name == 'cast' else 'fifo_session->delay'}.tv_sec"
        body = extract.between(text, start, end)
        setup = "struct { uint64_t delay_ms; } storage, *ps = &storage;" if name == "pulse" else "uint64_t delay_ms;"
        delays.append(f"static uint64_t {name}_delay(int offset)\n{{\n"
                      "  struct output_device storage_device = { offset, \"test\" }, *device = &storage_device;\n"
                      f"  {setup}\n{body}\n  return {value};\n}}\n")
    for name in ("alsa", "cast", "pulse"):
        text = native(f"src/outputs/{name}.c")
        start = text.index("  if (", text.index("  offset_ms ="))
        end = text.index("device->offset_ms = offset_ms;", start) + len("device->offset_ms = offset_ms;")
        body = text[start:end]
        config.append(f"static int {name}_config(int offset_ms)\n{{\n"
                      "  struct output_device storage_device = { 23, \"test\" }, *device = &storage_device;\n"
                      f"{body}\n  return device->offset_ms;\n}}\n")
    output = extract.between(native("src/outputs.c"), "  start_buffer_opt =", "\n  if (outputs_buffer_duration_ms !=")
    fixture = (ROOT / "tests/native/test_offset_arithmetic.c").read_text()
    return (fixture.replace("/* @ACTUAL_DELAY_FUNCTIONS@ */", "\n".join(delays))
            .replace("/* @ACTUAL_CONFIG_FUNCTIONS@ */", "\n".join(config))
            .replace("/* @ACTUAL_BUFFER_CONFIG@ */", output))


def build_buffer_fixture(native, *, after):
    text = native("src/outputs.c")
    helpers = "\n".join(extract.function(text, name) for name in (
        "outputs_buffer_samples_get", "outputs_buffer_size_get")) if after else ""
    functions = []
    for name, variable, device, master, latency in (
        ("raop", "rs", "rd", "rms", "RAOP_AUDIO_LATENCY_MS"),
        ("airplay", "session", "device", "ams", "AIRPLAY_AUDIO_LATENCY_MS"),
    ):
        text = native(f"src/outputs/{name}.c")
        field = re.search(r"  (int(?:64_t)?) offset_samples;", text).group(1)
        assignment = next(line for line in text.splitlines() if f"{variable}->offset_samples = " in line)
        stamp = next(line for line in text.splitlines() if f"cur_stamp.pos -= {variable}->offset_samples;" in line)
        functions.append(f"static int64_t {name}_offset(int offset, int rate)\n{{\n"
                         f"  struct output_device storage_device={{offset,{{rate,16,2}}}}, *{device}=&storage_device;\n"
                         f"  struct {{ {field} offset_samples; }} storage_session, *{variable}=&storage_session;\n"
                         f"{assignment}\n  return {variable}->offset_samples;\n}}\n"
                         f"static uint32_t {name}_stamp(uint32_t pos, int64_t offset)\n{{\n"
                         f"  struct {{ {field} offset_samples; }} storage_session={{offset}}, *{variable}=&storage_session;\n"
                         f"  struct {{ uint32_t pos; }} cur_stamp={{pos}};\n{stamp}\n  return cur_stamp.pos;\n}}\n")
        start = f"  if (outputs_buffer_samples_get(&buffer_samples, buffer_duration_ms - {latency}" if after else f"  {master}->output_buffer_samples = (buffer_duration_ms"
        end = text.index(f"  {master}->output_buffer_samples =", text.index(start))
        end = text.index(";", end) + 1
        body = text[text.index(start):end]
        declarations = "  uint64_t buffer_samples;\n" if after else ""
        label = " error:\n  return -1;\n" if after else ""
        functions.append(f"static int {name}_buffer(uint64_t buffer_duration_ms, int rate, uint32_t *out)\n{{\n"
                         "  struct media_quality quality_value={rate,16,2}, *quality=&quality_value;\n"
                         f"  struct {{ uint32_t output_buffer_samples; }} storage, *{master}=&storage;\n"
                         f"{declarations}{body}\n  *out={master}->output_buffer_samples;\n  return 0;\n{label}}}\n")
    text = native("src/outputs/alsa.c")
    start = "  if (outputs_buffer_samples_get(&buffer_samples, as->delay_ms" if after else "  pb->buffer_nsamp = as->delay_ms *"
    end = text.index("\n  ringbuffer_init", text.index(start))
    body = text[text.index(start):end]
    functions.append("static int alsa_buffer(uint64_t delay, int rate, int bits, int channels, uint64_t *frames, uint64_t *bytes)\n{\n"
                     "  struct { uint64_t delay_ms; } storage={delay}, *as=&storage;\n"
                     "  struct { struct media_quality quality; int buffer_nsamp; } playback={{rate,bits,channels},0}, *pb=&playback;\n"
                     "  size_t size;\n" + ("  uint64_t buffer_samples, buffer_bytes;\n" if after else "") + body +
                     "\n  *frames=pb->buffer_nsamp; *bytes=size;\n  return 0;\n" + (" error:\n  return -1;\n" if after else "") + "}\n")
    text = native("src/outputs/pulse.c")
    start = "  if (outputs_buffer_samples_get(&buffer_samples, ps->delay_ms" if after else "  ps->attr.tlength   = STOB"
    end = text.index("\n  ps->attr.prebuf", text.index(start))
    body = text[text.index(start):end]
    functions.append("static int pulse_buffer(uint64_t delay, int rate, int bits, int channels, uint32_t *out)\n{\n"
                     "  struct media_quality quality_value={rate,bits,channels}, *quality=&quality_value;\n"
                     "  struct { uint64_t delay_ms; struct { uint32_t tlength,maxlength; } attr; } playback={delay,{0,0}}, *ps=&playback;\n" +
                     ("  uint64_t buffer_samples, buffer_bytes;\n" if after else "  struct { uint32_t rate; } ss={(uint32_t)rate};\n") + body +
                     "\n  *out=ps->attr.tlength;\n  return 0;\n" + (" unlock_and_fail:\n  return -1;\n" if after else "") + "}\n")
    raop = native("src/outputs/raop.c")
    rate_bound = re.search(r"(?m)^#define RAOP_QUALITY_SAMPLE_RATE_MAX[^\n]+", raop).group(0) if after else ""
    start = "  // Missing legacy fields use defaults." if after else "  // Quality supported - note"
    discovery = extract.between(raop, start, "  if (!quality_is_equal")
    fixture = (ROOT / "tests/native/test_offset_buffers.c").read_text()
    return (fixture.replace("/* @ACTUAL_RATE_BOUND@ */", rate_bound)
            .replace("/* @ACTUAL_CHECKED_CONVERSIONS@ */", helpers)
            .replace("/* @ACTUAL_OFFSET_AND_BUFFER_FUNCTIONS@ */", "\n".join(functions))
            .replace("/* @ACTUAL_DISCOVERY_ADMISSION@ */", discovery))


def build_request_fixture(native):
    source = native("src/outputs/raop.c")
    # Each existing sender checks the admission error before submission. Keep
    # exhaustion on that established cleanup path rather than wrapping IDs.
    calls = list(re.finditer(r"ret = raop_add_headers\([^;]+;", source))
    if any(not re.match(r"\s*if \(ret < 0\)", source[call.end():]) for call in calls):
        raise RuntimeError("A RAOP request ignores header admission failure")
    return (ROOT / "tests/native/test_raop_cseq.c").read_text().replace(
        "/* @ACTUAL_REQUEST_HEADERS@ */", extract.function(source, "raop_add_headers"))


def run_tests(*, source: Path | None = None, compiler=None, sanitize=True):
    compiler = compiler or shutil.which("clang") or shutil.which("cc")
    if not compiler:
        raise RuntimeError("A C compiler is required for native offset tests")
    flags = ["-std=c11", "-O1", "-Wall", "-Wextra", "-Werror"]
    if sanitize:
        flags += ["-fsanitize=address,undefined", "-fno-sanitize-recover=all"]
    environment = dict(os.environ, ASAN_OPTIONS="detect_leaks=0:halt_on_error=1", UBSAN_OPTIONS="halt_on_error=1")
    results = []
    with tempfile.TemporaryDirectory(prefix="shiri-offset-native-") as temporary:
        directory = Path(temporary)
        for kind in ("offset", "buffer", "request"):
          for after in (True, False):
            def native(name, after=after):
                if source and after:
                    return (source / name).read_text()
                return extract.patch_image(PATCH, name, after=after)
            name = kind + ("_postimage" if after else "_preimage")
            unit, executable = directory / (name + ".c"), directory / name
            unit.write_text(build_fixture(native) if kind == "offset" else
                            build_buffer_fixture(native, after=after) if kind == "buffer" else
                            build_request_fixture(native))
            # Upstream's unsigned <0 comparison is the reproduced fault, not a
            # warning allowed in the repaired code. Preimage-only case helpers
            # are unused, whereas every repaired test compiles with -Werror.
            extra = [] if after else ["-DTEST_PREIMAGE", "-Wno-type-limits", "-Wno-unused-function"]
            subprocess.run([compiler, *flags, *extra, str(unit), "-o", str(executable)],
                           check=True, capture_output=True, text=True, timeout=30)
            completed = subprocess.run([str(executable)], check=True, capture_output=True, text=True,
                                       timeout=30, env=environment)
            results.append(completed.stdout.strip())
            if kind == "buffer" and not after and sanitize:
                for argument in ("--offset-overflow", "--alsa-overflow"):
                    failed = subprocess.run([str(executable), argument], capture_output=True, text=True,
                                            timeout=30, env=environment)
                    if failed.returncode == 0 or "signed integer overflow" not in failed.stderr:
                        raise RuntimeError(f"Actual preimage did not reproduce {argument}: {failed.stderr}")
                results.append("Actual preimage UBSan failures: RAOP int sample multiplication and ALSA int prebuffer byte multiplication")
            if kind == "request" and not after and sanitize:
                failed = subprocess.run([str(executable), "--cseq-overflow"], capture_output=True, text=True,
                                        timeout=30, env=environment)
                if failed.returncode == 0 or "signed integer overflow" not in failed.stderr:
                    raise RuntimeError(f"Actual RAOP CSeq preimage did not overflow: {failed.stderr}")
                results.append("Actual preimage UBSan failure: RAOP INT_MAX request sequence increment")
    return {"ok": True, "sanitized": sanitize, "actual_source": source is not None, "results": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--compiler")
    parser.add_argument("--no-sanitizers", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(run_tests(source=args.source, compiler=args.compiler,
                                   sanitize=not args.no_sanitizers), indent=2))
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.stdout + exc.stderr) from None
