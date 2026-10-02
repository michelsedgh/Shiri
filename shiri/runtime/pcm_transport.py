"""Versioned final-output PCM packets, independent of codecs and schedulers.

OwnTone delivers due, gain-scaled PCM. This protocol preserves exact negotiated
caps and frame continuity; only FLUSH/END request the selected BlueALSA Drop.
TTS uses ordinary DATA packets and does not open or reset an output session.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import IntEnum
import struct
from uuid import UUID

from .bluealsa import FORMATS, PCMEndpoint
from .system import RuntimeFailure

MAGIC = b"SHRIOUT1"
VERSION = 1
HEADER = struct.Struct("!8sHHI16s16s16sQQQIHHIIQ")
HEADER_BYTES = 112
MAX_PAYLOAD = 16384
MAX_PACKET = HEADER_BYTES + MAX_PAYLOAD
MAX_QUEUE_MS = 200
CONTROL_TIMEOUT = 0.2
MAX_VALUE = 2**63 - 1
# Each native clock mapping is bounded to 1.5 ms. A pair can differ by 3 ms;
# the producer permits at most 500 ppm clock drift and 200 ms converter lag.
# The wire has converted counts only, so this lag adds a conservative 100 us
# to its clock bound. Frame counts are still exact; no receive time is used.
CLOCK_PAIR_UNCERTAINTY_NS = 3_000_000
MAX_CONVERTER_LAG_NS = 200_000_000
DRIFT_DIVISOR = 2000


def cadence_bound_ns(converted_duration_ns):
    return CLOCK_PAIR_UNCERTAINTY_NS + (converted_duration_ns + MAX_CONVERTER_LAG_NS) // DRIFT_DIVISOR + 1


class Operation(IntEnum):
    START = 1
    DATA = 2
    FLUSH = 3
    END = 4
    READY = 5
    DROP_ACK = 6


def identity(value):
    try:
        result = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise RuntimeFailure("Final PCM transport identity is malformed") from exc
    if result.int == 0:
        raise RuntimeFailure("Final PCM transport needs a nonempty exact identity")
    return result


@dataclass(frozen=True)
class Frame:
    operation: Operation
    room_id: str
    launch: str
    stream: str
    generation: int
    sequence: int
    pts_ns: int
    rate: int
    format_code: int
    channels: int
    frames: int = 0
    first_frame: int = 0
    payload: bytes = b""

    def validate(self):
        if (not isinstance(self.operation, Operation) or not isinstance(self.payload, bytes)
                or len(self.payload) > MAX_PAYLOAD
                or type(self.generation) is not int or not 1 <= self.generation <= MAX_VALUE
                or any(type(value) is not int or not 0 <= value <= MAX_VALUE
                       for value in [self.sequence, self.pts_ns, self.first_frame])
                or type(self.rate) is not int or not 1 <= self.rate <= 384000
                or type(self.format_code) is not int or self.format_code not in FORMATS
                or type(self.channels) is not int or not 1 <= self.channels <= 8
                or type(self.frames) is not int or not 0 <= self.frames <= 2**32 - 1):
            raise RuntimeFailure("Final PCM transport header is malformed or unsupported")
        for value in [self.room_id, self.launch, self.stream]:
            identity(value)
        if self.operation == Operation.DATA:
            width = FORMATS[self.format_code][1] * self.channels
            if (not self.frames or not self.pts_ns or not self.sequence
                    or self.first_frame + self.frames > MAX_VALUE or len(self.payload) != self.frames * width):
                raise RuntimeFailure("Final PCM DATA must contain complete exact-format frames")
        elif (self.payload or self.pts_ns or self.first_frame
              or (self.operation != Operation.DROP_ACK and self.frames != 0)):
            raise RuntimeFailure("Final PCM control packet cannot contain audio or a presentation time")
        if self.operation in {Operation.START, Operation.READY} and (self.generation != 1 or self.sequence != 0):
            raise RuntimeFailure("Final PCM session must start at generation one and sequence zero")
        if self.operation == Operation.DROP_ACK and self.frames not in {Operation.FLUSH, Operation.END}:
            raise RuntimeFailure("Final PCM DROP_ACK must identify its exact acknowledged operation")
        if self.operation in {Operation.FLUSH, Operation.END, Operation.DROP_ACK} and (
            self.generation < 2 or not self.sequence
        ):
            raise RuntimeFailure("Final PCM Drop controls must advance an acknowledged session generation")
        return self

    def encode(self):
        self.validate()
        return HEADER.pack(MAGIC, VERSION, int(self.operation), HEADER_BYTES,
                           identity(self.room_id).bytes, identity(self.launch).bytes, identity(self.stream).bytes,
                           self.generation, self.sequence, self.pts_ns, self.rate, self.format_code,
                           self.channels, self.frames, len(self.payload), self.first_frame) + self.payload

    @classmethod
    def decode(cls, packet):
        if not isinstance(packet, bytes) or not HEADER_BYTES <= len(packet) <= MAX_PACKET:
            raise RuntimeFailure("Final PCM packet is truncated or exceeds its bound")
        values = HEADER.unpack_from(packet)
        magic, version, operation, size = values[:4]
        if magic != MAGIC or version != VERSION or size != HEADER_BYTES or values[14] != len(packet) - HEADER_BYTES:
            raise RuntimeFailure("Final PCM packet has a malformed version or exact payload size")
        try:
            frame = cls(Operation(operation), *(str(UUID(bytes=value)) for value in values[4:7]),
                        *values[7:14], values[15], packet[HEADER_BYTES:])
        except ValueError as exc:
            raise RuntimeFailure("Final PCM packet operation or identity is invalid") from exc
        return frame.validate()

    def acknowledgment(self):
        if self.operation == Operation.START:
            return replace(self, operation=Operation.READY)
        if self.operation in {Operation.FLUSH, Operation.END}:
            return replace(self, operation=Operation.DROP_ACK, frames=int(self.operation))
        raise RuntimeFailure("Final PCM DATA does not have a transport acknowledgment")


class StreamState:
    """One peer launch/session; failed or overlapping generations never recover."""
    def __init__(self, endpoint: PCMEndpoint, room_id: str, launch: str):
        self.endpoint, self.room_id, self.launch = endpoint, str(identity(room_id)), str(identity(launch))
        self.stream, self.generation, self.sequence = None, 0, 0
        self.next_frame, self.last_pts_ns = 0, None
        self.last_first_frame, self.origin_pts_ns = 0, None
        self.pending_control, self.ended, self.error = None, False, None

    def accept(self, frame: Frame):
        if self.error:
            raise RuntimeFailure(self.error)
        try:
            frame.validate()
            if (str(identity(frame.room_id)) != self.room_id or str(identity(frame.launch)) != self.launch
                    or (frame.rate, frame.format_code, frame.channels) !=
                    (self.endpoint.rate, self.endpoint.format_code, self.endpoint.channels)):
                raise RuntimeFailure("Final PCM packet belongs to another room, launch or admitted format")
            if self.stream is None:
                if frame.operation != Operation.START:
                    raise RuntimeFailure("Final PCM DATA requires an acknowledged exact session start")
                self.stream, self.generation, self.sequence = str(identity(frame.stream)), frame.generation, frame.sequence
                return frame.acknowledgment()
            if (self.ended or self.pending_control is not None or str(identity(frame.stream)) != self.stream
                    or frame.sequence != self.sequence + 1):
                raise RuntimeFailure("Final PCM stream is ended, pending a Drop, stale or discontinuous")
            if frame.operation == Operation.DATA:
                if frame.generation != self.generation or frame.first_frame != self.next_frame:
                    raise RuntimeFailure("Final PCM frames were lost, repeated or belong to a retired generation")
                if self.last_pts_ns is not None:
                    elapsed = (frame.first_frame * 1_000_000_000 // frame.rate
                               - self.last_first_frame * 1_000_000_000 // frame.rate)
                    expected = self.last_pts_ns + elapsed
                    if abs(frame.pts_ns - expected) > cadence_bound_ns(elapsed):
                        raise RuntimeFailure("Final PCM presentation timestamps are discontinuous")
                elapsed = frame.first_frame * 1_000_000_000 // frame.rate
                if self.origin_pts_ns is not None and abs(frame.pts_ns - self.origin_pts_ns - elapsed) > cadence_bound_ns(elapsed):
                    raise RuntimeFailure("Final PCM presentation clock exceeded its cumulative origin bound")
                if self.origin_pts_ns is None:
                    self.origin_pts_ns = frame.pts_ns
                self.sequence, self.next_frame = frame.sequence, self.next_frame + frame.frames
                self.last_pts_ns, self.last_first_frame = frame.pts_ns, frame.first_frame
                return None
            if frame.operation not in {Operation.FLUSH, Operation.END} or frame.generation != self.generation + 1:
                raise RuntimeFailure("Final PCM control is stale or does not advance its exact generation")
            self.pending_control = frame
            return None
        except Exception as exc:
            self.error = str(exc) or "Final PCM stream validation failed"
            raise

    def dropped(self):
        if self.error or self.pending_control is None:
            raise RuntimeFailure(self.error or "Final PCM has no pending Drop to acknowledge")
        frame, self.pending_control = self.pending_control, None
        self.generation, self.sequence = frame.generation, frame.sequence
        self.next_frame, self.last_pts_ns = 0, None
        self.last_first_frame, self.origin_pts_ns = 0, None
        self.ended = frame.operation == Operation.END
        return frame.acknowledgment()
