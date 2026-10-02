"""Actual Python owner consumers under gated authenticated replies.

All control replies and datagram writes are in memory. Real RTC SDP parsing and
AV resampling are exercised without listeners, peers on a network, or devices.
Backend media retirement itself is proved by the separate actual-C regressions.
"""
from array import array
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from shiri.rpc import RpcError
from shiri.runtime.audio import AudioWorker
from shiri.runtime.backend import OwnToneRejected
from shiri.runtime.native import NativeHandle
from shiri.runtime.speech_output import CONTROL, HEADER, HEADER_BYTES, PCM, SpeechOutput
from test_audio import FakePeer, request
from test_native_audio import begin, pcm
from test_speech_resampler_tail import Track, reference
from test_speech_startup import native_controller


class ReplyGates:
    """Hold replies after the delegate's authenticated handler has run."""
    def __init__(self, delegate):
        self.delegate = delegate
        self.begin_executed = asyncio.Event()
        self.begin_reply = asyncio.Event()
        self.begin_reply.set()
        self.cancel_executed = asyncio.Event()
        self.cancel_reply = asyncio.Event()
        self.cancel_reply.set()
        self.begin_dependency_cancelled = False
        self.begin_failure = None
        self.corrupt_begin = False
        self.begin_outcomes = []
        self.events = []

    async def request(self, method, path, *, json):
        action = json.get("action") if path == "/api/player/shiri-speech-ready" else None
        reply = await self.delegate.request(method, path, json=json)
        self.events.append((action, json.get("speech_id"), "executed"))
        if action == "begin":
            self.begin_executed.set()
            try:
                await self.begin_reply.wait()
            except asyncio.CancelledError:
                self.begin_dependency_cancelled = True
                raise
            if self.begin_failure is not None:
                raise self.begin_failure
            if self.corrupt_begin:
                reply = {**reply, "speech_id": uuid4().hex}
            if self.begin_outcomes:
                outcome = self.begin_outcomes.pop(0)
                if isinstance(outcome, BaseException):
                    raise outcome
                if outcome is False:
                    reply = {**json, "connected": False, "ready": False,
                             "prepared_monotonic_ns": 0, "mixed_monotonic_ns": 0, "output_count": 0}
        elif action == "cancel":
            self.cancel_executed.set()
            await self.cancel_reply.wait()
        self.events.append((action, json.get("speech_id"), "reply"))
        return reply

    def unblock(self):
        self.begin_reply.set()
        self.cancel_reply.set()


@pytest.fixture
async def valid_sdp():
    from aiortc import RTCConfiguration, RTCPeerConnection
    peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    peer.addTransceiver("audio", direction="sendonly")
    try:
        yield (await peer.createOffer()).sdp
    finally:
        await peer.close()


@asynccontextmanager
async def owned_worker(*, peer=None, music=False, allow_failed_cleanup=False, idle_seconds=1):
    native, writer, client, overlay = await native_controller()
    gates = ReplyGates(client)
    native.client = gates
    client.connect()
    client.first_mix()
    handle = None
    if music:
        handle = NativeHandle(native)
        await native.begin(begin(), handle)
    peer = peer or FakePeer()
    worker = AudioWorker(native.mixer, native=native, peer_factory=lambda: peer,
                         idle_seconds=idle_seconds)
    try:
        yield worker, native, writer, client, overlay, gates, peer, handle
    finally:
        gates.unblock()
        try:
            await asyncio.wait_for(worker.close(), 1)
        except RpcError:
            if not allow_failed_cleanup or worker._cleanup_error is None:
                raise


def speech_requests(client, action=None):
    return [body for _method, path, body in client.requests
            if path == "/api/player/shiri-speech-ready"
            and (action is None or body["action"] == action)]


def source_requests(client):
    return [record for record in client.requests if record[1] != "/api/player/shiri-speech-ready"]


async def settled(task):
    await asyncio.wait_for(asyncio.wait({task}), 1)


