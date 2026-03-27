"""
zone_lifecycle.py — Zone start/stop implementation for Shiri.

All the heavy subprocess/netns/process launching machinery lives here.
ZoneManager (in zone.py) delegates to these functions for the actual
start and stop sequences. This keeps the manager focused on CRUD and
API concerns while lifecycle implementation details stay isolated.

Translates dual_zone_demo.sh into Python, calling the SAME system commands.
"""

import logging
import os
import signal
import subprocess
import time

from owntone_api import OwnToneAPI
from config import (
    SCRIPT_DIR,
    setup_directories,
    allocate_loopback_subdevice,
    release_loopback_subdevice,
    generate_shairport_config,
    generate_owntone_config,
    generate_arecord_supervisor,
    get_zone_wrapper_path,
)

log = logging.getLogger("shiri.zone")


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Zone START sequence
# Follows the exact same sequence as dual_zone_demo.sh's main()
# ---------------------------------------------------------------------------

def start_zone_thread(zone, cleanup_fn):
    """
    Full zone startup sequence:
    1. Allocate loopback subdevice
    2. Setup directories & FIFOs
    3. Generate configs
    4. Start zone in netns (nqptp + shairport-sync + OwnTone)
    5. Wait for OwnTone, rescan library, verify pipe
    6. Start arecord supervisor on host
    7. Start pause bridge on host
    8. Restore saved speaker selections
    """
    from zone import Zone  # Import here to avoid circular import

    try:
        if not zone.interface:
            zone._set_status(Zone.STATUS_ERROR, "No network interface configured")
            return

        _allocate_resources(zone)
        _generate_configs(zone)
        _launch_netns(zone)

        if zone._stop_event.is_set():
            return

        _wait_and_verify(zone)

        if zone._stop_event.is_set():
            return

        _launch_host_processes(zone)
        _read_shairport_pid(zone)
        _restore_speakers(zone)

        zone._set_status(Zone.STATUS_RUNNING)
        log.info("Zone %s is RUNNING! AirPlay name: '%s'",
                  zone.zone_id, zone.display_name)

    except Exception as e:
        log.exception("Failed to start zone %s", zone.zone_id)
        zone._set_status(Zone.STATUS_ERROR, str(e))
        cleanup_fn(zone)


def _allocate_resources(zone):
    """Step 1-2: Allocate loopback subdevice and setup directories."""
    from zone import Zone

    # Step 1: Allocate loopback subdevice
    # Same file-locking as allocate_loopback_subdevice() in demo.sh
    subdev = allocate_loopback_subdevice()
    if subdev is None:
        zone._set_status(Zone.STATUS_ERROR, "No free loopback subdevices")
        raise RuntimeError("No free loopback subdevices")
    zone.allocated_subdevice = subdev

    # Step 2: Setup directories & FIFOs
    # Same as setup_directories() in demo.sh
    setup_directories(zone)


def _generate_configs(zone):
    """Step 3: Generate all config files from templates."""
    # Same templates as generate_shairport_config() and generate_owntone_config()
    generate_shairport_config(zone)
    generate_owntone_config(zone)


def _launch_netns(zone):
    """Step 4: Create netns + macvlan and start OwnTone in it."""
    _start_owntone_in_netns(zone)


def _wait_and_verify(zone):
    """Step 5: Wait for OwnTone to be ready, rescan library, verify pipe."""
    if not _wait_for_owntone(zone):
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


def _launch_host_processes(zone):
    """Step 6-7: Start arecord supervisor and pause bridge on host."""
    # Note: metadata_relay disabled - shairport writes directly to pause_bridge.metadata
    # which pause_bridge.sh reads. OwnTone metadata (for lyrics) not currently supported.
    _start_pause_bridge(zone)
    _start_arecord_supervisor(zone)


def _read_shairport_pid(zone):
    """Read shairport-sync PID from the file the netns wrapper wrote."""
    # shairport-sync now runs inside the netns wrapper (with per-zone nqptp)
    # Read its PID from the file the wrapper wrote
    shairport_pid_file = os.path.join(zone.grp_dir, "state", "shairport.pid")
    for _ in range(10):
        if os.path.exists(shairport_pid_file):
            with open(shairport_pid_file) as f:
                pid_str = f.read().strip()
            if pid_str:
                zone.shairport_pid = int(pid_str)
                log.info("shairport-sync for %s running in netns (pid %d)",
                         zone.zone_id, zone.shairport_pid)
                break
        time.sleep(1)

    # shairport_ip = macvlan IP (same as OwnTone, both in the netns)
    zone.shairport_ip = zone.owntone_ip

    # Brief pause for avahi registration
    time.sleep(2)


