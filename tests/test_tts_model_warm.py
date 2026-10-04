"""Explicit model hints discard audio and join decoder reset before speech."""
import asyncio
import base64
import json
import multiprocessing
from pathlib import Path
import time
from types import SimpleNamespace

import httpx
import pytest

from shiri.tts.models import DEFAULT_MODEL_ID, QWEN_MODEL_ID
from shiri.tts.worker import ModelWorker, WARM_RECEIPT_LIMIT, create_worker_app

TOKEN = "model-warm-test-private-credential-1234567890"
ID = "a" * 32
SUCCESSOR_PCM = b"\x00\x20" * 960


def actual_backend_child(connection, cancel, entered, reset_gate, first_gate, reset_fails):
    """Production child/backend/resampler with a controlled non-MLX decoder."""
    import numpy as np
    import shiri.tts.backend as backend_module
    from shiri.tts.models import get_model
    from shiri.tts.worker import _child

    class Decoder:
        dirty = False

        def reset_streaming_state(self):
            entered.set()
            if not reset_gate.wait(5):
                raise RuntimeError("test reset gate timed out")
            if reset_fails:
                raise RuntimeError("controlled decoder reset failure")
            self.dirty = False

    decoder = Decoder()

    class Model:
        speech_tokenizer = SimpleNamespace(decoder=decoder)

        def generate(self, *, text, **options):
            if decoder.dirty:
                raise RuntimeError("previous decoder state contaminated successor")
            decoder.dirty = True
            if text == "Hi.":
                while not first_gate.is_set() and not cancel.is_set():
                    time.sleep(.001)
                yield SimpleNamespace(audio=np.full(960, .125, dtype=np.float32), sample_rate=48000,
                                      token_count=1)
                while not cancel.is_set():
                    time.sleep(.001)
                yield SimpleNamespace(audio=np.full(960, .75, dtype=np.float32), sample_rate=48000,
                                      token_count=1)
            else:
                if cancel.is_set():
                    raise RuntimeError("old cancellation contaminated successor")
                yield SimpleNamespace(audio=np.full(960, .25, dtype=np.float32), sample_rate=48000,
                                      token_count=1)

    engine = backend_module.MLXBackend(get_model(QWEN_MODEL_ID), Model(), Path("/no-assets-needed"))
    engine.prewarm = lambda: {"quiet_test": True}
    backend_module.load_backend = lambda *args, **kwargs: engine
    _child(connection, get_model(QWEN_MODEL_ID), None, False, cancel)


async def spawn_worker(*, reset_blocked=False, first_blocked=False, reset_fails=False):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    cancel, entered, reset_gate, first_gate = (context.Event() for _ in range(4))
    if not reset_blocked:
        reset_gate.set()
    if not first_blocked:
        first_gate.set()
    process = context.Process(target=actual_backend_child,
                              args=(child, cancel, entered, reset_gate, first_gate, reset_fails), daemon=True)
    process.start()
    child.close()
    worker = ModelWorker()
    worker.process, worker.connection, worker.cancel_event = process, parent, cancel
    worker.model_id = QWEN_MODEL_ID
    message = await worker._receive(5)
    assert message["type"] == "ready"
    worker.state, worker.warmup = "ready", message["warmup"]
    return worker, entered, reset_gate, first_gate


async def settled(worker, request_id=ID):
    async with asyncio.timeout(4):
        while True:
            result = worker.warm_status(request_id)
            if result and result["state"] != "preparing":
                # completed is claimed only after reset/retirement is recorded.
                if result["total_ms"] is not None:
                    return result
            await asyncio.sleep(.005)


async def successor(worker):
    prepared = await worker.reserve({"model_id": QWEN_MODEL_ID, "text": "successor"})
    records = [json.loads(line) async for line in worker.stream(prepared)]
    assert [record["type"] for record in records] == ["format", "pcm", "end"]
    assert base64.b64decode(records[1]["pcm_base64"]) == SUCCESSOR_PCM


