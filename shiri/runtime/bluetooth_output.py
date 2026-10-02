"""Descriptor-only Bluetooth output worker; no system bus or ALSA devices.

OwnTone owns presentation pacing and final volume. This worker preserves its
complete PCM frames, bounds unplayed audio, and acknowledges each flush only
after the admitted BlueALSA controller completes DropSync. Stock asynchronous
Drop is never a fallback. Physical pairing and
Bluetooth latency/drift still require operator acceptance.
"""
from __future__ import annotations

import argparse
import array
import asyncio
from collections import deque
from contextlib import suppress
import fcntl
import os
from pathlib import Path
import signal
import socket
import stat
import termios
import time

from shiri.rpc import RpcError, serve_rpc

from .bluealsa import _bounded, _close_fds, _descriptor_ready, _socket_ready, peer_identity, receive_handoff, validate_pcm_fds
from .pcm_transport import CONTROL_TIMEOUT, Frame, MAX_PACKET, MAX_QUEUE_MS, Operation, StreamState
from .system import RuntimeFailure


class PCMRelay:
    """Bounded complete-frame forwarding to one already admitted PCM pipe."""
    def __init__(self, endpoint, descriptors):
        if type(endpoint.synchronous_drop) is not bool or not endpoint.synchronous_drop:
            raise RuntimeFailure("Bluetooth PCM requires a completed synchronous Drop capability")
        if type(endpoint.restricted_controller) is not bool or not endpoint.restricted_controller:
            raise RuntimeFailure("Bluetooth PCM requires a restricted controller capability")
        validate_pcm_fds(descriptors)
        # Keep caller ownership unchanged until construction has succeeded.
        # A socket object must not close a caller FD during a failed constructor.
        self.endpoint, self.pcm_fd = endpoint, os.dup(descriptors[0])
        try:
            self.controller = socket.socket(fileno=os.dup(descriptors[1]))
        except BaseException:
            os.close(self.pcm_fd)
            raise
        self.controller.setblocking(False)
        self.queue, self.queued_bytes, self.written_bytes = deque(), 0, 0
        self.pump, self.error, self.closed = None, None, False
        self.failed = asyncio.Event()
        self.frames_discarded, self.flushes, self.last_output_at = 0, 0, None
        self.limit_bytes = endpoint.rate * endpoint.frame_bytes * MAX_QUEUE_MS // 1000
        try:
            self.write_limit = os.fpathconf(self.pcm_fd, "PC_PIPE_BUF")
            self.write_limit -= self.write_limit % endpoint.frame_bytes
            if self.write_limit <= 0 or self.limit_bytes < endpoint.frame_bytes:
                raise RuntimeFailure("Bluetooth PCM has no usable bounded atomic write capacity")
        except BaseException:
            self.controller.close()
            os.close(self.pcm_fd)
            raise

    def pending_bytes(self):
        result = array.array("i", [0])
        fcntl.ioctl(self.pcm_fd, termios.FIONREAD, result, True)
        if result[0] < 0:
            raise RuntimeFailure("Bluetooth PCM pipe reported invalid pending audio")
        return result[0]

    def require_healthy(self):
        if self.closed or self.error:
            raise RuntimeFailure(self.error or "Bluetooth PCM relay has closed")

    def enqueue(self, frame):
        self.require_healthy()
        if frame.operation != Operation.DATA:
            raise RuntimeFailure("Bluetooth PCM relay only forwards DATA")
        pending = self.pending_bytes()
        if self.queued_bytes + pending + len(frame.payload) > self.limit_bytes:
            self.error = "Bluetooth PCM output exceeded its 200 ms unplayed-audio bound"
            self.failed.set()
            raise RuntimeFailure(self.error)
        self.queue.append([frame.payload, 0, time.monotonic() + MAX_QUEUE_MS / 1000])
        self.queued_bytes += len(frame.payload)
        if self.pump is None or self.pump.done():
            self.pump = asyncio.create_task(self._pump())

    async def _pump(self):
        try:
            while self.queue:
                payload, offset, deadline = self.queue[0]
                if time.monotonic() >= deadline:
                    raise RuntimeFailure("Bluetooth PCM speaker did not consume audio within 200 ms")
                free = self.limit_bytes - self.pending_bytes()
                free -= free % self.endpoint.frame_bytes
                count = min(len(payload) - offset, self.write_limit, free)
                if count <= 0:
                    # A writable FD can still have insufficient *our bounded*
                    # PCM budget. Waiting on it would spin while its larger
                    # kernel pipe remains writable. Poll consumption briefly.
                    await asyncio.sleep(min(0.002, max(0.0001, deadline - time.monotonic())))
                    continue
                try:
                    written = os.write(self.pcm_fd, payload[offset:offset + count])
                except BlockingIOError:
                    await _descriptor_ready(self.pcm_fd, write=True, timeout=max(0.001, deadline - time.monotonic()))
                    continue
                if written <= 0 or written > count:
                    raise RuntimeFailure("Bluetooth PCM write made no valid forward progress")
                self.written_bytes += written
                self.queued_bytes -= written
                self.last_output_at = time.monotonic()
                offset += written
                if offset == len(payload):
                    self.queue.popleft()
                else:
                    self.queue[0][1] = offset
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = str(exc) or "Bluetooth PCM forwarding failed"
            self.failed.set()

    async def discard(self):
        self.require_healthy()
        if self.pump and not self.pump.done():
            self.pump.cancel()
        if self.pump:
            await asyncio.gather(self.pump, return_exceptions=True)
            self.pump = None
        self.require_healthy()
        self.frames_discarded += self.queued_bytes // self.endpoint.frame_bytes
        self.queue.clear()
        self.queued_bytes = 0
        async def drop():
            await asyncio.get_running_loop().sock_sendall(self.controller, b"DropSync")
            response = await asyncio.get_running_loop().sock_recv(self.controller, 32)
            if response != b"OK":
                raise RuntimeFailure("Bluetooth PCM controller did not acknowledge the exact completed DropSync")
        try:
            await _bounded(drop(), CONTROL_TIMEOUT)
            self.require_healthy()
            self.flushes += 1
        except BaseException as exc:
            self.error = str(exc) or "Bluetooth PCM DropSync exceeded its 200 ms deadline"
            self.failed.set()
            raise

    async def close(self):
        if self.closed:
            return
        if self.pump and not self.pump.done():
            self.pump.cancel()
        if self.pump:
            await asyncio.gather(self.pump, return_exceptions=True)
        self.queue.clear()
        self.queued_bytes = 0
        self.closed = True
        self.controller.close()
        _close_fds([self.pcm_fd])

    def health(self):
        return {"ready": not self.closed and not self.error, "error": self.error,
                "format": self.endpoint.format, "rate": self.endpoint.rate, "channels": self.endpoint.channels,
                "frames_forwarded": self.written_bytes // self.endpoint.frame_bytes,
                "frames_discarded": self.frames_discarded, "flushes": self.flushes,
                "queue_bytes": self.queued_bytes, "last_output_at": self.last_output_at,
                "uses_system_bus": False, "uses_alsa_devices": False}


