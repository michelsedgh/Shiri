#!/usr/bin/env python3
"""Dedicated candidate systemd validation; never starts or changes live app units."""
from __future__ import annotations

from datetime import datetime, timezone
import grp
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import stat
import subprocess
import time
import traceback
import urllib.error
import urllib.request

SOURCE = Path("/opt/shiri-v2")
PYTHON = SOURCE / ".venv/bin/python"
BACKENDS = Path("/opt/shiri-v2-deps")
CONFIG = Path("/etc/shiri-v2-test")
API_STATE = Path("/var/lib/shiri-v2-test-api")
RUNTIME_STATE = Path("/var/lib/shiri-v2-test-runtime")
RUNTIME = Path("/run/shiri-v2-test")
RESULT = Path("/tmp/shiri-v2-service-check-result.json")
RUNTIME_UNIT = "shiri-v2-test-runtime.service"
API_UNIT = "shiri-v2-test-api.service"
UNITS = [RUNTIME_UNIT, API_UNIT]
MARKER = "# Managed by Shiri candidate service validation; isolated test units only."
BASE = "http://127.0.0.1:18082"
TOKEN = ""
REPORT = {"started": datetime.now(timezone.utc).isoformat(), "passed": False, "checks": [], "rooms_created": []}
REPORT["journal_since"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def clean(text):
    return text.replace(TOKEN, "<redacted>") if TOKEN else text


def command(args, *, check=True, timeout=40):
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(clean(f"{args[:3]} failed ({result.returncode}): {result.stderr[-3000:] or result.stdout[-3000:]}"))
    return result


def record(name, **details):
    REPORT["checks"].append({"name": name, **details})


def host_network_snapshot():
    links = json.loads(command(["ip", "-j", "link", "show"]).stdout)
    addresses = json.loads(command(["ip", "-j", "addr", "show"]).stdout)
    return {
        "links": sorted((link["ifname"], link.get("address"), link.get("linkinfo", {}).get("info_kind")) for link in links),
        "addresses": sorted((interface["ifname"], address.get("family"), address.get("local"), address.get("prefixlen"))
                            for interface in addresses for address in interface.get("addr_info", [])),
    }


def private_write(path, text, *, uid=0, gid=0, mode=0o600):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, mode)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, uid} or info.st_nlink != 1:
            raise RuntimeError(f"Refusing an unowned or nonregular output: {path}")
        os.fchown(descriptor, uid, gid)
        os.fchmod(descriptor, mode)
        os.ftruncate(descriptor, 0)
        with os.fdopen(descriptor, "w") as stream:
            descriptor = -1
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def directory(path, uid, gid, mode):
    if path.is_symlink():
        raise RuntimeError(f"Refusing a symlink directory: {path}")
    path.mkdir(parents=True, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, uid}:
        raise RuntimeError(f"Refusing an unowned directory: {path}")
    os.chown(path, uid, gid)
    os.chmod(path, mode)


def properties(unit):
    result = command(["systemctl", "show", unit, "--property=MainPID,User,Group,ActiveState,SubState,ControlGroup,PrivateMounts,ProtectSystem,ProtectHome,NoNewPrivileges,ReadWritePaths,RestrictAddressFamilies,CapabilityBoundingSet"])
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def request(method, path, payload=None, *, authenticated=True):
    headers = {"Content-Type": "application/json"}
    if authenticated:
        headers["Authorization"] = "Bearer " + TOKEN
    data = json.dumps(payload).encode() if payload is not None else None
    query = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(query, timeout=8) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as response:
        return response.code, json.load(response)


def expect(method, path, payload=None, *, status=200, authenticated=True):
    observed, body = request(method, path, payload, authenticated=authenticated)
    if observed != status:
        raise RuntimeError(clean(f"{method} {path}: expected HTTP {status}, got {observed}: {body}"))
    return body


