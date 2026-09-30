"""Read-only planning and atomic opt-in import of a legacy JSON configuration.

Migration never starts rooms or changes the source file. Unknown legacy speaker
protocols are reported for manual selection instead of guessing a transport.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field, ValidationError

from .domain import Conflict, Room, RoomCreate, SpeakerRef, StrictModel, ValidationIssue, local_audio_device_key, speaker_key

if TYPE_CHECKING:
    from .store import Store

MAX_LEGACY_BYTES = 4 * 1024 * 1024
_PROTOCOLS = {"AirPlay": "airplay1", "AirPlay 1": "airplay1", "AirPlay 2": "airplay2",
              "Chromecast": "chromecast", "ALSA": "alsa", "PulseAudio": "pulseaudio"}


class LegacyRoom(StrictModel):
    legacy_zone_id: str
    room_id: str
    create: RoomCreate
    speakers: list[SpeakerRef] = Field(default_factory=list)
    volume: int = Field(default=50, ge=0, le=100)
    duck_gain: float = Field(default=0.28, ge=0, le=1, allow_inf_nan=False)


class MigrationPlan(StrictModel):
    source_path: str
    source_sha256: str
    rooms: list[LegacyRoom]
    warnings: list[str] = Field(default_factory=list)
    applied: bool = False


def _unique_object(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError(f"Duplicate JSON key {key}")
        data[key] = value
    return data


def _reject_constant(value):
    raise ValueError(f"Non-finite legacy number {value}")


def plan_legacy(source_path: str | Path) -> MigrationPlan:
    path = Path(source_path).expanduser().resolve()
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_LEGACY_BYTES + 1)
        if len(raw) > MAX_LEGACY_BYTES:
            raise ValidationIssue("Legacy configuration exceeds 4 MiB")
        data = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (OSError, ValueError) as exc:
        raise ValidationIssue(f"Cannot read legacy configuration: {exc}; source preserved") from exc
    if not isinstance(data, dict) or not isinstance(data.get("zones"), dict):
        raise ValidationIssue("Legacy configuration must contain a zones object; source preserved")
    digest = hashlib.sha256(raw).hexdigest()
    rooms = []
    warnings = []
    names, receivers, bindings, assignments, devices = set(), set(), set(), set(), set()
    for zone_id, settings in sorted(data["zones"].items()):
        if not isinstance(settings, dict):
            raise ValidationIssue(f"Legacy zone {zone_id} must be an object")
        try:
            creation = RoomCreate(
                name=settings.get("name"), airplay_name=settings.get("airplay_name"),
                nobly_room_id=settings.get("lionos_room_id", settings.get("room_id")),
                interface=settings.get("interface"), local_audio_device=settings.get("local_audio_device"),
            )
            receiver_name = creation.airplay_name if creation.airplay_name is not None else creation.name
            if creation.name.casefold() in names or receiver_name.casefold() in receivers:
                raise Conflict("Legacy room or AirPlay receiver names conflict; source preserved")
            if creation.nobly_room_id is not None and creation.nobly_room_id in bindings:
                raise Conflict("Legacy Nobly room bindings conflict; source preserved")
            names.add(creation.name.casefold())
            receivers.add(receiver_name.casefold())
            if creation.nobly_room_id is not None:
                bindings.add(creation.nobly_room_id)
            if creation.local_audio_device is not None:
                device = local_audio_device_key(creation.local_audio_device)
                if device in devices:
                    raise Conflict("Legacy local audio device is configured for multiple rooms; source preserved")
                devices.add(device)
            raw_ids = settings.get("speakers", [])
            identities = settings.get("speaker_names", [])
            if not isinstance(raw_ids, list) or not isinstance(identities, list) or any(not isinstance(identity, dict) for identity in identities):
                raise ValidationIssue(f"Legacy speaker selection for {zone_id} is malformed")
            identity_by_id = {}
            for identity in identities:
                if identity.get("id") is not None:
                    output_id = str(identity["id"])
                    if output_id in identity_by_id:
                        raise Conflict(f"Legacy speaker identities for {zone_id} are ambiguous; source preserved")
                    identity_by_id[output_id] = identity
            ids = list(dict.fromkeys(str(output_id) for output_id in raw_ids))
            if not ids:
                ids = list(identity_by_id)
            speakers = []
            for output_id in ids:
                # Validate ids even when the legacy protocol is unavailable.
                placeholder = SpeakerRef(id=output_id, name="Legacy output", protocol="airplay2")
                identity = identity_by_id.get(output_id, {})
                protocol = identity.get("protocol") or _PROTOCOLS.get(identity.get("type"))
                key = ("owntone", placeholder.id)
                if protocol in {"alsa", "pulseaudio"} and creation.local_audio_device:
                    key = ("local", local_audio_device_key(creation.local_audio_device))
                if key in assignments:
                    raise Conflict("Legacy speaker is assigned to multiple rooms; source preserved")
                assignments.add(key)
                if protocol is None:
                    warnings.append(f"{creation.name}: speaker {output_id} needs manual selection because its legacy protocol was not stored")
                    continue
                if protocol in {"alsa", "pulseaudio"} and not creation.local_audio_device:
                    warnings.append(f"{creation.name}: local speaker {output_id} needs an explicit local audio device before it can be selected")
                    continue
                speakers.append(SpeakerRef(id=output_id, name=identity.get("name", output_id), protocol=protocol,
                                           offset_ms=identity.get("offset_ms", 0)))
            policy = settings.get("tts_policy", {})
            if not isinstance(policy, dict):
                raise ValidationIssue(f"Legacy speech policy for {zone_id} is malformed")
            level = policy.get("zone", policy.get("room", {}))
            if not isinstance(level, dict):
                raise ValidationIssue(f"Legacy speech policy for {zone_id} is malformed")
            reduction = level.get("reduction_pct", 72)
            if type(reduction) not in {int, float} or not 0 <= reduction <= 100:
                raise ValidationIssue(f"Legacy speech reduction for {zone_id} is invalid")
            migrated = LegacyRoom(legacy_zone_id=zone_id,
                                  room_id=str(uuid5(NAMESPACE_URL, f"shiri:legacy:{digest}:{zone_id}")),
                                  create=creation, speakers=speakers, volume=settings.get("master_volume", 50),
                                  duck_gain=round(1.0 - reduction / 100.0, 6))
            rooms.append(migrated)
        except ValidationError as exc:
            raise ValidationIssue(f"Legacy zone {zone_id} cannot be imported: {exc}; source preserved") from exc
    if len(rooms) > 8:
        raise Conflict("Legacy configuration exceeds the eight-room runtime capacity; source preserved")
    return MigrationPlan(source_path=str(path), source_sha256=digest, rooms=rooms, warnings=warnings)


def _validate_destination(store: Store, connection, plan: MigrationPlan) -> None:
    existing = [store._get_room(connection, row[0]) for row in connection.execute("SELECT id FROM rooms").fetchall()]
    if len(existing) + len(plan.rooms) > store.max_rooms:
        raise Conflict(f"Migration exceeds the destination's {store.max_rooms}-room capacity")
    names = {room.name.casefold() for room in existing}
    receivers = {room.airplay_name.casefold() for room in existing}
    bindings = {room.nobly_room_id for room in existing if room.nobly_room_id is not None}
    assignments = {speaker_key(speaker, room.local_audio_device) for room in existing for speaker in room.speakers}
    devices = {local_audio_device_key(room.local_audio_device) for room in existing if room.local_audio_device is not None}
    for legacy in plan.rooms:
        if legacy.create.name.casefold() in names or (legacy.create.airplay_name or legacy.create.name).casefold() in receivers:
            raise Conflict("A migration room or AirPlay name conflicts with the destination")
        if legacy.create.nobly_room_id is not None and legacy.create.nobly_room_id in bindings:
            raise Conflict("A migration Nobly room binding conflicts with the destination")
        if any(speaker_key(speaker, legacy.create.local_audio_device) in assignments for speaker in legacy.speakers):
            raise Conflict("A migration speaker is already assigned in the destination")
        if legacy.create.local_audio_device is not None and local_audio_device_key(legacy.create.local_audio_device) in devices:
            raise Conflict("A migration local audio device is already configured in the destination")


def import_legacy(source_path: str | Path, dry_run: bool = True, *, store: Store | None = None) -> MigrationPlan:
    if type(dry_run) is not bool:
        raise ValidationIssue("dry_run must be true or false")
    plan = plan_legacy(source_path)
    if dry_run:
        if store is not None:
            with store._transaction(write=False) as connection:
                _validate_destination(store, connection, plan)
        return plan
    if store is None:
        raise ValidationIssue("A destination Store is required for an applied migration")
    with store._transaction() as connection:
        _validate_destination(store, connection, plan)
        for legacy in plan.rooms:
            existing = connection.execute("SELECT id FROM rooms WHERE id=?", (legacy.room_id,)).fetchone()
            if existing:
                raise Conflict("This legacy room has already been imported")
            room = store._insert_room(connection, legacy.create, room_id=legacy.room_id)
            room = Room.model_validate({**room.model_dump(), "speakers": legacy.speakers,
                                       "volume": legacy.volume, "duck_gain": legacy.duck_gain, "enabled": False})
            store._write_speakers(connection, room)
            store._update_row(connection, room)
            store._event(connection, "room.imported", f"Imported disabled legacy room {room.name}", room.id)
    return MigrationPlan.model_validate({**plan.model_dump(), "applied": True})
