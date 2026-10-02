"""Fixed daemon account admission is checked before installation mutations."""

import importlib.util
import fcntl
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

INSTALLATION = "ebdb6996-f3a2-4e38-8d5c-9fbdfc9849cb"


@pytest.fixture
def provisioner(monkeypatch):
    source = Path(__file__).resolve().parents[1] / "deploy/provision_daemons.py"
    spec = importlib.util.spec_from_file_location("daemon_provisioning", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    accounts = {"shiri": SimpleNamespace(pw_uid=150, pw_gid=150)}
    groups = {}

    def lookup(collection, name):
        if name not in collection:
            raise KeyError(name)
        return collection[name]

    for number, (role, name) in enumerate(module.expected_accounts().items(), 200):
        accounts[name] = SimpleNamespace(pw_name=name, pw_uid=number, pw_gid=number,
            pw_shell="/usr/sbin/nologin", pw_gecos=f"{module.PURPOSE} {role} {INSTALLATION}",
            pw_dir=f"/nonexistent/shiri/{name}")
        groups[name] = SimpleNamespace(gr_name=name, gr_gid=number, gr_mem=[])
    monkeypatch.setattr(module.pwd, "getpwnam", lambda name: lookup(accounts, name))
    monkeypatch.setattr(module.grp, "getgrnam", lambda name: lookup(groups, name))
    monkeypatch.setattr(module.grp, "getgrall", lambda: list(groups.values()))
    monkeypatch.setattr(module, "trusted_parent", lambda path: None)
    monkeypatch.setattr(module, "load_mapping", lambda path: None)
    chown = Mock()
    monkeypatch.setattr(module.os, "fchown", chown)
    run = Mock()
    monkeypatch.setattr(module.subprocess, "run", run)
    return SimpleNamespace(module=module, accounts=accounts, groups=groups, run=run, chown=chown)


def test_existing_accounts_publish_one_root_only_exact_identity_map(provisioner, tmp_path):
    fixture = provisioner
    path = tmp_path / "daemon-identities.json"
    result = fixture.module.provision(path, INSTALLATION, tmp_path / "runtime")
    assert json.loads(path.read_text()) == result
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.stat().st_nlink == 1
    assert len(result["accounts"]) == len({item["uid"] for item in result["accounts"].values()}) == 50
    assert result["version"] == 2
    assert result["accounts"]["slot7.bridge"]["uid"] != result["accounts"]["slot7.output"]["uid"]
    fixture.chown.assert_called_once()
    assert fixture.chown.call_args.args[1:] == (0, 0)
    fixture.run.assert_not_called()


@pytest.mark.parametrize("drift", ["foreign_purpose", "duplicate_uid", "api_uid", "orphan_group", "changed_gid"])
def test_late_account_conflict_never_creates_earlier_missing_role(provisioner, tmp_path, drift):
    fixture = provisioner
    names = list(fixture.module.expected_accounts().values())
    # The earliest role is absent. A later conflict must be caught before the
    # installer creates any users/groups or publishes a partial map.
    del fixture.accounts[names[0]], fixture.groups[names[0]]
    victim = fixture.accounts[names[-1]]
    if drift == "foreign_purpose":
        victim.pw_gecos = "Someone else's daemon"
    elif drift == "duplicate_uid":
        victim.pw_uid = fixture.accounts[names[-2]].pw_uid
    elif drift == "api_uid":
        victim.pw_uid = fixture.accounts["shiri"].pw_uid
    elif drift == "orphan_group":
        del fixture.accounts[names[-1]]
    else:
        fixture.groups[names[-1]].gr_gid += 2000
    path = tmp_path / "daemon-identities.json"
    with pytest.raises(RuntimeError):
        fixture.module.provision(path, INSTALLATION, tmp_path / "runtime")
    fixture.run.assert_not_called()
    fixture.chown.assert_not_called()
    assert not path.exists()


@pytest.mark.parametrize("change", ["installation_id", "runtime_state_dir", "account_uid"])
def test_published_mapping_mismatch_is_preserved_before_account_mutations(provisioner, tmp_path, monkeypatch, change):
    fixture = provisioner
    path = tmp_path / "daemon-identities.json"
    result = fixture.module.provision(path, INSTALLATION, tmp_path / "runtime")
    before = path.read_bytes()
    if change == "account_uid":
        result["accounts"]["slot7.audio"]["uid"] += 500
    else:
        result[change] = "fbdb6996-f3a2-4e38-8d5c-9fbdfc9849cb" if change == "installation_id" else "/var/lib/other-installation"
    monkeypatch.setattr(fixture.module, "load_mapping", lambda path: result)
    fixture.run.reset_mock()
    fixture.chown.reset_mock()
    with pytest.raises(RuntimeError):
        fixture.module.provision(path, INSTALLATION, tmp_path / "runtime")
    fixture.run.assert_not_called()
    fixture.chown.assert_not_called()
    assert path.read_bytes() == before


def test_concurrent_map_publication_never_overwrites_other_installation(provisioner, tmp_path, monkeypatch):
    fixture = provisioner
    path = tmp_path / "daemon-identities.json"
    link = os.link

    def racing_link(source, destination, **kwargs):
        path.write_bytes(b"Other installation's mapping")
        return link(source, destination, **kwargs)

    monkeypatch.setattr(fixture.module.os, "link", racing_link)
    with pytest.raises(FileExistsError):
        fixture.module.provision(path, INSTALLATION, tmp_path / "runtime")
    assert path.read_bytes() == b"Other installation's mapping"
    assert not list(tmp_path.glob(".daemon-identities.*"))


@pytest.fixture
def legacy_map(provisioner, tmp_path, monkeypatch):
    module = provisioner.module
    state, runtime = tmp_path / "state", tmp_path / "run"
    state.mkdir()
    runtime.mkdir()
    names = module.expected_accounts(1)
    accounts = {role: {"name": name, "uid": provisioner.accounts[name].pw_uid,
                       "gid": provisioner.groups[name].gr_gid} for role, name in names.items()}
    mapping = {"version": 1, "installation_id": INSTALLATION, "runtime_state_dir": str(state), "accounts": accounts}
    path = tmp_path / "daemon-identities.json"
    path.write_text(json.dumps(mapping))
    path.chmod(0o600)
    manifest = state / "ownership.json"
    manifest.write_text(json.dumps({"version": 1, "installation_id": INSTALLATION, "networks": {}, "processes": {}}))
    manifest.chmod(0o600)
    monkeypatch.setattr(module, "load_mapping", lambda _: mapping)
    # Portable test files belong to the test user. Preserve real held inodes,
    # link counts, locks and modes while mocking the root-only owner check.
    real_fstat = os.fstat

    def root_owner(fd):
        actual = real_fstat(fd)
        return SimpleNamespace(st_mode=actual.st_mode, st_nlink=actual.st_nlink,
                               st_uid=0, st_size=actual.st_size, st_dev=actual.st_dev, st_ino=actual.st_ino)

    monkeypatch.setattr(module.os, "fstat", root_owner)
    return SimpleNamespace(fixture=provisioner, module=module, state=state, runtime=runtime,
                           path=path, mapping=mapping, manifest=manifest)


def test_quiescent_v1_migration_preserves_every_published_uid_and_adds_distinct_bridges(legacy_map, monkeypatch):
    env = legacy_map
    scan = Mock()
    monkeypatch.setattr(env.module, "managed_processes_absent", scan)
    result = env.module.provision(env.path, INSTALLATION, env.state, env.runtime)
    assert result["version"] == 2 and result["runtime_dir"] == str(env.runtime)
    assert all(result["accounts"][role] == value for role, value in env.mapping["accounts"].items())
    assert len({account["uid"] for account in result["accounts"].values()}) == 50
    assert json.loads(env.path.read_text()) == result
    assert scan.call_count == 3  # Before validation, account creation and atomic publication.
    env.fixture.run.assert_not_called()


@pytest.mark.parametrize("resource", ["networks", "processes"])
def test_migration_with_owned_resources_preserves_mapping_and_accounts(legacy_map, resource):
    env = legacy_map
    data = json.loads(env.manifest.read_text())
    data[resource] = {"still-owned": {}}
    env.manifest.write_text(json.dumps(data))
    before = env.path.read_bytes()
    with pytest.raises(RuntimeError, match="resources remain"):
        env.module.provision(env.path, INSTALLATION, env.state, env.runtime)
    assert env.path.read_bytes() == before
    env.fixture.run.assert_not_called()
    env.fixture.chown.assert_not_called()


def test_active_broker_lock_refuses_migration_before_process_scan_or_account_mutation(legacy_map, monkeypatch):
    env = legacy_map
    fd = os.open(env.runtime / "broker.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    scan = Mock()
    monkeypatch.setattr(env.module, "managed_processes_absent", scan)
    before = env.path.read_bytes()
    try:
        with pytest.raises(RuntimeError, match="broker is active"):
            env.module.provision(env.path, INSTALLATION, env.state, env.runtime)
        assert env.path.read_bytes() == before
        scan.assert_not_called()
        env.fixture.run.assert_not_called()
    finally:
        os.close(fd)


@pytest.mark.parametrize("failure_phase", [1, 2, 3])
def test_any_live_process_recheck_preserves_old_mapping_before_publication(legacy_map, monkeypatch, failure_phase):
    env = legacy_map
    before = env.path.read_bytes()
    effects = [None] * (failure_phase - 1) + [RuntimeError("Managed daemon processes are still live")]
    monkeypatch.setattr(env.module, "managed_processes_absent", Mock(side_effect=effects))
    with pytest.raises(RuntimeError, match="still live"):
        env.module.provision(env.path, INSTALLATION, env.state, env.runtime)
    assert env.path.read_bytes() == before
    env.fixture.run.assert_not_called()


def test_replaced_mapping_inode_is_preserved_during_migration(legacy_map, monkeypatch):
    env = legacy_map
    replacement = env.path.with_name("new-map")
    original_inode = env.path.stat().st_ino
    replacement_bytes = env.path.read_bytes()
    replacement.write_bytes(replacement_bytes)
    replacement.chmod(0o600)
    replacement_inode = replacement.stat().st_ino

    def replace_during_scan(_uids):
        os.replace(replacement, env.path)

    monkeypatch.setattr(env.module, "managed_processes_absent", replace_during_scan)
    with pytest.raises(RuntimeError, match="inode was replaced"):
        env.module.provision(env.path, INSTALLATION, env.state, env.runtime)
    assert env.path.stat().st_ino == replacement_inode != original_inode
    assert env.path.read_bytes() == replacement_bytes
    env.fixture.run.assert_not_called()


@pytest.mark.parametrize("field", [0, 1, 2, 3])
def test_uid_scan_detects_real_effective_saved_and_filesystem_worker_identity(provisioner, tmp_path, field):
    directory = tmp_path / "1234"
    directory.mkdir()
    ids = [500, 500, 500, 500]
    ids[field] = 201
    (directory / "status").write_text("Name:\tworker\nUid:\t" + "\t".join(map(str, ids)) + "\n")
    with pytest.raises(RuntimeError, match="still live"):
        provisioner.module.managed_processes_absent({201}, proc=tmp_path)


@pytest.mark.parametrize("status", ["Name:\tworker\n", "Uid:\t201\n", "Uid:\t1\tbad\t2\t3\n"])
def test_unreadable_uid_admission_cannot_prove_migration_quiescent(provisioner, tmp_path, status):
    directory = tmp_path / "1234"
    directory.mkdir()
    (directory / "status").write_text(status)
    with pytest.raises(RuntimeError, match="Cannot prove"):
        provisioner.module.managed_processes_absent({201}, proc=tmp_path)


def test_missing_owned_manifest_cannot_upgrade_the_uid_map(legacy_map, monkeypatch):
    env = legacy_map
    before = env.path.read_bytes()
    env.manifest.unlink()
    scan = Mock()
    monkeypatch.setattr(env.module, "managed_processes_absent", scan)
    with pytest.raises(FileNotFoundError):
        env.module.provision(env.path, INSTALLATION, env.state, env.runtime)
    assert env.path.read_bytes() == before
    scan.assert_not_called()
    env.fixture.run.assert_not_called()


def test_published_missing_old_account_refuses_migration_before_new_identity_creation(legacy_map, monkeypatch):
    env = legacy_map
    monkeypatch.setattr(env.module, "managed_processes_absent", Mock())
    for slot in range(8):
        del env.fixture.accounts[f"shiri-bridge-{slot}"], env.fixture.groups[f"shiri-bridge-{slot}"]
    del env.fixture.accounts["shiri-output-7"]
    before = env.path.read_bytes()
    with pytest.raises(RuntimeError, match="disappeared"):
        env.module.provision(env.path, INSTALLATION, env.state, env.runtime)
    assert env.path.read_bytes() == before
    env.fixture.run.assert_not_called()


def test_new_bridge_creation_keeps_all_old_uid_assignments(legacy_map, monkeypatch):
    env = legacy_map
    monkeypatch.setattr(env.module, "managed_processes_absent", Mock())
    for slot in range(8):
        del env.fixture.accounts[f"shiri-bridge-{slot}"], env.fixture.groups[f"shiri-bridge-{slot}"]

    def create_account(args, **kwargs):
        name = args[-1]
        slot = int(name.removeprefix("shiri-bridge-"))
        assert kwargs == {"check": True}
        if args[0] == "/usr/sbin/groupadd":
            env.fixture.groups[name] = SimpleNamespace(gr_name=name, gr_gid=500 + slot, gr_mem=[])
        else:
            assert args[0] == "/usr/sbin/useradd"
            env.fixture.accounts[name] = SimpleNamespace(
                pw_name=name, pw_uid=500 + slot, pw_gid=500 + slot, pw_shell="/usr/sbin/nologin",
                pw_gecos=f"{env.module.PURPOSE} slot{slot}.bridge {INSTALLATION}",
                pw_dir=f"/nonexistent/shiri/{name}",
            )

    env.fixture.run.side_effect = create_account
    result = env.module.provision(env.path, INSTALLATION, env.state, env.runtime)
    assert env.fixture.run.call_count == 16
    assert all(result["accounts"][role] == previous for role, previous in env.mapping["accounts"].items())
    assert result["accounts"]["slot7.bridge"]["uid"] == 507
    assert json.loads(env.path.read_text()) == result