def wait_ready(timeout=45):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            status, body = request("GET", "/api/v1/health/ready", authenticated=False)
            if status == 200 and body.get("ready"):
                return body
            last = {"status": status, "body": body}
        except (OSError, ValueError) as exc:
            last = str(exc)
        time.sleep(0.25)
    raise RuntimeError(f"Candidate services did not become ready: {last}")


def wait_room(room_id, target, timeout=60):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        snapshot = expect("GET", "/api/v1/state")
        room = next((entry for entry in snapshot["rooms"] if entry["id"] == room_id), None)
        if room:
            last = room["runtime"]
            if last.get("status") == target:
                return room
            if last.get("status") == "error":
                raise RuntimeError(f"Candidate room failed: {last}")
        time.sleep(0.25)
    raise RuntimeError(f"Candidate room did not become {target}: {last}")


def verify_namespace_visibility(manifest, runtime_props):
    host_mount_ns = os.readlink("/proc/1/ns/mnt")
    broker_mount_ns = os.readlink(f"/proc/{runtime_props['MainPID']}/ns/mnt")
    assert broker_mount_ns == host_mount_ns, "Root setup broker cannot persist namespaces from a private mount view"
    namespaces = []
    REPORT["owned_networks"] = [dict(network) for network in manifest["networks"].values()]
    for key, network in manifest["networks"].items():
        path = Path("/run/netns") / network["namespace"]
        assert os.path.ismount(path), f"Owned namespace {key} is only a placeholder in the host mount namespace"
        assert path.stat().st_ino == network["inode"], f"Host namespace inode differs from owned record for {key}"
        command(["ip", "netns", "exec", network["namespace"], "true"])
        namespaces.append({"key": key, "namespace": network["namespace"], "inode": network["inode"]})
    return {"host_mount_namespace": host_mount_ns, "broker_mount_namespace": broker_mount_ns,
            "owned_namespaces": namespaces}


def crash_and_recover(room_id, original_manifest, original_props):
    # Only the exact managed candidate unit's current main PID is signaled.
    current = properties(RUNTIME_UNIT)
    assert current["MainPID"] == original_props["MainPID"] and int(current["MainPID"]) > 1
    assert int(Path(f"/proc/{current['MainPID']}/status").read_text().split("Uid:", 1)[1].split()[1]) == 0
    assert Path("/etc/systemd/system", RUNTIME_UNIT).read_text().startswith(MARKER + "\n")
    command(["systemctl", "kill", "--kill-who=main", "--signal=SIGKILL", RUNTIME_UNIT])
    deadline = time.monotonic() + 75
    replacement = None
    while time.monotonic() < deadline:
        observed = properties(RUNTIME_UNIT)
        if (observed["ActiveState"] == "active" and int(observed["MainPID"]) > 1
                and observed["MainPID"] != original_props["MainPID"]):
            replacement = observed
            break
        time.sleep(0.25)
    if replacement is None:
        raise RuntimeError("The dedicated root broker did not restart after SIGKILL")
    # Requires= can stop the API when its dependency dies. Resume only this test API.
    command(["systemctl", "start", API_UNIT], timeout=45)
    wait_ready(timeout=60)
    recovered = wait_room(room_id, "running", timeout=75)
    assert recovered["runtime"]["selected_ids"] == []
    assert recovered["runtime"]["processes"] and all(proc["alive"] for proc in recovered["runtime"]["processes"])
    manifest = json.loads((RUNTIME_STATE / "ownership.json").read_text())
    assert manifest["installation_id"] == original_manifest["installation_id"]
    assert set(manifest["networks"]) == set(original_manifest["networks"])
    for key, network in manifest["networks"].items():
        original = original_manifest["networks"][key]
        assert network["namespace"] == original["namespace"] and network["mac"] == original["mac"]
    visibility = verify_namespace_visibility(manifest, replacement)
    record("systemd_sigkill_recovery_with_host_visible_namespace_ownership",
           previous_broker_pid=int(original_props["MainPID"]), replacement_broker_pid=int(replacement["MainPID"]),
           installation_id=manifest["installation_id"], receiver_ip=recovered["runtime"]["receiver_ip"],
           processes=recovered["runtime"]["processes"], selected_ids=[], **visibility)
    return recovered


