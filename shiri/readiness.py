"""Finite room connection leases, independent of voice and music ownership."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import json
import re
import time
from typing import Literal
from uuid import uuid4

from pydantic import Field

from shiri.domain import Conflict, NotFound, StrictModel
from shiri.rpc import RpcError
from shiri.tts.models import DEFAULT_MODEL_ID

RETENTION_NS = 600_000_000_000
TERMINAL = {"released", "expired", "revoked", "failed"}


class WarmRequest(StrictModel):
    request_id: str = Field(default_factory=lambda: uuid4().hex, pattern=r"^[0-9a-f]{32}$")
    ttl_seconds: int = Field(default=60, ge=5, le=300)
    purpose: Literal["presence", "interaction"] = "presence"
    model_id: str | None = Field(default=None, min_length=1, max_length=128)


class WarmRenew(StrictModel):
    request_id: str = Field(default_factory=lambda: uuid4().hex, pattern=r"^[0-9a-f]{32}$")
    ttl_seconds: int = Field(default=60, ge=5, le=300)


def transport_fingerprint(room):
    """Volume and balancing preserve a connection; routing changes invalidate it."""
    intent = {key: getattr(room, key) for key in (
        "id", "enabled", "interface", "slot", "airplay_name", "local_audio_device"
    )}
    intent["speakers"] = sorted(
        (speaker.id, speaker.protocol, speaker.offset_ms, speaker.airplay_timing)
        for speaker in room.speakers
    )
    return hashlib.sha256(json.dumps(intent, sort_keys=True).encode()).hexdigest()


@dataclass
class RoomLease:
    id: str
    room_id: str
    fingerprint: str
    deadline_ns: int
    purpose: str
    model_id: str | None
    state: str = "pending"
    ended_ns: int | None = None
    connection: dict = field(default_factory=dict)
    model: dict = field(default_factory=dict)
    task: asyncio.Task | None = None
    operations: set[asyncio.Task] = field(default_factory=set)
    launch_generation: str | None = None
    automatic: bool = False

    def public(self, now_ns):
        return {"lease_id": self.id, "admitted_room_id": self.room_id,
                "state": self.state, "purpose": self.purpose,
                "remaining_ms": max(0, (self.deadline_ns-now_ns)//1_000_000)
                if self.state not in TERMINAL else 0,
                "connection": self.connection.copy(), "model": self.model.copy(),
                "acoustic_ready": None}


class RoomReadinessCoordinator:
    def __init__(self, service, tts=None, *, now_ns=time.monotonic_ns, automatic=False):
        self.service, self.tts, self.now_ns = service, tts, now_ns
        self.leases = OrderedDict()
        self.receipts = OrderedDict()
        self._lock = asyncio.Lock()
        self._closing = False
        self._cleanup = set()
        self.mode = "ready" if automatic is True else (automatic if automatic else "on_demand")
        if self.mode not in {"ready", "adaptive", "on_demand"}:
            raise ValueError("Unknown autonomous room readiness policy")
        self.automatic = self.mode != "on_demand"
        self._auto = {}
        self._auto_next = {}
        self._auto_failures = {}
        self._auto_until = {}
        self._auto_fingerprints = {}

    def _prune(self):
        now = self.now_ns()
        for key, (created, _, _) in list(self.receipts.items()):
            if now-created >= RETENTION_NS:
                del self.receipts[key]
        for key, lease in list(self.leases.items()):
            if lease.ended_ns is not None and (now-lease.ended_ns >= RETENTION_NS or (
                    lease.automatic and not lease.operations)):
                del self.leases[key]

    @staticmethod
    def _intent(action, payload):
        return hashlib.sha256(json.dumps({"action": action, **payload}, sort_keys=True).encode()).hexdigest()

    def _replay(self, request_id, fingerprint):
        old = self.receipts.get(request_id)
        if old:
            if old[1] != fingerprint:
                raise Conflict("This warm request ID belongs to different intent")
            return self.leases[old[2]]
        if len(self.receipts) >= 256:
            raise Conflict("Warm request retention is full; retry after older receipts expire")
        return None

    def _remember(self, request_id, fingerprint, lease):
        self.receipts[request_id] = (self.now_ns(), fingerprint, lease.id)

    def _retire(self, lease, state):
        if lease.state in TERMINAL:
            return
        lease.state, lease.ended_ns = state, self.now_ns()
        lease.connection = {**lease.connection, "retention_release": "pending"}
        if lease.automatic:
            if state == "failed":
                failures = min(6, self._auto_failures.get(lease.room_id, 0)+1)
                self._auto_failures[lease.room_id] = failures
                self._auto_next[lease.room_id] = self.now_ns()+min(120, 5*2**(failures-1))*1_000_000_000
            else:
                self._auto_next[lease.room_id] = self.now_ns()
        for operation in lease.operations:
            if operation is not asyncio.current_task() and not operation.done():
                operation.cancel()
        task = asyncio.create_task(self._release_backend(lease), name="room-warm-release")
        self._cleanup.add(task)
        task.add_done_callback(self._cleanup.discard)

    @staticmethod
    def _track(lease, coroutine, name):
        task = asyncio.create_task(coroutine, name=name)
        lease.operations.add(task)
        lease.task = task

        def finished(operation):
            lease.operations.discard(operation)
            if not operation.cancelled():
                operation.exception()

        task.add_done_callback(finished)
        return task

    async def _release_backend(self, lease):
        try:
            result = await asyncio.wait_for(self.service.runtime.call("warm", {
                "room_id": lease.room_id, "action": "release", "lease_id": lease.id,
                "transport_fingerprint": lease.fingerprint,
                "launch_generation": lease.launch_generation,
            }), 5)
            if not isinstance(result, dict) or result.get("state") != "released":
                raise ValueError("Invalid room retention release response")
        except Exception:
            # An uncertain control connection cannot extend the native deadline.
            observation = "unconfirmed"
        else:
            observation = "acknowledged"
        async with self._lock:
            lease.connection = {**lease.connection, "retention_release": observation}

    async def acquire(self, body: WarmRequest, *, room_id=None, external_id=None):
        intent = self._intent("acquire", {**body.model_dump(), "room_id": room_id, "external_id": external_id})
        async with self.service._mutation:
            async with self._lock:
                self._prune()
                if self._closing:
                    raise Conflict("Room readiness is shutting down")
                await self._expire_locked()
                replay = self._replay(body.request_id, intent)
                if replay:
                    return replay.public(self.now_ns())
                room = (await self.service.resolve_nobly(external_id) if external_id is not None
                        else await self.service._store("get_room", room_id))
                if not room.enabled or not room.speakers:
                    raise Conflict("Enable the target room and select speakers before warming it")
                if len(self.leases) >= 64 or sum(
                    lease.room_id == room.id and lease.state not in TERMINAL
                    for lease in self.leases.values()
                ) >= 4:
                    raise Conflict("Room warm lease capacity is full")
                lease = RoomLease(uuid4().hex, room.id, transport_fingerprint(room),
                                  self.now_ns()+body.ttl_seconds*1_000_000_000,
                                  body.purpose, body.model_id or (
                                      DEFAULT_MODEL_ID if body.purpose == "interaction" else None))
                self.leases[lease.id] = lease
                self._remember(body.request_id, intent, lease)
                self._track(lease, self._prepare(lease), "room-warm-prepare")
                return lease.public(self.now_ns())

    async def _prepare(self, lease):
        if lease.state in TERMINAL or self.now_ns() >= lease.deadline_ns:
            async with self._lock:
                self._retire(lease, "expired")
            return
        model_task = (asyncio.create_task(self._prepare_model(lease), name="room-model-warm")
                      if lease.model_id is not None and self.tts is not None else None)
        try:
            result = await asyncio.wait_for(self.service.runtime.call("warm", {
                "room_id": lease.room_id, "action": "acquire", "lease_id": lease.id,
                "deadline_monotonic_ns": lease.deadline_ns,
                "transport_fingerprint": lease.fingerprint,
                "launch_generation": lease.launch_generation,
            }), min(20, max(0, (lease.deadline_ns-self.now_ns())/1e9)))
            self._validate_connection(result)
            async with self._lock:
                if lease.state in TERMINAL:
                    return
                if self.now_ns() >= lease.deadline_ns:
                    self._retire(lease, "expired")
                    return
                lease.launch_generation = result["launch_generation"]
                lease.connection = result
                lease.state = result["state"]
                if lease.automatic:
                    self._auto_next[lease.room_id] = self.now_ns()+30_000_000_000
                    self._auto_failures.pop(lease.room_id, None)
            if model_task is not None:
                observation = await model_task
                async with self._lock:
                    if lease.state not in TERMINAL:
                        lease.model = observation
        except asyncio.CancelledError:
            raise
        except Exception:
            async with self._lock:
                if lease.state not in TERMINAL:
                    lease.connection = {"state": "unavailable"}
                    self._retire(lease, "failed")
        finally:
            if model_task is not None:
                if not model_task.done():
                    model_task.cancel()
                await asyncio.gather(model_task, return_exceptions=True)

    async def _prepare_model(self, lease):
        try:
            result = await asyncio.wait_for(self.tts.warm(lease.model_id, purpose=lease.purpose), 5)
            if not isinstance(result, dict):
                raise ValueError("Invalid model readiness response")
            return result
        except Exception:
            return {"state": "unavailable", "model_id": lease.model_id}

    @staticmethod
    def _validate_connection(result):
        if (not isinstance(result, dict) or result.get("state") not in {"connected", "degraded"}
                or not isinstance(result.get("launch_generation"), str)
                or not re.fullmatch(r"[0-9a-f]{32}", result["launch_generation"])
                or type(result.get("connected")) is not bool
                or type(result.get("output_count")) is not int or not 0 <= result["output_count"] <= 128
                or result["state"] == "connected" and (
                    not result["connected"] or result["output_count"] == 0)):
            raise ValueError("Invalid room connection receipt")

    def _get(self, room_id, lease_id):
        lease = self.leases.get(lease_id)
        if lease is None or lease.room_id != room_id or lease.automatic:
            raise NotFound("This room warm lease does not exist")
        return lease

    async def _expire_locked(self):
        for lease in self.leases.values():
            if lease.state not in TERMINAL and self.now_ns() >= lease.deadline_ns:
                self._retire(lease, "expired")

    async def get(self, room_id, lease_id):
        async with self._lock:
            await self._expire_locked()
            lease = self._get(room_id, lease_id)
            observe = lease.state in {"connected", "degraded"}
        if observe:
            try:
                result = await asyncio.wait_for(self.service.runtime.call("warm", {
                    "room_id": room_id, "action": "observe", "lease_id": lease_id,
                    "transport_fingerprint": lease.fingerprint,
                    "launch_generation": lease.launch_generation,
                }), 2)
                self._validate_connection(result)
            except Exception as exc:
                async with self._lock:
                    if lease.state not in TERMINAL:
                        if isinstance(exc, RpcError) and exc.code in {"session_conflict", "room_not_found"}:
                            self._retire(lease, "revoked")
                        else:
                            lease.state = "degraded"
                            lease.connection = {"state": "unavailable"}
            else:
                async with self._lock:
                    if lease.state not in TERMINAL:
                        if result["launch_generation"] != lease.launch_generation:
                            self._retire(lease, "revoked")
                        else:
                            lease.connection, lease.state = result, result["state"]
        async with self._lock:
            await self._expire_locked()
            return lease.public(self.now_ns())

    async def renew(self, room_id, lease_id, body: WarmRenew):
        intent = self._intent("renew", {**body.model_dump(), "room_id": room_id, "lease_id": lease_id})
        async with self.service._mutation:
            async with self._lock:
                self._prune()
                await self._expire_locked()
                replay = self._replay(body.request_id, intent)
                if replay:
                    return replay.public(self.now_ns())
                lease = self._get(room_id, lease_id)
                room = await self.service._store("get_room", room_id)
                if transport_fingerprint(room) != lease.fingerprint:
                    self._retire(lease, "revoked")
                if lease.state in TERMINAL:
                    raise Conflict("This warm lease has ended; acquire a new lease")
                lease.deadline_ns = max(lease.deadline_ns, self.now_ns()+body.ttl_seconds*1_000_000_000)
                self._remember(body.request_id, intent, lease)
                # One exact task at a time; a renewal during preparation is
                # forwarded after that preparation finishes, never cancelled.
                previous = lease.task
                self._track(lease, self._renew_after(lease, previous), "room-warm-renew")
                return lease.public(self.now_ns())

    async def _renew_after(self, lease, previous):
        if previous is not None:
            await asyncio.gather(previous, return_exceptions=True)
        if lease.state not in TERMINAL:
            await self._prepare(lease)

    async def release(self, room_id, lease_id):
        async with self._lock:
            await self._expire_locked()
            lease = self._get(room_id, lease_id)
            self._retire(lease, "released")
            return lease.public(self.now_ns())

    async def reconcile_locked(self, rooms=None):
        """Called under the service mutation guard after desired intent changes."""
        rooms = {room.id: room for room in (
            rooms if rooms is not None else await self.service._store("list_rooms"))}
        async with self._lock:
            self._prune()
            await self._expire_locked()
            for lease in self.leases.values():
                room = rooms.get(lease.room_id)
                if lease.state not in TERMINAL and (room is None or transport_fingerprint(room) != lease.fingerprint):
                    self._retire(lease, "revoked")
                    if room is not None and room.enabled and room.speakers:
                        self.touch(room.id)
                if room is not None and not room.enabled:
                    self._auto_until.pop(room.id, None)

    def touch(self, room_id):
        self._auto_until[room_id] = self.now_ns()+300_000_000_000

    async def maintain_locked(self, observed=None, *, rooms=None):
        """Autonomous connection readiness, called after runtime reconciliation.

        Successful holds renew every thirty seconds with a sixty-second crash
        guard. Failures back off; no generated audio or speech owner is used.
        """
        if not self.automatic or self._closing:
            return
        rooms = {room.id: room for room in (
            rooms if rooms is not None else await self.service._store("list_rooms"))}
        raw_rooms = observed.get("rooms", []) if isinstance(observed, dict) else []
        activity = {entry["room_id"]: entry["activity"]
                    for entry in raw_rooms if isinstance(entry, dict)
                    and isinstance(entry.get("room_id"), str) and isinstance(entry.get("activity"), dict)
                    } if isinstance(raw_rooms, list) else {}
        async with self._lock:
            self._prune()
            await self._expire_locked()
            for room_id in list(self._auto):
                if room_id not in rooms:
                    self._auto.pop(room_id, None)
                    self._auto_next.pop(room_id, None)
                    self._auto_failures.pop(room_id, None)
                    self._auto_until.pop(room_id, None)
                    self._auto_fingerprints.pop(room_id, None)
            for room in rooms.values():
                current_fingerprint = transport_fingerprint(room)
                if self._auto_fingerprints.get(room.id) != current_fingerprint:
                    self.touch(room.id)
                self._auto_fingerprints[room.id] = current_fingerprint
                if not room.enabled or not room.speakers:
                    continue
                recent = activity.get(room.id, {})
                observed_ns = recent.get("observed_monotonic_ns")
                if (type(observed_ns) is int and 0 <= self.now_ns()-observed_ns <= 15_000_000_000
                        and (recent.get("music_active") is True or recent.get("speech_active") is True)):
                    self.touch(room.id)
                self._auto_until.setdefault(room.id, self.now_ns()+300_000_000_000)
                lease = self.leases.get(self._auto.get(room.id))
                if self.mode == "adaptive" and self.now_ns() >= self._auto_until[room.id]:
                    if lease is not None:
                        self._retire(lease, "expired")
                    continue
                if self.now_ns() < self._auto_next.get(room.id, 0):
                    continue
                if lease is not None and lease.operations:
                    continue
                if lease is not None and lease.state not in TERMINAL:
                    if lease.state == "degraded":
                        self._retire(lease, "failed")
                        continue
                    lease.deadline_ns = min(self.now_ns()+60_000_000_000,
                                            self._auto_until[room.id] if self.mode == "adaptive" else 2**63-1)
                    self._track(lease, self._prepare(lease), "automatic-room-warm-renew")
                else:
                    if len(self.leases) >= 64:
                        continue
                    lease = RoomLease(uuid4().hex, room.id, transport_fingerprint(room),
                                      min(self.now_ns()+60_000_000_000,
                                          self._auto_until[room.id] if self.mode == "adaptive" else 2**63-1),
                                      "presence", None,
                                      automatic=True)
                    self.leases[lease.id] = lease
                    self._auto[room.id] = lease.id
                    self._track(lease, self._prepare(lease), "automatic-room-warm-prepare")

    def room_observation(self, room_id):
        lease = self.leases.get(self._auto.get(room_id))
        observation = lease.public(self.now_ns()) if lease else None
        if observation is not None:
            observation.pop("lease_id")
            observation.pop("admitted_room_id")
        return {"mode": self.mode,
                "hold": observation}

    async def close(self):
        async with self._lock:
            self._closing = True
            for lease in self.leases.values():
                self._retire(lease, "released")
            tasks = [task for lease in self.leases.values() for task in lease.operations]
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*self._cleanup, return_exceptions=True)
