#!/usr/bin/env python3
"""
app.py — Main Shiri daemon.

Serves the web UI and REST API on port 8080.
Orchestrates zone lifecycle using the existing shell scripts.

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

from config import ConfigStore
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

# ---------------------------------------------------------------------------
# Log streaming — single thread tails all watched zones
# ---------------------------------------------------------------------------
LOG_DIR = "/var/lib/shiri/groups"
LOG_TYPES = ["shairport", "owntone", "owntone_wrapper", "arecord", "pause_bridge", "volume_bridge", "sync_reset"]
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
                        for line in new_data.splitlines():
                            if line.strip():
                                socketio.emit("zone_log", {
                                    "zone_id": zone_id,
                                    "log_type": log_type,
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
    zone_id = data.get("zone_id")
    if zone_id:
        start_log_watch(zone_id)

@socketio.on("unsubscribe_logs")
def handle_unsubscribe_logs(data):
    zone_id = data.get("zone_id")
    if zone_id:
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

    # Clean up any stale host nqptp (each zone now runs its own in netns)
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
