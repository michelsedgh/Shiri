"""Capability/credential boundaries and kernel-bound daemon cleanup regressions."""

import asyncio
from copy import deepcopy
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from shiri.runtime import units
from shiri.runtime.identities import DaemonIdentities, PURPOSE, expected_accounts
from shiri.runtime.network import NetworkManager
from shiri.runtime.system import RuntimeFailure

INSTALLATION = "ebdb6996-f3a2-4e38-8d5c-9fbdfc9849cb"
BOOT = "2d5b0d48-8a5c-4f28-af13-39c20b8375ea"
NATIVE = Path(__file__).parent / "native/check_avahi_patch.py"


@pytest.fixture(autouse=True)
def boot(monkeypatch):
    monkeypatch.setattr(units, "boot_id", lambda: BOOT)


def spec(role="shairport", *, ptp=False):
    return units.UnitSpec(
        units.new_unit("ebdb6996", "sender", role), role,
        "shiri-timing" if ptp else "shiri-receiver-7",
        "shiri-timing" if ptp else "shiri-receiver-7",
        ("/opt/shiri/sbin/" + role, "-v"), namespace="/run/netns/shiri_rx_abcdef12",
        binds=(units.Bind("/var/lib/shiri-runtime/config", "/run/shiri-worker/config"),), ptp=ptp,
    )


def actual(service):
    words = service.properties()["DeviceAllow"].split()
    return {
        "Id": service.name, "Description": service.description, "Transient": True,
        "User": service.user, "Group": service.group, "NetworkNamespacePath": service.namespace,
        "ExecStart": [[service.command[0], list(service.command), False, 0, 0, 0, 0, 201, 0, 0]],
        "KillMode": "control-group", "NoNewPrivileges": True,
        "CapabilityBoundingSet": 1024 if service.ptp else 0, "AmbientCapabilities": 1024 if service.ptp else 0,
        "ProtectSystem": "strict", "ProtectHome": "yes", "PrivateTmp": True,
        "PrivateDevices": True, "ProtectControlGroups": True, "ProtectKernelTunables": True,
        "ProtectKernelModules": True, "ProtectKernelLogs": True, "ProtectProc": "invisible",
        "RestrictSUIDSGID": True, "LockPersonality": True, "RestrictNamespaces": 0, "RestrictRealtime": not service.ptp,
        "DevicePolicy": "strict", "Delegate": False, "TasksMax": 128, "MemoryMax": 768 * 1024 * 1024,
        "TimeoutStopUSec": 6_000_000, "UMask": 0o022 if service.ptp else 0o077,
        "Environment": list(service.environment), "SupplementaryGroups": list(service.supplementary_groups),
        "BindPaths": [], "BindReadOnlyPaths": [[item.source, item.target, False, 0] for item in service.binds],
        "DeviceAllow": [words[index:index + 2] for index in range(0, len(words), 2)],
        "RestrictAddressFamilies": [True, service.properties()["RestrictAddressFamilies"].split()],
        "SystemCallFilter": [False, ["mount", "umount2", "ptrace", "process_vm_readv", "process_vm_writev", "reboot"]],
        "InvocationID": list(bytes.fromhex("11" * 16)), "ControlGroup": f"/system.slice/{service.name}",
        "ActiveState": "active", "MainPID": 201, "ExecMainStatus": 0,
        "InaccessiblePaths": list(service.inaccessible), "LimitCORE": 0, "LimitMEMLOCK": 8388608 if service.ptp else 0,
        "LimitRTPRIO": 5 if service.ptp else 0, "SendSIGKILL": True, "Restart": "no",
        "StandardOutput": "journal", "StandardError": "journal",
    }


def manager():
    return units.UnitManager(SimpleNamespace(run=AsyncMock(), start=AsyncMock()), Mock())


@pytest.mark.parametrize("field,value", [
    ("User", "root"), ("Group", "root"), ("Transient", False), ("NoNewPrivileges", False),
    ("AmbientCapabilities", 1), ("CapabilityBoundingSet", 1 << 19),
    ("ProtectSystem", "no"), ("ProtectHome", "no"), ("Delegate", True),
    ("PrivateDevices", False), ("BindReadOnlyPaths", []),
    ("BindPaths", [["/etc/shiri", "/run/shiri-worker/config", False, 0]]),
    ("DeviceAllow", [["char-alsa", "rwm"]]), ("SupplementaryGroups", ["sudo"]),
    ("Environment", ["LD_PRELOAD=/tmp/attack.so"]),
    ("NetworkNamespacePath", "/run/netns/unowned"), ("KillMode", "process"),
    ("SystemCallFilter", [False, []]), ("RestrictAddressFamilies", [False, []]),
])
def test_changed_daemon_privilege_boundary_refuses_authority(field, value):
    service = spec()
    changed = actual(service) | {field: value}
    with pytest.raises(RuntimeFailure):
        manager().verify(service.intent(), changed)


def test_ptp_only_receives_low_port_bind_not_network_admin_or_scheduler_capability():
    service = spec("nqptp", ptp=True)
    assert manager().verify(service.intent(), actual(service))[0] == "11" * 16
    with pytest.raises(RuntimeFailure, match="Only PTP"):
        replace(spec(), ptp=True)
    assert service.properties()["LimitRTPRIO"] == "5"
    assert service.properties()["LimitMEMLOCK"] == "8388608"


def test_systemd249_debug_group_does_not_imply_cross_process_memory_calls():
    service = spec()
    properties = actual(service) | {"SystemCallFilter": [False, ["mount", "umount2", "ptrace", "reboot"]]}
    legacy = service.intent()
    legacy.pop("policy_version")
    legacy["properties"]["SystemCallFilter"] = units.SYSCALL_POLICY_V1
    assert manager().verify(legacy, properties)[0] == "11" * 16
    for missing in ["mount", "umount2", "ptrace", "reboot"]:
        changed = [name for name in properties["SystemCallFilter"][1] if name != missing]
        with pytest.raises(RuntimeFailure, match="syscall boundary"):
            manager().verify(legacy, properties | {"SystemCallFilter": [False, changed]})
    with pytest.raises(RuntimeFailure, match="syscall boundary"):
        manager().verify(service.intent(), properties)
    assert service.intent()["policy_version"] == 2
    assert "process_vm_readv process_vm_writev" in service.properties()["SystemCallFilter"]


