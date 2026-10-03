"""Validated room definitions and errors shared by the control and runtime services.

An external room id is an exact integration key. A backend output id describes
one endpoint in OwnTone, and cannot be assigned to multiple rooms at once.
"""

from __future__ import annotations

import math
import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_ROOMS = 8
MAX_AIRPLAY_NAME_BYTES = 50
MAX_OUTPUT_ID = (1 << 64) - 1
Protocol = Literal["airplay1", "airplay2", "chromecast", "alsa", "pulseaudio"]
AirplayTiming = Literal["auto", "ptp", "ntp"]


class DomainError(Exception):
    """An expected, actionable domain error that may be exposed by the API."""


class NotFound(DomainError):
    """The requested configured object does not exist."""


class Conflict(DomainError):
    """A revision, name, room binding, or output assignment conflicts."""


class ValidationIssue(DomainError):
    """A proposed configuration violates a domain invariant."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, validate_default=True, revalidate_instances="always")


def _name(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 128 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Name must contain 1 to 128 characters without control characters")
    return value


def _airplay_name(value: str) -> str:
    value = _name(value)
    if len(value.encode("utf-8")) > MAX_AIRPLAY_NAME_BYTES:
        raise ValueError("AirPlay name must fit within 50 UTF-8 bytes")
    # Shairport expands these sequences; accepting them changes the advertised
    # name and defeats the receiver-loop exclusion based on exact names.
    if re.search(r"%[hHvV]", value):
        raise ValueError("AirPlay names cannot contain Shairport substitution sequences")
    return value


def _interface(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}", value):
        raise ValueError("Interface must be a Linux device name of 1 to 15 ASCII characters")
    if value == "lo":
        raise ValueError("A LAN interface is required; loopback cannot advertise room audio")
    return value


def _external_room_id(value: str | None) -> str | None:
    if value is not None and (
        not value or value != value.strip() or len(value) > 128
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("Nobly room id must be an exact nonempty identifier without surrounding whitespace")
    return value


def validate_local_audio_device(value: str | None) -> str | None:
    """Validate a playback endpoint without substituting another device.

    Numeric card identities remain normalizable for explicit migration/audit
    analysis, but cannot be admitted as persisted or opened playback intent.
    """
    if value is not None:
        canonical = local_audio_device_key(value)
        if re.fullmatch(r"hw:CARD=\d+,DEV=\d+,SUBDEV=\d+", canonical):
            raise ValueError(
                "Numeric ALSA CARD IDs can target a different device after restart; "
                "choose and verify a stable named CARD, for example "
                "plughw:CARD=KitchenDAC,DEV=0 or hw:CARD=Loopback,DEV=1,SUBDEV=7"
            )
    return value


def local_audio_device_key(value: str) -> str:
    """Allow explicit hardware endpoints, never root-executed ALSA plugin DSL.

    hw/plughw names and omitted zero indexes identify the same physical PCM.
    Named cards remain case sensitive. Numeric normalization is for explicit
    alias/migration analysis only; room admission and playback reject it.
    """
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError("Local audio device must be an explicit hardware or BlueALSA endpoint")
    if value.startswith("shiri:device="):
        raw = value.removeprefix("shiri:device=")
        try:
            identifier = str(UUID(raw))
        except ValueError as exc:
            raise ValueError("Local speaker binding must use its exact saved device identifier") from exc
        if identifier != raw:
            raise ValueError("Local speaker binding must use its exact saved device identifier")
        return value
    card = r"(?P<card>[A-Za-z0-9_][A-Za-z0-9_-]{0,63})"
    number = r"(?:0|[1-9][0-9]{0,2})"
    match = re.fullmatch(
        rf"(?:hw|plughw):{card}(?:,(?P<device>{number})(?:,(?P<subdevice>{number}))?)?", value
    ) or re.fullmatch(
        rf"(?:hw|plughw):CARD={card}(?:,DEV=(?P<device>{number}))?(?:,SUBDEV=(?P<subdevice>{number}))?", value
    )
    if match:
        identity = match["card"]
        if identity.isdecimal():
            if len(identity) > 3 or int(identity) > 255 or identity != str(int(identity)):
                raise ValueError("ALSA card index must be between 0 and 255")
            identity = str(int(identity))
        device, subdevice = int(match["device"] or 0), int(match["subdevice"] or 0)
        if device > 255 or subdevice > 255:
            raise ValueError("ALSA device and subdevice indexes must be between 0 and 255")
        return f"hw:CARD={identity},DEV={device},SUBDEV={subdevice}"
    bluetooth = re.fullmatch(
        r"bluealsa:DEV=(?P<mac>[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}),PROFILE=a2dp", value
    )
    if bluetooth:
        return f"bluealsa:DEV={bluetooth['mac'].upper()},PROFILE=a2dp"
    raise ValueError(
        "Local audio device must use hw:/plughw: with an explicit card, "
        "or bluealsa:DEV=MAC,PROFILE=a2dp; arbitrary ALSA plugins and aliases are forbidden"
    )


def speaker_key(speaker: SpeakerRef, local_audio_device: str | None = None) -> tuple[str, str]:
    """Network ids are global; local outputs identify their configured audio card."""
    if speaker.protocol in {"alsa", "pulseaudio"}:
        if not local_audio_device:
            raise ValueError("A local speaker requires an explicit local_audio_device")
        return "local", local_audio_device_key(local_audio_device)
    return "owntone", speaker.id


class SpeakerRef(StrictModel):
    id: str
    name: str
    protocol: Protocol
    offset_ms: int = Field(default=0, ge=-2000, le=2000)
    balance_percent: int = Field(default=100, ge=0, le=100)
    airplay_timing: AirplayTiming = "auto"

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not re.fullmatch(r"0|[1-9][0-9]{0,19}", value) or int(value) > MAX_OUTPUT_ID:
            raise ValueError("Speaker id must be a canonical decimal OwnTone output id (unsigned 64-bit)")
        return value

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _name(value)

    @model_validator(mode="after")
    def validate_airplay_timing(self):
        if self.airplay_timing != "auto" and self.protocol != "airplay2":
            raise ValueError("Choose an AirPlay timing mode only for an AirPlay 2 speaker")
        return self


class RoomCreate(StrictModel):
    name: str
    airplay_name: str | None = None
    nobly_room_id: str | None = None
    interface: str
    local_audio_device: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _name(value)

    @field_validator("airplay_name")
    @classmethod
    def validate_airplay_name(cls, value: str | None) -> str | None:
        return _airplay_name(value) if value is not None else None

    @field_validator("interface")
    @classmethod
    def validate_interface(cls, value: str) -> str:
        return _interface(value)

    @field_validator("nobly_room_id")
    @classmethod
    def validate_room_id(cls, value: str | None) -> str | None:
        return _external_room_id(value)

    @field_validator("local_audio_device")
    @classmethod
    def validate_local_audio_device(cls, value: str | None) -> str | None:
        return validate_local_audio_device(value)

    @model_validator(mode="after")
    def validate_effective_airplay_name(self):
        _airplay_name(self.airplay_name if self.airplay_name is not None else self.name)
        return self


class RoomPatch(StrictModel):
    name: str | None = None
    airplay_name: str | None = None
    nobly_room_id: str | None = None
    interface: str | None = None
    local_audio_device: str | None = None
    enabled: bool | None = None
    volume: int | None = Field(default=None, ge=0, le=100)
    duck_gain: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_changes(self):
        for key in self.model_fields_set - {"airplay_name", "nobly_room_id", "local_audio_device"}:
            if getattr(self, key) is None:
                raise ValueError(f"{key} cannot be null")
        if self.name is not None:
            object.__setattr__(self, "name", _name(self.name))
        if self.airplay_name is not None:
            object.__setattr__(self, "airplay_name", _airplay_name(self.airplay_name))
        if self.interface is not None:
            _interface(self.interface)
        _external_room_id(self.nobly_room_id)
        validate_local_audio_device(self.local_audio_device)
        if self.duck_gain is not None and not math.isfinite(self.duck_gain):
            raise ValueError("duck_gain must be finite")
        return self


class Room(StrictModel):
    id: str
    slot: int = Field(ge=0, lt=MAX_ROOMS)
    name: str
    airplay_name: str
    nobly_room_id: str | None = None
    interface: str
    local_audio_device: str | None = None
    enabled: bool = False
    volume: int = Field(default=50, ge=0, le=100)
    duck_gain: float = Field(default=0.28, ge=0, le=1, allow_inf_nan=False)
    speakers: list[SpeakerRef] = Field(default_factory=list)
    revision: int = Field(default=1, ge=1)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        try:
            parsed = UUID(value)
        except ValueError as exc:
            raise ValueError("Room id must be a UUID") from exc
        if str(parsed) != value:
            raise ValueError("Room id must be a canonical lowercase UUID")
        return value

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _name(value)

    @field_validator("airplay_name")
    @classmethod
    def validate_airplay_name(cls, value: str) -> str:
        return _airplay_name(value)

    @field_validator("interface")
    @classmethod
    def validate_interface(cls, value: str) -> str:
        return _interface(value)

    @field_validator("nobly_room_id")
    @classmethod
    def validate_room_id(cls, value: str | None) -> str | None:
        return _external_room_id(value)

    @field_validator("local_audio_device")
    @classmethod
    def validate_local_audio_device(cls, value: str | None) -> str | None:
        return validate_local_audio_device(value)

    @model_validator(mode="after")
    def validate_speakers(self):
        keys = [speaker_key(speaker, self.local_audio_device) for speaker in self.speakers]
        if len(keys) != len(set(keys)):
            raise ValueError("A speaker can appear only once in a room")
        return self


class Event(StrictModel):
    id: int
    kind: str
    message: str
    room_id: str | None = None
    created_at: str
