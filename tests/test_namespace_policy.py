"""Real file lifecycle and unchanged manager security fences; no VM/units."""

import asyncio
from copy import deepcopy
import hashlib
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from shiri.runtime import namespace_policy as policy, units
from shiri.runtime.system import RuntimeFailure
from test_daemon_privileges import BOOT, actual, spec


@pytest.fixture(autouse=True)
def boot(monkeypatch):
    monkeypatch.setattr(units, "boot_id", lambda: BOOT)


def sealed():
    service = spec()
    record = policy.create(policy.plan(service.name, BOOT))
    return service, record, policy.ROOT / (service.name + ".d") / policy.NAME


def test_exact_real_artifact_creation_validation_and_retirement():
    service, record, path = sealed()
    assert record["phase"] == "sealed"
    assert path.read_bytes() == policy.BODY
    assert hashlib.sha256(path.read_bytes()).hexdigest() == record["sha256"]
    assert [path.stat().st_dev, path.stat().st_ino] == record["file"]
    assert path.stat().st_mode & 0o777 == 0o644
    assert path.parent.stat().st_mode & 0o777 == 0o755
    policy.verify(record)
    owner = units.UnitManager(SimpleNamespace(), Mock())
    assert owner.verify(service.intent() | {"namespace_policy": record}, actual(service))[0] == "11" * 16
    policy.discard(record, current_boot=BOOT)
    assert not path.parent.exists()


@pytest.mark.parametrize(
    "fault", ["bytes", "same-bytes-new-file", "new-directory", "mode", "symlink", "hardlink", "extra-file"]
)
def test_changed_owned_artifact_refuses_verification_and_cleanup(fault, tmp_path):
    service, record, path = sealed()
    if fault == "bytes":
        path.write_bytes(b"[Service]\nRestrictNamespaces=no\n")
    elif fault == "same-bytes-new-file":
        replacement = path.parent / "replacement"
        replacement.write_bytes(policy.BODY)
        replacement.chmod(0o644)
        replacement.replace(path)
    elif fault == "new-directory":
        path.parent.rename(path.parent.with_name("retained-original"))
        path.parent.mkdir(mode=0o755)
        path.write_bytes(policy.BODY)
        path.chmod(0o644)
    elif fault == "mode":
        path.chmod(0o666)
    elif fault == "symlink":
        path.unlink()
        target = tmp_path / "target"
        target.write_bytes(policy.BODY)
        path.symlink_to(target)
    elif fault == "hardlink":
        os.link(path, tmp_path / "retained-link")
    else:
        (path.parent / "foreign.conf").write_text("[Service]\n")
    for action in [lambda: policy.verify(record), lambda: policy.discard(record, current_boot=BOOT)]:
        with pytest.raises((RuntimeFailure, OSError)):
            action()
    assert path.parent.exists()
    owner = units.UnitManager(SimpleNamespace(), Mock())
    with pytest.raises((RuntimeFailure, OSError)):
        owner.verify(service.intent() | {"namespace_policy": record}, actual(service))


@pytest.mark.parametrize("value", [2**64 - 1, 0x7E020000, "yes", "no", None, True, False])
def test_persisted_text_never_permits_relaxed_or_untyped_actual_namespace_mask(value):
    service, record, _ = sealed()
    owner = units.UnitManager(SimpleNamespace(), Mock())
    with pytest.raises(RuntimeFailure, match="RestrictNamespaces"):
        owner.verify(
            service.intent() | {"namespace_policy": record}, actual(service) | {"RestrictNamespaces": value}
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("unit", "unowned.service"),
        ("boot_id", "other-boot"),
        ("sha256", "0" * 64),
        ("file", [True, 1]),
        ("phase", "unverified"),
    ],
)
def test_malformed_durable_receipt_never_gains_admission(field, value):
    service, record, _ = sealed()
    with pytest.raises(RuntimeFailure):
        units.UnitManager(SimpleNamespace(), Mock()).validate_saved(
            service.intent() | {"namespace_policy": record | {field: value}}
        )


