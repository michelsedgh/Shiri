"""Observe the actual native socket boundary without a receiver or hardware.

Linux uses production SOCK_SEQPACKET. macOS lacks that Unix socket type, so
message-boundary regressions use a real private SOCK_DGRAM socketpair there;
the actual Linux listener/peer-credential test remains Linux-only.
"""
import asyncio
from dataclasses import replace
import json
import logging
import os
import socket

import pytest

from shiri.runtime.native import NativeHandle
from shiri.runtime.timing import Clock, Packet, TimingError
from test_native_audio import begin, controller, pcm as fixture_pcm


def pcm(token, **changes):
    # Keep real message boundaries within macOS's 2048-byte Unix datagram cap.
    return fixture_pcm(token, frames=240, **changes)


def serve(c):
    try:
        receiver, producer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    except OSError:
        if hasattr(socket, "SO_PEERCRED"):
            raise
        receiver, producer = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    receiver.setblocking(False)
    producer.setblocking(False)
    task = asyncio.create_task(c._connection(receiver))
    c.connections.add(task)
    task.add_done_callback(c.connections.discard)
    return producer, task


async def grant(producer):
    loop = asyncio.get_running_loop()
    await loop.sock_sendall(producer, begin().encode())
    return Packet.decode(await asyncio.wait_for(loop.sock_recv(producer, 4096), 1))


async def sent(producer, packet):
    await asyncio.get_running_loop().sock_sendall(producer, packet.encode())


async def until(predicate):
    async def poll():
        for _ in range(1000):
            if predicate():
                return
            await asyncio.sleep(0)
        raise AssertionError("Native socket observation did not complete")
    await asyncio.wait_for(poll(), 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,changes", [
    ("clock_bracket", {"monotonic_after_ns": 15_001_000_001}),
    ("clock_age", {"monotonic_before_ns": 14_749_999_999, "monotonic_after_ns": 14_750_000_199}),
    ("clock_projection", {"presentation_ns": 12_000_000_001}),
    ("frame_gap", {"sequence": 2}),
    ("presentation_order", {"clock": Clock.MONOTONIC}),
])
async def test_actual_socket_preserves_gate_reason_before_exact_source_retirement(caplog, reason, changes):
    c, writer, client = controller()
    await c.initialize()
    producer, task = serve(c)
    try:
        token = await grant(producer)
        await sent(producer, pcm(token))
        await until(lambda: len(writer.packets) == 1)
        seen_on_retirement = []
        original = client.request
        async def request(method, path, *, json):
            if json["session_id"] is None and json["epoch"]:
                seen_on_retirement.append((c.health()["native_ingress_fault"], len(caplog.records)))
            return await original(method, path, json=json)
        client.request = request
        caplog.set_level(logging.WARNING, logger="shiri.runtime.native")
        await sent(producer, replace(pcm(token, sequence=1, frame_index=240), **changes))
        await asyncio.wait_for(task, 1)
        fault = c.health()["native_ingress_fault"]
        assert fault["reason"] == reason and fault["stage"] == "pcm"
        assert fault["previous"]["sequence"] == 0 and fault["previous"]["next_frame"] == 240
        assert fault["packet"]["frame_index"] == 240
        assert seen_on_retirement == [(fault, 1)]
        assert c.mixer.error == f"Native timing ingress failed: {reason}"
        assert c.actor.snapshot()["owner"] is None and len(writer.packets) == 1
        assert len(client.requests) == 3  # idle, exact grant, fault retirement
        rendered = caplog.records[0].getMessage()
        assert json.loads(rendered.split(": ", 1)[1]) == fault
        assert token.session_id not in rendered and token.incarnation.hex() not in rendered
        assert len(rendered) < 2000
        # External readers cannot mutate the retained observation.
        fault["packet"]["sequence"] = -1
        assert c.health()["native_ingress_fault"]["packet"]["sequence"] >= 0
    finally:
        producer.close()
        await c.close()


