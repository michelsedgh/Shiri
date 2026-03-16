"""
zone_manager.py — Core zone lifecycle management for Shiri.

Translates dual_zone_demo.sh into Python, calling the SAME system commands.
All the intricate audio pipeline logic (ALSA loopback, arecord supervisor,
netns+macvlan, PTP sync via shared nqptp) is preserved exactly.

The existing pause_bridge.sh and volume_bridge.sh scripts are launched
as subprocesses — they are NOT reimplemented.
"""

import logging
import os
import signal
import subprocess
import textwrap
import threading
import time
import uuid

from owntone_api import OwnToneAPI

log = logging.getLogger("shiri.zone")

BASE_DIR = "/var/lib/shiri"
LOOPBACK_LOCK_DIR = os.path.join(BASE_DIR, "loopback")

# Path to the existing scripts (relative to where the daemon runs)
SCRIPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "multiroom-demo")

# Default latency offset for timeline/lyrics sync
# Negative = audio delivered EARLIER to compensate for pipeline buffer delay
# Tune this if lyrics on iPhone are ahead/behind speaker audio
DEFAULT_LATENCY_OFFSET = -2.3


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
        self.nqptp_pid = None  # Shared, tracked by ZoneManager
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


def _run(cmd, check=False, **kwargs):
    """Run a command, log it, return CompletedProcess."""
    log.debug("Running: %s", " ".join(cmd) if isinstance(cmd, list) else cmd)
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def _kill_pid(pid, label="process"):
    """Gracefully kill a PID (TERM then KILL)."""
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGTERM)
        log.info("Sent SIGTERM to %s (pid %d)", label, pid)
    except ProcessLookupError:
        return
    time.sleep(1)
    try:
        os.kill(pid, signal.SIGKILL)
        log.info("Sent SIGKILL to %s (pid %d)", label, pid)
    except ProcessLookupError:
        pass


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
        Same as start_host_nqptp() in dual_zone_demo.sh.
        ONE shared nqptp instance for ALL zones — CRITICAL for multi-room sync.
        """
        # Check if already running
        result = _run(["pgrep", "-x", "nqptp"])
        if result.returncode == 0 and result.stdout.strip():
            self._host_nqptp_pid = int(result.stdout.strip().split()[0])
            log.info("nqptp already running (pid %d) — reusing for shared timing",
                      self._host_nqptp_pid)
            return True

        log.info("Starting shared nqptp on HOST (CRITICAL for multi-room sync)...")
        proc = subprocess.Popen(["nqptp"], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        self._host_nqptp_pid = proc.pid
        time.sleep(1)

        # Verify it started
        try:
            os.kill(proc.pid, 0)
        except ProcessLookupError:
            log.error("Failed to start nqptp — check if ports 319/320 are available")
            return False

        # Verify shared memory
        if not os.path.exists("/dev/shm/nqptp"):
            log.error("nqptp started but /dev/shm/nqptp not created")
            return False

        log.info("Host nqptp started (pid %d) — /dev/shm/nqptp ready", proc.pid)
        return True

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
        nqptp_running = False
        if self._host_nqptp_pid:
            try:
                os.kill(self._host_nqptp_pid, 0)
                nqptp_running = True
            except ProcessLookupError:
                pass

        return {
            "nqptp_running": nqptp_running,
            "nqptp_pid": self._host_nqptp_pid,
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
    # Zone lifecycle — START
    # Follows the exact same sequence as dual_zone_demo.sh's main()
    # -------------------------------------------------------------------------

    def start_zone(self, zone_id):
        """Start a zone in a background thread."""
        zone = self.get_zone(zone_id)
        if not zone:
            return False
        if zone.status != Zone.STATUS_STOPPED:
            return False

        zone._set_status(Zone.STATUS_STARTING)
        t = threading.Thread(target=self._start_zone_thread, args=(zone,),
                             daemon=True, name=f"start-{zone_id}")
        t.start()
        return True

    def _start_zone_thread(self, zone):
        """
        Full zone startup sequence. Same order as dual_zone_demo.sh main():
        1. Allocate loopback subdevice
        2. Setup directories & FIFOs
        3. Generate configs
        4. Start OwnTone in netns
        5. Wait for OwnTone, rescan library, verify pipe
        6. Start shairport-sync on host
        7. Start pause bridge
        """
        try:
            if not zone.interface:
                zone._set_status(Zone.STATUS_ERROR, "No network interface configured")
                return

            # Step 1: Allocate loopback subdevice
            # Same file-locking as allocate_loopback_subdevice() in demo.sh
            subdev = self._allocate_loopback_subdevice()
            if subdev is None:
                zone._set_status(Zone.STATUS_ERROR, "No free loopback subdevices")
                return
            zone.allocated_subdevice = subdev

            # Step 2: Setup directories & FIFOs
            # Same as setup_directories() in demo.sh
            self._setup_directories(zone)

            # Step 3: Generate configs
            # Same templates as generate_shairport_config() and generate_owntone_config()
            self._generate_shairport_config(zone)
            self._generate_owntone_config(zone)

            # Step 4: Start OwnTone in netns FIRST (same as demo.sh order)
            self._start_owntone_in_netns(zone)

            if zone._stop_event.is_set():
                return

            # Step 5: Wait for OwnTone to be ready
            if not self._wait_for_owntone(zone):
                log.warning("OwnTone not ready for %s, continuing anyway", zone.zone_id)

            # Trigger library rescan so OwnTone finds the pipes
            if zone.owntone_api:
                zone.owntone_api.rescan_library()
                time.sleep(3)

                # Verify pipe discovery
                found, tracks = zone.owntone_api.verify_pipe()
                if found:
                    log.info("OwnTone %s found the audio pipe!", zone.zone_id)
                else:
                    log.warning("OwnTone %s has NOT discovered audio pipe yet", zone.zone_id)

            if zone._stop_event.is_set():
                return

            # Note: metadata_relay disabled - shairport writes directly to pause_bridge.metadata
            # which pause_bridge.sh reads. OwnTone metadata (for lyrics) not currently supported.
            self._start_pause_bridge(zone)
            self._start_shairport_on_host(zone)

            # Brief pause for avahi registration
            time.sleep(2)

            # Restore saved speaker selections with retry loop
            # AirPlay speaker discovery via mDNS can take 5-15 seconds
            if zone.owntone_api and (zone.config.get("speakers") or zone.config.get("speaker_names")):
                speaker_names = zone.config.get("speaker_names", [])
                speaker_ids = zone.config.get("speakers", [])
                saved_names = [s.get("name") for s in speaker_names if s.get("name")]
                
                log.info("Waiting for speakers to appear: %s", saved_names or speaker_ids)
                
                # Retry up to 10 times (20 seconds total) for speakers to be discovered
                for attempt in range(10):
                    try:
                        time.sleep(2)
                        available_outputs = zone.owntone_api.get_outputs()
                        available_by_name = {o.get("name"): o.get("id") for o in available_outputs}
                        available_by_id = {str(o.get("id")): o.get("id") for o in available_outputs}
                        
                        # Try to match by name first (more reliable)
                        matched_ids = []
                        for saved in speaker_names:
                            name = saved.get("name")
                            if name and name in available_by_name:
                                matched_ids.append(available_by_name[name])
                                log.info("Matched speaker by name: %s -> %s", name, available_by_name[name])
                        
                        # Fall back to ID matching
                        if not matched_ids and speaker_ids:
                            for sid in speaker_ids:
                                if str(sid) in available_by_id:
                                    matched_ids.append(available_by_id[str(sid)])
                                    log.info("Matched speaker by ID: %s", sid)
                        
                        if matched_ids:
                            zone.owntone_api.set_outputs(matched_ids)
                            log.info("Restored %d speakers for %s (attempt %d)", 
                                     len(matched_ids), zone.zone_id, attempt + 1)
                            break
                        else:
                            # Check if we found ANY of the saved speakers
                            found_any = any(name in available_by_name for name in saved_names)
                            if not found_any and attempt < 9:
                                log.debug("Speakers not yet discovered (attempt %d), available: %s",
                                          attempt + 1, list(available_by_name.keys()))
                                continue
                            log.warning("Could not find saved speakers for %s. Available: %s, Wanted: %s",
                                        zone.zone_id, list(available_by_name.keys()), saved_names or speaker_ids)
                            break
                    except Exception as e:
                        log.warning("Speaker restore attempt %d failed: %s", attempt + 1, e)
                        if attempt >= 9:
                            log.error("Gave up restoring speakers after 10 attempts")

            zone._set_status(Zone.STATUS_RUNNING)
            log.info("Zone %s is RUNNING! AirPlay name: '%s'",
                      zone.zone_id, zone.display_name)

        except Exception as e:
            log.exception("Failed to start zone %s", zone.zone_id)
            zone._set_status(Zone.STATUS_ERROR, str(e))
            self._cleanup_zone(zone)

    # -------------------------------------------------------------------------
    # Zone lifecycle — STOP
    # Same cleanup sequence as cleanup() in dual_zone_demo.sh
    # -------------------------------------------------------------------------

    def stop_zone(self, zone_id):
        """Stop a running zone."""
        zone = self.get_zone(zone_id)
        if not zone:
            return False
        if zone.status not in (Zone.STATUS_RUNNING, Zone.STATUS_STARTING, Zone.STATUS_ERROR):
            return False

        zone._stop_event.set()
        zone._set_status(Zone.STATUS_STOPPING)
        t = threading.Thread(target=self._stop_zone_thread, args=(zone,),
                             daemon=True, name=f"stop-{zone_id}")
        t.start()
        return True

    def _stop_zone_thread(self, zone):
        """Full zone cleanup — same order as cleanup() in dual_zone_demo.sh."""
        try:
            self._cleanup_zone(zone)
            zone._set_status(Zone.STATUS_STOPPED)
            zone._stop_event.clear()
            log.info("Zone %s stopped", zone.zone_id)
        except Exception as e:
            log.exception("Error stopping zone %s", zone.zone_id)
            zone._set_status(Zone.STATUS_ERROR, f"Cleanup error: {e}")

    def _cleanup_zone(self, zone):
        """
        Same as cleanup() in dual_zone_demo.sh:
        1. Release loopback subdevice
        2. Stop HOST processes (shairport-sync, arecord)
        3. Stop pause bridge
        4. Release DHCP leases in namespace
        5. Stop services in namespace
        6. Force kill remaining
        7. Tear down netns + macvlan
        """
        log.info("Cleaning up zone %s...", zone.zone_id)

        # 1. Release loopback subdevice
        if zone.allocated_subdevice is not None:
            lock_file = os.path.join(LOOPBACK_LOCK_DIR,
                                      f"subdev_{zone.allocated_subdevice}.lock")
            try:
                os.remove(lock_file)
                log.info("Released loopback subdevice %d", zone.allocated_subdevice)
            except FileNotFoundError:
                pass
            zone.allocated_subdevice = None

        # 2. Stop HOST processes
        _kill_pid(zone.shairport_pid, f"shairport-sync ({zone.zone_id})")
        _kill_pid(zone.arecord_supervisor_pid, f"arecord supervisor ({zone.zone_id})")
        _kill_pid(zone.metadata_relay_pid, f"metadata relay ({zone.zone_id})")
        zone.shairport_pid = None
        zone.arecord_supervisor_pid = None
        zone.metadata_relay_pid = None

        # 3. Stop pause bridge
        _kill_pid(zone.pause_bridge_pid, f"pause_bridge ({zone.zone_id})")
        zone.pause_bridge_pid = None

        # 4-5. Cleanup in OwnTone namespace
        ns = zone.netns_name
        if ns:
            # Check if namespace still exists
            result = _run(["ip", "netns", "list"])
            if ns in (result.stdout or ""):
                # Release DHCP lease (same as demo cleanup)
                result = _run(["ip", "netns", "exec", ns, "ip", "-o", "link", "show"])
                iface_match = None
                for line in (result.stdout or "").splitlines():
                    if "ot_" in line:
                        parts = line.split(": ")
                        if len(parts) >= 2:
                            iface_match = parts[1].split("@")[0]
                            break
                if iface_match:
                    _run(["ip", "netns", "exec", ns, "dhclient", "-r", iface_match])
                    log.info("Released DHCP lease on %s in %s", iface_match, ns)

                # Stop services
                _run(["ip", "netns", "exec", ns, "pkill", "-TERM", "avahi-daemon"])
                _run(["ip", "netns", "exec", ns, "pkill", "-TERM", "owntone"])

        # Give services time to shut down
        time.sleep(2)

        # 6. Force kill remaining namespace processes
        if ns:
            result = _run(["ip", "netns", "list"])
            if ns in (result.stdout or ""):
                result = _run(["ip", "netns", "pids", ns])
                for pid_str in (result.stdout or "").split():
                    try:
                        os.kill(int(pid_str), signal.SIGKILL)
                    except (ProcessLookupError, ValueError):
                        pass
                time.sleep(0.3)

                # Delete namespace
                _run(["ip", "netns", "delete", ns])
                log.info("Deleted netns %s", ns)

        # Delete macvlan interface
        if zone.macvlan_if:
            result = _run(["ip", "link", "show", zone.macvlan_if])
            if result.returncode == 0:
                _run(["ip", "link", "delete", zone.macvlan_if])
                log.info("Deleted macvlan %s", zone.macvlan_if)

        # Reset state
        zone.netns_name = None
        zone.macvlan_if = None
        zone.shairport_ip = None
        zone.owntone_ip = None
        zone.owntone_api = None

        log.info("Zone %s cleanup complete", zone.zone_id)

    # -------------------------------------------------------------------------
    # Internal helpers — each maps to a function in dual_zone_demo.sh
    # -------------------------------------------------------------------------

    def _allocate_loopback_subdevice(self):
        """
        Same as allocate_loopback_subdevice() in dual_zone_demo.sh.
        File-based locking in /var/lib/shiri/loopback/.
        """
        os.makedirs(LOOPBACK_LOCK_DIR, exist_ok=True)
        my_pid = str(os.getpid())

        for i in range(16):
            lock_file = os.path.join(LOOPBACK_LOCK_DIR, f"subdev_{i}.lock")
            try:
                # Try exclusive create (same as set -o noclobber in bash)
                fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, my_pid.encode())
                os.close(fd)
                log.info("Allocated loopback subdevice %d", i)
                return i
            except FileExistsError:
                # Check if stale
                try:
                    with open(lock_file, "r") as f:
                        pid = f.read().strip()
                    if pid and not os.path.exists(f"/proc/{pid}"):
                        # Stale lock, remove and claim
                        os.remove(lock_file)
                        fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                        os.write(fd, my_pid.encode())
                        os.close(fd)
                        log.info("Allocated loopback subdevice %d (reclaimed stale)", i)
                        return i
                except (IOError, FileExistsError):
                    continue

        log.error("No free loopback subdevices available (all 16 in use)")
        return None

    def _setup_directories(self, zone):
        """
        Same as setup_directories() in dual_zone_demo.sh.
        Creates dirs, clears stale state, creates FIFOs.
        """
        grp_dir = zone.grp_dir
        for subdir in ["pipes", "config", "logs", "state"]:
            os.makedirs(os.path.join(grp_dir, subdir), exist_ok=True)
            os.chmod(os.path.join(grp_dir, subdir), 0o755)

        # Clear stale state and logs
        for subdir in ["state", "logs"]:
            for f in os.listdir(os.path.join(grp_dir, subdir)):
                try:
                    os.remove(os.path.join(grp_dir, subdir, f))
                except OSError:
                    pass

        # Create FIFOs (same as demo.sh)
        audio_pipe = os.path.join(grp_dir, "pipes", "audio.pipe")
        meta_pipe = os.path.join(grp_dir, "pipes", "audio.pipe.metadata")
        pause_meta_pipe = os.path.join(grp_dir, "pipes", "pause_bridge.metadata")
        shairport_meta_pipe = os.path.join(grp_dir, "pipes", "shairport.metadata")
        format_file = os.path.join(grp_dir, "pipes", "audio.pipe.format")

        for pipe in [audio_pipe, meta_pipe, pause_meta_pipe, shairport_meta_pipe, format_file]:
            try:
                os.remove(pipe)
            except FileNotFoundError:
                pass

        for pipe in [audio_pipe, meta_pipe, pause_meta_pipe, shairport_meta_pipe]:
            os.mkfifo(pipe, 0o666)
            os.chmod(pipe, 0o666)

        # Format file — OwnTone REQUIRES this to know the pipe's audio format
        with open(format_file, "w") as f:
            f.write("16,44100,2\n")

        log.info("Created directories and FIFOs for %s", zone.zone_id)

    def _generate_shairport_config(self, zone):
        """
        Same as generate_shairport_config() in dual_zone_demo.sh.
        Generates the EXACT SAME config template with the same parameters.
        """
        grp_dir = zone.grp_dir
        conf_path = os.path.join(grp_dir, "config", "shairport-sync.conf")
        subdev = zone.allocated_subdevice
        alsa_device = f"hw:Loopback,0,{subdev}"
        device_offset = subdev + 1
        port = 7000 + subdev
        udp_port_base = 6001 + subdev * 100

        volume_bridge_script = os.path.join(SCRIPT_DIR, "volume_bridge.sh")
        os.chmod(volume_bridge_script, 0o755)

        # Get latency offset from zone config, or use default
        # This can be tuned per-zone if needed
        latency_offset = zone.config.get("latency_offset", DEFAULT_LATENCY_OFFSET)
        log.info("Using latency offset: %s seconds for %s", latency_offset, zone.zone_id)

        # Create pipe reset script — CRITICAL FOR MULTI-ROOM SYNC
        # This script:
        # 1. Stops OwnTone playback (flushes its internal buffers)
        # 2. Kills arecord
        # 3. Drains the audio pipe
        # This ensures NO accumulated buffer state between sessions
        flush_script = os.path.join(grp_dir, "config", "reset_audio_pipe.sh")
        with open(flush_script, "w") as f:
            f.write(textwrap.dedent(f"""\
                #!/bin/bash
                # Reset audio pipeline completely for sync
                # Called by shairport-sync BEFORE play begins
                # CRITICAL: Must flush OwnTone buffers to prevent cumulative drift!

                PIPE="{grp_dir}/pipes/audio.pipe"
                ARECORD_PID_FILE="{grp_dir}/state/arecord.pid"
                OWNTONE_IP_FILE="{grp_dir}/state/owntone_ip.txt"
                OWNTONE_NETNS_FILE="{grp_dir}/state/owntone_netns.txt"
                LOG="{grp_dir}/logs/sync_reset.log"
                TIMESTAMP=$(date '+%H:%M:%S.%3N')

                echo "" >> "$LOG"
                echo "[$TIMESTAMP] ========== SYNC RESET TRIGGERED ==========" >> "$LOG"

                # Step 1: STOP OwnTone playback to flush its internal buffers
                # This is CRITICAL - without this, OwnTone accumulates delay each reconnect
                if [[ -f "$OWNTONE_IP_FILE" ]]; then
                  OWNTONE_IP=$(cat "$OWNTONE_IP_FILE")
                  OWNTONE_NETNS=""
                  if [[ -f "$OWNTONE_NETNS_FILE" ]]; then
                    OWNTONE_NETNS=$(cat "$OWNTONE_NETNS_FILE")
                  fi
                  
                  echo "[$TIMESTAMP] Stopping OwnTone playback (flush buffers)" >> "$LOG"
                  if [[ -n "$OWNTONE_NETNS" ]]; then
                    ip netns exec "$OWNTONE_NETNS" curl -s -X PUT "http://$OWNTONE_IP:3689/api/player/stop" --connect-timeout 1 >> "$LOG" 2>&1 || true
                  else
                    curl -s -X PUT "http://$OWNTONE_IP:3689/api/player/stop" --connect-timeout 1 >> "$LOG" 2>&1 || true
                  fi
                  echo "[$TIMESTAMP] OwnTone stopped" >> "$LOG"
                else
                  echo "[$TIMESTAMP] WARNING: OwnTone IP not found, cannot flush" >> "$LOG"
                fi

                # Step 2: Kill arecord to stop writing to pipe
                if [[ -f "$ARECORD_PID_FILE" ]]; then
                  ARECORD_PID=$(cat "$ARECORD_PID_FILE")
                  echo "[$TIMESTAMP] Found arecord PID file: $ARECORD_PID" >> "$LOG"
                  if kill -0 "$ARECORD_PID" 2>/dev/null; then
                    echo "[$TIMESTAMP] Killing arecord (pid $ARECORD_PID)" >> "$LOG"
                    kill -TERM "$ARECORD_PID" 2>/dev/null || true
                    sleep 0.1
                    echo "[$TIMESTAMP] arecord killed" >> "$LOG"
                  else
                    echo "[$TIMESTAMP] arecord (pid $ARECORD_PID) not running" >> "$LOG"
                  fi
                else
                  echo "[$TIMESTAMP] WARNING: arecord PID file not found!" >> "$LOG"
                fi

                # Step 3: Drain any buffered data from the pipe
                if [[ -p "$PIPE" ]]; then
                  DRAINED=$(timeout 0.2 dd if="$PIPE" of=/dev/null bs=65536 iflag=nonblock 2>&1 | grep -oP '\\d+ bytes' || echo "0 bytes")
                  echo "[$TIMESTAMP] Drained pipe: $DRAINED" >> "$LOG"
                fi

                echo "[$TIMESTAMP] Reset complete - pipeline flushed" >> "$LOG"
                echo "[$TIMESTAMP] ==========================================" >> "$LOG"
            """))
        os.chmod(flush_script, 0o755)

        # Generate shairport-sync config — SAME template as dual_zone_demo.sh
        with open(conf_path, "w") as f:
            f.write(textwrap.dedent(f"""\
                // shairport-sync.conf for {zone.zone_id}
                general =
                {{
                  name = "{zone.display_name}";
                  interpolation = "soxr";  // High-quality resampling for sync
                  output_backend = "alsa"; // ALSA backend enables PTP clock sync
                  mdns_backend = "avahi";

                  // CRITICAL FOR MULTI-INSTANCE SYNC:
                  port = {port};
                  udp_port_base = {udp_port_base};
                  udp_port_range = 100;
                  airplay_device_id_offset = {device_offset};

                  // Tighter sync tolerances for multi-room
                  drift_tolerance_in_seconds = 0.001;
                  resync_threshold_in_seconds = 0.025;
                  resync_recovery_time_in_seconds = 0.050;

                  // LYRICS/VIDEO SYNC FIX:
                  // Negative = deliver audio EARLIER to compensate for buffer delays
                  // If lyrics are AHEAD of speaker: make more negative (e.g. -2.5)
                  // If speaker is AHEAD of lyrics: make less negative (e.g. -2.0)
                  audio_backend_latency_offset_in_seconds = {latency_offset};

                  // INSTANT VOLUME CONTROL via OwnTone:
                  ignore_volume_control = "yes";

                  // Hook for volume changes — calls existing volume_bridge.sh
                  run_this_when_volume_is_set = "{volume_bridge_script} {grp_dir} ";
                }};

                alsa =
                {{
                  output_device = "{alsa_device}";
                  disable_standby_mode = "always";
                }};

                // CRITICAL: Session control hooks to flush pipes on play start/stop
                sessioncontrol =
                {{
                  run_this_before_play_begins = "{flush_script}";
                  // REMOVED: run_this_after_play_ends - causes double-reset race on reconnect
                  // Only resetting on play_begin is sufficient for sync
                  wait_for_completion = "yes";
                }};

                diagnostics =
                {{
                  statistics = "yes";
                  log_verbosity = 1;
                }};

                airplay =
                {{
                }};

                metadata =
                {{
                  enabled = "yes";
                  include_cover_art = "no";
                  pipe_name = "{grp_dir}/pipes/pause_bridge.metadata";
                  pipe_timeout = 5000;
                }};
            """))
        log.info("Generated shairport-sync config for %s at %s", zone.zone_id, conf_path)
        log.info("  -> latency_offset=%s, port=%d, alsa_device=%s", latency_offset, port, alsa_device)
        
        # Verify config was written correctly by reading it back
        with open(conf_path, "r") as f:
            config_content = f.read()
            if "audio_backend_latency_offset_in_seconds" in config_content:
                log.info("  -> Config verification: latency offset line found ✓")
            else:
                log.error("  -> Config verification FAILED: latency offset NOT in config!")

    def _generate_owntone_config(self, zone):
        """
        Same as generate_owntone_config() in dual_zone_demo.sh.
        """
        grp_dir = zone.grp_dir
        conf_path = os.path.join(grp_dir, "config", "owntone.conf")

        # Ensure cache dir exists
        os.makedirs(os.path.join(grp_dir, "state", "cache"), exist_ok=True)

        with open(conf_path, "w") as f:
            f.write(textwrap.dedent(f"""\
                # OwnTone config for {zone.zone_id} (runs in network namespace)

                general {{
                \tuid = "root"
                \tdb_path = "{grp_dir}/state/songs3.db"
                \tlogfile = "{grp_dir}/logs/owntone.log"
                \tloglevel = log
                \tadmin_password = ""
                \twebsocket_port = 3688
                \tcache_dir = "{grp_dir}/state/cache"
                \tcache_daap_threshold = 1000
                \tspeaker_autoselect = no
                \thigh_resolution_clock = yes
                }}

                library {{
                \tname = "{zone.display_name} Library"
                \tport = 3689
                \tdirectories = {{ "{grp_dir}/pipes" }}
                \tfollow_symlinks = false
                \tfilescan_disable = false
                \tpipe_autostart = true
                \tclear_queue_on_stop_disable = true
                }}

                audio {{
                \ttype = "alsa"
                \tcard = "default"
                \tnickname = "Local Output"
                }}

                mpd {{
                \tport = 6600
                }}

                streaming {{
                \tsample_rate = 44100
                \tbit_rate = 192
                }}
            """))
        log.info("Generated OwnTone config for %s", zone.zone_id)

    def _start_owntone_in_netns(self, zone):
        """
        Same as start_owntone_in_netns() in dual_zone_demo.sh.
        Creates netns + macvlan, runs the wrapper script inside.
        """
        grp_dir = zone.grp_dir
        iface = zone.interface

        suffix = str(int(time.time() * 1e9))[-9:]
        ns_name = f"owntone_{zone.zone_id}_{suffix}"
        mv_if = f"ot_{zone.zone_id[:6]}_{suffix[:5]}"

        zone.netns_name = ns_name
        zone.macvlan_if = mv_if

        log.info("Creating OwnTone netns %s for %s", ns_name, zone.zone_id)

        # Create namespace and macvlan — same commands as demo.sh
        _run(["ip", "netns", "add", ns_name])
        _run(["ip", "link", "add", mv_if, "link", iface, "type", "macvlan", "mode", "bridge"])
        _run(["ip", "link", "set", mv_if, "netns", ns_name])

        # Save netns name so volume_bridge.sh can use it
        with open(os.path.join(grp_dir, "state", "owntone_netns.txt"), "w") as f:
            f.write(ns_name)

        # Create the wrapper script — VERBATIM from dual_zone_demo.sh
        # NOTE: Written as raw string to avoid heredoc indentation issues
        wrapper_path = os.path.join(grp_dir, "config", "owntone_wrapper.sh")
        with open(wrapper_path, "w") as f:
            f.write("""#!/usr/bin/env bash
