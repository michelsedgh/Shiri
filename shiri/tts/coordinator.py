"""Text admission and paced private PCM delivery, independent of model engines."""
from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict, deque
from dataclasses import dataclass, field
import json
import math
import hashlib
import io
import wave
import time
from uuid import uuid4

import httpx
from anyio import CancelScope
from pydantic import Field, field_validator

from shiri.domain import Conflict, DomainError, NotFound, StrictModel
from shiri.rpc import RpcError
from shiri.tts.models import DEFAULT_MODEL_ID, MAX_GENERATED_SECONDS

WORKER_SETTLE_SECONDS = 8


class TextSpeechRequest(StrictModel):
    request_id: str = Field(default_factory=lambda: uuid4().hex, pattern=r"^[0-9a-f]{32}$")
    replace_job_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    text: str = Field(min_length=1, max_length=2000)
    model_id: str = Field(default=DEFAULT_MODEL_ID, min_length=1, max_length=128)
    voice: str | None = Field(default=None, max_length=128)
    language: str | None = Field(default=None, max_length=128)
    speed: float = Field(default=1.0, ge=0.5, le=2.0, allow_inf_nan=False)
    instruction: str | None = Field(default=None, max_length=512)

    @field_validator("text")
    @classmethod
    def valid_text(cls, value):
        if not value.strip() or any(ord(char) < 32 and char not in "\n\t" for char in value):
            raise ValueError("Provide nonempty text without control characters")
        return value


@dataclass
class SpeechJob:
    id: str
    kind: str
    room_id: str | None
    state: str = "queued"
    metrics: dict = field(default_factory=dict)
    error: str | None = None
    task: asyncio.Task | None = None
    started: bool = False
    cancel_requested: bool = False
    fingerprint: str = ""
    audio: bytearray | None = None
    replaces: str | None = None
    route_fingerprint: str | None = None
    activate: asyncio.Event = field(default_factory=asyncio.Event)
    received_at: float = field(default_factory=time.monotonic)
    admitted_at: float = field(default_factory=time.monotonic)

    def public(self):
        return {"id": self.id, "kind": self.kind, "room_id": self.room_id, "state": self.state,
                "metrics": self.metrics.copy(), "error": self.error,
                "sample_available": self.state == "completed" and bool(self.audio)}


