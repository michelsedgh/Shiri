"""Room intent, discovery and runtime observations meet at this service boundary."""
import asyncio
import time

from shiri.domain import Conflict, NotFound, Room, RoomCreate, RoomPatch, SpeakerRef, ValidationIssue, speaker_key
from shiri.rpc import RpcError
from pydantic import ValidationError

CAPABILITIES = {
    "inputs": {"airplay": True, "webrtc_speech": True, "google_cast": False},
    "outputs": {"airplay1": "protocol_timed", "airplay2": "protocol_timed",
                "chromecast": "approximate", "alsa": "device_dependent", "pulseaudio": "adapter_required"},
    "sync": "Speakers fed by one room sender share a timeline; separate rooms are independent timelines.",
    "google_cast_receiver": "Google's receiver SDK targets Cast-compatible devices; no generic Linux Cast input adapter is installed.",
    "bluetooth": "Use a paired Linux Bluetooth audio adapter exposed as an ALSA device; delay depends on the device.",
    "nobly": "Room-addressed speech API available; an external Nobly application is not configured.",
    "calibration": "Per-speaker timing offsets are supported; microphone measurement and drift analysis are separate from protocol clock sync.",
}


class RoomService:
    def __init__(self, store, runtime):
        self.store = store
        self.runtime = runtime
        self._mutation = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._reconcile_lock = asyncio.Lock()
        self._phone_lock = asyncio.Lock()
        self._generation = 0
        self._cached_state = None

    async def _store(self, method, *args, **kwargs):
        return await asyncio.to_thread(getattr(self.store, method), *args, **kwargs)

    def invalidate(self):
        self._generation += 1
        self._cached_state = None

    async def sync_phone_volume(self):
        # The phone callback applies immediately in the runtime, but SQLite
        # remains the durable authority. A revision prevents a late phone event
        # from overwriting a newer UI/API command.
        async with self._phone_lock:
            try:
                snapshot = await self.runtime.call("health")
            except RpcError:
                return
            for observed in snapshot.get("rooms", []):
                update = observed.get("phone_volume_update") if isinstance(observed, dict) else None
                if not isinstance(update, dict):
                    continue
                if (not isinstance(update.get("id"), str) or not 1 <= len(update["id"]) <= 128
                        or type(update.get("volume")) is not int or not 0 <= update["volume"] <= 100
                        or type(update.get("base_revision")) is not int or update["base_revision"] < 1):
                    raise RpcError("invalid_response", "Runtime returned an invalid phone volume event")
                try:
                    room, accepted, committed_revision = await self._store(
                        "apply_phone_volume", observed["room_id"], update["volume"],
                        update["base_revision"], update["id"])
                    if accepted:
                        self.invalidate()
                except NotFound:
                    continue
                try:
                    await self.runtime.call("ack_phone_volume", {"room_id": room.id, "update_id": update["id"],
                                                                 "accepted": accepted,
                                                                 "volume": update["volume"] if accepted else room.volume,
                                                                 "committed_revision": committed_revision})
                except RpcError:
                    # Atomic receipts replay the original commit even if a
                    # newer UI command has changed the room before this ACK.
                    continue

    async def reconcile(self):
        # Capture intent inside this lock, so an older background retry cannot
        # arrive after a newer mutation and restore obsolete room configuration.
        async with self._reconcile_lock:
            await self.sync_phone_volume()
            rooms = await self._store("list_rooms")
            try:
                snapshot = await self.runtime.call("reconcile", {"rooms": [r.model_dump(mode="json") for r in rooms]})
                return {"runtime_accepted": True, "runtime": snapshot}
            except RpcError as exc:
                return {"runtime_accepted": False, "pending_reason": str(exc)}

    async def discover(self, room_id):
        result = await self.runtime.call("outputs", {"room_id": room_id})
        raw = result.get("outputs")
        if not isinstance(raw, list) or len(raw) > 1024:
            raise RpcError("invalid_response", "Runtime did not return a bounded output list")
        outputs = []
        for entry in raw:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not isinstance(entry.get("name"), str):
                raise RpcError("invalid_response", "Runtime returned an invalid speaker identity")
            output = dict(entry)
            output["assignable"] = bool(output.get("assignable", True) and output.get("available", True))
            if output.get("protocol") not in {"airplay1", "airplay2", "chromecast", "alsa", "pulseaudio"}:
                output["assignable"] = False
                output["reason"] = output.get("reason") or "Unsupported backend output"
            else:
                try:
                    SpeakerRef(id=output["id"], name=output["name"], protocol=output["protocol"])
                except ValidationError as exc:
                    raise RpcError("invalid_response", "Runtime returned an invalid speaker identity") from exc
            outputs.append(output)
        return outputs

    async def state(self):
        async with self._state_lock:
            if self._cached_state and time.monotonic() - self._cached_state[0] < 0.5:
                return self._cached_state[1]
            await self.sync_phone_volume()
            generation = self._generation
            rooms = await self._store("list_rooms")
            try:
                snapshot, interfaces = await asyncio.gather(self.runtime.call("health"), self.runtime.call("interfaces"))
                snapshot.setdefault("error", None)
            except RpcError as exc:
                snapshot, interfaces = {"ready": False, "simulation": False, "error": str(exc), "rooms": []}, {"interfaces": []}
            observed = {entry["room_id"]: entry for entry in snapshot.get("rooms", [])
                        if isinstance(entry, dict) and isinstance(entry.get("room_id"), str)}
            # A backend never determines ownership. Persisted desired assignments do.
            owners = {speaker_key(s, r.local_audio_device): r.id for r in rooms for s in r.speakers}
            limit = asyncio.Semaphore(8)
            async def summarize(room):
                info = observed.get(room.id, {"status": "stopped" if not room.enabled else "error",
                                              "error": snapshot.get("error") or ("Awaiting runtime reconciliation" if room.enabled else None)})
                outputs, outputs_error = [], None
                if room.enabled and info.get("status") in {"running", "degraded"}:
                    async with limit:
                        try:
                            outputs = await self.discover(room.id)
                            for output in outputs:
                                output["assigned_room_id"] = None
                                if output["assignable"]:
                                    ref = SpeakerRef(id=output["id"], name=output["name"], protocol=output["protocol"])
                                    output["assigned_room_id"] = owners.get(speaker_key(ref, room.local_audio_device))
                        except RpcError as exc:
                            outputs_error = str(exc)
                return {**room.model_dump(mode="json"), "runtime": info, "outputs": outputs,
                        "outputs_error": outputs_error}
            summaries = await asyncio.gather(*(summarize(room) for room in rooms))
            result = {"rooms": summaries, "runtime": {k: v for k, v in snapshot.items() if k != "rooms"},
                      "interfaces": [entry if isinstance(entry, str) else entry["name"]
                                     for entry in interfaces.get("interfaces", [])
                                     if isinstance(entry, str) or isinstance(entry, dict) and entry.get("eligible") and isinstance(entry.get("name"), str)],
                      "interface_details": interfaces.get("interfaces", []), "capabilities": CAPABILITIES}
            if generation == self._generation:
                self._cached_state = (time.monotonic(), result)
            return result

    async def readiness(self):
        rooms = await self._store("list_rooms")
        try:
            health = await self.runtime.call("health")
        except RpcError:
            return False
        observed = {entry.get("room_id"): entry for entry in health.get("rooms", []) if isinstance(entry, dict)}
        return bool(health.get("ready") and all(not room.enabled or observed.get(room.id, {}).get("status") == "running" for room in rooms))

    async def create(self, definition: RoomCreate):
        async with self._mutation:
            room = await self._store("create_room", definition)
            self.invalidate()
            return {"room": room.model_dump(mode="json"), **await self.reconcile()}

    async def update(self, room_id: str, changes: RoomPatch, revision: int):
        async with self._mutation:
            await self.sync_phone_volume()
            room = await self._store("update_room", room_id, changes, revision)
            self.invalidate()
            return {"room": room.model_dump(mode="json"), **await self.reconcile()}

    async def delete(self, room_id: str, revision: int):
        async with self._mutation:
            await self._store("delete_room", room_id, revision)
            self.invalidate()
            return {"ok": True, **await self.reconcile()}

    async def assign(self, room_id: str, ids: list[str], revision: int):
        async with self._mutation:
            room = await self._store("get_room", room_id)
            if room.revision != revision:
                raise Conflict("Room changed; reload it before saving speaker assignments")
            if len(ids) != len(set(ids)):
                raise Conflict("Speaker assignments must not contain duplicate outputs")
            previous = {s.id: s for s in room.speakers}
            new_ids = set(ids) - set(previous)
            available = {}
            if new_ids:
                available = {o["id"]: o for o in await self.discover(room_id)
                             if o["assignable"] and not o.get("requires_auth")}
                if any(sid not in available for sid in new_ids):
                    raise Conflict("A requested speaker is unavailable or requires authorization; refresh discovery before saving")
            # Existing saved outputs can be retained or removed during an outage.
            refs = [previous[sid] if sid in previous else SpeakerRef(
                id=sid, name=available[sid]["name"], protocol=available[sid]["protocol"]) for sid in ids]
            # Intent is durable before the broker applies it. Failures remain visible
            # as pending/degraded, never as a fabricated live output state.
            room = await self._store("assign_speakers", room_id, refs, revision)
            self.invalidate()
            return {"room": room.model_dump(mode="json"), **await self.reconcile()}

    async def offset(self, room_id: str, speaker_id: str, offset_ms: int, revision: int):
        async with self._mutation:
            room = await self._store("get_room", room_id)
            if not any(s.id == speaker_id for s in room.speakers):
                raise NotFound("Speaker is not assigned to this room")
            room = await self._store("update_speaker_offset", room_id, speaker_id, offset_ms, revision)
            self.invalidate()
            return {"room": room.model_dump(mode="json"), **await self.reconcile()}

    async def speech(self, room_id: str, payload: dict):
        room: Room = await self._store("get_room", room_id)
        if not room.enabled and payload.get("action", "offer") != "close":
            raise Conflict("Enable this room before sending speech")
        if not room.speakers and payload.get("action", "offer") != "close":
            raise Conflict("Assign speakers to this room before sending speech")
        result = await self.runtime.call("speech", {**payload, "room_id": room_id, "duck_gain": room.duck_gain})
        return {**result, "admitted_room_id": room.id}

    async def admit_nobly(self, external_id: str, payload: dict):
        if payload.get("action", "offer") != "offer":
            raise ValidationIssue(
                "Nobly bindings admit offers only; send control/close to "
                "/api/v1/rooms/{admitted_room_id}/speech using the UUID returned by the offer"
            )
        # Edits use this same guard. The exact binding cannot move between
        # resolution and the broker's bounded admission acknowledgment. Session
        # followups use that stable room UUID, never a fresh binding lookup.
        async with self._mutation:
            room = await self.resolve_nobly(external_id)
            return await self.speech(room.id, payload)

    async def resolve_nobly(self, external_id: str):
        rooms = await self._store("list_rooms")
        matches = [room for room in rooms if room.nobly_room_id == external_id]
        if len(matches) != 1:
            raise NotFound("Nobly room is not bound to exactly one audio room")
        return matches[0]
