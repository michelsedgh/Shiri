"""Room intent, discovery and runtime observations meet at this service boundary."""
import asyncio
import hashlib
import time
import re

from shiri.domain import AirplayTiming, Conflict, NotFound, Room, RoomCreate, RoomPatch, SpeakerRef, ValidationIssue, speaker_key, validate_local_audio_device
from shiri.rpc import RpcError
from pydantic import ValidationError
from shiri.calibration import CalibrationSession, CalibrationSessions, analyze_wav, available, fingerprint, probe_wav

CAPABILITIES = {
    "inputs": {"airplay": True, "webrtc_speech": True, "google_cast": False},
    "outputs": {"airplay1": "protocol_timed", "airplay2": "protocol_timed",
                "chromecast": "approximate", "alsa": "device_dependent", "pulseaudio": "adapter_required"},
    "sync": "AirPlay relays preserve the phone's presentation timeline across selected zones. Whole-path native grouping and mixed physical outputs still require measurement.",
    "google_cast_receiver": "Google's receiver SDK targets Cast-compatible devices; no generic Linux Cast input adapter is installed.",
    "bluetooth": "Use a paired Linux Bluetooth audio adapter exposed as an ALSA device; delay depends on the device.",
    "nobly": "Room-addressed speech API available; an external Nobly application is not configured.",
    "calibration": "Measure within-zone or cross-zone arrival, jitter and short-window drift from shared-clock stereo recordings; review guarded corrections with the target zone off and verify with fresh recordings.",
}


