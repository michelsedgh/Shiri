#!/usr/bin/env python3
"""
app.py — Main Shiri daemon.

Serves the web UI and REST API on port 8080.
Orchestrates zone lifecycle using the existing shell scripts.

Run with: sudo python3 app.py
"""

import logging
import os
import json
import signal
import sys
import threading
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from flask_socketio import SocketIO

from config import ConfigStore
from zone import ZoneManager, _slugify_room_id

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("shiri")

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app = Flask(__name__, static_folder=STATIC_DIR)
app.config["SECRET_KEY"] = "shiri-secret-key"
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------
config_store = ConfigStore()
zone_manager = ZoneManager(config_store, socketio)

# ---------------------------------------------------------------------------
# Log streaming — single thread tails all watched zones
# ---------------------------------------------------------------------------
LOG_DIR = "/var/lib/shiri/groups"
LOG_TYPES = ["shairport", "owntone", "owntone_wrapper", "mixer", "pause_bridge", "volume_bridge", "sync_reset"]
LOG_FILTERS = {
    "all": LOG_TYPES,
    "tts": ["mixer"],
    "speaker": ["owntone", "owntone_wrapper"],
    "volume": ["volume_bridge", "owntone"],
    "airplay": ["shairport"],
    "owntone": ["owntone", "owntone_wrapper"],
    "errors": LOG_TYPES,
}
DEFAULT_LIONOS_BASE_URL = os.environ.get("SHIRI_LIONOS_BASE_URL", "http://192.168.1.198:7001")
LIONOS_TIMEOUT_SECONDS = float(os.environ.get("SHIRI_LIONOS_TIMEOUT", "1.5"))
_watched_zones = set()
_log_positions = {}
_log_lock = threading.Lock()
_log_stop = threading.Event()


def _read_log_tail(zone_id, log_type, lines=100):
    path = os.path.join(LOG_DIR, zone_id, "logs", f"{log_type}.log")
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", errors="replace") as f:
            return [l.rstrip("\n") for l in f.readlines()[-lines:]]
    except IOError:
        return []


def _log_watcher_loop():
    while not _log_stop.is_set():
        with _log_lock:
            zones = set(_watched_zones)
        for zone_id in zones:
            logs_dir = os.path.join(LOG_DIR, zone_id, "logs")
            if not os.path.isdir(logs_dir):
                continue
            for log_type in LOG_TYPES:
                path = os.path.join(logs_dir, f"{log_type}.log")
                if not os.path.exists(path):
                    continue
                try:
                    size = os.path.getsize(path)
                    pos = _log_positions.get(path, 0)
                    if size < pos:
                        pos = 0
                    if size > pos:
                        with open(path, "r", errors="replace") as f:
                            f.seek(pos)
                            new_data = f.read()
                            _log_positions[path] = f.tell()
                        zone = zone_manager.get_zone(zone_id)
                        for line in new_data.splitlines():
                            if line.strip():
                                socketio.emit("zone_log", {
                                    "zone_id": zone_id,
                                    "zone_name": zone.display_name if zone else zone_id,
                                    "room_id": zone.room_id if zone else None,
                                    "room_name": zone.room_name if zone else None,
                                    "log_type": log_type,
                                    "category": _log_category(log_type, line),
                                    "severity": _log_severity(line),
                                    "line": line,
                                    "timestamp": time.time(),
                                })
                except IOError:
                    pass
        _log_stop.wait(0.5)


def start_log_watch(zone_id):
    with _log_lock:
        _watched_zones.add(zone_id)


def stop_log_watch(zone_id):
    with _log_lock:
        _watched_zones.discard(zone_id)

# ---------------------------------------------------------------------------
# LionOS state + dashboard aggregation
# ---------------------------------------------------------------------------

def _settings():
    settings = config_store.get_settings()
    settings.setdefault("default_interface", "")
    settings.setdefault("lionos_base_url", DEFAULT_LIONOS_BASE_URL)
    return settings


def _public_settings(settings=None):
    settings = settings or _settings()
    return {
        "default_interface": settings.get("default_interface", ""),
        "lionos_base_url": settings.get("lionos_base_url") or DEFAULT_LIONOS_BASE_URL,
    }


