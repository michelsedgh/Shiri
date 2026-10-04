"""Reject unsafe lab admission using private files and simulated OS identities.

No VM, namespace, package, device, user or system service is created or changed.
Actual held-file checks run on real private files with explicitly simulated
root metadata. VM/service facts are test inputs, never evidence of a real run.
"""

import ast
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
from uuid import uuid4

import pytest

from shiri.runtime.system import RuntimeFailure, atomic_json

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "tested_native_lab_admission", ROOT / "tests/linux/native_lab.py"
)
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def profile_data():
    uuid = str(uuid4())
    marker = json.dumps(
        {"vm_uuid": uuid, "hostname": "shiri-rehearsal-" + uuid[:8], "mac": "02:01:02:03:04:05"}
    ).encode()
    source = {name: digest((ROOT / name).read_bytes()) for name in lab.REQUIRED_SOURCE}
    source.update(
        {
            str(path.relative_to(ROOT)): digest(path.read_bytes())
            for path in (ROOT / "shiri").rglob("*")
            if path.is_file() and not path.name.endswith(".pyc") and "__pycache__" not in path.parts
        }
    )
    identities = b'{"simulated": "actual identity bytes"}'
    data = {
        "version": 1,
        "scope": "isolated-clean-vm",
        "vm": {
            "uuid": uuid,
            "hostname": "shiri-rehearsal-" + uuid[:8],
            "mac": "02:01:02:03:04:05",
            "machine_id": "a" * 32,
            "boot_id": str(uuid4()),
            "marker_sha256": digest(marker),
            "original_netns": {"st_dev": 4, "st_ino": 5},
        },
        "installation": {
            "id": str(uuid4()),
            "state_dir": "/var/lib/shiri-runtime",
            "run_dir": "/run/shiri",
            "identities": "/etc/shiri/daemon-identities.json",
            "identities_sha256": digest(identities),
        },
        "source": {"root": "/opt/shiri-rehearsal-lab/source", "files": source},
        "installed": {
            "root": "/opt/shiri/venv/lib/python3.10/site-packages",
            "files": {name: value for name, value in source.items() if name.startswith("shiri/")},
        },
        "binaries": {
            "root": "/opt/shiri",
            "files": {name: digest(name.encode()) for name in lab.REQUIRED_BINARIES},
        },
        "work_dir": "/var/lib/shiri-rehearsal-native-group-review",
    }
    return data, marker, identities


@pytest.mark.parametrize(
    "field",
    [
        "scope",
        "version",
        "unknown",
        "vm_unknown",
        "uuid",
        "boot",
        "host",
        "mac",
        "machine",
        "ns_bool",
        "ns_zero",
        "installation",
        "state",
        "run",
        "identities",
        "source_root",
        "installed_root",
        "binaries_root",
        "source_missing",
        "source_extra",
        "source_hash",
        "installed_bytes",
        "binary_missing",
        "work",
    ],
)
def test_profile_schema_cannot_silently_expand_or_substitute_admitted_scope(field):
    data, _, _ = profile_data()
    if field == "scope":
        data["scope"] = "any-linux-host"
    elif field == "version":
        data["version"] = True
    elif field == "unknown":
        data["allow_legacy_skip"] = True
    elif field == "vm_unknown":
        data["vm"]["skip_boot"] = True
    elif field == "uuid":
        data["vm"]["uuid"] = data["vm"]["uuid"].upper()
    elif field == "boot":
        data["vm"]["boot_id"] = "unknown"
    elif field == "host":
        data["vm"]["hostname"] = "production"
    elif field == "mac":
        data["vm"]["mac"] = "00:01:02:03:04:05"
    elif field == "machine":
        data["vm"]["machine_id"] = ""
    elif field == "ns_bool":
        data["vm"]["original_netns"]["st_dev"] = True
    elif field == "ns_zero":
        data["vm"]["original_netns"]["st_ino"] = 0
    elif field == "installation":
        data["installation"]["id"] = "b265"
    elif field == "state":
        data["installation"]["state_dir"] = "/var/lib/fake-runtime"
    elif field == "run":
        data["installation"]["run_dir"] = "/run/fake-runtime"
    elif field == "identities":
        data["installation"]["identities"] = "/etc/fake-identities.json"
    elif field == "source_root":
        data["source"]["root"] = "/tmp/untrusted/source"
    elif field == "installed_root":
        data["installed"]["root"] = "/opt/shiri/venv/lib/../foreign/site-packages"
    elif field == "binaries_root":
        data["binaries"]["root"] = "/usr/local"
    elif field == "source_missing":
        del data["source"]["files"]["tests/linux/native_lab.py"]
    elif field == "source_extra":
        data["source"]["files"]["../outside.py"] = "0" * 64
    elif field == "source_hash":
        data["source"]["files"]["tests/linux/native_lab.py"] = "not a hash"
    elif field == "installed_bytes":
        data["installed"]["files"]["shiri/runtime/broker.py"] = "0" * 64
    elif field == "binary_missing":
        del data["binaries"]["files"]["libexec/shiri-pcm-exec"]
    elif field == "work":
        data["work_dir"] = "/var/lib/shiri"
    with pytest.raises(RuntimeFailure):
        lab.schema(data)


