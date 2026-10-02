"""Versioned PCM provenance, not a speaker scheduler.

Native receivers supply the first sample's intended presentation time. The
room forwards that association through mixing and OwnTone's existing input
timestamp seam. Numeric RTP values are diagnostics, never producer identity.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import IntEnum
import math
import struct
import time
from uuid import UUID

RATE = 48000
FRAME_BYTES = 4
HEADER_BYTES = 160
MAX_FRAMES = 8192
MAGIC = b"SHRIPCM1"
ZERO_UUID = bytes(16)
FLAG_GAP = 1
FLAG_AIRPLAY2 = 2
FLAG_GROUP_LEADER = 4
FLAG_SPEECH_ONLY = 8
KNOWN_FLAGS = FLAG_GAP | FLAG_AIRPLAY2 | FLAG_GROUP_LEADER | FLAG_SPEECH_ONLY
# Local zero-offset default. The broker always passes the frozen shared plan
# explicitly for the selected routes and their saved corrections.
RELAY_DELAY_NS = 140_000_000
_HEADER = struct.Struct("!8sBBHIIIIII16s16s16sQQQQQQQQ12x")
assert _HEADER.size == HEADER_BYTES


class TimingError(ValueError):
    pass


class Kind(IntEnum):
    BEGIN = 1
    PCM = 2
    FLUSH = 3
    END = 4
    VOLUME = 5
    GRANT = 6


class Clock(IntEnum):
    RAW = 1
    MONOTONIC = 2


def _integer(value, maximum, label, *, minimum=0):
    if type(value) is not int or not minimum <= value <= maximum:
        raise TimingError(f"Invalid bounded PCM {label}")


@dataclass(frozen=True)
class Packet:
    kind: Kind
    session: bytes
    incarnation: bytes = ZERO_UUID
    group: bytes = ZERO_UUID
    epoch: int = 0
    generation: int = 1
    sequence: int = 0
    frame_index: int = 0
    presentation_ns: int = 0
    clock_sample_ns: int = 0
    monotonic_before_ns: int = 0
    monotonic_after_ns: int = 0
    clock: Clock = Clock.RAW
    frames: int = 0
    rtp: int = 0
    flags: int = 0
    pcm: bytes = b""

    def __post_init__(self):
        if not isinstance(self.kind, Kind) or not isinstance(self.clock, Clock):
            raise TimingError("Unsupported PCM message or clock domain")
        for value in (self.session, self.incarnation, self.group):
            if not isinstance(value, bytes) or len(value) != 16:
                raise TimingError("PCM identities must be exact 16-byte UUID values")
        for key in ("epoch", "sequence", "frame_index", "presentation_ns", "clock_sample_ns",
                    "monotonic_before_ns", "monotonic_after_ns"):
            _integer(getattr(self, key), (1 << 63) - 1, key)
        _integer(self.generation, (1 << 63) - 1, "generation", minimum=1)
        _integer(self.rtp, (1 << 32) - 1, "RTP")
        _integer(self.flags, KNOWN_FLAGS, "flags")
        if self.flags & ~KNOWN_FLAGS:
            raise TimingError("Unknown PCM flags")
        if not isinstance(self.pcm, bytes):
            raise TimingError("PCM payload must be immutable bytes")
        if self.kind is Kind.PCM:
            _integer(self.frames, MAX_FRAMES, "sample count", minimum=1)
            if len(self.pcm) != self.frames * FRAME_BYTES or not self.presentation_ns:
                raise TimingError("PCM sample count or first-sample time is invalid")
        else:
            _integer(self.frames, 100 if self.kind is Kind.VOLUME else 0, "control value")
            if self.pcm:
                raise TimingError("Control messages cannot contain PCM")
        if self.session == ZERO_UUID and not self.flags & FLAG_SPEECH_ONLY:
            raise TimingError("Native PCM requires an exact producer incarnation")

    @property
    def session_id(self):
        return str(UUID(bytes=self.session))

    def encode(self):
        return _HEADER.pack(
            MAGIC, self.kind, self.clock, 1, HEADER_BYTES, len(self.pcm), RATE,
            self.frames, self.rtp, self.flags, self.session, self.incarnation, self.group,
            self.epoch, self.generation, self.sequence, self.frame_index, self.presentation_ns,
            self.clock_sample_ns, self.monotonic_before_ns, self.monotonic_after_ns,
        ) + self.pcm

    @classmethod
    def decode(cls, message):
        if not isinstance(message, bytes) or len(message) < HEADER_BYTES or len(message) > HEADER_BYTES + MAX_FRAMES * FRAME_BYTES:
            raise TimingError("Incomplete or oversized PCM message")
        fields = _HEADER.unpack_from(message)
        magic, kind, clock, sample_format, header, payload, rate, frames, rtp, flags, *tail = fields
        if (magic != MAGIC or sample_format != 1 or header != HEADER_BYTES or rate != RATE
                or payload != len(message) - HEADER_BYTES or any(message[148:160])):
            raise TimingError("Unsupported or malformed PCM envelope")
        try:
            return cls(Kind(kind), tail[0], tail[1], tail[2], *tail[3:], clock=Clock(clock),
                       frames=frames, rtp=rtp, flags=flags, pcm=message[HEADER_BYTES:])
        except ValueError as exc:
            raise TimingError(str(exc)) from exc


@dataclass(frozen=True)
class MappedTime:
    monotonic_ns: int
    uncertainty_ns: int
    bracket_ns: int


def map_native_time(packet: Packet, *, now_ns=None, maximum_age_ns=250_000_000):
    """Map one fresh RAW anchor with a measured same-host clock bracket.

    Refresh on every native packet. The projection includes Linux's maximum
    normal clock slew (500 ppm), rather than treating RAW as MONOTONIC or
    extrapolating a startup offset indefinitely.
    """
    now_ns = time.monotonic_ns() if now_ns is None else now_ns
    before, after, raw = packet.monotonic_before_ns, packet.monotonic_after_ns, packet.clock_sample_ns
    if not raw or not before or after < before or after - before > 1_000_000:
        raise TimingError("Native clock mapping has no sufficiently narrow bracket")
    midpoint = before + (after - before) // 2
    if now_ns < before or now_ns - after > maximum_age_ns:
        raise TimingError("Native clock mapping is stale or belongs to another clock")
    distance = packet.presentation_ns - raw
    if abs(distance) > 2_000_000_000:
        raise TimingError("Native presentation anchor exceeds the clock projection bound")
    if packet.clock is Clock.MONOTONIC:
        return MappedTime(packet.presentation_ns, 0, after - before)
    uncertainty = (after - before + 1) // 2 + math.ceil(abs(distance) / 2000)
    return MappedTime(midpoint + distance, uncertainty, after - before)


class StreamFence:
    """Per-incarnation native sequence/flush fence, separate from music ownership."""
    def __init__(self, session: bytes, generation=1):
        self.session, self.generation = session, generation
        self.sequence = None
        self.next_frame = None
        self.closed = False
        self.gaps = 0
        self.presentation = None
        self.clock = None

    def flush(self, packet: Packet):
        if (self.closed or packet.kind is not Kind.FLUSH or packet.session != self.session
                or packet.generation != self.generation + 1):
            raise TimingError("Stale or nonconsecutive native flush generation")
        self.generation = packet.generation
        self.sequence = self.next_frame = None
        self.presentation = self.clock = None

    def accept(self, packet: Packet):
        if (self.closed or packet.kind is not Kind.PCM or packet.session != self.session
                or packet.generation != self.generation):
            raise TimingError("PCM belongs to an ended producer or stale flush generation")
        if self.presentation is not None and (packet.presentation_ns <= self.presentation or packet.clock is not self.clock):
            raise TimingError("Native presentation clock repeated, moved backwards or changed without a flush")
        if self.sequence is not None:
            if packet.sequence <= self.sequence or packet.frame_index < self.next_frame:
                raise TimingError("Repeated or reordered native PCM")
            missing = packet.sequence != self.sequence + 1 or packet.frame_index != self.next_frame
            if missing and not packet.flags & FLAG_GAP:
                raise TimingError("Missing native PCM without an explicit gap marker")
            self.gaps += int(missing)
        self.sequence, self.next_frame = packet.sequence, packet.frame_index + packet.frames
        self.presentation, self.clock = packet.presentation_ns, packet.clock

    def end(self, packet: Packet):
        if packet.kind is not Kind.END or packet.session != self.session or packet.generation != self.generation:
            raise TimingError("Stale native end event")
        self.closed = True


def output_packet(packet: Packet, pcm: bytes, token, *, now_ns=None, relay_delay_ns=RELAY_DELAY_NS):
    """Preserve the native block anchor; add only the declared common relay delay."""
    mapped = map_native_time(packet, now_ns=now_ns)
    if token.session_id != packet.session_id:
        raise TimingError("Mixed PCM source token does not match its native producer")
    return replace(packet, incarnation=UUID(token.incarnation).bytes, epoch=token.epoch,
                   clock=Clock.MONOTONIC, presentation_ns=mapped.monotonic_ns + relay_delay_ns, pcm=pcm), mapped


class FramedFifoWriter:
    """One atomic envelope+PCM write; backpressure never builds a program queue."""
    def __init__(self, path):
        import os
        import stat
        self.path = path
        self.fd = None
        self._identity = path.lstat()
        if not stat.S_ISFIFO(self._identity.st_mode):
            raise TimingError("Timed audio output must be an existing private FIFO")
        self.maximum_frames = (os.pathconf(path, "PC_PIPE_BUF") - HEADER_BYTES) // FRAME_BYTES
        if self.maximum_frames < 1:
            raise TimingError("FIFO cannot atomically carry a timed stereo sample")
        self.maximum_frames = min(MAX_FRAMES, self.maximum_frames)
        self.sequence = 0
        self.owner = None
        self.next_frame = None
        self.gap = False
        self.written_bytes = self.dropped_bytes = 0
        self.reader_present = False

    def reset(self, owner):
        if owner != self.owner:
            self.owner = owner
            self.sequence = 0
            self.next_frame = None
            self.gap = False

    def close(self):
        import os
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        self.reader_present = False

    def write(self, packet: Packet):
        import errno
        import os
        import stat
        if packet.kind is not Kind.PCM or packet.clock is not Clock.MONOTONIC:
            raise TimingError("OwnTone framed output requires mapped timed PCM")
        owner = (packet.incarnation, packet.session, packet.epoch, packet.generation)
        if self.owner != owner:
            raise TimingError("Output PCM was not armed for its exact source generation")
        if self.fd is None:
            try:
                self.fd = os.open(self.path, os.O_WRONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            except OSError as exc:
                if exc.errno != errno.ENXIO:
                    raise
                self.dropped_bytes += len(packet.pcm)
                self.gap = True
                return False
            info = os.fstat(self.fd)
            if (not stat.S_ISFIFO(info.st_mode) or info.st_dev != self._identity.st_dev
                    or info.st_ino != self._identity.st_ino):
                self.close()
                raise TimingError("Timed audio FIFO changed ownership identity")
        for start in range(0, packet.frames, self.maximum_frames):
            frames = min(self.maximum_frames, packet.frames - start)
            self.sequence += 1
            first_frame = packet.frame_index + start
            missing = self.next_frame is not None and first_frame != self.next_frame
            flags = packet.flags | (FLAG_GAP if self.gap or missing else 0)
            piece = replace(packet, frames=frames, sequence=self.sequence, frame_index=first_frame,
                            presentation_ns=packet.presentation_ns + start * 1_000_000_000 // RATE,
                            flags=flags, pcm=packet.pcm[start * 4:(start + frames) * 4])
            message = piece.encode()
            try:
                written = os.write(self.fd, message)
                if written != len(message):
                    self.close()
                    raise TimingError("Atomic timed FIFO write was unexpectedly partial")
            except (BlockingIOError, BrokenPipeError) as exc:
                if isinstance(exc, BrokenPipeError):
                    self.close()
                self.gap = True
                self.dropped_bytes += (packet.frames - start) * 4
                return False
            self.reader_present = True
            self.written_bytes += frames * 4
            self.next_frame = first_frame + frames
            self.gap = False
        return True