def _fetch_lionos_state():
    settings = _settings()
    base_url = (settings.get("lionos_base_url") or DEFAULT_LIONOS_BASE_URL).rstrip("/")
    url = urljoin(f"{base_url}/", "api/state")
    started_at = time.time()
    try:
        req = Request(url, headers={"Accept": "application/json", "User-Agent": "Shiri/room-console"})
        with urlopen(req, timeout=LIONOS_TIMEOUT_SECONDS) as resp:
            body = resp.read()
        state = json.loads(body.decode("utf-8"))
        fetched_at = time.time()
        cache = {
            "base_url": base_url,
            "fetched_at": fetched_at,
            "state": state,
        }
        try:
            config_store.update_settings({"lionos_state_cache": cache})
        except OSError as exc:
            log.debug("Could not persist LionOS state cache: %s", exc)
        return {
            "online": True,
            "base_url": base_url,
            "state": state,
            "cached": False,
            "fetched_at": fetched_at,
            "latency_ms": round((fetched_at - started_at) * 1000),
            "error": None,
        }
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        cache = settings.get("lionos_state_cache")
        cached_state = cache.get("state") if isinstance(cache, dict) else None
        return {
            "online": False,
            "base_url": base_url,
            "state": cached_state if isinstance(cached_state, dict) else None,
            "cached": isinstance(cached_state, dict),
            "fetched_at": cache.get("fetched_at") if isinstance(cache, dict) else None,
            "latency_ms": None,
            "error": str(exc),
        }


def _member_names(state):
    members = state.get("members", []) if isinstance(state, dict) else []
    names = {}
    for member in members if isinstance(members, list) else []:
        if isinstance(member, dict) and member.get("id"):
            names[str(member["id"])] = member.get("name") or str(member["id"])
    return names


def _zone_bound_to_room(room_id):
    normalized = _slugify_room_id(room_id)
    for zone in zone_manager.list_zones():
        if _slugify_room_id(zone.room_id) == normalized:
            return zone
    return None


def _zone_summary(zone):
    speakers = []
    try:
        speakers = zone_manager._known_speakers(zone)
    except Exception as exc:
        log.debug("Could not summarize speakers for %s: %s", zone.zone_id, exc)

    volume = None
    volume_error = None
    player = None
    player_error = None
    if zone.status == zone.STATUS_RUNNING:
        volume, volume_error = zone_manager.get_volume(zone.zone_id)
        player, player_error = zone_manager.get_player_status(zone.zone_id)

    policy = zone_manager.get_tts_policy(zone.zone_id)[0] or {}
    return {
        "zone_id": zone.zone_id,
        "zone_name": zone.display_name,
        "status": zone.status,
        "error_message": zone.error_message,
        "room_id": zone.room_id,
        "room_name": zone.room_name,
        "default_room": bool(zone.config.get("default_room", False)),
        "interface": zone.interface,
        "auto_start": bool(zone.config.get("auto_start", False)),
        "latency_offset": zone.config.get("latency_offset"),
        "shairport_ip": zone.shairport_ip,
        "shairport_port": zone.shairport_port,
        "owntone_ip": zone.owntone_ip,
        "owntone_port": zone.owntone_port,
        "allocated_subdevice": zone.allocated_subdevice,
        "volume": volume,
        "volume_error": volume_error,
        "player": player or {},
        "player_error": player_error,
        "speakers": speakers,
        "tts_policy": policy.get("policy"),
        "tts_effective": policy.get("effective"),
        "can_start": zone.status in {zone.STATUS_STOPPED, zone.STATUS_ERROR},
        "can_stop": zone.status in {zone.STATUS_RUNNING, zone.STATUS_STARTING},
    }


def _room_activity(state, room_id):
    if not isinstance(state, dict):
        return {
            "presence": [],
            "presence_names": [],
            "primary_member_id": None,
            "activities": [],
            "devices": [],
            "agent_sessions": [],
        }
    names = _member_names(state)
    presence_by_room = state.get("presence_by_room") if isinstance(state.get("presence_by_room"), dict) else {}
    primary_by_room = state.get("primary_by_room") if isinstance(state.get("primary_by_room"), dict) else {}
    presence = [str(item) for item in presence_by_room.get(room_id, [])]
    activities = [
        item for item in state.get("activities", [])
        if isinstance(item, dict) and item.get("room_id") == room_id
    ]
    devices = [
        item for item in state.get("devices", [])
        if isinstance(item, dict) and item.get("room_id") == room_id
    ]
    sessions = [
        item for item in state.get("agent_sessions", [])
        if isinstance(item, dict) and item.get("room_id") == room_id
    ]
    return {
        "presence": presence,
        "presence_names": [names.get(member_id, member_id) for member_id in presence],
        "primary_member_id": primary_by_room.get(room_id),
        "activities": activities,
        "devices": devices,
        "agent_sessions": sessions,
    }