def configure():
    global TOKEN
    if os.geteuid() != 0 or not PYTHON.is_file():
        raise RuntimeError("Run as root in the staged Linux candidate environment")
    try:
        group = grp.getgrnam("shiri")
    except KeyError:
        command(["groupadd", "--system", "shiri"])
        group = grp.getgrnam("shiri")
    try:
        account = pwd.getpwnam("shiri")
    except KeyError:
        command(["useradd", "--system", "--gid", "shiri", "--home-dir", "/var/lib/shiri", "--shell", "/usr/sbin/nologin", "shiri"])
        account = pwd.getpwnam("shiri")
    if account.pw_gid != group.gr_gid:
        raise RuntimeError("Existing shiri user has a different primary group; preserve it for inspection")
    for unit in UNITS:
        path = Path("/etc/systemd/system") / unit
        if path.is_symlink() or (path.exists() and MARKER not in path.read_text()):
            raise RuntimeError(f"Refusing to replace an unrelated unit: {unit}")
    command(["systemctl", "stop", *UNITS], check=False, timeout=150)
    for unit in UNITS:
        info = properties(unit)
        if info.get("ActiveState") in {"active", "activating", "deactivating"} or int(info.get("MainPID", 0)):
            raise RuntimeError(f"The dedicated test unit did not stop: {unit}")
    directory(CONFIG, 0, group.gr_gid, 0o750)
    directory(API_STATE, account.pw_uid, group.gr_gid, 0o750)
    directory(RUNTIME_STATE, 0, 0, 0o700)
    directory(RUNTIME, 0, group.gr_gid, 0o750)
    ownership = RUNTIME_STATE / "ownership.json"
    if ownership.exists():
        manifest = json.loads(ownership.read_text())
        if manifest.get("networks") or manifest.get("processes"):
            raise RuntimeError("Candidate lifecycle still has owned resources; do not start concurrent service validation")
        REPORT["preserved_installation_id"] = manifest["installation_id"]
    # This explicitly disposable API database is archived intact for reproducibility.
    database = API_STATE / "shiri.sqlite3"
    if database.exists() or database.is_symlink():
        archive = API_STATE / "archive" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        archive.mkdir(parents=True)
        for path in [database, Path(str(database) + "-wal"), Path(str(database) + "-shm")]:
            if path.exists() or path.is_symlink():
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, account.pw_uid}:
                    raise RuntimeError("Disposable candidate database contains an unexpected file; preserved")
                shutil.move(str(path), archive / path.name)
        REPORT["previous_api_database_archive"] = str(archive)
    token_path = CONFIG / "api-token"
    if token_path.exists() or token_path.is_symlink():
        info = token_path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0:
            raise RuntimeError("Candidate token path is not an owned regular file")
        TOKEN = token_path.read_text().strip()
        if not 32 <= len(TOKEN) <= 256:
            raise RuntimeError("Existing candidate token is invalid; preserved")
        os.chown(token_path, 0, group.gr_gid)
        os.chmod(token_path, 0o640)
    else:
        TOKEN = secrets.token_urlsafe(48)
        private_write(token_path, TOKEN + "\n", gid=group.gr_gid, mode=0o640)
    environment = f"""SHIRI_STATE_DIR={API_STATE}
SHIRI_RUNTIME_STATE_DIR={RUNTIME_STATE}
SHIRI_RUNTIME_DIR={RUNTIME}
SHIRI_RUNTIME_SOCKET={RUNTIME}/runtime.sock
SHIRI_BINARY_DIR={BACKENDS}
SHIRI_API_TOKEN_FILE={CONFIG}/api-token
SHIRI_HOST=127.0.0.1
SHIRI_PORT=18082
SHIRI_MAX_ROOMS=8
LD_LIBRARY_PATH={BACKENDS}/lib
PYTHONPATH={SOURCE}
"""
    private_write(CONFIG / "shiri.env", environment, gid=group.gr_gid, mode=0o640)
    for source_name, unit in [("shiri-runtime.service", RUNTIME_UNIT), ("shiri-api.service", API_UNIT)]:
        text = (SOURCE / "deploy" / source_name).read_text()
        text = text.replace("@PREFIX@/venv/bin/python", str(PYTHON))
        text = text.replace("/etc/shiri/shiri.env", str(CONFIG / "shiri.env"))
        text = text.replace("shiri-runtime.service", RUNTIME_UNIT)
        text = text.replace("RuntimeDirectory=shiri\n", "RuntimeDirectory=shiri-v2-test\n")
        text = text.replace("/var/lib/shiri-runtime", str(RUNTIME_STATE))
        # Replace the exact API writable path, without replacing the runtime path twice.
        text = text.replace("ReadWritePaths=/var/lib/shiri\n", f"ReadWritePaths={API_STATE}\n")
        text = text.replace("Description=Shiri", "Description=Shiri candidate validation")
        text = text.replace("[Service]\n", f"[Service]\nWorkingDirectory={SOURCE}\n")
        private_write(Path("/etc/systemd/system") / unit, MARKER + "\n" + text, mode=0o644)
    command(["systemctl", "daemon-reload"])
    command(["systemd-analyze", "verify", *(str(Path("/etc/systemd/system") / unit) for unit in UNITS)], timeout=30)
    return account, group


