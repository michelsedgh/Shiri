"""
zone_lifecycle.py — Zone start/stop implementation for Shiri.

All the heavy subprocess/netns/process launching machinery lives here.
ZoneManager (in zone.py) delegates to these functions for the actual
start and stop sequences. This keeps the manager focused on CRUD and
API concerns while lifecycle implementation details stay isolated.

Translates dual_zone_demo.sh into Python, calling the SAME system commands.
"""

import logging
import glob
import os
import signal
import subprocess
import time

from owntone_api import OwnToneAPI
from config import (
    BASE_DIR,
    SCRIPT_DIR,
    OWNTONE_PORT_BASE,
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
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if check and result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        detail = stderr or stdout or f"exit code {result.returncode}"
        raise RuntimeError(f"Command failed: {' '.join(cmd)}: {detail}")
    return result


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


def _terminate_pid(pid, label="process", timeout=5):
    """Gracefully terminate a PID, allowing a longer cleanup window."""
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGTERM)
        log.info("Sent SIGTERM to %s (pid %d)", label, pid)
    except ProcessLookupError:
        return

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.2)

    try:
        os.kill(pid, signal.SIGKILL)
        log.info("Sent SIGKILL to %s (pid %d)", label, pid)
    except ProcessLookupError:
        pass


def _read_text(path):
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except (FileNotFoundError, OSError):
        return ""


def _read_pid(path):
    value = _read_text(path)
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _pid_command(pid):
    if pid is None:
        return ""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        return ""


def _kill_pid_if_command(pid, needle, label):
    cmdline = _pid_command(pid)
    if cmdline and needle in cmdline:
        _kill_pid(pid, label)


def _terminate_pid_if_command(pid, needle, label, timeout=2):
    cmdline = _pid_command(pid)
    if cmdline and needle in cmdline:
        _terminate_pid(pid, label, timeout=timeout)


def _state_path(grp_dir, filename):
    return os.path.join(grp_dir, "state", filename)


def _dhclient_paths(grp_dir):
    return (
        _state_path(grp_dir, "dhclient.leases"),
        _state_path(grp_dir, "dhclient.pid"),
    )


def _netns_list_output():
    result = _run(["ip", "netns", "list"])
    return result.stdout or ""


def _netns_exists(ns):
    if not ns:
        return False
    for line in _netns_list_output().splitlines():
        parts = line.split()
        if parts and parts[0] == ns:
            return True
    return False


def _netns_exec(ns, args, **kwargs):
    return _run(["ip", "netns", "exec", ns] + args, **kwargs)


def _find_macvlan_in_netns(ns, preferred=None):
    """Return the zone macvlan interface inside a namespace."""
    if not _netns_exists(ns):
        return None

    if preferred:
        result = _netns_exec(ns, ["ip", "link", "show", preferred])
        if result.returncode == 0:
            return preferred

    result = _netns_exec(ns, ["ip", "-o", "link", "show"])
    for line in (result.stdout or "").splitlines():
        parts = line.split(": ")
        if len(parts) < 2:
            continue
        iface = parts[1].split("@")[0]
        if iface.startswith("ot_"):
            return iface
    return None


def _release_dhcp_lease(grp_dir, ns, iface):
    """Release the DHCP lease using the same per-zone files used at acquire."""
    if not ns or not iface or not _netns_exists(ns):
        return

    lease_file, pid_file = _dhclient_paths(grp_dir)
    released = False
    if os.path.exists(lease_file) or os.path.exists(pid_file):
        result = _netns_exec(ns, [
            "dhclient", "-r",
            "-lf", lease_file,
            "-pf", pid_file,
            iface,
        ])
        released = result.returncode == 0
        if result.returncode != 0:
            log.warning("DHCP release using zone files failed for %s/%s: %s",
                        ns, iface, (result.stderr or result.stdout or "").strip())

    # Fallback for older runs that used dhclient defaults.
    if not released:
        legacy_result = _netns_exec(ns, [
            "dhclient", "-r",
            "-lf", "/run/dhclient.leases",
            "-pf", "/run/dhclient.pid",
            iface,
        ])
        released = legacy_result.returncode == 0

    if not released:
        _netns_exec(ns, ["dhclient", "-r", iface])

    _terminate_namespace_processes(ns, ["dhclient"])
    log.info("Released DHCP lease on %s in %s", iface, ns)


def _namespace_pids(ns):
    if not ns or not _netns_exists(ns):
        return []
    result = _run(["ip", "netns", "pids", ns])
    pids = []
    for pid_str in (result.stdout or "").split():
        try:
            pids.append(int(pid_str))
        except ValueError:
            pass
    return pids


