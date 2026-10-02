"""A daemon may execute only after durable exact-cgroup kernel admission."""

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from shiri.runtime import bind_policy, launch_gate, units
from shiri.runtime.network import NetworkManager
from shiri.runtime.system import CommandResult, RuntimeFailure
from test_daemon_privileges import BOOT, actual


def output_spec():
    return units.UnitSpec(units.new_unit("b265eb7d", "sender", "owntone"), "owntone",
                          "shiri-output-7", "shiri-output-7", ("/opt/shiri/sbin/owntone", "-v"),
                          namespace="/run/netns/shiri_ot_b265eb7d", listen_port=3939)


def proof(descriptor, port=3939):
    info = os.fstat(descriptor)
    return {"version": 1, "port": port, "cgroup_dev": info.st_dev, "cgroup_inode": info.st_ino,
            "boot_id": BOOT, "inet4": {"id": 51, "tag": "11" * 8},
            "inet6": {"id": 52, "tag": "22" * 8},
            "instructions_verified": True, "effective_verified": True}


@pytest.mark.parametrize("field,bad", [("cgroup_inode", 1), ("cgroup_dev", 0), ("port", 3869),
                                       ("boot_id", "other"), ("effective_verified", False),
                                       ("instructions_verified", 1)])
def test_kernel_proof_must_match_the_held_descriptor(tmp_path, field, bad):
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(RuntimeFailure, match="different launch"):
            bind_policy.proof(proof(descriptor) | {field: bad}, descriptor=descriptor, boot=BOOT, port=3939)
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("bad", [{"id": True, "tag": "11" * 8}, {"id": 0, "tag": "11" * 8},
                                  {"id": 0x100000000, "tag": "11" * 8},
                                  {"id": 51, "tag": "11"}, {"id": 51, "tag": "11" * 8, "extra": 1}])
def test_kernel_program_identity_is_strict_and_bounded(tmp_path, bad):
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(RuntimeFailure, match="program identity"):
            bind_policy.proof(proof(descriptor) | {"inet4": bad}, descriptor=descriptor, boot=BOOT, port=3939)
    finally:
        os.close(descriptor)


@pytest.mark.asyncio
async def test_helper_inherits_exact_held_fd_and_sanitized_environment(tmp_path, monkeypatch):
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.setattr(bind_policy, "trusted_file", lambda path, **_: path)
    expected = proof(descriptor)
    process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(json.dumps(expected).encode(), b"")))
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(bind_policy.asyncio, "create_subprocess_exec", spawn)
    try:
        policy = bind_policy.BindPolicy(Path("/opt/shiri/libexec/shiri-bind-policy"))
        assert await policy.run("verify", descriptor, BOOT, 3939, expected) == expected
        assert spawn.call_args.kwargs["pass_fds"] == (descriptor,)
        assert spawn.call_args.kwargs["env"] == {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}
        assert spawn.call_args.args[-4:] == ("51", "11" * 8, "52", "22" * 8)
        process.communicate.return_value = (json.dumps(expected | {"inet4": {"id": 53, "tag": "33" * 8}}).encode(), b"")
        with pytest.raises(RuntimeFailure, match="identity changed"):
            await policy.run("verify", descriptor, BOOT, 3939, expected)
    finally:
        os.close(descriptor)