def test_exact_profile_accepts_only_canonical_paths_and_complete_actual_package_mapping():
    data, _, _ = profile_data()
    lab.schema(data)
    for value in (
        "relative",
        "/",
        "/etc//profile",
        "/etc/./profile",
        "/etc/../profile",
        "/etc/profile/",
        "/etc/x\n",
    ):
        with pytest.raises(RuntimeFailure):
            lab.canonical(value)
    with pytest.raises(RuntimeFailure, match="duplicate"):
        lab.json_object(b'{"version":1,"version":2}')


def root_files(monkeypatch):
    """Keep real descriptors and metadata; simulate only Linux root ownership."""
    actual = os

    def root_stat(descriptor):
        info = actual.fstat(descriptor)
        return SimpleNamespace(
            **{
                name: getattr(info, name)
                for name in (
                    "st_dev",
                    "st_ino",
                    "st_size",
                    "st_mtime_ns",
                    "st_ctime_ns",
                    "st_mode",
                    "st_nlink",
                )
            },
            st_uid=0,
        )

    proxy = SimpleNamespace(
        open=actual.open,
        read=actual.read,
        close=actual.close,
        fstat=root_stat,
        O_RDONLY=actual.O_RDONLY,
        O_CLOEXEC=actual.O_CLOEXEC,
        O_NOFOLLOW=actual.O_NOFOLLOW,
    )
    monkeypatch.setattr(lab, "os", proxy)
    monkeypatch.setattr(lab, "trusted_file", lambda path: path)
    original_signature = lab.signature

    def signature(info):
        copied = SimpleNamespace(
            **{
                name: getattr(info, name)
                for name in (
                    "st_dev",
                    "st_ino",
                    "st_size",
                    "st_mtime_ns",
                    "st_ctime_ns",
                    "st_mode",
                    "st_nlink",
                )
            },
            st_uid=0,
        )
        return original_signature(copied)

    monkeypatch.setattr(lab, "signature", signature)
    return proxy


@pytest.mark.parametrize(
    "fault", ["healthy", "mode", "private_readable", "hardlink", "symlink", "oversize", "replace", "mutate"]
)
def test_real_held_file_inspection_rejects_unsafe_or_changed_profile_bytes(tmp_path, monkeypatch, fault):
    proxy = root_files(monkeypatch)
    path = tmp_path / "profile"
    path.write_bytes(b"admitted actual file bytes")
    path.chmod(0o600)
    if fault == "mode":
        path.chmod(0o622)
    elif fault == "private_readable":
        path.chmod(0o644)
    elif fault == "hardlink":
        os.link(path, tmp_path / "alias")
    elif fault == "symlink":
        path.rename(tmp_path / "original")
        path.symlink_to(tmp_path / "original")
    elif fault == "oversize":
        path.write_bytes(b"x" * 129)
    elif fault in {"replace", "mutate"}:
        original_read = proxy.read
        changed = False

        def read(descriptor, count):
            nonlocal changed
            value = original_read(descriptor, count)
            if not changed:
                changed = True
                if fault == "replace":
                    path.unlink()
                path.write_bytes(b"foreign replacement bytes")
                path.chmod(0o600)
            return value

        proxy.read = read
    if fault == "healthy":
        contents, _ = lab.read_file(path, private=True, maximum=128)
        assert contents == b"admitted actual file bytes"
    else:
        with pytest.raises((RuntimeFailure, OSError)):
            lab.read_file(path, private=True, maximum=128)