@pytest.mark.parametrize("version", [0, 6, True, "2"])
def test_unknown_policy_version_cannot_admit_stop_authority(version):
    with pytest.raises(RuntimeFailure, match="policy version"):
        manager().verify(spec().intent() | {"policy_version": version}, actual(spec()))


def bridge_spec(owner='sender'):
    return units.UnitSpec(
        units.new_unit("ebdb6996", owner, "bluetooth-output"), "bluetooth-output",
        "shiri-bridge-7", "shiri-bridge-7", ("/usr/bin/python3.10", "-m", "shiri.runtime.bluetooth_output"),
        binds=(units.Bind("/var/lib/shiri-runtime/handoff", "/run/shiri-worker/handoff"),),
    )


def test_fd_only_bridge_uses_distinct_identity_and_unix_only_policy():
    service = bridge_spec()
    entry = service.intent()
    assert entry["policy_version"] == 5
    assert service.user == "shiri-bridge-7" and service.namespace == ""
    assert service.properties()["RestrictAddressFamilies"] == "AF_UNIX"
    assert service.properties()["AmbientCapabilities"] == service.properties()["CapabilityBoundingSet"] == ""
    assert "audio" not in service.properties().get("SupplementaryGroups", "")
    assert "/dev/snd/" not in service.properties()["DeviceAllow"]
    assert manager().verify(entry, actual(service))[0] == "11" * 16


def test_normal_uuid_room_bridge_name_is_valid_and_retains_its_exact_role():
    service = bridge_spec(INSTALLATION)
    parsed = units.UNIT_RE.fullmatch(service.name)
    assert parsed.groupdict() | {'nonce': 'ignored'} == {
        'installation': 'ebdb6996', 'owner': INSTALLATION.replace('-', ''),
        'role': 'bluetooth-output', 'nonce': 'ignored',
    }
    assert service.intent()['policy_version'] == 5
    assert manager().verify(service.intent(), actual(service))[0] == '11'*16
    with pytest.raises(RuntimeFailure, match='owned daemon unit identity'):
        replace(service, name=units.new_unit('ebdb6996', INSTALLATION, 'shairport'))


@pytest.mark.asyncio
async def test_uuid_room_v5_bridge_reloads_and_recovers_its_exact_durable_kernel_owner(tmp_path, kernel_group):
    service = bridge_spec(INSTALLATION)
    source = NetworkManager(tmp_path, SimpleNamespace())
    source.manifest['installation_id'] = INSTALLATION
    source.save()
    source = NetworkManager(tmp_path, SimpleNamespace())
    entry = service.intent() | {'invocation_id': '11'*16, 'control_group': f'/system.slice/{service.name}',
                               'cgroup_inode': kernel_group.directory.stat().st_ino}
    key = INSTALLATION+':bluetooth-output'
    source.reserve_unit(key, entry)
    recovered = NetworkManager(tmp_path, SimpleNamespace())
    assert recovered.manifest['processes'][key] == entry
    owner = units.UnitManager(SimpleNamespace(), recovered)
    owner.inspect = AsyncMock(return_value=actual(service))
    owner.open_cgroup, owner.cgroup_write = kernel_group.opened, kernel_group.write
    recovered.runner = SimpleNamespace(stop_saved=owner.stop_saved)
    await recovered.recover()
    assert not NetworkManager(tmp_path, SimpleNamespace()).manifest['processes']
    assert len(kernel_group.signals) == 2
    assert kernel_group.writes == [('cgroup.freeze', b'1'), ('cgroup.freeze', b'0')]


@pytest.mark.parametrize("changes", [
    {"user": "shiri-output-7", "group": "shiri-output-7"},
    {"namespace": "/run/netns/shiri_ot_abcdef12"},
    {"environment": ("DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/dbus/system_bus_socket",)},
    {"environment": ("ALSA_CONFIG_PATH=/run/shiri-worker/config/alsa.conf",)},
    {"devices": ("/dev/snd/pcmC1D1p",)}, {"supplementary_groups": ("audio",)},
    {"ptp": True}, {"inaccessible": ()},
    {"binds": (units.Bind("/run/dbus", "/run/dbus"),)},
])
def test_fd_only_bridge_cannot_regain_host_bus_or_shared_output_credentials(changes):
    with pytest.raises(RuntimeFailure):
        replace(bridge_spec(), **changes)


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_bridge_cannot_be_reinterpreted_as_an_older_saved_policy(version):
    with pytest.raises(RuntimeFailure, match="policy version"):
        manager().verify(bridge_spec().intent() | {"policy_version": version}, actual(bridge_spec()))


def test_bridge_observed_network_access_cannot_pass_ownership_verification():
    service = bridge_spec()
    with pytest.raises(RuntimeFailure, match="socket-family"):
        manager().verify(service.intent(), actual(service) | {
            "RestrictAddressFamilies": [True, ["AF_UNIX", "AF_INET"]],
        })


@pytest.mark.asyncio
async def test_legacy_host_bus_bridge_is_recovery_only_and_cannot_launch(tmp_path):
    service = units.UnitSpec(
        units.new_unit("ebdb6996", "sender", "local-output"), "local-output",
        "shiri-output-7", "shiri-output-7", ("/usr/bin/python3.10", "-m", "shiri.runtime.local_output"),
        environment=("DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/dbus/system_bus_socket",),
        inaccessible=(),
    )
    owner = manager()
    assert owner.verify(service.intent(), actual(service))[0] == "11" * 16
    owner.inspect = AsyncMock()
    with pytest.raises(RuntimeFailure, match="Legacy host-bus"):
        await owner.start("sender:local-output", service, tmp_path / "log")
    owner.inspect.assert_not_awaited()
    owner.manifest.reserve_unit.assert_not_called()
    owner.runner.run.assert_not_awaited()


