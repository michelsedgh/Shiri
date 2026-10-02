"""Real Unix reader retirement, descriptor reuse and native/RPC coexistence.

Linux uses the production SEQPACKET ingress. The existing portable fixture uses
message-preserving Unix datagrams on macOS; neither path needs GI or speakers.
"""
import asyncio
from dataclasses import replace
import os
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import weakref

import pytest

from shiri.rpc import call_rpc, serve_rpc
from shiri.runtime.audio import AudioWorker
from shiri.runtime.native import NativeHandle
from shiri.runtime.timing import Kind
from test_native_audio import begin, controller
from test_native_ingress_diagnostics import grant, pcm, sent, serve, until


def registered(fd):
    # Verify the actual selector bookkeeping implicated in the descriptor-reuse
    # defect, in addition to observing a successful framed RPC round trip.
    return fd in asyncio.get_running_loop()._selector.get_map()


@pytest.mark.asyncio
@pytest.mark.parametrize("retirement", ["quiesce", "synchronous_abort", "watchdog_abort"])
async def test_first_health_after_actual_native_takeover_survives_descriptor_reuse(retirement):
    c, writer, _ = controller()
    untouched, other_writer, _ = controller()
    await c.initialize()
    await untouched.initialize()
    old, old_task = serve(c)
    successor, successor_task = serve(c)
    other, other_task = serve(untouched)
    worker = AudioWorker(c.mixer, native=c)
    directory = TemporaryDirectory(prefix="shiri-native-reader-", dir="/tmp")
    path = Path(directory.name) / "audio.sock"
    server = await serve_rpc(path, worker.dispatch,
                             allowed_uids={os.getuid()} if hasattr(socket, "SO_PEERCRED") else None)
    retirement_entered, retirement_release, retirement_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    try:
        previous = await grant(old)
        other_grant = await grant(other)
        old_handle = c.handles[previous.session_id]
        old_fd = old_handle.connection.fileno()
        await until(lambda: registered(old_fd))
        receive = getattr(old_handle, "receive_task", None)
        if retirement != "quiesce":
            async def blocked_retirement(_token):
                retirement_entered.set()
                await retirement_release.wait()
            old_handle.quiesce = blocked_retirement
            original_abort = old_handle.abort
            def observed_abort(token):
                original_abort(token)
                if old_handle.closed:
                    retirement_finished.set()
            old_handle.abort = observed_abort
            if retirement == "watchdog_abort":
                c.actor._timeout = .02
        current = await grant(successor)
        if retirement != "quiesce":
            await asyncio.wait_for(retirement_entered.wait(), .3)
            if retirement == "synchronous_abort":
                old_handle.abort(old_handle.token)
            await asyncio.wait_for(retirement_finished.wait(), .3)
        # The old descriptor is now the lowest free descriptor and is reused by
        # the new RPC stream. The preimage leaves its selector entry behind:
        # the first RPC times out even though its handler returns healthy state.
        health = await call_rpc(path, "health", {}, timeout=.3)
        assert health["source"]["owner"]["session_id"] == current.session_id
        assert health["source"]["ready"] and health["native_ingress_fault"] is None
        await until(old_task.done)
        assert old_handle.closed and receive is not None and receive.done()
        assert old_handle.receive_task is None and not registered(old_fd)
        assert previous.session_id not in c.handles
        await sent(successor, pcm(current))
        await sent(other, pcm(other_grant))
        await until(lambda: len(writer.packets) == len(other_writer.packets) == 1)
        assert writer.packets[0].session == current.session
        assert other_writer.packets[0].session == other_grant.session
        assert c.actor.snapshot()["owner"]["epoch"] == 2
        assert untouched.actor.snapshot()["owner"]["epoch"] == 1
        assert untouched.health()["native_ingress_fault"] is None
        assert not successor_task.done() and not other_task.done()
        assert not c.mixer.accept(pcm(previous), old_handle.token)
        assert len(writer.packets) == 1
    finally:
        retirement_release.set()
        server.close()
        await server.wait_closed()
        old.close()
        successor.close()
        other.close()
        await c.close()
        await untouched.close()
        directory.cleanup()