def admitted(monkeypatch):
    data, marker, identities = profile_data()
    profile = Path("/etc/shiri-rehearsal/native-lab.json")
    raw = json.dumps(data, sort_keys=True).encode()
    files = {profile: raw, lab.MARKER: marker, Path(data["installation"]["identities"]): identities}
    for tree in ("source", "installed", "binaries"):
        for name in data[tree]["files"]:
            files[Path(data[tree]["root"]) / name] = (
                (ROOT / name).read_bytes() if tree != "binaries" else name.encode()
            )
    state = {
        "data": data,
        "files": files,
        "profile": profile,
        "identity": ("held-profile",),
        "services": 0,
        "boot": {name: data["vm"][name] for name in ("hostname", "machine_id", "boot_id", "uuid")},
        "namespace": deepcopy(data["vm"]["original_netns"]),
        "mac_checks": [],
    }

    def read(path, **_kwargs):
        target = Path(path)
        return files[target], state["identity"] if target == profile else ("actual-held-file",)

    def inventory(_root):
        names = set(data["source"]["files"])
        directories = {str(parent) for name in names for parent in Path(name).parents if parent != Path(".")}
        return names | state.get("extra_files", set()), directories | state.get("extra_dirs", set())

    def services():
        state["services"] += 1
        if state.get("live_services"):
            raise RuntimeFailure("actual installed service is live")

    actual_identities = SimpleNamespace(
        installation_id=data["installation"]["id"],
        runtime_state_dir=Path(data["installation"]["state_dir"]),
        runtime_dir=Path(data["installation"]["run_dir"]),
    )
    monkeypatch.setattr(
        lab,
        "sys",
        SimpleNamespace(platform="linux", executable="/opt/shiri/venv/bin/python", dont_write_bytecode=True),
    )
    monkeypatch.setattr(lab, "os", SimpleNamespace(geteuid=lambda: 0))
    monkeypatch.setattr(lab, "read_file", read)
    monkeypatch.setattr(lab, "source_inventory", inventory)
    monkeypatch.setattr(lab, "boot_identity", lambda: state["boot"])
    monkeypatch.setattr(lab, "namespace_identity", lambda _fd=None: state["namespace"])
    monkeypatch.setattr(lab, "marker_mac_present", lambda mac: state["mac_checks"].append(mac))
    monkeypatch.setattr(lab, "installed_services_stopped", services)
    monkeypatch.setattr(
        lab, "DaemonIdentities", lambda _path: SimpleNamespace(load=lambda: actual_identities)
    )
    state["identities"] = actual_identities
    instance = lab.NativeLab(profile)
    manifest = {"version": 1, "installation_id": data["installation"]["id"], "networks": {}, "processes": {}}
    return instance, manifest, state


@pytest.mark.parametrize("mode", sorted(lab.MODES))
def test_fresh_admission_retains_real_uuid_and_installed_path_receipt_and_rechecks_cleanup(monkeypatch, mode):
    instance, manifest, state = admitted(monkeypatch)
    first = instance.admit(manifest, mode=mode, project=instance.project)
    assert first["installation_id"] == manifest["installation_id"]
    assert first["legacy_process_verification"] == "not_applicable_clean_vm"
    assert not any("pid" in key for key in first)
    assert state["mac_checks"] == [state["data"]["vm"]["mac"]]
    second = instance.admit(manifest, mode=mode, original_netns_fd=17, project=instance.project)
    assert second == first and state["services"] == 2
    assert (
        len(state["mac_checks"]) == 1
    )  # Inner namespace uses the inherited original FD, not a fake visible NIC.
    state["files"][state["profile"]] += b" "
    with pytest.raises(RuntimeFailure, match="profile changed"):
        instance.admit(manifest, mode=mode, original_netns_fd=17)


