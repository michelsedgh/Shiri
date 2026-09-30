"""Real socket regressions for unread peers, partial frames, and credential denial."""
import asyncio
from pathlib import Path
import socket
import struct
from tempfile import TemporaryDirectory

import pytest

from shiri.rpc import RpcError, call_rpc, serve_rpc, write_message


@pytest.fixture
def pressure_path():
    # macOS Unix sockets have a short path limit; avoid pytest's long tmp paths.
    with TemporaryDirectory(prefix="shiri-pressure-", dir="/tmp") as directory:
        yield Path(directory) / "rpc.sock"


async def await_empty(server):
    while server.tasks:  # noqa: ASYNC110 - RpcServer exposes its tracked task set, without a completion event.
        await asyncio.sleep(0.005)


async def test_client_timeout_cannot_wait_forever_flushing_to_an_unresponsive_peer(pressure_path):
    stop, entered = asyncio.Event(), asyncio.Event()
    peers = set()
    async def never_read(_reader, writer):
        peers.add(asyncio.current_task())
        entered.set()
        try:
            await stop.wait()
        finally:
            writer.transport.abort()
            await writer.wait_closed()
            peers.discard(asyncio.current_task())
    server = await asyncio.start_unix_server(never_read, path=str(pressure_path))
    caller = asyncio.create_task(call_rpc(pressure_path, "large-offer", {"blob": "x" * 1048576}, timeout=0.1))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        done, _pending = await asyncio.wait({caller}, timeout=0.6)
        assert caller in done, "The RPC timeout must include flushing/closing the request writer"
        with pytest.raises(RpcError) as failure:
            await caller
        assert failure.value.code == "runtime_unavailable"
    finally:
        stop.set()
        await asyncio.wait_for(asyncio.gather(caller, return_exceptions=True), 1)
        server.close()
        await server.wait_closed()
        await asyncio.wait_for(asyncio.gather(*tuple(peers)), 1)


async def test_server_reply_shares_the_deadline_and_never_appends_a_frame_to_partial_json(pressure_path):
    entered = asyncio.Event()
    async def large_reply(*_):
        entered.set()
        return {"blob": "x" * 1048576}
    server = await serve_rpc(pressure_path, large_reply)
    reader, writer = await asyncio.open_unix_connection(str(pressure_path))
    try:
        await write_message(writer, {"id": "unread", "operation": "large", "payload": {}, "timeout_ms": 100})
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(await_empty(server), 0.6)
        # StreamReader pauses after its limited buffer fills. Only now consume
        # the deliberately unread reply and check its closed partial frame.
        response = await asyncio.wait_for(reader.read(), 1)
        assert len(response) > 4
        declared = struct.unpack("!I", response[:4])[0]
        assert len(response) < declared + 4
        assert b"deadline_exceeded" not in response
    finally:
        writer.transport.abort()
        await writer.wait_closed()
        server.close()
        await asyncio.wait_for(server.wait_closed(), 1)


async def test_shutdown_cancels_an_unread_reply_without_flushing_it(pressure_path):
    entered = asyncio.Event()
    async def large_reply(*_):
        entered.set()
        return {"blob": "x" * 1048576}
    server = await serve_rpc(pressure_path, large_reply)
    _reader, writer = await asyncio.open_unix_connection(str(pressure_path))
    try:
        await write_message(writer, {"id": "shutdown", "operation": "large", "payload": {}, "timeout_ms": 30000})
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.sleep(0.03)
        server.close()
        await asyncio.wait_for(server.wait_closed(), 0.6)
        assert not server.tasks
    finally:
        writer.transport.abort()
        await writer.wait_closed()
        server.close()
        await asyncio.wait_for(server.wait_closed(), 1)


@pytest.mark.parametrize("size", [100000, 524301, 1048576])
async def test_a_healthy_large_reply_is_not_truncated_below_the_drain_low_watermark(
    pressure_path, monkeypatch, size,
):
    original = asyncio.start_unix_server
    async def constrained(callback, **kwargs):
        def connected(reader, writer):
            writer.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            return callback(reader, writer)
        return await original(connected, **kwargs)
    monkeypatch.setattr(asyncio, "start_unix_server", constrained)
    async def reply(*_):
        return {"blob": "x" * size}
    server = await serve_rpc(pressure_path, reply)
    try:
        result = await call_rpc(pressure_path, "large", timeout=2)
        assert result == {"blob": "x" * size}
    finally:
        server.close()
        await asyncio.wait_for(server.wait_closed(), 1)


@pytest.mark.parametrize("response,code", [
    ({"id": None, "ok": False, "code": "forbidden", "error": "Peer denied"}, "forbidden"),
    ({"id": None, "ok": False, "code": "runtime_error", "error": "Unidentified failure"}, "invalid_response"),
    ({"id": None, "ok": True, "result": {}}, "invalid_response"),
    ({"id": "different", "ok": True, "result": {}}, "invalid_response"),
    ({"id": "different", "ok": False, "code": "forbidden"}, "invalid_response"),
    ({"id": None, "ok": True, "code": "forbidden", "result": {}}, "invalid_response"),
])
async def test_only_explicit_pre_envelope_credential_denial_can_omit_the_response_id(pressure_path, response, code):
    peers = set()
    async def respond(_reader, writer):
        peers.add(asyncio.current_task())
        try:
            await write_message(writer, response)
        finally:
            writer.close()
            await writer.wait_closed()
            peers.discard(asyncio.current_task())
    server = await asyncio.start_unix_server(respond, path=str(pressure_path))
    try:
        with pytest.raises(RpcError) as failure:
            await call_rpc(pressure_path, "health", timeout=1)
        assert failure.value.code == code
    finally:
        server.close()
        await server.wait_closed()
        await asyncio.wait_for(asyncio.gather(*tuple(peers)), 1)