async def receive_frame(peer):
    while True:
        try:
            data, ancillary, flags, _address = peer.recvmsg(MAX_PACKET, socket.CMSG_SPACE(16 * 4),
                                                          getattr(socket, "MSG_CMSG_CLOEXEC", 0))
            descriptors = []
            for level, kind, contents in ancillary:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    values = array.array("i")
                    values.frombytes(contents[:len(contents) - len(contents) % values.itemsize])
                    descriptors.extend(values)
            try:
                if ancillary or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or not data:
                    raise RuntimeFailure("Final PCM stream was closed, truncated or carried unexpected descriptors")
                return Frame.decode(data)
            finally:
                _close_fds(descriptors)
        except BlockingIOError:
            # Output sessions may legitimately remain connected while idle.
            # No audio or pending controller operation waits on this deadline.
            try:
                await _socket_ready(peer, timeout=1.0)
            except asyncio.TimeoutError:
                continue


class BluetoothOutput:
    def __init__(self, endpoint, descriptors, *, room_id, generation, output_uid, path):
        self.room_id, self.generation, self.output_uid = room_id, generation, output_uid
        self.path = Path(path)
        if type(output_uid) is not int or output_uid <= 0 or output_uid == os.geteuid():
            raise RuntimeFailure("Bluetooth bridge requires a distinct exact OwnTone output UID")
        self.expected_pid, self.active, self.error, self.closed = None, False, None, False
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            parent = self.path.parent.lstat()
            if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or parent.st_mode & 0o077:
                raise RuntimeFailure("Final PCM socket needs the bridge's private state directory")
            self.listener.bind(str(self.path))
            os.chmod(self.path, 0o600)
            self.inode = self.path.lstat().st_ino
            self.listener.listen(1)
            self.listener.setblocking(False)
        except BaseException:
            self.listener.close()
            raise
        try:
            self.relay = PCMRelay(endpoint, descriptors)
        except BaseException:
            self.listener.close()
            with suppress(FileNotFoundError):
                self.path.unlink()
            raise

    async def serve(self):
        try:
            while not self.closed:
                peer, _address = await _bounded(asyncio.get_running_loop().sock_accept(self.listener), None,
                                               abandoned=lambda accepted: accepted[0].close())
                peer.setblocking(False)
                self.active = True
                try:
                    if self.expected_pid is None or peer_identity(peer) != (self.expected_pid, self.output_uid):
                        raise RuntimeFailure("Final PCM peer does not own the authorized OwnTone launch")
                    state = StreamState(self.relay.endpoint, self.room_id, self.generation)
                    while not state.ended:
                        frame = await receive_frame(peer)
                        acknowledgment = state.accept(frame)
                        if frame.operation == Operation.DATA:
                            self.relay.enqueue(frame)
                        elif frame.operation in {Operation.FLUSH, Operation.END}:
                            await self.relay.discard()
                            acknowledgment = state.dropped()
                        if acknowledgment:
                            packet = acknowledgment.encode()
                            await _bounded(asyncio.get_running_loop().sock_sendall(peer, packet), 0.2)
                finally:
                    self.active = False
                    peer.close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = str(exc) or "Bluetooth final-output transport failed"
            self.relay.failed.set()

    async def dispatch(self, operation, payload):
        if operation == "health" and not payload:
            result = self.relay.health()
            error = self.error or result["error"]
            return {**result, "ready": result["ready"] and not error and not self.closed,
                    "status": "error" if error else "stopped" if self.closed else "running",
                    "error": error, "peer_authorized": self.expected_pid is not None, "active": self.active}
        if operation == "authorize-peer" and set(payload) == {"pid"}:
            pid = payload["pid"]
            if type(pid) is not int or pid <= 0 or self.expected_pid not in {None, pid}:
                raise RpcError("forbidden", "Final PCM peer must belong to this exact OwnTone launch")
            self.expected_pid = pid
            return {"ok": True}
        raise RpcError("invalid_request", "Bluetooth worker accepts only health and exact peer authorization")

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.listener.close()
        await self.relay.close()
        with suppress(FileNotFoundError):
            current = self.path.lstat()
            if stat.S_ISSOCK(current.st_mode) and current.st_uid == os.geteuid() and current.st_ino == self.inode:
                self.path.unlink()