@pytest.mark.asyncio
async def test_malformed_owned_packet_has_safe_decode_reason_and_previous_fence(caplog):
    c, writer, _ = controller()
    await c.initialize()
    producer, task = serve(c)
    try:
        token = await grant(producer)
        await sent(producer, pcm(token))
        await until(lambda: len(writer.packets) == 1)
        caplog.set_level(logging.WARNING, logger="shiri.runtime.native")
        await asyncio.get_running_loop().sock_sendall(producer, b"private arbitrary malformed bytes")
        await asyncio.wait_for(task, 1)
        fault = c.health()["native_ingress_fault"]
        assert fault["reason"] == "invalid_envelope" and fault["stage"] == "decode"
        assert fault["previous"]["next_frame"] == 240 and "packet" not in fault
        assert "private arbitrary" not in caplog.text
    finally:
        producer.close()
        await c.close()


@pytest.mark.asyncio
async def test_unknown_exception_text_and_pcm_are_never_retained_or_logged(caplog):
    c, _, _ = controller()
    await c.initialize()
    producer, task = serve(c)
    private = "untrusted-private-SDP-token\n" * 1000
    async def failure(packet, handle):
        raise TimingError(private)
    c.message = failure
    try:
        token = await grant(producer)
        caplog.set_level(logging.WARNING, logger="shiri.runtime.native")
        await sent(producer, pcm(token))
        await asyncio.wait_for(task, 1)
        fault = c.health()["native_ingress_fault"]
        assert fault["reason"] == "timing_invalid"
        assert "untrusted-private" not in caplog.text + json.dumps(fault)
        assert "pcm" not in fault["packet"] and len(caplog.records[0].getMessage()) < 2000
    finally:
        producer.close()
        await c.close()


@pytest.mark.asyncio
async def test_late_old_connection_fault_cannot_fault_or_retire_successor(caplog):
    c, writer, client = controller()
    await c.initialize()
    old, old_task = serve(c)
    successor, successor_task = serve(c)
    entered, release = asyncio.Event(), asyncio.Event()
    original = c.message
    try:
        previous = await grant(old)
        async def delayed(packet, handle):
            if packet.session == previous.session:
                entered.set()
                await release.wait()
                raise TimingError("Native clock mapping has no sufficiently narrow bracket")
            return await original(packet, handle)
        c.message = delayed
        await sent(old, pcm(previous))
        await asyncio.wait_for(entered.wait(), 1)
        current = await grant(successor)
        await sent(successor, pcm(current))
        await until(lambda: len(writer.packets) == 1)
        caplog.set_level(logging.WARNING, logger="shiri.runtime.native")
        release.set()
        await asyncio.wait_for(old_task, 1)
        assert c.health()["native_ingress_fault"] is None and c.mixer.error is None
        assert c.actor.snapshot()["owner"]["session_id"] == current.session_id
        assert len(client.requests) == 3 and not caplog.records
        assert not successor_task.done() and writer.packets[-1].session == current.session
    finally:
        release.set()
        old.close()
        successor.close()
        await c.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not hasattr(socket, "SO_PEERCRED"), reason="Production native peer credentials require Linux")
async def test_actual_wrong_peer_never_enters_admission_or_faults_current_owner(tmp_path, caplog):
    c, _, client = controller()
    await c.initialize()
    admitted = await c.begin(begin(), NativeHandle(c))
    path = tmp_path / "native.sock"
    await c.listen(path, native_uid=os.getuid() + 1)
    producer = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    producer.setblocking(False)
    try:
        loop = asyncio.get_running_loop()
        caplog.set_level(logging.WARNING, logger="shiri.runtime.native")
        await loop.sock_connect(producer, str(path))
        try:
            await loop.sock_sendall(producer, begin().encode())
            assert await asyncio.wait_for(loop.sock_recv(producer, 4096), 1) == b""
        except (ConnectionResetError, BrokenPipeError):
            pass
        assert c.health()["native_ingress_fault"] is None and c.mixer.error is None
        assert c.actor.snapshot()["owner"]["session_id"] == admitted.session_id and len(client.requests) == 2
        assert not caplog.records
    finally:
        producer.close()
        await c.close()