def _room_entry(room_id, room_name, *, source, lionos_room=None, lionos_state=None):
    zone = _zone_bound_to_room(room_id)
    activity = _room_activity(lionos_state, room_id)
    return {
        "room_id": room_id,
        "room_name": room_name,
        "source": source,
        "lionos_room": lionos_room or None,
        "presence": activity["presence"],
        "presence_names": activity["presence_names"],
        "primary_member_id": activity["primary_member_id"],
        "activities": activity["activities"],
        "devices": activity["devices"],
        "agent_sessions": activity["agent_sessions"],
        "binding": _zone_summary(zone) if zone else None,
        "bound": zone is not None,
    }


def _dashboard_payload():
    lionos = _fetch_lionos_state()
    lionos_state = lionos.get("state") or {}
    lionos_rooms = lionos_state.get("rooms", []) if isinstance(lionos_state, dict) else []
    rooms = []
    seen_room_ids = set()

    if isinstance(lionos_rooms, list):
        for room in lionos_rooms:
            if not isinstance(room, dict) or not room.get("id"):
                continue
            room_id = str(room["id"])
            seen_room_ids.add(_slugify_room_id(room_id))
            rooms.append(_room_entry(
                room_id,
                room.get("name") or room_id,
                source="lionos",
                lionos_room=room,
                lionos_state=lionos_state,
            ))

    for zone in zone_manager.list_zones():
        normalized = _slugify_room_id(zone.room_id)
        if normalized in seen_room_ids:
            continue
        seen_room_ids.add(normalized)
        rooms.append(_room_entry(
            zone.room_id,
            zone.room_name,
            source="shiri",
            lionos_room=None,
            lionos_state=lionos_state,
        ))

    rooms.sort(key=lambda item: (item["source"] != "lionos", item["room_name"].lower()))
    zones = [_zone_summary(zone) for zone in zone_manager.list_zones()]
    default_room = next((room for room in rooms if (room.get("binding") or {}).get("default_room")), None)
    return {
        "system": zone_manager.get_system_status(),
        "lionos": {k: v for k, v in lionos.items() if k != "state"},
        "settings": _public_settings(),
        "rooms": rooms,
        "zones": zones,
        "default_room_id": default_room.get("room_id") if default_room else None,
        "log_types": LOG_TYPES,
        "log_filters": sorted(LOG_FILTERS.keys()),
        "generated_at": time.time(),
    }


def _parse_volume(value):
    try:
        parsed = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return min(max(parsed, 0), 100)


def _log_severity(line):
    text = line.lower()
    if any(token in text for token in ("error", "failed", "failure", "died", "exception", "fatal")):
        return "error"
    if any(token in text for token in ("warn", "retry", "timeout", "closed")):
        return "warning"
    return "info"


def _log_category(log_type, line):
    if log_type in {"mixer", "arecord"} or "tts" in line.lower():
        return "tts"
    if log_type == "shairport":
        return "airplay"
    if log_type in {"owntone", "owntone_wrapper"}:
        return "owntone"
    if log_type == "volume_bridge":
        return "volume"
    return "system"


def _logs_for_query(zone_id=None, room_id=None, log_filter="all", lines=200):
    log_filter = (log_filter or "all").lower()
    selected_types = LOG_FILTERS.get(log_filter, LOG_TYPES if log_filter == "all" else [log_filter])
    if selected_types == ["all"]:
        selected_types = LOG_TYPES

    zones = []
    if zone_id:
        zone = zone_manager.get_zone(zone_id)
        if zone:
            zones = [zone]
    elif room_id:
        zone = _zone_bound_to_room(room_id)
        if zone:
            zones = [zone]
    else:
        zones = zone_manager.list_zones()

    entries = []
    for zone in zones:
        for log_type in selected_types:
            if log_type not in LOG_TYPES:
                continue
            for line in _read_log_tail(zone.zone_id, log_type, lines):
                severity = _log_severity(line)
                if log_filter == "errors" and severity == "info":
                    continue
                entries.append({
                    "zone_id": zone.zone_id,
                    "zone_name": zone.display_name,
                    "room_id": zone.room_id,
                    "room_name": zone.room_name,
                    "log_type": log_type,
                    "category": _log_category(log_type, line),
                    "severity": severity,
                    "line": line,
                    "timestamp": None,
                })
    return entries[-lines:]