class TextSpeechCoordinator:
    def __init__(self, service, *, worker_url=None, worker_token=None, client=None, max_rooms=8):
        self.service = service
        self.url = worker_url.rstrip("/") if worker_url else None
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(180, connect=5),
                                                headers={"Authorization": "Bearer " + (worker_token or "")},
                                                trust_env=False)
        self.jobs = OrderedDict()
        self.active = {}
        self.pending = {}
        self.max_rooms = max_rooms
        self._lock = asyncio.Lock()
        self._generation_lock = asyncio.Lock()
        self._closing = False

    def require_worker(self):
        if not self.url:
            raise DomainError("Configure the optional Shiri TTS generation worker first")

    async def catalog(self, *, wait_seconds=5):
        if not self.url:
            return {"enabled": False, "worker": {"state": "stopped"}, "models": []}
        try:
            response = await self.client.get(self.url + "/v1/models", timeout=wait_seconds)
            response.raise_for_status()
            catalog = response.json()
            if (not isinstance(catalog, dict) or not isinstance(catalog.get("worker"), dict)
                    or not isinstance(catalog["worker"].get("state"), str)
                    or catalog["worker"].get("state") not in {"stopped", "loading", "ready", "busy", "failed"}
                    or not isinstance(catalog.get("models"), list)
                    or any(not isinstance(model, dict) for model in catalog["models"])):
                raise ValueError("Invalid generation worker catalog")
            return {**catalog, "enabled": True}
        except (httpx.HTTPError, ValueError):
            return {"enabled": True, "worker": {"state": "unavailable", "error": "Generation worker is unreachable"},
                    "models": []}

    async def _settle_worker(self):
        # Closing HTTP only signals disconnect. The private worker remains busy
        # while its decoder resets or its bounded kill/reap fallback completes.
        # Observe that retirement before releasing our slot; never retry audio
        # or send cancellation to a possibly different private operation.
        async def observe():
            while True:
                state = (await self.catalog(wait_seconds=1))["worker"]["state"]
                if state in {"ready", "stopped", "failed"}:
                    return True
                if state == "unavailable":
                    return False
                await asyncio.sleep(.05)

        try:
            return await asyncio.wait_for(observe(), WORKER_SETTLE_SECONDS)
        except Exception:
            # A failed control client cannot leave admission owned forever.
            return False

    async def load(self, model_id):
        self.require_worker()
        async with self._lock:
            if self.active:
                raise Conflict("Finish or cancel the active speech job before changing models")
            try:
                response = await self.client.post(self.url + "/v1/load", json={"model_id": model_id}, timeout=5)
                result = response.json()
                if not isinstance(result, dict):
                    raise ValueError("Invalid generation worker load response")
                if response.status_code != 202:
                    error = result.get("error", "Could not load model")
                    if not isinstance(error, str):
                        raise ValueError("Invalid generation worker refusal")
                    raise Conflict(error[:512])
                if not isinstance(result.get("state"), str) or result["state"] not in {"loading", "ready"}:
                    raise ValueError("Invalid generation worker load state")
                return result
            except (httpx.HTTPError, ValueError) as exc:
                raise RpcError("audio_unavailable", "Generation worker is unreachable or returned an invalid response") from exc

    async def warm(self, model_id, *, purpose="presence"):
        """Observe presence; prime only for an explicitly anticipated reply."""
        if not self.url:
            return {"state": "disabled", "model_id": model_id}
        if purpose == "presence":
            worker = (await self.catalog())["worker"]
            return {"state": "observed", "model_id": model_id, "worker": worker}
        async with self._lock:
            if self.active:
                return {"state": "busy", "model_id": model_id}
        # Optional preparation must not hold speech admission behind its HTTP
        # response. If speech wins this race, the worker skips the later hint;
        # if the hint wins, real generation preempts its exact reset owner.
        request_id = uuid4().hex
        try:
            response = await self.client.post(self.url+"/v1/warm", json={
                "request_id": request_id, "model_id": model_id,
            }, timeout=5)
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError("Invalid model warm response")
            if response.status_code == 409:
                return {"state": "not_admitted", "model_id": model_id}
            if response.status_code != 202:
                raise ValueError("Model warm request refused")
            warm = result.get("warm")
            if (not isinstance(warm, dict) or warm.get("model_id") != model_id
                    or warm.get("request_id") != request_id
                    or type(warm.get("accepted")) is not bool
                    or warm.get("state") not in {"preparing", "completed", "cancelled", "failed", "skipped"}
                    or (warm["state"] == "skipped") != (not warm["accepted"])):
                raise ValueError("Invalid model warm receipt")
            return {"state": "requested" if warm["accepted"] else "skipped", "model_id": model_id,
                    "request_id": request_id, "observation": result}
        except (httpx.HTTPError, ValueError):
            return {"state": "unavailable", "model_id": model_id}

    async def admit(self, payload: TextSpeechRequest, *, room_id=None, external_id=None, benchmark=False):
        received_at = time.monotonic()
        self.require_worker()
        fingerprint = hashlib.sha256(json.dumps({"request": payload.model_dump(), "room_id": room_id,
                                               "external_id": external_id, "benchmark": benchmark},
                                              sort_keys=True).encode()).hexdigest()
        # The store resolves one coherent routing snapshot. Our short admission
        # lock fences request replay and room ownership, independently of slow
        # reconciliation or changes to an unrelated room.
        async with self._lock:
            if self._closing:
                raise Conflict("Speech admission is shutting down")
            previous = self.jobs.get(payload.request_id)
            if previous:
                if previous.fingerprint != fingerprint:
                    raise Conflict("This request ID already belongs to different speech or routing intent")
                return previous.public()
            if not benchmark:
                room = await self.service.resolve_text_speech(room_id=room_id, external_id=external_id)
                if not room.enabled or not room.speakers:
                    raise Conflict("Enable the target room and select its speakers before speaking")
                room_id = room.id
                readiness = getattr(self.service, "warm_coordinator", None)
                if readiness is not None:
                    readiness.touch(room_id)
            key = None if benchmark else room_id
            current = self.active.get(key)
            if payload.replace_job_id is not None:
                if benchmark or current != payload.replace_job_id:
                    raise Conflict("Interruption requires the exact active speech job in this room")
                if self.jobs[current].replaces is not None:
                    raise Conflict("This room is still retiring its previous interruption")
            elif current is not None:
                if benchmark:
                    raise Conflict("A quiet benchmark is already active")
                if len(self.pending.get(key, ())) >= 2:
                    raise Conflict("This room already has two waiting replies; wait or cancel an exact queued job")
            if not benchmark and current is None and sum(key is not None for key in self.active) >= self.max_rooms:
                raise Conflict("Speech capacity is limited to the configured rooms")
            while len(self.jobs) >= 32:
                expired = next((identity for identity, item in self.jobs.items()
                                if item.task is None or item.task.done()), None)
                if expired is None:
                    raise Conflict("Speech receipt capacity is busy; wait for retiring requests")
                del self.jobs[expired]
            job = SpeechJob(payload.request_id, "benchmark" if benchmark else "speech", room_id,
                            fingerprint=fingerprint, replaces=current if payload.replace_job_id is not None else None,
                            received_at=received_at, admitted_at=time.monotonic())
            job.metrics["admission_ms"] = (job.admitted_at - received_at) * 1000
            if not benchmark:
                from shiri.readiness import transport_fingerprint
                job.route_fingerprint = transport_fingerprint(room)
            self.jobs[job.id] = job
            if current is None or job.replaces is not None:
                self.active[key] = job.id
                job.activate.set()
            else:
                self.pending.setdefault(key, deque()).append(job.id)
            job.task = asyncio.create_task(self._run(job, payload))
            return job.public()

    def _release(self, job):
        key = None if job.kind == "benchmark" else job.room_id
        waiting = self.pending.get(key)
        if waiting and job.id in waiting:
            waiting.remove(job.id)
        if self.active.get(key) == job.id:
            del self.active[key]
            if not self._closing and waiting:
                while waiting:
                    next_job = self.jobs[waiting.popleft()]
                    if not next_job.cancel_requested:
                        self.active[key] = next_job.id
                        next_job.activate.set()
                        break
        if waiting is not None and not waiting:
            self.pending.pop(key, None)

    def get(self, job_id):
        if job_id not in self.jobs:
            raise NotFound("Speech job does not exist or has expired")
        return self.jobs[job_id]

    def sample(self, job_id):
        job = self.get(job_id)
        if job.kind != "benchmark" or job.state != "completed" or not job.audio:
            raise NotFound("This quiet speech sample is unavailable or has expired")
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(48000)
            wav.writeframes(job.audio)
        return output.getvalue()

    def _retain_sample(self, job):
        # At most 12 MiB across retained quiet previews, in addition to the
        # single bounded active generation. No recordings persist to disk.
        total = sum(len(item.audio or b"") for item in self.jobs.values())
        for previous in self.jobs.values():
            if total <= 12 * 1024**2:
                break
            if previous is not job and previous.audio:
                total -= len(previous.audio)
                previous.audio = None

    async def cancel(self, job_id):
        job = self.get(job_id)
        if job.task and not job.task.done():
            if not job.cancel_requested:
                job.cancel_requested = True
                job.task.cancel()
            if not job.started:
                # A cancelled task that never entered _run has no finally to
                # retire its slot. Do this synchronously before joining: the
                # HTTP caller itself may disconnect during that await.
                if job.replaces is not None:
                    async def retire_unstarted():
                        await self.cancel(job.replaces)
                        job.replaces = None
                        job.state = "cancelled"
                        self._release(job)
                    job.started = True
                    job.task = asyncio.create_task(retire_unstarted())
                else:
                    job.state = "cancelled"
                    self._release(job)
            await asyncio.shield(asyncio.gather(job.task, return_exceptions=True))
        return job.public()

    async def _generate(self, job, payload, records, admitted, started):
        """Own inference only; room playback never holds this shared slot."""
        attempted = False
        queued_at = time.monotonic()
        try:
            async with self._generation_lock:
                job.metrics["generation_wait_ms"] = (time.monotonic() - queued_at) * 1000
                job.state = "generating"
                got_format, got_end = False, False
                received_frames = 0
                end_metrics = {}
                try:
                    attempted = True
                    job.metrics["generation_requested_ms"] = (time.monotonic() - started) * 1000
                    async with self.client.stream("POST", self.url + "/v1/generate",
                            json=payload.model_dump(exclude_none=True, exclude={"request_id", "replace_job_id"})) as response:
                        if response.status_code != 200:
                            attempted = False
                            body = await response.aread()
                            raise Conflict(json.loads(body).get("error", "Generation was refused"))
                        # A recovering resident model can delay HTTP admission.
                        # Do not own/duck a room until its actual decoder is ready.
                        admitted.set()
                        job.metrics["worker_admission_ms"] = (time.monotonic() - started) * 1000
                        async for line in response.aiter_lines():
                            if not line or len(line) > 16_384:
                                raise ValueError("Invalid generation stream record")
                            event = json.loads(line)
                            kind = event.get("type")
                            if kind == "format":
                                if got_format or event != {"type": "format", "format": "s16le", "sample_rate": 48000, "channels": 1}:
                                    raise ValueError("Generation format changed or is unsupported")
                                got_format = True
                            elif kind == "pcm":
                                if not got_format or got_end:
                                    raise ValueError("Audio arrived outside its admitted generation")
                                pcm = base64.b64decode(event["pcm_base64"], validate=True)
                                if not pcm or len(pcm) % 2 or len(pcm) > 1920:
                                    raise ValueError("Invalid generation PCM frame")
                                received_frames += len(pcm) // 2
                                if received_frames > 48000 * MAX_GENERATED_SECONDS:
                                    raise ValueError("Speech exceeds the audio duration limit")
                                job.metrics.setdefault("first_worker_pcm_received_ms", (time.monotonic() - started) * 1000)
                                job.metrics["received_audio_s"] = received_frames / 48000
                                await records.put(("pcm", pcm))
                            elif kind == "end":
                                if got_end or not got_format or received_frames == 0:
                                    raise ValueError("Incomplete or repeated generation completion")
                                got_end = True
                                end_metrics = event.get("metrics", {})
                                if not isinstance(end_metrics, dict):
                                    raise ValueError("Invalid generation completion metrics")
                            elif kind == "error":
                                raise ValueError(str(event.get("error", "Generation failed"))[:512])
                            else:
                                raise ValueError("Unknown generation stream record")
                        if not got_end:
                            raise ValueError("Generation connection ended before natural completion")
                    aliases = {"first_pcm_ms": "first_pcm_ms", "first_nonquiet_pcm_ms": "first_non_silent_pcm_ms",
                               "leading_silence_ms": "leading_silence_ms", "rtf": "realtime_factor",
                               "generated_audio_seconds": "audio_duration_s", "generation_ms": "generation_ms"}
                    for source, target in aliases.items():
                        value = end_metrics.get(source)
                        if (type(value) in (int, float) and 0 <= value <= 1e12 and math.isfinite(value)):
                            job.metrics[target] = value
                    await records.put(("end", None))
                except Exception as exc:
                    while not records.empty():
                        records.get_nowait()
                    await records.put(("error", exc))
                finally:
                    if attempted:
                        # Keep the one model slot until the previous decoder is
                        # reusable. This retirement is independent of playback.
                        cleanup = asyncio.create_task(self._settle_worker())
                        cancelled = False
                        with CancelScope(shield=True):
                            while not cleanup.done():
                                try:
                                    await asyncio.shield(cleanup)
                                except asyncio.CancelledError:
                                    cancelled = True
                            confirmed = cleanup.result()
                            job.metrics["worker_cleanup_confirmed"] = confirmed
                            if not confirmed:
                                job.error = job.error or "Generation worker cleanup could not be confirmed; inspect worker diagnostics"
                        if cancelled:
                            raise asyncio.CancelledError
                    job.metrics["generation_released_ms"] = (time.monotonic() - started) * 1000
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A failed generation must discard unsent buffered speech, rather
            # than let a consumer play seconds of a known incomplete utterance.
            while not records.empty():
                records.get_nowait()
            await records.put(("error", exc))

    async def _run(self, job, payload):
        job.started = True
        if job.kind == "benchmark":
            job.audio = bytearray()
        started = job.received_at
        identity = {"session_id": "tts-" + job.id, "request_id": job.id}
        if job.route_fingerprint is not None:
            identity["transport_fingerprint"] = job.route_fingerprint
        # One room can retain at most 60 seconds of PCM. A record bound also
        # prevents tiny malicious frames from growing Python object overhead.
        records = None
        generation_admitted = asyncio.Event()
        producer = preparation = None
        prepared = channel = None
        finished = False
        terminal_state = "failed"
        frame_index, sequence = 0, 1
        pace_start = None
        job.metrics.update(received_audio_s=0.0, delivered_audio_s=0.0)
        try:
            await job.activate.wait()
            job.metrics["room_queue_wait_ms"] = (time.monotonic() - job.admitted_at) * 1000
            if job.replaces is not None:
                await self.cancel(job.replaces)
                job.replaces = None
            records = asyncio.Queue(maxsize=int(MAX_GENERATED_SECONDS * 50) + 2)
            producer = asyncio.create_task(self._generate(job, payload, records, generation_admitted, started))
            if job.kind == "speech":
                async def prepare_room():
                    nonlocal channel
                    # Queued inference must not duck music for the duration of
                    # another room's generation. Start both once our turn begins.
                    await generation_admitted.wait()
                    prepare_sent_ns = time.monotonic_ns()
                    job.metrics["room_prepare_requested_ms"] = (time.monotonic() - started) * 1000
                    channel = await self.service.open_text_speech(job.room_id, {**identity, "duck_on_prepare": True})
                    receipt = channel.prepared
                    if receipt.get("ok") and receipt.get("stream_id"):
                        job.metrics["backend_ready_ms"] = (time.monotonic() - started) * 1000
                        duck_ns = receipt.get("duck_requested_monotonic_ns")
                        verified = type(duck_ns) is int and prepare_sent_ns <= duck_ns <= time.monotonic_ns()
                        job.metrics["music_duck_clock_verified"] = verified
                        if verified:
                            job.metrics["music_duck_requested_ms"] = (duck_ns / 1e9 - started) * 1000
                            job.metrics["music_duck_attack_ms"] = receipt.get("duck_attack_ms")
                            job.metrics["music_duck_release_ms"] = receipt.get("duck_release_ms")
                        job.metrics["backend_startup_steps"] = receipt.get("backend_startup_steps", {})
                    return receipt

                preparation = asyncio.create_task(prepare_room())
            while True:
                kind, value = await records.get()
                if kind == "error":
                    raise value
                if kind == "end":
                    if job.kind == "speech":
                        receipt = await channel.finish(sequence - 1, frame_index)
                        if not receipt.get("ok") or not receipt.get("finished"):
                            raise Conflict(receipt.get("error", "Room could not complete speech"))
                        finished = True
                    break
                pcm = value
                if preparation is not None and prepared is None:
                    prepared = await asyncio.shield(preparation)
                    if not prepared.get("ok") or not prepared.get("stream_id"):
                        raise Conflict(prepared.get("error", "Room did not become ready"))
                if job.kind == "speech":
                    sent_ns = time.monotonic_ns()
                    receipt = await channel.send_pcm(pcm, sequence, frame_index)
                    replied_ns = time.monotonic_ns()
                    if not receipt.get("ok") or receipt.get("next_sequence") != sequence + 1:
                        raise Conflict(receipt.get("error", "Room refused speech audio"))
                    if pace_start is None:
                        admitted_ns = receipt.get("first_pcm_admitted_monotonic_ns")
                        if (receipt.get("pcm_clock") != "room_audio_worker_monotonic; admission_not_acoustic"
                                or type(admitted_ns) is not int or not sent_ns <= admitted_ns <= replied_ns):
                            raise ValueError("Room PCM admission clock could not be verified")
                        pace_start = admitted_ns / 1_000_000_000
                        job.metrics["room_admission_ms"] = (pace_start - started) * 1000
                        job.metrics["first_pcm_dispatch_ms"] = (sent_ns / 1_000_000_000 - started) * 1000
                        job.metrics["first_pcm_rpc_ms"] = (replied_ns - sent_ns) / 1_000_000
                    job.state = "playing"
                else:
                    job.audio.extend(pcm)
                frame_index += len(pcm) // 2
                job.metrics["delivered_audio_s"] = frame_index / 48000
                sequence += 1
            terminal_state = "completed"
        except asyncio.CancelledError:
            terminal_state = "cancelled"
        except Exception as exc:
            job.error = str(exc)[:512]
        finally:
            async def retire():
                nonlocal prepared
                if job.replaces is not None:
                    await self.cancel(job.replaces)
                    job.replaces = None
                if producer is not None and not producer.done() and terminal_state != "completed":
                    producer.cancel()
                if preparation is not None and not finished:
                    try:
                        if not generation_admitted.is_set():
                            preparation.cancel()
                            await asyncio.gather(preparation, return_exceptions=True)
                        else:
                            if prepared is None:
                                prepared = await asyncio.shield(preparation)
                            if channel is not None:
                                await channel.close()
                    except Exception:
                        job.error = job.error or "Speech cleanup could not be confirmed; inspect room diagnostics"
                if producer is not None:
                    await asyncio.gather(producer, return_exceptions=True)

            retirement = asyncio.create_task(retire())
            with CancelScope(shield=True):
                while not retirement.done():
                    try:
                        await asyncio.shield(retirement)
                    except asyncio.CancelledError:
                        terminal_state = "cancelled"
            retirement.result()
            if terminal_state != "completed":
                job.audio = None
            job.state = terminal_state
            if terminal_state == "completed":
                self._retain_sample(job)
            job.metrics["total_ms"] = (time.monotonic() - started) * 1000
            job.metrics["delivered_audio_s"] = frame_index / 48000
            self._release(job)

    async def close(self):
        self._closing = True
        await asyncio.gather(*(self.cancel(identity) for identity, job in tuple(self.jobs.items())
                               if job.task is not None and not job.task.done()))
        await self.client.aclose()
