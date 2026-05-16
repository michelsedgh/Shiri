"""
config.py — All configuration concerns for Shiri.

Two responsibilities:
1. ConfigStore: Persistent zone settings storage (JSON file on disk, thread-safe)
2. Config builder: Reads template files from templates/, substitutes per-zone
   variables using %%PLACEHOLDER%% syntax, writes runtime configs to each zone's
   directory. Also handles directory setup, FIFO creation, and loopback allocation.
"""

import json
import logging
import os
import threading

log = logging.getLogger("shiri.config")

BASE_DIR = "/var/lib/shiri"
LOOPBACK_LOCK_DIR = os.path.join(BASE_DIR, "loopback")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
_LOOPBACK_ALLOC_LOCK = threading.Lock()

# Resolve paths relative to this file's location
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(_THIS_DIR, "templates")
SCRIPT_DIR = os.path.join(_THIS_DIR, "scripts")
MIXER_SCRIPT = os.path.join(_THIS_DIR, "audio_mixer.py")

# Default latency offset for timeline/lyrics sync
# Negative = audio delivered EARLIER to compensate for pipeline buffer delay
# Tune this if lyrics on iPhone are ahead/behind speaker audio
DEFAULT_LATENCY_OFFSET = -2.3


# ===========================================================================
# ConfigStore — persistent zone settings (JSON)
# ===========================================================================

class ConfigStore:
    """Thread-safe JSON config store for zone definitions."""

    def __init__(self, path=CONFIG_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._data = {"zones": {}, "settings": {"default_interface": ""}}
        self._load()

    def _load(self):
        """Load config from disk."""
        if os.path.exists(self.path):
            try:
                with open(self.path, "r") as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        # Ensure structure
        self._data.setdefault("zones", {})
        self._data.setdefault("settings", {"default_interface": ""})

    def _save(self):
        """Write config to disk."""
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self._data, f, indent=2)

    # -- Zone CRUD --

    def list_zones(self):
        """Return dict of all zone configs keyed by zone_id."""
        with self._lock:
            return dict(self._data["zones"])

    def get_zone(self, zone_id):
        """Return config for a single zone, or None."""
        with self._lock:
            return self._data["zones"].get(zone_id)

    def save_zone(self, zone_id, config):
        """Create or update a zone config."""
        with self._lock:
            self._data["zones"][zone_id] = config
            self._save()

    def delete_zone(self, zone_id):
        """Remove a zone config."""
        with self._lock:
            self._data["zones"].pop(zone_id, None)
            self._save()

    # -- Settings --

    def get_settings(self):
        with self._lock:
            return dict(self._data.get("settings", {}))

    def update_settings(self, settings):
        with self._lock:
            self._data["settings"].update(settings)
            self._save()


# ===========================================================================
# Template helpers
# ===========================================================================

def _read_template(name):
    """Read a template file from the templates/ directory."""
    path = os.path.join(TEMPLATE_DIR, name)
    with open(path, "r") as f:
        return f.read()


def _write_file(path, content, executable=False):
    """Write content to a file, optionally making it executable."""
    with open(path, "w") as f:
        f.write(content)
    if executable:
        os.chmod(path, 0o755)


# ===========================================================================
# Directory & FIFO setup
# ===========================================================================

def setup_directories(zone):
    """
    Same as setup_directories() in dual_zone_demo.sh.
    Creates dirs, clears stale state, creates FIFOs.
    """
    grp_dir = zone.grp_dir
    for subdir in ["pipes", "config", "logs", "state", "tts_queue"]:
        os.makedirs(os.path.join(grp_dir, subdir), exist_ok=True)
        os.chmod(os.path.join(grp_dir, subdir), 0o755)

    # Clear stale state, logs, and queued TTS from the last daemon run.
    for subdir in ["state", "logs", "tts_queue"]:
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


# ===========================================================================
# Loopback subdevice allocation
# ===========================================================================

def allocate_loopback_subdevice():
    """
    Same as allocate_loopback_subdevice() in dual_zone_demo.sh.
    File-based locking in /var/lib/shiri/loopback/.
    """
    with _LOOPBACK_ALLOC_LOCK:
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
                try:
                    with open(lock_file, "r") as f:
                        pid = f.read().strip()
                    if not _lock_owner_is_live_shiri(pid):
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


def _lock_owner_is_live_shiri(pid):
    """Return True only when a loopback lock belongs to a live Shiri daemon."""
    if not pid or not pid.isdigit():
        return False
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return False
    return "python" in cmdline and "app.py" in cmdline