@pytest.mark.asyncio
async def test_repeated_takeovers_do_not_accumulate_readers_connections_or_admission_handles():
    c, writer, _ = controller()
    await c.initialize()
    worker = AudioWorker(c.mixer, native=c)
    with TemporaryDirectory(prefix="shiri-native-takeovers-", dir="/tmp") as directory:
        path = Path(directory) / "audio.sock"
        server = await serve_rpc(path, worker.dispatch)
        peers, connections, reads = [], [], []
        previous, old_handle = None, None
        try:
            for index in range(12):  # Beyond the8 transport/admission limit.
                peer, connection = serve(c)
                peers.append(peer)
                connections.append(connection)
                token = await grant(peer)
                handle = c.handles[token.session_id]
                fd = handle.connection.fileno()
                await until(lambda handle=handle, fd=fd: handle.receive_task is not None and registered(fd))
                reads.append(handle.receive_task)
                health = await call_rpc(path, "health", {}, timeout=.3)
                assert health["source"]["owner"]["session_id"] == token.session_id
                assert health["source"]["ready"] and health["native_ingress_fault"] is None
                await sent(peer, pcm(token))
                await until(lambda index=index: len(writer.packets) == index + 1)
                if previous is not None:
                    assert not c.mixer.accept(pcm(previous), old_handle.token)
                    await until(connections[-2].done)
                    assert reads[-2].done() and old_handle.connection.fileno() == -1
                await until(lambda: len(c.connections) == 1)
                assert len(c.handles) == len(c.connections) == 1
                assert writer.packets[-1].session == token.session
                previous, old_handle = token, handle
            assert c.actor.snapshot()["epoch"] == 12
            assert all(task.done() for task in reads[:-1] + connections[:-1])
        finally:
            server.close()
            await server.wait_closed()
            for peer in peers:
                peer.close()
            await c.close()
        assert all(task.done() for task in reads + connections)
        assert not c.handles and not c.connections


class ObservedSocket(socket.socket):
    before_close = None

    def close(self):
        if self.fileno() >= 0 and self.before_close is not None:
            self.before_close()
        super().close()


def observed_pair():
    receiver, peer = socket.socketpair()
    receiver = ObservedSocket(fileno=receiver.detach())
    receiver.setblocking(False)
    peer.setblocking(False)
    return receiver, peer


@pytest.mark.asyncio
async def test_quiesce_joins_exact_receive_before_close_without_joining_parent_connection():
    c, _, _ = controller()
    await c.initialize()
    receiver, peer = observed_pair()
    handle = NativeHandle(c, receiver)
    token = (await c.begin(begin(), handle))
    fd = receiver.fileno()
    parent = asyncio.create_task(handle.receive(4096, 30))
    observations = []
    try:
        await until(lambda: handle.receive_task is not None and registered(fd))
        exact_read = handle.receive_task
        receiver.before_close = lambda: observations.append((exact_read.done(), registered(fd)))
        await asyncio.wait_for(handle.quiesce(handle.token), .5)
        result = await asyncio.gather(parent, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
        assert observations == [(True, False)]
        assert receiver.fileno() == -1 and exact_read.done()
        assert token.session_id == handle.token.session_id
    finally:
        receiver.close()
        peer.close()
        parent.cancel()
        await asyncio.gather(parent, return_exceptions=True)
        await c.close()


@pytest.mark.asyncio
async def test_abort_unregisters_reader_before_immediate_close_and_ignores_wrong_token():
    c, _, _ = controller()
    await c.initialize()
    receiver, peer = observed_pair()
    handle = NativeHandle(c, receiver)
    await c.begin(begin(), handle)
    fd = receiver.fileno()
    parent = asyncio.create_task(handle.receive(4096, 30))
    observations = []
    try:
        await until(lambda: handle.receive_task is not None and registered(fd))
        handle.abort(handle.token.model_copy(update={"epoch": handle.token.epoch + 1}))
        assert receiver.fileno() == fd and registered(fd) and not parent.done()
        exact_read = handle.receive_task
        receiver.before_close = lambda: observations.append(registered(fd))
        handle.abort(handle.token)
        assert observations == [False] and receiver.fileno() == -1
        assert not registered(fd)
        await asyncio.gather(parent, return_exceptions=True)
        assert exact_read.done()
    finally:
        receiver.close()
        peer.close()
        parent.cancel()
        await asyncio.gather(parent, return_exceptions=True)
        await c.close()


@pytest.mark.asyncio
async def test_canceled_quiesce_still_retires_exact_reader_without_orphaning_it():
    c, _, _ = controller()
    await c.initialize()
    receiver, peer = observed_pair()
    handle = NativeHandle(c, receiver)
    await c.begin(begin(), handle)
    fd = receiver.fileno()
    parent = asyncio.create_task(handle.receive(4096, 30))
    quiesce = None
    try:
        await until(lambda: handle.receive_task is not None and registered(fd))
        exact_read = handle.receive_task
        quiesce = asyncio.create_task(handle.quiesce(handle.token))
        await until(lambda: handle.closed)
        quiesce.cancel()
        result = await asyncio.gather(quiesce, parent, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
        assert receiver.fileno() == -1 and not registered(fd)
        assert exact_read.done() and handle.receive_task is None
    finally:
        receiver.close()
        peer.close()
        for task in (quiesce, parent):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in (quiesce, parent) if task is not None), return_exceptions=True)
        await c.close()