threading.Thread(target=_log_watcher_loop, daemon=True, name="log-watcher").start()

# ---------------------------------------------------------------------------
# Static file serving
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")

@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(STATIC_DIR, path)

# ---------------------------------------------------------------------------
# System API
# ---------------------------------------------------------------------------

@app.route("/api/system/status")
def system_status():
    return jsonify(zone_manager.get_system_status())

@app.route("/api/system/interfaces")
def system_interfaces():
    return jsonify({"interfaces": zone_manager.get_network_interfaces()})

@app.route("/api/settings", methods=["GET"])
def get_settings():
    return jsonify({"settings": _public_settings()})

@app.route("/api/settings", methods=["PUT"])
def update_settings():
    data = request.get_json() or {}
    updates = {}
    if "lionos_base_url" in data:
        base_url = str(data.get("lionos_base_url") or "").strip().rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            return jsonify({"error": "lionos_base_url must start with http:// or https://"}), 400
        updates["lionos_base_url"] = base_url
    if "default_interface" in data:
        updates["default_interface"] = str(data.get("default_interface") or "").strip()
    if updates:
        config_store.update_settings(updates)
    return jsonify({"settings": _public_settings()})

@app.route("/api/lionos/status")
def lionos_status():
    state = _fetch_lionos_state()
    return jsonify({k: v for k, v in state.items() if k != "state"})

@app.route("/api/lionos/state")
def lionos_state():
    state = _fetch_lionos_state()
    status = {k: v for k, v in state.items() if k != "state"}
    return jsonify({"lionos": status, "state": state.get("state")})

@app.route("/api/dashboard")
def dashboard():
    return jsonify(_dashboard_payload())

# ---------------------------------------------------------------------------
# Zone CRUD API
# ---------------------------------------------------------------------------

@app.route("/api/zones")
def list_zones():
    zones = zone_manager.list_zones()
    return jsonify({"zones": [z.to_dict() for z in zones]})

@app.route("/api/zones", methods=["POST"])
def create_zone():
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    interface = data.get("interface", "").strip()
    latency_offset = data.get("latency_offset")

    if not name:
        return jsonify({"error": "Zone name is required"}), 400
    if not interface:
        return jsonify({"error": "Network interface is required"}), 400
    if latency_offset is not None:
        try:
            latency_offset = float(latency_offset)
        except (ValueError, TypeError):
            return jsonify({"error": "latency_offset must be a number"}), 400
        if latency_offset < -10 or latency_offset > 5:
            return jsonify({"error": "latency_offset should be between -10 and +5 seconds"}), 400

    zone = zone_manager.create_zone(
        name=name,
        interface=interface,
        auto_start=data.get("auto_start", False),
        latency_offset=latency_offset,
        room_id=data.get("room_id"),
        room_name=data.get("room_name"),
        default_room=bool(data.get("default_room", False)),
    )
    return jsonify(zone.to_dict()), 201

@app.route("/api/zones/<zone_id>")
def get_zone(zone_id):
    zone = zone_manager.get_zone(zone_id)
    if not zone:
        return jsonify({"error": "Zone not found"}), 404
    return jsonify(zone.to_dict())

@app.route("/api/zones/<zone_id>", methods=["PUT"])
def update_zone(zone_id):
    data = request.get_json() or {}
    # Allow updating running zones - they will be restarted automatically
    zone, restarted = zone_manager.update_zone_config(zone_id, data, restart_if_running=True)
    if not zone:
        return jsonify({"error": "Zone not found"}), 400
    result = zone.to_dict()
    result["restarted"] = restarted
    return jsonify(result)

@app.route("/api/zones/<zone_id>", methods=["DELETE"])
def delete_zone(zone_id):
    if zone_manager.delete_zone(zone_id):
        stop_log_watch(zone_id)
        return jsonify({"ok": True})
    return jsonify({"error": "Zone not found"}), 404

# ---------------------------------------------------------------------------
# Zone lifecycle API
# ---------------------------------------------------------------------------

@app.route("/api/zones/<zone_id>/start", methods=["POST"])
def start_zone(zone_id):
    if zone_manager.start_zone(zone_id):
        start_log_watch(zone_id)
        return jsonify({"ok": True})
    return jsonify({"error": "Cannot start zone (not found or already running)"}), 400

