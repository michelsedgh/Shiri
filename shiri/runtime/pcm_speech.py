"""Exact, bounded direct speech PCM admission; no transport or music ownership.

The generator paces one mono 48 kHz stream. This receiver rejects replay,
gaps and excessive leading audio instead of dropping samples or silently
extending the backend's bounded speech queue. Its clocks describe admission
in this Linux process, never a speaker's acoustic render time.
"""

from array import array
import base64
import binascii
import sys
import time
from uuid import uuid4

from shiri.rpc import RpcError

RATE = 48000
MAX_SAMPLES = 960
MAX_LEAD_NS = 100_000_000
MAX_STALL_NS = 250_000_000
TAIL_NS = 250_000_000


class PcmSpeech:
    def __init__(self, *, now_ns=time.monotonic_ns):
        self.stream_id = uuid4().hex
        self.now_ns = now_ns
        self.prepared_ns = None
        self.sequence = 0
        self.frames = 0
        self.next_ns = None
        self.first_admitted_ns = None
        self.first_audible_ns = None
        self.last_admitted_ns = None
        self.closed = False

    def prepared(self, session_id, request_id):
        self.prepared_ns = self.now_ns()
        return {"ok": True, "session_id": session_id, "request_id": request_id,
                "stream_id": self.stream_id, "format": "s16le", "sample_rate": RATE,
                "channels": 1, "max_samples": MAX_SAMPLES, **self.receipt()}

    def receipt(self):
        return {"stream_id": self.stream_id, "next_sequence": self.sequence + 1,
                "next_frame_index": self.frames, "admitted_frames": self.frames,
                "pcm_prepared_monotonic_ns": self.prepared_ns,
                "prepared_to_first_pcm_ms": None if self.first_admitted_ns is None or self.prepared_ns is None else
                (self.first_admitted_ns - self.prepared_ns) / 1_000_000,
                "first_pcm_admitted_monotonic_ns": self.first_admitted_ns,
                "first_audible_pcm_admitted_monotonic_ns": self.first_audible_ns,
                "last_pcm_admitted_monotonic_ns": self.last_admitted_ns,
                "pcm_clock": "room_audio_worker_monotonic; admission_not_acoustic"}

    def identity(self, payload):
        if payload.get("stream_id") != self.stream_id:
            raise RpcError("session_conflict", "Direct speech requires its exact admitted stream identity")
        if self.closed:
            raise RpcError("not_found", "This direct speech stream has ended")

    def decode(self, payload):
        self.identity(payload)
        sequence, frame_index = payload.get("sequence"), payload.get("frame_index")
        if (type(sequence) is not int or sequence != self.sequence + 1
                or type(frame_index) is not int or frame_index != self.frames):
            raise RpcError("invalid_media", "Direct speech sequence or frame position does not match")
        encoded = payload.get("pcm_base64")
        if not isinstance(encoded, str) or not 0 < len(encoded) <= MAX_SAMPLES * 8 // 3:
            raise RpcError("invalid_media", "Direct speech requires a bounded canonical PCM frame")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise RpcError("invalid_media", "Direct speech PCM is not canonical base64") from None
        if (not data or len(data) % 2 or len(data) > MAX_SAMPLES * 2
                or base64.b64encode(data).decode("ascii") != encoded):
            raise RpcError("invalid_media", "Direct speech PCM has an invalid frame size or encoding")
        now = self.now_ns()
        samples = len(data) // 2
        next_ns = (now if self.next_ns is None else self.next_ns) + samples * 1_000_000_000 // RATE
        if next_ns > now + MAX_LEAD_NS:
            raise RpcError("media_overrun", "Direct speech producer exceeded its 100 ms leading audio bound")
        if self.next_ns is not None and now - self.next_ns > MAX_STALL_NS:
            raise RpcError("media_underrun", "Direct speech producer stalled beyond its 250 ms delivery bound")
        values = array("h", data)
        if sys.byteorder != "little":
            values.byteswap()
        audible = sum(value * value for value in values) > len(values) * 65 * 65
        return data, samples, now, next_ns, audible

    def admitted(self, samples, now, next_ns, audible):
        self.sequence += 1
        self.frames += samples
        self.next_ns = next_ns
        if self.first_admitted_ns is None:
            self.first_admitted_ns = now
        if audible and self.first_audible_ns is None:
            self.first_audible_ns = now
        self.last_admitted_ns = now
        return {"ok": True, "sequence": self.sequence, **self.receipt()}

    def finish(self, payload):
        self.identity(payload)
        if (type(payload.get("final_sequence")) is not int or payload["final_sequence"] != self.sequence
                or type(payload.get("final_frame_index")) is not int
                or payload["final_frame_index"] != self.frames):
            raise RpcError("invalid_media", "Direct speech EOF must account for every admitted frame")
        self.closed = True

    def tail_seconds(self):
        if self.last_admitted_ns is None:
            return 0.0
        return max(0.0, min(TAIL_NS, self.last_admitted_ns + TAIL_NS - self.now_ns())) / 1e9