@pytest.fixture
def gate_manager(tmp_path, monkeypatch):
    monkeypatch.setattr(units, "boot_id", lambda: BOOT)
    monkeypatch.setattr(units, "process_birth", lambda pid: "111")
    monkeypatch.setattr(units, "trusted_file", lambda path, **_: path)
    monkeypatch.setattr(units, "root_directory", lambda path: path.mkdir(parents=True, exist_ok=True))
    original_stat = os.fstat

    def root_info(fd):
        info = original_stat(fd)
        return SimpleNamespace(st_uid=0, st_mode=info.st_mode, st_nlink=info.st_nlink, st_size=info.st_size,
                               st_dev=info.st_dev, st_ino=info.st_ino)

    monkeypatch.setattr(units.os, "fstat", root_info)
    group = tmp_path / "kernel"
    group.mkdir()
    events, saved, services = [], {}, []
    manifest = SimpleNamespace(state_dir=tmp_path)
    manifest.reserve_unit = Mock(side_effect=lambda key, entry: saved.update({key: dict(entry)}))
    def remember(key, entry):
        events.append("durable-policy" if "bind_policy" in entry else "durable-invocation")
        saved[key] = dict(entry)
    manifest.remember_unit = Mock(side_effect=remember)
    manifest.forget_unit = Mock(side_effect=lambda key: saved.pop(key, None))
    runner = SimpleNamespace(run=AsyncMock(), start=AsyncMock())
    manager = units.UnitManager(runner, manifest, bind_policy_helper=Path("/opt/shiri/libexec/shiri-bind-policy"))
    manager.pcm_exec_helper = Path("/opt/shiri/libexec/shiri-pcm-exec")
    prepare = manager.prepare_gate
    def prepared(service):
        service = prepare(service)
        services.append(service)
        return service
    manager.prepare_gate = prepared
    launched = False
    async def inspect(name):
        if not launched:
            return None
        return actual(services[0]) | {"SocketBindAllow": [], "SocketBindDeny": []}
    manager.inspect = AsyncMock(side_effect=inspect)
    async def run(args, **kwargs):
        nonlocal launched
        launched = True
        events.append("wrapper-started")
        assert "-I" in args and str(units.VIEW / "gate/ready.json") in args
        assert not (Path(services[0].gate.directory) / "ready.json").exists()
    runner.run.side_effect = run
    manager.open_cgroup = Mock(side_effect=lambda entry: os.open(group, os.O_RDONLY | os.O_DIRECTORY))
    async def policy(action, descriptor, boot, port, expected=None):
        events.append(action)
        assert not (Path(services[0].gate.directory) / "ready.json").exists()
        return proof(descriptor, port)
    manager.bind_policy.run = AsyncMock(side_effect=policy)
    publish = manager.publish_gate
    def released(entry, descriptor):
        assert saved["sender:owntone"]["bind_policy"] == entry["bind_policy"]
        events.append("released")
        publish(entry, descriptor)
    manager.publish_gate = released
    # The first post-release monitor need not query again within 10 seconds.
    return SimpleNamespace(manager=manager, events=events, saved=saved, services=services, group=group)


@pytest.mark.asyncio
async def test_payload_is_gated_until_kernel_proof_is_durable(gate_manager, tmp_path):
    fixture = gate_manager
    unit = await fixture.manager.start("sender:owntone", output_spec(), tmp_path / "log")
    try:
        assert fixture.events[:6] == ["wrapper-started", "durable-invocation", "attach", "verify",
                                      "durable-policy", "released"]
        entry = fixture.saved["sender:owntone"]
        assert entry["policy_version"] == 3
        assert entry["gate"]["payload"] == ["/opt/shiri/sbin/owntone", "-v"]
        assert "SocketBindAllow" not in entry["properties"] and "SocketBindDeny" not in entry["properties"]
        release = json.loads((Path(entry["gate"]["directory"]) / "ready.json").read_text())
        assert release["nonce"] == entry["gate"]["nonce"]
        assert release["invocation_id"] == entry["invocation_id"]
        assert release["cgroup_inode"] == entry["bind_policy"]["cgroup_inode"]
        fixture.manager.verify(entry, actual(fixture.services[0]) | {"SocketBindAllow": [], "SocketBindDeny": []})
    finally:
        unit.monitor.cancel()
        await asyncio.gather(unit.monitor, return_exceptions=True)


@pytest.mark.asyncio
async def test_control_admission_requires_immutable_pcm_exec_inside_the_gate(gate_manager, tmp_path):
    fixture = gate_manager
    payload = output_spec().command
    pcm = units.PCMExec("/opt/shiri/libexec/shiri-pcm-exec", payload)
    service = replace(output_spec(), devices=("/dev/snd/pcmC1D1p", "/dev/snd/controlC1"),
                      command=pcm.command(), pcm_exec=pcm)
    unit = await fixture.manager.start("sender:owntone", service, tmp_path / "log")
    try:
        entry = fixture.saved["sender:owntone"]
        assert entry["policy_version"] == 4
        assert entry["pcm_exec"] == {"helper": pcm.helper, "payload": list(payload)}
        assert entry["gate"]["payload"] == list(pcm.command())
        assert entry["argv"] == list(fixture.services[0].gate.command(service.name))
        assert "SocketBindDeny" not in entry["properties"]
        units.UnitManager.validate_saved(entry)
        with pytest.raises(RuntimeFailure, match="immutable output payload"):
            changed = dict(entry, pcm_exec={"helper": pcm.helper, "payload": ["/usr/bin/other"]})
            units.UnitManager.validate_saved(changed)
    finally:
        unit.monitor.cancel()
        await asyncio.gather(unit.monitor, return_exceptions=True)