def _terminate_namespace_processes(ns, names, timeout=2):
    wanted = tuple(names)
    for pid in _namespace_pids(ns):
        cmdline = _pid_command(pid)
        if any(name in cmdline for name in wanted):
            _terminate_pid(pid, f"netns {ns} process", timeout=timeout)


def _terminate_namespace_services(ns):
    _terminate_namespace_processes(ns, [
        "shairport-sync",
        "nqptp",
        "avahi-daemon",
        "owntone",
        "dbus-daemon",
        "dhclient",
    ])


def _kill_namespace_pids(ns):
    for pid in _namespace_pids(ns):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _delete_netns(ns):
    if not ns or not _netns_exists(ns):
        return
    last_result = None
    for _ in range(5):
        last_result = _run(["ip", "netns", "delete", ns])
        if last_result.returncode == 0:
            log.info("Deleted netns %s", ns)
            return
        time.sleep(0.2)
    log.warning("Failed to delete netns %s: %s", ns,
                (last_result.stderr or last_result.stdout or "").strip())


def _delete_host_link(iface):
    if not iface:
        return
    result = _run(["ip", "link", "show", iface])
    if result.returncode == 0:
        result = _run(["ip", "link", "delete", iface])
        if result.returncode == 0:
            log.info("Deleted host link %s", iface)
        else:
            log.warning("Failed to delete host link %s: %s", iface,
                        (result.stderr or result.stdout or "").strip())


def _clear_runtime_state(grp_dir):
    for filename in [
        "arecord.pid",
        "avahi.pid",
        "dbus.pid",
        "dhclient.leases",
        "dhclient.pid",
        "dhclient_lease_path.txt",
        "dhclient_pid_path.txt",
        "macvlan_if.txt",
        "macvlan_mac.txt",
        "nqptp.pid",
        "owntone.pid",
        "owntone_ip.txt",
        "owntone_port.txt",
        "owntone_netns.txt",
        "shairport.pid",
        "shairport_ip.txt",
    ]:
        try:
            os.remove(_state_path(grp_dir, filename))
        except FileNotFoundError:
            pass
        except OSError as e:
            log.debug("Could not remove runtime state %s: %s", filename, e)


def _kill_orphaned_host_processes():
    result = _run(["ps", "-eo", "pid=,ppid=,args="])
    for line in (result.stdout or "").splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid_str, ppid_str, args = parts
        if ppid_str != "1" or "/var/lib/shiri/groups/" not in args:
            continue
        if "arecord_supervisor.sh" in args or "audio_mixer.py" in args:
            _kill_pid(int(pid_str), "orphaned audio mixer")
        elif "pause_bridge.sh" in args:
            _kill_pid(int(pid_str), "orphaned pause bridge")
        elif "shairport-sync" in args:
            _kill_pid(int(pid_str), "orphaned shairport-sync")
        elif "owntone" in args:
            _kill_pid(int(pid_str), "orphaned owntone")


def _zone_id_from_netns(ns):
    if not ns.startswith("owntone_"):
        return None
    body = ns[len("owntone_"):]
    if "_" not in body:
        return body
    return body.rsplit("_", 1)[0]


def cleanup_stale_runtime():
    """Reap Shiri netns/macvlan leftovers from a previous daemon run."""
    log.info("Checking for stale Shiri runtime state...")

    stale_namespaces = set()
    stale_group_dirs = set()
    for line in _netns_list_output().splitlines():
        parts = line.split()
        if not parts:
            continue
        ns = parts[0]
        if ns.startswith("owntone_"):
            stale_namespaces.add(ns)

    for path in glob.glob(os.path.join(BASE_DIR, "groups", "*", "state", "owntone_netns.txt")):
        ns = _read_text(path)
        if ns.startswith("owntone_"):
            stale_namespaces.add(ns)
            stale_group_dirs.add(os.path.dirname(os.path.dirname(path)))

    for ns in sorted(stale_namespaces):
        zone_id = _zone_id_from_netns(ns)
        grp_dir = os.path.join(BASE_DIR, "groups", zone_id) if zone_id else BASE_DIR
        if zone_id:
            stale_group_dirs.add(grp_dir)
        if not _netns_exists(ns):
            continue

        log.info("Cleaning stale namespace %s", ns)
        iface = _find_macvlan_in_netns(ns)
        if zone_id:
            _release_dhcp_lease(grp_dir, ns, iface)
        elif iface:
            _netns_exec(ns, ["dhclient", "-r", iface])
        _terminate_namespace_services(ns)
        time.sleep(1)
        _kill_namespace_pids(ns)
        _delete_netns(ns)

    for grp_dir in stale_group_dirs:
        _kill_pid_if_command(
            _read_pid(_state_path(grp_dir, "arecord.pid")),
            "arecord",
            "stale arecord",
        )
    _kill_orphaned_host_processes()
    for grp_dir in stale_group_dirs:
        _clear_runtime_state(grp_dir)

    result = _run(["ip", "-o", "link", "show"])
    for line in (result.stdout or "").splitlines():
        parts = line.split(": ")
        if len(parts) < 2:
            continue
        iface = parts[1].split("@")[0]
        if iface.startswith("ot_"):
            _delete_host_link(iface)

    log.info("Stale Shiri runtime cleanup complete")


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
    4. Start Shairport + OwnTone on host-network ports
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
    """Step 4: Start Shairport + OwnTone.

    Host mode is the default because this VM/router path drops unicast traffic
    for macvlan child MAC addresses even while mDNS still arrives.
    """
    if zone.config.get("network_mode") == "macvlan":
        _start_owntone_in_netns(zone)
    else:
        _start_zone_on_host(zone)