class BackoffGates:
    """Gate only this production module's sleep, never global asyncio."""
    def __init__(self, monkeypatch):
        import shiri.runtime.speech_startup as startup
        original = startup.asyncio
        self.entered = asyncio.Queue()
        self.replies = asyncio.Queue()
        self.waits = 0

        async def sleep(delay):
            if delay == 0.01:
                self.waits += 1
                self.entered.put_nowait(self.waits)
                await self.replies.get()
            else:
                await original.sleep(delay)

        namespace = dict(vars(original))
        namespace["sleep"] = sleep
        monkeypatch.setattr(startup, "asyncio", SimpleNamespace(**namespace))

    async def reached(self, number):
        assert await asyncio.wait_for(self.entered.get(), 1) == number

    def resume(self):
        self.replies.put_nowait(None)


async def test_definitive_not_ready_tail_retries_share_one_begin_task_and_gate_pcm(valid_sdp, monkeypatch):
    backoff = BackoffGates(monkeypatch)
    async with owned_worker(music=True) as (worker, native, _writer, client, overlay, gates, _peer, handle):
        gates.begin_outcomes = [False, False, True]
        program = (handle.token, native.mixer.route, native.operation_generation,
                   native.mixer.output_buffer_ms, native.mixer.relay_delay_ns, source_requests(client))
        offer = asyncio.create_task(worker.dispatch("speech", request(valid_sdp)))
        try:
            await backoff.reached(1)
            prep = native.mixer.speech_preparation
            task = prep.begin_task
            assert prep.begin_reply["ready"] is False and not prep.begin_inflight
            first = array("h", [1000] * 960).tobytes()
            second = array("h", [2300] * 960).tobytes()
            native.mixer.set_speech_gain(0.1)
            native.mixer.push_speech(first, 960)
            native.mixer.set_speech_gain(0.8)
            native.mixer.push_speech(second, 960)
            assert prep.frames == 1920 and not overlay.sent and overlay.owner is None
            backoff.resume()
            await backoff.reached(2)
            assert prep.begin_task is task and not task.done() and not offer.done()
            assert prep.begin_reply["ready"] is False and not prep.begin_inflight
            assert len(speech_requests(client, "begin")) == 2 and not overlay.sent
            backoff.resume()
            await asyncio.wait_for(offer, 1)
            assert prep.begin_task is task and task.done() and task.result()["ready"] is True
            assert len(speech_requests(client, "begin")) == 3
            assert {body["speech_id"] for body in speech_requests(client, "begin")} == {prep.speech_id}
            assert [(data, frames, gain) for data, frames, gain, _ns in overlay.sent] == [
                (first, 960, 0.1), (second, 960, 0.8),
            ]
            assert prep.backend_reply == prep.begin_reply and prep.backend_reply["action"] == "begin"
            assert prep.phase == "ready" and not prep.frames
            assert not speech_requests(client, "cancel") and not speech_requests(client, "finish")
            assert (handle.token, native.mixer.route, native.operation_generation,
                    native.mixer.output_buffer_ms, native.mixer.relay_delay_ns, source_requests(client)) == program
        finally:
            backoff.resume()
            gates.unblock()
            await settled(offer)