def test_unsealed_crash_reservation_recovers_exact_expected_artifact():
    service = spec()
    planned = policy.plan(service.name, BOOT)
    policy.create(planned)  # Model crash before the sealed receipt reached disk.
    policy.discard(planned, current_boot=BOOT)
    assert not (policy.ROOT / (service.name + ".d")).exists()


def test_planned_intent_without_files_is_safe_to_retire():
    service = spec()
    planned = policy.plan(service.name, BOOT)
    policy.discard(planned, current_boot=BOOT)
    assert list(policy.ROOT.iterdir()) == []


def test_prior_boot_receipt_cannot_retarget_current_boot_even_with_same_bytes():
    _, record, path = sealed()
    with pytest.raises(RuntimeFailure, match="another boot"):
        policy.discard(record, current_boot="a-different-boot")
    assert path.read_bytes() == policy.BODY


def test_prior_boot_volatile_absence_needs_no_current_inode_authority():
    _, record, path = sealed()
    policy.discard(record, current_boot=BOOT)
    policy.discard(record, current_boot="a-different-boot")
    assert not path.parent.exists()


def test_existing_directory_never_replaced_even_when_policy_bytes_agree():
    service, _, path = sealed()
    with pytest.raises(RuntimeFailure, match="already exists"):
        policy.plan(service.name, BOOT)
    assert path.read_bytes() == policy.BODY


def test_launch_error_does_not_delete_another_policy_directory():
    service = spec()
    planned = policy.plan(service.name, BOOT)
    foreign = policy.ROOT / (service.name + ".d")
    foreign.mkdir(mode=0o755)
    marker = foreign / "operator.conf"
    marker.write_text("operator data")
    with pytest.raises(RuntimeFailure):
        policy.create(planned)
    assert marker.read_text() == "operator data"
    with pytest.raises(RuntimeFailure, match="Unexpected"):
        policy.discard(planned, current_boot=BOOT)