@app.route("/api/zones/<zone_id>/stop", methods=["POST"])
def stop_zone(zone_id):
    if zone_manager.stop_zone(zone_id):
        stop_log_watch(zone_id)
        return jsonify({"ok": True})
    return jsonify({"error": "Cannot stop zone (not found or not running)"}), 400

# ---------------------------------------------------------------------------
# Room routing API
# ---------------------------------------------------------------------------

@app.route("/api/rooms")
def list_rooms():
    return jsonify({"rooms": zone_manager.list_rooms()})

@app.route("/api/rooms/<room_id>/binding", methods=["PUT"])
def set_room_binding(room_id):
    data = request.get_json() or {}
    zone_id = (data.get("zone_id") or "").strip()
    if not zone_id:
        return jsonify({"error": "zone_id is required"}), 400
    room_name = data.get("room_name")
    if not room_name:
        lionos = _fetch_lionos_state()
        state = lionos.get("state") or {}
        rooms = state.get("rooms", []) if isinstance(state, dict) else []
        for room in rooms if isinstance(rooms, list) else []:
            if isinstance(room, dict) and room.get("id") == room_id:
                room_name = room.get("name")
                break
    zone, error = zone_manager.set_zone_room(
        zone_id,
        room_id,
        room_name=room_name or room_id,
        default_room=bool(data.get("default_room", False)),
    )
    if error:
        return jsonify({"error": error}), 404
    return jsonify({"room": _room_entry(zone.room_id, zone.room_name, source="shiri")})

@app.route("/api/zones/<zone_id>/room", methods=["PUT"])
def set_zone_room(zone_id):
    data = request.get_json() or {}
    room_id = (data.get("room_id") or "").strip()
    if not room_id:
        return jsonify({"error": "room_id is required"}), 400
    zone, error = zone_manager.set_zone_room(
        zone_id,
        room_id,
        room_name=data.get("room_name"),
        default_room=bool(data.get("default_room", False)),
    )
    if error:
        return jsonify({"error": error}), 404
    return jsonify(zone.to_dict())

@app.route("/api/rooms/<room_id>/volume")
def get_room_volume(room_id):
    zone = _zone_bound_to_room(room_id)
    if not zone:
        return jsonify({"error": "No Shiri zone bound to this room"}), 404
    volume, error = zone_manager.get_volume(zone.zone_id)
    if error:
        return jsonify({"error": error, "zone_id": zone.zone_id}), 400
    return jsonify({"room_id": zone.room_id, "zone_id": zone.zone_id, "volume": volume})

@app.route("/api/rooms/<room_id>/volume", methods=["PUT"])
def set_room_volume(room_id):
    data = request.get_json() or {}
    volume = _parse_volume(data.get("volume"))
    if volume is None:
        return jsonify({"error": "volume must be a number from 0 to 100"}), 400
    zone = _zone_bound_to_room(room_id)
    if not zone:
        return jsonify({"error": "No Shiri zone bound to this room"}), 404
    ok, error = zone_manager.set_volume(zone.zone_id, volume)
    if error:
        return jsonify({"error": error, "zone_id": zone.zone_id}), 400
    return jsonify({"ok": bool(ok), "room_id": zone.room_id, "zone_id": zone.zone_id, "volume": volume})

@app.route("/api/zones/<zone_id>/tts-policy")
def get_tts_policy(zone_id):
    result, error = zone_manager.get_tts_policy(zone_id)
    if error:
        return jsonify({"error": error}), 404
    return jsonify(result)

@app.route("/api/zones/<zone_id>/tts-policy", methods=["PUT"])
def set_tts_policy(zone_id):
    data = request.get_json() or {}
    result, error = zone_manager.set_tts_policy(zone_id, data)
    if error:
        return jsonify({"error": error}), 404
    return jsonify(result)

@app.route("/api/rooms/<room_id>/tts-policy")
def get_room_tts_policy(room_id):
    zone, error = zone_manager.resolve_zone_for_room(room_id, require_running=False)
    if error:
        return jsonify({"error": error}), 404
    result, error = zone_manager.get_tts_policy(zone.zone_id)
    if error:
        return jsonify({"error": error}), 404
    return jsonify(result)

@app.route("/api/rooms/<room_id>/tts-policy", methods=["PUT"])
def set_room_tts_policy(room_id):
    zone, error = zone_manager.resolve_zone_for_room(room_id, require_running=False)
    if error:
        return jsonify({"error": error}), 404
    data = request.get_json() or {}
    result, error = zone_manager.set_tts_policy(zone.zone_id, data)
    if error:
        return jsonify({"error": error}), 404
    return jsonify(result)