def test_replacement_invocation_or_boot_is_not_stop_authority():
    service = spec()
    entry = service.intent() | {"invocation_id": "22" * 16}
    with pytest.raises(RuntimeFailure, match="invocation was replaced"):
        manager().verify(entry, actual(service))
    entry["boot_id"] = "another-boot"
    with pytest.raises(RuntimeFailure, match="another boot"):
        manager().verify(entry, actual(service))


@pytest.mark.asyncio
async def test_unit_name_collision_never_launches_or_overwrites_intent(tmp_path):
    service, owner = spec(), manager()
    owner.inspect = AsyncMock(return_value=actual(service))
    with pytest.raises(RuntimeFailure, match="already exists"):
        await owner.start("room:shairport", service, tmp_path / "log")
    owner.runner.run.assert_not_awaited()
    owner.manifest.reserve_unit.assert_not_called()


@pytest.fixture
def kernel_group(tmp_path, monkeypatch):
    directory = tmp_path / "cgroup"
    directory.mkdir()
    (directory / "cgroup.events").write_text("populated 1\nfrozen 0\n")
    (directory / "cgroup.procs").write_text("201\n202\n")
    (directory / "cgroup.freeze").write_text("0")
    (directory / "cgroup.kill").write_text("0")
    writes, signals = [], []
    state = {"populated": True, "frozen": False}

    def opened(entry):
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        if entry.get("cgroup_inode") and entry["cgroup_inode"] != os.fstat(descriptor).st_ino:
            os.close(descriptor)
            raise RuntimeFailure("Daemon cgroup was replaced")
        return descriptor

    def write(descriptor, filename, value):
        writes.append((filename, value))
        if filename == "cgroup.freeze":
            state["frozen"] = value == b"1"
        if filename == "cgroup.kill":
            state["populated"] = False
        (directory / "cgroup.events").write_text(
            f"populated {int(state['populated'])}\nfrozen {int(state['frozen'])}\n"
        )

    def send(handle, number):
        signals.append((handle, number))
        state["populated"] = False

    monkeypatch.setattr(units.os, "pidfd_open", lambda pid, flags: os.open("/dev/null", os.O_RDONLY), raising=False)
    monkeypatch.setattr(units.signal, "pidfd_send_signal", send, raising=False)
    monkeypatch.setattr(units.asyncio, "sleep", AsyncMock())
    return SimpleNamespace(directory=directory, opened=opened, write=write,
                           writes=writes, signals=signals, state=state)


@pytest.mark.asyncio
async def test_failed_freeze_write_always_thaws_the_exact_owned_group(kernel_group):
    service, owner = spec(), manager()
    owner.inspect = AsyncMock(return_value=actual(service))
    owner.open_cgroup = kernel_group.opened

    def partially_applied_write(descriptor, filename, value):
        kernel_group.write(descriptor, filename, value)
        if filename == "cgroup.freeze" and value == b"1":
            raise RuntimeFailure("The control write failed after taking effect")

    owner.cgroup_write = partially_applied_write
    with pytest.raises(RuntimeFailure, match="after taking effect"):
        await owner.stop_saved(service.intent())
    assert kernel_group.writes == [("cgroup.freeze", b"1"), ("cgroup.freeze", b"0")]
    assert not kernel_group.state["frozen"]
    assert not kernel_group.signals


@pytest.mark.asyncio
async def test_stop_uses_frozen_kernel_group_and_pidfds_never_manager_name_kill(kernel_group):
    service, owner = spec(), manager()
    owner.inspect = AsyncMock(return_value=actual(service))
    owner.open_cgroup, owner.cgroup_write = kernel_group.opened, kernel_group.write
    owner._call = AsyncMock()
    entry = service.intent()
    await owner.stop_saved(entry)
    assert len(kernel_group.signals) == 2
    assert kernel_group.writes == [("cgroup.freeze", b"1"), ("cgroup.freeze", b"0")]
    assert entry["cgroup_inode"] == kernel_group.directory.stat().st_ino
    owner._call.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [1, 2, 4])
async def test_saved_legacy_host_bus_bridge_still_stops_only_its_exact_kernel_group(kernel_group, version):
    payload = ("/usr/bin/python3.10", "-m", "shiri.runtime.local_output")
    pcm = units.PCMExec("/opt/shiri/libexec/shiri-pcm-exec", payload) if version == 4 else None
    service = units.UnitSpec(
        units.new_unit("ebdb6996", "sender", "local-output"), "local-output",
        "shiri-output-7", "shiri-output-7", pcm.command() if pcm else payload,
        environment=("DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/dbus/system_bus_socket",), inaccessible=(),
        devices=("/dev/snd/controlC1", "/dev/snd/pcmC1D0c") if pcm else (), pcm_exec=pcm,
    )
    entry = service.intent()
    if version == 1:
        entry["policy_version"] = 1
        entry["properties"]["SystemCallFilter"] = units.SYSCALL_POLICY_V1
    owner = manager()
    owner.inspect = AsyncMock(return_value=actual(service))
    owner.open_cgroup, owner.cgroup_write = kernel_group.opened, kernel_group.write
    owner._call = AsyncMock()
    await owner.stop_saved(entry)
    assert entry["policy_version"] == version
    assert len(kernel_group.signals) == 2
    assert kernel_group.writes == [("cgroup.freeze", b"1"), ("cgroup.freeze", b"0")]
    owner._call.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement_point", [1, 2])
async def test_replacement_during_frozen_snapshot_gets_no_signal_and_is_thawed(kernel_group, replacement_point):
    service, owner = spec(), manager()
    good = actual(service)
    foreign = good | {"InvocationID": list(bytes.fromhex("33" * 16))}
    owner.inspect = AsyncMock(side_effect=[good] * replacement_point + [foreign])
    owner.open_cgroup, owner.cgroup_write = kernel_group.opened, kernel_group.write
    with pytest.raises(RuntimeFailure, match="invocation was replaced"):
        await owner.stop_saved(service.intent())
    assert not kernel_group.signals
    assert kernel_group.writes[-1] == ("cgroup.freeze", b"0")


