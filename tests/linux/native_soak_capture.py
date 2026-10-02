"""Bounded streaming evidence for the explicit digital soak only.

Lifetime counters/digests describe all observed blocks. Window receipts describe
only the retained immutable PCM; they never relabel lifetime counts as a window.
Importing this module starts no device, process, timer, or network observation.
"""

from __future__ import annotations

from collections import deque
from copy import deepcopy
import hashlib
import json
import math
from types import SimpleNamespace

from shiri.runtime.system import RuntimeFailure
from shiri.runtime.timing import RATE

RETAIN_SECONDS = 12
MAX_BLOCKS = 1024
MAX_BYTES = 4 * 1024 * 1024


def require(value, message):
    if not value:
        raise RuntimeFailure(message)


class AbsoluteSequence:
    """Bounded storage with lifetime indices for the unchanged per-buffer guard."""

    def __init__(self):
        self.values, self.first, self.end = deque(), 0, 0

    def __len__(self):
        return self.end

    def __iter__(self):
        return iter(self.values)

    def __getitem__(self, index):
        if isinstance(index, slice):
            start, stop, step = index.indices(self.end)
            require(start >= self.first, "Soak requested an evicted PCM range")
            return [self[i] for i in range(start, stop, step)]
        if index < 0:
            index += self.end
        require(self.first <= index < self.end, "Soak requested an evicted PCM block")
        return self.values[index - self.first]

    def append(self, value):
        self.values.append(value)
        self.end += 1

    def discard_first(self):
        value = self.values.popleft()
        self.first += 1
        return value