@pytest.mark.asyncio
async def test_native_end_cannot_self_join_its_connection_finally():
    c, _, _ = controller()
    await c.initialize()
    peer, connection = serve(c)
    try:
        token = await grant(peer)
        handle = c.handles[token.session_id]
        fd = handle.connection.fileno()
        await sent(peer, replace(token, kind=Kind.END))
        await until(connection.done)
        assert c.actor.snapshot()["owner"] is None and c.actor.snapshot()["ready"]
        assert handle.closed and handle.connection.fileno() == -1 and not registered(fd)
        assert c.health()["native_ingress_fault"] is None
        assert token.session_id not in c.handles
    finally:
        peer.close()
        await c.close()


@pytest.mark.asyncio
async def test_failed_native_end_barrier_still_closes_owned_socket_and_reader():
    c, _, client = controller()
    await c.initialize()
    peer, connection = serve(c)
    try:
        token = await grant(peer)
        handle = c.handles[token.session_id]
        fd = handle.connection.fileno()
        await until(lambda: handle.receive_task is not None and registered(fd))
        client.bad_ack = True
        await sent(peer, replace(token, kind=Kind.END))
        await until(connection.done)
        assert c.actor.snapshot()["owner"] is None and not c.actor.snapshot()["ready"]
        assert c.actor.snapshot()["error"]
        assert handle.connection.fileno() == -1 and not registered(fd)
        assert token.session_id not in c.handles
    finally:
        peer.close()
        await c.close()


@pytest.mark.asyncio
async def test_canceled_inflight_takeover_retires_both_exact_readers_without_granting_successor():
    c, _, client = controller()
    await c.initialize()
    old, old_connection = serve(c)
    incoming, incoming_connection = serve(c)
    try:
        token = await grant(old)
        handle = c.handles[token.session_id]
        fd = handle.connection.fileno()
        await until(lambda: handle.receive_task is not None and registered(fd))
        read = handle.receive_task
        client.block = asyncio.Event()
        await sent(incoming, begin())
        await until(lambda: c.actor.snapshot()["transitioning"])
        incoming_connection.cancel()
        result = await asyncio.gather(incoming_connection, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
        await until(old_connection.done)
        assert read.done() and handle.connection.fileno() == -1 and not registered(fd)
        assert c.actor.snapshot()["owner"] is None and not c.actor.snapshot()["ready"]
        await until(lambda: not c.connections)
        assert not c.handles and not c.connections
    finally:
        old.close()
        incoming.close()
        await c.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not hasattr(socket, "SO_PEERCRED"), reason="Actual native SEQPACKET peer EOF requires Linux")
