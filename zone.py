"""
zone.py — Zone model and ZoneManager for Shiri.

Zone: Data model representing a single AirPlay zone and its runtime state.
ZoneManager: CRUD operations, system setup, API facade (speakers, volume,
latency, player status), diagnostic monitoring, event emission, and shutdown.

Start/stop implementation details are delegated to zone_lifecycle.py.
"""

import logging
import os
import subprocess
import threading
import time
import uuid

from config import (
    BASE_DIR,
    DEFAULT_LATENCY_OFFSET,
)
from zone_lifecycle import (
    _run,
    _kill_pid,
    start_zone_thread,
    stop_zone_thread,
    cleanup_zone,
    cleanup_stale_runtime,
)

log = logging.getLogger("shiri.zone")


class Zone:
    """
    Represents a single Shiri AirPlay zone.
    Manages the full lifecycle: create → start → running → stop → cleanup.
    """

    # Possible statuses
    STATUS_STOPPED = "stopped"
    STATUS_STARTING = "starting"
    STATUS_RUNNING = "running"
    STATUS_STOPPING = "stopping"
    STATUS_ERROR = "error"

    def __init__(self, zone_id, config, on_status_change=None):
        """
        config: {
            "name": "Living Room",          # Display name / AirPlay name
            "interface": "eth0",            # Network interface for macvlan
            "auto_start": False,
            "speakers": ["speaker_id_1"],   # Saved speaker selections
        }
        """
        self.zone_id = zone_id
        self.config = config
        self.status = self.STATUS_STOPPED
        self.error_message = ""
        self.on_status_change = on_status_change

        # Runtime state (populated when running)
        self.allocated_subdevice = None
        self.shairport_pid = None
        self.arecord_supervisor_pid = None
        self.metadata_relay_pid = None
        self.pause_bridge_pid = None
        self.owntone_pid = None
        self.nqptp_pid = None  # Per-zone, runs inside netns
        self.netns_name = None
        self.macvlan_if = None
        self.shairport_ip = None
        self.owntone_ip = None
        self.owntone_api = None  # OwnToneAPI instance
        self._grp_dir = None
        self._stop_event = threading.Event()

    @property
    def grp_dir(self):
        if self._grp_dir is None:
            self._grp_dir = os.path.join(BASE_DIR, "groups", self.zone_id)
        return self._grp_dir

    @property
    def display_name(self):
        return self.config.get("name", f"Shiri {self.zone_id}")

    @property
    def interface(self):
        return self.config.get("interface", "")

    def _set_status(self, status, error=""):
        self.status = status
        self.error_message = error
        if self.on_status_change:
            self.on_status_change(self)

    def to_dict(self):
        """Serialize zone state for API response."""
        return {
            "zone_id": self.zone_id,
            "config": self.config,
            "status": self.status,
            "error_message": self.error_message,
            "shairport_ip": self.shairport_ip,
            "owntone_ip": self.owntone_ip,
            "netns_name": self.netns_name,
            "allocated_subdevice": self.allocated_subdevice,
            "latency_offset": self.config.get("latency_offset", DEFAULT_LATENCY_OFFSET),
        }


