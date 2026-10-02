"""Bounded, framed JSON RPC over a local Unix socket. No arbitrary shell commands."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import socket
import stat
import struct
from collections.abc import Awaitable, Callable
import uuid

from shiri.deadline import bounded

MAX_MESSAGE_BYTES = 2 * 1024 * 1024
MAX_CONNECTIONS = 32


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("RPC JSON contains duplicate keys")
        result[key] = value
    return result


class RpcServer:
    """Closing the listener also cancels and joins active client operations."""
    def __init__(self, server, tasks, state):
        self.server, self.tasks, self.state = server, tasks, state

    def close(self):
        self.state["closed"] = True
        self.server.close()
        for task in tuple(self.tasks):
            task.cancel()

    async def wait_closed(self):
        await self.server.wait_closed()
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)


class RpcError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


async def read_message(reader: asyncio.StreamReader) -> dict:
    length = struct.unpack("!I", await reader.readexactly(4))[0]
    if not 0 < length <= MAX_MESSAGE_BYTES:
        raise RpcError("invalid_request", "RPC message exceeds its size limit")
    def constant(value):
        raise ValueError(f"Non-finite JSON constant: {value}")
    payload = json.loads(await reader.readexactly(length), parse_constant=constant, object_pairs_hook=unique_object)
    if not isinstance(payload, dict):
        raise RpcError("invalid_request", "RPC message must be an object")
    return payload


async def write_message(writer: asyncio.StreamWriter, payload: dict):
    encoded = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise RpcError("invalid_request", "RPC message exceeds its size limit")
    writer.write(struct.pack("!I", len(encoded)) + encoded)
    await writer.drain()


async def call_rpc(socket_path: Path | str, operation: str, payload: dict | None = None, *, timeout=15.0):
    async def exchange():
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        try:
            request_id = uuid.uuid4().hex
            await write_message(writer, {"id": request_id, "operation": operation, "payload": payload or {},
                                         "timeout_ms": int(timeout * 1000)})
            response = await read_message(reader)
            # Peer credentials can reject a caller before its envelope is read.
            denied_before_request = (response.get("id") is None and response.get("ok") is False
                                     and response.get("code") == "forbidden")
            if response.get("id") != request_id and not denied_before_request:
                raise RpcError("invalid_response", "RPC response identity does not match")
            if response.get("ok") is not True:
                raise RpcError(str(response.get("code") or "runtime_error"), str(response.get("error") or "Runtime rejected request"))
            result = response.get("result")
            if not isinstance(result, dict):
                raise RpcError("invalid_response", "Runtime response must contain an object result")
            return result
        finally:
            # Every connection carries exactly one request. On cancellation,
            # close()/wait_closed() may flush forever to a peer that never reads.
            writer.transport.abort()
    try:
        return await bounded(exchange(), timeout)
    except (ValueError, UnicodeError) as exc:
        raise RpcError("invalid_response", "Audio runtime returned malformed JSON") from exc
    except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError) as exc:
        raise RpcError("runtime_unavailable", "Audio runtime is unavailable or timed out") from exc


def peer_uid(writer) -> int:
    """Kernel credentials, never an identity claimed in the JSON envelope."""
    peer = writer.get_extra_info("socket")
    _, uid, _ = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    return uid


async def serve_rpc(socket_path: Path | str, handler: Callable[[str, dict], Awaitable[dict]], *,
                    mode=0o660, allowed_uids: set[int] | None = None, socket_gid: int | None = None,
                    operation_uids: dict[str, set[int]] | None = None):
    operation_uids = {key: frozenset(value) for key, value in (operation_uids or {}).items()}
    if any(not isinstance(key, str) or not key or len(key) > 64
           or any(type(uid) is not int or not 0 <= uid <= 2**32 - 1 for uid in value)
           for key, value in operation_uids.items()):
        raise ValueError("Invalid RPC operation credential policy")
    if operation_uids and not hasattr(socket, "SO_PEERCRED"):
        raise RuntimeError("RPC operation authorization requires Linux peer credentials")
    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        if not stat.S_ISSOCK(path.lstat().st_mode):
            raise RuntimeError(f"Refusing to replace a non-socket path: {path}")
        # Caller holds a singleton lock before replacing a stale socket.
        path.unlink()
    slots = asyncio.Semaphore(MAX_CONNECTIONS)
    tasks = set()
    state = {"closed": False}

    async def connection(reader, writer):
        current = asyncio.current_task()
        tasks.add(current)
        request_id = None
        deadline = asyncio.get_running_loop().time() + 5
        response_started = False
        response_complete = False

        async def reply(payload):
            nonlocal response_started, response_complete
            response_started = True
            remaining = max(0, deadline - asyncio.get_running_loop().time())
            await asyncio.wait_for(write_message(writer, payload), timeout=remaining)
            response_complete = True

        try:
            if state["closed"] or slots.locked():
                return
            async with slots:
                uid = None
                if allowed_uids is not None or operation_uids:
                    uid = peer_uid(writer)
                    if allowed_uids is not None and uid not in allowed_uids:
                        raise RpcError("forbidden", "This process cannot control the runtime")
                message = await asyncio.wait_for(read_message(reader), timeout=5)
                request_id = message.get("id")
                operation, payload = message.get("operation"), message.get("payload")
                if not isinstance(request_id, str) or len(request_id) > 128 or not isinstance(operation, str) or len(operation) > 64 or not isinstance(payload, dict):
                    raise RpcError("invalid_request", "Invalid RPC envelope")
                if operation in operation_uids and uid not in operation_uids[operation]:
                    raise RpcError("forbidden", "This process cannot perform this runtime operation")
                timeout_ms = message.get("timeout_ms", 15000)
                if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or not 100 <= timeout_ms <= 30000:
                    raise RpcError("invalid_request", "Invalid RPC deadline")
                deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
                operation_task = asyncio.create_task(handler(operation, payload))
                disconnect = asyncio.create_task(reader.read(1))
                try:
                    done, _ = await asyncio.wait({operation_task, disconnect}, timeout=timeout_ms / 1000,
                                                 return_when=asyncio.FIRST_COMPLETED)
                    if operation_task in done:
                        result = operation_task.result()
                    elif disconnect in done:
                        if disconnect.result():
                            raise RpcError("invalid_request", "One RPC request is allowed per connection")
                        return
                    else:
                        raise RpcError("deadline_exceeded", "Runtime request timed out")
                finally:
                    for pending in (operation_task, disconnect):
                        if not pending.done():
                            pending.cancel()
                    await asyncio.gather(operation_task, disconnect, return_exceptions=True)
                await reply({"id": request_id, "ok": True, "result": result})
        except (RpcError, ValueError, asyncio.TimeoutError) as exc:
            if not response_started:
                with contextlib.suppress(OSError, asyncio.TimeoutError):
                    await reply({"id": request_id, "ok": False, "code": getattr(exc, "code", "invalid_request"), "error": str(exc) or "Runtime request timed out"})
        except (OSError, asyncio.IncompleteReadError):
            pass
        except Exception:
            if not response_started:
                with contextlib.suppress(OSError, asyncio.TimeoutError):
                    await reply({"id": request_id, "ok": False, "code": "runtime_error", "error": "Runtime operation failed; inspect its service logs"})
        finally:
            try:
                if response_complete and not state["closed"]:
                    # drain() can return with a tail below its low watermark.
                    # Flush that tail for healthy readers, within the deadline.
                    writer.close()
                    try:
                        remaining = max(0, deadline - asyncio.get_running_loop().time())
                        await asyncio.wait_for(writer.wait_closed(), timeout=remaining)
                    except (OSError, asyncio.TimeoutError, asyncio.CancelledError):
                        writer.transport.abort()
                else:
                    writer.transport.abort()
            finally:
                tasks.discard(current)

    server = await asyncio.start_unix_server(connection, path=str(path))
    try:
        if socket_gid is not None:
            os.chown(path, -1, socket_gid)
        os.chmod(path, mode)
    except BaseException:
        server.close()
        await server.wait_closed()
        raise
    return RpcServer(server, tasks, state)
