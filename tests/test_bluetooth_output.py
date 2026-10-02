"""Real pipe bounds/partial writes plus selected-PCM controller failure cases.

Linux uses the actual admitted SEQPACKET descriptor checks. macOS lacks that
socket type: its controller is a connected datagram stand-in for these relay
unit tests; production credential/SCM_RIGHTS tests remain explicitly Linux-only.
"""
import asyncio
from dataclasses import replace
import fcntl
import os
import socket
import stat
import sys
from uuid import uuid4
from tempfile import TemporaryDirectory

import pytest

from shiri.runtime import bluetooth_output as output
from shiri.runtime.bluealsa import PCMEndpoint
from shiri.runtime.pcm_transport import Frame, Operation
from shiri.runtime.system import RuntimeFailure


def endpoint():
    return PCMEndpoint(":1.22", "/org/bluealsa/hci0/dev_AA_BB_CC_DD_EE_FF/a2dpsrc/sink",
                       "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF", "AA:BB:CC:DD:EE:FF", 12,
                       0x8210, "S16LE", 2, 2, 48000, "SBC", True, True)


def data(payload, first_frame=0):
    return Frame(Operation.DATA, str(uuid4()), uuid4().hex, str(uuid4()), 1, 1,
                 10_000_000_000 + first_frame * 1_000_000_000 // 48000, 48000, 0x8210, 2,
                 len(payload) // 4, first_frame, payload)


@pytest.fixture
def pipe_relay(monkeypatch):
    read, write = os.pipe()
    kind = socket.SOCK_SEQPACKET if sys.platform == "linux" else socket.SOCK_DGRAM
    client, server = socket.socketpair(socket.AF_UNIX, kind)
    if sys.platform != "linux":
        def pipe_check(descriptors):
            assert stat.S_ISFIFO(os.fstat(descriptors[0]).st_mode)
            assert fcntl.fcntl(descriptors[0], fcntl.F_GETFL) & os.O_ACCMODE == os.O_WRONLY
            os.set_blocking(descriptors[0], False)
        monkeypatch.setattr(output, "validate_pcm_fds", pipe_check)
    server.setblocking(False)
    relay = output.PCMRelay(endpoint(), [write, client.fileno()])
    try:
        yield read, write, client, server, relay
    finally:
        client.close()
        server.close()
        for fd in (read, write):
            try:
                os.close(fd)
            except OSError:
                pass
        # A failing assertion must not leave an unclosed task/FD in the tests.
        relay.controller.close()
        try:
            os.close(relay.pcm_fd)
        except OSError:
            pass


async def test_real_forwarding_is_exact_and_does_not_mutate_other_endpoint_bytes(pipe_relay):
    read, original, _client, _server, relay = pipe_relay
    payload = bytes(range(256)) * 8
    relay.enqueue(data(payload))
    await relay.pump
    assert os.read(read, len(payload)) == payload
    assert relay.health()["frames_forwarded"] == 512 and not relay.queue and not relay.error
    await relay.close()
    # The relay owns duplicates; its close cannot close caller/root lease FDs.
    os.fstat(original)


async def test_positive_partial_writes_preserve_every_unwritten_byte(pipe_relay, monkeypatch):
    read, _write, _client, _server, relay = pipe_relay
    original = os.write
    def partial(fd, payload):
        return original(fd, payload[:7])
    monkeypatch.setattr(output.os, "write", partial)
    payload = bytes(range(64)) * 4
    relay.enqueue(data(payload))
    await relay.pump
    assert os.read(read, len(payload)) == payload
    assert relay.written_bytes == len(payload) and relay.queued_bytes == 0 and not relay.error
    await relay.close()


async def test_absent_reader_fails_without_blocking_rpc_or_accumulating_a_backlog(pipe_relay):
    read, _write, _client, _server, relay = pipe_relay
    os.close(read)
    relay.enqueue(data(b"\x01\x02\x03\x04" * 960))
    await asyncio.wait_for(relay.failed.wait(), 0.3)
    assert relay.error and len(relay.queue) == 1
    with pytest.raises(RuntimeFailure):
        relay.enqueue(data(b"\x01\x02\x03\x04" * 960))
    await relay.close()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux FIONREAD on a writable pipe reports the shared pending PCM")
async def test_full_kernel_pipe_shares_the_same_200ms_unplayed_audio_bound(pipe_relay):
    _read, original, _client, _server, relay = pipe_relay
    chunk = b"\x01\x02\x03\x04" * 1024
    os.set_blocking(original, False)
    while True:
        try:
            os.write(original, chunk)
        except BlockingIOError:
            break
    assert relay.pending_bytes() > 0
    with pytest.raises(RuntimeFailure, match="200 ms"):
        relay.enqueue(data(b"\x01\x02\x03\x04" * 960))
    assert relay.failed.is_set() and not relay.queue
    await relay.close()


async def test_eagain_retains_tail_and_is_fatal_at_the_original_packet_deadline(pipe_relay, monkeypatch):
    _read, _write, _client, _server, relay = pipe_relay
    def blocked(*_args):
        raise BlockingIOError
    monkeypatch.setattr(output.os, "write", blocked)
    began = asyncio.get_running_loop().time()
    relay.enqueue(data(b"\x01\x02\x03\x04" * 960))
    await asyncio.wait_for(relay.failed.wait(), 0.5)
    assert 0.15 <= asyncio.get_running_loop().time() - began < 0.4
    assert relay.queued_bytes == 3840 and relay.written_bytes == 0
    await relay.close()


async def test_ready_pipe_cancel_never_waits_for_packet_deadline_on_shutdown(pipe_relay, monkeypatch):
    _read, _write, _client, _server, relay = pipe_relay
    entered = asyncio.Event()
    def blocked(*_args):
        entered.set()
        raise BlockingIOError
    monkeypatch.setattr(output.os, "write", blocked)
    relay.enqueue(data(b"\x01\x02\x03\x04" * 960))
    await entered.wait()
    pump = relay.pump
    await asyncio.wait_for(relay.close(), 0.1)
    assert pump.done() and pump.cancelled() and relay.closed
    assert not relay.queue and relay.queued_bytes == 0 and not relay.error


async def test_drop_cancels_pending_old_generation_before_exact_controller_ack(pipe_relay, monkeypatch):
    _read, _write, _client, server, relay = pipe_relay
    def blocked(*_args):
        raise BlockingIOError
    monkeypatch.setattr(output.os, "write", blocked)
    relay.enqueue(data(b"\x01\x02\x03\x04" * 960))
    await asyncio.sleep(0)
    requested = asyncio.Event()
    async def controller():
        assert await asyncio.get_running_loop().sock_recv(server, 32) == b"DropSync"
        assert relay.pump is None and not relay.queue and not relay.queued_bytes
        requested.set()
        await asyncio.sleep(0.01)
        await asyncio.get_running_loop().sock_sendall(server, b"OK")
    task = asyncio.create_task(controller())
    await relay.discard()
    await task
    assert requested.is_set() and relay.flushes == 1 and relay.frames_discarded == 960
    await relay.close()


@pytest.mark.parametrize("ack", [b"O", b"OK extra", b"ERROR"])
async def test_drop_ack_requires_the_complete_literal_ok(pipe_relay, ack):
    _read, _write, _client, server, relay = pipe_relay
    async def controller():
        await asyncio.get_running_loop().sock_recv(server, 32)
        await asyncio.get_running_loop().sock_sendall(server, ack)
    task = asyncio.create_task(controller())
    with pytest.raises(RuntimeFailure, match="exact completed DropSync"):
        await relay.discard()
    await task
    assert relay.failed.is_set() and relay.flushes == 0
    await relay.close()


async def test_silent_controller_cannot_block_drop_or_shutdown(pipe_relay):
    _read, _write, _client, _server, relay = pipe_relay
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(relay.discard(), 0.5)
    assert relay.failed.is_set() and relay.error and not relay.flushes
    await asyncio.wait_for(relay.close(), 0.1)


async def test_relay_constructor_does_not_take_caller_descriptors_on_format_capacity_failure(pipe_relay, monkeypatch):
    _read, original, client, _server, existing = pipe_relay
    monkeypatch.setattr(output.os, "fpathconf", lambda *_args: 1)
    with pytest.raises(RuntimeFailure, match="atomic write"):
        output.PCMRelay(replace(endpoint(), channels=2), [original, client.fileno()])
    os.fstat(original)
    assert client.fileno() >= 0
    await existing.close()


@pytest.mark.parametrize("capability", [False, None, 1, "true"])
@pytest.mark.parametrize("field,message", [("synchronous_drop", "completed synchronous Drop"),
                                          ("restricted_controller", "restricted controller")])
def test_relay_requires_typed_capabilities_before_touching_any_fd(monkeypatch, capability, field, message):
    inspected = []
    monkeypatch.setattr(output, "validate_pcm_fds", lambda _fds: inspected.append(True))
    with pytest.raises(RuntimeFailure, match=message):
        output.PCMRelay(replace(endpoint(), **{field: capability}), [9999, 9998])
    assert not inspected


@pytest.mark.skipif(sys.platform != "linux", reason="Complete final-output sessions require Linux SEQPACKET")
async def test_actual_wire_session_keeps_pcm_then_flushes_before_ack_and_reuses_the_output(pipe_relay, monkeypatch):
    read, write, client, controller, existing = pipe_relay
    await existing.close()
    room_id, generation, output_uid = str(uuid4()), uuid4().hex, os.geteuid() + 1001
    monkeypatch.setattr(output, "peer_identity", lambda _peer: (os.getpid(), output_uid))
    with TemporaryDirectory(prefix="shiri-pcm-", dir="/tmp") as directory:
        os.chmod(directory, 0o700)
        path = os.path.join(directory, "final.sock")
        worker = output.BluetoothOutput(endpoint(), [write, client.fileno()], room_id=room_id,
                                         generation=generation, output_uid=output_uid, path=path)
        await worker.dispatch("authorize-peer", {"pid": os.getpid()})
        serving = asyncio.create_task(worker.serve())
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        peer.setblocking(False)
        controller_events = []
        async def acknowledge_drops():
            for _ in range(2):
                assert await asyncio.get_running_loop().sock_recv(controller, 32) == b"DropSync"
                controller_events.append("drop")
                # Simulate the exact BlueALSA pipe drain performed by Drop.
                os.set_blocking(read, False)
                try:
                    while os.read(read, 4096):
                        pass
                except BlockingIOError:
                    pass
                await asyncio.get_running_loop().sock_sendall(controller, b"OK")
        dropping = asyncio.create_task(acknowledge_drops())
        initial = Frame(Operation.START, room_id, generation, str(uuid4()), 1, 0, 0, 48000, 0x8210, 2)
        loop = asyncio.get_running_loop()
        try:
            await loop.sock_connect(peer, path)
            await loop.sock_sendall(peer, initial.encode())
            ready = Frame.decode(await asyncio.wait_for(loop.sock_recv(peer, 20000), 0.5))
            assert ready.encode() == initial.acknowledgment().encode()
            payload = b"\x12\x34\x56\x78" * 480
            audio = replace(initial, operation=Operation.DATA, sequence=1, frames=480,
                            pts_ns=10_000_000_000, payload=payload)
            await loop.sock_sendall(peer, audio.encode())
            async def forwarded():
                while worker.relay.written_bytes < len(payload):  # noqa: ASYNC110 - bounded observation of actual pipe writes.
                    await asyncio.sleep(0.001)
            await asyncio.wait_for(forwarded(), 0.2)
            assert os.read(read, len(payload)) == payload
            flush = replace(initial, operation=Operation.FLUSH, generation=2, sequence=2)
            await loop.sock_sendall(peer, flush.encode())
            ack = Frame.decode(await asyncio.wait_for(loop.sock_recv(peer, 20000), 0.5))
            assert ack.encode() == flush.acknowledgment().encode() and controller_events == ["drop"]
            successor = replace(audio, generation=2, sequence=3, pts_ns=20_000_000_000)
            await loop.sock_sendall(peer, successor.encode())
            ending = replace(initial, operation=Operation.END, generation=3, sequence=4)
            await loop.sock_sendall(peer, ending.encode())
            assert Frame.decode(await asyncio.wait_for(loop.sock_recv(peer, 20000), 0.5)).encode() == ending.acknowledgment().encode()
            await dropping
            health = await worker.dispatch("health", {})
            assert health["ready"] and health["flushes"] == 2 and not health["uses_system_bus"]
            assert not health["uses_alsa_devices"] and not health["error"]
        finally:
            peer.close()
            serving.cancel()
            dropping.cancel()
            await asyncio.gather(serving, dropping, return_exceptions=True)
            await worker.close()


async def test_control_cannot_acknowledge_a_pump_failure_that_completed_during_cancel(pipe_relay, monkeypatch):
    _read, _write, _client, _server, relay = pipe_relay
    entered = asyncio.Event()
    async def failing():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            relay.error = "The pipe failed before Drop took ownership"
            relay.failed.set()
            raise
    relay.pump = asyncio.create_task(failing())
    await entered.wait()
    with pytest.raises(RuntimeFailure, match="before Drop"):
        await relay.discard()
    assert relay.flushes == 0
    await relay.close()