@pytest.mark.asyncio
async def test_cgroup_inode_replacement_is_refused_before_freeze(kernel_group):
    service, owner = spec(), manager()
    owner.inspect = AsyncMock(return_value=actual(service))
    owner.open_cgroup, owner.cgroup_write = kernel_group.opened, kernel_group.write
    with pytest.raises(RuntimeFailure, match="cgroup was replaced"):
        await owner.stop_saved(service.intent() | {"cgroup_inode": 1})
    assert not kernel_group.writes and not kernel_group.signals


@pytest.mark.asyncio
async def test_nested_cgroup_refuses_broad_process_capture_and_releases_freeze(kernel_group):
    service, owner = spec(), manager()
    (kernel_group.directory / "unexpected-child").mkdir()
    owner.inspect = AsyncMock(return_value=actual(service))
    owner.open_cgroup, owner.cgroup_write = kernel_group.opened, kernel_group.write
    with pytest.raises(RuntimeFailure, match="nested cgroup"):
        await owner.stop_saved(service.intent())
    assert not kernel_group.signals and kernel_group.writes[-1] == ("cgroup.freeze", b"0")


@pytest.mark.asyncio
async def test_replacement_before_final_kill_preserves_foreign_invocation(kernel_group, monkeypatch):
    service, owner = spec(), manager()
    good = actual(service)
    replacement = good | {"InvocationID": list(bytes.fromhex("44" * 16))}
    owner.inspect = AsyncMock(side_effect=[good, good, good, replacement])
    owner.open_cgroup, owner.cgroup_write = kernel_group.opened, kernel_group.write
    # TERM is ignored by the original worker. Final termination must perform
    # another invocation check rather than killing whatever owns its old name.
    monkeypatch.setattr(units.signal, "pidfd_send_signal",
                        lambda handle, number: kernel_group.signals.append((handle, number)))
    with pytest.raises(RuntimeFailure, match="invocation was replaced"):
        await owner.stop_saved(service.intent())
    assert len(kernel_group.signals) == 2
    assert all(name != "cgroup.kill" for name, _ in kernel_group.writes)
    assert not kernel_group.state["frozen"]