async def test_real_backend_first_frame_warm_retains_child_and_clean_successor():
    worker, _, _, _ = await spawn_worker()
    process, connection = worker.process, worker.connection
    try:
        first = await worker.warm(QWEN_MODEL_ID, ID)
        assert first["accepted"] and first["state"] == "preparing"
        result = await settled(worker)
        assert result["state"] == "completed" and result["first_pcm_ms"] is not None
        assert result["discarded_received_audio_ms"] == 20 and not result["room_audio_sent"]
        assert not result["latency_guaranteed"]
        assert worker.process is process and worker.connection is connection
        assert worker.state == "ready" and not worker.lock.locked()
        await successor(worker)
        assert worker.process is process and worker.state == "ready"
        cached = await worker.warm(QWEN_MODEL_ID, ID)
        assert cached["state"] == "completed" and cached["first_pcm_ms"] == result["first_pcm_ms"]
        with pytest.raises(ValueError, match="different model"):
            await worker.warm(DEFAULT_MODEL_ID, ID)
    finally:
        await worker.close()


async def test_speech_priority_during_reset_joins_same_owner_without_killing_child():
    worker, entered, gate, _ = await spawn_worker(reset_blocked=True)
    process, connection = worker.process, worker.connection
    generation = None
    try:
        await worker.warm(QWEN_MODEL_ID, ID)
        assert await asyncio.to_thread(entered.wait, 2)
        pending = worker.warm_status(ID)
        assert pending["state"] == "preparing" and pending["first_pcm_ms"] is not None
        assert pending["total_ms"] is None
        generation = asyncio.create_task(successor(worker))
        await asyncio.sleep(.03)
        assert not generation.done() and worker.state == "busy" and worker.lock.locked()
        assert worker.process is process
        gate.set()
        await asyncio.wait_for(generation, 3)
        assert worker.process is process and worker.connection is connection
        assert worker.warm_status(ID)["reason"] == "speech_priority"
        assert worker.state == "ready"
    finally:
        gate.set()
        if generation is not None:
            await asyncio.gather(generation, return_exceptions=True)
        await worker.close()


async def test_priority_before_prime_task_first_turn_retires_entered_iterator():
    worker, _, _, _ = await spawn_worker(first_blocked=True)
    process = worker.process
    try:
        await worker.warm(QWEN_MODEL_ID, ID)
        # No intervening scheduler turn: the prime task has not started yet.
        await successor(worker)
        result = worker.warm_status(ID)
        assert result["state"] == "cancelled" and result["reason"] == "speech_priority"
        assert worker.process is process and worker.state == "ready" and not worker.lock.locked()
    finally:
        await worker.close()


async def test_reset_failure_cannot_claim_primed_readiness_or_reuse_child():
    worker, _, _, _ = await spawn_worker(reset_fails=True)
    try:
        await worker.warm(QWEN_MODEL_ID, ID)
        result = await settled(worker)
        assert result["state"] == "failed"
        assert worker.state != "ready" and worker.process is None and not worker.lock.locked()
        with pytest.raises(ValueError, match="one generation"):
            await worker.reserve({"model_id": QWEN_MODEL_ID, "text": "successor"})
    finally:
        await worker.close()


async def test_cancel_is_owned_and_invalid_speech_cannot_displace_prime():
    worker, _, _, _ = await spawn_worker(first_blocked=True)
    try:
        await worker.warm(QWEN_MODEL_ID, ID)
        with pytest.raises(ValueError):
            await worker.reserve({"model_id": QWEN_MODEL_ID, "text": ""})
        assert await worker.cancel_warm("b" * 32) is None
        assert worker.state == "busy" and worker.operation == "warming"
        await worker.cancel_warm(ID)
        prepared = await worker.reserve({"model_id": QWEN_MODEL_ID, "text": "successor"})
        iterator = worker.stream(prepared)
        await iterator.__anext__()
        assert (await worker.cancel_warm(ID))["state"] == "cancelled"
        assert worker.operation == "speech" and worker.lock.locked()
        records = [json.loads(line) async for line in iterator]
        assert base64.b64decode(records[0]["pcm_base64"]) == SUCCESSOR_PCM
    finally:
        await worker.close()