class ZoneManager:
    """
    Manages all Shiri zones. Handles shared resources (nqptp, ALSA loopback)
    and orchestrates zone lifecycle.
    """

    def __init__(self, config_store, socketio=None):
        self.config_store = config_store
        self.socketio = socketio
        self.zones = {}  # zone_id -> Zone
        self._lock = threading.Lock()
        self._host_nqptp_pid = None
        self._alsa_ready = False

    # -------------------------------------------------------------------------
    # System-level setup (same as dual_zone_demo.sh top-level functions)
    # -------------------------------------------------------------------------

    def setup_alsa_loopback(self):
        """
        Same as setup_alsa_loopback() in dual_zone_demo.sh.
        Load snd-aloop with 16 subdevices if not already loaded.
        """
        # Check if already loaded
        result = _run(["lsmod"])
        if "snd_aloop" in (result.stdout or ""):
            log.info("snd-aloop already loaded")
            self._alsa_ready = True
            return True

        log.info("Loading snd-aloop kernel module with 16 subdevices...")
        result = _run(["modprobe", "snd-aloop", "pcm_substreams=16"])
        if result.returncode != 0:
            # Try without options
            log.warning("Failed with options, trying without...")
            result = _run(["modprobe", "snd-aloop"])
            if result.returncode != 0:
                log.error("Cannot load snd-aloop module. Install linux-modules-extra-$(uname -r)")
                return False

        time.sleep(1)

        # Verify loopback card exists
        result = _run(["aplay", "-l"])
        if "Loopback" not in (result.stdout or ""):
            log.error("ALSA Loopback card not found after loading module")
            return False

        log.info("ALSA Loopback ready")
        self._alsa_ready = True
        return True

    def start_host_nqptp(self):
        """
        Previously started a shared host nqptp for all zones.
        Now each zone runs its own nqptp inside its netns with private /dev/shm.
        This isolates PTP timing so one zone's disconnect can't poison another's clock.
        
        If a stale host nqptp is running, kill it to free ports 319/320 for per-zone instances.
        """
        host_netns = os.stat("/proc/self/ns/net").st_ino
        result = _run(["pgrep", "-x", "nqptp"])
        if result.returncode == 0 and result.stdout.strip():
            for pid_str in result.stdout.strip().split():
                pid = int(pid_str)
                try:
                    pid_netns = os.stat(f"/proc/{pid}/ns/net").st_ino
                except OSError:
                    continue
                if pid_netns != host_netns:
                    continue
                log.info("Killing stale host nqptp (pid %d) — now per-zone in netns", pid)
                _kill_pid(pid, "stale host nqptp")
        self._host_nqptp_pid = None
        log.info("nqptp is now per-zone (each zone runs its own in isolated netns)")
        return True

    def cleanup_stale_runtime(self):
        """Remove stale Shiri namespaces/processes left by an unclean daemon exit."""
        cleanup_stale_runtime()

    def get_network_interfaces(self):
        """
        Same as select_parent_interface() listing logic in dual_zone_demo.sh.
        Returns list of interface names (excluding lo).
        """
        result = _run(["ip", "-o", "link", "show"])
        if result.returncode != 0:
            return []
        interfaces = []
        for line in result.stdout.splitlines():
            # Format: "2: eth0: <...>"
            parts = line.split(": ")
            if len(parts) >= 2:
                iface = parts[1].split("@")[0]  # Handle veth@if... style
                if iface != "lo":
                    interfaces.append(iface)
        return interfaces

    def get_system_status(self):
        """Return system-level health info."""
        return {
            "nqptp_mode": "per-zone",
            "alsa_ready": self._alsa_ready,
            "interfaces": self.get_network_interfaces(),
            "zone_count": len(self.zones),
            "running_zones": sum(1 for z in self.zones.values()
                                 if z.status == Zone.STATUS_RUNNING),
        }

    # -------------------------------------------------------------------------
    # Zone CRUD
    # -------------------------------------------------------------------------

    def create_zone(self, name, interface, auto_start=False, latency_offset=None):
        """Create a new zone (does not start it)."""
        zone_id = f"zone_{uuid.uuid4().hex[:8]}"
        config = {
            "name": name,
            "interface": interface,
            "auto_start": auto_start,
            "speakers": [],
        }
        if latency_offset is not None:
            config["latency_offset"] = latency_offset
        zone = Zone(zone_id, config, on_status_change=self._emit_zone_status)
        with self._lock:
            self.zones[zone_id] = zone
        self.config_store.save_zone(zone_id, config)
        self._emit_zone_status(zone)
        log.info("Created zone %s (%s)", zone_id, name)
        return zone

    def delete_zone(self, zone_id):
        """Stop and remove a zone. Waits for stop to complete before deleting."""
        with self._lock:
            zone = self.zones.get(zone_id)
            if not zone:
                return False
        
        # If zone is running, stop it and wait for completion
        if zone.status in (Zone.STATUS_RUNNING, Zone.STATUS_STARTING, Zone.STATUS_STOPPING):
            self.stop_zone(zone_id)
            # Wait for zone to actually stop (up to 30 seconds)
            for _ in range(60):
                if zone.status == Zone.STATUS_STOPPED:
                    break
                time.sleep(0.5)
            else:
                log.warning("Zone %s did not stop in time, deleting anyway", zone_id)
        
        # Prevent the background stop thread from emitting 'stopped' and reviving the zone on the UI
        zone.on_status_change = None 
        
        with self._lock:
            self.zones.pop(zone_id, None)
        
        self.config_store.delete_zone(zone_id)
        if self.socketio:
            self.socketio.emit("zone_deleted", {"zone_id": zone_id})
        log.info("Deleted zone %s", zone_id)
        return True

    def update_zone_config(self, zone_id, updates, restart_if_running=False):
        """Update zone config (name, interface, etc.). 
        If restart_if_running=True and zone is running, it will be restarted."""
        with self._lock:
            zone = self.zones.get(zone_id)
            if not zone:
                return None, False
            
            was_running = zone.status == Zone.STATUS_RUNNING
            
            if was_running and not restart_if_running:
                return None, False
            
            zone.config.update(updates)
        
        self.config_store.save_zone(zone_id, zone.config)
        self._emit_zone_status(zone)
        
        # If zone was running, restart it to apply changes
        needs_restart = was_running and restart_if_running
        if needs_restart:
            log.info("Restarting zone %s to apply config changes", zone_id)
            self.stop_zone(zone_id)
            # Start in background after stop completes
            def restart_after_stop():
                for _ in range(60):  # Wait up to 30 seconds
                    if zone.status == Zone.STATUS_STOPPED:
                        self.start_zone(zone_id)
                        return
                    time.sleep(0.5)
                log.warning("Zone %s did not stop in time for restart", zone_id)
            threading.Thread(target=restart_after_stop, daemon=True).start()
        
        return zone, needs_restart

    def get_zone(self, zone_id):
        with self._lock:
            return self.zones.get(zone_id)

    def list_zones(self):
        with self._lock:
            return list(self.zones.values())

    def load_saved_zones(self):
        """Load zones from persistent config (called on startup)."""
        saved = self.config_store.list_zones()
        for zone_id, config in saved.items():
            zone = Zone(zone_id, config, on_status_change=self._emit_zone_status)
            with self._lock:
                self.zones[zone_id] = zone
            log.info("Loaded saved zone: %s (%s)", zone_id, config.get("name"))

    # -------------------------------------------------------------------------
    # Speaker management (consolidated from routes + startup restore)
    # -------------------------------------------------------------------------

    def get_speakers(self, zone_id):
        """Get available speakers for a zone. Returns (speakers, error)."""
        zone = self.get_zone(zone_id)
        if not zone or not zone.owntone_api:
            return None, "Zone not running or not found"
        outputs = zone.owntone_api.get_outputs()
        return outputs, None

    def set_speakers(self, zone_id, speaker_ids):
        """Set active speakers for a zone and persist selection. Returns (ok, error)."""
        zone = self.get_zone(zone_id)
        if not zone or not zone.owntone_api:
            return False, "Zone not running or not found"

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
        self.config_store.save_zone(zone_id, zone.config)

        return True, None

    def toggle_speaker(self, zone_id, speaker_id, enabled):
        """Toggle a single speaker on/off and persist selection. Returns (ok, error)."""
        zone = self.get_zone(zone_id)
        if not zone or not zone.owntone_api:
            return False, "Zone not running or not found"

        if enabled:
            zone.owntone_api.enable_output(speaker_id)
        else:
            zone.owntone_api.disable_output(speaker_id)

        # CRITICAL: Save current speaker selection to config for persistence
        # Without this, speaker selections are lost on zone restart!
        try:
            outputs = zone.owntone_api.get_outputs()
            selected_speakers = []
            selected_ids = []
            for out in outputs:
                if out.get("selected"):
                    selected_ids.append(out.get("id"))
                    selected_speakers.append({
                        "id": out.get("id"),
                        "name": out.get("name", "Unknown")
                    })
            zone.config["speakers"] = selected_ids
            zone.config["speaker_names"] = selected_speakers
            self.config_store.save_zone(zone_id, zone.config)
        except Exception as e:
            log.warning("Failed to save speaker selection: %s", e)

        return True, None

    # -------------------------------------------------------------------------
    # Volume management
    # -------------------------------------------------------------------------

    def get_volume(self, zone_id):
        """Get master volume for a zone. Returns (volume, error)."""
        zone = self.get_zone(zone_id)
        if not zone or not zone.owntone_api:
            return None, "Zone not running or not found"
        volume = zone.owntone_api.get_volume()
        return volume, None

    def set_volume(self, zone_id, volume):
        """Set master volume for a zone. Returns (ok, error)."""
        zone = self.get_zone(zone_id)
        if not zone or not zone.owntone_api:
            return False, "Zone not running or not found"
        zone.owntone_api.set_volume(volume)
        return True, None

    def set_speaker_volume(self, zone_id, speaker_id, volume):
        """Set volume for a specific speaker. Returns (ok, error)."""
        zone = self.get_zone(zone_id)
        if not zone or not zone.owntone_api:
            return False, "Zone not running or not found"
        zone.owntone_api.set_output_volume(speaker_id, volume)
        return True, None

    # -------------------------------------------------------------------------
    # Latency management
    # -------------------------------------------------------------------------

    def get_latency(self, zone_id):
        """Get current latency offset for a zone. Returns (result_dict, error)."""
        zone = self.get_zone(zone_id)
        if not zone:
            return None, "Zone not found"
        offset = zone.config.get("latency_offset", DEFAULT_LATENCY_OFFSET)
        return {"latency_offset": offset, "default": DEFAULT_LATENCY_OFFSET}, None

    def set_latency(self, zone_id, offset):
        """Set latency offset for a zone. Returns (result_dict, error)."""
        zone = self.get_zone(zone_id)
        if not zone:
            return None, "Zone not found"

        zone.config["latency_offset"] = offset
        self.config_store.save_zone(zone_id, zone.config)
        
        log.info("Set latency_offset=%s for %s (restart zone to apply)", offset, zone_id)
        return {
            "ok": True,
            "latency_offset": offset,
            "note": "Restart zone to apply new latency offset"
        }, None

    # -------------------------------------------------------------------------
    # Player status
    # -------------------------------------------------------------------------

    def get_player_status(self, zone_id):
        """Get player status for a zone. Returns (status_dict, error)."""
        zone = self.get_zone(zone_id)
        if not zone or not zone.owntone_api:
            return None, "Zone not running or not found"
        status = zone.owntone_api.get_player_status()
        return status, None

    # -------------------------------------------------------------------------
    # Zone lifecycle — delegates to zone_lifecycle.py
    # -------------------------------------------------------------------------

    def start_zone(self, zone_id):
        """Start a zone in a background thread."""
        zone = self.get_zone(zone_id)
        if not zone:
            return False
        if zone.status != Zone.STATUS_STOPPED:
            return False

        zone._set_status(Zone.STATUS_STARTING)
        t = threading.Thread(
            target=start_zone_thread, args=(zone, cleanup_zone),
            daemon=True, name=f"start-{zone_id}")
        t.start()
        return True

    def stop_zone(self, zone_id):
        """Stop a running zone."""
        zone = self.get_zone(zone_id)
        if not zone:
            return False
        if zone.status not in (Zone.STATUS_RUNNING, Zone.STATUS_STARTING, Zone.STATUS_ERROR):
            return False

        zone._stop_event.set()
        zone._set_status(Zone.STATUS_STOPPING)
        t = threading.Thread(
            target=stop_zone_thread, args=(zone, cleanup_zone),
            daemon=True, name=f"stop-{zone_id}")
        t.start()
        return True

    # -------------------------------------------------------------------------
    # Diagnostic monitoring for AirPlay disconnect debugging
    # -------------------------------------------------------------------------

    def start_diagnostic_monitor(self):
        """Start background thread that polls OwnTone player state for all running zones."""
        self._diag_stop = threading.Event()
        self._diag_last_state = {}  # zone_id -> last known state dict
        t = threading.Thread(target=self._diagnostic_monitor_loop, daemon=True,
                             name="diag-monitor")
        t.start()
        log.info("[DIAG] Diagnostic monitor started — polling OwnTone player state every 2s")

    def _diagnostic_monitor_loop(self):
        """Poll each running zone's OwnTone player status and log changes."""
        diag = logging.getLogger("shiri.diag")
        diag.setLevel(logging.DEBUG)

        while not self._diag_stop.is_set():
            for zone_id, zone in list(self.zones.items()):
                if zone.status != Zone.STATUS_RUNNING or not zone.owntone_api:
                    continue
                try:
                    player = zone.owntone_api.get_player_status()
                    if not player:
                        prev = self._diag_last_state.get(zone_id, {})
                        if prev:
                            diag.warning("[DIAG][%s] OwnTone API returned None (was: state=%s)",
                                         zone.display_name, prev.get("state"))
                        self._diag_last_state[zone_id] = {}
                        continue

                    state = player.get("state", "unknown")
                    volume = player.get("volume", -1)
                    item_id = player.get("item_id", 0)

                    prev = self._diag_last_state.get(zone_id, {})
                    prev_state = prev.get("state")
                    prev_volume = prev.get("volume", -1)
                    prev_item = prev.get("item_id", 0)

                    # Log any change in state, volume, or track
                    if state != prev_state:
                        diag.info("[DIAG][%s] PLAYER STATE CHANGED: %s -> %s (vol=%s, item=%s)",
                                  zone.display_name, prev_state, state, volume, item_id)
                    if volume != prev_volume and prev_volume != -1:
                        diag.info("[DIAG][%s] VOLUME CHANGED: %s -> %s (state=%s)",
                                  zone.display_name, prev_volume, volume, state)
                    if item_id != prev_item and prev_item != 0:
                        diag.info("[DIAG][%s] ITEM CHANGED: %s -> %s (state=%s)",
                                  zone.display_name, prev_item, item_id, state)

                    self._diag_last_state[zone_id] = {
                        "state": state, "volume": volume, "item_id": item_id
                    }

                    # Check process liveness
                    for label, pid in [("shairport-sync", zone.shairport_pid),
                                       ("arecord-supervisor", zone.arecord_supervisor_pid),
                                       ("pause-bridge", zone.pause_bridge_pid)]:
                        if pid is None:
                            continue
                        alive_key = f"{zone_id}_{label}_alive"
                        try:
                            os.kill(pid, 0)
                            was_dead = self._diag_last_state.get(alive_key) == False
                            if was_dead:
                                diag.info("[DIAG][%s] %s (pid %d) is ALIVE again", zone.display_name, label, pid)
                            self._diag_last_state[alive_key] = True
                        except ProcessLookupError:
                            was_alive = self._diag_last_state.get(alive_key, True)
                            if was_alive:
                                diag.error("[DIAG][%s] %s (pid %d) DIED!", zone.display_name, label, pid)
                            self._diag_last_state[alive_key] = False

                except Exception as e:
                    diag.warning("[DIAG][%s] Poll error: %s", zone.display_name, e)

            self._diag_stop.wait(2)

    def stop_diagnostic_monitor(self):
        if hasattr(self, '_diag_stop'):
            self._diag_stop.set()

    # -------------------------------------------------------------------------
    # Event emission
    # -------------------------------------------------------------------------

    def _emit_zone_status(self, zone):
        """Emit zone status change via SocketIO."""
        if self.socketio:
            self.socketio.emit("zone_status", zone.to_dict())

    # -------------------------------------------------------------------------
    # Shutdown
    # -------------------------------------------------------------------------

    def shutdown(self):
        """Stop all zones gracefully."""
        log.info("Shutting down all zones...")
        self.stop_diagnostic_monitor()
        for zone_id in list(self.zones.keys()):
            zone = self.zones[zone_id]
            if zone.status in (Zone.STATUS_RUNNING, Zone.STATUS_STARTING):
                cleanup_zone(zone)
                zone._set_status(Zone.STATUS_STOPPED)
        log.info("All zones stopped")
