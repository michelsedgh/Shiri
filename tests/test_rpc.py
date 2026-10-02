"""Real Unix-socket protocol tests, including disconnected and expired callers."""
import asyncio
import os
import struct
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from shiri.rpc import MAX_MESSAGE_BYTES, RpcError, call_rpc, read_message, serve_rpc, write_message


@pytest.fixture
def rpcdir():
    with TemporaryDirectory(prefix="shiri-rpc-", dir="/tmp") as directory:
        yield Path(directory)


async def test_real_exchange_and_peer_authorization(rpcdir):
    async def handler(operation, payload):
        return {"operation": operation, "payload": payload}
    path = rpcdir / "runtime.sock"
    server = await serve_rpc(path, handler)
    try:
        result = await call_rpc(path, "health", {"value": "safe"})
        assert result == {"operation": "health", "payload": {"value": "safe"}}
        assert path.stat().st_mode & 0o777 == 0o660
    finally:
        server.close()
        await server.wait_closed()


async def test_expired_request_cancels_operation_and_releases_connection(rpcdir):
    cancelled = asyncio.Event()
    async def handler(*_):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    server = await serve_rpc(rpcdir / "rpc.sock", handler)
    try:
        with pytest.raises(RpcError, match="unavailable|timed out"):
            await call_rpc(rpcdir / "rpc.sock", "slow", timeout=.12)
        await asyncio.wait_for(cancelled.wait(), 1)
    finally:
        server.close()
        await server.wait_closed()


async def test_disconnect_cancels_active_operation_without_waiting_for_deadline(rpcdir):
    entered, cancelled = asyncio.Event(), asyncio.Event()
    async def handler(*_):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    path = rpcdir / "rpc.sock"
    server = await serve_rpc(path, handler)
    try:
        _, writer = await asyncio.open_unix_connection(str(path))
        await write_message(writer, {"id": "one", "operation": "offer", "payload": {}, "timeout_ms": 15000})
        await entered.wait()
        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(cancelled.wait(), 1)
    finally:
        server.close()
        await server.wait_closed()


async def test_server_shutdown_cancels_and_joins_clients(rpcdir):
    entered, cancelled = asyncio.Event(), asyncio.Event()
    async def handler(*_):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    path = rpcdir / "rpc.sock"
    server = await serve_rpc(path, handler)
    pending = asyncio.create_task(call_rpc(path, "slow"))
    await entered.wait()
    server.close()
    await asyncio.wait_for(server.wait_closed(), 1)
    assert cancelled.is_set()
    with pytest.raises(RpcError):
        await pending


@pytest.mark.parametrize("payload", [b'{"id":"a","id":"b"}', b'{"value":NaN}', b'[]'])
async def test_ambiguous_or_invalid_json_is_rejected(payload):
    reader = asyncio.StreamReader()
    reader.feed_data(struct.pack("!I", len(payload)) + payload)
    reader.feed_eof()
    with pytest.raises((RpcError, ValueError)):
        await read_message(reader)


async def test_rejects_length_before_reading_body():
    reader = asyncio.StreamReader()
    reader.feed_data(struct.pack("!I", MAX_MESSAGE_BYTES + 1))
    with pytest.raises(RpcError):
        await read_message(reader)


async def test_refuses_non_socket_path(rpcdir):
    path = rpcdir / "data"
    path.write_text("important")
    with pytest.raises(RuntimeError, match="non-socket"):
        await serve_rpc(path, lambda *_: None)
    assert path.read_text() == "important"


@pytest.mark.skipif(not hasattr(__import__('socket'), 'SO_PEERCRED'), reason="Linux credentials")
async def test_unauthorized_local_peer_does_not_reach_handler(rpcdir):
    entered = []
    async def handler(*_):
        entered.append(True)
        return {}
    path = rpcdir / "rpc.sock"
    server = await serve_rpc(path, handler, allowed_uids={os.getuid() + 10000})
    try:
        with pytest.raises(RpcError, match="cannot control"):
            await call_rpc(path, "health")
        assert entered == []
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_operation_authority_uses_kernel_uid_before_scheduling_handler(rpcdir, monkeypatch):
    import socket
    import shiri.rpc as rpc
    peer = {"uid": 202}
    monkeypatch.setattr(socket, "SO_PEERCRED", 17, raising=False)
    monkeypatch.setattr(rpc, "peer_uid", lambda writer: peer["uid"])
    called = []
    async def handler(operation, payload):
        called.append((operation, payload))
        return {"ok": True}
    path = rpcdir / "restricted.sock"
    server = await serve_rpc(path, handler, allowed_uids={0, 202}, operation_uids={"control-intent": {0}})
    try:
        assert await call_rpc(path, "health") == {"ok": True}
        with pytest.raises(RpcError) as rejected:
            await call_rpc(path, "control-intent", {"uid": 0, "revision": 2})
        assert rejected.value.code == "forbidden"
        assert called == [("health", {})]
        peer["uid"] = 0
        await call_rpc(path, "control-intent", {"revision": 2})
        assert called[-1] == ("control-intent", {"revision": 2})
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_operation_authority_cannot_start_without_peer_credentials(rpcdir, monkeypatch):
    import socket
    monkeypatch.delattr(socket, "SO_PEERCRED", raising=False)
    path = rpcdir / "restricted.sock"
    with pytest.raises(RuntimeError, match="peer credentials"):
        await serve_rpc(path, lambda *_: None, operation_uids={"control-intent": {0}})
    assert not path.exists()
