"""Replay actual worker pacing through the exact maintained native speech mixer."""
import asyncio
import importlib.util
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
from types import SimpleNamespace

import pytest

from shiri.rpc import RpcError
from shiri.runtime.pcm_speech import MAX_LEAD_NS
from test_pcm_speech import opening, ready_worker as ready_worker

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = 1_000_000_000


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tests/native" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def native_replay(tmp_path_factory):
    compiler = shutil.which("clang") or shutil.which("cc")
    if compiler is None:
        pytest.skip("Native pacing qualification requires a C compiler")
    directory = tmp_path_factory.mktemp("speech-delivery-native")
    load("check_speech_owner").assemble(directory)
    for layer in ("speaker-balance", "duck-envelope"):
        subprocess.run(["git", "apply", "--include=src/shiri_speech.c", "--include=src/shiri_speech.h",
                        str(ROOT / f"install/patches/owntone-29.3-{layer}.patch")],
                       cwd=directory, check=True, capture_output=True, timeout=10)
    source = directory / "src/shiri_speech.c"
    # Later warm-lease changes do not modify this media module. Check the exact
    # current maintained bytes, rather than duplicating its queue arithmetic.
    assert hashlib.sha256(source.read_bytes()).hexdigest() == load("check_duck_envelope").SOURCE_SHA["src/shiri_speech.c"]
    binary = directory / "delivery"
    subprocess.run([compiler, "-std=gnu11", "-Wall", "-Wextra", "-Werror",
                    "-Wno-unused-function", "-Wno-unused-variable", "-O1", "-g",
                    "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                    f'-DSHIRI_SPEECH_SOURCE="{source}"',
                    str(ROOT / "tests/native/test_speech_delivery_lead.c"), "-lm", "-o", str(binary)],
                   check=True, capture_output=True, timeout=60)
    return binary


async def capture_schedule(worker, monkeypatch, *, sizes, stall_ms=0):
    """Run the real admitted worker session with only its clock/transport faked."""
    import shiri.runtime.audio as module
    sessions, emitted = [], []
    clock = [ORIGIN]

    async def capture(_reader, _writer, session):
        sessions.append(session)

    monkeypatch.setattr(module, "serve_speech", capture)
    admission = await worker.open_speech(opening())
    await admission.run(None, None)
    stream = worker.session.pcm
    stream.now_ns = lambda: clock[0]
    original_push = worker.mixer.push_speech

    def push(data, samples):
        emitted.append((clock[0], samples))
        original_push(data, samples)

    async def sleep(delay):
        target = clock[0] + round(delay * 1e9)
        if stall_ms and clock[0] < ORIGIN + 200_000_000 <= target:
            target += stall_ms * 1_000_000
        clock[0] = target

    # Patch this module's names, not the global asyncio/time modules used by
    # native preparation and cleanup ownership.
    with monkeypatch.context() as local:
        local.setattr(module, "time", SimpleNamespace(monotonic_ns=lambda: clock[0], monotonic=lambda: clock[0]/1e9))
        local.setattr(module, "asyncio", SimpleNamespace(**{**vars(module.asyncio), "sleep": sleep}))
        local.setattr(worker.mixer, "push_speech", push)
        frame = 0
        for sequence, size in enumerate(sizes, 1):
            pcm = struct.pack("<h", 100) * size
            await sessions[0].send_pcm(pcm, sequence, frame)
            frame += size
            assert stream.next_ns - clock[0] <= MAX_LEAD_NS
            assert stream.first_admitted_ns == ORIGIN
    await admission.close()
    return emitted


@pytest.mark.parametrize("stall_ms", [0, 40, 60, 80, 100, 120])
async def test_bounded_lead_preserves_native_sample_continuity_through_short_delivery_stalls(
    ready_worker, monkeypatch, native_replay, stall_ms,
):
    worker, *_ = ready_worker
    schedule = await capture_schedule(worker, monkeypatch, sizes=[960] * 50, stall_ms=stall_ms)
    result = await asyncio.to_thread(subprocess.run, [str(native_replay)],
                                    input="".join(f"{at} {samples}\n" for at, samples in schedule),
                                    text=True, capture_output=True, check=True, timeout=10)
    evidence = json.loads(result.stdout)
    assert evidence["onset_ns"] == 20_000_000
    assert evidence["ordered_frames"] == 48000
    assert evidence["maximum_queue_frames"] <= 6720 < 12000
    if stall_ms <= 100:
        assert evidence["gap_frames"] == evidence["underflow_frames"] == 0
    else:
        # Prove the instrument detects a pause beyond available headroom;
        # bounded buffering does not promise continuity across arbitrary gaps.
        assert evidence["gap_frames"] == evidence["underflow_frames"] == 960


async def test_partial_chunks_cap_the_packet_end_and_start_without_extra_wait(ready_worker, monkeypatch):
    worker, *_ = ready_worker
    sizes = [960, 960, 960, 928, 960, 1, 31, 960]
    emitted = await capture_schedule(worker, monkeypatch, sizes=sizes)
    frames = 0
    for (at, size) in emitted:
        frames += size
        assert at == ORIGIN + max(0, frames * 1_000_000_000 // 48000 - MAX_LEAD_NS)
    assert emitted[0][0] == ORIGIN


async def test_leading_reserve_does_not_reanchor_a_missed_nominal_calendar(ready_worker, monkeypatch):
    worker, *_ = ready_worker
    with pytest.raises(RpcError, match="missed its admitted sample calendar"):
        await capture_schedule(worker, monkeypatch, sizes=[960] * 50, stall_ms=240)