@pytest.mark.parametrize(
    "fault",
    [
        "profile_inode",
        "marker",
        "hostname",
        "boot",
        "machine",
        "vm",
        "namespace",
        "source",
        "installed",
        "binary",
        "identity_bytes",
        "identity_uuid",
        "identity_state",
        "identity_run",
        "source_extra",
        "source_empty_dir",
        "services",
        "python",
        "project",
        "ownership_uuid",
        "ownership_live",
        "ownership_network",
        "ownership_extra",
        "ownership_version",
        "standalone",
    ],
)
def test_changed_external_fact_or_owned_resource_cannot_be_admitted(monkeypatch, fault):
    instance, manifest, state = admitted(monkeypatch)
    if fault == "profile_inode":
        state["identity"] = ("foreign-profile",)
    elif fault == "marker":
        state["files"][lab.MARKER] += b" "
    elif fault in {"hostname", "boot", "machine", "vm"}:
        key = {"hostname": "hostname", "boot": "boot_id", "machine": "machine_id", "vm": "uuid"}[fault]
        state["boot"][key] = "foreign"
    elif fault == "namespace":
        state["namespace"]["st_ino"] += 1
    elif fault in {"source", "installed", "binary"}:
        tree = "binaries" if fault == "binary" else fault
        key = next(iter(state["data"][tree]["files"]))
        state["files"][Path(state["data"][tree]["root"]) / key] += b"foreign"
    elif fault == "identity_bytes":
        state["files"][instance.identities] += b" "
    elif fault == "identity_uuid":
        state["identities"].installation_id = str(uuid4())
    elif fault == "identity_state":
        state["identities"].runtime_state_dir = Path("/var/lib/another")
    elif fault == "identity_run":
        state["identities"].runtime_dir = Path("/run/another")
    elif fault == "source_extra":
        state["extra_files"] = {"__pycache__/native_lab.pyc"}
    elif fault == "source_empty_dir":
        state["extra_dirs"] = {"unadmitted_import_directory"}
    elif fault == "services":
        state["live_services"] = True
    elif fault == "python":
        lab.sys.executable = "/usr/bin/python3"
    elif fault == "ownership_uuid":
        manifest["installation_id"] = str(uuid4())
    elif fault == "ownership_live":
        manifest["processes"]["room:output"] = {"pid": 123}
    elif fault == "ownership_network":
        manifest["networks"]["sender"] = {}
    elif fault == "ownership_extra":
        manifest["allow_mutation"] = True
    elif fault == "ownership_version":
        manifest["version"] = True
    with pytest.raises(RuntimeFailure):
        instance.admit(
            manifest,
            mode="airplay_api_tts" if fault == "standalone" else "grouping",
            project=Path("/another/tree") if fault == "project" else instance.project,
        )


@pytest.mark.parametrize(
    "fault",
    [
        "healthy",
        "rc",
        "stderr",
        "oversize",
        "missing",
        "active",
        "activating",
        "failed",
        "pid",
        "control",
        "unloaded",
        "malformed",
    ],
)
def test_actual_service_query_requires_both_published_units_to_be_dead(monkeypatch, fault):
    block = "LoadState=loaded\nActiveState=inactive\nSubState=dead\nMainPID=0\nControlPID=0\n"
    answer = SimpleNamespace(returncode=0, stdout=block + "\n" + block, stderr="")
    if fault == "rc":
        answer.returncode = 1
    elif fault == "stderr":
        answer.stderr = "permission denied"
    elif fault == "oversize":
        answer.stdout = "x" * 16385
    elif fault == "missing":
        answer.stdout = block
    elif fault in {"active", "activating", "failed"}:
        answer.stdout = answer.stdout.replace("ActiveState=inactive", "ActiveState=" + fault, 1)
    elif fault == "pid":
        answer.stdout = answer.stdout.replace("MainPID=0", "MainPID=34", 1)
    elif fault == "control":
        answer.stdout = answer.stdout.replace("ControlPID=0", "ControlPID=35", 1)
    elif fault == "unloaded":
        answer.stdout = answer.stdout.replace("LoadState=loaded", "LoadState=not-found", 1)
    elif fault == "malformed":
        answer.stdout = "garbage\n\n" + block
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return answer

    monkeypatch.setattr(lab.subprocess, "run", run)
    if fault == "healthy":
        lab.installed_services_stopped()
    else:
        with pytest.raises(RuntimeFailure):
            lab.installed_services_stopped()
    assert len(calls) == 1 and calls[0][0][:4] == [
        "/usr/bin/systemctl",
        "show",
        "shiri-api.service",
        "shiri-runtime.service",
    ]
    assert calls[0][1]["timeout"] == 10 and not any(
        action in calls[0][0] for action in ("stop", "start", "restart")
    )


