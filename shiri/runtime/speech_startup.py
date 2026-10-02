"""Private proposal: exact idle readiness before late speech TTL admission.

This owns a bounded per-negotiation prefix and exact backend voice admission.
It never mutates music ownership or program clocks. Original receive/prepare times remain in its
receipt; freshness begins at the actual datagram admission, not a claimed source
or encoder timestamp. Unexpected early RTP fails instead of losing its prefix.
"""

from __future__ import annotations

import asyncio
from collections import deque
import time
from uuid import uuid4

from shiri.deadline import bounded
from shiri.rpc import RpcError

SETUP_SECONDS = 5.0
PREFIX_MAX_FRAMES = 9600  # 200ms leaves margin inside the backend250ms queue.
PREFIX_MAX_AGE_NS = 250_000_000


class SpeechPreparation:
    def __init__(self, identity, *, now_ns=time.monotonic_ns, commit_required=False):
        self.identity = identity
        self.speech_id = uuid4().hex
        self.now_ns = now_ns
        self.started_ns = now_ns()
        self.deadline_ns = self.started_ns + round(SETUP_SECONDS * 1e9)
        self.phase = "preparing"
        self.body = None
        self.prefix = deque()
        self.frames = 0
        self.original_first_received_ns = None
        self.original_last_received_ns = None
        self.prepared_ns = None
        self.first_mix_ns = None
        self.released_ns = None
        self.maximum_prefix_wait_ns = 0
        self.error = None
        self.retired = False
        self.idle = False
        self.output_bed = False
        self.commit_required = commit_required
        self.negotiated = asyncio.Event()
        self.backend_reply = None
        self.owned = None
        self.begin_task = None
        self.begin_reply = None
        self.begin_inflight = False
        self.begin_dispatched = False
        self.retirement_reply = None

    def fail(self, reason):
        if self.error is None:
            self.error = reason
        self.phase = "failed"
        self.prefix.clear()
        self.frames = 0
        raise RpcError(
            "audio_unavailable", "Speech startup failed before its bounded prefix could be delivered"
        )

    def emit(self, data, samples, gain, send):
        try:
            send(data, samples, gain)
        except Exception:
            if self.error is None:
                self.error = "prepared_output_rejected"
            self.phase = "failed"
            self.prefix.clear()
            self.frames = 0
            raise

    def push(self, data, samples, gain, send):
        if self.retired or self.phase == "failed":
            return False
        now = self.now_ns()
        self.original_first_received_ns = self.original_first_received_ns or now
        self.original_last_received_ns = now
        if self.phase == "ready":
            self.emit(data, samples, gain, send)
            return True
        if (
            self.frames + samples > PREFIX_MAX_FRAMES
            or now >= self.deadline_ns
            or self.prefix
            and now - self.prefix[0][3] >= PREFIX_MAX_AGE_NS
        ):
            self.fail("prefix_capacity_or_age")
        self.prefix.append((data, samples, gain, now))
        self.frames += samples
        return True

    def ready(self, reply, send):
        now = self.now_ns()
        if self.retired or now >= self.deadline_ns:
            self.fail("setup_deadline_or_retirement")
        if self.prefix and now - self.prefix[0][3] >= PREFIX_MAX_AGE_NS:
            self.fail("prefix_age")
        # Validate the entire prefix before emitting any of it. Its recorded
        # receive timestamps are retained separately from fresh wire admission.
        self.prepared_ns = reply["prepared_monotonic_ns"]
        self.first_mix_ns = reply["mixed_monotonic_ns"]
        self.backend_reply = dict(reply)
        if self.commit_required:
            self.phase = "waiting_for_negotiation"
            return
        self.commit(send)

    def commit(self, send):
        now = self.now_ns()
        if self.retired or self.backend_reply is None or now >= self.deadline_ns:
            self.fail("commit_before_ready_or_after_deadline")
        if self.owned is not None and not self.owned():
            self.fail("commit_source_or_session_changed")
        if self.prefix and now - self.prefix[0][3] >= PREFIX_MAX_AGE_NS:
            self.fail("prefix_age_at_negotiation")
        if not 0 <= now - self.first_mix_ns < 100_000_000:
            self.fail("first_mix_stale_at_negotiation")
        self.released_ns = now
        self.phase = "ready"
        while self.prefix:
            data, samples, gain, received = self.prefix.popleft()
            self.maximum_prefix_wait_ns = max(self.maximum_prefix_wait_ns, now - received)
            self.emit(data, samples, gain, send)
        self.frames = 0

    def retire(self):
        self.retired = True
        self.phase = "retired"
        self.prefix.clear()
        self.frames = 0

    def receipt(self):
        identity = {key: getattr(self.identity, key, None) for key in ("session_id", "request_id")}
        identity = {
            key: value for key, value in identity.items() if isinstance(value, str) and len(value) <= 128
        }
        return {
            "speech_startup_identity": {"speech_id": self.speech_id, **identity},
            "speech_startup_authenticated_begin_ack": dict(self.begin_reply) if self.begin_reply else None,
            "speech_startup_authenticated_retirement_ack": dict(self.retirement_reply) if self.retirement_reply else None,
            "speech_startup_authenticated_ready_ack": dict(self.backend_reply)
            if self.backend_reply
            else None,
            "speech_startup_phase": self.phase,
            "speech_startup_error": self.error,
            "speech_startup_started_monotonic_ns": self.started_ns,
            "speech_startup_prepared_monotonic_ns": self.prepared_ns,
            "speech_startup_first_mix_monotonic_ns": self.first_mix_ns,
            "speech_startup_released_monotonic_ns": self.released_ns,
            "speech_startup_original_first_received_monotonic_ns": self.original_first_received_ns,
            "speech_startup_original_last_received_monotonic_ns": self.original_last_received_ns,
            "speech_startup_maximum_prefix_wait_ns": self.maximum_prefix_wait_ns,
            "speech_startup_pending_frames": self.frames,
            "speech_startup_setup_budget_ns": round(SETUP_SECONDS * 1e9),
            "speech_startup_performance_qualified": False,
            "speech_startup_output_bed": self.output_bed,
        }


