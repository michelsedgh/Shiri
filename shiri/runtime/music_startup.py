"""Bounded observations of native music onset; never a playback clock.

The receiver's BEGIN is not the phone's Play-button time, and a FIFO write is
not sound at a speaker. Keep these boundaries explicit while comparing a new
producer with its later FLUSH generations. Tokens stay private; published
records contain only worker-local counters, clocks and durations, never PCM.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy

from .timing import FLAG_GAP


class MusicStartupTrace:
    """Observe only the exact current token/generation, retaining eight takes."""

    def __init__(self):
        self._records = deque(maxlen=8)
        self._owner = None
        self._current = None
        self._sequence = 0

    def start(self, token, generation, requested_ns):
        if self._current is not None and not self._current["closed"]:
            self._current["closed"] = True
            self._current["status"] = "superseded"
        self._sequence += 1
        self._owner = (token.model_copy(deep=True), generation)
        self._current = {
            "trace_sequence": self._sequence,
            "trigger": "begin" if generation == 1 else "flush",
            "source_epoch": token.epoch,
            "native_generation": generation,
            "requested_monotonic_ns": requested_ns,
            "status": "preparing_outputs",
            "closed": False,
            "backend_prepare": None,
            "grant_ready_monotonic_ns": None,
            "grant_sent_monotonic_ns": None,
            "first_pcm": None,
            "first_nonzero_input_pcm_monotonic_ns": None,
            "first_fifo_write": None,
            "fifo_dropped_bytes_before_first_write": 0,
        }
        self._records.append(self._current)

    def _record(self, token, generation):
        if (self._current is None or self._current["closed"]
                or self._owner != (token, generation)):
            return None
        return self._current

    def _backend_record(self, owner):
        if self._owner is None:
            return None
        token, generation = self._owner
        # UUID conversion is not needed: the private token has the same exact
        # incarnation/session strings that NativeController uses for its body.
        incarnation, session, epoch, native_generation = owner
        if (token.incarnation.replace("-", "") != incarnation.hex()
                or token.session_id.replace("-", "") != session.hex()
                or token.epoch != epoch or generation != native_generation):
            return None
        return self._record(token, generation)

    def backend_started(self, owner, operation, now_ns):
        record = self._backend_record(owner)
        if record is not None:
            record["backend_prepare"] = {
                "operation_generation": operation,
                "started_monotonic_ns": now_ns,
                "completed_monotonic_ns": None,
                "elapsed_ns": None,
                "acknowledged": False,
            }

    def backend_completed(self, owner, operation, now_ns, *, acknowledged):
        record = self._backend_record(owner)
        if record is None:
            return
        preparation = record["backend_prepare"]
        if preparation is None or preparation["operation_generation"] != operation:
            return
        preparation["completed_monotonic_ns"] = now_ns
        preparation["elapsed_ns"] = now_ns - preparation["started_monotonic_ns"]
        preparation["acknowledged"] = acknowledged
        record["status"] = "waiting_for_pcm" if acknowledged else "prepare_failed"
        if not acknowledged:
            record["closed"] = True

    def grant_ready(self, token, generation, now_ns):
        record = self._record(token, generation)
        if record is not None and record["grant_ready_monotonic_ns"] is None:
            record["grant_ready_monotonic_ns"] = now_ns

    def grant_sent(self, token, generation, now_ns):
        record = self._record(token, generation)
        if record is not None and record["grant_sent_monotonic_ns"] is None:
            record["grant_sent_monotonic_ns"] = now_ns

    def pcm(self, token, packet, mapped, *, received_ns, completed_ns,
            relay_delay_ns, output_buffer_ms, written_bytes, dropped_bytes):
        record = self._record(token, packet.generation)
        if record is None:
            return
        # The first accepted block can be silence. Keep the first nonzero input
        # as a separate software boundary, without asserting it was audible.
        if record["first_nonzero_input_pcm_monotonic_ns"] is None and any(packet.pcm):
            record["first_nonzero_input_pcm_monotonic_ns"] = received_ns
        if record["first_pcm"] is None:
            record["first_pcm"] = {
                "received_monotonic_ns": received_ns,
                "processed_monotonic_ns": completed_ns,
                "sequence": packet.sequence,
                "frame_index": packet.frame_index,
                "frames": packet.frames,
                "gap": bool(packet.flags & FLAG_GAP),
                "native_clock": packet.clock.name.lower(),
                "native_presentation_ns": packet.presentation_ns,
                "mapped_presentation_monotonic_ns": mapped.monotonic_ns,
                "mapping_uncertainty_ns": mapped.uncertainty_ns,
                "output_presentation_monotonic_ns": mapped.monotonic_ns + relay_delay_ns,
                "relay_delay_ns": relay_delay_ns,
                "output_buffer_ms": output_buffer_ms,
                "native_presentation_lead_ns": mapped.monotonic_ns - received_ns,
                "requested_to_received_ns": received_ns - record["requested_monotonic_ns"],
            }
            record["status"] = "pcm_received"
        if record["first_fifo_write"] is None:
            if written_bytes > 0:
                record["first_fifo_write"] = {
                    "completed_monotonic_ns": completed_ns,
                    "written_pcm_bytes": written_bytes,
                    "sequence": packet.sequence,
                    "frame_index": packet.frame_index,
                    "output_presentation_monotonic_ns": mapped.monotonic_ns + relay_delay_ns,
                    "presentation_lead_ns": mapped.monotonic_ns + relay_delay_ns - completed_ns,
                    "requested_to_write_ns": completed_ns - record["requested_monotonic_ns"],
                }
                record["status"] = "fifo_written"
            else:
                record["fifo_dropped_bytes_before_first_write"] += dropped_bytes

    def retire(self, token):
        if (self._current is not None and not self._current["closed"]
                and self._owner is not None and self._owner[0] == token):
            self._current["closed"] = True
            self._current["status"] = "ended"

    def snapshot(self):
        return {
            "clock": "worker_monotonic",
            "scope": "Native BEGIN/FLUSH to backend acknowledgement and timed FIFO; phone Play time and physical output are not observed",
            "retained_limit": self._records.maxlen,
            "records": deepcopy(list(self._records)),
        }