class StreamingEvidence:
    """Every block is validated before it can enter or leave the bounded ring."""

    def initialize_streaming(self):
        self.chunks, self.captured_at = AbsoluteSequence(), AbsoluteSequence()
        self.retained_bytes = 0
        self.pcm_digest, self.metadata_digest = hashlib.sha256(), hashlib.sha256()
        self.previous_anchor, self.previous_frames = None, None
        self.reference_pcm = None
        self.reference_next_frame = None
        self.reference_index = None
        self.music_verified_frames = 0
        self.music_digest = hashlib.sha256()
        self.music_first_pts_ns = None
        self.music_start_index = None
        self.evicted_blocks = self.peak_retained_bytes = self.peak_retained_blocks = 0

    def poll(self):
        require(self.error is None, self.error or "Soak final capture failed permanently")
        require(self.capture_dropped == 0, "Soak final observer dropped PCM")
        if self.needs_latency:
            self.needs_latency = False
            self.pipeline.recalculate_latency()
        try:
            while self.pending:
                at, data, rate, channels, audio_format, metadata = self.pending[0]
                frames = len(data) // 4
                require(
                    type(data) is bytes and bool(data) and len(data) % 4 == 0 and 0 < frames <= 960,
                    "Soak output lost whole bounded stereo frames",
                )
                require((rate, channels, audio_format) == (RATE, 2, "S16LE"), "Soak output changed caps")
                anchor = self.absolute.get(at)
                require(
                    type(anchor) is int and anchor > 0 and type(at) in (int, float) and math.isfinite(at),
                    "Soak output lost its actual timestamp",
                )
                if self.previous_anchor is not None:
                    require(
                        anchor > self.previous_anchor
                        and abs(anchor - self.previous_anchor - self.previous_frames * 1e9 / RATE)
                        < 20_000_000,
                        "Soak output has a backwards or unbounded anchor gap",
                    )
                require(
                    self.retained_bytes + len(data) <= MAX_BYTES and len(self.chunks.values) < MAX_BLOCKS,
                    "Soak retained PCM exceeded its declared bound before verification/eviction",
                )
                self.sequence.push(metadata, frames, rate)
                self.pcm_digest.update(data)
                self.metadata_digest.update(
                    json.dumps(
                        {
                            "index": len(self.chunks),
                            "anchor_ns": anchor,
                            "frames": frames,
                            "metadata": metadata,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                    + b"\n"
                )
                self.chunks.append(data)
                self.captured_at.append(at)
                self.total += len(data)
                self.retained_bytes += len(data)
                self.rate, self.channels, self.format = rate, channels, audio_format
                self.previous_anchor, self.previous_frames = anchor, frames
                self.peak_retained_bytes = max(self.peak_retained_bytes, self.retained_bytes)
                self.peak_retained_blocks = max(self.peak_retained_blocks, len(self.chunks.values))
                self.pending.popleft()
        except RuntimeFailure as exc:
            self.error = str(exc)[:2000]
            raise
        return {
            "capture": self.device,
            "format": self.format,
            "rate": self.rate,
            "channels": self.channels,
            "observed_bytes": self.total,
            "capture_dropped_bytes": self.capture_dropped,
            "discontinuities": self.discontinuities,
            "max_packet_gap_seconds": self.max_packet_gap,
            "common_clock_base_ns": self.expected_base,
            "clock_offset_to_monotonic_ns": self.clock_offset_ns,
            "frame_continuity": deepcopy(self.sequence.evidence()),
            "streaming": self.streaming_receipt(),
        }

    def streaming_receipt(self):
        return {
            "scope": "Lifetime counters/digests over every observed block; bounded ring is a separate window",
            "lifetime_pcm_sha256": self.pcm_digest.hexdigest(),
            "lifetime_metadata_sha256": self.metadata_digest.hexdigest(),
            "lifetime_observed_bytes": self.total,
            "music_verified_frames": self.music_verified_frames,
            "music_sha256": self.music_digest.hexdigest(),
            "music_first_sample_pts_ns": self.music_first_pts_ns,
            "retained_first_block": self.chunks.first,
            "retained_end_block": len(self.chunks),
            "retained_blocks": len(self.chunks.values),
            "retained_bytes": self.retained_bytes,
            "evicted_blocks": self.evicted_blocks,
            "maximum_retained_bytes": MAX_BYTES,
            "maximum_retained_blocks": MAX_BLOCKS,
            "peak_retained_bytes": self.peak_retained_bytes,
            "peak_retained_blocks": self.peak_retained_blocks,
        }

    def establish_reference(self, program_pcm):
        """Find the full exact first second once; never infer onset from a tone phase."""
        self.poll()
        require(
            self.reference_pcm is None and self.chunks.first == 0,
            "Soak reference must precede eviction and cannot be replaced",
        )
        raw = b"".join(self.chunks)
        prefix = program_pcm(0, frames=RATE)
        first = raw.find(prefix)
        if first < 0:
            return False
        require(
            first % 4 == 0 and raw.find(prefix, first + 4) < 0,
            "Soak first-second music reference is unaligned or ambiguous",
        )
        self.reference_pcm = program_pcm
        remaining = first
        for index, (at, data) in enumerate(zip(self.captured_at, self.chunks, strict=True)):
            if remaining >= len(data):
                remaining -= len(data)
                continue
            self.music_start_index = index
            self.music_first_pts_ns = self.absolute[at] + (remaining // 4) * 1_000_000_000 // RATE
            self.reference_index = index + 1
            self.reference_next_frame = 0
            self._verify_bytes(data[remaining:])
            break
        require(self.music_first_pts_ns is not None, "Soak reference origin is outside actual retained PCM")
        self.verify_reference()
        return True

    def _verify_bytes(self, data):
        frames = len(data) // 4
        require(
            data == self.reference_pcm(self.reference_next_frame, frames=frames),
            "Soak final PCM differs from the immutable source frame calendar",
        )
        self.music_digest.update(data)
        self.music_verified_frames += frames
        self.reference_next_frame += frames

    def verify_reference(self, *, through=None):
        require(self.reference_pcm is not None, "Soak waveform was not established")
        end = len(self.chunks) if through is None else through
        require(
            self.reference_index <= end <= len(self.chunks), "Soak reference consumed an invalid block range"
        )
        try:
            while self.reference_index < end:
                self._verify_bytes(self.chunks[self.reference_index])
                self.reference_index += 1
        except RuntimeFailure as exc:
            self.error = str(exc)[:2000]
            raise

    def evict_verified(self, guard_index):
        require(
            type(guard_index) is int and self.reference_index is not None,
            "Soak cannot evict without waveform and per-buffer guard authority",
        )
        end = min(guard_index, self.reference_index)
        require(self.chunks.first <= end <= len(self.chunks), "Soak eviction authority exceeds verified PCM")
        cutoff = self.previous_anchor - RETAIN_SECONDS * 1_000_000_000
        while self.chunks.first < end and self.absolute[self.captured_at[self.chunks.first]] < cutoff:
            at = self.captured_at.discard_first()
            data = self.chunks.discard_first()
            self.retained_bytes -= len(data)
            self.absolute.pop(at)
            self.buffer_metadata.pop(at)
            self.evicted_blocks += 1

    def snapshot(self, start_ns=None, end_ns=None):
        """A frozen window with its own actual metadata sequence and local counts."""
        self.poll()
        records = [
            (index, self.captured_at[index], self.chunks[index])
            for index in range(self.chunks.first, len(self.chunks))
            if (start_ns is None or self.absolute[self.captured_at[index]] >= start_ns)
            and (end_ns is None or self.absolute[self.captured_at[index]] < end_ns)
        ]
        require(bool(records), "Soak snapshot has no retained PCM")
        if start_ns is not None or end_ns is not None:
            require(
                self.reference_index is not None and records[-1][0] < self.reference_index,
                "Soak analysis window includes unverified music PCM",
            )
        sequence = type(self.sequence)()
        for _, at, data in records:
            metadata = deepcopy(self.buffer_metadata[at])
            # The initial marker is copied honestly. Later retained windows do
            # not manufacture a discontinuity to hide a break in lifetime PCM.
            sequence.push(metadata, len(data) // 4, RATE)
        return SimpleNamespace(
            chunks=tuple(data for _, _, data in records),
            captured_at=tuple(at for _, at, _ in records),
            absolute={at: self.absolute[at] for _, at, _ in records},
            frame_continuity=deepcopy(sequence.evidence()),
            lifetime=deepcopy(self.streaming_receipt()),
            retained_block_range=(records[0][0], records[-1][0] + 1),
        )


def capture_type(base):
    class SoakCapture(StreamingEvidence, base):
        maximum_bytes = MAX_BYTES

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.initialize_streaming()

    return SoakCapture
