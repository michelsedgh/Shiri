"""Bounded terminal observer for the explicit disconnected AirPlay LAN gate.

This fixture observes decoded PCM from a real Shairport receiver. It never
writes audio, supplies a presentation anchor, or admits production music.
Only the configured receiver UID can obtain a fresh bounded capture token.
Importing this module creates no socket or other resources.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import replace
import os
from pathlib import Path
import socket
import stat
import struct
import time
from uuid import uuid4

from shiri.runtime.receiver_volume import BIND, BOUND, Message, SIZE

from shiri.runtime.timing import (
    FLAG_AIRPLAY2, FLAG_SPEECH_ONLY, HEADER_BYTES, Kind, MAX_FRAMES, Packet,
    RATE, StreamFence, TimingError, ZERO_UUID, map_native_time,
)


class CaptureRegistry:
    """One current capture session; retired sessions can never be readmitted."""

    def __init__(self, *, maximum_sessions=32, ring_seconds=4):
        self.incarnation = uuid4().bytes
        self.maximum_sessions = maximum_sessions
        self.epoch = 0
        self.seen = set()
        self.current = None
        self.fence = None
        self.ring = deque()
        self.ring_bytes = 0
        self.maximum_bytes = RATE * 4 * ring_seconds
        self.frames = self.blocks = self.ends = self.flushes = 0
        self.volumes = deque(maxlen=128)
        self.events = deque(maxlen=512)
        self.mapping_uncertainty_max_ns = 0
        self.minimum_presentation_lead_ns = None
        self.late_blocks = self.late_frames = 0
        self.late_packets = deque(maxlen=64)
        self.last_pcm_received = None

    def begin(self, packet, connection_id):
        if (packet.kind is not Kind.BEGIN or packet.session == ZERO_UUID
                or packet.incarnation != ZERO_UUID or packet.epoch != 0
                or packet.generation != 1 or packet.flags & FLAG_SPEECH_ONLY
                or not packet.flags & FLAG_AIRPLAY2 or packet.session in self.seen
                or len(self.seen) >= self.maximum_sessions or self.current is not None):
            raise TimingError("Capture BEGIN is not a fresh bounded AirPlay2 session")
        self.epoch += 1
        self.seen.add(packet.session)
        self.current = (connection_id, packet.session, self.epoch)
        self.fence = StreamFence(packet.session)
        self.last_pcm_received = None
        self.events.append({"kind": "begin", "epoch": self.epoch, "generation": 1,
                            "at_ns": time.monotonic_ns()})
        return self.grant(packet)

    def grant(self, packet):
        return replace(packet, kind=Kind.GRANT, frames=0, pcm=b"",
                       incarnation=self.incarnation, epoch=self.current[2])

    def message(self, packet, connection_id, *, now_ns=None):
        now_ns = time.monotonic_ns() if now_ns is None else now_ns
        if (self.current is None or self.current != (connection_id, packet.session, packet.epoch)
                or packet.incarnation != self.incarnation):
            raise TimingError("Capture event does not carry its exact admitted token")
        if packet.kind is Kind.FLUSH:
            self.fence.flush(packet)
            self.flushes += 1
            self.events.append({"kind": "flush", "epoch": packet.epoch,
                                "generation": packet.generation, "at_ns": now_ns})
            return self.grant(packet)
        if packet.generation != self.fence.generation:
            raise TimingError("Capture event belongs to a retired flush generation")
        if packet.kind is Kind.END:
            self.fence.end(packet)
            self.ends += 1
            self.events.append({"kind": "end", "epoch": packet.epoch,
                                "generation": packet.generation, "at_ns": now_ns})
            self.current = self.fence = None
        elif packet.kind is Kind.VOLUME:
            self.volumes.append({"percent": packet.frames, "epoch": packet.epoch,
                                 "generation": packet.generation, "at_ns": now_ns})
        elif packet.kind is Kind.PCM:
            self.fence.accept(packet)
            mapped = map_native_time(packet, now_ns=now_ns)
            # This is an observation, not a replacement audio timestamp. A
            # late decoded terminal block is useful evidence and fails the
            # explicit deadline assertion in the final receipt.
            self.mapping_uncertainty_max_ns = max(self.mapping_uncertainty_max_ns,
                                                  mapped.uncertainty_ns)
            self.blocks += 1
            self.frames += packet.frames
            lead = mapped.monotonic_ns - now_ns
            self.minimum_presentation_lead_ns = (
                lead if self.minimum_presentation_lead_ns is None
                else min(self.minimum_presentation_lead_ns, lead))
            previous = self.last_pcm_received
            receive_gap = (now_ns - previous[2] if previous is not None
                           and previous[:2] == (packet.epoch, packet.generation) else None)
            self.last_pcm_received = (packet.epoch, packet.generation, now_ns)
            if lead <= 0:
                self.late_blocks += 1
                self.late_frames += packet.frames
                self.late_packets.append({
                    "epoch": packet.epoch, "generation": packet.generation,
                    "sequence": packet.sequence, "frame_index": packet.frame_index,
                    "frames": packet.frames, "flags": packet.flags,
                    "presentation_ns": mapped.monotonic_ns, "received_ns": now_ns,
                    "lead_ns": lead, "mapping_uncertainty_ns": mapped.uncertainty_ns,
                    "clock_sample_age_ns": now_ns - packet.monotonic_after_ns,
                    "receive_gap_ns": receive_gap,
                    "pcm_peak": max(abs(sample[0]) for sample in struct.iter_unpack("<h", packet.pcm)),
                })
            self.ring.append((now_ns, packet.epoch, packet.generation, mapped.monotonic_ns,
                              packet.flags, packet.pcm))
            self.ring_bytes += len(packet.pcm)
            while self.ring_bytes > self.maximum_bytes:
                self.ring_bytes -= len(self.ring.popleft()[-1])
        else:
            raise TimingError("Capture received an unexpected receiver message")
        return None

    def disconnected(self, connection_id):
        if self.current is not None and self.current[0] == connection_id:
            self.events.append({"kind": "eof", "epoch": self.current[2],
                                "at_ns": time.monotonic_ns()})
            self.current = self.fence = None

    def snapshot(self):
        return {"sessions": len(self.seen), "current_epoch": self.current[2] if self.current else None,
                "blocks": self.blocks, "frames": self.frames, "flushes": self.flushes,
                "ends": self.ends, "buffered_bytes": self.ring_bytes,
                "mapping_uncertainty_max_ns": self.mapping_uncertainty_max_ns,
                "minimum_presentation_lead_ns": self.minimum_presentation_lead_ns,
                "late_blocks": self.late_blocks, "late_frames": self.late_frames,
                "late_packets": list(self.late_packets),
                "events": list(self.events), "volumes": list(self.volumes)}

    def deadline_observation(self, *, since_ns):
        """Bounded metadata for the exact spectrum window; never changes PCM."""
        blocks = frames = late_blocks = late_frames = 0
        leads = []
        for received, _epoch, _generation, presentation, _flags, data in self.ring:
            if received < since_ns:
                continue
            blocks += 1
            count = len(data) // 4
            frames += count
            lead = presentation - received
            leads.append(lead)
            if lead <= 0:
                late_blocks += 1
                late_frames += count
        return {"since_ns": since_ns, "blocks": blocks, "frames": frames,
                "late_blocks": late_blocks, "late_frames": late_frames,
                "minimum_presentation_lead_ns": min(leads) if leads else None,
                "maximum_presentation_lead_ns": max(leads) if leads else None,
                "late_packets": [dict(sample) for sample in self.late_packets
                                 if sample["received_ns"] >= since_ns],
                "sample_limit": self.late_packets.maxlen}

    def spectrum(self, *, since_ns, minimum_frames=RATE // 4):
        """Fit both independent tones on actual decoded stereo blocks."""
        import numpy as np
        amplitudes, rms, peaks, leads, frames, channel_error, gaps = [], [], [], [], 0, 0, 0
        for received, _epoch, _generation, presentation, flags, data in self.ring:
            if received < since_ns:
                continue
            pcm = np.frombuffer(data, dtype="<i2").reshape(-1, 2).astype(np.float64)
            frames += len(pcm)
            channel_error = max(channel_error, float(np.max(np.abs(pcm[:, 0] - pcm[:, 1]))))
            peaks.append(float(np.max(np.abs(pcm))))
            leads.append(presentation - received)
            gaps += bool(flags & 1)
            if len(pcm) < 128:
                continue
            positions = np.arange(len(pcm)) / RATE
            basis = np.column_stack((np.sin(2*np.pi*440*positions), np.cos(2*np.pi*440*positions),
                                     np.sin(2*np.pi*880*positions), np.cos(2*np.pi*880*positions),
                                     np.ones(len(pcm))))
            fitted, *_ = np.linalg.lstsq(basis, pcm[:, 0], rcond=None)
            amplitudes.append((float(np.hypot(*fitted[:2])), float(np.hypot(*fitted[2:4]))))
            rms.append(float(np.sqrt(np.mean(pcm[:, 0] ** 2))))
        if frames < minimum_frames or not amplitudes:
            return None
        return {"frames": frames, "music_440_amplitude": float(np.median([v[0] for v in amplitudes])),
                "speech_880_amplitude": float(np.median([v[1] for v in amplitudes])),
                "rms": float(np.median(rms)), "peak": max(peaks),
                "channel_error": channel_error, "gap_blocks": gaps,
                "minimum_presentation_lead_ns": min(leads),
                "maximum_presentation_lead_ns": max(leads)}


class NativeObserver:
    """Authenticated root terminal sink; total session and task counts bounded."""

    def __init__(self, path: Path, receiver_uid: int, receiver_gid: int):
        if (type(receiver_uid) is not int or not 0 < receiver_uid < 2**32
                or type(receiver_gid) is not int or not 0 < receiver_gid < 2**32):
            raise ValueError("Terminal observer requires the managed nonroot receiver credentials")
        self.path, self.receiver_uid, self.receiver_gid = path, receiver_uid, receiver_gid
        self.registry = CaptureRegistry()
        self.listener = self.task = self.inode = None
        self.listeners = {}
        self.inodes = {}
        self.connection_sockets = {}
        self.control_binds = 0
        self.tasks = set()
        self.errors = []
        self.connections = 0
        self.hold_next_begin_seconds = 0
        self.held_begins = []
        self.closed = False

    async def start(self):
        volume_path = self.path.with_name("volume.sock")
        if (os.geteuid() != 0 or self.path.exists() or volume_path.exists()
                or max(len(str(self.path)), len(str(volume_path))) >= 108):
            raise TimingError("Terminal capture requires a fresh root-owned short Unix socket path")
        for path in (self.path, volume_path):
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC)
            listener.setblocking(False)
            try:
                listener.bind(str(path))
                self.listeners[path] = listener
                info = path.lstat()
                self.inodes[path] = info.st_dev, info.st_ino
                os.chown(path, 0, self.receiver_gid)
                path.chmod(0o660)
                listener.listen(2)
            except BaseException:
                listener.close()
                raise
        self.listener = self.listeners[self.path]
        self.inode = self.inodes[self.path]
        self.task = asyncio.create_task(self._serve())

    async def _serve(self):
        acceptors = [asyncio.create_task(self._accept(self.listeners[self.path], "pcm")),
                     asyncio.create_task(self._accept(self.listeners[self.path.with_name("volume.sock")], "control"))]
        try:
            await asyncio.gather(*acceptors)
        finally:
            # gather propagates one child's failure without cancelling its
            # sibling. Own and join both readers before a listener can close.
            for task in acceptors:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*acceptors, return_exceptions=True)

    async def _accept(self, listener, lane):
        loop = asyncio.get_running_loop()
        while not self.closed:
            connection, _ = await loop.sock_accept(listener)
            try:
                credential = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                _pid, uid, _gid = struct.unpack("3i", credential)
            except BaseException:
                connection.close()
                raise
            if uid != self.receiver_uid or len(self.tasks) >= 4 or self.connections >= 32:
                connection.close()
                self.errors.append("unauthorized_or_unbounded_terminal_connection")
                continue
            self.connections += 1
            connection.setblocking(False)
            self._start_connection(connection, self.connections, lane)

    def _start_connection(self, connection, identity, lane="pcm"):
        # Own the descriptor before scheduling. Cancellation before the
        # coroutine's first instruction cannot run its finally block.
        self.connection_sockets[identity] = connection
        coroutine = self._connection(connection, identity) if lane == "pcm" else self._control_connection(connection)
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)

        def retired(completed):
            self.tasks.discard(completed)
            if self.connection_sockets.get(identity) is connection:
                self.connection_sockets.pop(identity)
                self.registry.disconnected(identity)
                connection.close()
            if not completed.cancelled() and completed.exception() is not None:
                self.errors.append("terminal_task_"+type(completed.exception()).__name__)
        task.add_done_callback(retired)
        return task

    async def _control_connection(self, connection):
        """Final volume1 receiver requires a separate exact default bind lane.

        It supplies only the terminal fixture's100% default. It never sends a
        reverse event or claims the source applied a volume edit.
        """
        loop = asyncio.get_running_loop()
        try:
            raw = await asyncio.wait_for(loop.sock_recv(connection, SIZE+1), 1)
            if Message.decode(raw) != Message(BIND):
                raise ValueError("Terminal control bind is not the exact fresh request")
            answer = Message(BOUND, incarnation=self.registry.incarnation, volume=100)
            await loop.sock_sendall(connection, answer.encode())
            self.control_binds += 1
            # The receiver only sends REPLY to SET. This fixture sends no SET,
            # so EOF is the only permissible next observation.
            if await loop.sock_recv(connection, SIZE+1):
                raise ValueError("Terminal control sent an unsolicited reply")
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError, asyncio.TimeoutError) as exc:
            self.errors.append("terminal_control_"+type(exc).__name__)
        finally:
            connection.close()

    async def _connection(self, connection, identity):
        loop, admitted = asyncio.get_running_loop(), False
        try:
            while not self.closed:
                receive = loop.sock_recv(connection, HEADER_BYTES + MAX_FRAMES*4 + 1)
                message = await receive if admitted else await asyncio.wait_for(receive, 7)
                if not message:
                    return
                packet = Packet.decode(message)
                if not admitted:
                    answer = self.registry.begin(packet, identity)
                    hold = self.hold_next_begin_seconds
                    self.hold_next_begin_seconds = 0
                    if hold:
                        if not 3 < hold < 6:
                            raise TimingError("Terminal reply hold must exceed the 3s source budget and remain below 6s")
                        receipt = {"connection": identity, "hold_seconds": hold,
                                   "started_ns": time.monotonic_ns(), "delivered": False}
                        self.held_begins.append(receipt)
                        await asyncio.sleep(hold)
                    await loop.sock_sendall(connection, answer.encode())
                    if hold:
                        receipt.update(delivered=True, finished_ns=time.monotonic_ns())
                    admitted = True
                else:
                    answer = self.registry.message(packet, identity)
                    if answer is not None:
                        await loop.sock_sendall(connection, answer.encode())
                    if packet.kind is Kind.END:
                        return
        except asyncio.CancelledError:
            raise
        except (OSError, asyncio.TimeoutError, TimingError) as exc:
            # The deliberate held-reply fixture may be closed by the actual
            # timed-out transport. Record that expected evidence separately.
            if not admitted and self.held_begins and self.held_begins[-1]["connection"] == identity:
                self.held_begins[-1].update(closed_before_grant=True, finished_ns=time.monotonic_ns())
            else:
                self.errors.append(type(exc).__name__ + ": " + str(exc))
        finally:
            self.registry.disconnected(identity)
            connection.close()

    async def close(self):
        self.closed = True
        for task in [self.task, *self.tasks]:
            if task is not None:
                task.cancel()
        await asyncio.gather(*(t for t in [self.task, *self.tasks] if t is not None), return_exceptions=True)
        for identity, connection in list(self.connection_sockets.items()):
            self.registry.disconnected(identity)
            connection.close()
            self.connection_sockets.pop(identity)
        for path, listener in self.listeners.items():
            listener.close()
            info = path.lstat()
            if (not stat.S_ISSOCK(info.st_mode) or (info.st_dev, info.st_ino) != self.inodes[path]
                    or info.st_uid != 0 or info.st_gid != self.receiver_gid or stat.S_IMODE(info.st_mode) != 0o660):
                raise TimingError("Terminal capture socket changed; refusing retirement")
            path.unlink()
