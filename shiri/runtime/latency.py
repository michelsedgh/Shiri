"""Pure route buffer policy; no physical latency or runtime admission claim.

OwnTone's timed input subtracts B from the native final presentation P + H;
each output then adds B and its saved offset. All enabled rooms therefore need
the same H, even when their required B differs. A disabled room does not affect
the plan until it is enabled.

This is a configuration candidate, not a scheduler or a live reconfiguration
API. A caller must freeze one plan for a program incarnation and coordinate any
changed buffers/horizon before opening it. TTS must never change that plan.
The first P + H - B anchor must still be in the future when OwnTone arms its
timer. Native clock validation alone does not guarantee that cold-start lead.

The speaker lead is 500 ms for AirPlay1/2, 250 ms for Cast/Pulse, and two
native20ms periods for ALSA, including the private framed A2DP output.
Every enabled room shares H=max(B)+100ms; selected corrections retain each
endpoint's required lead. These are software buffers, not device measurements.
The exact pinned Shairport AP2 consumer subtracts 250 ms protocol
latency and its 150 ms backend buffer from this lead; 500 ms retains the same
100 ms packet window as the ordinary zero-offset route. A 250 ms lead becomes
negative and wraps into that receiver's unsigned latency. Applying the 500 ms
candidate to AirPlay1 too is conservative, not a measured physical profile.

The AirPlay500 ms lead meets the pinned AirPlay/RAOP source minimum, but
real receivers may ignore it or require a larger measured device profile
(including the previous 2250 ms buffer). Cast's additional transport delay and
physical output latency are not removed by this calculation. Qualification
must use the actual generated buffers and unchanged source presentation P.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from shiri.domain import MAX_ROOMS, Room, SpeakerRef

NEGATIVE_OFFSET_MARGIN_MS = 250
AIRPLAY_SPEAKER_LEAD_MS = 500
MINIMUM_LOCAL_OUTPUT_BUFFER_MS = 40
MINIMUM_COMMON_HORIZON_MARGIN_MS = 100


@dataclass(frozen=True, slots=True)
class RoomBuffer:
    """Buffer for one enabled room and the exact saved revision it describes."""

    room_id: str
    revision: int
    output_buffer_ms: int


@dataclass(frozen=True, slots=True)
class LatencyPlan:
    """An immutable plan shared by all enabled room program inputs."""

    common_horizon_ms: int
    rooms: tuple[RoomBuffer, ...]

    @property
    def common_horizon_ns(self) -> int:
        return self.common_horizon_ms * 1_000_000

    def for_room(self, room_id: str) -> RoomBuffer:
        """Return the enabled room's decision; never substitute another room."""
        for decision in self.rooms:
            if decision.room_id == room_id:
                return decision
        raise KeyError(room_id)


def _validated_room(room: Room) -> Room:
    if not isinstance(room, Room):
        raise ValueError("Latency policy requires validated Room definitions")
    # Frozen domain models still contain a mutable speaker list, and callers
    # can bypass validation with model_construct/model_copy(update=...).
    # Revalidation snapshots that list and rejects bool/noninteger offsets,
    # malformed revisions, and offsets outside the admitted +/-2000 ms range.
    return Room.model_validate(room)


def speaker_lead_ms(speaker: SpeakerRef) -> int:
    """Return the candidate lead required by one exact selected speaker.

    This is shared with explicit config admission. A protocol label does not
    establish that an arbitrary physical receiver honors the requested lead;
    unknown AirPlay compatibility and measured profiles remain release gates.
    """
    if not isinstance(speaker, SpeakerRef):
        raise ValueError("Latency policy requires a validated SpeakerRef")
    speaker = SpeakerRef.model_validate(speaker)
    if speaker.protocol == "alsa":
        return MINIMUM_LOCAL_OUTPUT_BUFFER_MS
    return AIRPLAY_SPEAKER_LEAD_MS if speaker.protocol in {"airplay1", "airplay2"} else NEGATIVE_OFFSET_MARGIN_MS


def _buffer_ms(room: Room) -> int:
    return max(MINIMUM_LOCAL_OUTPUT_BUFFER_MS,
               max((speaker_lead_ms(speaker) - min(0, speaker.offset_ms)
                    for speaker in room.speakers), default=0))


def room_buffer_ms(room: Room) -> int:
    """Return the smallest B retaining every saved selected speaker's lead.

    Availability is deliberately absent: a configured endpoint's reappearance
    must not silently change the presentation horizon of an active program.
    """
    return _buffer_ms(_validated_room(room))


def latency_plan(rooms: Sequence[Room]) -> LatencyPlan:
    """Plan at most eight rooms without reading clocks, discovery, or devices.

    AirPlay1/2 retain500ms, Cast/Pulse250ms and ALSA40ms as qualified floors.
    A saved negative offset enlarges B to preserve that route's lead; positive
    offsets never lower a protocol below its qualified floor.
    H=max(enabled B,40)+100. Admitted corrections bound B to40..2500ms
    and H to140..2600ms. Disabled definitions are validated but omitted.
    An empty/all-disabled plan has the explicit local140ms horizon.
    """
    if isinstance(rooms, (str, bytes, bytearray)) or not isinstance(rooms, Sequence):
        raise ValueError("Latency policy requires a bounded sequence of Room definitions")
    if len(rooms) > MAX_ROOMS:
        raise ValueError(f"Latency policy supports at most {MAX_ROOMS} rooms")

    definitions = tuple(_validated_room(room) for room in rooms)
    identifiers = [room.id for room in definitions]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("Latency policy requires unique room identifiers")

    decisions = tuple(sorted(
        (RoomBuffer(room.id, room.revision, _buffer_ms(room)) for room in definitions if room.enabled),
        key=lambda decision: decision.room_id,
    ))
    maximum_buffer_ms = max((decision.output_buffer_ms for decision in decisions), default=MINIMUM_LOCAL_OUTPUT_BUFFER_MS)
    return LatencyPlan(maximum_buffer_ms + MINIMUM_COMMON_HORIZON_MARGIN_MS, decisions)
