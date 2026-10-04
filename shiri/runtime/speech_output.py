"""Bounded speech delivery to OwnTone's last common PCM mix point.

Music uses the native framed FIFO. Speech uses a separate authenticated Unix
datagram socket, so a queued music timeline cannot delay an announcement.
This producer never changes the player, music owner or presentation clock.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import socket
import stat
import struct
import time
from uuid import UUID

import numpy as np

from shiri.rpc import RpcError

MAGIC = b"SHRITTS1"
HEADER = struct.Struct("!8sBBHIQ16s16sQIIII16s")
HEADER_BYTES = 96
MAX_FRAMES = 960
RATE = 48000
PCM = 1
CONTROL = 2
assert HEADER.size == HEADER_BYTES


def identifier(value: str, *, launch=False) -> bytes:
    try:
        parsed = UUID(value)
        if not parsed.int or value != (parsed.hex if launch else str(parsed)):
            raise ValueError
        return parsed.bytes
    except (ValueError, AttributeError, TypeError):
        raise ValueError("Speech output requires an exact room and launch identity") from None


class SpeechOutput:
    def __init__(self, path: Path, room_id: str, launch: str, peer_uid: int, *,
                 now_ns=time.monotonic_ns):
        self.room = identifier(room_id)
        self.launch = identifier(launch, launch=True)
        if (not isinstance(path, Path) or not path.is_absolute()
                or ".." in path.parts or len(os.fsencode(path)) >= 108
                or type(peer_uid) is not int or not 0 < peer_uid < 2**32):
            raise ValueError("Speech output requires a private path and exact output UID")
        self.path, self.peer_uid, self.now_ns = path, peer_uid, now_ns
        self.socket = None
        self.sequence = 0
        self.owner = None
        self.active = False
        self.duck_gain = 0.28
        self.sent_frames = 0
        self.dropped_frames = 0
        self.sent_controls = 0
        self.error = None
        self.error_errno = None
        self._last_control_state = None
        self._last_control_ns = 0
        self.envelope = 0
        self.first_active_control_ns = None

    def open(self):
        if self.socket is not None:
            return
        parent, endpoint = self.path.parent.lstat(), self.path.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != self.peer_uid
                or parent.st_gid != os.getgid() or stat.S_IMODE(parent.st_mode) != 0o2710
                or not stat.S_ISSOCK(endpoint.st_mode) or endpoint.st_uid != self.peer_uid
                or endpoint.st_gid != os.getgid() or stat.S_IMODE(endpoint.st_mode) != 0o660):
            raise PermissionError("Speech endpoint does not match the admitted output identity")
        channel = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            channel.setblocking(False)
            channel.connect(str(self.path))
        except BaseException:
            channel.close()
            raise
        self.socket = channel

    def begin(self, speech_id: str, *, attack_ms=0, release_ms=0):
        """Install only an authenticated player-thread admitted voice ID."""
        owner = identifier(speech_id, launch=True)
        if (type(attack_ms) is not int or type(release_ms) is not int
                or not ((attack_ms == release_ms == 0)
                        or (40 <= attack_ms <= 2000 and 40 <= release_ms <= 5000))):
            raise ValueError("Speech envelope requires bounded attack and release durations")
        envelope = (attack_ms << 16) | release_ms
        if self.owner is not None and self.owner != owner:
            raise RpcError("session_conflict", "Another admitted speech voice owns this output")
        if self.owner == owner and self.envelope != envelope:
            raise RpcError("session_conflict", "An admitted speech envelope cannot change")
        self.owner = owner
        self.envelope = envelope
        self.first_active_control_ns = None
        self.active = False
        self._last_control_state = None
        self._last_control_ns = 0

    def retire(self, speech_id: str):
        """Fence delayed local sends without replacing a successor owner."""
        if self.owner == identifier(speech_id, launch=True):
            self.owner = None
            self.active = False
            self._last_control_state = None
            self._last_control_ns = 0

    def _send(self, operation, payload=b"", frames=0, *, active=None):
        if self.owner is None:
            return False
        self.sequence += 1
        if self.sequence > 2**63-1:
            raise RuntimeError("Speech output sequence exhausted")
        flags = int(self.active if active is None else active)
        emitted_ns = self.now_ns()
        message = HEADER.pack(MAGIC, 2, operation, HEADER_BYTES, len(payload), self.sequence,
                              self.room, self.launch, emitted_ns, frames,
                              round(self.duck_gain*65536), flags, self.envelope, self.owner)+payload
        try:
            if self.socket is None:
                self.open()
            if self.socket.send(message) != len(message):
                raise OSError("Speech datagram write was incomplete")
        except OSError as exc:
            # Speech delivery failure cannot stop or reconfigure music. Close
            # the exact channel; a later frame can retry the admitted endpoint.
            self.error = type(exc).__name__
            self.error_errno = exc.errno
            self.dropped_frames += frames
            self.close()
            return False
        self.error = None
        self.error_errno = None
        self.sent_frames += frames
        self.sent_controls += operation == CONTROL
        if operation == CONTROL and flags and self.first_active_control_ns is None:
            self.first_active_control_ns = emitted_ns
        if operation == PCM and flags:
            # Audible PCM establishes a backend lease too. Remember that
            # transition so an EOF after a short utterance cannot coalesce
            # against an older inactive control and leave music ducked.
            self._last_control_state = (True, self.duck_gain)
            self._last_control_ns = emitted_ns
        return True

    def push(self, data: bytes, samples: int):
        if (type(samples) is not int or not 0 < samples <= RATE//5
                or not isinstance(data, bytes) or len(data) != samples*2):
            raise RpcError("invalid_media", "Speech audio frame has an invalid size")
        accepted = True
        for first in range(0, samples, MAX_FRAMES):
            count = min(MAX_FRAMES, samples-first)
            payload = data[first*2:(first+count)*2]
            # Decoders can return several 20 ms packets at once. A quiet
            # packet must not acquire an audible lease merely because a later
            # packet in that decoded block contains speech.
            values = np.frombuffer(payload, dtype='<i2').astype(np.float64)
            packet_audible = bool(float(np.sqrt(np.mean(values*values))) > 65)
            accepted = self._send(PCM, payload, count, active=packet_audible) and accepted
        return accepted

    def control(self, active: bool, duck_gain: float):
        if type(active) is not bool:
            raise ValueError("Speech gain control must be finite and bounded")
        self.set_gain(duck_gain)
        self.active = active
        now = self.now_ns()
        state = (active, self.duck_gain)
        if state == self._last_control_state and (not active or now-self._last_control_ns < 100_000_000):
            return True
        sent = self._send(CONTROL)
        if sent:
            self._last_control_state, self._last_control_ns = state, now
        return sent

    def set_gain(self, duck_gain: float):
        """Prepare this media session's policy without emitting or ducking."""
        if (isinstance(duck_gain, bool) or not isinstance(duck_gain, (int, float))
                or not math.isfinite(duck_gain) or not 0 <= duck_gain <= 1):
            raise ValueError("Speech gain control must be finite and bounded")
        self.duck_gain = float(duck_gain)

    def health(self):
        return {"speech_mix": "owntone_player", "speech_sent_frames": self.sent_frames,
                "speech_dropped_frames": self.dropped_frames, "speech_sent_controls": self.sent_controls,
                "speech_output_error": self.error, "speech_output_errno": self.error_errno}

    def close(self):
        if self.socket is not None:
            self.socket.close()
            self.socket = None