# ---------------------------------------------------------------------------
# TTS routing API
# ---------------------------------------------------------------------------

@app.route("/api/rooms/<room_id>/tts-rtp", methods=["POST"])
def prepare_tts_rtp_for_room(room_id):
    result, error = _prepare_tts_rtp(default_room_id=room_id)
    if error:
        return jsonify({"error": error}), 400
    return jsonify(result), 201


@app.route("/api/zones/<zone_id>/tts-rtp", methods=["POST"])
def prepare_tts_rtp_for_zone(zone_id):
    result, error = _prepare_tts_rtp(zone_id=zone_id)
    if error:
        return jsonify({"error": error}), 400
    return jsonify(result), 201


@app.route("/api/rooms/<room_id>/tts-debug")
def tts_debug_for_room(room_id):
    zone, error = zone_manager.resolve_zone_for_room(room_id, require_running=False)
    if error:
        return jsonify({"error": error}), 404
    return jsonify(_tts_debug_payload(zone))


@app.route("/api/zones/<zone_id>/tts-debug")
def tts_debug_for_zone(zone_id):
    zone = zone_manager.get_zone(zone_id)
    if not zone:
        return jsonify({"error": "Zone not found"}), 404
    return jsonify(_tts_debug_payload(zone))


def _prepare_tts_rtp(default_room_id=None, zone_id=None):
    data = request.args.to_dict()
    if request.is_json:
        data.update(request.get_json(silent=True) or {})

    resolved_zone = None
    if zone_id is not None:
        resolved_zone = zone_manager.get_zone(zone_id)
        if not resolved_zone:
            return None, "Zone not found"
    else:
        room_id = data.get("room_id") or default_room_id
        resolved_zone, error = zone_manager.resolve_zone_for_room(room_id)
        if error:
            return None, error

    result, error = zone_manager.prepare_tts_rtp(
        resolved_zone.zone_id,
        request_id=data.get("request_id") or uuid.uuid4().hex,
        audio_format=data.get("format") or data.get("audio_format") or "rtp_l16",
        sample_rate=data.get("sample_rate") or 24000,
        channels=data.get("channels") or 1,
        sample_width=data.get("sample_width") or 2,
        text=data.get("text"),
        speaker_id=data.get("speaker_id"),
        speaker_name=data.get("speaker_name"),
    )
    if error:
        return None, error
    host = _request_hostname()
    result["rtp"] = {
        "host": host,
        "port": result["tts_rtp_port"],
        "payload_type": result["payload_type"],
        "encoding_name": result["encoding_name"],
        "clock_rate": result["sample_rate"],
        "channels": result["channels"],
        "sample_format": result["sample_format"],
        "packet_time_ms": 20,
    }
    result["sender_contract"] = {
        "pace": "realtime",
        "rtp_timestamp_clock": result["sample_rate"],
        "payload_bytes": "RTP/L16 16-bit signed big-endian PCM",
        "appsrc_input": "Feed VibeVoice S16LE PCM bytes to appsrc; do not build RTP packets in Python.",
        "timing_owner": "GStreamer rawaudioparse/rtpL16pay/udpsink owns timestamps and pacing.",
        "lifecycle": (
            "Prefer one long-lived sender pipeline per Shiri RTP target. If creating a pipeline per utterance, "
            "call appsrc end-of-stream and wait for the GStreamer bus EOS before setting the pipeline to NULL."
        ),
        "tail_flush": "Pad the final PCM chunk to a 20 ms boundary, or EOS+wait, so rtpL16pay flushes the tail packet.",
        "do_not": [
            "Do not time.sleep between audio chunks.",
            "Do not set RTP timestamps or sequence numbers in Python.",
            "Do not set buffer PTS/duration when using the appsrc+rawaudioparse pipeline.",
            "Do not send UDP packets directly from Python.",
            "Do not stop the sender pipeline immediately after the last push-buffer.",
        ],
    }
    result["sender_gstreamer_appsrc_pipeline"] = (
        "appsrc name=tts_src is-live=true format=bytes do-timestamp=false "
        "block=true max-bytes=9600 "
        f"caps=audio/x-unaligned-raw,format=S16LE,layout=interleaved,rate={result['sample_rate']},"
        f"channels={result['channels']} ! "
        "rawaudioparse use-sink-caps=true ! "
        "queue max-size-time=200000000 max-size-bytes=0 max-size-buffers=0 ! "
        "audioconvert ! audioresample ! "
        f"audio/x-raw,format=S16BE,layout=interleaved,rate={result['sample_rate']},"
        f"channels={result['channels']} ! "
        f"rtpL16pay pt={result['payload_type']} "
        f"ptime-multiple=20000000 ! udpsink host={host} "
        f"port={result['tts_rtp_port']} sync=true async=false"
    )
    result["sender_gstreamer_sink"] = result["sender_gstreamer_appsrc_pipeline"]
    return result, None


