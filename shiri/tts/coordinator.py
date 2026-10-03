"""Text admission and paced private PCM delivery, independent of model engines."""
from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
from dataclasses import dataclass, field
import json
import math
import hashlib
import io
import wave
import time
from uuid import uuid4

import httpx
from pydantic import Field, field_validator

from shiri.domain import Conflict, DomainError, NotFound, StrictModel
from shiri.rpc import RpcError
from shiri.tts.models import DEFAULT_MODEL_ID, MAX_GENERATED_SECONDS

WORKER_SETTLE_SECONDS = 8


class TextSpeechRequest(StrictModel):
    request_id: str = Field(default_factory=lambda: uuid4().hex, pattern=r"^[0-9a-f]{32}$")
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

    def public(self):
        return {"id": self.id, "kind": self.kind, "room_id": self.room_id, "state": self.state,
                "metrics": self.metrics.copy(), "error": self.error,
                "sample_available": self.state == "completed" and bool(self.audio)}


class TextSpeechCoordinator:
    def __init__(self, service, *, worker_url=None, worker_token=None, client=None):
        self.service = service
        self.url = worker_url.rstrip("/") if worker_url else None
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(180, connect=5),
                                                headers={"Authorization": "Bearer " + (worker_token or "")},
                                                trust_env=False)
        self.jobs = OrderedDict()
        self.active = None
        self._lock = asyncio.Lock()

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

    async def admit(self, payload: TextSpeechRequest, *, room_id=None, external_id=None, benchmark=False):
        self.require_worker()
        fingerprint = hashlib.sha256(json.dumps({"request": payload.model_dump(), "room_id": room_id,
                                               "external_id": external_id, "benchmark": benchmark},
                                              sort_keys=True).encode()).hexdigest()
        # Resolve an external binding exactly once under the room mutation lock.
        # Every continuation and cancellation retains this admitted UUID.
        async with self.service._mutation:
            previous = self.jobs.get(payload.request_id)
            if previous:
                if previous.fingerprint != fingerprint:
                    raise Conflict("This request ID already belongs to different speech or routing intent")
                return previous.public()
            if not benchmark:
                room = (await self.service.resolve_nobly(external_id) if external_id is not None
                        else await self.service._store("get_room", room_id))
                if not room.enabled or not room.speakers:
                    raise Conflict("Enable the target room and select its speakers before speaking")
                room_id = room.id
            async with self._lock:
                if self.active:
                    raise Conflict("A speech job is already active; finish or cancel it first")
                job = SpeechJob(payload.request_id, "benchmark" if benchmark else "speech", room_id,
                                fingerprint=fingerprint)
                while len(self.jobs) >= 32:
                    self.jobs.popitem(last=False)
                self.jobs[job.id] = job
                self.active = job.id
                job.task = asyncio.create_task(self._run(job, payload))
                return job.public()

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
                job.state = "cancelled"
                if self.active == job.id:
                    self.active = None
            await asyncio.shield(asyncio.gather(job.task, return_exceptions=True))
        return job.public()

    async def _run(self, job, payload):
        job.started = True
        if job.kind == "benchmark":
            job.audio = bytearray()
        started = time.monotonic()
        identity = {"session_id": "tts-" + job.id, "request_id": job.id}
        preparation = None
        prepared = None
        finished = False
        generation_attempted = False
        terminal_state = "failed"
        frame_index, sequence = 0, 1
        pace_start = None
        try:
            catalog = await self.catalog()
            worker = catalog["worker"]
            if worker.get("state") != "ready" or worker.get("model_id") != payload.model_id:
                raise Conflict("Load and warm the selected model before speaking")
            job.state = "generating"
            if job.kind == "speech":
                async def prepare_room():
                    receipt = await self.service.speech(job.room_id, {**identity, "action": "prepare-pcm"})
                    if receipt.get("ok") and receipt.get("stream_id"):
                        # Preparation and generation run concurrently. Record
                        # confirmation here rather than when PCM later joins
                        # the ready backend; that would include model wait.
                        job.metrics["backend_ready_ms"] = (time.monotonic() - started) * 1000
                    return receipt

                preparation = asyncio.create_task(prepare_room())
            generation_attempted = True
            async with self.client.stream("POST", self.url + "/v1/generate",
                                          json=payload.model_dump(exclude_none=True, exclude={"request_id"})) as response:
                if response.status_code != 200:
                    generation_attempted = False
                    body = await response.aread()
                    raise Conflict(json.loads(body).get("error", "Generation was refused"))
                got_format, got_end = False, False
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
                        if frame_index + len(pcm) // 2 > 48000 * MAX_GENERATED_SECONDS:
                            raise ValueError("Speech exceeds the audio duration limit")
                        if preparation is not None and prepared is None:
                            prepared = await asyncio.shield(preparation)
                            if not prepared.get("ok") or not prepared.get("stream_id"):
                                raise Conflict(prepared.get("error", "Room did not become ready"))
                        if job.kind == "speech":
                            if pace_start is not None:
                                target = pace_start + frame_index / 48000
                                await asyncio.sleep(max(0, target - time.monotonic()))
                                if time.monotonic() - target > .15:
                                    raise ValueError("PCM delivery missed its live playback deadline")
                            sent_ns = time.monotonic_ns()
                            receipt = await self.service.speech(job.room_id, {**identity, "action": "pcm",
                                "stream_id": prepared["stream_id"], "sequence": sequence,
                                "frame_index": frame_index, "pcm_base64": event["pcm_base64"]})
                            replied_ns = time.monotonic_ns()
                            if not receipt.get("ok") or receipt.get("next_sequence") != sequence + 1:
                                raise Conflict(receipt.get("error", "Room refused speech audio"))
                            if pace_start is None:
                                admitted_ns = receipt.get("first_pcm_admitted_monotonic_ns")
                                # API and room AudioWorker share the Linux kernel
                                # clock over private local RPC. First dispatch
                                # can be slow before the receiver begins; reply
                                # latency after that admission remains lateness.
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
                        sequence += 1
                    elif kind == "end":
                        if got_end or not got_format or frame_index == 0:
                            raise ValueError("Incomplete or repeated generation completion")
                        got_end = True
                        metrics = event.get("metrics", {})
                        aliases = {"first_pcm_ms": "first_pcm_ms", "first_nonquiet_pcm_ms": "first_non_silent_pcm_ms",
                                   "leading_silence_ms": "leading_silence_ms", "rtf": "realtime_factor",
                                   "generated_audio_seconds": "audio_duration_s", "generation_ms": "generation_ms"}
                        for source, target in aliases.items():
                            value = metrics.get(source)
                            if isinstance(value, (int, float)) and not isinstance(value, bool):
                                if math.isfinite(value) and value >= 0:
                                    job.metrics[target] = value
                        if job.kind == "speech":
                            receipt = await self.service.speech(job.room_id, {**identity, "action": "finish",
                                "stream_id": prepared["stream_id"], "final_sequence": sequence - 1,
                                "final_frame_index": frame_index})
                            if not receipt.get("ok") or not receipt.get("finished"):
                                raise Conflict(receipt.get("error", "Room could not complete speech"))
                            finished = True
                    elif kind == "error":
                        raise ValueError(str(event.get("error", "Generation failed"))[:512])
                    else:
                        raise ValueError("Unknown generation stream record")
                if not got_end:
                    raise ValueError("Generation connection ended before natural completion")
            terminal_state = "completed"
        except asyncio.CancelledError:
            terminal_state = "cancelled"
        except Exception as exc:
            job.error = str(exc)[:512]
        finally:
            async def retire():
                nonlocal prepared
                if preparation is not None and not finished:
                    try:
                        if prepared is None:
                            # The runtime RPC owns its 15-second deadline.
                            # Retain its exact admission result and cleanup.
                            prepared = await asyncio.shield(preparation)
                        if prepared and prepared.get("stream_id"):
                            await self.service.speech(job.room_id, {**identity, "action": "close", "stream_id": prepared["stream_id"]})
                    except Exception:
                        job.error = job.error or "Speech cleanup could not be confirmed; inspect room diagnostics"
                if generation_attempted:
                    confirmed = await self._settle_worker()
                    job.metrics["worker_cleanup_confirmed"] = confirmed
                    if not confirmed:
                        job.error = job.error or "Generation worker cleanup could not be confirmed; inspect worker diagnostics"

            # DELETE can first arrive during natural-completion cleanup. Keep
            # that bounded retirement independently owned even then, and join
            # it before publishing cancellation or opening the admission slot.
            retirement = asyncio.create_task(retire())
            try:
                await asyncio.shield(retirement)
            except asyncio.CancelledError:
                terminal_state = "cancelled"
                await asyncio.shield(retirement)
            if terminal_state != "completed":
                job.audio = None
            job.state = terminal_state
            if terminal_state == "completed":
                self._retain_sample(job)
            job.metrics["total_ms"] = (time.monotonic() - started) * 1000
            job.metrics["delivered_audio_s"] = frame_index / 48000
            # No await separates terminal publication from exact slot release.
            if self.active == job.id:
                self.active = None

    async def close(self):
        if self.active:
            await self.cancel(self.active)
        await self.client.aclose()