async def complete(preparation, client, body, *, owned, bed, send, admit, interval=0.01):
    """Each response proves the exact request; calls never run in media ticks."""
    preparation.body = dict(body)
    preparation.owned = owned

    async def exchange(action, *, require_owned=True):
        request = {**body, "action": action}
        reply = await client.request("POST", "/api/player/shiri-speech-ready", json=request)
        expected = set(request) | {
            "connected",
            "ready",
            "prepared_monotonic_ns",
            "mixed_monotonic_ns",
            "output_count",
        }
        now = preparation.now_ns()
        if (
            require_owned and not owned()
            or type(reply) is not dict
            or set(reply) != expected
            or any(
                type(reply.get(key)) is not type(value) or reply[key] != value
                for key, value in request.items()
            )
            or type(reply["connected"]) is not bool
            or type(reply["ready"]) is not bool
            or type(reply["prepared_monotonic_ns"]) is not int
            or reply["prepared_monotonic_ns"] < 0
            or type(reply["mixed_monotonic_ns"]) is not int
            or reply["mixed_monotonic_ns"] < 0
            or type(reply["output_count"]) is not int
            or not 0 <= reply["output_count"] <= 128
        ):
            preparation.fail("readiness_identity_or_schema")
        if reply["ready"] and (
            not reply["connected"]
            or not reply["output_count"]
            or not 0 <= now - reply["mixed_monotonic_ns"] < 100_000_000
        ):
            preparation.fail("readiness_mix_age")
        return reply

    try:

        async def run():
            observation = None
            needs_preparation = preparation.idle
            if not preparation.idle:
                # A retained phone source may be paused or have never emitted
                # PCM. Observe first so recent music needs no output setup.
                observation = await exchange("observe")
                needs_preparation = not observation["ready"]
                preparation.output_bed = needs_preparation
                if needs_preparation:
                    observation = None
            if needs_preparation:
                while True:
                    if not owned():
                        preparation.fail("source_or_session_changed")
                    reply = await exchange("prepare")
                    if reply["connected"]:
                        break
                    await asyncio.sleep(interval)
                if (
                    not reply["output_count"]
                    or not 0 < reply["prepared_monotonic_ns"] <= preparation.now_ns()
                ):
                    preparation.fail("outputs_not_connected")
                preparation.phase = "waiting_for_mix"
                bed(preparation)
            if preparation.commit_required:
                await preparation.negotiated.wait()
            while True:
                if not owned():
                    preparation.fail("source_or_session_changed")
                reply = observation or await exchange("ready" if needs_preparation else "observe")
                observation = None
                if reply["ready"]:
                    if needs_preparation and reply["mixed_monotonic_ns"] < reply["prepared_monotonic_ns"]:
                        preparation.fail("mix_predates_preparation")
                    # One owned task serializes BEGIN attempts. Only an exact
                    # not-ready echo permits bounded retry; no uncertain request
                    # is repeated or abandoned before observed cancellation.
                    async def begin():
                        async def attempts():
                            last = None
                            while True:
                                if not owned():
                                    if last is not None:
                                        return last
                                    preparation.fail("voice_begin_owner_changed")
                                preparation.begin_dispatched = True
                                preparation.begin_inflight = True
                                result = await exchange("begin", require_owned=False)
                                preparation.begin_inflight = False
                                preparation.begin_reply = dict(result)
                                if result["ready"]:
                                    return result
                                last = result
                                await asyncio.sleep(interval)

                        remaining = (preparation.deadline_ns - preparation.now_ns()) / 1e9
                        return await bounded(attempts(), remaining)

                    preparation.begin_task = asyncio.create_task(begin(), name="speech-voice-begin")
                    preparation.begin_task.add_done_callback(
                        lambda task: task.exception() if not task.cancelled() else None
                    )
                    await asyncio.wait({preparation.begin_task})
                    admitted = preparation.begin_task.result()
                    preparation.begin_reply = dict(admitted)
                    if not admitted["ready"]:
                        preparation.fail("voice_begin_not_ready")
                    if not owned():
                        preparation.fail("voice_begin_owner_changed")
                    admit(preparation.speech_id)
                    preparation.ready(admitted, send)
                    return preparation.receipt()
                await asyncio.sleep(interval)

        remaining = (preparation.deadline_ns - preparation.now_ns()) / 1e9
        if remaining <= 0:
            preparation.fail("setup_deadline_before_request")
        return await bounded(run(), remaining)
    except asyncio.CancelledError:
        preparation.retire()
        raise
    except Exception:
        if not preparation.retired and preparation.error is None:
            preparation.error = "setup_request_or_deadline"
            preparation.phase = "failed"
            preparation.prefix.clear()
            preparation.frames = 0
        raise


