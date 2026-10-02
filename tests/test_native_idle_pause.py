"""Actual Unix socket waits during phone pause and exact native retirement.

Production SEQPACKET runs on Linux; macOS uses a real STREAM pair for this
single-message-paced lifecycle test so peer EOF remains observable.
"""
import asyncio
from dataclasses import replace
import socket

import pytest

from shiri.runtime.native import NativeHandle
from shiri.runtime.timing import Kind
from test_native_audio import begin, controller
from test_native_ingress_diagnostics import grant, pcm, sent, until
from test_native_reader_lifecycle import registered


def serve(c):
    try:
        receiver, producer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    except OSError:
        if hasattr(socket, "SO_PEERCRED"):
            raise
        receiver, producer = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    receiver.setblocking(False)
    producer.setblocking(False)
    return producer, c._start_connection(receiver)


@pytest.mark.asyncio
async def test_phone_pause_beyond_actual_old_30_second_cutoff_then_resume_without_volume():
    c, writer, _ = controller()
    await c.initialize()
    peer, task = serve(c)
    try:
        token = await grant(peer)
        handle = c.handles[token.session_id]
        await until(lambda: handle.receive_task is not None)
        read = handle.receive_task
        await asyncio.sleep(31)
        assert not task.done() and not read.done() and c.actor.owns(handle.token)
        assert c.health()["native_ingress_fault"] is None
        await sent(peer, pcm(token))
        await until(lambda: len(writer.packets) == 1)
        assert writer.packets[0].session == token.session
        assert handle.last_volume is None
    finally:
        peer.close()
        await asyncio.wait_for(c.close(), .5)
    assert task.done() and read.done() and not c.connections and not c.handles


@pytest.mark.asyncio
@pytest.mark.parametrize("retirement", ["eof", "end", "revoke", "malformed", "shutdown", "cancel"])
async def test_admitted_idle_reader_waits_without_deadline_and_retires_exactly(monkeypatch, retirement):
    timeouts = []
    receive = NativeHandle.receive
    async def observed(self, maximum, timeout):
        timeouts.append(timeout)
        # Accelerate only finite old deadlines. The fixed indefinite wait keeps
        # the actual registered socket reader throughout the idle interval.
        return await receive(self, maximum, timeout=.03 if timeout is not None else None)
    monkeypatch.setattr(NativeHandle, "receive", observed)
    c, writer, client = controller()
    await c.initialize()
    peer, task = serve(c)
    successor = successor_task = None
    try:
        token = await grant(peer)
        handle = c.handles[token.session_id]
        fd = handle.connection.fileno()
        await until(lambda: handle.receive_task is not None and registered(fd))
        read = handle.receive_task
        assert timeouts[:2] == [7, None]
        await asyncio.sleep(.07)
        assert not read.done() and not task.done() and c.actor.owns(handle.token)
        if retirement == "eof":
            peer.close()
        elif retirement == "end":
            await sent(peer, replace(token, kind=Kind.END))
        elif retirement == "revoke":
            successor, successor_task = serve(c)
            current = await grant(successor)
            await sent(successor, pcm(current))
            await until(lambda: len(writer.packets) == 1)
            assert writer.packets[0].session == current.session
            assert not c.mixer.accept(pcm(token), handle.token)
        elif retirement == "malformed":
            await asyncio.get_running_loop().sock_sendall(peer, b"broken-native-packet")
        elif retirement == "shutdown":
            await asyncio.wait_for(c.close(), .5)
        elif retirement == "cancel":
            task.cancel()
        await until(task.done)
        assert read.done() and handle.closed and handle.connection.fileno() == -1
        assert not registered(fd) and token.session_id not in c.handles
        if retirement == "malformed":
            assert c.health()["native_ingress_fault"]["reason"] == "invalid_envelope"
        else:
            assert c.health()["native_ingress_fault"] is None
    finally:
        peer.close()
        if successor is not None:
            successor.close()
        await asyncio.wait_for(c.close(), .5)
    assert not c.connections and not c.handles
    assert successor_task is None or successor_task.done()


@pytest.mark.asyncio
async def test_initial_begin_remains_exactly_seven_seconds_and_refusal_retires_socket(monkeypatch):
    timeouts = []
    receive = NativeHandle.receive
    async def accelerated(self, maximum, timeout):
        timeouts.append(timeout)
        assert timeout == 7
        return await receive(self, maximum, timeout=.02)
    monkeypatch.setattr(NativeHandle, "receive", accelerated)
    c, _, _ = controller()
    await c.initialize()
    peer, task = serve(c)
    try:
        await asyncio.wait_for(asyncio.shield(task), 1)
        await asyncio.sleep(0)  # Let the exact connection done callback remove its registration.
        assert not c.connections
        assert timeouts == [7] and not c.handles and c.actor.snapshot()["owner"] is None
        assert c.health()["native_ingress_fault"] is None
    finally:
        peer.close()
        await asyncio.wait_for(c.close(), .5)
    assert not c.connections


@pytest.mark.asyncio
async def test_failed_takeover_barrier_retires_old_indefinite_reader_and_new_candidate():
    c, _, client = controller()
    await c.initialize()
    peer, task = serve(c)
    successor, successor_task = serve(c)
    try:
        token = await grant(peer)
        handle = c.handles[token.session_id]
        fd = handle.connection.fileno()
        await until(lambda: handle.receive_task is not None and registered(fd))
        read = handle.receive_task
        client.bad_ack = True
        await sent(successor, begin())
        await until(lambda: task.done() and successor_task.done() and not c.connections)
        assert read.done() and handle.connection.fileno() == -1 and not registered(fd)
        assert not c.handles and not c.connections
        assert c.actor.snapshot()["owner"] is None and c.actor.snapshot()["error"]
    finally:
        peer.close()
        successor.close()
        await asyncio.wait_for(c.close(), .5)