@pytest.mark.parametrize("change", ["cancel", "source"])
async def test_not_ready_backoff_retirement_preserves_prior_tail_without_cancel(valid_sdp, monkeypatch, change):
    backoff = BackoffGates(monkeypatch)
    async with owned_worker(music=True) as (worker, native, _writer, client, overlay, gates, _peer, handle):
        gates.begin_outcomes = [False]
        offer = asyncio.create_task(worker.dispatch("speech", request(valid_sdp)))
        try:
            await backoff.reached(1)
            session = worker.session
            prep = native.mixer.speech_preparation
            assert prep.begin_reply["ready"] is False and not prep.begin_inflight
            if change == "cancel":
                offer.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(offer, 0.2)
            else:
                await native.actor.end_native(handle.token)
                handle = NativeHandle(native)
                await native.begin(begin(), handle)
            program = (handle.token, native.mixer.route, native.operation_generation, source_requests(client))
            backoff.resume()
            await settled(offer)
            if change == "source":
                with pytest.raises(RpcError):
                    offer.result()
            await asyncio.wait_for(worker._dispose(session), 1)
            assert session.disposed and prep.retired and prep.retirement_reply is None
            assert len(speech_requests(client, "begin")) == 1
            assert not speech_requests(client, "cancel") and not speech_requests(client, "finish")
            assert not overlay.sent and overlay.owner is None and not worker._cleanup_error
            assert (handle.token, native.mixer.route, native.operation_generation, source_requests(client)) == program
            assert native.actor.owns(handle.token) and not handle.closed
        finally:
            backoff.resume()
            gates.unblock()
            await settled(offer)


async def test_deadline_in_known_not_ready_backoff_allows_fresh_offer_without_cancel(valid_sdp, monkeypatch):
    monkeypatch.setattr("shiri.runtime.speech_startup.SETUP_SECONDS", 0.03)
    backoff = BackoffGates(monkeypatch)
    async with owned_worker(music=True) as (worker, native, _writer, client, overlay, gates, _peer, handle):
        gates.begin_outcomes = [False]
        program = (handle.token, native.mixer.route, native.operation_generation, source_requests(client))
        offer = asyncio.create_task(worker.dispatch("speech", request(valid_sdp)))
        try:
            await backoff.reached(1)
            prep = native.mixer.speech_preparation
            assert prep.begin_reply["ready"] is False and not prep.begin_inflight
            await settled(offer)
            with pytest.raises(asyncio.TimeoutError):
                offer.result()
            assert prep.begin_task.done() and prep.retired and prep.retirement_reply is None
            assert len(speech_requests(client, "begin")) == 1
            assert not speech_requests(client, "cancel") and not speech_requests(client, "finish")
            assert not overlay.sent and overlay.owner is None and not worker._cleanup_error
            assert not worker._disposals and worker.session is None
            assert (handle.token, native.mixer.route, native.operation_generation, source_requests(client)) == program
            worker.peer_factory = FakePeer
            await worker.dispatch("speech", request(valid_sdp, session="retry", request_id="retry-offer"))
            assert native.mixer.speech_preparation.speech_id != prep.speech_id
            assert native.actor.owns(handle.token) and not handle.closed
        finally:
            backoff.resume()
            gates.unblock()
            await settled(offer)


async def test_uncertain_attempt_after_known_not_ready_is_not_retried(valid_sdp, monkeypatch):
    backoff = BackoffGates(monkeypatch)
    async with owned_worker(allow_failed_cleanup=True) as (worker, native, _writer, client, overlay, gates, _peer, _handle):
        gates.begin_outcomes = [False, OSError("Second BEGIN reply lost after dispatch"), True]
        offer = asyncio.create_task(worker.dispatch("speech", request(valid_sdp)))
        try:
            await backoff.reached(1)
            prep = native.mixer.speech_preparation
            task = prep.begin_task
            assert prep.begin_reply["ready"] is False
            backoff.resume()
            await settled(offer)
            with pytest.raises(RpcError, match="outcome is uncertain"):
                offer.result()
            assert prep.begin_task is task and prep.begin_inflight
            assert prep.begin_reply["ready"] is False and gates.begin_outcomes == [True]
            assert len(speech_requests(client, "begin")) == 2
            assert speech_requests(client, "cancel") == [{**prep.body, "action": "cancel"}]
            assert prep.retirement_reply["speech_id"] == prep.speech_id
            assert worker._cleanup_error and worker._disposals and not overlay.sent
            with pytest.raises(RpcError, match="Earlier speech resources could not be released"):
                await worker.dispatch("speech", request(valid_sdp, session="successor"))
        finally:
            backoff.resume()
            gates.unblock()
            await settled(offer)