def _wait_and_verify(zone):
    """Step 5: Wait for OwnTone to be ready, rescan library, verify pipe."""
    if not _wait_for_owntone(zone):
        raise RuntimeError(f"OwnTone did not become ready for {zone.zone_id}")

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
    if zone.shairport_pid:
        zone.shairport_ip = zone.shairport_ip or _host_ipv4_for_interface(zone.interface)
        time.sleep(2)
        return

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
    1. Stop HOST processes (arecord, pause_bridge)
    2. Release the zone DHCP lease while the macvlan is still alive
    3. Stop NETNS processes (nqptp, shairport-sync, owntone, avahi, dbus)
    4. Force kill remaining netns processes
    5. Tear down netns + macvlan
    6. Release loopback subdevice after all users are gone
    """
    log.info("Cleaning up zone %s...", zone.zone_id)

    grp_dir = zone.grp_dir
    ns = zone.netns_name or _read_text(_state_path(grp_dir, "owntone_netns.txt"))
    macvlan_if = zone.macvlan_if or _read_text(_state_path(grp_dir, "macvlan_if.txt"))
    if ns and _netns_exists(ns):
        macvlan_if = _find_macvlan_in_netns(ns, macvlan_if) or macvlan_if

    # 1. Stop HOST processes (arecord + pause_bridge remain on host).
    # shairport-sync is in the netns and is killed below.
    _kill_pid(zone.arecord_supervisor_pid, f"arecord supervisor ({zone.zone_id})")
    _kill_pid(_read_pid(_state_path(grp_dir, "arecord.pid")), f"arecord ({zone.zone_id})")
    _kill_pid(zone.metadata_relay_pid, f"metadata relay ({zone.zone_id})")
    _kill_pid(zone.pause_bridge_pid, f"pause_bridge ({zone.zone_id})")

    zone.arecord_supervisor_pid = None
    zone.metadata_relay_pid = None
    zone.pause_bridge_pid = None

    # Host-network mode has direct child PIDs instead of a zone wrapper.
    _terminate_pid(
        zone.shairport_pid or _read_pid(_state_path(grp_dir, "shairport.pid")),
        f"shairport-sync ({zone.zone_id})",
        timeout=3,
    )
    zone.shairport_pid = None

    # Let the wrapper run its trap first; it owns the private mount namespace
    # where dhclient keeps its lease/pid files.
    _terminate_pid(
        zone.owntone_pid or _read_pid(_state_path(grp_dir, "owntone.pid")),
        f"owntone/wrapper ({zone.zone_id})",
        timeout=5,
    )

    # 2-3. Release the DHCP lease and stop services while the namespace exists.
    if ns and _netns_exists(ns):
        _release_dhcp_lease(grp_dir, ns, macvlan_if)
        _terminate_pid_if_command(
            _read_pid(_state_path(grp_dir, "avahi.pid")),
            "avahi-daemon",
            f"avahi ({zone.zone_id})",
        )
        _terminate_pid_if_command(
            _read_pid(_state_path(grp_dir, "dbus.pid")),
            "dbus-daemon",
            f"dbus ({zone.zone_id})",
        )
        _terminate_namespace_services(ns)
        time.sleep(2)

    # 4. Force kill remaining namespace processes
    _kill_namespace_pids(ns)
    time.sleep(0.3)

    # 5. Delete namespace. A macvlan inside the namespace usually disappears
    # with it; the host-link fallback handles failed/partial starts.
    _delete_netns(ns)
    _delete_host_link(macvlan_if)

    # 6. Release loopback subdevice only after all processes that could touch it
    # have been stopped.
    release_loopback_subdevice(zone.allocated_subdevice)
    zone.allocated_subdevice = None
    _clear_runtime_state(grp_dir)

    # Reset state
    zone.netns_name = None
    zone.macvlan_if = None
    zone.shairport_ip = None
    zone.owntone_ip = None
    zone.shairport_port = None
    zone.owntone_port = None
    zone.owntone_api = None
    zone.owntone_pid = None
    zone.nqptp_pid = None

    log.info("Zone %s cleanup complete", zone.zone_id)


# ---------------------------------------------------------------------------
# Internal helpers — netns + process launching
# ---------------------------------------------------------------------------

def _host_ipv4_for_interface(iface):
    if not iface:
        return "127.0.0.1"
    result = _run(["ip", "-4", "-o", "addr", "show", "dev", iface])
    for line in (result.stdout or "").splitlines():
        parts = line.split()
        if "inet" in parts:
            cidr = parts[parts.index("inet") + 1]
            return cidr.split("/", 1)[0]
    return "127.0.0.1"


def _start_zone_on_host(zone):
    """Start Shairport and OwnTone on host ports, avoiding macvlan extra MACs."""
    grp_dir = zone.grp_dir
    subdev = zone.allocated_subdevice
    owntone_port = zone.owntone_port or (OWNTONE_PORT_BASE + subdev * 10)
    shairport_ip = _host_ipv4_for_interface(zone.interface)

    zone.netns_name = ""
    zone.macvlan_if = ""
    zone.owntone_ip = "127.0.0.1"
    zone.shairport_ip = shairport_ip
    zone.owntone_port = owntone_port

    with open(os.path.join(grp_dir, "state", "owntone_ip.txt"), "w") as f:
        f.write(zone.owntone_ip)
    with open(os.path.join(grp_dir, "state", "owntone_port.txt"), "w") as f:
        f.write(str(owntone_port))
    with open(os.path.join(grp_dir, "state", "shairport_ip.txt"), "w") as f:
        f.write(shairport_ip)

    shairport_log = open(os.path.join(grp_dir, "logs", "shairport.log"), "w")
    shairport_proc = subprocess.Popen(
        ["setsid", "chrt", "-f", "50", "shairport-sync",
         "-c", os.path.join(grp_dir, "config", "shairport-sync.conf"),
         "--statistics"],
        stdout=shairport_log,
        stderr=subprocess.STDOUT,
    )
    zone.shairport_pid = shairport_proc.pid
    with open(os.path.join(grp_dir, "state", "shairport.pid"), "w") as f:
        f.write(str(shairport_proc.pid))
    log.info("Started shairport-sync for %s on host (pid %d)",
             zone.zone_id, shairport_proc.pid)

    owntone_log = open(os.path.join(grp_dir, "logs", "owntone_wrapper.log"), "w")
    owntone_proc = subprocess.Popen(
        ["chrt", "-f", "50", "owntone", "-f",
         "-c", os.path.join(grp_dir, "config", "owntone.conf"),
         "--mdns-no-rsp", "--mdns-no-daap", "--mdns-no-web", "--mdns-no-cname"],
        stdout=owntone_log,
        stderr=subprocess.STDOUT,
    )
    zone.owntone_pid = owntone_proc.pid
    with open(os.path.join(grp_dir, "state", "owntone.pid"), "w") as f:
        f.write(str(owntone_proc.pid))
    log.info("Started OwnTone for %s on host port %d (pid %d)",
             zone.zone_id, owntone_port, owntone_proc.pid)


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

    # Create namespace and macvlan.
    _run(["ip", "netns", "add", ns_name], check=True)
    _run(["ip", "link", "add", mv_if, "link", iface, "type", "macvlan", "mode", "bridge"],
         check=True)
    _run(["ip", "link", "set", mv_if, "netns", ns_name], check=True)

    # Save netns name so volume_bridge.sh can use it
    with open(os.path.join(grp_dir, "state", "owntone_netns.txt"), "w") as f:
        f.write(ns_name)
    with open(os.path.join(grp_dir, "state", "macvlan_if.txt"), "w") as f:
        f.write(mv_if)

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
    port = zone.owntone_port or int(_read_text(_state_path(zone.grp_dir, "owntone_port.txt")) or 3689)
    zone.owntone_port = port
    zone.owntone_api = OwnToneAPI(zone.owntone_ip, zone.netns_name, port=port)

    # Wait for API to respond
    log.info("Waiting for OwnTone API at %s:%d...", zone.owntone_ip, port)
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
    Start the host audio mixer.
    It captures ALSA loopback, overlays queued TTS, and writes OwnTone's audio.pipe.
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