def test_unfiltered_control_device_and_substitute_launcher_are_refused():
    with pytest.raises(RuntimeFailure, match="inherited ioctl"):
        replace(output_spec(), devices=("/dev/snd/controlC1",))
    pcm = units.PCMExec("/opt/shiri/libexec/shiri-pcm-exec", ("/opt/shiri/sbin/owntone",))
    with pytest.raises(RuntimeFailure, match="immutable output payload"):
        replace(output_spec(), devices=("/dev/snd/controlC1",), pcm_exec=pcm)
    for devices in [("/dev/snd/pcmC1D1p", "/dev/snd/controlC2"),
                    ("/dev/snd/pcmC1D1p",), ("/dev/snd/pcmC1D1c", "/dev/snd/controlC1")]:
        with pytest.raises(RuntimeFailure, match="matching card control"):
            replace(output_spec(), command=pcm.command(), devices=devices, pcm_exec=pcm)


def test_bridge_filtered_payload_and_known_old_policies_can_recover(monkeypatch):
    monkeypatch.setattr(units, "boot_id", lambda: BOOT)
    pcm = units.PCMExec("/opt/shiri/libexec/shiri-pcm-exec", ("/usr/bin/python3.10", "-m", "shiri.runtime.local_output"))
    service = units.UnitSpec(units.new_unit("b265eb7d", "sender", "local-output"), "local-output",
                            "shiri-output-7", "shiri-output-7", pcm.command(),
                            devices=("/dev/snd/pcmC1D0c", "/dev/snd/controlC1"), pcm_exec=pcm)
    entry = service.intent()
    units.UnitManager.validate_saved(entry)
    assert entry["policy_version"] == 4 and "gate" not in entry and "bind_policy" not in entry
    for role in ["shairport", "owntone"]:
        old = output_spec() if role == "owntone" else units.UnitSpec(
            units.new_unit("b265eb7d", "sender", role), role, "shiri-receiver-7", "shiri-receiver-7",
            ("/opt/shiri/sbin/shairport-sync",),
        )
        units.UnitManager.validate_saved(old.intent())
        legacy = old.intent() | {"policy_version": 1}
        legacy["properties"]["SystemCallFilter"] = units.SYSCALL_POLICY_V1
        units.UnitManager.validate_saved(legacy)


@pytest.mark.parametrize("failure", ["attach", "verify", "persist", "replacement", "cancel"])
@pytest.mark.asyncio
async def test_admission_failure_never_releases_and_retains_if_rollback_fails(gate_manager, tmp_path, failure):
    fixture = gate_manager
    original_policy = fixture.manager.bind_policy.run.side_effect
    async def failing(action, *args):
        if action == failure:
            raise RuntimeFailure("Policy unavailable")
        if failure == "cancel" and action == "attach":
            raise asyncio.CancelledError()
        return await original_policy(action, *args)
    fixture.manager.bind_policy.run.side_effect = failing
    if failure == "persist":
        original_remember = fixture.manager.manifest.remember_unit.side_effect
        def remembering(key, entry):
            if "bind_policy" in entry:
                raise RuntimeFailure("Storage unavailable")
            original_remember(key, entry)
        fixture.manager.manifest.remember_unit.side_effect = remembering
    elif failure == "replacement":
        original_inspect = fixture.manager.inspect.side_effect
        async def replacing(name):
            result = await original_inspect(name)
            if result and "attach" in fixture.events:
                result["InvocationID"] = list(bytes.fromhex("33" * 16))
            return result
        fixture.manager.inspect.side_effect = replacing
    fixture.manager.stop_saved = AsyncMock(side_effect=RuntimeFailure("Cannot prove cleanup"))
    with pytest.raises(RuntimeFailure, match="Cannot prove cleanup"):
        await fixture.manager.start("sender:owntone", output_spec(), tmp_path / "log")
    assert "sender:owntone" in fixture.saved
    assert "released" not in fixture.events
    assert not (Path(fixture.services[0].gate.directory) / "ready.json").exists()
    fixture.manager.manifest.forget_unit.assert_not_called()


@pytest.mark.asyncio
async def test_missing_bind_controller_refuses_before_unit_intent(tmp_path):
    manager = units.UnitManager(Mock(), Mock())
    manager.inspect = AsyncMock(return_value=None)
    with pytest.raises(RuntimeFailure, match="verified kernel"):
        await manager.start("sender:owntone", output_spec(), tmp_path / "log")
    manager.manifest.reserve_unit.assert_not_called()


