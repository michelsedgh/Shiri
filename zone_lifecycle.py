"""
zone_lifecycle.py — Zone start/stop implementation for Shiri.

All the subprocess and process-lifecycle machinery lives here.
ZoneManager (in zone.py) delegates to these functions for the actual
start and stop sequences. This keeps the manager focused on CRUD and
API concerns while lifecycle implementation details stay isolated.
"""

import logging
import hashlib
import os
import signal
import shlex
import shutil
import subprocess
import threading
import time

from owntone_api import OwnToneAPI
from config import (
    BASE_DIR,
    OWNTONE_PORT_BASE,
    OWNTONE_SENDER_NS,
    OWNTONE_SENDER_IFACE,
    OWNTONE_API_HOST_IFACE,
    OWNTONE_API_NS_IFACE,
    OWNTONE_API_HOST_IP,
    OWNTONE_API_NS_IP,
    OWNTONE_API_HOST_CIDR,
    OWNTONE_API_NS_CIDR,
    OWNTONE_SENDER_DIR,
    SCRIPT_DIR,
    setup_directories,
    allocate_loopback_subdevice,
    release_loopback_subdevice,
    generate_shairport_config,
    generate_owntone_config,
    generate_mixer_supervisor,
)

log = logging.getLogger("shiri.zone")

PREFERRED_BINARIES = {
    "airptpd": "/usr/local/sbin/airptpd",
    "nqptp": "/usr/local/bin/nqptp",
    "owntone": "/usr/local/sbin/owntone",
    "shairport-sync": "/usr/local/bin/shairport-sync",
}
_SENDER_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _run(cmd, check=False, **kwargs):
    """Run a command, log it, return CompletedProcess."""
    log.debug("Running: %s", " ".join(cmd) if isinstance(cmd, list) else cmd)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    except subprocess.TimeoutExpired as exc:
        log.warning("Command timed out after %ss: %s", exc.timeout, " ".join(cmd))
        result = subprocess.CompletedProcess(
            cmd,
            124,
            stdout=exc.stdout or "",
            stderr=exc.stderr or f"timeout after {exc.timeout}s",
        )
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
    if _pid_is_zombie(pid):
        _reap_pid(pid)
        return
    try:
        os.kill(pid, signal.SIGTERM)
        log.info("Sent SIGTERM to %s (pid %d)", label, pid)
    except ProcessLookupError:
        return
    time.sleep(1)
    if _pid_is_zombie(pid):
        _reap_pid(pid)
        return
    try:
        os.kill(pid, signal.SIGKILL)
        log.info("Sent SIGKILL to %s (pid %d)", label, pid)
    except ProcessLookupError:
        pass


def _terminate_pid(pid, label="process", timeout=5):
    """Gracefully terminate a PID, allowing a longer cleanup window."""
    if pid is None:
        return
    if _pid_is_zombie(pid):
        _reap_pid(pid)
        return
    try:
        os.kill(pid, signal.SIGTERM)
        log.info("Sent SIGTERM to %s (pid %d)", label, pid)
    except ProcessLookupError:
        return

    deadline = time.time() + timeout
    while time.time() < deadline:
        if _pid_is_zombie(pid):
            _reap_pid(pid)
            return
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


def _pid_is_zombie(pid):
    if pid is None:
        return False
    try:
        with open(f"/proc/{pid}/stat", "r") as f:
            parts = f.read().split()
    except OSError:
        return False
    return len(parts) > 2 and parts[2] == "Z"


def _reap_pid(pid):
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    except OSError:
        pass


def _kill_pid_if_command(pid, needle, label):
    cmdline = _pid_command(pid)
    if cmdline and needle in cmdline:
        _kill_pid(pid, label)


def _state_path(grp_dir, filename):
    return os.path.join(grp_dir, "state", filename)


