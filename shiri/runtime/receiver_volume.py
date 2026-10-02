"""Bounded receiver feedback on a separate authenticated control socket.

The room master is authoritative at OwnTone. A feedback failure cannot revoke
music or occupy the PCM reader. AP2 SET_PARAMETER has no origin identifier;
matching recent command echoes are suppressed for two seconds for the exact
admitted source/generation. This cannot distinguish a same-value human edit
inside that interval, including a human returning to an older sent value after
a newer web edit. Such an ambiguous move may be ignored until the window ends.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import os
from pathlib import Path
import socket
import stat
import struct
import time
from uuid import UUID

from shiri.deadline import bounded
from shiri.rpc import RpcError

WIRE = struct.Struct("!4sBBH16s16sQQQII8x")
SIZE = WIRE.size
ZERO = bytes(16)
BIND, BOUND, SET, REPLY = 1, 2, 3, 4
# Status values are protocol constants, not platform errno numbers.
OK, STALE, UNAVAILABLE, TIMEOUT, PROTOCOL, IO, UNSUPPORTED = range(7)


@dataclass(frozen=True)
class Message:
    kind: int
    incarnation: bytes = ZERO
    session: bytes = ZERO
    epoch: int = 0
    generation: int = 0
    revision: int = 1
    volume: int = 50
    notify: bool = False
    status: int = OK

    def encode(self):
        if (self.kind not in {BIND, BOUND, SET, REPLY} or type(self.notify) is not bool
                or len(self.incarnation) != 16 or len(self.session) != 16
                or type(self.volume) is not int or not 0 <= self.volume <= 100
                or type(self.revision) is not int or not 1 <= self.revision <= 2**63-1
                or type(self.epoch) is not int or type(self.generation) is not int
                or not 0 <= self.epoch <= 2**63-1 or not 0 <= self.generation <= 2**63-1
                or self.status not in range(7)
                or (self.session == ZERO and (self.epoch or self.generation))
                or (self.session != ZERO and (not self.epoch or not self.generation))):
            raise ValueError("Invalid receiver volume envelope")
        return WIRE.pack(b"SHV1", self.kind, int(self.notify), 0, self.incarnation,
                         self.session, self.epoch, self.generation, self.revision, self.volume, self.status)

    @classmethod
    def decode(cls, data):
        if len(data) != SIZE:
            raise ValueError("Invalid receiver volume envelope size")
        magic, kind, flags, reserved, incarnation, session, epoch, generation, revision, volume, status = WIRE.unpack(data)
        if magic != b"SHV1" or flags > 1 or reserved or any(data[72:]):
            raise ValueError("Invalid receiver volume envelope header")
        result = cls(kind, incarnation, session, epoch, generation, revision, volume, bool(flags), status)
        result.encode()
        return result


@dataclass(frozen=True)
class PendingNotification:
    volume: int
    target: tuple[str, str, int]


class ReceiverVolume:
    def __init__(self, controller, volume=50, *, deadline=1.0, echo_seconds=2.0):
        if type(volume) is not int or not 0 <= volume <= 100:
            raise ValueError("Receiver master must be an integer from zero to one hundred")
        self.controller = controller
        self.volume = volume
        self.revision = controller.control_revision
        self.notify = False
        self.target = None
        self.pending_notification = None
        self.applied_intent = True
        self.deadline = deadline
        self.echo_seconds = echo_seconds
        self.changed = asyncio.Event()
        self.listener = self.server = self.connection = self.task = None
        self.path = None
        self.closed = False
        self.serial = 0
        self.echoes = []
        self.result = {"status": "waiting_for_receiver", "revision": self.revision}

    def intent(self, revision):
        if revision > self.revision:
            self.revision = revision
            # A gain command for the preceding intent must never be dispatched
            # under the new revision while its replacement is being applied.
            self.serial += 1
            self.notify = False
            self.target = None
            self.applied_intent = False
            self.changed.set()

    def queue(self, revision, volume, notify):
        if (type(revision) is not int or revision != self.controller.control_revision
                or type(volume) is not int or not 0 <= volume <= 100 or type(notify) is not bool):
            raise RpcError("invalid_request", "Receiver master needs the current revision, integer volume and boolean notify")
        self.revision, self.volume = revision, volume
        source = self.controller.actor.snapshot()
        owner = source.get("owner")
        target = ((source["incarnation"], owner["session_id"], owner["epoch"])
                  if owner else None)
        if notify:
            self.pending_notification = (PendingNotification(volume, target)
                                         if target and source.get("ready") else None)
        elif (self.pending_notification is not None
              and (self.pending_notification.volume != volume or self.pending_notification.target != target)):
            # A different phone/web master cancels the old obligation. A
            # same-master administrative revision preserves the original
            # target and can never redirect feedback to a successor source.
            self.pending_notification = None
        self.notify = self.pending_notification is not None
        self.target = self.pending_notification.target if self.notify else None
        self.applied_intent = True
        self.serial += 1
        self.result = {"status": "queued", "revision": revision, "volume": volume}
        self.changed.set()
        return {"queued": True, "revision": revision, "volume": volume}

    def packet(self, kind=SET):
        source = self.controller.actor.snapshot()
        owner = source.get("owner")
        incarnation = UUID(source["incarnation"]).bytes
        if owner and source.get("ready"):
            handle = self.controller.handles.get(owner["session_id"])
            if handle is not None and not handle.closed and handle.token.model_dump() == owner:
                return Message(kind, incarnation, UUID(owner["session_id"]).bytes, owner["epoch"],
                               handle.generation, self.revision, self.volume,
                               bool(self.notify and self.target ==
                                    (source["incarnation"], owner["session_id"], owner["epoch"])))
        # During a transition there is no safe feedback target. The idle
        # command will be refused if the receiver has a pending native BEGIN.
        return Message(kind, incarnation, revision=self.revision, volume=self.volume)

    def is_echo(self, token, generation, volume):
        now = time.monotonic()
        self.echoes = [entry for entry in self.echoes if entry[0] > now]
        key = (UUID(token.incarnation).bytes, UUID(token.session_id).bytes, token.epoch, generation, volume)
        return any(entry[1] == key for entry in self.echoes)

    def _remember(self, packet):
        if not packet.notify or packet.session == ZERO:
            return
        now = time.monotonic()
        key = (packet.incarnation, packet.session, packet.epoch, packet.generation, packet.volume)
        self.echoes = [entry for entry in self.echoes if entry[0] > now][-31:]
        self.echoes.append((now+self.echo_seconds, key))

    async def _exchange(self, connection, packet):
        loop = asyncio.get_running_loop()
        async def exchange():
            self._remember(packet)  # The sender may echo before returning its event response.
            await loop.sock_sendall(connection, packet.encode())
            data = await loop.sock_recv(connection, SIZE+1)
            return Message.decode(data)
        answer = await bounded(exchange(), self.deadline)
        if answer.kind != REPLY or replace(answer, kind=packet.kind, status=OK) != packet:
            raise ValueError("Receiver reply does not acknowledge this exact volume command")
        return answer

    async def run(self, connection):
        loop = asyncio.get_running_loop()
        try:
            async def bind_receiver():
                bind = Message.decode(await loop.sock_recv(connection, SIZE+1))
                if bind != Message(BIND):
                    raise ValueError("Receiver must bind a fresh control connection")
                await loop.sock_sendall(connection, replace(self.packet(BOUND), notify=False).encode())
            await bounded(bind_receiver(), self.deadline)
            observed, attempts = -1, 0
            while not self.closed and connection is self.connection:
                self.changed.clear()
                packet = self.packet()
                key = (self.serial, packet.session, packet.epoch, packet.generation)
                if key != observed:
                    attempts = 0
                if self.applied_intent and (key != observed or (attempts and attempts < 3)):
                    serial = self.serial
                    notification = self.pending_notification if packet.notify else None
                    answer = await self._exchange(connection, packet)
                    current = self.packet()
                    exact_current = serial == self.serial and packet == current
                    if (answer.status == OK and notification is not None
                            and self.pending_notification is notification
                            and notification.volume == packet.volume
                            and notification.target == (str(UUID(bytes=packet.incarnation)),
                                                        str(UUID(bytes=packet.session)), packet.epoch)
                            and (packet.incarnation, packet.session, packet.epoch, packet.generation)
                            == (current.incarnation, current.session, current.epoch, current.generation)):
                        # A successful old-revision ACK can satisfy the same
                        # retained master notification after an administrative
                        # edit. It cannot clear a replacement command, source
                        # or FLUSH generation, or relabel the latest result.
                        self.pending_notification = None
                        self.notify, self.target = False, None
                    if exact_current:
                        self.result = {"status": ("delivered" if answer.status == OK else
                                                  {STALE:"stale_source", UNAVAILABLE:"event_channel_unavailable",
                                                   TIMEOUT:"event_timeout", PROTOCOL:"event_protocol_error",
                                                   IO:"event_io_error", UNSUPPORTED:"unsupported_input"}[answer.status]),
                                       "revision": packet.revision, "volume": packet.volume,
                                       "sender_notified": bool(packet.notify and packet.session != ZERO and answer.status == OK)}
                    observed = key
                    attempts = attempts+1 if answer.status in {STALE, UNAVAILABLE} else 0
                # Watch source/generation changes without any audio callback
                # performing socket I/O; retry a not-yet-ready event channel
                # at most three times per edit/source, never in a busy loop.
                try:
                    await asyncio.wait_for(self.changed.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
                # EOF and unsolicited control bytes are observed even when
                # there is no pending volume command. This descriptor has no
                # audio reader and never accepts asynchronous PCM/GRANT data.
                try:
                    connection.recv(1, socket.MSG_PEEK)
                except BlockingIOError:
                    pass
                else:
                    raise ValueError("Receiver control closed or sent unsolicited data")
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError, asyncio.TimeoutError):
            if connection is self.connection:
                self.result = {"status": "control_disconnected", "revision": self.revision}
        finally:
            connection.close()
            if connection is self.connection:
                self.connection = None

    async def listen(self, path: Path, native_uid: int, mode=0o660):
        if not hasattr(socket, "SO_PEERCRED"):
            raise RuntimeError("Receiver feedback requires Linux peer credentials")
        try:
            info = path.lstat()
        except FileNotFoundError:
            info = None
        if info:
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise RuntimeError("Refusing to replace another receiver feedback endpoint")
            path.unlink()
        self.path = path
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.listener.setblocking(False)
        self.listener.bind(str(path))
        path.chmod(mode)
        self.listener.listen(1)
        async def accept():
            loop = asyncio.get_running_loop()
            while not self.closed:
                connection, _ = await loop.sock_accept(self.listener)
                transferred = False
                try:
                    raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
                    if struct.unpack("3i", raw)[1] != native_uid:
                        continue
                    connection.setblocking(False)
                    # Join the previous reader before descriptor reuse.
                    if self.task:
                        self.task.cancel()
                        await asyncio.gather(self.task, return_exceptions=True)
                    self.connection = connection
                    self.task = asyncio.create_task(self.run(connection))
                    # Cancellation before the coroutine's first instruction
                    # still retires the exact accepted descriptor.
                    self.task.add_done_callback(lambda _done, accepted=connection: accepted.close())
                    transferred = True
                finally:
                    if not transferred:
                        connection.close()
        self.server = asyncio.create_task(accept())

    async def close(self):
        self.closed = True
        for task in (self.server, self.task):
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if self.connection:
            self.connection.close()
            self.connection = None
        if self.listener:
            self.listener.close()
            self.path.unlink(missing_ok=True)
