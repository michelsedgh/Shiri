"""Text request fades precede PCM and remain tied to the admitted voice."""
import asyncio

import pytest

from shiri.rpc import RpcError
from shiri.runtime.audio import AudioWorker
import test_pcm_speech
from test_pcm_speech import ending, media, opening
from test_speech_startup import native_controller

ready_worker = test_pcm_speech.ready_worker


def text_opening():
    return {**opening(), "duck_on_prepare": True}


async def test_text_fade_waits_for_exact_begin_but_not_first_generated_audio():
    native, writer, client, overlay = await native_controller()
    worker = AudioWorker(native.mixer, native=native)
    pending = asyncio.create_task(worker.dispatch("speech", text_opening()))
    try:
        await asyncio.wait_for(client.prepare_entered.wait(), 1)
        await worker.tick(.05)
        assert not overlay.owner and not overlay.controls and not overlay.sent
        client.connect()
        client.first_mix()
        prepared = await asyncio.wait_for(pending, 1)
        assert overlay.envelope == (300, 600)
        assert overlay.controls[-1] == (True, .17)
        assert prepared["duck_requested_monotonic_ns"] > 0
        assert prepared["duck_attack_ms"] == 300 and prepared["duck_release_ms"] == 600
        assert not overlay.sent and not writer.packets
        assert prepared["backend_startup_steps"]["begin"]["requests"] == 1
        # Renew the explicit request even though no audible PCM exists yet.
        for _ in range(8):
            await worker.tick(.05)
        assert all(active for active, _gain in overlay.controls)
        await worker.dispatch("speech", ending(prepared, action="close"))
        assert worker.session is None and overlay.owner is None
        assert client.requests[-1][2]["action"] == "cancel"
        assert not worker._disposals
    finally:
        await worker.close()
        await asyncio.gather(pending, return_exceptions=True)


async def test_text_keeps_one_envelope_through_quiet_generated_pcm(ready_worker):
    worker, _native, _writer, _client, overlay = ready_worker
    prepared = await worker.dispatch("speech", text_opening())
    await worker.dispatch("speech", media(prepared, value=0))
    await worker.tick(.05)
    assert worker.session.last_audible == 0
    assert overlay.controls[-1] == (True, .17)
    await worker.dispatch("speech", ending(prepared))
    assert worker.session is None and overlay.owner is None


async def test_failed_first_fade_retires_the_exact_voice_and_allows_successor(ready_worker):
    worker, _native, _writer, client, overlay = ready_worker
    original = overlay.control
    overlay.control = lambda *_args: False
    with pytest.raises(RpcError, match="music fade"):
        await worker.dispatch("speech", text_opening())
    assert worker.session is None and overlay.owner is None and not worker._disposals
    assert client.requests[-1][2]["action"] == "cancel"
    overlay.control = original
    successor = await worker.dispatch("speech", text_opening())
    assert successor["duck_requested_monotonic_ns"] > 0


async def test_prepared_replay_cannot_switch_the_admitted_fade_policy(ready_worker):
    worker, _native, _writer, _client, overlay = ready_worker
    prepared = await worker.dispatch("speech", text_opening())
    with pytest.raises(RpcError, match="Another speech producer"):
        await worker.dispatch("speech", opening())
    assert await worker.dispatch("speech", text_opening()) == prepared
    assert overlay.envelope == (300, 600)


@pytest.mark.parametrize("flag", [None, 1, "yes", [], {}])
async def test_invalid_request_fade_policy_never_admits_a_voice(ready_worker, flag):
    worker, _native, _writer, client, overlay = ready_worker
    before = len(client.requests)
    with pytest.raises(RpcError, match="boolean"):
        await worker.dispatch("speech", {**opening(), "duck_on_prepare": flag})
    assert worker.session is None and overlay.owner is None
    assert len(client.requests) == before and not overlay.controls