def exercise(account, group):
    REPORT["host_network_before"] = host_network_snapshot()
    command(["systemctl", "start", RUNTIME_UNIT, API_UNIT], timeout=90)
    wait_ready()
    runtime_props, api_props = properties(RUNTIME_UNIT), properties(API_UNIT)
    for props in [runtime_props, api_props]:
        if props["ActiveState"] != "active":
            raise RuntimeError(f"A candidate unit is inactive: {props}")
    api_uid = Path(f"/proc/{api_props['MainPID']}/status").read_text().split("Uid:", 1)[1].split()[1]
    runtime_uid = Path(f"/proc/{runtime_props['MainPID']}/status").read_text().split("Uid:", 1)[1].split()[1]
    assert int(api_uid) == account.pw_uid and int(runtime_uid) == 0
    assert runtime_props["User"] == "root" and runtime_props["Group"] == "root"
    assert api_props["User"] == "shiri" and api_props["Group"] == "shiri"
    assert api_props["ProtectSystem"] == "strict" and api_props["NoNewPrivileges"] == "yes"
    socket_info = (RUNTIME / "runtime.sock").stat()
    assert stat.S_ISSOCK(socket_info.st_mode) and socket_info.st_uid == 0 and socket_info.st_gid == group.gr_gid
    assert stat.S_IMODE(socket_info.st_mode) == 0o660 and stat.S_IMODE(RUNTIME.stat().st_mode) == 0o750
    record("rootless_api_and_privileged_socket", api_uid=int(api_uid), runtime_uid=int(runtime_uid),
           socket_group=socket_info.st_gid, socket_mode=oct(stat.S_IMODE(socket_info.st_mode)),
           runtime_unit=runtime_props, api_unit=api_props)
    expect("GET", "/api/v1/state", status=401, authenticated=False)
    expect("POST", "/api/v1/session", {"token": "invalid-token"}, status=401, authenticated=False)
    expect("POST", "/api/v1/session", {"token": TOKEN}, authenticated=False)
    record("authentication", anonymous_state_status=401, invalid_login_status=401, valid_login_status=200)
    state = expect("GET", "/api/v1/state")
    assert state["runtime"]["ready"] and not state["runtime"]["simulation"]
    if not state["interfaces"]:
        raise RuntimeError("No eligible LAN interface is available for hardened DHCP validation")
    interface = state["interfaces"][0]
    for slot in range(8):
        created = expect("POST", "/api/v1/rooms", {"name": f"Candidate service test {slot}",
                          "airplay_name": f"Shiri candidate validation {slot}", "interface": interface}, status=201)
        room = created["room"]
        assert room["slot"] == slot and not room["enabled"] and created["runtime_accepted"]
        REPORT["rooms_created"].append(room)
    tested = REPORT["rooms_created"][-1]
    patched = expect("PATCH", f"/api/v1/rooms/{tested['id']}", {"expected_revision": tested["revision"],
                     "changes": {"name": "Candidate service slot seven", "volume": 17}})
    tested = patched["room"]
    REPORT["rooms_created"][-1] = tested
    assert tested["volume"] == 17 and not tested["enabled"]
    expect("PATCH", f"/api/v1/rooms/{tested['id']}", {"expected_revision": 1, "changes": {"volume": 80}}, status=409)
    db_info = (API_STATE / "shiri.sqlite3").stat()
    assert db_info.st_uid == account.pw_uid and stat.S_IMODE(db_info.st_mode) == 0o600
    record("durable_disabled_crud_and_revision", tested_slot=7, database_uid=db_info.st_uid,
           database_mode=oct(stat.S_IMODE(db_info.st_mode)), stale_update_status=409)
    enabled = expect("PATCH", f"/api/v1/rooms/{tested['id']}", {"expected_revision": tested["revision"], "changes": {"enabled": True}})
    tested = enabled["room"]
    REPORT["rooms_created"][-1] = tested
    active = wait_room(tested["id"], "running")
    assert active["slot"] == 7 and active["speakers"] == [] and active["runtime"]["selected_ids"] == []
    assert active["runtime"]["receiver_ip"] and all(proc["alive"] for proc in active["runtime"]["processes"])
    record("hardened_slot_seven_dhcp_backend_start", receiver_ip=active["runtime"]["receiver_ip"],
           processes=active["runtime"]["processes"], selected_ids=[])
    manifest = json.loads((RUNTIME_STATE / "ownership.json").read_text())
    record("host_visible_owned_namespace_mounts", **verify_namespace_visibility(manifest, runtime_props))
    sender = manifest["networks"]["sender"]
    receiver = manifest["networks"]["receiver:" + tested["id"]]
    listening = command(["ip", "netns", "exec", sender["namespace"], "ss", "-H", "-ltn"])
    tcp_ports = sorted({int(line.split()[3].rsplit(":", 1)[1]) for line in listening.stdout.splitlines()})
    assert tcp_ports == [3939], f"Unexpected candidate TCP listener: {tcp_ports}"
    anonymous = command(["ip", "netns", "exec", receiver["namespace"], str(PYTHON), "-c",
        "import urllib.request,urllib.error,sys\n"
        "try:\n r=urllib.request.urlopen(sys.argv[1],timeout=3); print(r.status)\n"
        "except urllib.error.HTTPError as e:\n print(e.code)\n",
        f"http://{sender['ip']}:3939/api/player"])
    assert anonymous.stdout.strip() == "401", f"Anonymous LAN backend access was not denied: {anonymous.stdout}"
    record("unused_backend_listeners_closed_and_lan_auth_required", sender_tcp_ports=tcp_ports,
           anonymous_backend_status=401)
    tested = crash_and_recover(tested["id"], manifest, runtime_props)
    REPORT["rooms_created"][-1] = tested
    disabled = expect("PATCH", f"/api/v1/rooms/{tested['id']}", {"expected_revision": tested["revision"], "changes": {"enabled": False}})
    tested = disabled["room"]
    REPORT["rooms_created"][-1] = tested
    stopped = wait_room(tested["id"], "stopped")
    assert stopped["runtime"]["processes"] == [] and stopped["runtime"]["receiver_ip"] is None
    record("hardened_room_disable_releases_resources", status=stopped["runtime"]["status"])
    for room in list(REPORT["rooms_created"]):
        deleted = expect("DELETE", f"/api/v1/rooms/{room['id']}?expected_revision={room['revision']}")
        assert deleted["ok"] and deleted["runtime_accepted"]
    assert expect("GET", "/api/v1/state")["rooms"] == []
    wait_ready()
    record("deleted_all_disposable_rooms", remaining_rooms=0)