async def test_created_undispatched_begin_does_not_claim_uncertainty_or_cancel(valid_sdp, monkeypatch):
    import shiri.runtime.speech_startup as startup
    original = startup.asyncio
    created, resume = asyncio.Event(), asyncio.Event()

    def create_task(coroutine, *args, **kwargs):
        if kwargs.get("name") != "speech-voice-begin":
            return original.create_task(coroutine, *args, **kwargs)

        async def held():
            created.set()
            await resume.wait()
            return await coroutine

        return original.create_task(held(), *args, **kwargs)

    namespace = dict(vars(original))
    namespace["create_task"] = create_task
    monkeypatch.setattr(startup, "asyncio", SimpleNamespace(**namespace))
    async with owned_worker() as (worker, native, _writer, client, overlay, _gates, _peer, _handle):
        offer = asyncio.create_task(worker.dispatch("speech", request(valid_sdp)))
        try:
            await asyncio.wait_for(created.wait(), 1)
            session = worker.session
            prep = native.mixer.speech_preparation
            assert prep.begin_task is not None and prep.begin_reply is None
            assert not prep.begin_dispatched and not prep.begin_inflight
            offer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(offer, 0.2)
            resume.set()
            await asyncio.wait_for(worker._dispose(session), 1)
            assert prep.begin_task.done() and prep.retired and not prep.begin_dispatched
            assert prep.retirement_reply is None and not worker._cleanup_error and not worker._disposals
            assert not any(body["action"] in {"begin", "cancel", "finish"} for body in speech_requests(client))
            assert overlay.owner is None and not overlay.sent
        finally:
            resume.set()
            await settled(offer)


async def test_begin_ack_is_required_before_original_prefix_is_emitted(valid_sdp):
    async with owned_worker(music=True) as (worker, native, _writer, client, overlay, gates, _peer, handle):
        gates.begin_reply.clear()
        program = (handle.token, native.mixer.route, native.operation_generation,
                   native.mixer.output_buffer_ms, native.mixer.relay_delay_ns, source_requests(client))
        offer = asyncio.create_task(worker.dispatch("speech", request(valid_sdp)))
        try:
            await asyncio.wait_for(gates.begin_executed.wait(), 1)
            prep = native.mixer.speech_preparation
            voice = array("h", [1000] * 960).tobytes()
            native.mixer.set_speech_gain(0.1)
            native.mixer.push_speech(voice, 960)
            assert prep.frames == 960 and not overlay.sent and overlay.owner is None
            assert not offer.done()
            gates.begin_reply.set()
            await asyncio.wait_for(offer, 1)
            assert [(data, frames, gain) for data, frames, gain, _ns in overlay.sent] == [(voice, 960, 0.1)]
            assert overlay.owner == prep.speech_id == prep.begin_reply["speech_id"]
            assert prep.frames == 0 and prep.phase == "ready"
            assert (handle.token, native.mixer.route, native.operation_generation,
                    native.mixer.output_buffer_ms, native.mixer.relay_delay_ns, source_requests(client)) == program
            assert native.actor.owns(handle.token) and not handle.closed
        finally:
            gates.unblock()
            await settled(offer)