def _request_hostname():
    host = request.headers.get("X-Forwarded-Host") or request.host
    if host.startswith("["):
        return host.split("]", 1)[0].strip("[]")
    return host.split(":", 1)[0]


def _tts_debug_payload(zone):
    return {
        "ok": True,
        "zone_id": zone.zone_id,
        "zone_status": zone.status,
        "room_id": zone.room_id,
        "room_name": zone.room_name,
        "grp_dir": zone.grp_dir,
        "tts_transport": "rtp_l16",
        "tts_rtp_port": zone.tts_rtp_port,
        "mixer_log_tail": _read_log_tail(zone.zone_id, "mixer", lines=80),
        "arecord_log_tail": _read_log_tail(zone.zone_id, "arecord", lines=20),
    }

# ---------------------------------------------------------------------------
# Speaker / Output API
# ---------------------------------------------------------------------------

@app.route("/api/zones/<zone_id>/speakers")
def get_speakers(zone_id):
    speakers, error = zone_manager.get_speakers(zone_id)
    if error:
        return jsonify({"error": error}), 400
    return jsonify({"speakers": speakers})

@app.route("/api/zones/<zone_id>/speakers", methods=["PUT"])
def set_speakers(zone_id):
    data = request.get_json() or {}
    speaker_ids = data.get("speaker_ids", [])
    ok, error = zone_manager.set_speakers(zone_id, speaker_ids)
    if error:
        return jsonify({"error": error}), 400
    return jsonify({"ok": True})

@app.route("/api/zones/<zone_id>/speakers/<speaker_id>/toggle", methods=["POST"])
def toggle_speaker(zone_id, speaker_id):
    data = request.get_json() or {}
    enabled = data.get("enabled", True)
    ok, error = zone_manager.toggle_speaker(zone_id, speaker_id, enabled)
    if error:
        return jsonify({"error": error}), 400
    return jsonify({"ok": True})

# ---------------------------------------------------------------------------
# Volume API
# ---------------------------------------------------------------------------

@app.route("/api/zones/<zone_id>/volume")
def get_volume(zone_id):
    volume, error = zone_manager.get_volume(zone_id)
    if error:
        return jsonify({"error": error}), 400
    return jsonify({"volume": volume})

@app.route("/api/zones/<zone_id>/volume", methods=["PUT"])
def set_volume(zone_id):
    data = request.get_json() or {}
    volume = data.get("volume", 50)
    ok, error = zone_manager.set_volume(zone_id, volume)
    if error:
        return jsonify({"error": error}), 400
    return jsonify({"ok": True, "volume": volume})

@app.route("/api/zones/<zone_id>/speakers/<speaker_id>/volume", methods=["PUT"])
def set_speaker_volume(zone_id, speaker_id):
    data = request.get_json() or {}
    volume = data.get("volume", 50)
    ok, error = zone_manager.set_speaker_volume(zone_id, speaker_id, volume)
    if error:
        return jsonify({"error": error}), 400
    return jsonify({"ok": True, "volume": volume})

# ---------------------------------------------------------------------------
# Sync/Latency API
# ---------------------------------------------------------------------------

@app.route("/api/zones/<zone_id>/latency")
def get_latency(zone_id):
    result, error = zone_manager.get_latency(zone_id)
    if error:
        return jsonify({"error": error}), 404
    return jsonify(result)

@app.route("/api/zones/<zone_id>/latency", methods=["PUT"])
def set_latency(zone_id):
    """
    Set latency offset for timeline/lyrics sync.
    Negative = audio delivered EARLIER (use if lyrics are ahead of speaker).
    Requires zone restart to take effect.
    """
    data = request.get_json() or {}
    offset = data.get("latency_offset")

    if offset is None:
        return jsonify({"error": "latency_offset is required"}), 400

    try:
        offset = float(offset)
    except (ValueError, TypeError):
        return jsonify({"error": "latency_offset must be a number"}), 400

    # Sanity check: offset should typically be between -5 and +1
    if offset < -10 or offset > 5:
        return jsonify({"error": "latency_offset should be between -10 and +5 seconds"}), 400

    result, error = zone_manager.set_latency(zone_id, offset)
    if error:
        return jsonify({"error": error}), 404
    return jsonify(result)