@pytest.mark.asyncio
async def test_cgroup_path_replacement_after_freeze_does_not_receive_signal(kernel_group, tmp_path):
    service, owner = spec(), manager()
    owner.inspect = AsyncMock(return_value=actual(service))
    foreign = tmp_path / "foreign-cgroup"
    foreign.mkdir()
    opened = 0

    def replacing_path(entry):
        nonlocal opened
        opened += 1
        return kernel_group.opened(entry) if opened == 1 else os.open(foreign, os.O_RDONLY | os.O_DIRECTORY)

    owner.open_cgroup, owner.cgroup_write = replacing_path, kernel_group.write
    with pytest.raises(RuntimeFailure, match="changed before signal admission"):
        await owner.stop_saved(service.intent())
    assert not kernel_group.signals
    assert kernel_group.writes == [("cgroup.freeze", b"1"), ("cgroup.freeze", b"0")]
    assert not list(foreign.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("removal_phase", ["before_thaw", "after_thaw"])
async def test_kernel_removes_empty_owned_cgroup_without_touching_replacement(kernel_group, monkeypatch, removal_phase):
    service, owner = spec(), manager()
    owner.inspect = AsyncMock(return_value=actual(service))
    owner.open_cgroup = kernel_group.opened
    inode = kernel_group.directory.stat().st_ino
    removed = False
    # The fake models the kernel invariant: only an empty cgroup is removed.
    # Its held inode retains authority even if its former path is reused.
    monkeypatch.setattr(units, "is_cgroup2", lambda fd: os.fstat(fd).st_ino == inode)

    def remove_empty():
        nonlocal removed
        for path in kernel_group.directory.iterdir():
            path.unlink()
        kernel_group.directory.rmdir()
        kernel_group.directory.mkdir()
        (kernel_group.directory / "cgroup.events").write_text("populated 1\nfrozen 0\n")
        removed = True

    def write(fd, filename, value):
        if removed:
            # Relative lookup through the original unlinked directory fails;
            # looking up by its old name would incorrectly reach the foreign one.
            raise FileNotFoundError(filename)
        kernel_group.write(fd, filename, value)
        if filename == "cgroup.freeze" and value == b"0" and removal_phase == "after_thaw":
            remove_empty()

    def send(fd, number):
        kernel_group.signals.append((fd, number))
        kernel_group.state["populated"] = False
        if removal_phase == "before_thaw" and not removed:
            remove_empty()

    owner.cgroup_write = write
    monkeypatch.setattr(units.signal, "pidfd_send_signal", send)
    await owner.stop_saved(service.intent())
    assert removed and len(kernel_group.signals) == 2
    assert (kernel_group.directory / "cgroup.events").read_text() == "populated 1\nfrozen 0\n"
    assert not (kernel_group.directory / "cgroup.kill").exists()


def test_missing_population_file_on_non_kernel_filesystem_is_not_termination_proof(tmp_path, monkeypatch):
    directory = tmp_path / "untrusted"
    directory.mkdir()
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.setattr(units, "is_cgroup2", lambda _: False)
    try:
        with pytest.raises(RuntimeFailure, match="non-kernel"):
            manager().cgroup_events(descriptor)
    finally:
        os.close(descriptor)


def test_permission_failure_reading_population_remains_failure(tmp_path):
    owner = manager()
    owner.cgroup_read = Mock(side_effect=PermissionError("permission denied"))
    with pytest.raises(PermissionError):
        owner.cgroup_events(123)


def test_output_tcp_listener_is_exactly_one_room_port_with_dynamic_ipv4_udp_only():
    service = units.UnitSpec(
        units.new_unit("ebdb6996", "sender", "owntone"), "owntone", "shiri-output-7", "shiri-output-7",
        ("/opt/shiri/sbin/owntone",), listen_port=3939,
    )
    properties = actual(service) | {"SocketBindAllow": [[2, 6, 1, 3939], [2, 17, 0, 0]],
                                    "SocketBindDeny": [[0, 0, 0, 0]]}
    assert manager().verify(service.intent(), properties)[0] == "11" * 16
    for changed in ([ [2, 6, 0, 0], [2, 17, 0, 0] ], [ [2, 6, 1, 3929], [2, 17, 0, 0] ]):
        with pytest.raises(RuntimeFailure, match="HTTP bind isolation"):
            manager().verify(service.intent(), properties | {"SocketBindAllow": changed})
    with pytest.raises(RuntimeFailure, match="HTTP bind isolation"):
        manager().verify(service.intent(), properties | {"SocketBindDeny": []})


def managed_accounts():
    accounts, groups, saved = {}, {}, {}
    for index, (role, name) in enumerate(expected_accounts().items(), 200):
        accounts[name] = SimpleNamespace(pw_name=name, pw_uid=index, pw_gid=index,
            pw_shell="/usr/sbin/nologin", pw_gecos=f"{PURPOSE} {role} {INSTALLATION}",
            pw_dir=f"/nonexistent/shiri/{name}")
        groups[name] = SimpleNamespace(gr_name=name, gr_gid=index, gr_mem=[])
        saved[role] = {"name": name, "uid": index, "gid": index}
    accounts["shiri"] = SimpleNamespace(pw_uid=150, pw_gid=150)
    return accounts, groups, saved


@pytest.mark.parametrize("change", ["api_uid", "root_uid", "duplicate_uid", "shell", "home", "purpose", "group", "supplementary"])
def test_static_account_drift_is_refused_without_mutation(monkeypatch, change):
    accounts, groups, saved = managed_accounts()
    managed = DaemonIdentities(Path("/etc/shiri/daemon-identities.json"))
    managed.installation_id, managed.accounts = INSTALLATION, saved
    name = saved["slot7.receiver"]["name"]
    account = accounts[name]
    if change == "api_uid":
        account.pw_uid = 150
    elif change == "root_uid":
        account.pw_uid = 0
    elif change == "duplicate_uid":
        account.pw_uid = saved["slot6.receiver"]["uid"]
    elif change == "shell":
        account.pw_shell = "/bin/bash"
    elif change == "home":
        account.pw_dir = "/home/user"
    elif change == "purpose":
        account.pw_gecos = PURPOSE + " another installation"
    elif change == "group":
        groups[name].gr_gid += 1000
    else:
        groups["sudo"] = SimpleNamespace(gr_name="sudo", gr_gid=100, gr_mem=[name])
    monkeypatch.setattr("shiri.runtime.identities.pwd.getpwnam", accounts.__getitem__)
    monkeypatch.setattr("shiri.runtime.identities.grp.getgrnam", groups.__getitem__)
    monkeypatch.setattr("shiri.runtime.identities.grp.getgrall", lambda: list(groups.values()))
    with pytest.raises(RuntimeFailure):
        managed.validate()


def test_all_daemon_accounts_are_distinct_nonlogin_and_do_not_share_api_identity(monkeypatch):
    accounts, groups, saved = managed_accounts()
    managed = DaemonIdentities(Path("/etc/shiri/daemon-identities.json"))
    managed.installation_id, managed.accounts = INSTALLATION, saved
    monkeypatch.setattr("shiri.runtime.identities.pwd.getpwnam", accounts.__getitem__)
    monkeypatch.setattr("shiri.runtime.identities.grp.getgrnam", groups.__getitem__)
    monkeypatch.setattr("shiri.runtime.identities.grp.getgrall", lambda: list(groups.values()))
    managed.validate()
    assert len({item["uid"] for item in saved.values()}) == 50
    assert saved["slot7.bridge"]["uid"] != saved["slot7.output"]["uid"]


def test_native_avahi_patch_preserves_defaults_and_enforces_exact_private_credentials():
    loader = importlib.util.spec_from_file_location("avahi_native_review", NATIVE)
    module = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(module)
    assert module.run_tests()["ok"]


def test_typed_ubuntu_manager_reply_uses_exact_argv_and_invocation_byte_array():
    service = spec()
    properties = actual(service)
    envelope = {"type": "a{sv}", "data": [{key: {"type": "v", "data": value} for key, value in properties.items()}]}
    decoded = units.decode_properties(units.decode_bus_reply(json.dumps(envelope)))
    assert manager().verify(service.intent(), decoded)[0] == "11" * 16


def room_runtime(tmp_path, *, revision=1):
    from uuid import uuid4
    from shiri.domain import Room
    from shiri.runtime.broker import Broker, RuntimeRoom
    from shiri.settings import Settings
    broker = Broker(Settings(state_dir=tmp_path / "api", runtime_state_dir=tmp_path / "root",
                             runtime_dir=tmp_path / "run", api_token_file=tmp_path / "config" / "secret",
                             daemon_identity_file=tmp_path / "config" / "daemon-identities.json"))
    room = RuntimeRoom(Room(id=str(uuid4()), slot=7, name="Test", airplay_name="Test zone",
                            interface="eth0", enabled=True, volume=50, revision=revision), tmp_path / "room")
    room.directory.mkdir()
    room.launch_generation = "aa" * 16
    async def accept(method, path, *, json):
        return json
    room.client = SimpleNamespace(request=AsyncMock(side_effect=accept))
    return broker, room


def native_event(room, *, volume=18, base=1):
    from uuid import uuid4
    return {"launch_generation": room.launch_generation, "event_id": str(uuid4()), "base_revision": base,
            "token": {"zone_id": room.desired.id, "incarnation": str(uuid4()), "session_id": str(uuid4()),
                      "epoch": 3, "protocol": "airplay2"}, "generation": 4, "volume": volume}


@pytest.mark.asyncio
async def test_native_volume_retry_receipt_never_reapplies_after_newer_ui_intent(tmp_path):
    from uuid import UUID
    broker, room = room_runtime(tmp_path)
    event = native_event(room)
    accepted = await broker._native_volume(room, event)
    assert accepted["durable"] and accepted["accepted"]
    saved = json.loads((room.directory / "phone-volume.json").read_text())
    assert saved["pending"]["id"] == UUID(event["event_id"]).hex
    assert saved["pending"]["base_revision"] == 1
    room.desired = room.desired.model_copy(update={"revision": 3, "volume": 72})
    room.current_volume = 72
    assert await broker._native_volume(room, event) == accepted
    assert room.current_volume == 72
    room.client.request.assert_awaited_once()


@pytest.mark.asyncio
async def test_native_volume_stale_captured_revision_never_touches_backend(tmp_path):
    broker, room = room_runtime(tmp_path, revision=3)
    receipt = await broker._native_volume(room, native_event(room, base=1))
    assert receipt["durable"] and not receipt["accepted"]
    assert room.phone_volume_update is None
    room.client.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_volume_reused_event_id_or_cross_room_is_rejected(tmp_path):
    from uuid import uuid4
    from shiri.rpc import RpcError
    broker, room = room_runtime(tmp_path)
    event = native_event(room)
    await broker._native_volume(room, event)
    with pytest.raises(RpcError, match="reused"):
        await broker._native_volume(room, event | {"volume": 19})
    altered = dict(event["token"], zone_id=str(uuid4()))
    with pytest.raises(RpcError, match="another room"):
        await broker._native_volume(room, event | {"token": altered})
    with pytest.raises(RpcError, match="launch"):
        await broker._native_volume(room, event | {"launch_generation": "cc" * 16})
    room.client.request.assert_awaited_once()


@pytest.mark.asyncio
async def test_native_volume_fenced_stale_owner_is_durable_rejection_but_failure_retries(tmp_path):
    from shiri.runtime.backend import OwnToneRejected
    broker, room = room_runtime(tmp_path)
    event = native_event(room)
    room.client.request.side_effect = OwnToneRejected("POST", "/api/player/shiri-volume", 409)
    receipt = await broker._native_volume(room, event)
    assert receipt["durable"] and not receipt["accepted"]
    assert room.phone_volume_update is None
    room.client.request.side_effect = OwnToneRejected("POST", "/api/player/shiri-volume", 503)
    with pytest.raises(OwnToneRejected):
        await broker._native_volume(room, native_event(room))
    assert len(room.native_volume_receipts) == 1


@pytest.mark.asyncio
async def test_failed_native_volume_durable_write_cannot_become_successful_retry(tmp_path, monkeypatch):
    broker, room = room_runtime(tmp_path)
    event = native_event(room)
    monkeypatch.setattr(broker, "_save_phone_volume", Mock(side_effect=OSError("full disk")))
    for _ in range(2):
        with pytest.raises(OSError, match="full disk"):
            await broker._native_volume(room, event)
        assert not room.native_volume_receipts
        assert room.phone_volume_update is None and room.current_volume == 50


@pytest.mark.asyncio
async def test_newer_intent_during_native_backend_ack_is_restored_and_not_queued(tmp_path):
    broker, room = room_runtime(tmp_path)
    event = native_event(room)
    async def newer(method, path, *, json):
        room.desired = room.desired.model_copy(update={"revision": 3, "volume": 72})
        return json
    room.client.request.side_effect = newer
    result = await broker._native_volume(room, event)
    assert not result["accepted"] and room.wake.is_set()
    assert room.phone_volume_update is None


@pytest.mark.asyncio
async def test_native_queue_receipts_survive_phone_store_ack_and_broker_restore(tmp_path):
    broker, room = room_runtime(tmp_path)
    broker.rooms[room.desired.id] = room
    event = native_event(room)
    await broker._native_volume(room, event)
    await broker.ack_phone_volume(room, {"update_id": room.phone_volume_update["id"],
                                       "accepted": True, "volume": 18, "committed_revision": 2})
    saved = json.loads((room.directory / "phone-volume.json").read_text())
    assert saved["pending"] is None and saved["native_receipts"]
    room.client.request.reset_mock()
    assert (await broker._native_volume(room, event))["accepted"]
    room.client.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_system_manager_transport_preserves_variant_types_and_exact_endpoint():
    from dbus_next import Message, MessageType, Variant
    service = spec()
    props = actual(service)
    variants = {"User": Variant("s", service.user), "MainPID": Variant("u", 201),
                "InvocationID": Variant("ay", bytes(props["InvocationID"]))}
    reply = Message(message_type=MessageType.METHOD_RETURN, reply_serial=1,
                    signature="a{sv}", body=[variants])
    bus = SimpleNamespace(call=AsyncMock(return_value=reply))
    owner = units.UnitManager(Mock(), Mock(), bus=bus)
    body, error = await owner._call("/unit", "org.freedesktop.DBus.Properties", "GetAll", "s", units.UNIT_INTERFACE)
    decoded = units.decode_properties(body)
    assert decoded == {"User": service.user, "MainPID": 201, "InvocationID": props["InvocationID"]}
    assert error is None
    message = bus.call.await_args.args[0]
    assert message.destination == "org.freedesktop.systemd1"
    assert message.body == [units.UNIT_INTERFACE]


@pytest.mark.asyncio
async def test_partial_unit_intent_stays_reserved_until_verified_stop_before_network(tmp_path):
    broker, room = room_runtime(tmp_path)
    entry = spec().intent()
    broker.network = SimpleNamespace(manifest={"processes": {room.desired.id + ":shairport": entry}},
                                     forget_process=Mock(), remove=AsyncMock())
    broker.runner.stop_saved = AsyncMock(side_effect=RuntimeFailure("invocation changed"))
    with pytest.raises(RuntimeFailure, match="invocation changed"):
        await broker._stop_room(room)
    broker.network.remove.assert_not_awaited()
    broker.network.forget_process.assert_not_called()


@pytest.mark.asyncio
async def test_daemon_launch_has_no_root_process_fallback_and_hides_actual_candidate_secrets(tmp_path):
    broker, room = room_runtime(tmp_path)
    broker.network = SimpleNamespace(installation_tag="ebdb6996")
    broker.runner.start = AsyncMock()
    account = {"name": "shiri-receiver-7", "uid": 200, "gid": 200}
    with pytest.raises(RuntimeFailure, match="system-manager"):
        await broker._start_process(room.desired.id + ":shairport", "shairport", ["/usr/bin/test"],
                                    tmp_path, account=account)
    broker.runner.start.assert_not_awaited()
    broker.unit_manager = SimpleNamespace(start=AsyncMock())
    await broker._start_process(room.desired.id + ":shairport", "shairport", ["/usr/bin/test"],
                                tmp_path, account=account)
    service = broker.unit_manager.start.await_args.args[1]
    assert service.user == "shiri-receiver-7" and not service.devices
    assert service.properties()["CapabilityBoundingSet"] == ""
    assert service.supplementary_groups == ()
    assert str(broker.config.api_token_file) in service.inaccessible
    assert str(broker.config.api_token_file.parent) in service.inaccessible
    assert str(broker.config.daemon_identity_file.parent) in service.inaccessible
    assert "/etc/shiri" not in service.inaccessible
    assert str(broker.config.state_dir) in service.inaccessible
    assert str(broker.config.runtime_dir) in service.inaccessible


@pytest.mark.asyncio
async def test_shared_playback_node_different_subdevices_is_rejected_before_launch(tmp_path):
    from uuid import uuid4
    from shiri.rpc import RpcError
    broker, room = room_runtime(tmp_path)
    first = room.desired.model_copy(update={"local_audio_device": "hw:CARD=USB,DEV=0,SUBDEV=0"})
    second = first.model_copy(update={"id": str(uuid4()), "slot": 6, "airplay_name": "Other zone",
                                      "local_audio_device": "plughw:CARD=USB,DEV=0,SUBDEV=1"})
    pin = SimpleNamespace(playback_node="/dev/snd/pcmC5D0p", close=Mock())
    broker._resolve_local_pin = Mock(return_value=pin)
    with pytest.raises(RpcError, match="separate ALSA playback"):
        await broker.reconcile({"rooms": [first.model_dump(), second.model_dump()]})
    assert not broker.rooms


@pytest.mark.asyncio
async def test_unknown_namespace_occupants_never_become_signal_authority(tmp_path, monkeypatch):
    from shiri.runtime.network import NetworkManager
    owner = NetworkManager(tmp_path, Mock())
    record = owner.new_record("sender", "eth0")
    pidfile = tmp_path / "client.pid"
    pidfile.write_text("201")
    record["pid_file"] = str(pidfile)
    owner.namespace_pids = AsyncMock(return_value=[201, 202])
    # Treat the disposable file as the root-provisioned pidfile; no live process
    # is opened or signalled in this regression.
    fstat = os.fstat
    def root_info(fd):
        info = list(fstat(fd))
        info[4] = 0
        return os.stat_result(info)
    monkeypatch.setattr("shiri.runtime.network.os.fstat", root_info)
    capture = Mock()
    monkeypatch.setattr("shiri.runtime.network.os.pidfd_open", capture, raising=False)
    with pytest.raises(RuntimeFailure, match="Unrecorded"):
        await owner._stop_dhcp_survivors(record)
    capture.assert_not_called()


INSPECTION_UNIT_KEYS = {"Id", "LoadState", "Transient", "InvocationID", "Description", "ActiveState", "SubState"}


def inspection_reply(values):
    """The normalized typed envelopes returned by the existing D-Bus transport."""
    arrays = {"InvocationID": "ay", "ExecStart": "a(sasbttttuii)",
              "SystemCallFilter": "(bas)", "RestrictAddressFamilies": "(bas)",
              "BindPaths": "a(ssbt)", "BindReadOnlyPaths": "a(ssbt)", "DeviceAllow": "a(ss)"}
    return [{key: {"type": arrays.get(key, "b" if type(value) is bool else
                                     "t" if type(value) is int else "s" if type(value) is str else "as"),
                   "data": deepcopy(value)} for key, value in values.items()}], None


def inspection_transaction(service, before, middle=None, after=None):
    """GetUnit resolves first; individual GetAll replies can then cross GC."""
    middle = before if middle is None else middle
    after = before if after is None else after
    path = "/org/freedesktop/systemd1/unit/exact_owned_fixture"
    return [([path], None), inspection_reply({k: v for k, v in before.items() if k in INSPECTION_UNIT_KEYS}),
            inspection_reply({k: v for k, v in middle.items() if k not in INSPECTION_UNIT_KEYS}),
            inspection_reply({k: v for k, v in after.items() if k in INSPECTION_UNIT_KEYS})]


def unloaded_snapshot(service):
    return {"Id": service.name, "LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead",
            "Transient": False, "InvocationID": [0] * 16, "Description": service.name,
            "ControlGroup": "", "MainPID": 0, "ControlPID": 0, "User": "", "Group": "", "ExecStart": []}


def inspection_owner(service, *transactions):
    owner = manager()
    owner._call = AsyncMock(side_effect=[reply for transaction in transactions for reply in transaction])
    owner.cgroup_empty = Mock(return_value=True)
    owner.open_cgroup = Mock(side_effect=AssertionError("No absent cgroup control admitted"))
    owner.cgroup_write = Mock(side_effect=AssertionError("No absent cgroup write admitted"))
    return owner


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["refresh", "stop_saved"])
@pytest.mark.parametrize("race", ["before_unit_properties", "between_unit_service", "after_service_properties"])
async def test_gc_during_getall_retires_only_typed_absence_without_any_signal(operation, race, tmp_path):
    service = spec()
    live = actual(service) | {"LoadState": "loaded", "SubState": "running", "ControlPID": 0}
    absent = unloaded_snapshot(service)
    if race == "before_unit_properties":
        transactions = [inspection_transaction(service, absent)]
    else:
        middle = absent if race == "between_unit_service" else live
        transactions = [inspection_transaction(service, live, middle, absent),
                        inspection_transaction(service, absent)]
    owner = inspection_owner(service, *transactions)
    entry = service.intent() | {"invocation_id": "11" * 16,
                               "control_group": "/system.slice/" + service.name, "cgroup_inode": 12345}
    original = deepcopy(entry)
    unit = units.OwnedUnit(service.role, owner, entry, tmp_path / "owned.log")
    if operation == "refresh":
        await owner.refresh(unit)
        assert not unit.alive and unit.process.returncode == 0
    else:
        await owner.stop_saved(entry)
        owner.cgroup_empty.assert_called_once_with(entry)
    assert entry == original
    assert owner._call.await_count == 4 * len(transactions)
    assert [call.args[2] for call in owner._call.await_args_list] == ["GetUnit", "GetAll", "GetAll", "GetAll"] * len(transactions)
    owner.open_cgroup.assert_not_called()
    owner.cgroup_write.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("Id", "foreign.service"), ("ActiveState", "active"), ("ActiveState", "failed"),
    ("SubState", "running"), ("Transient", True), ("MainPID", 123), ("ControlPID", 123),
    ("MainPID", False), ("ControlPID", None), ("ControlGroup", "/system.slice/foreign.service"),
    ("User", "root"), ("Group", "root"), ("ExecStart", [["/foreign/daemon"]]),
    ("InvocationID", [1] * 16), ("InvocationID", [0] * 15), ("InvocationID", [False] * 16),
])
async def test_not_found_never_hides_active_foreign_or_incomplete_properties(field, value):
    service = spec()
    absent = unloaded_snapshot(service) | {field: value}
    owner = inspection_owner(service, inspection_transaction(service, absent))
    with pytest.raises(RuntimeFailure, match="complete absence proof"):
        await owner.stop_saved(service.intent())
    owner.cgroup_empty.assert_not_called()
    owner.open_cgroup.assert_not_called()
    owner.cgroup_write.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("proof", [False, "uncertain", "replaced"])
