"""
WebRTC TTS signaling proxy for Shiri.

The Flask app forwards zone-addressed SDP/control messages to the zone mixer
over a private Unix socket. The mixer owns WebRTC termination and feeds decoded
audio into its GStreamer audiomixer path.
"""

from __future__ import annotations

import json
import logging
import socket
from typing import Any


log = logging.getLogger("shiri.tts_webrtc")
SOCKET_TIMEOUT_SECONDS = 16.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class TtsWebRtcService:
    def __init__(self, zone_manager):
        self.zone_manager = zone_manager

    def start(self):
        log.info("TTS WebRTC signaling proxy ready")

    def stop(self):
        return

    def submit_offer(self, zone_id, payload):
        payload = payload if isinstance(payload, dict) else {}
        result, error = self.zone_manager.prepare_tts_webrtc(
            zone_id,
            request_id=payload.get("request_id"),
            session_id=payload.get("session_id"),
            text=payload.get("text"),
            speaker_id=payload.get("speaker_id"),
            speaker_name=payload.get("speaker_name"),
        )
        if error:
            raise ValueError(error)

        mixer_payload = dict(payload)
        mixer_payload.setdefault("action", "offer")
        mixer_payload["request_id"] = result["request_id"]
        mixer_payload["session_id"] = result["session_id"]
        mixer_payload["duck_gain"] = result["duck_gain"]

        response = _send_mixer_request(result["tts_webrtc_socket"], mixer_payload)
        if not response.get("ok", False):
            raise RuntimeError(str(response.get("error") or "Mixer rejected WebRTC TTS request."))
        response.update({
            "zone_id": result["zone_id"],
            "lionos_room_id": result["lionos_room_id"],
            "lionos_room_name": result["lionos_room_name"],
            "effective_policy": result["effective_policy"],
            "public_transport": "webrtc",
            "internal_audio_target": response.get("internal_audio_target") or "gstreamer-audiomixer",
        })
        return response


def _send_mixer_request(socket_path: str, payload: dict[str, Any]) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(SOCKET_TIMEOUT_SECONDS)
        client.connect(socket_path)
        client.sendall(json.dumps(payload).encode("utf-8"))
        client.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise RuntimeError("Mixer WebRTC response is too large")
    raw = b"".join(chunks).strip()
    if not raw:
        raise RuntimeError("Mixer WebRTC response was empty")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("Mixer WebRTC response must be an object")
    return data
