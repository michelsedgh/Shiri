"""One admitted, bounded binary speech connection; no room discovery or speaker I/O.

The initial ordinary RPC authenticates and fixes room/session/stream identity.
Only sample continuity travels per frame. One acknowledged frame at a time
provides backpressure through both persistent local hops; the room owns pacing.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import struct
from uuid import uuid4
from anyio import CancelScope

from shiri.deadline import bounded
from shiri.rpc import AdmissionRefused, RpcError, read_message, write_message

PCM, FINISH, CANCEL, ACK, ENDED, ERROR = 1, 2, 3, 128, 129, 255
HEADER = struct.Struct("!BI")
POSITION = struct.Struct("!QQ")
RECEIPT = struct.Struct("!QQQQQ")
MAX_PCM_BYTES = 1920
MAX_FRAMES = 48000 * 60
MAX_RECORD = 4096
CLOCK = "room_audio_worker_monotonic; admission_not_acoustic"


async def read_packet(reader):
    kind, size = HEADER.unpack(await reader.readexactly(HEADER.size))
    if kind not in {PCM, FINISH, CANCEL, ACK, ENDED, ERROR} or not 0 <= size <= MAX_RECORD:
        raise RpcError("invalid_media", "Invalid speech stream record")
    if ((kind == PCM and not POSITION.size + 2 <= size <= POSITION.size + MAX_PCM_BYTES)
            or (kind == FINISH and size != POSITION.size) or (kind == CANCEL and size != 0)
            or (kind in {ACK, ENDED} and size != RECEIPT.size)):
        raise RpcError("invalid_media", "Invalid speech stream record size")
    return kind, await reader.readexactly(size)


async def write_packet(writer, kind, data=b""):
    if len(data) > MAX_RECORD:
        raise RpcError("invalid_media", "Speech stream record exceeds its bound")
    writer.write(HEADER.pack(kind, len(data)) + data)
    await writer.drain()


def encode_receipt(result):
    return RECEIPT.pack(result.get("next_sequence", 1) - 1,
                        result.get("admitted_frames", 0),
                        result.get("first_pcm_admitted_monotonic_ns") or 0,
                        result.get("first_audible_pcm_admitted_monotonic_ns") or 0,
                        result.get("last_pcm_admitted_monotonic_ns") or 0)


def decode_receipt(data, prepared, *, ended=False):
    sequence, frames, first, audible, last = RECEIPT.unpack(data)
    prepared_ns = prepared.get("pcm_prepared_monotonic_ns")
    return {"ok": True, "sequence": sequence, "next_sequence": sequence + 1,
            "next_frame_index": frames, "admitted_frames": frames,
            "stream_id": prepared["stream_id"], "pcm_clock": CLOCK,
            "first_pcm_admitted_monotonic_ns": first or None,
            "first_audible_pcm_admitted_monotonic_ns": audible or None,
            "last_pcm_admitted_monotonic_ns": last or None,
            "pcm_prepared_monotonic_ns": prepared_ns,
            "prepared_to_first_pcm_ms": (first - prepared_ns) / 1e6 if first and prepared_ns else None,
            **({"finished": True} if ended else {})}


async def serve_speech(reader, writer, session, *, validate=lambda: None):
    """Relay only one complete request at a time, with no detached media queue."""
    try:
        while True:
            kind, data = await asyncio.wait_for(read_packet(reader), timeout=10)
            validate()
            if kind == PCM:
                sequence, frame = POSITION.unpack_from(data)
                result = await session.send_pcm(data[POSITION.size:], sequence, frame)
            elif kind == FINISH:
                result = await session.finish(*POSITION.unpack(data))
            elif kind == CANCEL:
                result = await session.close()
            else:
                raise RpcError("invalid_media", "Speech stream expected media or an exact terminal command")
            await asyncio.wait_for(write_packet(writer, ACK if kind == PCM else ENDED,
                                                encode_receipt(result)), timeout=2)
            if kind != PCM:
                return
    except (RpcError, ValueError, asyncio.TimeoutError) as exc:
        failure = {"code": getattr(exc, "code", "invalid_media"),
                   "error": str(exc)[:512] or "Speech stream timed out"}
        # A rejected frame still has an exact owner. Carry its observed
        # retirement in the error so one malformed job need not fence the room
        # forever. A lost reply deliberately provides no such assurance.
        try:
            retired = await session.close()
            failure["retirement"] = encode_receipt(retired).hex()
        except (RpcError, OSError, asyncio.TimeoutError):
            pass
        message = json.dumps(failure).encode()
        with contextlib.suppress(OSError, asyncio.TimeoutError):
            await asyncio.wait_for(write_packet(writer, ERROR, message), timeout=1)
        raise


class SpeechStream:
    def __init__(self, reader, writer, prepared):
        self.reader, self.writer, self.prepared = reader, writer, prepared
        self._lock = asyncio.Lock()
        self._ended = None
        self._broken = False

    async def _exchange(self, kind, body):
        await write_packet(self.writer, kind, body)
        response, data = await read_packet(self.reader)
        if response == ERROR:
            failure = json.loads(data)
            if not isinstance(failure, dict):
                raise RpcError("invalid_response", "Speech stream returned an invalid error")
            retirement = failure.get("retirement")
            if retirement is not None:
                if not isinstance(retirement, str) or re.fullmatch(r"[0-9a-f]{80}", retirement) is None:
                    raise RpcError("invalid_response", "Speech stream returned an invalid retirement")
                self._ended = decode_receipt(bytes.fromhex(retirement), self.prepared)
                self.writer.transport.abort()
            raise RpcError(failure.get("code", "audio_unavailable"), failure.get("error", "Speech refused"))
        if response != (ACK if kind == PCM else ENDED):
            raise RpcError("invalid_response", "Speech stream response does not match its command")
        result = decode_receipt(data, self.prepared, ended=kind == FINISH)
        if kind != PCM:
            self._ended = result
            self.writer.transport.abort()
        return result

    async def _request(self, kind, body=b""):
        async with self._lock:
            if self._ended is not None:
                if kind == CANCEL:
                    return self._ended
                raise RpcError("session_conflict", "This speech stream has ended")
            if self._broken:
                raise RpcError("audio_unavailable", "Speech stream lost its retirement acknowledgement")
            task = asyncio.create_task(bounded(self._exchange(kind, body), 5))
            interrupted = False
            try:
                # Consume exactly this response before cancellation escapes.
                # close() can then explicitly retire the same stream without a
                # half-read ACK being confused with its cancellation receipt.
                with CancelScope(shield=True):
                    while True:
                        try:
                            result = await asyncio.shield(task)
                            break
                        except asyncio.CancelledError:
                            if task.cancelled():
                                raise
                            interrupted = True
            except BaseException as exc:
                self._broken = True
                self.writer.transport.abort()
                if isinstance(exc, (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError)):
                    raise RpcError("audio_unavailable", "Speech stream disconnected or timed out") from exc
                raise
            if interrupted:
                raise asyncio.CancelledError
            return result

    async def send_pcm(self, pcm, sequence, frame_index):
        if (not isinstance(pcm, bytes) or not 0 < len(pcm) <= MAX_PCM_BYTES or len(pcm) % 2
                or type(sequence) is not int or not 0 < sequence < 2**63
                or type(frame_index) is not int or not 0 <= frame_index <= MAX_FRAMES - len(pcm)//2):
            raise RpcError("invalid_media", "Speech requires bounded complete PCM and exact positions")
        result = await self._request(PCM, POSITION.pack(sequence, frame_index) + pcm)
        if result["next_sequence"] != sequence + 1 or result["admitted_frames"] != frame_index + len(pcm)//2:
            self._broken = True
            self.writer.transport.abort()
            raise RpcError("invalid_response", "Speech admission did not acknowledge exact frame continuity")
        return result

    async def finish(self, final_sequence, final_frame_index):
        if (type(final_sequence) is not int or not 0 <= final_sequence < 2**63
                or type(final_frame_index) is not int or not 0 <= final_frame_index <= MAX_FRAMES):
            raise RpcError("invalid_media", "Speech finish requires exact bounded positions")
        result = await self._request(FINISH, POSITION.pack(final_sequence, final_frame_index))
        if result["sequence"] != final_sequence or result["admitted_frames"] != final_frame_index:
            raise RpcError("invalid_response", "Speech finish did not acknowledge exact frame continuity")
        return result

    async def close(self):
        return await self._request(CANCEL)


async def open_speech(socket_path, payload):
    """Authenticate once using the existing private RPC endpoint and policy."""
    writer = None
    try:
        async def admit():
            nonlocal writer
            reader, writer = await asyncio.open_unix_connection(str(socket_path), limit=8192)
            identity = uuid4().hex
            await write_message(writer, {"id": identity, "operation": "speech-stream", "payload": payload,
                                         "timeout_ms": 20000})
            reply = await read_message(reader)
            denied_before_request = (reply.get("id") is None and reply.get("ok") is False
                                     and reply.get("code") == "forbidden")
            if reply.get("id") != identity and not denied_before_request:
                raise RpcError("invalid_response", "Speech admission response identity does not match")
            if reply.get("ok") is not True:
                refusal = AdmissionRefused if reply.get("admission_refused") is True else RpcError
                raise refusal(reply.get("code", "audio_unavailable"), reply.get("error", "Speech admission refused"))
            prepared = reply.get("result")
            if (not isinstance(prepared, dict)
                    or prepared.get("ok") is not True
                    or prepared.get("format") != "s16le" or prepared.get("sample_rate") != 48000
                    or prepared.get("channels") != 1 or prepared.get("max_samples") != 960
                    or not isinstance(prepared.get("stream_id"), str)
                    or re.fullmatch(r"[0-9a-f]{32}", prepared["stream_id"]) is None
                    or prepared.get("session_id") != payload.get("session_id")
                    or prepared.get("request_id") != payload.get("request_id")):
                raise RpcError("invalid_response", "Speech admission returned an invalid identity or format")
            return SpeechStream(reader, writer, prepared)
        return await bounded(admit(), 20)
    except BaseException as exc:
        if writer is not None:
            writer.transport.abort()
        if isinstance(exc, (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError)):
            raise RpcError("audio_unavailable", "Speech admission disconnected or timed out") from exc
        if isinstance(exc, (ValueError, UnicodeError)):
            raise RpcError("invalid_response", "Speech admission returned malformed JSON") from exc
        raise