async def test_manager_absence_cannot_release_populated_uncertain_or_replaced_old_cgroup(proof):
    service = spec()
    owner = inspection_owner(service, inspection_transaction(service, unloaded_snapshot(service)))
    if proof is False:
        owner.cgroup_empty.return_value = False
    else:
        owner.cgroup_empty.side_effect = RuntimeFailure("Exact old cgroup " + proof)
    with pytest.raises(RuntimeFailure, match="populated control group|Exact old cgroup"):
        await owner.stop_saved(service.intent())
    owner.cgroup_empty.assert_called_once()
    owner.open_cgroup.assert_not_called()
    owner.cgroup_write.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("InvocationID", [0x22] * 16), ("User", "root"), ("ExecStart", [["/foreign"]])])
async def test_loaded_replacement_and_changed_launch_policy_still_reject_unchanged_verify(field, value):
    service = spec()
    replacement = actual(service) | {"LoadState": "loaded", "SubState": "running", "ControlPID": 0, field: value}
    owner = inspection_owner(service, inspection_transaction(service, replacement))
    entry = service.intent() | {"invocation_id": "11" * 16}
    with pytest.raises(RuntimeFailure):
        await owner.stop_saved(entry)
    owner.cgroup_empty.assert_not_called()
    owner.open_cgroup.assert_not_called()
    owner.cgroup_write.assert_not_called()