def finish():
    errors = []
    for unit in [API_UNIT, RUNTIME_UNIT]:
        path = Path("/etc/systemd/system") / unit
        if path.is_file() and MARKER in path.read_text():
            result = command(["systemctl", "stop", unit], check=False, timeout=150)
            if result.returncode:
                errors.append(clean(result.stderr))
            command(["systemctl", "disable", unit], check=False)
    for unit in UNITS:
        journal = command(["journalctl", "-u", unit, "--since", REPORT["journal_since"], "--no-pager", "--output=short-iso"], check=False)
        private_write(Path("/tmp") / f"{unit}.validation.log", clean(journal.stdout + journal.stderr))
    ownership = RUNTIME_STATE / "ownership.json"
    if ownership.exists():
        manifest = json.loads(ownership.read_text())
        REPORT["final_manifest"] = {"installation_id": manifest.get("installation_id"),
                                    "networks": manifest.get("networks"), "processes": manifest.get("processes")}
        if manifest.get("networks") or manifest.get("processes"):
            errors.append("Test runtime shutdown left owned resources reserved")
        if REPORT.get("preserved_installation_id") and manifest.get("installation_id") != REPORT["preserved_installation_id"]:
            errors.append("Runtime installation identity changed")
    remaining_namespaces = []
    remaining_control_links = []
    for network in REPORT.get("owned_networks", []):
        path = Path("/run/netns") / network["namespace"]
        if path.exists():
            remaining_namespaces.append({"namespace": network["namespace"], "is_mount": os.path.ismount(path)})
        if interface := network.get("api_host_interface"):
            observed = command(["ip", "-j", "link", "show", "dev", interface], check=False)
            if observed.returncode == 0:
                remaining_control_links.extend(json.loads(observed.stdout))
    REPORT["remaining_owned_namespace_paths"] = remaining_namespaces
    REPORT["remaining_owned_control_links"] = remaining_control_links
    if remaining_namespaces or remaining_control_links:
        errors.append("Test service shutdown left owned host namespace paths or control links")
    slot_status = {}
    for endpoint in ["pcm0p", "pcm1c", "pcm1p", "pcm0c"]:
        path = Path("/proc/asound/Loopback") / endpoint / "sub7/status"
        if path.is_file():
            slot_status[endpoint] = path.read_text().strip()
    REPORT["final_slot_seven_status"] = slot_status
    if any(status != "closed" for status in slot_status.values()):
        errors.append("Test service shutdown did not release Loopback slot seven")
    if "host_network_before" in REPORT:
        REPORT["host_network_after"] = host_network_snapshot()
        if REPORT["host_network_before"] != REPORT["host_network_after"]:
            errors.append("Host network links or addresses differ from the test baseline")
    REPORT["shutdown_errors"] = errors
    if errors:
        REPORT["passed"] = False


def main():
    try:
        account, group = configure()
        exercise(account, group)
        REPORT["passed"] = True
    except Exception as exc:
        REPORT["error"] = clean(str(exc))
        REPORT["traceback"] = clean(traceback.format_exc())
    finally:
        try:
            finish()
        except Exception as exc:
            REPORT["passed"] = False
            REPORT["shutdown_exception"] = clean(str(exc))
        REPORT["finished"] = datetime.now(timezone.utc).isoformat()
        private_write(RESULT, clean(json.dumps(REPORT, indent=2)))
    print(json.dumps({"passed": REPORT["passed"], "result_file": str(RESULT), "checks": len(REPORT["checks"])}))
    raise SystemExit(0 if REPORT["passed"] else 1)


if __name__ == "__main__":
    main()