@pytest.mark.asyncio
async def test_real_launch_seam_reserves_then_pins_launch_and_persists_before_return(tmp_path, monkeypatch):
    service = spec()
    events, ledger = [], {}

    def reserve(key, entry):
        events.append("reserved")
        assert entry["namespace_policy"]["phase"] == "planned"
        assert not (policy.ROOT / (service.name + ".d")).exists()
        ledger[key] = deepcopy(entry)

    def remember(key, entry):
        events.append("persisted")
        policy.verify(entry["namespace_policy"])
        assert entry["invocation_id"] == "11" * 16 and entry["cgroup_inode"] > 0
        ledger[key] = deepcopy(entry)

    async def run(argv, **kwargs):
        events.append("start")
        assert "--property=RestrictNamespaces=yes" in argv
        assert ledger["sender:shairport"]["namespace_policy"]["phase"] == "planned"
        assert not (policy.ROOT / (service.name + ".d")).exists()

    manifest = SimpleNamespace(
        reserve_unit=reserve, remember_unit=remember, forget_unit=lambda key: ledger.pop(key)
    )
    logger = SimpleNamespace(stop=AsyncMock())
    runner = SimpleNamespace(run=AsyncMock(side_effect=run), start=AsyncMock(return_value=logger))
    owner = units.UnitManager(runner, manifest)
    snapshots = [None]
    owner.inspect = AsyncMock(side_effect=lambda _: snapshots[0])
    original_run = runner.run.side_effect

    async def launch(argv, **kwargs):
        await original_run(argv, **kwargs)
        snapshots[0] = actual(service)

    runner.run.side_effect = launch
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "cgroup.events").write_text("populated 0\nfrozen 0\n")
    owner.open_cgroup = lambda _: os.open(cgroup, os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.setattr(units, "trusted_file", lambda path, **_: path)
    monkeypatch.setattr(units, "process_birth", lambda _: "test-owned-parent")
    unit = await owner.start("sender:shairport", service, tmp_path / "log")
    assert events[:6] == ["reserved", "start", "persisted", "persisted", "persisted", "persisted"]
    assert ledger["sender:shairport"]["namespace_policy"]["phase"] == "sealed"
    await asyncio.sleep(0)
    await unit.stop()
    assert not (policy.ROOT / (service.name + ".d")).exists()
    logger.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_actual_policy_never_signals_or_discards_artifact(monkeypatch):
    service, record, path = sealed()
    entry = service.intent() | {"namespace_policy": record}
    owner = units.UnitManager(SimpleNamespace(), Mock())
    owner.inspect = AsyncMock(return_value=actual(service) | {"RestrictNamespaces": 2**64 - 1})
    signal = Mock(side_effect=AssertionError("Unexpected process authority"))
    monkeypatch.setattr(units.signal, "pidfd_send_signal", signal, raising=False)
    owner.open_cgroup = Mock(side_effect=AssertionError("Unexpected cgroup authority"))
    with pytest.raises(RuntimeFailure, match="RestrictNamespaces"):
        await owner.stop_saved(entry)
    signal.assert_not_called()
    owner.open_cgroup.assert_not_called()
    assert path.read_bytes() == policy.BODY


@pytest.mark.parametrize(
    "stage",
    [
        "directory-before-save",
        "directory-after-save",
        "file-before-save",
        "file-after-save",
        "sealed-before-save",
    ],
)
@pytest.mark.parametrize("failure", [RuntimeFailure, asyncio.CancelledError])
def test_crash_at_each_publication_phase_recovers_only_its_reserved_artifact(stage, failure):
    service = spec()
    durable = policy.plan(service.name, BOOT)
    last = durable

    def persist(record):
        nonlocal last
        last = record
        if stage == record["phase"] + "-before-save":
            raise failure("injected crash")
        durable.clear()
        durable.update(deepcopy(record))
        if stage == record["phase"] + "-after-save":
            raise failure("injected crash")

    if stage == "sealed-before-save":
        policy.create(durable, persist=persist)
    else:
        with pytest.raises(failure, match="injected"):
            policy.create(durable, persist=persist)
    policy.discard(durable, current_boot=BOOT)
    assert not (policy.ROOT / (service.name + ".d")).exists()


def test_partial_planned_write_is_recoverable_but_altered_bytes_remain_reserved():
    service = spec()
    durable = policy.plan(service.name, BOOT)
    path = policy.ROOT / (service.name + ".d") / policy.NAME
    path.parent.mkdir(mode=0o755)
    path.write_bytes(policy.BODY[:13])
    path.chmod(0o644)
    policy.discard(durable, current_boot=BOOT)
    path.parent.mkdir(mode=0o755)
    path.write_bytes(b"[Service]\nUnexpected=yes\n")
    path.chmod(0o644)
    with pytest.raises(RuntimeFailure, match="bytes changed"):
        policy.discard(durable, current_boot=BOOT)
    assert path.exists()


def test_standard_runtime_root_is_created_only_by_a_launch_plan():
    service = spec()
    policy.ROOT.rmdir()
    planned = policy.plan(service.name, BOOT)
    assert policy.ROOT.stat().st_mode & 0o777 == 0o755
    policy.ROOT.rmdir()
    policy.discard(planned, current_boot="a-new-boot")
    assert not policy.ROOT.exists()


@pytest.mark.parametrize(
    "phase,artifact", [("directory", "directory"), ("file", "directory"), ("file", "file")]
)
def test_already_durable_creation_inode_cannot_be_retargeted_on_recovery(phase, artifact):
    service = spec()
    planned = policy.plan(service.name, BOOT)
    receipts = {}
    policy.create(planned, persist=lambda record: receipts.update({record["phase"]: deepcopy(record)}))
    record = receipts[phase]
    path = policy.ROOT / (service.name + ".d") / policy.NAME
    if artifact == "directory":
        path.parent.rename(path.parent.with_name("original-held-directory"))
        path.parent.mkdir(mode=0o755)
        path.write_bytes(policy.BODY)
        path.chmod(0o644)
    else:
        replacement = path.parent / "replacement"
        replacement.write_bytes(policy.BODY)
        replacement.chmod(0o644)
        replacement.replace(path)
    with pytest.raises(RuntimeFailure, match="artifact changed"):
        policy.discard(record, current_boot=BOOT)
    assert path.exists()