@pytest.mark.asyncio
async def test_gc_then_loaded_foreign_replacement_cannot_be_classified_as_absent():
    service = spec()
    live = actual(service) | {"LoadState": "loaded", "SubState": "running", "ControlPID": 0}
    replacement = live | {"InvocationID": [0x22] * 16}
    owner = inspection_owner(service, inspection_transaction(service, live, replacement, replacement),
                             inspection_transaction(service, replacement))
    with pytest.raises(RuntimeFailure, match="invocation was replaced"):
        await owner.stop_saved(service.intent() | {"invocation_id": "11" * 16})
    assert owner._call.await_count == 8
    owner.cgroup_empty.assert_not_called()
    owner.open_cgroup.assert_not_called()
    owner.cgroup_write.assert_not_called()


@pytest.mark.asyncio
async def test_healthy_loaded_snapshot_keeps_every_original_property_and_launch_fence():
    service = spec()
    live = actual(service) | {"LoadState": "loaded", "SubState": "running", "ControlPID": 0}
    owner = inspection_owner(service, inspection_transaction(service, live))
    observed = await owner.inspect(service.name)
    assert observed == live and owner.verify(service.intent(), observed) == ("11" * 16, live["ControlGroup"])
    assert owner._call.await_count == 4
    owner.cgroup_empty.assert_not_called()
    owner.open_cgroup.assert_not_called()