def _restore_speakers(zone):
    """Restore saved speaker selections with retry loop.
    AirPlay speaker discovery via mDNS can take 5-15 seconds."""
    if not zone.owntone_api:
        return
    if not (zone.config.get("speakers") or zone.config.get("speaker_names")):
        return

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


# ---------------------------------------------------------------------------
# Zone STOP sequence
# Same cleanup sequence as cleanup() in dual_zone_demo.sh
# ---------------------------------------------------------------------------

def stop_zone_thread(zone, cleanup_fn):
    """Full zone cleanup — same order as cleanup() in dual_zone_demo.sh."""
    from zone import Zone

    try:
        cleanup_fn(zone)
        zone._set_status(Zone.STATUS_STOPPED)
        zone._stop_event.clear()
        log.info("Zone %s stopped", zone.zone_id)
    except Exception as e:
        log.exception("Error stopping zone %s", zone.zone_id)
        zone._set_status(Zone.STATUS_ERROR, f"Cleanup error: {e}")


def cleanup_zone(zone):
    """
    Zone cleanup sequence:
    1. Release loopback subdevice
    2. Stop HOST processes (arecord, pause_bridge)
    3. Stop NETNS processes (nqptp, shairport-sync, owntone, avahi)
    4. Force kill remaining netns processes
    5. Tear down netns + macvlan
    """
    log.info("Cleaning up zone %s...", zone.zone_id)

    # 1. Release loopback subdevice
    release_loopback_subdevice(zone.allocated_subdevice)
    zone.allocated_subdevice = None

    # 2. Stop HOST processes (arecord + pause_bridge remain on host)
    # shairport-sync is now in the netns — killed in step 3
    _kill_pid(zone.arecord_supervisor_pid, f"arecord supervisor ({zone.zone_id})")
    _kill_pid(zone.metadata_relay_pid, f"metadata relay ({zone.zone_id})")
    _kill_pid(zone.pause_bridge_pid, f"pause_bridge ({zone.zone_id})")
    zone.shairport_pid = None
    zone.arecord_supervisor_pid = None
    zone.metadata_relay_pid = None
    zone.pause_bridge_pid = None

    # 3. Cleanup in zone namespace (nqptp + shairport-sync + owntone + avahi)
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

            # Gracefully stop all services in the namespace
            _run(["ip", "netns", "exec", ns, "pkill", "-TERM", "shairport-sync"])
            _run(["ip", "netns", "exec", ns, "pkill", "-TERM", "nqptp"])
            _run(["ip", "netns", "exec", ns, "pkill", "-TERM", "avahi-daemon"])
            _run(["ip", "netns", "exec", ns, "pkill", "-TERM", "owntone"])

    # Give services time to shut down
    time.sleep(2)

    # 4. Force kill remaining namespace processes
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


# ---------------------------------------------------------------------------
# Internal helpers — netns + process launching
# ---------------------------------------------------------------------------

def _start_owntone_in_netns(zone):
    """
    Creates netns + macvlan, runs the zone wrapper script inside.
    The wrapper starts nqptp + shairport-sync + OwnTone, all with
    private /dev/shm for PTP isolation between zones.
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

    # Use the static zone wrapper script (no longer generated per-zone)
    wrapper_path = get_zone_wrapper_path()

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


def _wait_for_owntone(zone, timeout=60):
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


def _start_arecord_supervisor(zone):
    """
    Start the arecord supervisor on the HOST.
    arecord captures from ALSA loopback and pipes to OwnTone's audio.pipe.
    Runs on host because ALSA loopback is a kernel device accessible from anywhere.
    shairport-sync now starts inside the netns wrapper (not here).
    """
    # Generate the supervisor script from template
    script_path = generate_arecord_supervisor(zone)

    log_path = os.path.join(zone.grp_dir, "logs", "arecord.log")
    # Don't use context manager - file must stay open for subprocess lifetime
    log_file = open(log_path, "w")
    proc = subprocess.Popen(
        ["bash", script_path],
        stdout=log_file, stderr=subprocess.STDOUT
    )
    zone.arecord_supervisor_pid = proc.pid
    log.info("Started arecord supervisor for %s (pid %d)", zone.zone_id, proc.pid)


def _start_pause_bridge(zone):
    """
    Launches existing scripts/pause_bridge.sh as a subprocess.
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