@pytest.mark.asyncio
async def test_manifest_reservation_failure_discards_only_its_empty_new_gate(gate_manager, tmp_path):
    fixture = gate_manager
    fixture.saved["sender:owntone"] = {"old": "reservation"}
    fixture.manager.manifest.reserve_unit.side_effect = RuntimeFailure("Manifest storage failed")
    with pytest.raises(RuntimeFailure, match="Manifest storage failed"):
        await fixture.manager.start("sender:owntone", output_spec(), tmp_path / "log")
    assert fixture.saved["sender:owntone"] == {"old": "reservation"}
    assert not await asyncio.to_thread(Path(fixture.services[0].gate.directory).exists)
    fixture.manager.runner.run.assert_not_awaited()
    fixture.manager.manifest.forget_unit.assert_not_called()


@pytest.mark.asyncio
async def test_manifest_failure_preserves_a_replacement_gate(gate_manager, tmp_path):
    fixture = gate_manager
    original = tmp_path / "moved-owned-gate"
    def failed(key, entry):
        directory = Path(entry["gate"]["directory"])
        directory.rename(original)
        directory.mkdir()
        (directory / "foreign").write_text("keep")
        raise RuntimeFailure("Manifest storage failed")
    fixture.manager.manifest.reserve_unit.side_effect = failed
    with pytest.raises(RuntimeFailure, match="changed; preserve"):
        await fixture.manager.start("sender:owntone", output_spec(), tmp_path / "log")
    assert original.is_dir()
    assert (Path(fixture.services[0].gate.directory) / "foreign").read_text() == "keep"
    fixture.manager.runner.run.assert_not_awaited()
    fixture.manager.manifest.forget_unit.assert_not_called()


def test_release_rejects_wrong_generation_boot_group_and_writable_file(tmp_path, monkeypatch):
    path = tmp_path / "ready.json"
    value = {"unit": output_spec().name, "nonce": "44" * 16, "boot_id": BOOT, "invocation_id": "55" * 16,
             "cgroup_dev": 1, "cgroup_inode": 72}
    stat_info = os.fstat
    monkeypatch.setattr(launch_gate.os, "fstat", lambda fd: SimpleNamespace(**{
        "st_uid": 0, "st_mode": stat_info(fd).st_mode, "st_nlink": stat_info(fd).st_nlink,
        "st_size": stat_info(fd).st_size,
    }))
    path.write_text(json.dumps(value))
    path.chmod(0o644)
    assert launch_gate.release(path, unit=value["unit"], nonce=value["nonce"], boot=BOOT,
                               invocation=value["invocation_id"], cgroup=(1, 72))
    for field, invalid in [("nonce", "66" * 16), ("boot_id", "other"), ("cgroup_inode", 73),
                           ("invocation_id", "0" * 32), ("invocation_id", "66" * 16), ("cgroup_dev", True)]:
        path.write_text(json.dumps(value | {field: invalid}))
        with pytest.raises(ValueError, match="different daemon"):
            launch_gate.release(path, unit=value["unit"], nonce=value["nonce"], boot=BOOT,
                                invocation=value["invocation_id"], cgroup=(1, 72))
    path.write_text(json.dumps(value))
    path.chmod(0o666)
    with pytest.raises(ValueError, match="immutable root"):
        launch_gate.release(path, unit=value["unit"], nonce=value["nonce"], boot=BOOT,
                            invocation=value["invocation_id"], cgroup=(1, 72))


@pytest.mark.parametrize("text", ["3869 60999", "1024 3939", "65535 1024", "1 1023", "32768", "32768 bad"])
@pytest.mark.asyncio
async def test_sender_implicit_bind_cannot_allocate_another_room_port(tmp_path, text):
    network = NetworkManager.__new__(NetworkManager)
    network.owned_namespace = AsyncMock(return_value=True)
    network.runner = SimpleNamespace(run=AsyncMock(return_value=CommandResult([], 0, text)))
    with pytest.raises(RuntimeFailure, match="automatic port range"):
        await network.validate_sender_ephemeral_range({"role": "sender", "namespace": "shiri_ot_test"})


@pytest.mark.asyncio
async def test_sender_safe_range_is_observed_inside_exact_namespace(tmp_path):
    network = NetworkManager.__new__(NetworkManager)
    network.owned_namespace = AsyncMock(return_value=True)
    network.runner = SimpleNamespace(run=AsyncMock(return_value=CommandResult([], 0, "32768\t60999\n")))
    assert await network.validate_sender_ephemeral_range({"role": "sender", "namespace": "shiri_ot_test"}) == (32768, 60999)
    assert network.runner.run.call_args.args[0][:4] == ["ip", "netns", "exec", "shiri_ot_test"]
    network.owned_namespace.return_value = False
    with pytest.raises(RuntimeFailure, match="unowned"):
        await network.validate_sender_ephemeral_range({"role": "sender", "namespace": "shiri_ot_test"})