async def _calibration_completion(work):
    """Keep the resource guard until bounded work actually finishes.

    Canceling to_thread's future cannot stop its FFT or SQLite thread. Repeated
    caller cancellation must not release a guard and admit more worker memory,
    or discard the receipt for a profile that has already committed.
    """
    interrupted = False
    while True:
        try:
            result = await asyncio.shield(work)
            break
        except asyncio.CancelledError:
            if work.cancelled():
                raise
            interrupted = True
    if interrupted:
        raise asyncio.CancelledError
    return result


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
        self.calibrations = CalibrationSessions()
        self._calibration_analysis = asyncio.Lock()

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
        room = await self._store("get_room", room_id)
        requested_timing = {speaker.id: speaker.airplay_timing for speaker in room.speakers if speaker.protocol == "airplay2"}
        result = await self.runtime.call("outputs", {"room_id": room_id})
        raw = result.get("outputs")
        if not isinstance(raw, list) or len(raw) > 1024:
            raise RpcError("invalid_response", "Runtime did not return a bounded output list")
        outputs = []
        for entry in raw:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not isinstance(entry.get("name"), str):
                raise RpcError("invalid_response", "Runtime returned an invalid speaker identity")
            output = dict(entry)
            output["requested_airplay_timing"] = requested_timing.get(output["id"]) if output.get("protocol") == "airplay2" else None
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
                      "interface_details": interfaces.get("interfaces", []),
                      "capabilities": {**CAPABILITIES, "calibration_analysis": available()}}
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

    async def local_devices(self):
        result = await self.runtime.call("local_devices")
        devices = result.get("devices")
        if not isinstance(devices, list) or len(devices) > 512:
            raise RpcError("invalid_response", "Runtime returned an invalid local speaker inventory")
        for item in devices:
            if (not isinstance(item, dict) or not isinstance(item.get("selection_id"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", item["selection_id"])
                    or not isinstance(item.get("label"), str) or not 1 <= len(item["label"]) <= 256
                    or any(ord(char) < 32 or ord(char) == 127 for char in item["label"])
                    or not isinstance(item.get("can_bind_by"), list)
                    or not item["can_bind_by"] or len(item["can_bind_by"]) > 4
                    or any(not isinstance(mode, str) for mode in item["can_bind_by"])
                    or len(set(item["can_bind_by"])) != len(item["can_bind_by"])
                    or any(mode not in {"serial", "port", "path", "loopback"} for mode in item["can_bind_by"])
                    or not isinstance(item.get("bindings"), list)
                    or len(item["bindings"]) != len(item["can_bind_by"])):
                raise RpcError("invalid_response", "Runtime returned an invalid local speaker identity")
            for binding, mode in zip(item["bindings"], item["can_bind_by"], strict=True):
                if (not isinstance(binding, dict) or binding.get("binding") != mode
                        or set(binding) != {"binding", "device"}):
                    raise RpcError("invalid_response", "Runtime returned an invalid local speaker binding")
                if binding["device"] is not None:
                    try:
                        validate_local_audio_device(binding["device"])
                        if not binding["device"].startswith("shiri:device="):
                            raise ValueError("Expected a saved device binding")
                    except (ValueError, AttributeError, TypeError) as exc:
                        raise RpcError("invalid_response", "Runtime returned an invalid saved speaker binding") from exc
        return {"devices": devices, "simulation": bool(result.get("simulation", False))}

    async def bind_local_device(self, selection_id, binding, conversion=True):
        result = await self.runtime.call("bind_local_device", {"selection_id": selection_id,
                                                               "binding": binding, "conversion": conversion})
        try:
            device = result["device"]
            validate_local_audio_device(device)
            if not device.startswith("shiri:device=") or result.get("binding") != binding:
                raise ValueError("Unexpected physical binding")
            label = result["label"]
            if (not isinstance(label, str) or not 1 <= len(label) <= 256
                    or any(ord(char) < 32 or ord(char) == 127 for char in label)):
                raise ValueError("Invalid device label")
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise RpcError("invalid_response", "Runtime did not acknowledge the verified local speaker binding") from exc
        self.invalidate()
        return {"device": device, "label": label, "binding": binding}

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
            self.calibrations.remove_room(room_id)
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
            return await self._offset_locked(room_id, speaker_id, offset_ms, revision)

    async def balance(self, room_id: str, speaker_id: str, balance_percent: int, revision: int):
        async with self._mutation:
            await self.sync_phone_volume()
            room = await self._store("update_speaker_balance", room_id, speaker_id, balance_percent, revision)
            self.invalidate()
            return {"room": room.model_dump(mode="json"), **await self.reconcile()}

    async def airplay_timing(self, room_id: str, speaker_id: str, airplay_timing: AirplayTiming, revision: int):
        async with self._mutation:
            await self.sync_phone_volume()
            room = await self._store("update_speaker_airplay_timing", room_id, speaker_id, airplay_timing, revision)
            self.invalidate()
            return {"room": room.model_dump(mode="json"), **await self.reconcile()}

    async def _offset_locked(self, room_id, speaker_id, offset_ms, revision, *, on_saved=None):
        room = await self._store("get_room", room_id)
        if not any(s.id == speaker_id for s in room.speakers):
            raise NotFound("Speaker is not assigned to this room")
        room = await self._store("update_speaker_offset", room_id, speaker_id, offset_ms, revision)
        self.invalidate()
        if on_saved is not None:
            on_saved(room)
        return {"room": room.model_dump(mode="json"), **await self.reconcile()}

    async def speech(self, room_id: str, payload: dict):
        room: Room = await self._store("get_room", room_id)
        if not room.enabled and payload.get("action", "offer") not in {"close", "finish"}:
            raise Conflict("Enable this room before sending speech")
        if not room.speakers and payload.get("action", "offer") not in {"close", "finish"}:
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

    async def calibration_create(self, room_id, definition):
        async with self._mutation:
            room = await self._store("get_room", room_id)
            if room.revision != definition.expected_revision:
                raise Conflict("Room changed; reload before starting a calibration session")
            reference_id = definition.reference_room_id or room_id
            reference = await self._store("get_room", reference_id)
            cross_zone = reference.id != room.id
            if cross_zone and (definition.expected_reference_revision is None or not definition.playback_context):
                raise ValidationIssue("Cross-zone calibration requires the current reference room revision and a declared common playback/grouping context")
            if definition.expected_reference_revision is not None and reference.revision != definition.expected_reference_revision:
                raise Conflict("Reference room changed; reload before starting a calibration session")
            session = self.calibrations.create(room, **definition.model_dump(exclude={"expected_revision", "reference_room_id", "expected_reference_revision"}),
                                                reference_room=reference, enforce_capacity=False)
            async def persist():
                try:
                    return await self._save_calibration(session, expected_room_revision=room.revision, expected_reference_revision=reference.revision)
                except BaseException:
                    self.calibrations.sessions.pop(session.id, None)
                    raise
            return (await _calibration_completion(asyncio.create_task(persist()))).public()

    async def _load_calibration(self, room_id, session_id):
        session = CalibrationSession.from_record(await self._store("get_calibration", room_id, session_id))
        self.calibrations.sessions[session.id] = session
        self.calibrations.prune()
        return session

    async def _save_calibration(self, session, **kwargs):
        saved = await self._store("save_calibration", session.record(), session.generation, **kwargs)
        retained = CalibrationSession.from_record(saved)
        self.calibrations.sessions[retained.id] = retained
        self.calibrations.prune()
        return retained

    async def calibration_get(self, room_id, session_id):
        return (await self._load_calibration(room_id, session_id)).public()

    async def calibration_list(self, room_id):
        records = await self._store("list_calibrations", room_id)
        return {"sessions": [CalibrationSession.from_record(record).public() for record in records]}

    async def calibration_probe(self, room_id, session_id):
        session = await self._load_calibration(room_id, session_id)
        return self._calibration_probe_bytes(session)

    @staticmethod
    def _calibration_probe_bytes(session):
        data = probe_wav(session.seed)
        if hashlib.sha256(data).hexdigest() != session.probe_metadata["sha256"]:
            raise Conflict("The installed probe generator differs from this retained measurement; export it and start a new session")
        return data

    async def calibration_recording(self, room_id, session_id, data, verification=False):
        # One bounded FFT analysis at a time. Analyze away from the HTTP event
        # loop; cancellation cannot append evidence from an abandoned thread.
        if self._calibration_analysis.locked():
            raise Conflict("Another recording is being analyzed; wait for it to finish before importing again")
        async with self._calibration_analysis:
            session = await self._load_calibration(room_id, session_id)
            self.calibrations.check_recording(session, verification=verification)
            self._calibration_probe_bytes(session)
            analysis = asyncio.create_task(asyncio.to_thread(analyze_wav, data, session.seed, session.max_lag_ms,
                                                             session.geometry_correction_ms))
            result = await _calibration_completion(analysis)
            async with self._mutation:
                current = await self._load_calibration(room_id, session_id)
                if current.generation != session.generation:
                    raise Conflict("Calibration session changed during analysis")
                room = await self._store("get_room", room_id)
                try:
                    reference = room if session.reference_room_id == room_id else await self._store("get_room", session.reference_room_id)
                except NotFound as exc:
                    raise Conflict("The reference room was removed; export this evidence and start a new measurement") from exc
                expected = session.configuration if not verification else self._calibration_applied_configuration(session)
                if fingerprint(room) != expected:
                    raise Conflict("Speaker configuration changed; start a new calibration session")
                if reference.id != room.id and fingerprint(reference) != session.reference_configuration:
                    raise Conflict("Reference speaker identities or saved settings changed; this calibration is stale")
                backend = None
                import_environment = {"room_revision": room.revision, "volume": room.volume, "enabled": room.enabled,
                                      "reference_room_id": reference.id, "reference_room_revision": reference.revision,
                                      "reference_volume": reference.volume, "reference_enabled": reference.enabled,
                                      "observed_at": time.time(), "provenance": "User-imported PCM; runtime observation at import does not attest capture origin, chronology or native phone grouping"}
                if verification:
                    if not room.enabled or not reference.enabled:
                        raise Conflict("Enable both measured rooms and confirm output readbacks before importing post-change verification")
                    target_outputs = {output["id"]: output for output in await self.discover(room_id)}
                    reference_outputs = target_outputs if reference.id == room.id else {output["id"]: output for output in await self.discover(reference.id)}
                    reference_offset = next(s.offset_ms for s in reference.speakers if s.id == session.reference_id)
                    endpoints = [(room.id, session.target_id, session.applied_offset_ms, target_outputs),
                                 (reference.id, session.reference_id, reference_offset, reference_outputs)]
                    for _rid, sid, offset, outputs in endpoints:
                        output = outputs.get(sid, {})
                        if (not output.get("selected") or not output.get("available", True)
                                or type(output.get("offset_ms")) is not int or output["offset_ms"] != offset):
                            raise Conflict("Both measured speakers must be active with the saved OwnTone offsets reported before verification")
                    health = await self.runtime.call("health")
                    backend = {"room_revision": room.revision, "simulation": bool(health.get("simulation")),
                               "versions": health.get("versions", {}),
                               "reference_room_revision": reference.revision,
                               "reported_endpoints": [{"room_id": rid, "output_id": sid, "offset_ms": outputs[sid]["offset_ms"]}
                                                      for rid, sid, _offset, outputs in endpoints]}
                    if reference.id == room.id:
                        backend["reported_offsets"] = {sid: outputs[sid]["offset_ms"] for _rid, sid, _offset, outputs in endpoints}
                    import_environment.update(backend)
                else:
                    try:
                        health = await self.runtime.call("health")
                        import_environment.update(versions=health.get("versions", {}), simulation=bool(health.get("simulation")))
                    except RpcError:
                        import_environment["runtime_unavailable"] = True
                result["environment_at_import"] = import_environment
                self.calibrations.add_result(session, result, verification=verification)
                if backend is not None:
                    session.verification_backend.append(backend)
                async def persist():
                    return (await self._save_calibration(session, expected_room_revision=room.revision, expected_reference_revision=reference.revision)).public()
                return await _calibration_completion(asyncio.create_task(persist()))

    @staticmethod
    def _calibration_applied_configuration(session):
        # Copy without mutating the retained pre-change evidence.
        config = {**session.configuration, "speakers": [dict(speaker) for speaker in session.configuration["speakers"]]}
        for speaker in config["speakers"]:
            if speaker["id"] == session.target_id:
                speaker["offset_ms"] = session.applied_offset_ms
        return config

    async def calibration_apply(self, room_id, session_id, revision, *, rollback=False, expected_generation=None, expected_reference_revision=None):
        async with self._mutation:
            session = await self._load_calibration(room_id, session_id)
            if expected_generation is not None and session.generation != expected_generation:
                raise Conflict("Calibration evidence changed; refresh and review it before changing timing")
            # The room/profile write and its receipt share one SQL transaction.
            # The store rechecks all guards against cross-process mutations.
            async def save_and_record():
                saved, record = await self._store("commit_calibration_offset", room_id, session_id, revision, session.generation, rollback=rollback,
                                                   expected_reference_revision=expected_reference_revision)
                retained = CalibrationSession.from_record(record)
                self.calibrations.sessions[retained.id] = retained
                self.invalidate()
                outcome = {"room": saved.model_dump(mode="json"), **await self.reconcile()}
                retained.application = {"runtime_accepted": outcome["runtime_accepted"], "pending_reason": outcome.get("pending_reason"),
                                        "state": "saved_room_off", "message": "Delay saved; enable the room, verify OwnTone readback and import fresh post-change recordings"}
                try:
                    retained = await self._save_calibration(retained)
                except Conflict:
                    # Another process may already have appended fresh evidence
                    # or removed the session. Its newer history remains intact;
                    # the offset receipt was durable before reconciliation.
                    retained = CalibrationSession.from_record(record)
                return {**outcome, "calibration": retained.public()}
            # A disconnected caller must not leave a committed profile without
            # its rollback receipt. Preserve the lock until this bounded write
            # and runtime acknowledgment finish, then propagate cancellation.
            work = asyncio.create_task(save_and_record())
            return await _calibration_completion(work)

    async def calibration_delete(self, room_id, session_id):
        async with self._mutation:
            async def remove():
                await self._store("delete_calibration", room_id, session_id)
                self.calibrations.sessions.pop(session_id, None)
                return {"ok": True}
            return await _calibration_completion(asyncio.create_task(remove()))