async def test_cancelled_offer_leaves_begin_owned_and_blocks_successor_until_cancel_ack(valid_sdp):
    async with owned_worker(music=True) as (worker, native, _writer, client, overlay, gates, _peer, handle):
        gates.begin_reply.clear()
        gates.cancel_reply.clear()
        program = (handle.token, native.mixer.route, native.operation_generation, source_requests(client))
        offer = asyncio.create_task(worker.dispatch("speech", request(valid_sdp)))
        try:
            await asyncio.wait_for(gates.begin_executed.wait(), 1)
            session = worker.session
            prep = native.mixer.speech_preparation
            offer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(offer, 0.2)
            assert worker.session is None and prep.retired and not overlay.sent
            assert not prep.begin_task.done() and not gates.begin_dependency_cancelled
            assert session.disposal in worker._disposals and not session.disposal.done()
            assert not speech_requests(client, "cancel")
            with pytest.raises(RpcError, match="retirement is still being observed"):
                await worker.dispatch("speech", request(valid_sdp, session="successor"))
            gates.begin_reply.set()
            await asyncio.wait_for(gates.cancel_executed.wait(), 1)
            cancel = speech_requests(client, "cancel")
            assert len(cancel) == 1 and cancel[0] == {**prep.body, "action": "cancel"}
            assert prep.begin_reply["speech_id"] == prep.speech_id
            assert prep.retirement_reply is None and not session.disposed
            with pytest.raises(RpcError, match="retirement is still being observed"):
                await worker.dispatch("speech", request(valid_sdp, session="successor"))
            gates.cancel_reply.set()
            await asyncio.wait_for(worker._dispose(session), 1)
            assert session.disposed and not worker._disposals and not worker._cleanup_error
            assert prep.retirement_reply["action"] == "cancel"
            assert (handle.token, native.mixer.route, native.operation_generation, source_requests(client)) == program
            worker.peer_factory = FakePeer
            await worker.dispatch("speech", request(valid_sdp, session="successor", request_id="next-offer"))
            assert native.mixer.speech_preparation.speech_id != prep.speech_id
            assert native.actor.owns(handle.token) and not handle.closed
        finally:
            gates.unblock()
            await settled(offer)


async def test_close_rpc_cancel_does_not_cancel_observed_voice_retirement(valid_sdp):
    async with owned_worker() as (worker, native, _writer, client, overlay, gates, peer, _handle):
        await worker.dispatch("speech", request(valid_sdp))
        session = worker.session
        prep = native.mixer.speech_preparation
        gates.cancel_reply.clear()
        closing = asyncio.create_task(worker.dispatch("speech", request(action="close")))
        try:
            await asyncio.wait_for(gates.cancel_executed.wait(), 1)
            closing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closing
            assert overlay.owner is None and worker.session is None
            assert not session.disposal.cancelled() and not session.disposal.done()
            assert peer.close_calls == 1 and not session.natural_eof
            with pytest.raises(RpcError, match="retirement is still being observed"):
                await worker.dispatch("speech", request(valid_sdp, session="successor"))
            gates.cancel_reply.set()
            await asyncio.wait_for(worker._dispose(session), 1)
            assert prep.retirement_reply == {**prep.body, "action": "cancel", "connected": False,
                "ready": False, "prepared_monotonic_ns": 0, "mixed_monotonic_ns": 0, "output_count": 0}
            assert len(speech_requests(client, "cancel")) == 1 and not speech_requests(client, "finish")
            assert len(speech_requests(client, "begin")) == 1
        finally:
            gates.unblock()
            await settled(closing)


async def test_offer_cancel_before_begin_never_dispatches_backend_voice_actions(valid_sdp):
    peer = FakePeer(blocked=True)
    async with owned_worker(peer=peer) as (worker, native, _writer, client, overlay, _gates, _peer, _handle):
        offer = asyncio.create_task(worker.dispatch("speech", request(valid_sdp)))
        try:
            await asyncio.wait_for(peer.entered.wait(), 1)
            session = worker.session
            prep = native.mixer.speech_preparation
            offer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(offer, 0.2)
            await asyncio.wait_for(worker._dispose(session), 1)
            assert prep.begin_task is None and prep.retired and prep.retirement_reply is None
            assert not any(body["action"] in {"begin", "cancel", "finish"} for body in speech_requests(client))
            assert overlay.owner is None and not overlay.sent and not worker._cleanup_error
        finally:
            await settled(offer)


