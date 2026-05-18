#!/usr/bin/env python3
"""
WebSocket PCM ingress for TTS.

LionOS sends decoded PCM over a persistent WebSocket session. Shiri writes that
PCM into the selected zone's TTS FIFO, and the zone mixer consumes it directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import logging
import os
import threading
import time
from urllib.parse import unquote, urlsplit


DEFAULT_TTS_PCM_WS_PORT = 8091
DEFAULT_START_PREBUFFER_MS = 360
DEFAULT_LEAD_IN_SILENCE_MS = 160
DEFAULT_END_SILENCE_MS = 260
DEFAULT_FINAL_FADE_MS = 45
PCM_FIFO_WRITE_FRAME_MS = 20


log = logging.getLogger("shiri.tts_pcm_ws")


class TtsPcmWebSocketServer:
    def __init__(self, zone_manager, *, host="0.0.0.0", port=DEFAULT_TTS_PCM_WS_PORT):
        self.zone_manager = zone_manager
        self.host = host
        self.port = int(port)
        self._thread = None
        self._loop = None
        self._stop_event = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_thread, name="tts-pcm-ws", daemon=True)
        self._thread.start()

    def stop(self):
        if self._loop is not None and self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)

    def _run_thread(self):
        try:
            asyncio.run(self._run())
        except Exception:
            log.exception("TTS PCM WebSocket server crashed")

    async def _run(self):
        import websockets

        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        async with websockets.serve(
            self._handle,
            self.host,
            self.port,
            max_size=None,
            ping_interval=20,
            ping_timeout=20,
        ):
            log.info("TTS PCM WebSocket listening on %s:%d", self.host, self.port)
            await self._stop_event.wait()
            log.info("TTS PCM WebSocket stopping")

    async def _handle(self, websocket, path=None):
        session = None
        try:
            route = _parse_route(_connection_path(websocket, path))
            first = await asyncio.wait_for(websocket.recv(), timeout=10)
            if isinstance(first, bytes):
                raise ValueError("first TTS PCM WebSocket message must be JSON")
            start = _json_object(first)
            if start.get("type", "start") != "start":
                raise ValueError("first TTS PCM WebSocket message must be type=start")

            result, error = self._prepare_tts(route, start)
            if error:
                await websocket.send(json.dumps({"type": "error", "ok": False, "error": error}))
                return

            session = TtsPcmPipeSession(
                request_id=result["request_id"],
                pipe_path=result["tts_pcm_pipe"],
                sample_rate=result["sample_rate"],
                channels=result["channels"],
                sample_width=result["sample_width"],
                start_prebuffer_ms=_int_range(
                    start.get("start_prebuffer_ms"),
                    DEFAULT_START_PREBUFFER_MS,
                    0,
                    1500,
                ),
                lead_in_ms=_int_range(start.get("lead_in_ms"), DEFAULT_LEAD_IN_SILENCE_MS, 0, 1500),
                end_silence_ms=_int_range(
                    start.get("end_silence_ms"),
                    DEFAULT_END_SILENCE_MS,
                    0,
                    2000,
                ),
                final_fade_ms=_int_range(start.get("final_fade_ms"), DEFAULT_FINAL_FADE_MS, 0, 500),
            )
            session.open()
            await websocket.send(
                json.dumps(
                    {
                        "type": "started",
                        "ok": True,
                        "transport": "ws_pcm_s16le",
                        "request_id": result["request_id"],
                        "room_id": result["room_id"],
                        "room_name": result["room_name"],
                        "zone_id": result["zone_id"],
                        "sample_rate": result["sample_rate"],
                        "channels": result["channels"],
                        "sample_width": result["sample_width"],
                        "format": "pcm_s16le",
                        "codec": "pcm_s16le",
                        "tts_pcm_pipe": result["tts_pcm_pipe"],
                        "start_prebuffer_ms": session.start_prebuffer_ms,
                        "lead_in_ms": session.lead_in_ms,
                        "end_silence_ms": session.end_silence_ms,
                        "final_fade_ms": session.final_fade_ms,
                        "fifo_write_frame_ms": PCM_FIFO_WRITE_FRAME_MS,
                        "effective_policy": result["effective_policy"],
                    }
                )
            )

            async for message in websocket:
                if isinstance(message, bytes):
                    await asyncio.to_thread(session.push, bytes(message))
                    continue
                payload = _json_object(message)
                event_type = payload.get("type")
                if event_type == "end":
                    break
                if event_type == "cancel":
                    await asyncio.to_thread(session.cancel)
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "finished",
                                "ok": False,
                                "request_id": result["request_id"],
                                "cancelled": True,
                            }
                        )
                    )
                    return
                if event_type == "ping":
                    await websocket.send(json.dumps({"type": "pong", "request_id": result["request_id"]}))

            stats = await asyncio.to_thread(session.finish)
            await websocket.send(
                json.dumps(
                    {
                        "type": "finished",
                        "ok": True,
                        "transport": "ws_pcm_s16le",
                        "request_id": result["request_id"],
                        **stats,
                    }
                )
            )
        except Exception as exc:
            log.warning("TTS PCM WebSocket session failed: %s", exc)
            with contextlib.suppress(Exception):
                await websocket.send(json.dumps({"type": "error", "ok": False, "error": str(exc)}))
            if session is not None:
                await asyncio.to_thread(session.cancel)

    def _prepare_tts(self, route, start):
        sample_rate = _int_range(start.get("sample_rate"), 24000, 1, 192000)
        channels = _int_range(start.get("channels"), 1, 1, 8)
        sample_width = _int_range(start.get("sample_width"), 2, 1, 4)
        request_id = str(start.get("request_id") or "").strip() or f"tts_pcm_{int(time.time() * 1000)}"

        if route["kind"] == "room":
            zone, error = self.zone_manager.resolve_zone_for_room(route["id"])
            if error:
                return None, error
            zone_id = zone.zone_id
        else:
            zone_id = route["id"]

        return self.zone_manager.prepare_tts_pcm(
            zone_id,
            request_id=request_id,
            audio_format="pcm_s16le",
            sample_rate=sample_rate,
            channels=channels,
            sample_width=sample_width,
            text=start.get("text"),
            speaker_id=start.get("speaker_id"),
            speaker_name=start.get("speaker_name"),
        )


class TtsPcmPipeSession:
    def __init__(
        self,
        *,
        request_id,
        pipe_path,
        sample_rate,
        channels,
        sample_width,
        start_prebuffer_ms=DEFAULT_START_PREBUFFER_MS,
        lead_in_ms=DEFAULT_LEAD_IN_SILENCE_MS,
        end_silence_ms=DEFAULT_END_SILENCE_MS,
        final_fade_ms=DEFAULT_FINAL_FADE_MS,
    ):
        self.request_id = request_id
        self.pipe_path = str(pipe_path)
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.sample_width = int(sample_width)
        self.start_prebuffer_ms = int(start_prebuffer_ms)
        self.lead_in_ms = int(lead_in_ms)
        self.end_silence_ms = int(end_silence_ms)
        self.final_fade_ms = int(final_fade_ms)
        self._fd = None
        self._started = False
        self._cancelled = False
        self._held_chunk = None
        self._start_buffer = []
        self._start_buffered_bytes = 0
        self._pace_next_at = None
        self._started_at = time.time()
        self._stats = {
            "audio_chunk_count": 0,
            "audio_bytes_received": 0,
            "audio_bytes_pushed": 0,
            "lead_in_bytes": 0,
            "tail_bytes": 0,
            "fifo_write_frame_ms": PCM_FIFO_WRITE_FRAME_MS,
            "started_at": self._started_at,
        }

    def open(self):
        flags = os.O_WRONLY | os.O_NONBLOCK
        try:
            fd = os.open(self.pipe_path, flags)
        except OSError as exc:
            if exc.errno == errno.ENXIO:
                raise RuntimeError(f"TTS PCM mixer pipe is not ready: {self.pipe_path}") from exc
            raise
        current = os.get_blocking(fd)
        if not current:
            os.set_blocking(fd, True)
        self._fd = fd

    def push(self, chunk):
        if not chunk or self._cancelled:
            return
        if self._fd is None:
            raise RuntimeError("TTS PCM pipe session is not open")
        self._stats["audio_chunk_count"] += 1
        self._stats["audio_bytes_received"] += len(chunk)
        if "first_input_ms" not in self._stats:
            self._stats["first_input_ms"] = _elapsed_ms(self._started_at)

        if not self._started:
            self._start_buffer.append(chunk)
            self._start_buffered_bytes += len(chunk)
            if self._start_buffered_bytes < _pcm_bytes_for_ms(self, self.start_prebuffer_ms):
                return
            self._start_pipe()
            return

        self._emit_audio_chunk(chunk)

    def finish(self):
        if self._fd is None:
            return dict(self._stats)
        try:
            if not self._cancelled:
                if not self._started and self._start_buffered_bytes > 0:
                    self._start_pipe()
                if self._held_chunk:
                    self._write(_fade_out_pcm_s16le(self, self._held_chunk, self.final_fade_ms))
                    self._held_chunk = None
                tail = _silence_pcm_s16le(self, self.end_silence_ms)
                if tail:
                    self._stats["tail_bytes"] = len(tail)
                    self._write(tail)
            self._stats["finished_ms"] = _elapsed_ms(self._started_at)
            return dict(self._stats)
        finally:
            self._close()

    def cancel(self):
        self._cancelled = True
        self._close()

    def _start_pipe(self):
        self._started = True
        self._stats["start_prebuffer_bytes"] = self._start_buffered_bytes
        lead = _silence_pcm_s16le(self, self.lead_in_ms)
        if lead:
            self._stats["lead_in_bytes"] = len(lead)
            self._write(lead)
        for buffered in self._start_buffer:
            self._emit_audio_chunk(buffered)
        self._start_buffer.clear()
        self._start_buffered_bytes = 0

    def _emit_audio_chunk(self, chunk):
        if self._held_chunk is not None:
            self._write(self._held_chunk)
        self._held_chunk = chunk

    def _write(self, chunk):
        if not chunk or self._fd is None:
            return
        frame_width = max(1, self.channels * self.sample_width)
        bytes_per_second = max(frame_width, self.sample_rate * frame_width)
        frame_bytes = max(frame_width, int(bytes_per_second * PCM_FIFO_WRITE_FRAME_MS / 1000))
        frame_bytes -= frame_bytes % frame_width
        if frame_bytes <= 0:
            frame_bytes = frame_width

        view = memoryview(chunk)
        total = 0
        while total < len(view):
            end = min(total + frame_bytes, len(view))
            written = 0
            frame = view[total:end]
            while written < len(frame):
                written += os.write(self._fd, frame[written:])
            total = end
            self._pace_after_write(len(frame), bytes_per_second)
        self._stats["audio_bytes_pushed"] += len(chunk)
        if "first_audio_push_ms" not in self._stats:
            self._stats["first_audio_push_ms"] = _elapsed_ms(self._started_at)

    def _pace_after_write(self, byte_count, bytes_per_second):
        duration = max(0.0, float(byte_count) / float(bytes_per_second))
        if duration <= 0:
            return
        now = time.monotonic()
        if self._pace_next_at is None or self._pace_next_at < now - 0.25:
            self._pace_next_at = now
        self._pace_next_at += duration
        delay = self._pace_next_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        elif delay < -0.25:
            self._pace_next_at = time.monotonic()

    def _close(self):
        if self._fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self._fd = None


def _connection_path(websocket, path):
    if path:
        return path
    request = getattr(websocket, "request", None)
    if request is not None and getattr(request, "path", None):
        return request.path
    legacy_path = getattr(websocket, "path", None)
    return legacy_path or "/"


def _parse_route(raw_path):
    path = urlsplit(raw_path).path.rstrip("/")
    parts = [unquote(part) for part in path.split("/") if part]
    if len(parts) == 4 and parts[0] == "tts" and parts[1] in {"rooms", "zones"} and parts[3] == "pcm":
        return {"kind": "room" if parts[1] == "rooms" else "zone", "id": parts[2]}
    raise ValueError("expected /tts/rooms/<room_id>/pcm or /tts/zones/<zone_id>/pcm")


def _json_object(raw):
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid JSON message") from exc
    if not isinstance(payload, dict):
        raise ValueError("JSON message must be an object")
    return payload


def _int_range(value, default, minimum, maximum):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(int(minimum), min(int(maximum), parsed))


def _pcm_bytes_for_ms(target, duration_ms):
    frame_width = target.sample_width * target.channels
    frames = int(target.sample_rate * max(0, int(duration_ms)) / 1000)
    return max(0, frames * frame_width)


def _silence_pcm_s16le(target, duration_ms):
    return b"\x00" * _pcm_bytes_for_ms(target, duration_ms)


def _fade_out_pcm_s16le(target, chunk, fade_ms):
    if not chunk or target.sample_width != 2 or target.channels <= 0:
        return chunk
    frame_width = target.sample_width * target.channels
    audio_len = len(chunk) - (len(chunk) % frame_width)
    if audio_len <= 0:
        return chunk
    frames = audio_len // frame_width
    fade_frames = min(frames, int(target.sample_rate * max(0, int(fade_ms)) / 1000))
    if fade_frames <= 1:
        return chunk

    faded = bytearray(chunk)
    fade_start = frames - fade_frames
    for frame_index in range(fade_start, frames):
        gain = (frames - frame_index - 1) / fade_frames
        frame_offset = frame_index * frame_width
        for channel in range(target.channels):
            offset = frame_offset + channel * target.sample_width
            sample = int.from_bytes(faded[offset : offset + 2], "little", signed=True)
            faded_sample = int(sample * gain)
            faded[offset : offset + 2] = faded_sample.to_bytes(2, "little", signed=True)
    return bytes(faded)


def _elapsed_ms(started_at):
    return int((time.time() - started_at) * 1000)
