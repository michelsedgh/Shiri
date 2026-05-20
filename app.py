#!/usr/bin/env python3
"""
app.py — Main Shiri daemon.

Serves the web UI and REST API on port 8080.
Orchestrates the Shairport receiver, mixer, and OwnTone sender lifecycle.

Run with: sudo python3 app.py
"""

import logging
import os
import signal
import sys
import threading
import time

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from flask_socketio import SocketIO

from config import ConfigStore, MAX_SHAIRPORT_LATENCY_OFFSET
from tts_webrtc import TtsWebRtcService
from zone import ZoneManager

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
tts_webrtc_service = TtsWebRtcService(zone_manager)

# ---------------------------------------------------------------------------
# Log streaming — single thread tails all watched zones
# ---------------------------------------------------------------------------
LOG_DIR = "/var/lib/shiri/groups"
LOG_TYPES = ["shairport", "owntone", "owntone_wrapper", "mixer", "volume_bridge"]
LOG_FILTERS = {
    "all": LOG_TYPES,
    "tts": ["mixer"],
    "speaker": ["owntone", "owntone_wrapper"],
    "volume": ["volume_bridge", "owntone"],
    "airplay": ["shairport"],
    "owntone": ["owntone", "owntone_wrapper"],
    "errors": LOG_TYPES,
}
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
                                    "lionos_room_id": zone.lionos_room_id if zone else None,
                                    "lionos_room_name": zone.lionos_room_name if zone else None,
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
# Dashboard aggregation
# ---------------------------------------------------------------------------

def _settings():
    settings = config_store.get_settings()
    settings.setdefault("default_interface", "")
    return settings


def _public_settings(settings=None):
    settings = settings or _settings()
    return {
        "default_interface": settings.get("default_interface", ""),
    }


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
        "lionos_room_id": zone.lionos_room_id,
        "lionos_room_name": zone.lionos_room_name,
        "default_lionos_room": bool(zone.config.get("default_lionos_room", False)),
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


def _dashboard_payload():
    zones = [_zone_summary(zone) for zone in zone_manager.list_zones()]
    return {
        "system": zone_manager.get_system_status(),
        "settings": _public_settings(),
        "zones": zones,
        "default_lionos_room_id": next(
            (zone["lionos_room_id"] for zone in zones if zone.get("default_lionos_room")),
            None,
        ),
        "log_types": LOG_TYPES,
        "log_filters": sorted(LOG_FILTERS.keys()),
        "generated_at": time.time(),
    }


def _log_severity(line):
    text = line.lower()
    if any(token in text for token in ("error", "failed", "failure", "died", "exception", "fatal")):
        return "error"
    if any(token in text for token in ("warn", "retry", "timeout", "closed")):
        return "warning"
    return "info"


def _log_category(log_type, line):
    if log_type == "mixer" or "tts" in line.lower():
        return "tts"
    if log_type == "shairport":
        return "airplay"
    if log_type in {"owntone", "owntone_wrapper"}:
        return "owntone"
    if log_type == "volume_bridge":
        return "volume"
    return "system"