@pytest.mark.asyncio
async def test_continuously_replaced_snapshot_exhausts_only_two_transactions():
    service = spec()
    first = actual(service) | {"LoadState": "loaded", "SubState": "running", "ControlPID": 0}
    second = first | {"InvocationID": [0x22] * 16}
    owner = inspection_owner(service, inspection_transaction(service, first, second, second),
                             inspection_transaction(service, second, first, first))
    with pytest.raises(RuntimeFailure, match="identity changed during inspection"):
        await owner.stop_saved(service.intent())
    assert owner._call.await_count == 8
    owner.cgroup_empty.assert_not_called()
    owner.open_cgroup.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("boundary", [0, 1, 2, 3])
async def test_complete_inspection_deadline_and_caller_cancel_join_every_owned_rpc(monkeypatch, cancel, boundary):
    from shiri.deadline import bounded
    service, owner = spec(), manager()
    started, release, joined = asyncio.Event(), asyncio.Event(), asyncio.Event()
    bounds = []
    replies = inspection_transaction(service, actual(service) | {"LoadState": "loaded"})
    calls = 0
    async def call(*_args, **_kwargs):
        nonlocal calls
        current, calls = calls, calls + 1
        if current < boundary:
            return replies[current]
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await release.wait()
            joined.set()
    async def short_bound(awaitable, seconds):
        bounds.append(seconds)
        return await bounded(awaitable, 30 if cancel else .001)
    monkeypatch.setattr(units, "bounded", short_bound)
    owner._call = call
    task = asyncio.create_task(owner.inspect(service.name))
    await started.wait()
    if cancel:
        task.cancel()
    else:
        await asyncio.sleep(.005)
    assert not task.done() and not joined.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeFailure, match=None if cancel else "15-second transaction deadline"):
        await task
    assert joined.is_set() and bounds == [15]
