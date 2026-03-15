#!/usr/bin/env python3
"""
shiri_daemon.py — Main Shiri daemon.

Serves the web UI and REST API on port 8080.
Orchestrates zone lifecycle using the existing shell scripts.

Run with: sudo python3 shiri_daemon.py
"""

import logging
import os
import signal
import sys

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from flask_socketio import SocketIO

from config_store import ConfigStore
from log_streamer import LogStreamer
from zone_manager import ZoneManager

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
log_streamer = LogStreamer(socketio)

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
    zone = zone_manager.update_zone_config(zone_id, data)
    if not zone:
        return jsonify({"error": "Zone not found or currently running"}), 400
    return jsonify(zone.to_dict())


@app.route("/api/zones/<zone_id>", methods=["DELETE"])
def delete_zone(zone_id):
    if zone_manager.delete_zone(zone_id):
        log_streamer.stop_watching(zone_id)
        return jsonify({"ok": True})
    return jsonify({"error": "Zone not found"}), 404

# ---------------------------------------------------------------------------
# Zone lifecycle API
# ---------------------------------------------------------------------------

@app.route("/api/zones/<zone_id>/start", methods=["POST"])
def start_zone(zone_id):
    if zone_manager.start_zone(zone_id):
        log_streamer.start_watching(zone_id)
        return jsonify({"ok": True})
    return jsonify({"error": "Cannot start zone (not found or already running)"}), 400


@app.route("/api/zones/<zone_id>/stop", methods=["POST"])
def stop_zone(zone_id):
    if zone_manager.stop_zone(zone_id):
        log_streamer.stop_watching(zone_id)
        return jsonify({"ok": True})
    return jsonify({"error": "Cannot stop zone (not found or not running)"}), 400

# ---------------------------------------------------------------------------
# Speaker / Output API
# ---------------------------------------------------------------------------

@app.route("/api/zones/<zone_id>/speakers")
def get_speakers(zone_id):
    zone = zone_manager.get_zone(zone_id)
    if not zone or not zone.owntone_api:
        return jsonify({"error": "Zone not running or not found"}), 400
    outputs = zone.owntone_api.get_outputs()
    return jsonify({"speakers": outputs})


@app.route("/api/zones/<zone_id>/speakers", methods=["PUT"])
def set_speakers(zone_id):
    zone = zone_manager.get_zone(zone_id)
    if not zone or not zone.owntone_api:
        return jsonify({"error": "Zone not running or not found"}), 400

    data = request.get_json() or {}
    speaker_ids = data.get("speaker_ids", [])
    zone.owntone_api.set_outputs(speaker_ids)

    # Get current outputs to save names for reliable restoration
    outputs = zone.owntone_api.get_outputs()
    selected_speakers = []
    for out in outputs:
        if str(out.get("id")) in [str(sid) for sid in speaker_ids]:
            selected_speakers.append({
                "id": out.get("id"),
                "name": out.get("name", "Unknown")
            })
    
    # Save speaker selection with names for restoration
    zone.config["speakers"] = speaker_ids  # Keep IDs for backwards compat
    zone.config["speaker_names"] = selected_speakers  # Save names for reliable restore
    config_store.save_zone(zone_id, zone.config)

    return jsonify({"ok": True})


@app.route("/api/zones/<zone_id>/speakers/<speaker_id>/toggle", methods=["POST"])
def toggle_speaker(zone_id, speaker_id):
    zone = zone_manager.get_zone(zone_id)
    if not zone or not zone.owntone_api:
        return jsonify({"error": "Zone not running or not found"}), 400

    data = request.get_json() or {}
    enabled = data.get("enabled", True)

    if enabled:
        zone.owntone_api.enable_output(speaker_id)
    else:
        zone.owntone_api.disable_output(speaker_id)

    return jsonify({"ok": True})

# ---------------------------------------------------------------------------
# Volume API
# ---------------------------------------------------------------------------

@app.route("/api/zones/<zone_id>/volume")
def get_volume(zone_id):
    zone = zone_manager.get_zone(zone_id)
    if not zone or not zone.owntone_api:
        return jsonify({"error": "Zone not running or not found"}), 400
    volume = zone.owntone_api.get_volume()
    return jsonify({"volume": volume})