async def test_native_peer_eof_joins_its_reader_without_an_ingress_fault():
    c, _, _ = controller()
    await c.initialize()
    peer, connection = serve(c)
    try:
        token = await grant(peer)
        handle = c.handles[token.session_id]
        fd = handle.connection.fileno()
        await until(lambda: handle.receive_task is not None and registered(fd))
        read = handle.receive_task
        peer.close()
        await until(connection.done)
        assert read.done() and handle.connection.fileno() == -1 and not registered(fd)
        assert c.actor.snapshot()["owner"] is None and c.actor.snapshot()["ready"]
        assert c.health()["native_ingress_fault"] is None
        await until(lambda: not c.connections)
        assert not c.handles and not c.connections
    finally:
        peer.close()
        await c.close()


@pytest.mark.asyncio
async def test_controller_shutdown_joins_all_owned_reads_and_closes_exact_sockets():
    c, _, _ = controller()
    await c.initialize()
    first, first_connection = serve(c)
    second, second_connection = serve(c)
    try:
        token = await grant(first)
        handle = c.handles[token.session_id]
        fd = handle.connection.fileno()
        await until(lambda: handle.receive_task is not None and registered(fd))
        read = handle.receive_task
        # Second peer deliberately remains before BEGIN, in its7s receive wait.
        await asyncio.sleep(.01)
        await asyncio.wait_for(c.close(), .5)
        assert first_connection.done() and second_connection.done() and read.done()
        assert handle.connection.fileno() == -1 and not registered(fd)
        assert not c.connections and not c.handles
    finally:
        first.close()
        second.close()
        await c.close()


@pytest.mark.asyncio
async def test_controller_close_retires_accepted_socket_before_connection_first_instruction():
    c, _, _ = controller()
    await c.initialize()
    accepted, peer = socket.socketpair()
    accepted.setblocking(False)
    fd, reference = accepted.fileno(), weakref.ref(accepted)
    task = c._start_connection(accepted)
    del accepted
    try:
        # Deliberately do not yield between acceptance and shutdown. Keeping
        # the canceled task alive exposes its retained socket, without GC.
        await c.close()
        assert task.cancelled() and not c.connections and not c.handles
        with pytest.raises(OSError):
            os.fstat(fd)
        assert reference() is None or reference().fileno() == -1
    finally:
        peer.close()
        await c.close()


@pytest.mark.asyncio
async def test_accepted_connection_done_callback_cannot_close_reused_successor_descriptor():
    c, _, _ = controller()
    await c.initialize()
    accepted, peer = socket.socketpair()
    old_fd = accepted.fileno()
    successors = []

    async def finish(connection):
        connection.close()
        successors.extend(socket.socketpair())
        assert successors[0].fileno() == old_fd

    c._connection = finish
    task = c._start_connection(accepted)
    try:
        await task
        await until(lambda: not c.connections)
        assert accepted.fileno() == -1
        assert successors[0].fileno() == old_fd
        successors[0].sendall(b"still owned by successor")
        assert successors[1].recv(64) == b"still owned by successor"
        await c.close()
        assert successors[0].fileno() == old_fd
    finally:
        accepted.close()
        peer.close()
        for connection in successors:
            connection.close()
        await c.close()


@pytest.mark.asyncio
async def test_accepted_connection_normal_shutdown_joins_registered_reader_before_callback_close():
    c, _, _ = controller()
    await c.initialize()
    accepted, peer = observed_pair()
    fd = accepted.fileno()
    observations = []
    accepted.before_close = lambda: observations.append(registered(fd))
    task = c._start_connection(accepted)
    try:
        await until(lambda: registered(fd))
        await asyncio.wait_for(c.close(), .5)
        assert task.done() and not c.connections and not c.handles
        assert accepted.fileno() == -1 and not registered(fd)
        assert observations == [False]
    finally:
        peer.close()
        accepted.close()
        await c.close()