def _logs_for_query(zone_id=None, log_filter="all", lines=200):
    log_filter = (log_filter or "all").lower()
    selected_types = LOG_FILTERS.get(log_filter, LOG_TYPES if log_filter == "all" else [log_filter])
    if selected_types == ["all"]:
        selected_types = LOG_TYPES

    zones = []
    if zone_id:
        zone = zone_manager.get_zone(zone_id)
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
                    "lionos_room_id": zone.lionos_room_id,
                    "lionos_room_name": zone.lionos_room_name,
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
    if "default_interface" in data:
        updates["default_interface"] = str(data.get("default_interface") or "").strip()
    if updates:
        config_store.update_settings(updates)
    return jsonify({"settings": _public_settings()})

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
        if abs(latency_offset) > MAX_SHAIRPORT_LATENCY_OFFSET:
            return jsonify({
                "error": f"latency_offset should be between -{MAX_SHAIRPORT_LATENCY_OFFSET} and +{MAX_SHAIRPORT_LATENCY_OFFSET} seconds"
            }), 400

    zone = zone_manager.create_zone(
        name=name,
        interface=interface,
        auto_start=data.get("auto_start", False),
        latency_offset=latency_offset,
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
# LionOS binding metadata API
# ---------------------------------------------------------------------------

@app.route("/api/zones/<zone_id>/binding", methods=["PUT"])
def set_zone_binding(zone_id):
    data = request.get_json() or {}
    lionos_room_id = (data.get("lionos_room_id") or "").strip()
    if not lionos_room_id:
        return jsonify({"error": "lionos_room_id is required"}), 400
    zone, error = zone_manager.set_zone_binding(
        zone_id,
        lionos_room_id,
        lionos_room_name=data.get("lionos_room_name"),
        default=bool(data.get("default", False)),
    )
    if error:
        return jsonify({"error": error}), 404
    return jsonify(zone.to_dict())

@app.route("/api/zones/<zone_id>/binding", methods=["DELETE"])
def clear_zone_binding(zone_id):
    zone, error = zone_manager.clear_zone_binding(zone_id)
    if error:
        return jsonify({"error": error}), 404
    return jsonify(zone.to_dict())


@app.route("/api/zones/<zone_id>/player/play", methods=["POST"])
def play_zone(zone_id):
    zone = zone_manager.get_zone(zone_id)
    if not zone:
        return jsonify({"error": "Zone not found"}), 404
    if zone.status != zone.STATUS_RUNNING:
        return jsonify({"error": "Zone is not running", "zone_id": zone.zone_id}), 400
    return _play_zone_response(zone)


def _play_zone_response(zone):
    if not zone.owntone_api:
        return jsonify({"error": "Zone does not have an OwnTone player", "zone_id": zone.zone_id}), 400
    data = request.get_json(silent=True) or {}
    response = zone.owntone_api.play()
    return jsonify({
        "ok": True,
        "zone_id": zone.zone_id,
        "playlist": data.get("playlist"),
        "owntone_response": response,
    })

@app.route("/api/zones/<zone_id>/player/stop", methods=["POST"])
def stop_zone_player(zone_id):
    zone = zone_manager.get_zone(zone_id)
    if not zone:
        return jsonify({"error": "Zone not found"}), 404
    if zone.status != zone.STATUS_RUNNING:
        return jsonify({"error": "Zone is not running", "zone_id": zone.zone_id}), 400
    return _stop_zone_player_response(zone)


def _stop_zone_player_response(zone):
    if not zone.owntone_api:
        return jsonify({"error": "Zone does not have an OwnTone player", "zone_id": zone.zone_id}), 400
    response = zone.owntone_api.stop()
    return jsonify({
        "ok": True,
        "zone_id": zone.zone_id,
        "owntone_response": response,
    })

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

# ---------------------------------------------------------------------------
# TTS routing API
# ---------------------------------------------------------------------------

@app.route("/api/zones/<zone_id>/tts-debug")
def tts_debug_for_zone(zone_id):
    zone = zone_manager.get_zone(zone_id)
    if not zone:
        return jsonify({"error": "Zone not found"}), 404
    return jsonify(_tts_debug_payload(zone))


@app.route("/api/zones/<zone_id>/tts/webrtc-offer", methods=["POST"])
def tts_webrtc_offer_for_zone(zone_id):
    zone = zone_manager.get_zone(zone_id)
    if not zone:
        return jsonify({"ok": False, "error": "Zone not found"}), 404
    data = request.get_json() or {}
    try:
        return jsonify(tts_webrtc_service.submit_offer(zone.zone_id, data))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503
    except Exception as exc:
        log.exception("WebRTC TTS offer failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


def _tts_debug_payload(zone):
    host = request.headers.get("X-Forwarded-Host") or request.host
    return {
        "ok": True,
        "zone_id": zone.zone_id,
        "zone_status": zone.status,
        "lionos_room_id": zone.lionos_room_id,
        "lionos_room_name": zone.lionos_room_name,
        "grp_dir": zone.grp_dir,
        "tts_transport": "webrtc",
        "tts_webrtc_socket": zone.tts_webrtc_socket,
        "tts_webrtc": {
            "offer_url": f"{request.scheme}://{host}/api/zones/{zone.zone_id}/tts/webrtc-offer",
            "transport": "webrtc",
            "codec": "opus",
            "session_model": "persistent_per_zone",
            "internal_audio_target": "gstreamer-audiomixer",
            "timing_owner": "Shiri zone mixer",
        },
        "mixer_log_tail": _read_log_tail(zone.zone_id, "mixer", lines=80),
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

    if abs(offset) > MAX_SHAIRPORT_LATENCY_OFFSET:
        return jsonify({
            "error": f"latency_offset should be between -{MAX_SHAIRPORT_LATENCY_OFFSET} and +{MAX_SHAIRPORT_LATENCY_OFFSET} seconds"
        }), 400

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

    # Reap Shiri-owned processes and old namespace leftovers from unclean exits.
    zone_manager.cleanup_stale_runtime()

    # Load saved zones from config
    zone_manager.load_saved_zones()
    zone_manager.cleanup_orphaned_group_dirs()

    # Auto-start zones that have auto_start=True
    for zone in zone_manager.list_zones():
        if zone.config.get("auto_start", False):
            log.info("Auto-starting zone: %s", zone.display_name)
            zone_manager.start_zone(zone.zone_id)

    # Start diagnostic monitor for AirPlay disconnect debugging
    zone_manager.start_diagnostic_monitor()
    tts_webrtc_service.start()

    log.info("Shiri daemon ready — UI at http://0.0.0.0:8080")


def shutdown_handler(signum, frame):
    """Graceful shutdown on SIGTERM/SIGINT."""
    log.info("Shutdown signal received...")
    _log_stop.set()
    tts_webrtc_service.stop()
    zone_manager.shutdown()
    sys.exit(0)


signal.signal(signal.SIGTERM, shutdown_handler)
signal.signal(signal.SIGINT, shutdown_handler)

if __name__ == "__main__":
    startup()
    socketio.run(app, host="0.0.0.0", port=8080, debug=False)