@app.route("/api/zones/<zone_id>/volume", methods=["PUT"])
def set_volume(zone_id):
    zone = zone_manager.get_zone(zone_id)
    if not zone or not zone.owntone_api:
        return jsonify({"error": "Zone not running or not found"}), 400

    data = request.get_json() or {}
    volume = data.get("volume", 50)
    zone.owntone_api.set_volume(volume)
    return jsonify({"ok": True, "volume": volume})


@app.route("/api/zones/<zone_id>/speakers/<speaker_id>/volume", methods=["PUT"])
def set_speaker_volume(zone_id, speaker_id):
    zone = zone_manager.get_zone(zone_id)
    if not zone or not zone.owntone_api:
        return jsonify({"error": "Zone not running or not found"}), 400

    data = request.get_json() or {}
    volume = data.get("volume", 50)
    zone.owntone_api.set_output_volume(speaker_id, volume)
    return jsonify({"ok": True, "volume": volume})

# ---------------------------------------------------------------------------
# Sync/Latency API
# ---------------------------------------------------------------------------

@app.route("/api/zones/<zone_id>/latency")
def get_latency(zone_id):
    """Get current latency offset for a zone."""
    zone = zone_manager.get_zone(zone_id)
    if not zone:
        return jsonify({"error": "Zone not found"}), 404
    from zone_manager import DEFAULT_LATENCY_OFFSET
    offset = zone.config.get("latency_offset", DEFAULT_LATENCY_OFFSET)
    return jsonify({"latency_offset": offset, "default": DEFAULT_LATENCY_OFFSET})


@app.route("/api/zones/<zone_id>/latency", methods=["PUT"])
def set_latency(zone_id):
    """
    Set latency offset for timeline/lyrics sync.
    Negative = audio delivered EARLIER (use if lyrics are ahead of speaker).
    Requires zone restart to take effect.
    """
    zone = zone_manager.get_zone(zone_id)
    if not zone:
        return jsonify({"error": "Zone not found"}), 404

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
    
    zone.config["latency_offset"] = offset
    config_store.save_zone(zone_id, zone.config)
    
    log.info("Set latency_offset=%s for %s (restart zone to apply)", offset, zone_id)
    return jsonify({
        "ok": True,
        "latency_offset": offset,
        "note": "Restart zone to apply new latency offset"
    })

# ---------------------------------------------------------------------------
# Player status API
# ---------------------------------------------------------------------------

@app.route("/api/zones/<zone_id>/player")
def get_player(zone_id):
    zone = zone_manager.get_zone(zone_id)
    if not zone or not zone.owntone_api:
        return jsonify({"error": "Zone not running or not found"}), 400
    status = zone.owntone_api.get_player_status()
    return jsonify(status or {})

# ---------------------------------------------------------------------------
# Logs API
# ---------------------------------------------------------------------------

@app.route("/api/zones/<zone_id>/logs/<log_type>")
def get_logs(zone_id, log_type):
    lines = int(request.args.get("lines", 100))
    log_lines = log_streamer.get_recent_logs(zone_id, log_type, lines)
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
        log_streamer.start_watching(zone_id)


@socketio.on("unsubscribe_logs")
def handle_unsubscribe_logs(data):
    zone_id = data.get("zone_id")
    if zone_id:
        log_streamer.stop_watching(zone_id)

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
        log.error("Shiri daemon must run as root (sudo python3 shiri_daemon.py)")
        sys.exit(1)

    # Setup ALSA loopback
    if not zone_manager.setup_alsa_loopback():
        log.error("Failed to setup ALSA loopback — some features may not work")

    # Start shared nqptp
    if not zone_manager.start_host_nqptp():
        log.error("Failed to start nqptp — AirPlay 2 sync will not work")

    # Load saved zones from config
    zone_manager.load_saved_zones()

    # Auto-start zones that have auto_start=True
    for zone in zone_manager.list_zones():
        if zone.config.get("auto_start", False):
            log.info("Auto-starting zone: %s", zone.display_name)
            zone_manager.start_zone(zone.zone_id)

    log.info("Shiri daemon ready — UI at http://0.0.0.0:8080")


def shutdown_handler(signum, frame):
    """Graceful shutdown on SIGTERM/SIGINT."""
    log.info("Shutdown signal received...")
    log_streamer.stop_all()
    zone_manager.shutdown()
    sys.exit(0)


signal.signal(signal.SIGTERM, shutdown_handler)
signal.signal(signal.SIGINT, shutdown_handler)

if __name__ == "__main__":
    startup()
    socketio.run(app, host="0.0.0.0", port=8080, debug=False)