set -e

MV_IF="$1"
GRP_DIR="$2"
GRP="$3"

ip link set lo up
ip link set "$MV_IF" up

echo "[owntone:$GRP] Running DHCP on $MV_IF ..."
dhclient -v "$MV_IF" \\
  -lf "/run/dhclient.leases" \\
  -pf "/run/dhclient.pid" \\
  2>&1 | head -20 || true
sleep 2

# Get and save the IP address
MY_IP=$(ip -4 addr show "$MV_IF" | grep -oP '(?<=inet\\s)\\d+(\\.\\d+){3}' | head -1)
echo "$MY_IP" > "$GRP_DIR/state/owntone_ip.txt"
echo "[owntone:$GRP] Got IP: $MY_IP"

# Private /run, /tmp, AND /dev/shm for complete isolation
mount -t tmpfs tmpfs /run
mount -t tmpfs tmpfs /tmp
mount -t tmpfs tmpfs /dev/shm
mkdir -p /run/dbus /run/avahi-daemon

echo "[owntone:$GRP] Starting dbus..."
dbus-daemon --system --fork --nopidfile
sleep 1

# Create per-instance avahi config to avoid mDNS conflicts
cat > /tmp/avahi-daemon.conf <<AVAHI_EOF
[server]
host-name=$(hostname)-owntone-$GRP
use-ipv4=yes
use-ipv6=no
allow-interfaces=$MV_IF
deny-interfaces=lo
ratelimit-interval-usec=1000000
ratelimit-burst=1000