async def test_busy_or_mismatched_hint_does_not_load_queue_or_displace_speech():
    worker, _, _, _ = await spawn_worker()
    try:
        mismatch = await worker.warm(DEFAULT_MODEL_ID, ID)
        assert not mismatch["accepted"] and mismatch["reason"] == "model_mismatch"
        prepared = await worker.reserve({"model_id": QWEN_MODEL_ID, "text": "successor"})
        skipped = await worker.warm(QWEN_MODEL_ID, "b" * 32)
        assert not skipped["accepted"] and skipped["reason"] == "busy"
        assert worker.operation == "speech" and worker.lock.locked()
        records = [json.loads(line) async for line in worker.stream(prepared)]
        assert base64.b64decode(records[1]["pcm_base64"]) == SUCCESSOR_PCM
        assert worker.model_id == QWEN_MODEL_ID
    finally:
        await worker.close()


async def test_receipts_are_bounded_expire_and_preserve_active_owner():
    worker, _, _, _ = await spawn_worker(first_blocked=True)
    try:
        await worker.warm(QWEN_MODEL_ID, ID)
        for index in range(WARM_RECEIPT_LIMIT - 1):
            result = await worker.warm(QWEN_MODEL_ID, f"{index:032x}")
            assert not result["accepted"]
        assert len(worker.warm_receipts) == WARM_RECEIPT_LIMIT
        assert worker.warm_status(ID)["state"] == "preparing"
        original = worker.warm_status("0" * 32)
        with pytest.raises(ValueError, match="receipt capacity"):
            await worker.warm(QWEN_MODEL_ID, "f" * 32)
        retried = await worker.warm(QWEN_MODEL_ID, "0" * 32)
        assert retried["began_unix_ms"] == original["began_unix_ms"]
        assert retried["state"] == "skipped" and not retried["accepted"]
        assert worker.warm_status("f" * 32) is None
        for record in worker.warm_receipts.values():
            if record.request_id != ID:
                record.ended = time.monotonic() - 601
        assert worker.warm_status("0" * 32) is None
        assert len(worker.warm_receipts) == 1 and worker.warm_status(ID) is not None
    finally:
        await worker.close()


async def test_first_pcm_deadline_is_bounded_and_retains_clean_decoder_after_ack(monkeypatch):
    import shiri.tts.worker as module
    monkeypatch.setattr(module, "WARM_FIRST_PCM_SECONDS", .025)
    worker, _, _, _ = await spawn_worker(first_blocked=True)
    process = worker.process
    try:
        await worker.warm(QWEN_MODEL_ID, ID)
        result = await settled(worker)
        assert result["state"] == "failed" and result["reason"] == "first_pcm_timeout"
        assert "deadline" in result["error"] and result["total_ms"] < 1500
        assert worker.process is process and worker.state == "ready" and not worker.lock.locked()
        await successor(worker)
    finally:
        await worker.close()


async def test_authenticated_warm_api_status_and_owned_cancel():
    worker, _, _, _ = await spawn_worker(first_blocked=True)
    app = create_worker_app(token=TOKEN, worker=worker)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker.test") as client:
            body = {"model_id": QWEN_MODEL_ID, "request_id": ID}
            assert (await client.post("/v1/warm", json=body)).status_code == 401
            client.headers["Authorization"] = "Bearer " + TOKEN
            bad = await client.post("/v1/warm", json={**body, "text": "arbitrary work is forbidden"})
            assert bad.status_code == 409 and worker.state == "ready"
            response = await client.post("/v1/warm", json=body)
            assert response.status_code == 202 and response.json()["worker"]["operation"] == "warming"
            status = await client.get("/v1/warm/" + ID)
            assert status.status_code == 200 and status.json()["warm"]["request_id"] == ID
            stale = await client.post("/v1/warm/cancel", json={"request_id": "b" * 32})
            assert stale.status_code == 404 and worker.lock.locked()
            cancel = await client.post("/v1/warm/cancel", json={"request_id": ID})
            assert cancel.status_code == 200 and cancel.json()["warm"]["state"] == "cancelled"
            assert worker.state == "ready" and not worker.lock.locked()
    finally:
        await worker.close()