@pytest.mark.parametrize("uncertainty", ["transport", "schema", "deadline"])
async def test_uncertain_begin_still_cancels_but_keeps_successor_admission_closed(valid_sdp, monkeypatch, uncertainty):
    if uncertainty == "deadline":
        monkeypatch.setattr("shiri.runtime.speech_startup.SETUP_SECONDS", 0.03)
    async with owned_worker(allow_failed_cleanup=True) as (worker, native, _writer, client, overlay, gates, _peer, _handle):
        if uncertainty == "transport":
            gates.begin_failure = OSError("Authenticated BEGIN reply lost after dispatch")
        elif uncertainty == "schema":
            gates.corrupt_begin = True
        else:
            gates.begin_reply.clear()
        offer = asyncio.create_task(worker.dispatch("speech", request(valid_sdp)))
        try:
            await asyncio.wait_for(gates.begin_executed.wait(), 1)
            prep = native.mixer.speech_preparation
            await settled(offer)
            with pytest.raises(RpcError, match="outcome is uncertain"):
                offer.result()
            assert prep.retirement_reply["action"] == "cancel"
            assert prep.retirement_reply["speech_id"] == prep.speech_id
            assert len(speech_requests(client, "cancel")) == 1 and not speech_requests(client, "finish")
            assert worker._cleanup_error and worker._disposals and worker.session is None
            assert not overlay.sent and overlay.owner is None
            health = await worker.dispatch("health", {})
            assert health["ready"] is True and health["speech_ready"] is False
            with pytest.raises(RpcError, match="Earlier speech resources could not be released"):
                await worker.dispatch("speech", request(valid_sdp, session="successor"))
            assert native.mixer.speech_preparation is prep
            if uncertainty == "deadline":
                assert gates.begin_dependency_cancelled and prep.begin_task.done()
        finally:
            gates.unblock()
            await settled(offer)


async def test_definitive_begin_rejection_needs_no_cancel_and_allows_fresh_offer(valid_sdp):
    async with owned_worker() as (worker, native, _writer, client, _overlay, gates, _peer, _handle):
        gates.begin_failure = OwnToneRejected("POST", "/api/player/shiri-speech-ready", 409)
        with pytest.raises(OwnToneRejected):
            await worker.dispatch("speech", request(valid_sdp))
        rejected = native.mixer.speech_preparation
        assert rejected.retired and not worker._cleanup_error and not worker._disposals
        assert not speech_requests(client, "cancel") and not speech_requests(client, "finish")
        gates.begin_failure = None
        worker.peer_factory = FakePeer
        await worker.dispatch("speech", request(valid_sdp, session="successor", request_id="new-offer"))
        assert native.mixer.speech_preparation.speech_id != rejected.speech_id


async def test_partial_prefix_output_refusal_cancels_the_admitted_voice(valid_sdp):
    async with owned_worker(music=True) as (worker, native, _writer, client, overlay, gates, _peer, handle):
        gates.begin_reply.clear()
        accepted = []
        attempts = []

        def refuse_second(data, samples):
            attempts.append((data, samples, overlay.gain))
            if len(attempts) == 2:
                return False
            accepted.append((data, samples, overlay.gain))
            return True

        overlay.push = refuse_second
        program = (handle.token, native.mixer.route, native.operation_generation, source_requests(client))
        offer = asyncio.create_task(worker.dispatch("speech", request(valid_sdp)))
        try:
            await asyncio.wait_for(gates.begin_executed.wait(), 1)
            prep = native.mixer.speech_preparation
            first = array("h", [1000] * 960).tobytes()
            second = array("h", [2300] * 960).tobytes()
            native.mixer.set_speech_gain(0.1)
            native.mixer.push_speech(first, 960)
            native.mixer.set_speech_gain(0.8)
            native.mixer.push_speech(second, 960)
            assert not attempts and prep.frames == 1920
            gates.begin_reply.set()
            await settled(offer)
            with pytest.raises(RpcError, match="refused bounded prepared PCM"):
                offer.result()
            assert attempts == [(first, 960, 0.1), (second, 960, 0.8)]
            assert accepted == [(first, 960, 0.1)]
            assert prep.error == "prepared_output_rejected" and prep.retired and not prep.prefix
            assert speech_requests(client, "cancel") == [{**prep.body, "action": "cancel"}]
            assert prep.retirement_reply["speech_id"] == prep.speech_id
            assert overlay.owner is None and not worker._cleanup_error and not worker._disposals
            assert (handle.token, native.mixer.route, native.operation_generation, source_requests(client)) == program
            assert native.actor.owns(handle.token) and not handle.closed
        finally:
            gates.unblock()
            await settled(offer)