def _write_text(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(str(value))


def _runtime_path(run_dir, filename):
    return os.path.join(run_dir, filename)


def _prepare_isolated_runtime(run_dir):
    os.makedirs(run_dir, exist_ok=True)
    os.chmod(run_dir, 0o777)
    for filename in ["dbus.pidfile", "system_bus_socket"]:
        try:
            os.remove(_runtime_path(run_dir, filename))
        except FileNotFoundError:
            pass
    for subdir in ["avahi-run", "shm"]:
        os.makedirs(os.path.join(run_dir, subdir), exist_ok=True)
        os.chmod(os.path.join(run_dir, subdir), 0o755)


def _ensure_isolated_mount_dirs(run_dir):
    os.makedirs(run_dir, exist_ok=True)
    os.chmod(run_dir, 0o777)
    for subdir in ["avahi-run", "shm"]:
        os.makedirs(os.path.join(run_dir, subdir), exist_ok=True)
        os.chmod(os.path.join(run_dir, subdir), 0o755)


def _write_dbus_config(run_dir):
    path = _runtime_path(run_dir, "dbus-system.conf")
    socket = _runtime_path(run_dir, "system_bus_socket")
    pidfile = _runtime_path(run_dir, "dbus.pidfile")
    content = f"""<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN" "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>system</type>
  <listen>unix:path={socket}</listen>
  <pidfile>{pidfile}</pidfile>
  <policy context="default">
    <allow user="*"/>
    <allow own="*"/>
    <allow send_destination="*"/>
    <allow send_interface="*"/>
    <allow receive_sender="*"/>
  </policy>
</busconfig>
"""
    _write_text(path, content)
    return path, socket


def _write_avahi_config(run_dir, hostname, iface):
    path = _runtime_path(run_dir, "avahi-daemon.conf")
    safe_hostname = hostname.replace("_", "-")
    content = f"""[server]
host-name={safe_hostname}
use-ipv4=yes
use-ipv6=yes
allow-interfaces={iface}
ratelimit-interval-usec=1000000
ratelimit-burst=1000

[publish]
disable-publishing=no
publish-addresses=yes
publish-hinfo=no
publish-workstation=no
publish-domain=no

[reflector]
enable-reflector=no

[rlimits]
"""
    _write_text(path, content)
    return path


def _start_dbus(run_dir, log_path):
    dbus_conf, _ = _write_dbus_config(run_dir)
    log_file = open(log_path, "a")
    proc = subprocess.Popen(
        ["dbus-daemon", "--config-file", dbus_conf, "--nofork"],
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    _write_text(_runtime_path(run_dir, "dbus.pid"), proc.pid)
    time.sleep(0.4)
    if proc.poll() is not None:
        raise RuntimeError(f"dbus-daemon exited during startup, see {log_path}")
    return proc


def _isolation_shell(run_dir, ns, dbus_socket, cmd):
    shm_dir = os.path.join(run_dir, "shm")
    avahi_run = os.path.join(run_dir, "avahi-run")
    env_cmd = ["env", f"DBUS_SYSTEM_BUS_ADDRESS=unix:path={dbus_socket}"] + cmd
    return (
        f"mount --bind {shlex.quote(shm_dir)} /dev/shm && "
        f"mount --bind {shlex.quote(avahi_run)} /run/avahi-daemon && "
        f"exec ip netns exec {shlex.quote(ns)} {shlex.join(env_cmd)}"
    )


def _popen_isolated(run_dir, ns, cmd, log_path):
    _ensure_isolated_mount_dirs(run_dir)
    _, dbus_socket = _write_dbus_config(run_dir)
    log_file = open(log_path, "a")
    proc = subprocess.Popen(
        ["unshare", "-m", "--propagation", "private", "sh", "-c",
         _isolation_shell(run_dir, ns, dbus_socket, cmd)],
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    return proc


def _start_avahi(run_dir, ns, hostname, iface, log_path):
    avahi_conf = _write_avahi_config(run_dir, hostname, iface)
    proc = _popen_isolated(
        run_dir,
        ns,
        ["avahi-daemon", "--file", avahi_conf, "--no-drop-root", "--no-chroot", "--debug"],
        log_path,
    )
    _write_text(_runtime_path(run_dir, "avahi.pid"), proc.pid)
    time.sleep(1)
    if proc.poll() is not None:
        raise RuntimeError(f"avahi-daemon exited during startup, see {log_path}")
    return proc


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


def _iface_ipv4_in_netns(ns, iface):
    result = _netns_exec(ns, ["ip", "-4", "-o", "addr", "show", "dev", iface])
    for line in (result.stdout or "").splitlines():
        parts = line.split()
        if "inet" in parts:
            return parts[parts.index("inet") + 1].split("/", 1)[0]
    return ""


def _ensure_netns(ns):
    if not _netns_exists(ns):
        _run(["ip", "netns", "add", ns], check=True)
    _netns_exec(ns, ["ip", "link", "set", "lo", "up"], check=False)


def _stable_mac(role_key):
    digest = hashlib.sha256(str(role_key).encode("utf-8")).digest()
    octets = [0x02, digest[0], digest[1], digest[2], digest[3], digest[4]]
    return ":".join(f"{octet:02x}" for octet in octets)


def _dhclient_identity(role_key):
    digest = hashlib.sha256(str(role_key).encode("utf-8")).hexdigest()[:16]
    return f"shiri-{digest}"


def _dhclient_paths(role_key):
    identity = _dhclient_identity(role_key)
    lease_file = f"/var/lib/dhcp/dhclient-{identity}.leases"
    pid_file = f"/run/dhclient-{identity}.pid"
    os.makedirs(os.path.dirname(lease_file), exist_ok=True)
    if not os.path.exists(lease_file):
        open(lease_file, "a").close()
    os.chmod(lease_file, 0o666)
    try:
        os.remove(pid_file)
    except FileNotFoundError:
        pass
    return lease_file, pid_file


def _namespace_dhclient_script():
    source = os.path.join(SCRIPT_DIR, "dhclient_namespace.sh")
    target = "/etc/dhcp/dhclient-script"
    marker = "Minimal dhclient hook for Shiri network namespaces."
    try:
        existing = _read_text(target)
    except OSError:
        existing = ""
    if existing and marker not in existing:
        raise RuntimeError(
            f"Refusing to overwrite non-Shiri dhclient script at {target}; "
            "remove or move it before starting Shiri"
        )
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if _read_text(source) != existing:
        shutil.copyfile(source, target)
    os.chmod(target, 0o755)
    return target


def _acquire_dhcp(ns, iface, role_key, timeout=20):
    lease_file, pid_file = _dhclient_paths(role_key)
    script_file = _namespace_dhclient_script()
    result = _netns_exec(ns, [
        "dhclient", "-4", "-1", "-v",
        "-lf", lease_file,
        "-pf", pid_file,
        "-sf", script_file,
        iface,
    ], timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"DHCP failed for {iface} in {ns}: {detail}")
    ip = _iface_ipv4_in_netns(ns, iface)
    if not ip:
        raise RuntimeError(f"DHCP succeeded but {iface} in {ns} has no IPv4 address")
    _preflight_lan_unicast(ns, iface, ip)
    return ip


def _preflight_lan_unicast(ns, iface, ip):
    result = _netns_exec(ns, ["ip", "-4", "route", "show", "default", "dev", iface])
    gateway = ""
    for line in (result.stdout or "").splitlines():
        parts = line.split()
        if "via" in parts:
            gateway = parts[parts.index("via") + 1]
            break
    if not gateway:
        log.warning("No default gateway found for %s/%s after DHCP", ns, iface)
        return

    ping = _netns_exec(ns, ["ping", "-c", "2", "-W", "1", gateway], timeout=4)
    if ping.returncode != 0:
        neigh = _netns_exec(ns, ["ip", "neigh", "show", "dev", iface])
        detail = (ping.stderr or ping.stdout or "").strip()
        neighbors = (neigh.stdout or "").strip()
        raise RuntimeError(
            f"LAN preflight failed for {ns}/{iface} at {ip}: DHCP worked, "
            f"but unicast to gateway {gateway} failed. This usually means the "
            "VM bridge/router is not passing traffic for macvlan secondary MACs. "
            f"ping={detail!r} neighbors={neighbors!r}"
        )
    log.info("LAN preflight OK for %s/%s at %s via %s", ns, iface, ip, gateway)


def _create_macvlan_in_netns(parent_iface, ns, iface, role_key):
    mac = _stable_mac(role_key)
    _delete_host_link(iface)
    _run([
        "ip", "link", "add", iface,
        "link", parent_iface,
        "address", mac,
        "type", "macvlan", "mode", "bridge",
    ], check=True)
    _run(["ip", "link", "set", iface, "netns", ns], check=True)
    _netns_exec(ns, ["ip", "link", "set", iface, "up"], check=True)
    log.info("Created macvlan %s in %s on %s with stable MAC %s", iface, ns, parent_iface, mac)


def _find_macvlan_in_netns(ns, preferred=None):
    """Return the Shiri macvlan interface inside a namespace."""
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
        if iface.startswith("ot_") or iface.startswith("otlan") or iface.startswith("rx"):
            return iface
    return None


def _receiver_ns(zone):
    return f"shiri_rx_{zone.zone_id.replace('zone_', '')[:8]}"


def _receiver_iface(zone):
    return f"rx{zone.allocated_subdevice}"


def _receiver_run_dir(zone):
    return _state_path(zone.grp_dir, "rx-runtime")


def _sender_run_dir():
    return os.path.join(OWNTONE_SENDER_DIR, "state")


def _sender_state(filename):
    return _runtime_path(_sender_run_dir(), filename)


def _ensure_owntone_sender(parent_iface):
    with _SENDER_LOCK:
        run_dir = _sender_run_dir()
        existing_api_ip = _read_text(_sender_state("api_ip.txt"))
        existing_bridge_ip = _read_text(_sender_state("bridge_ip.txt"))
        if _netns_exists(OWNTONE_SENDER_NS) and existing_api_ip and existing_bridge_ip:
            return existing_api_ip, existing_bridge_ip

        try:
            _teardown_owntone_sender()
            _prepare_isolated_runtime(run_dir)
            _ensure_netns(OWNTONE_SENDER_NS)

            _delete_host_link(OWNTONE_API_HOST_IFACE)
            _run(["ip", "link", "add", OWNTONE_API_HOST_IFACE, "type", "veth",
                  "peer", "name", OWNTONE_API_NS_IFACE], check=True)
            _run(["ip", "link", "set", OWNTONE_API_NS_IFACE, "netns", OWNTONE_SENDER_NS], check=True)
            _run(["ip", "addr", "add", OWNTONE_API_HOST_CIDR, "dev", OWNTONE_API_HOST_IFACE], check=True)
            _run(["ip", "link", "set", OWNTONE_API_HOST_IFACE, "up"], check=True)
            _netns_exec(OWNTONE_SENDER_NS, [
                "ip", "addr", "add", OWNTONE_API_NS_CIDR, "dev", OWNTONE_API_NS_IFACE,
            ], check=True)
            _netns_exec(OWNTONE_SENDER_NS, ["ip", "link", "set", OWNTONE_API_NS_IFACE, "up"], check=True)

            _create_macvlan_in_netns(
                parent_iface,
                OWNTONE_SENDER_NS,
                OWNTONE_SENDER_IFACE,
                "sender:owntone",
            )
            bridge_ip = _acquire_dhcp(
                OWNTONE_SENDER_NS,
                OWNTONE_SENDER_IFACE,
                "sender:owntone",
            )

            _write_text(_sender_state("netns.txt"), OWNTONE_SENDER_NS)
            _write_text(_sender_state("iface.txt"), OWNTONE_SENDER_IFACE)
            _write_text(_sender_state("api_ip.txt"), OWNTONE_API_NS_IP)
            _write_text(_sender_state("bridge_ip.txt"), bridge_ip)

            dbus_proc = _start_dbus(run_dir, _runtime_path(run_dir, "dbus.log"))
            _write_text(_sender_state("dbus.pid"), dbus_proc.pid)
            avahi_proc = _start_avahi(
                run_dir,
                OWNTONE_SENDER_NS,
                "shiri-owntone",
                OWNTONE_SENDER_IFACE,
                _runtime_path(run_dir, "avahi.log"),
            )
            _write_text(_sender_state("avahi.pid"), avahi_proc.pid)
            _start_airptpd(run_dir)
        except Exception:
            _teardown_owntone_sender(force=True)
            raise

        log.info("OwnTone sender namespace ready: api=%s bridge=%s", OWNTONE_API_NS_IP, bridge_ip)
        return OWNTONE_API_NS_IP, bridge_ip


def _start_airptpd(run_dir):
    if not _binary_exists("airptpd"):
        raise RuntimeError("airptpd is not installed; OwnTone AirPlay 2 output cannot start")
    proc = _popen_isolated(
        run_dir,
        OWNTONE_SENDER_NS,
        ["stdbuf", "-oL", "-eL", _binary("airptpd"), "-f", "-v"],
        _runtime_path(run_dir, "airptpd.log"),
    )
    _write_text(_sender_state("airptpd.pid"), proc.pid)
    time.sleep(1)
    if proc.poll() is not None:
        raise RuntimeError(f"airptpd exited during startup, see {_runtime_path(run_dir, 'airptpd.log')}")
    return proc


def _teardown_owntone_sender(force=False):
    with _SENDER_LOCK:
        run_dir = _sender_run_dir()
        if not force and _owntone_processes_in_sender():
            return

        for filename, label in [
            ("airptpd.pid", "airptpd"),
            ("avahi.pid", "sender avahi"),
            ("dbus.pid", "sender dbus"),
        ]:
            _terminate_pid(_read_pid(_sender_state(filename)), label, timeout=2)

        if _netns_exists(OWNTONE_SENDER_NS):
            _terminate_namespace_processes(OWNTONE_SENDER_NS, [
                "owntone",
                "airptpd",
                "avahi-daemon",
                "dhclient",
            ])
            time.sleep(0.5)
            _kill_namespace_pids(OWNTONE_SENDER_NS)
            _delete_netns(OWNTONE_SENDER_NS)

        _delete_host_link(OWNTONE_API_HOST_IFACE)
        for filename in [
            "airptpd.pid",
            "api_ip.txt",
            "avahi.pid",
            "bridge_ip.txt",
            "dbus.pid",
            "dbus.pidfile",
            "iface.txt",
            "netns.txt",
            "system_bus_socket",
        ]:
            try:
                os.remove(_sender_state(filename))
            except FileNotFoundError:
                pass
            except OSError:
                pass


def _owntone_processes_in_sender():
    if not _netns_exists(OWNTONE_SENDER_NS):
        return []
    pids = []
    for pid in _namespace_pids(OWNTONE_SENDER_NS):
        if "owntone" in _pid_command(pid):
            pids.append(pid)
    return pids


def _binary_exists(name):
    if os.path.exists(PREFERRED_BINARIES.get(name, "")):
        return True
    result = _run(["sh", "-c", f"command -v {shlex.quote(name)}"])
    return result.returncode == 0 and bool((result.stdout or "").strip())


def _binary(name):
    preferred = PREFERRED_BINARIES.get(name)
    if preferred and os.path.exists(preferred):
        return preferred
    return name


def _start_receiver_namespace(zone):
    ns = _receiver_ns(zone)
    iface = _receiver_iface(zone)
    run_dir = _receiver_run_dir(zone)
    os.makedirs(run_dir, exist_ok=True)
    _prepare_isolated_runtime(run_dir)

    _teardown_receiver_namespace(zone)
    _ensure_netns(ns)
    _create_macvlan_in_netns(zone.interface, ns, iface, f"receiver:{zone.zone_id}")
    receiver_ip = _acquire_dhcp(ns, iface, f"receiver:{zone.zone_id}")

    _write_text(_state_path(zone.grp_dir, "receiver_netns.txt"), ns)
    _write_text(_state_path(zone.grp_dir, "receiver_iface.txt"), iface)
    _write_text(_state_path(zone.grp_dir, "shairport_ip.txt"), receiver_ip)

    dbus_proc = _start_dbus(run_dir, os.path.join(zone.grp_dir, "logs", "receiver_dbus.log"))
    _write_text(_state_path(zone.grp_dir, "dbus.pid"), dbus_proc.pid)
    avahi_proc = _start_avahi(
        run_dir,
        ns,
        f"shiri-{zone.zone_id}",
        iface,
        os.path.join(zone.grp_dir, "logs", "receiver_avahi.log"),
    )
    _write_text(_state_path(zone.grp_dir, "avahi.pid"), avahi_proc.pid)
    _start_nqptp(zone, ns, run_dir)
    return ns, iface, receiver_ip


def _start_nqptp(zone, ns, run_dir):
    proc = _popen_isolated(
        run_dir,
        ns,
        [_binary("nqptp"), "-v"],
        os.path.join(zone.grp_dir, "logs", "nqptp.log"),
    )
    _write_text(_state_path(zone.grp_dir, "nqptp.pid"), proc.pid)
    time.sleep(1)
    if proc.poll() is not None:
        raise RuntimeError(f"nqptp exited during startup, see {os.path.join(zone.grp_dir, 'logs', 'nqptp.log')}")
    return proc


def _teardown_receiver_namespace(zone):
    ns = _read_text(_state_path(zone.grp_dir, "receiver_netns.txt")) or _receiver_ns(zone)
    iface = _read_text(_state_path(zone.grp_dir, "receiver_iface.txt")) or _receiver_iface(zone)
    for filename, label in [
        ("shairport.pid", f"shairport-sync ({zone.zone_id})"),
        ("nqptp.pid", f"nqptp ({zone.zone_id})"),
        ("avahi.pid", f"receiver avahi ({zone.zone_id})"),
        ("dbus.pid", f"receiver dbus ({zone.zone_id})"),
    ]:
        _terminate_pid(_read_pid(_state_path(zone.grp_dir, filename)), label, timeout=2)

    if _netns_exists(ns):
        _terminate_namespace_processes(ns, [
            _binary("shairport-sync"),
            "nqptp",
            "avahi-daemon",
            "dhclient",
        ])
        time.sleep(0.5)
        _kill_namespace_pids(ns)
        _delete_netns(ns)

    _delete_host_link(iface)


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
        "airptpd",
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
        "mixer.pid",
        "avahi.pid",
        "dbus.pid",
        "nqptp.pid",
        "owntone_api_ip.txt",
        "owntone_bridge_ip.txt",
        "owntone.pid",
        "owntone_port.txt",
        "receiver_iface.txt",
        "receiver_netns.txt",
        "shairport.pid",
        "shairport_ip.txt",
    ]:
        try:
            os.remove(_state_path(grp_dir, filename))
        except FileNotFoundError:
            pass
        except OSError as e:
            log.debug("Could not remove runtime state %s: %s", filename, e)
    shutil_path = os.path.join(grp_dir, "state", "rx-runtime")
    if os.path.isdir(shutil_path):
        import shutil
        shutil.rmtree(shutil_path, ignore_errors=True)


def _kill_orphaned_host_processes():
    result = _run(["ps", "-eo", "pid=,ppid=,args="])
    for line in (result.stdout or "").splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid_str, ppid_str, args = parts
        if ppid_str != "1" or "/var/lib/shiri/groups/" not in args:
            continue
        if "mixer_supervisor.sh" in args or "audio_mixer.py" in args:
            _kill_pid(int(pid_str), "orphaned audio mixer")
        elif "shairport-sync" in args:
            _kill_pid(int(pid_str), "orphaned shairport-sync")
        elif "owntone" in args:
            _kill_pid(int(pid_str), "orphaned owntone")


def cleanup_stale_runtime():
    """Reap Shiri netns/macvlan leftovers from a previous daemon run."""
    log.info("Checking for stale Shiri runtime state...")

    stale_namespaces = set()
    namespace_group_dirs = {}
    for line in _netns_list_output().splitlines():
        parts = line.split()
        if not parts:
            continue
        ns = parts[0]
        if ns.startswith("shiri_rx_") or ns == OWNTONE_SENDER_NS:
            stale_namespaces.add(ns)

    groups_root = os.path.join(BASE_DIR, "groups")
    if os.path.isdir(groups_root):
        for zone_id in os.listdir(groups_root):
            grp_dir = os.path.join(groups_root, zone_id)
            state_dir = os.path.join(grp_dir, "state")
            ns = _read_text(os.path.join(state_dir, "receiver_netns.txt"))
            if ns.startswith("shiri_rx_"):
                stale_namespaces.add(ns)
                namespace_group_dirs.setdefault(ns, set()).add(grp_dir)

    for ns in sorted(stale_namespaces):
        if ns == OWNTONE_SENDER_NS:
            continue
        if not _netns_exists(ns):
            continue

        log.info("Cleaning stale namespace %s", ns)
        _terminate_namespace_services(ns)
        time.sleep(1)
        _kill_namespace_pids(ns)
        _delete_netns(ns)

    stale_group_dirs = {grp_dir for dirs in namespace_group_dirs.values() for grp_dir in dirs}
    for grp_dir in stale_group_dirs:
        _kill_pid_if_command(
            _read_pid(_state_path(grp_dir, "mixer.pid")),
            "audio_mixer.py",
            "stale mixer",
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
        if iface.startswith("ot_") or iface.startswith("otlan") or iface == OWNTONE_API_HOST_IFACE or iface.startswith("rx"):
            _delete_host_link(iface)

    _teardown_owntone_sender(force=True)

    log.info("Stale Shiri runtime cleanup complete")


# ---------------------------------------------------------------------------
# Zone START sequence
# ---------------------------------------------------------------------------

def start_zone_thread(zone, cleanup_fn):
    """
    Full zone startup sequence:
    1. Allocate loopback subdevice
    2. Setup directories & FIFOs
    3. Generate configs
    4. Start Shairport and OwnTone in their AP2 timing namespaces
    5. Wait for OwnTone, rescan library, verify pipe
    6. Start mixer on host
    7. Restore saved speaker selections
    """
    from zone import Zone  # Import here to avoid circular import

    try:
        if not zone.interface:
            zone._set_status(Zone.STATUS_ERROR, "No network interface configured")
            return

        _allocate_resources(zone)
        _generate_configs(zone)
        _start_zone_airplay2_netns(zone)

        if zone._stop_event.is_set():
            return

        _wait_and_verify(zone)

        if zone._stop_event.is_set():
            return

        _launch_host_processes(zone)
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

    subdev = allocate_loopback_subdevice()
    if subdev is None:
        zone._set_status(Zone.STATUS_ERROR, "No free loopback subdevices")
        raise RuntimeError("No free loopback subdevices")
    zone.allocated_subdevice = subdev

    setup_directories(zone)


def _generate_configs(zone):
    """Step 3: Generate all config files from templates."""
    generate_shairport_config(zone)
    generate_owntone_config(zone)


def _wait_and_verify(zone):
    """Step 5: Wait for OwnTone to be ready, rescan library, verify pipe."""
    if not _wait_for_owntone(zone):
        raise RuntimeError(f"OwnTone did not become ready for {zone.zone_id}")

    _apply_persisted_master_volume(zone)

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


def _apply_persisted_master_volume(zone):
    """Apply the latest saved playback volume before audio starts flowing."""
    if not zone.owntone_api:
        return
    volume = _saved_master_volume(zone)
    if volume is None:
        return
    try:
        zone.owntone_api.set_volume(volume)
        log.info("Applied persisted master volume %s for %s", volume, zone.zone_id)
    except Exception as exc:
        log.warning("Could not apply persisted master volume for %s: %s", zone.zone_id, exc)


def _saved_master_volume(zone):
    raw = zone.config.get("master_volume")
    if raw is None:
        raw = zone.config.get("volume")
    if raw is None:
        raw = _read_text(os.path.join(zone.grp_dir, "state", "master_volume_last.txt"))
    if raw == "" or raw is None:
        return None
    try:
        return max(0, min(100, int(round(float(raw)))))
    except (TypeError, ValueError):
        return None


def _real_speaker_outputs(outputs, excluded_names):
    excluded = {str(name) for name in excluded_names if str(name)}
    return [
        output
        for output in outputs
        if str(output.get("type") or "") in {"AirPlay 2", "ALSA"}
        and str(output.get("name") or "") not in excluded
    ]


def _launch_host_processes(zone):
    """Step 6: Start the host mixer that feeds OwnTone's pipe input."""
    _start_mixer(zone)


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
            available_outputs = _real_speaker_outputs(
                zone.owntone_api.get_outputs(),
                getattr(zone, "excluded_airplay_names", []),
            )
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
                _apply_persisted_master_volume(zone)
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
# ---------------------------------------------------------------------------

def stop_zone_thread(zone, cleanup_fn):
    """Run full zone cleanup in the stop worker."""
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
    1. Stop host-side audio helpers
    2. Stop Shairport and OwnTone
    3. Release loopback subdevice after all users are gone
    """
    log.info("Cleaning up zone %s...", zone.zone_id)

    grp_dir = zone.grp_dir
    # 1. Stop host-side audio helpers.
    _kill_pid(zone.mixer_pid, f"mixer supervisor ({zone.zone_id})")
    _kill_pid(_read_pid(_state_path(grp_dir, "mixer.pid")), f"mixer ({zone.zone_id})")

    zone.mixer_pid = None

    # 2. Stop AirPlay receiver and OwnTone sender processes.
    _terminate_pid(
        zone.shairport_pid or _read_pid(_state_path(grp_dir, "shairport.pid")),
        f"shairport-sync ({zone.zone_id})",
        timeout=3,
    )
    zone.shairport_pid = None
    _teardown_receiver_namespace(zone)

    _terminate_pid(
        zone.owntone_pid or _read_pid(_state_path(grp_dir, "owntone.pid")),
        f"owntone ({zone.zone_id})",
        timeout=5,
    )
    zone.owntone_pid = None
    _teardown_owntone_sender()

    # 3. Release loopback subdevice only after all processes that could touch it
    # have been stopped.
    release_loopback_subdevice(zone.allocated_subdevice)
    zone.allocated_subdevice = None
    _clear_runtime_state(grp_dir)

    # Reset state
    zone.shairport_ip = None
    zone.owntone_ip = None
    zone.shairport_port = None
    zone.owntone_port = None
    zone.tts_pcm_pipe = None
    zone.owntone_api = None

    log.info("Zone %s cleanup complete", zone.zone_id)


def _start_zone_airplay2_netns(zone):
    """Start Shairport and OwnTone in their AirPlay 2 timing namespaces."""
    grp_dir = zone.grp_dir
    subdev = zone.allocated_subdevice
    owntone_port = zone.owntone_port or (OWNTONE_PORT_BASE + subdev * 10)
    api_ip, bridge_ip = _ensure_owntone_sender(zone.interface)
    receiver_ns, _, shairport_ip = _start_receiver_namespace(zone)

    zone.owntone_ip = api_ip
    zone.shairport_ip = shairport_ip
    zone.owntone_port = owntone_port

    _write_text(_state_path(grp_dir, "owntone_api_ip.txt"), api_ip)
    _write_text(_state_path(grp_dir, "owntone_bridge_ip.txt"), bridge_ip)
    _write_text(_state_path(grp_dir, "owntone_port.txt"), owntone_port)
    _write_text(_state_path(grp_dir, "owntone_netns.txt"), OWNTONE_SENDER_NS)
    _write_text(_state_path(grp_dir, "shairport_ip.txt"), shairport_ip)

    shairport_proc = _popen_isolated(
        _receiver_run_dir(zone),
        receiver_ns,
        ["chrt", "-f", "50", _binary("shairport-sync"),
         "-c", os.path.join(grp_dir, "config", "shairport-sync.conf"),
         "--statistics"],
        os.path.join(grp_dir, "logs", "shairport.log"),
    )
    zone.shairport_pid = shairport_proc.pid
    _write_text(_state_path(grp_dir, "shairport.pid"), shairport_proc.pid)
    log.info("Started shairport-sync for %s in %s at %s (pid %d)",
             zone.zone_id, receiver_ns, shairport_ip, shairport_proc.pid)

    owntone_proc = _popen_isolated(
        _sender_run_dir(),
        OWNTONE_SENDER_NS,
        ["chrt", "-f", "50", _binary("owntone"), "-f",
         "-c", os.path.join(grp_dir, "config", "owntone.conf"),
         "--mdns-no-rsp", "--mdns-no-daap", "--mdns-no-web", "--mdns-no-cname"],
        os.path.join(grp_dir, "logs", "owntone_wrapper.log"),
    )
    zone.owntone_pid = owntone_proc.pid
    _write_text(_state_path(grp_dir, "owntone.pid"), owntone_proc.pid)
    log.info("Started OwnTone for %s in %s port %d api %s bridge %s (pid %d)",
             zone.zone_id, OWNTONE_SENDER_NS, owntone_port, api_ip, bridge_ip, owntone_proc.pid)


def _wait_for_owntone(zone, timeout=60):
    """Wait for the generated OwnTone API endpoint, then poll the API."""
    ip_file = os.path.join(zone.grp_dir, "state", "owntone_api_ip.txt")

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
    zone.owntone_api = OwnToneAPI(zone.owntone_ip, port=port)

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


def _start_mixer(zone):
    """
    Start the host audio mixer.
    It captures ALSA loopback, overlays live streamed TTS, and writes OwnTone's audio.pipe.
    Runs on host beside Shairport and OwnTone.
    """
    # Generate the supervisor script from template
    script_path = generate_mixer_supervisor(zone)

    log_path = os.path.join(zone.grp_dir, "logs", "mixer.log")
    # Don't use context manager - file must stay open for subprocess lifetime
    log_file = open(log_path, "w")
    proc = subprocess.Popen(
        ["bash", script_path],
        stdout=log_file, stderr=subprocess.STDOUT
    )
    zone.mixer_pid = proc.pid
    log.info("Started mixer supervisor for %s (pid %d)", zone.zone_id, proc.pid)