[wide-area]
enable-wide-area=no

[publish]
publish-hinfo=no
publish-workstation=no

[reflector]
enable-reflector=no

[rlimits]
AVAHI_EOF

echo "[owntone:$GRP] Starting avahi..."
avahi-daemon --daemonize --no-chroot --no-drop-root --file /tmp/avahi-daemon.conf --no-rlimits 2>/dev/null || true
sleep 1

echo "[owntone:$GRP] Starting OwnTone with Real-Time priority..."
exec chrt -f 50 owntone -f -c "$GRP_DIR/config/owntone.conf"
""")
        os.chmod(wrapper_path, 0o755)

        # Launch wrapper inside netns — same command as demo.sh
        log_path = os.path.join(grp_dir, "logs", "owntone_wrapper.log")
        # Don't use context manager - file must stay open for subprocess lifetime
        log_file = open(log_path, "w")
        proc = subprocess.Popen(
            ["ip", "netns", "exec", ns_name, "unshare", "-m",
             "bash", wrapper_path, mv_if, grp_dir, zone.zone_id],
            stdout=log_file, stderr=subprocess.STDOUT
        )
        zone.owntone_pid = proc.pid
        log.info("Started OwnTone for %s (pid %d)", zone.zone_id, proc.pid)

    def _wait_for_owntone(self, zone, timeout=60):
        """
        Same as wait_for_owntone() in dual_zone_demo.sh.
        Waits for IP file, then polls API.
        """
        ip_file = os.path.join(zone.grp_dir, "state", "owntone_ip.txt")

        log.info("Waiting for OwnTone %s to get IP...", zone.zone_id)
        for _ in range(timeout):
            if zone._stop_event.is_set():
                return False
            if os.path.exists(ip_file):
                with open(ip_file, "r") as f:
                    ip = f.read().strip()
                if ip:
                    zone.owntone_ip = ip
                    log.info("OwnTone %s has IP: %s", zone.zone_id, ip)
                    break
            time.sleep(1)

        if not zone.owntone_ip:
            log.warning("OwnTone %s did not get an IP in time", zone.zone_id)
            return False

        # Create API client
        zone.owntone_api = OwnToneAPI(zone.owntone_ip, zone.netns_name)

        # Wait for API to respond
        log.info("Waiting for OwnTone API at %s:3689...", zone.owntone_ip)
        for i in range(timeout):
            if zone._stop_event.is_set():
                return False
            if zone.owntone_api.is_ready():
                log.info("OwnTone %s is ready", zone.zone_id)
                return True
            if i % 10 == 0 and i > 0:
                log.info("Still waiting for OwnTone API... (%d seconds)", i)
            time.sleep(1)

        log.warning("OwnTone %s API did not become ready in time", zone.zone_id)
        return False

    def _start_shairport_on_host(self, zone):
        """
        Same as start_shairport_on_host() in dual_zone_demo.sh.
        Starts arecord supervisor + shairport-sync on host with RT priority.
        """
        grp_dir = zone.grp_dir
        subdev = zone.allocated_subdevice

        # Get host IP from parent interface
        result = _run(["ip", "-4", "addr", "show", zone.interface])
        host_ip = "unknown"
        for line in (result.stdout or "").splitlines():
            if "inet " in line:
                parts = line.strip().split()
                if len(parts) >= 2:
                    host_ip = parts[1].split("/")[0]
                    break

        with open(os.path.join(grp_dir, "state", "shairport_ip.txt"), "w") as f:
            f.write(host_ip)
        zone.shairport_ip = host_ip

        capture_dev = f"hw:Loopback,1,{subdev}"

        # Start arecord supervisor — SAME bash loop as dual_zone_demo.sh
        # Uses small buffer (2048 frames = ~46ms) to minimize latency/drift
        arecord_supervisor_script = os.path.join(grp_dir, "config", "arecord_supervisor.sh")
        with open(arecord_supervisor_script, "w") as f:
            f.write(textwrap.dedent(f"""\
                #!/bin/bash
                while true; do
                  # Clear stale loopback data before each session
                  timeout 0.1 arecord -D "{capture_dev}" -f cd -c 2 -t raw -d 1 2>/dev/null >/dev/null || true

                  # Start arecord with small buffer for low latency
                  arecord -D "{capture_dev}" -f cd -c 2 -t raw --buffer-size=2048 --period-size=512 2>/dev/null > "{grp_dir}/pipes/audio.pipe" &
                  ARECORD_INNER_PID=$!

                  # Write the actual arecord PID for sync reset script
                  echo "$ARECORD_INNER_PID" > "{grp_dir}/state/arecord.pid"

                  # Wait for arecord to exit (killed by sync reset or pipe close)
                  wait $ARECORD_INNER_PID 2>/dev/null || true

                  echo "[$(date '+%H:%M:%S')] arecord exited, restarting in 0.3s..." >&2
                  sleep 0.3
                done
            """))
        os.chmod(arecord_supervisor_script, 0o755)

        log_path = os.path.join(grp_dir, "logs", "arecord.log")
        # Don't use context manager - file must stay open for subprocess lifetime
        log_file = open(log_path, "w")
        proc = subprocess.Popen(
            ["bash", arecord_supervisor_script],
            stdout=log_file, stderr=subprocess.STDOUT
        )
        zone.arecord_supervisor_pid = proc.pid
        log.info("Started arecord supervisor for %s (pid %d)", zone.zone_id, proc.pid)

        # Start shairport-sync on host with real-time priority — same as demo.sh
        shairport_log = os.path.join(grp_dir, "logs", "shairport.log")
        # Don't use context manager - file must stay open for subprocess lifetime
        log_file = open(shairport_log, "w")
        proc = subprocess.Popen(
            ["chrt", "-f", "50", "shairport-sync",
             "-c", os.path.join(grp_dir, "config", "shairport-sync.conf"),
                 "--statistics"],
                stdout=log_file, stderr=subprocess.STDOUT
            )
        zone.shairport_pid = proc.pid
        log.info("Started shairport-sync for %s (pid %d) — using shared nqptp",
                 zone.zone_id, proc.pid)

    def _start_metadata_relay(self, zone):
        grp_dir = zone.grp_dir
        relay_script = os.path.join(grp_dir, "config", "metadata_relay.py")
        with open(relay_script, "w") as f:
            f.write(textwrap.dedent("""\
                #!/usr/bin/env python3
                \"\"\"
                Metadata relay with timing cache.
                
                OwnTone only opens audio.pipe.metadata AFTER playback starts,
                but Shairport sends prgr (progress/timing) metadata BEFORE audio.
                This causes a race condition where initial timing info is lost.
                
                Fix: Cache the last prgr item and replay it when OwnTone connects.
                \"\"\"
                import datetime
                import errno
                import os
                import sys
                import time

                source_path, pause_path, owntone_path = sys.argv[1:4]
                outputs = [pause_path, owntone_path]
                writers = {path: None for path in outputs}
                
                # Cache for timing-critical metadata (prgr = 70726772 hex)
                cached_prgr = {path: None for path in outputs}

                def log(message):
                    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
                    print(f"[{ts}] {message}", flush=True)

                def close_writer(path):
                    fd = writers.get(path)
                    if fd is None:
                        return
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    writers[path] = None

                def is_prgr_item(item):
                    # prgr code in hex is 70726772
                    return "<code>70726772</code>" in item

                def get_writer(path):
                    fd = writers.get(path)
                    if fd is not None:
                        return fd
                    try:
                        fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
                    except OSError as exc:
                        if exc.errno in (errno.ENXIO, errno.ENOENT):
                            return None
                        raise
                    writers[path] = fd
                    log(f"Connected output: {path}")
                    # On new connection, replay cached prgr if we have one
                    prgr = cached_prgr.get(path)
                    if prgr:
                        try:
                            os.write(fd, prgr.encode("utf-8"))
                            log(f"Replayed cached prgr to {path}")
                        except OSError:
                            pass
                    return fd

                def fanout(payload):
                    # Add newline so pause_bridge.sh can read line-by-line
                    data = (payload + "\\n").encode("utf-8")
                    is_prgr = is_prgr_item(payload)
                    
                    for path in outputs:
                        # Cache prgr items for replay on late connections
                        if is_prgr:
                            cached_prgr[path] = payload + "\\n"
                        
                        try:
                            fd = get_writer(path)
                            if fd is None:
                                continue
                            os.write(fd, data)
                        except OSError as exc:
                            close_writer(path)
                            if exc.errno not in (errno.EPIPE, errno.ENXIO, errno.ENOENT):
                                log(f"Write error on {path}: {exc}")

                log(f"Relay source: {source_path}")
                log(f"Relay outputs: {pause_path}, {owntone_path}")

                buffer = ""
                while True:
                    try:
                        with open(source_path, "r", encoding="utf-8", errors="ignore") as source:
                            log("Source connected")
                            # Clear stale prgr cache on new session
                            for p in outputs:
                                cached_prgr[p] = None
                            while True:
                                chunk = source.read(4096)
                                if not chunk:
                                    log("Source disconnected")
                                    break
                                buffer += chunk
                                while True:
                                    end = buffer.find("</item>")
                                    if end == -1:
                                        if len(buffer) > 1048576:
                                            log("Discarding oversized partial metadata buffer")
                                            buffer = ""
                                        break
                                    item = buffer[:end + len("</item>")]
                                    buffer = buffer[end + len("</item>"):]
                                    fanout(item)
                    except FileNotFoundError:
                        time.sleep(0.2)
                    except Exception as exc:
                        log(f"Relay error: {exc}")
                        time.sleep(0.5)
            """))
        os.chmod(relay_script, 0o755)

        source_pipe = os.path.join(grp_dir, "pipes", "shairport.metadata")
        pause_pipe = os.path.join(grp_dir, "pipes", "pause_bridge.metadata")
        owntone_pipe = os.path.join(grp_dir, "pipes", "audio.pipe.metadata")
        log_path = os.path.join(grp_dir, "logs", "metadata_relay.log")
        with open(log_path, "w") as log_file:
            proc = subprocess.Popen(
                ["python3", relay_script, source_pipe, pause_pipe, owntone_pipe],
                stdout=log_file, stderr=subprocess.STDOUT
            )
        zone.metadata_relay_pid = proc.pid
        log.info("Started metadata relay for %s (pid %d)", zone.zone_id, proc.pid)

    def _start_pause_bridge(self, zone):
        """
        Launches existing multiroom-demo/pause_bridge.sh as a subprocess.
        No reimplementation — the script works perfectly as-is.
        """
        pause_bridge_script = os.path.join(SCRIPT_DIR, "pause_bridge.sh")

        if not os.path.isfile(pause_bridge_script):
            log.warning("pause_bridge.sh not found at %s", pause_bridge_script)
            return

        os.chmod(pause_bridge_script, 0o755)
        log_path = os.path.join(zone.grp_dir, "logs", "pause_bridge.log")
        # Don't use context manager - file must stay open for subprocess lifetime
        log_file = open(log_path, "w")
        proc = subprocess.Popen(
            [pause_bridge_script, zone.grp_dir],
            stdout=log_file, stderr=subprocess.STDOUT
        )
        zone.pause_bridge_pid = proc.pid
        log.info("Started pause bridge for %s (pid %d)", zone.zone_id, proc.pid)

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
        for zone_id in list(self.zones.keys()):
            zone = self.zones[zone_id]
            if zone.status in (Zone.STATUS_RUNNING, Zone.STATUS_STARTING):
                self._cleanup_zone(zone)
                zone._set_status(Zone.STATUS_STOPPED)
        log.info("All zones stopped")