async def test_source_successor_does_not_invalidate_exact_old_voice_cleanup(valid_sdp):
    async with owned_worker(music=True) as (worker, native, writer, client, overlay, gates, _peer, handle):
        gates.begin_reply.clear()
        offer = asyncio.create_task(worker.dispatch("speech", request(valid_sdp)))
        try:
            await asyncio.wait_for(gates.begin_executed.wait(), 1)
            prep = native.mixer.speech_preparation
            old_body = dict(prep.body)
            await native.actor.end_native(handle.token)
            successor = NativeHandle(native)
            grant = await native.begin(begin(), successor)
            program = (successor.token, native.mixer.route, native.operation_generation,
                       native.mixer.output_buffer_ms, native.mixer.relay_delay_ns, source_requests(client))
            gates.begin_reply.set()
            await settled(offer)
            with pytest.raises(RpcError):
                offer.result()
            assert prep.error == "voice_begin_owner_changed"
            assert speech_requests(client, "cancel") == [{**old_body, "action": "cancel"}]
            assert prep.retirement_reply["speech_id"] == prep.speech_id
            assert (successor.token, native.mixer.route, native.operation_generation,
                    native.mixer.output_buffer_ms, native.mixer.relay_delay_ns, source_requests(client)) == program
            assert native.actor.owns(successor.token) and not successor.closed
            now = native.mixer.now_ns()
            original = replace(pcm(grant, value=2300), monotonic_before_ns=now - 200,
                               monotonic_after_ns=now)
            await native.message(original, successor)
            assert writer.packets[-1].pcm == original.pcm and not overlay.sent
            assert not worker._cleanup_error
        finally:
            gates.unblock()
            await settled(offer)


class ResamplingPeer(FakePeer):
    def __init__(self, track):
        super().__init__()
        self.track = track
        self.track.kind = "audio"
        self.session = None
        self.close_reasons = []

    async def setRemoteDescription(self, description):
        self.handlers["track"](self.track)
        await super().setRemoteDescription(description)

    async def close(self):
        self.close_reasons.append(self.session.natural_eof if self.session else None)
        await super().close()


@pytest.mark.parametrize("termination", ["natural_eof", "explicit_close", "idle_timeout", "decode_failure"])
async def test_actual_av_tail_classification_survives_synchronous_peer_close(valid_sdp, termination):
    track = Track(8000)
    peer = ResamplingPeer(track)
    async with owned_worker(peer=peer, idle_seconds=0.03 if termination == "idle_timeout" else 1) as (
        worker, native, _writer, client, overlay, _gates, _peer, _handle
    ):
        await worker.dispatch("speech", request(valid_sdp, duck_gain=0.1))
        session = worker.session
        peer.session = session
        prep = native.mixer.speech_preparation
        await asyncio.wait_for(track.waiting.wait(), 1)
        immediate, tail = reference(8000)
        assert tail and b"".join(item[0] for item in overlay.sent) == immediate
        if termination == "natural_eof":
            track.eof()
        elif termination == "explicit_close":
            await worker.dispatch("speech", request(action="close"))
        elif termination == "decode_failure":
            track.queue.put_nowait(RuntimeError("Decoder failed after received PCM"))
        await settled(session.receiver)
        await asyncio.wait_for(worker._dispose(session), 1)
        natural = termination == "natural_eof"
        assert b"".join(item[0] for item in overlay.sent) == immediate + (tail if natural else b"")
        assert all(gain == 0.1 for _data, _frames, gain, _ns in overlay.sent)
        assert session.natural_eof is natural and peer.close_reasons == [natural]
        assert prep.retirement_reply["action"] == ("finish" if natural else "cancel")
        assert len(speech_requests(client, "finish")) == int(natural)
        assert len(speech_requests(client, "cancel")) == int(not natural)
        assert peer.close_calls == 1 and session.disposed and not worker._cleanup_error