def release_loopback_subdevice(subdevice):
    """Release a previously allocated loopback subdevice."""
    if subdevice is None:
        return
    with _LOOPBACK_ALLOC_LOCK:
        lock_file = os.path.join(LOOPBACK_LOCK_DIR, f"subdev_{subdevice}.lock")
        try:
            os.remove(lock_file)
            log.info("Released loopback subdevice %d", subdevice)
        except FileNotFoundError:
            pass


# ===========================================================================
# Config generation — reads templates, substitutes variables, writes output
# ===========================================================================

def generate_shairport_config(zone):
    """
    Same as generate_shairport_config() in dual_zone_demo.sh.
    Generates the EXACT SAME config template with the same parameters.
    """
    grp_dir = zone.grp_dir
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
    reset_template = _read_template("reset_audio_pipe.sh")
    reset_content = reset_template.replace("%%GRP_DIR%%", grp_dir)
    _write_file(flush_script, reset_content, executable=True)

    # Generate shairport-sync config — SAME template as dual_zone_demo.sh
    conf_path = os.path.join(grp_dir, "config", "shairport-sync.conf")
    template = _read_template("shairport_sync.conf")
    content = (template
               .replace("%%ZONE_ID%%", zone.zone_id)
               .replace("%%DISPLAY_NAME%%", zone.display_name)
               .replace("%%PORT%%", str(port))
               .replace("%%UDP_PORT_BASE%%", str(udp_port_base))
               .replace("%%DEVICE_OFFSET%%", str(device_offset))
               .replace("%%LATENCY_OFFSET%%", str(latency_offset))
               .replace("%%VOLUME_BRIDGE_SCRIPT%%", volume_bridge_script)
               .replace("%%GRP_DIR%%", grp_dir)
               .replace("%%ALSA_DEVICE%%", alsa_device)
               .replace("%%FLUSH_SCRIPT%%", flush_script))
    _write_file(conf_path, content)

    log.info("Generated shairport-sync config for %s at %s", zone.zone_id, conf_path)
    log.info("  -> latency_offset=%s, port=%d, alsa_device=%s", latency_offset, port, alsa_device)

    # Verify config was written correctly by reading it back
    with open(conf_path, "r") as f:
        config_content = f.read()
        if "audio_backend_latency_offset_in_seconds" in config_content:
            log.info("  -> Config verification: latency offset line found ✓")
        else:
            log.error("  -> Config verification FAILED: latency offset NOT in config!")


def generate_owntone_config(zone):
    """
    Same as generate_owntone_config() in dual_zone_demo.sh.
    """
    grp_dir = zone.grp_dir
    conf_path = os.path.join(grp_dir, "config", "owntone.conf")

    # Ensure cache dir exists
    os.makedirs(os.path.join(grp_dir, "state", "cache"), exist_ok=True)

    template = _read_template("owntone.conf")
    content = (template
               .replace("%%ZONE_ID%%", zone.zone_id)
               .replace("%%DISPLAY_NAME%%", zone.display_name)
               .replace("%%GRP_DIR%%", grp_dir))
    _write_file(conf_path, content)

    log.info("Generated OwnTone config for %s", zone.zone_id)


def generate_arecord_supervisor(zone):
    """
    Generate the arecord supervisor script for the HOST.
    The supervisor starts the Shiri mixer, which captures ALSA loopback audio
    and overlays queued TTS before writing OwnTone's audio.pipe.
    """
    grp_dir = zone.grp_dir
    subdev = zone.allocated_subdevice
    capture_dev = f"hw:Loopback,1,{subdev}"

    script_path = os.path.join(grp_dir, "config", "arecord_supervisor.sh")
    template = _read_template("arecord_supervisor.sh")
    content = (template
               .replace("%%CAPTURE_DEV%%", capture_dev)
               .replace("%%GRP_DIR%%", grp_dir)
               .replace("%%MIXER_SCRIPT%%", MIXER_SCRIPT))
    _write_file(script_path, content, executable=True)

    log.info("Generated arecord supervisor script for %s", zone.zone_id)
    return script_path


def get_zone_wrapper_path():
    """Return path to the static zone_wrapper.sh script."""
    wrapper = os.path.join(SCRIPT_DIR, "zone_wrapper.sh")
    os.chmod(wrapper, 0o755)
    return wrapper