# ---------------------------------------------------------------------------
# Player status API
# ---------------------------------------------------------------------------

@app.route("/api/zones/<zone_id>/player")
def get_player(zone_id):
    status, error = zone_manager.get_player_status(zone_id)
    if error:
        return jsonify({"error": error}), 400
    return jsonify(status or {})

# ---------------------------------------------------------------------------
# Logs API
# ---------------------------------------------------------------------------

@app.route("/api/zones/<zone_id>/logs/<log_type>")
def get_logs(zone_id, log_type):
    lines = int(request.args.get("lines", 100))
    log_lines = _read_log_tail(zone_id, log_type, lines)
    return jsonify({"lines": log_lines, "log_type": log_type})

@app.route("/api/logs")
def get_combined_logs():
    try:
        lines = int(request.args.get("lines", 200))
    except (TypeError, ValueError):
        lines = 200
    lines = min(max(lines, 1), 1000)
    entries = _logs_for_query(
        zone_id=request.args.get("zone_id"),
        room_id=request.args.get("room_id"),
        log_filter=request.args.get("type", "all"),
        lines=lines,
    )
    return jsonify({
        "entries": entries,
        "lines": [entry["line"] for entry in entries],
        "log_type": request.args.get("type", "all"),
    })

# ---------------------------------------------------------------------------
# SocketIO events
# ---------------------------------------------------------------------------

@socketio.on("connect")
def handle_connect():
    log.info("Client connected")
    # Send current state of all zones
    for zone in zone_manager.list_zones():
        socketio.emit("zone_status", zone.to_dict())

@socketio.on("subscribe_logs")
def handle_subscribe_logs(data):
    data = data or {}
    zone_id = data.get("zone_id")
    if data.get("all") or zone_id in {"*", "all"}:
        for zone in zone_manager.list_zones():
            start_log_watch(zone.zone_id)
    elif zone_id:
        start_log_watch(zone_id)

@socketio.on("unsubscribe_logs")
def handle_unsubscribe_logs(data):
    data = data or {}
    zone_id = data.get("zone_id")
    if data.get("all") or zone_id in {"*", "all"}:
        for zone in zone_manager.list_zones():
            stop_log_watch(zone.zone_id)
    elif zone_id:
        stop_log_watch(zone_id)

# ---------------------------------------------------------------------------
# Startup / Shutdown
# ---------------------------------------------------------------------------

def startup():
    """Initialize system and load saved zones."""
    log.info("=" * 60)
    log.info("  Shiri Multiroom AirPlay Manager")
    log.info("=" * 60)

    # Check root
    if os.geteuid() != 0:
        log.error("Shiri daemon must run as root (sudo python3 app.py)")
        sys.exit(1)

    # Setup ALSA loopback
    if not zone_manager.setup_alsa_loopback():
        log.error("Failed to setup ALSA loopback — some features may not work")

    # Reap legacy namespaces/macvlans/leases left by older daemon versions.
    zone_manager.cleanup_stale_runtime()

    # Start the shared host nqptp used by all Shairport instances.
    zone_manager.start_host_nqptp()

    # Load saved zones from config
    zone_manager.load_saved_zones()

    # Auto-start zones that have auto_start=True
    for zone in zone_manager.list_zones():
        if zone.config.get("auto_start", False):
            log.info("Auto-starting zone: %s", zone.display_name)
            zone_manager.start_zone(zone.zone_id)

    # Start diagnostic monitor for AirPlay disconnect debugging
    zone_manager.start_diagnostic_monitor()

    log.info("Shiri daemon ready — UI at http://0.0.0.0:8080")


def shutdown_handler(signum, frame):
    """Graceful shutdown on SIGTERM/SIGINT."""
    log.info("Shutdown signal received...")
    _log_stop.set()
    zone_manager.shutdown()
    sys.exit(0)


signal.signal(signal.SIGTERM, shutdown_handler)
signal.signal(signal.SIGINT, shutdown_handler)

if __name__ == "__main__":
    startup()
    socketio.run(app, host="0.0.0.0", port=8080, debug=False)