def test_service_probe_targets_the_exact_units_published_by_the_installer(monkeypatch):
    installer = (ROOT / "deploy/install_services.sh").read_text()
    loop = re.search(r"^for service in ([a-z -]+); do$", installer, flags=re.MULTILINE)
    assert loop is not None
    published = {name + ".service" for name in loop.group(1).split()}
    assert published == {"shiri-api.service", "shiri-runtime.service"}
    assert all((ROOT / "deploy" / name).is_file() for name in published)
    block = "LoadState=loaded\nActiveState=inactive\nSubState=dead\nMainPID=0\nControlPID=0\n"
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=block + "\n" + block, stderr="")

    monkeypatch.setattr(lab.subprocess, "run", run)
    lab.installed_services_stopped()
    assert len(calls) == 1 and set(calls[0][2:4]) == published


def test_no_profile_keeps_nonroot_import_free_of_privileged_admission(monkeypatch):
    monkeypatch.delenv(lab.ENVIRONMENT, raising=False)
    monkeypatch.setattr(
        lab, "read_file", lambda *_a, **_k: pytest.fail("default import read a root-only profile")
    )
    assert lab.from_environment() is None
    monkeypatch.setattr(lab, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(lab, "os", SimpleNamespace(geteuid=lambda: 1000, environ=os.environ))
    monkeypatch.setenv(lab.ENVIRONMENT, "/etc/shiri-rehearsal/native-lab.json")
    with pytest.raises(RuntimeFailure, match="Linux root"):
        lab.from_environment()


def test_producer_environment_does_not_inherit_root_only_lab_profile():
    from shiri.runtime.broker import Broker
    from shiri.settings import Settings

    environment = Broker(Settings())._environment()
    assert not any(item.startswith(lab.ENVIRONMENT + "=") for item in environment)


def test_hardware_snapshot_requires_explicit_lab_and_delegates_exact_profile():
    tree = ast.parse((ROOT / "tests/linux/check_native_grouping.py").read_text())
    snapshot = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "legacy_snapshot")
    namespace = {"require": lab.require, "NATIVE_LAB": None}
    exec(compile(ast.Module(body=[snapshot], type_ignores=[]), "<hardware snapshot>", "exec"), namespace)
    with pytest.raises(RuntimeFailure, match="explicit native lab profile"):
        namespace["legacy_snapshot"]()
    receipt = {"exact_profile": "retained"}
    namespace["NATIVE_LAB"] = SimpleNamespace(protected_snapshot=lambda: receipt)
    assert namespace["legacy_snapshot"]() is receipt


def test_manual_supervisors_share_explicit_profile_admission():
    for name in (
        "run_native_grouping", "run_native_zone_faults", "run_native_speech_stress",
        "run_native_latency_probe", "run_native_bluetooth_route",
    ):
        text = (ROOT / f"tests/linux/{name}.py").read_text()
        assert "startswith('b265')" not in text
        assert "native_lab_admission" in text
    for name in ("run_native_latency_probe", "run_native_bluetooth_route"):
        text = (ROOT / f"tests/linux/{name}.py").read_text()
        assert "'tests/linux/native_lab.py'" in text and "'tests/linux/native_lab_audio.py'" in text