async def run(args):
    if os.geteuid() == 0:
        raise RuntimeFailure("Bluetooth output must run under its distinct unprivileged bridge account")
    # No consumer library should attempt to discover or open another endpoint.
    os.environ.pop("DBUS_SYSTEM_BUS_ADDRESS", None)
    descriptors, worker, rpc, serving = [], None, None, None
    lock = os.open(str(args.socket) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for name in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(name, stop.set)
        endpoint, descriptors = await receive_handoff(args.handoff, args.room_id, args.generation)
        worker = BluetoothOutput(endpoint, descriptors, room_id=args.room_id, generation=args.generation,
                                 output_uid=args.output_uid, path=args.input)
        _close_fds(descriptors)
        descriptors = []
        rpc = await serve_rpc(args.socket, worker.dispatch, mode=0o600, allowed_uids={0},
                              operation_uids={"authorize-peer": {0}})
        serving = asyncio.create_task(worker.serve())
        stopping, failing = asyncio.create_task(stop.wait()), asyncio.create_task(worker.relay.failed.wait())
        try:
            await asyncio.wait({stopping, failing}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopping.cancel()
            failing.cancel()
            await asyncio.gather(stopping, failing, return_exceptions=True)
        if worker.error or worker.relay.error:
            raise RuntimeFailure(worker.error or worker.relay.error)
    finally:
        if serving:
            serving.cancel()
            await asyncio.gather(serving, return_exceptions=True)
        if rpc:
            rpc.close()
            await rpc.wait_closed()
        if worker:
            await worker.close()
        _close_fds(descriptors)
        os.close(lock)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--handoff", required=True, type=Path)
    parser.add_argument("--room-id", required=True)
    parser.add_argument("--generation", required=True)
    parser.add_argument("--output-uid", required=True, type=int)
    parser.add_argument("--socket", required=True, type=Path)
    try:
        asyncio.run(run(parser.parse_args()))
    except (RuntimeFailure, OSError) as exc:
        parser.exit(1, f"Bluetooth output stopped: {exc}\n")


if __name__ == "__main__":
    main()