async def retire_backend(preparation, client, *, natural=False):
    """Join the one BEGIN outcome, then observe exact player retirement.

    An uncertain BEGIN never authorizes a successor to replace its retained
    terminal fence, even if cancellation itself returns an exact receipt.
    """
    from .backend import OwnToneRejected

    task = preparation.begin_task
    if task is None:
        return
    uncertain = False
    admitted = False
    await asyncio.wait({task})
    try:
        reply = task.result()
        preparation.begin_reply = dict(reply)
        admitted = reply["ready"]
    except OwnToneRejected:
        # The authenticated handler rejects before media admission.
        return
    except asyncio.CancelledError:
        if not preparation.begin_dispatched:
            return
        uncertain = True
    except Exception:
        if not preparation.begin_dispatched:
            return
        if (not preparation.begin_inflight and preparation.begin_reply is not None
                and not preparation.begin_reply["ready"]):
            return  # deadline during definitive not-ready backoff: no voice was admitted
        uncertain = True
    if not admitted and not uncertain:
        return
    action = "finish" if natural and not uncertain else "cancel"
    request = {**preparation.body, "action": action}
    reply = await client.request("POST", "/api/player/shiri-speech-ready", json=request)
    expected = set(request) | {
        "connected", "ready", "prepared_monotonic_ns", "mixed_monotonic_ns", "output_count",
    }
    if (type(reply) is not dict or set(reply) != expected
            or any(type(reply.get(key)) is not type(value) or reply[key] != value
                   for key, value in request.items())
            or type(reply["connected"]) is not bool or reply["connected"]
            or type(reply["ready"]) is not bool or reply["ready"]
            or type(reply["prepared_monotonic_ns"]) is not int or reply["prepared_monotonic_ns"] != 0
            or type(reply["mixed_monotonic_ns"]) is not int or reply["mixed_monotonic_ns"] != 0
            or type(reply["output_count"]) is not int or reply["output_count"] != 0):
        raise RpcError("audio_unavailable", "Speech retirement did not acknowledge the exact voice")
    preparation.retirement_reply = dict(reply)
    if uncertain:
        raise RpcError("audio_unavailable", "Speech BEGIN outcome is uncertain; successor admission remains closed")