class MemoryDatagrams:
    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)
        return len(message)

    def close(self):
        return


def test_session_voice_id_survives_silence_and_fresh_owner_resets_control_cache():
    output = SpeechOutput(Path("/private/in-memory-speech.sock"), str(uuid4()), uuid4().hex, 1001,
                          now_ns=lambda: 17_000_000_000)
    channel = MemoryDatagrams()
    output.socket = channel
    first, successor = uuid4().hex, uuid4().hex
    output.begin(first)
    audible = array("h", [1000] * 960).tobytes()
    assert output.control(False, 0.1)
    assert output.push(audible, 960) and output.push(bytes(1920), 960)
    assert output.control(False, 0.1)
    assert output.control(True, 0.8)
    assert output.push(audible, 960)
    assert output.control(False, 0.1)
    first_count = len(channel.messages)
    assert output.control(False, 0.1) and len(channel.messages) == first_count
    first_headers = [HEADER.unpack(message[:HEADER_BYTES]) for message in channel.messages]
    assert {header[-1] for header in first_headers} == {bytes.fromhex(first)}
    assert [(header[2], header[11]) for header in first_headers] == [
        (CONTROL, 0), (PCM, 1), (PCM, 0), (CONTROL, 0), (CONTROL, 1), (PCM, 1), (CONTROL, 0),
    ]
    assert output.owner == bytes.fromhex(first)
    output.retire(first)
    sequence = output.sequence
    assert not output.push(audible, 960) and output.sequence == sequence
    output.begin(successor)
    assert output.control(False, 0.1)
    count = len(channel.messages)
    output.retire(first)
    assert output.owner == bytes.fromhex(successor)
    assert output.control(False, 0.1) and len(channel.messages) == count
    assert output.push(audible, 960)
    new_headers = [HEADER.unpack(message[:HEADER_BYTES]) for message in channel.messages[len(first_headers):]]
    assert [header[2] for header in new_headers] == [CONTROL, PCM]
    assert all(header[1] == 2 and header[3] == 96 and header[-1] == bytes.fromhex(successor)
               for header in new_headers)
    assert [header[5] for header in first_headers + new_headers] == list(range(1, sequence + 3))
    output.close()


async def test_stale_local_retirement_cannot_clear_successor_preparation_or_voice(valid_sdp):
    async with owned_worker() as (worker, native, _writer, client, overlay, _gates, _peer, _handle):
        await worker.dispatch("speech", request(valid_sdp))
        old_session = worker.session
        old = native.mixer.speech_preparation
        await worker.dispatch("speech", request(action="close"))
        worker.peer_factory = FakePeer
        await worker.dispatch("speech", request(valid_sdp, session="successor", request_id="next-offer"))
        current_session = worker.session
        current = native.mixer.speech_preparation
        assert overlay.owner == current.speech_id != old.speech_id
        assert native.retire_speech(old_session) is None
        overlay.retire(old.speech_id)
        worker._release_callback(old_session)
        assert worker.session is current_session and native.mixer.speech_preparation is current
        assert not current.retired and overlay.owner == current.speech_id
        begun = list(speech_requests(client, "begin"))
        await worker.dispatch("speech", request(action="control", session="successor",
                                                request_id="gain-control", duck_gain=0.8))
        assert worker.session is current_session and current_session.duck_gain == 0.8
        assert native.mixer.speech_preparation is current and overlay.owner == current.speech_id
        assert speech_requests(client, "begin") == begun
        voice = array("h", [2300] * 960).tobytes()
        native.mixer.push_speech(voice, 960)
        assert overlay.sent[-1][:2] == (voice, 960)