@pytest.mark.parametrize("in_use", [False, True])
def test_clean_vm_protected_snapshot_requires_real_closed_endpoints_and_never_invents_pid(
    monkeypatch, in_use
):
    instance, _, state = admitted(monkeypatch)
    original_path = Path

    class ObservedPath:
        def __init__(self, value):
            self.path = original_path(value)

        def read_text(self):
            if str(self.path) == "/proc/sys/kernel/random/boot_id":
                return state["data"]["vm"]["boot_id"]
            assert str(self.path).startswith("/proc/asound/Loopback/")
            return "RUNNING\nowner_pid: 44" if in_use and "pcm0p/sub0" in str(self.path) else "closed\n"

    monkeypatch.setattr(lab, "Path", ObservedPath)
    if in_use:
        with pytest.raises(RuntimeFailure, match="in use"):
            instance.protected_snapshot()
    else:
        receipt = instance.protected_snapshot()
        assert len(receipt["protected_slot0_2_state"]) == 8
        assert set(receipt["protected_slot0_2_state"].values()) == {"closed"}
        assert receipt["legacy_process_verification"] == "not_applicable_clean_vm"
        assert "pid" not in receipt and "birth" not in receipt


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["healthy", "wrong_inner", "changed_after_child", "changed_after_delete"])
async def test_real_fresh_supervisor_rechecks_admission_before_and_after_owned_namespace_cleanup(
    tmp_path, monkeypatch, fault
):
    instance, manifest, external = admitted(monkeypatch)
    state, work, nodes = (tmp_path / name for name in ("state", "work", "netns"))
    for directory in (state, work, nodes):
        directory.mkdir()
    (state / "ownership.json").write_text(json.dumps(manifest))
    original = tmp_path / "original-netns"
    original.write_text("Private descriptor lifetime fixture; no actual namespace")
    inner, outer = tmp_path / "inner.json", tmp_path / "outer.json"
    expected, calls = instance.receipt(), []
    source = ROOT / "tests/linux/run_native_grouping.py"
    tree = ast.parse(source.read_text())
    body = [
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name in {"run", "stop_child"}
    ]
    assert len(body) == 2

    def paths(value):
        return {"/run/netns": nodes, "/proc/self/ns/net": original}.get(str(value), Path(value))

    class Runner:
        async def run(self, command, **_kwargs):
            calls.append(command)
            if command[:3] == ["ip", "netns", "add"]:
                (nodes / command[3]).write_text("Owned private namespace inode fixture")
            if command[:3] == ["ip", "netns", "delete"]:
                (nodes / command[3]).unlink()
                if fault == "changed_after_delete":
                    external["files"][external["profile"]] += b" "
            return SimpleNamespace(stdout="")

        async def json(self, _command):
            return [{"ifname": "lo"}]

    class Child:
        returncode = None

        async def wait(self):
            self.returncode = 0
            if fault == "changed_after_child":
                external["files"][external["profile"]] += b" "
            return 0

    async def launch(*_arguments, **_options):
        atomic_json(
            inner,
            {
                "started_at": datetime.now(timezone.utc).isoformat(),
                "passed": True,
                "native_lab": {"foreign": True} if fault == "wrong_inner" else expected,
            },
        )
        return Child()

    async def snapshot():
        return "actual unchanged host fixture"

    def admission(current, mode):
        return instance.admit(current, mode=mode, project=instance.project)

    def directory(path, mode=0o700):
        path.mkdir(parents=True, exist_ok=True, mode=mode)

    namespace = {
        "asyncio": SimpleNamespace(
            wait_for=asyncio.wait_for,
            TimeoutError=asyncio.TimeoutError,
            create_subprocess_exec=launch,
            subprocess=asyncio.subprocess,
        ),
        "datetime": datetime,
        "timezone": timezone,
        "json": json,
        "Path": paths,
        "uuid4": uuid4,
        "os": SimpleNamespace(
            geteuid=lambda: 0,
            open=lambda path, *args: os.open(paths(path), *args),
            fstat=os.fstat,
            close=os.close,
            environ={},
            O_RDONLY=os.O_RDONLY,
            O_NOFOLLOW=os.O_NOFOLLOW,
            O_CLOEXEC=os.O_CLOEXEC,
        ),
        "sys": SimpleNamespace(platform="linux", executable="/opt/shiri/venv/bin/python"),
        "Runner": Runner,
        "RuntimeFailure": RuntimeFailure,
        "atomic_json": atomic_json,
        "boot_id": lambda: external["data"]["vm"]["boot_id"],
        "root_directory": directory,
        "RESULT": outer,
        "HERE": source,
        "group": SimpleNamespace(
            NATIVE_LAB=instance,
            native_lab_admission=admission,
            WORK=work,
            STATE=state,
            RESULT=inner,
            PROJECT=instance.project,
            isolated_lan=SimpleNamespace(
                namespace_identity=lambda fd: (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
            ),
            observation=SimpleNamespace(
                base=SimpleNamespace(closed_slot=lambda: None, host_snapshot=snapshot)
            ),
            legacy_snapshot=lambda: "actual closed protected endpoints fixture",
        ),
    }
    exec(
        compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), str(source), "exec"),
        namespace,
    )
    outcome = await namespace["run"]()
    result = json.loads(outer.read_text())
    if fault == "healthy":
        assert outcome == 0 and result["passed"] is True and result["cleanup"]["native_lab_preserved"] is True
        assert external["services"] == 3 and result["native_lab"] == expected
    else:
        assert outcome == 1 and result["passed"] is False
        if fault == "wrong_inner":
            assert "exact clean lab admission" in result["failure"]["message"]
        else:
            assert any("profile changed" in message for message in result["cleanup_errors"])
    deletions = [call for call in calls if call[:3] == ["ip", "netns", "delete"]]
    assert bool(deletions) is (fault != "changed_after_child")
    assert bool(list(nodes.iterdir())) is (fault == "changed_after_child")
