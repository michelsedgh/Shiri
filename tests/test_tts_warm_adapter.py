"""Quiet hints preserve worker ownership and report admission honestly."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from shiri.tts.coordinator import TextSpeechCoordinator, TextSpeechRequest
from shiri.tts.models import DEFAULT_MODEL_ID


@pytest.fixture
async def adapter():
    state = SimpleNamespace(calls=[], accepted=True, bad=None, status=202)

    def respond(request):
        state.calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"worker": {"state": "ready", "model_id": DEFAULT_MODEL_ID}, "models": []})
        import json
        body = json.loads(request.content)
        warm = {**body, "accepted": state.accepted, "state": "preparing" if state.accepted else "skipped"}
        if state.bad:
            warm.update(state.bad)
        return httpx.Response(state.status, json={"warm": warm, "worker": {"state": "busy"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    state.coordinator = TextSpeechCoordinator(None, worker_url="http://worker.test", client=client)
    yield state
    await client.aclose()


async def test_presence_observes_without_starting_generation(adapter):
    result = await adapter.coordinator.warm(DEFAULT_MODEL_ID, purpose="presence")
    assert result["state"] == "observed"
    assert [(call.method, call.url.path) for call in adapter.calls] == [("GET", "/v1/models")]


async def test_interaction_requests_owned_prime_without_catalog_roundtrip(adapter):
    result = await adapter.coordinator.warm(DEFAULT_MODEL_ID, purpose="interaction")
    assert result["state"] == "requested" and len(result["request_id"]) == 32
    assert [(call.method, call.url.path) for call in adapter.calls] == [("POST", "/v1/warm")]


async def test_skipped_busy_hint_is_not_reported_as_preparing(adapter):
    adapter.accepted = False
    assert (await adapter.coordinator.warm(DEFAULT_MODEL_ID, purpose="interaction"))["state"] == "skipped"


async def test_active_speech_has_priority_without_worker_request(adapter):
    adapter.coordinator.active = "owned-speech"
    assert (await adapter.coordinator.warm(DEFAULT_MODEL_ID, purpose="interaction"))["state"] == "busy"
    assert not adapter.calls


async def test_speech_admission_does_not_wait_for_pending_model_hint_response():
    warm_entered, warm_response, generation_entered = (asyncio.Event() for _ in range(3))

    async def respond(request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"worker": {"state": "ready", "model_id": DEFAULT_MODEL_ID}, "models": []})
        if request.url.path == "/v1/warm":
            body = json.loads(request.content)
            warm_entered.set()
            await warm_response.wait()
            return httpx.Response(202, json={"warm": {**body, "accepted": True, "state": "preparing"}})
        assert request.url.path == "/v1/generate"
        generation_entered.set()
        await asyncio.Event().wait()  # Cancellation retires this silent fake request.

    async def get_room(*_arguments):
        return SimpleNamespace(id="test-room", enabled=True, speakers=["silent-test-output"])

    async def speech(*_arguments):
        return {"ok": True, "stream_id": "owned-silent-test-stream"}

    service = SimpleNamespace(_mutation=asyncio.Lock(), _store=get_room, speech=speech)
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    coordinator = TextSpeechCoordinator(service, worker_url="http://worker.test", client=client)
    hint = asyncio.create_task(coordinator.warm(DEFAULT_MODEL_ID, purpose="interaction"))
    try:
        await asyncio.wait_for(warm_entered.wait(), 1)
        job = await asyncio.wait_for(coordinator.admit(
            TextSpeechRequest(request_id="b"*32, text="Actual speech takes priority."),
            room_id="test-room"), .5)
        assert coordinator.active == job["id"] and not hint.done() and not warm_response.is_set()
        await asyncio.wait_for(generation_entered.wait(), 1)
        result = await asyncio.wait_for(coordinator.cancel(job["id"]), 1)
        assert result["state"] == "cancelled" and coordinator.active is None
        assert result["metrics"]["worker_cleanup_confirmed"] is True
    finally:
        warm_response.set()
        await asyncio.gather(hint, return_exceptions=True)
        await coordinator.close()


@pytest.mark.parametrize("bad", [
    {"request_id": "a"*32}, {"model_id": "other"}, {"accepted": 1},
    {"state": "ready"}, {"state": "skipped", "accepted": True},
])
async def test_invalid_warm_identity_or_state_cannot_claim_readiness(adapter, bad):
    adapter.bad = bad
    assert (await adapter.coordinator.warm(DEFAULT_MODEL_ID, purpose="interaction"))["state"] == "unavailable"


async def test_unconfigured_generation_does_not_block_room_readiness():
    coordinator = TextSpeechCoordinator(None)
    try:
        assert (await coordinator.warm(DEFAULT_MODEL_ID, purpose="interaction"))["state"] == "disabled"
    finally:
        await coordinator.client.aclose()
