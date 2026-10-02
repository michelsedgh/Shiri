"""Linux final-output lifecycle tests with real pipes and SEQPACKET sockets.

Only peer credentials are injected to exercise worker ownership without creating
system accounts. Actual SO_PEERCRED admission is tested in test_bluealsa.py.
These tests do not open a system bus, ALSA device, or physical Bluetooth route.
"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

from shiri.runtime import bluetooth_output as output
from shiri.runtime.bluealsa import PCMEndpoint
from shiri.runtime.pcm_transport import Frame, Operation


pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Actual worker lifecycle requires Linux SEQPACKET")


@asynccontextmanager
async def session(monkeypatch, *, publish=False):
    endpoint = PCMEndpoint(":1.22", "/org/bluealsa/hci0/dev_AA_BB_CC_DD_EE_FF/a2dpsrc/sink",
                           "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF", "AA:BB:CC:DD:EE:FF", 12,
                           0x8210, "S16LE", 2, 2, 48000, "SBC", True, True)
    read, write = os.pipe()
    client, controller = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    controller.setblocking(False)
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    peer.setblocking(False)
    room_id, generation, output_uid = str(uuid4()), uuid4().hex, os.geteuid() + 1001
    monkeypatch.setattr(output, "peer_identity", lambda _peer: (os.getpid(), output_uid))
    worker, serving = None, None
    with TemporaryDirectory(prefix="shiri-life-", dir="/tmp") as directory:
        os.chmod(directory, 0o700)
        original = Path(directory) / "final.sock"
        published = Path(directory) / "published.sock"
        try:
            worker = output.BluetoothOutput(endpoint, [write, client.fileno()], room_id=room_id,
                                             generation=generation, output_uid=output_uid, path=original)
            await worker.dispatch("authorize-peer", {"pid": os.getpid()})
            if publish:
                # The broker moves the held socket into its root-only directory.
                # This fixture tests rename semantics, not directory credentials.
                original.rename(published)
            serving = asyncio.create_task(worker.serve())
            loop = asyncio.get_running_loop()
            await loop.sock_connect(peer, str(published if publish else original))
            initial = Frame(Operation.START, room_id, generation, str(uuid4()), 1, 0, 0, 48000, 0x8210, 2)
            await loop.sock_sendall(peer, initial.encode())
            ready = await asyncio.wait_for(loop.sock_recv(peer, 20000), 0.5)
            assert Frame.decode(ready).encode() == initial.acknowledgment().encode()
            assert worker.active
            yield worker, serving, peer, controller, initial, (read, write, client), published
        finally:
            peer.close()
            if serving:
                serving.cancel()
                await asyncio.gather(serving, return_exceptions=True)
            if worker:
                await worker.close()
            client.close()
            controller.close()
            os.close(read)
            os.close(write)


async def blocked_data(monkeypatch, worker, peer, initial):
    """Put a real DATA packet into the worker with its selected pipe blocked."""
    entered = asyncio.Event()
    original_write = os.write
    def blocked(fd, payload):
        if fd == worker.relay.pcm_fd:
            entered.set()
            raise BlockingIOError
        return original_write(fd, payload)
    monkeypatch.setattr(output.os, "write", blocked)
    audio = replace(initial, operation=Operation.DATA, sequence=1, frames=480,
                    pts_ns=10_000_000_000, payload=b"\x12\x34\x56\x78" * 480)
    await asyncio.get_running_loop().sock_sendall(peer, audio.encode())
    await asyncio.wait_for(entered.wait(), 0.1)
    assert worker.relay.queued_bytes == len(audio.payload) and worker.relay.written_bytes == 0
    return worker.relay.pump


def assert_closed(fd):
    with pytest.raises(OSError):
        os.fstat(fd)


async def test_active_session_cancellation_closes_peer_and_cleanup_stops_pending_pcm(monkeypatch):
    async with session(monkeypatch) as (worker, serving, peer, controller, initial, originals, _path):
        pump = await blocked_data(monkeypatch, worker, peer, initial)
        pcm_fd, control_fd = worker.relay.pcm_fd, worker.relay.controller.fileno()
        serving.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(serving, 0.1)
        assert not worker.active
        assert await asyncio.wait_for(asyncio.get_running_loop().sock_recv(peer, 20000), 0.1) == b""
        # run() always closes the worker after joining its canceled serve task.
        await asyncio.wait_for(worker.close(), 0.1)
        assert pump.done() and pump.cancelled() and not worker.relay.queue
        assert worker.relay.queued_bytes == 0 and worker.relay.written_bytes == 0
        assert_closed(pcm_fd)
        assert_closed(control_fd)
        os.fstat(originals[1])
        os.fstat(originals[2].fileno())
        with pytest.raises(BlockingIOError):
            controller.recv(32)


async def test_unexpected_peer_eof_fails_closed_and_run_cleanup_cancels_queued_pcm(monkeypatch):
    async with session(monkeypatch) as (worker, serving, peer, controller, initial, originals, _path):
        pump = await blocked_data(monkeypatch, worker, peer, initial)
        pcm_fd, control_fd = worker.relay.pcm_fd, worker.relay.controller.fileno()
        peer.shutdown(socket.SHUT_WR)
        await asyncio.wait_for(serving, 0.1)
        assert worker.error and "closed" in worker.error and worker.relay.failed.is_set()
        assert not worker.active and not (await worker.dispatch("health", {}))["ready"]
        assert await asyncio.wait_for(asyncio.get_running_loop().sock_recv(peer, 20000), 0.1) == b""
        await asyncio.wait_for(worker.close(), 0.1)
        assert pump.done() and pump.cancelled() and worker.relay.closed
        assert worker.relay.queued_bytes == 0 and worker.relay.written_bytes == 0
        assert_closed(pcm_fd)
        assert_closed(control_fd)
        os.fstat(originals[1])
        os.fstat(originals[2].fileno())
        assert worker.relay.flushes == 0
        with pytest.raises(BlockingIOError):
            controller.recv(32)


async def test_cancellation_waiting_for_device_drop_never_acknowledges_a_late_reply(monkeypatch):
    async with session(monkeypatch) as (worker, serving, peer, controller, initial, _originals, _path):
        pump = await blocked_data(monkeypatch, worker, peer, initial)
        flush = replace(initial, operation=Operation.FLUSH, generation=2, sequence=2)
        loop = asyncio.get_running_loop()
        await loop.sock_sendall(peer, flush.encode())
        assert await asyncio.wait_for(loop.sock_recv(controller, 32), 0.1) == b"DropSync"
        assert pump.done() and pump.cancelled() and worker.relay.pump is None
        assert not worker.relay.queue and worker.relay.queued_bytes == 0
        serving.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(serving, 0.1)
        await loop.sock_sendall(controller, b"OK")
        assert await asyncio.wait_for(loop.sock_recv(peer, 20000), 0.1) == b""
        health = await worker.dispatch("health", {})
        assert not health["ready"] and health["error"] and worker.relay.failed.is_set()
        assert worker.relay.flushes == 0 and worker.relay.written_bytes == 0
        await asyncio.wait_for(worker.close(), 0.1)


async def test_published_socket_keeps_held_listener_and_cleanup_preserves_broker_path(monkeypatch):
    async with session(monkeypatch, publish=True) as (worker, serving, peer, _controller, _initial, _originals, path):
        assert not worker.path.exists() and path.lstat().st_ino == worker.inode
        serving.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(serving, 0.1)
        assert await asyncio.wait_for(asyncio.get_running_loop().sock_recv(peer, 20000), 0.1) == b""
        await worker.close()
        # Only the broker can remove the published held inode after its exact
        # owning unit/cgroup has stopped; the worker never unlinks a new path.
        assert path.lstat().st_ino == worker.inode
