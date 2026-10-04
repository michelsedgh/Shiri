"""Missing loopback provisioning never chooses another kernel or dependency."""

import hashlib
import importlib.util
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


RELEASE = "5.15.0-194-generic"
EXTRA = "linux-modules-extra-" + RELEASE
VERSION = "5.15.0-194.204"
REGISTRY = "wireless-regdb"


def load(name):
    source = Path(__file__).resolve().parents[1] / "install" / (name + ".py")
    spec = importlib.util.spec_from_file_location("kernel_test_" + name, source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def kernel(monkeypatch):
    module = load("kernel_modules")
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(module.os, "uname", lambda: SimpleNamespace(release=RELEASE))
    monkeypatch.setattr(module.sys, "platform", "linux")
    return module


@pytest.mark.parametrize("root,platform", [(False, "linux"), (True, "darwin")])
def test_nonroot_or_nonlinux_does_not_even_probe(kernel, monkeypatch, root, platform):
    monkeypatch.setattr(kernel.os, "geteuid", lambda: 0 if root else 1001)
    monkeypatch.setattr(kernel.sys, "platform", platform)
    probe = Mock()
    monkeypatch.setattr(kernel, "available", probe)
    with pytest.raises(RuntimeError, match="root on Linux"):
        kernel.ensure_loopback()
    probe.assert_not_called()


def test_available_driver_never_reads_distro_or_installs(kernel, monkeypatch):
    monkeypatch.setattr(kernel, "available", lambda: True)
    distro, installer = Mock(), Mock()
    monkeypatch.setattr(kernel, "ubuntu_id", distro)
    monkeypatch.setattr(kernel, "dependency_installer", installer)
    kernel.ensure_loopback()
    distro.assert_not_called()
    installer.assert_not_called()


@pytest.mark.parametrize("ubuntu,release", [(False, RELEASE), (True, "6.8.0-custom"),
                                           (True, "5.15.0-194-aws"), (True, "5.15.0-194-generic;evil")])
def test_missing_unsupported_driver_never_invokes_apt(kernel, monkeypatch, ubuntu, release):
    monkeypatch.setattr(kernel, "available", lambda: False)
    monkeypatch.setattr(kernel, "ubuntu_id", lambda: ubuntu)
    monkeypatch.setattr(kernel.os, "uname", lambda: SimpleNamespace(release=release))
    installer = Mock()
    monkeypatch.setattr(kernel, "dependency_installer", installer)
    with pytest.raises(RuntimeError, match="will not replace the kernel"):
        kernel.ensure_loopback()
    installer.assert_not_called()


@pytest.mark.parametrize("success", [True, False])
def test_exact_name_and_mandatory_post_probe(kernel, monkeypatch, success):
    probes = iter((False, success))
    monkeypatch.setattr(kernel, "available", lambda: next(probes))
    monkeypatch.setattr(kernel, "ubuntu_id", lambda: True)
    installer = Mock()
    monkeypatch.setattr(kernel, "dependency_installer", lambda: installer)
    if success:
        kernel.ensure_loopback()
    else:
        with pytest.raises(RuntimeError, match="still unavailable"):
            kernel.ensure_loopback()
    assert installer.install.call_args.args == ([EXTRA, REGISTRY],)
    assert callable(installer.install.call_args.kwargs["exact_candidate"])


def test_failed_install_never_claims_or_post_probes(kernel, monkeypatch):
    probe = Mock(return_value=False)
    monkeypatch.setattr(kernel, "available", probe)
    monkeypatch.setattr(kernel, "ubuntu_id", lambda: True)
    installer = SimpleNamespace(install=Mock(side_effect=subprocess.CalledProcessError(1, ["dpkg"])))
    monkeypatch.setattr(kernel, "dependency_installer", lambda: installer)
    with pytest.raises(subprocess.CalledProcessError):
        kernel.ensure_loopback()
    assert probe.call_count == 1


def test_probe_cannot_run_operator_install_replacement(kernel, monkeypatch):
    run = Mock(return_value=SimpleNamespace(returncode=0, stderr=""))
    monkeypatch.setattr(kernel.subprocess, "run", run)
    assert kernel.available()
    assert run.call_args.args[0] == ["/usr/sbin/modprobe", "--ignore-install", "--dry-run", "snd-aloop"]
    assert run.call_args.kwargs["env"] == kernel.ENVIRONMENT
    run.return_value.returncode = 1
    assert not kernel.available()
    run.return_value.returncode = 2
    with pytest.raises(RuntimeError, match="Cannot probe"):
        kernel.available()


@pytest.mark.parametrize("text,expected", [("ID=ubuntu\n", True), ('ID="ubuntu"\n', True),
                                          ("ID=debian\nID_LIKE=ubuntu\n", False), ("NAME=Ubuntu\n", False)])
def test_exact_distro_identity(kernel, tmp_path, text, expected):
    path = tmp_path / "os-release"
    path.write_text(text)
    assert kernel.ubuntu_id(path) is expected


@pytest.mark.parametrize("text", ["ID=ubuntu\nID=ubuntu\n", "ID='ubuntu\n", "ID=ubuntu evil\n", "x" * 4097])
def test_ambiguous_distro_refused(kernel, tmp_path, text):
    path = tmp_path / "os-release"
    path.write_text(text)
    with pytest.raises((RuntimeError, ValueError)):
        kernel.ubuntu_id(path)


def metadata(package=EXTRA):
    content = (package + "-archive").encode()
    return {"Package": package, "Version": VERSION if package == EXTRA else "2026.05.30-0ubuntu1~22.04.1",
            "Architecture": "arm64" if package == EXTRA else "all", "SHA256": hashlib.sha256(content).hexdigest(),
            "Size": str(len(content)), "Source": "linux" if package == EXTRA else "wireless-regdb",
            "Depends": "linux-image-" + RELEASE + " | linux-image-unsigned-" + RELEASE + ", wireless-regdb"}


def paragraph(item):
    return "".join(f"{key}: {value}\n" for key, value in item.items())


def candidate_commands(kernel, monkeypatch, *, extra=None, registry_state="missing", policy=None):
    extra = metadata() if extra is None else extra
    calls = []

    def command(arguments, *, environment):
        calls.append(arguments)
        if arguments == ["/usr/bin/dpkg", "--print-architecture"]:
            return "arm64\n"
        package = arguments[-1].split("=", 1)[0]
        item = extra if package == EXTRA else metadata(REGISTRY)
        if "policy" in arguments:
            return policy if policy is not None else f'{package}:\n  Candidate: {item["Version"]}\n'
        return paragraph(item)

    monkeypatch.setattr(kernel, "command", command)
    monkeypatch.setattr(kernel, "ubuntu_id", lambda: True)
    state = SimpleNamespace(returncode=1 if registry_state == "missing" else 0,
                            stdout="" if registry_state == "missing" else registry_state + "\n", stderr="")
    def run(arguments, **kwargs):
        if "${binary:Package}" in arguments[2]:
            return SimpleNamespace(returncode=1, stdout=f"linux-image-{RELEASE}\tinstalled\t{VERSION}\tarm64\n", stderr="")
        return state

    monkeypatch.setattr(kernel.subprocess, "run", Mock(side_effect=run))
    return calls


@pytest.mark.parametrize("state,names", [("missing", [REGISTRY, EXTRA]), ("installed", [EXTRA])])
def test_candidate_admits_only_native_extra_and_missing_registry(kernel, monkeypatch, state, names):
    calls = candidate_commands(kernel, monkeypatch, registry_state=state)
    result = kernel.candidate(EXTRA, {}, RELEASE)
    assert [item["Package"] for item in result] == names
    assert not any("linux-image" in part for call in calls for part in call)


@pytest.mark.parametrize("field,value", [("Package", "linux-generic"), ("Architecture", "amd64"),
                                         ("Version", "6.8.0-1.2"), ("Source", "unreviewed-kernel")])
def test_foreign_candidate_identity_refused(kernel, monkeypatch, field, value):
    item = metadata()
    item[field] = value
    candidate_commands(kernel, monkeypatch, extra=item)
    if field == "Version":
        # Candidate policy and selected control paragraph must be identical.
        monkeypatch.setattr(kernel, "command", Mock(side_effect=["arm64\n", f"  Candidate: {VERSION}\n", paragraph(item)]))
    with pytest.raises(RuntimeError):
        kernel.candidate(EXTRA, {}, RELEASE)


@pytest.mark.parametrize("policy", ["  Candidate: (none)\n", "Candidate: " + VERSION,
                                    "  Candidate: " + VERSION + "\n  Candidate: " + VERSION,
                                    "  Candidate: --evil\n"])
def test_no_or_ambiguous_candidate_refused(kernel, monkeypatch, policy):
    candidate_commands(kernel, monkeypatch, policy=policy)
    with pytest.raises(RuntimeError):
        kernel.candidate(EXTRA, {}, RELEASE)


@pytest.mark.parametrize("state", ["unpacked", "half-configured", "config-files"])
def test_unfinished_registry_not_replaced(kernel, monkeypatch, state):
    candidate_commands(kernel, monkeypatch, registry_state=state)
    with pytest.raises(RuntimeError, match="unfinished"):
        kernel.candidate(EXTRA, {}, RELEASE)


@pytest.mark.parametrize("image", ["", f"linux-image-{RELEASE}\tinstalled\tother\tarm64\n",
                                   f"linux-image-{RELEASE}\tunpacked\t{VERSION}\tarm64\n",
                                   f"linux-image-{RELEASE}\tinstalled\t{VERSION}\tamd64\n",
                                   "linux-image-other\tinstalled\tversion\tarm64\n"])
def test_candidate_requires_existing_matching_configured_kernel_image(kernel, monkeypatch, image):
    candidate_commands(kernel, monkeypatch)
    monkeypatch.setattr(kernel.subprocess, "run", Mock(return_value=SimpleNamespace(
        returncode=1, stdout=image, stderr="")))
    with pytest.raises(RuntimeError, match="kernel image"):
        kernel.candidate(EXTRA, {}, RELEASE)


def test_changed_release_refused_before_candidate_query(kernel, monkeypatch):
    candidate_commands(kernel, monkeypatch)
    query = Mock()
    monkeypatch.setattr(kernel, "command", query)
    monkeypatch.setattr(kernel.os, "uname", lambda: SimpleNamespace(release="other"))
    with pytest.raises(RuntimeError, match="changed during module admission"):
        kernel.candidate(EXTRA, {}, RELEASE)
    query.assert_not_called()


@pytest.mark.parametrize("text", ["Package: a\nPackage: b\n", "Package: a\n\nPackage: a\n",
                                  "Package a\n", " unbound continuation\n"])
def test_ambiguous_control_paragraph_not_admitted(kernel, text):
    with pytest.raises(RuntimeError):
        kernel.control_fields(text)


@pytest.fixture
def transaction(monkeypatch, tmp_path):
    module = load("apt_dependencies")
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    original_metadata = module.root_metadata
    monkeypatch.setattr(module, "root_metadata", lambda info, **kwargs: original_metadata(
        SimpleNamespace(st_mode=info.st_mode, st_uid=0, st_nlink=info.st_nlink), **kwargs))
    monkeypatch.setattr(module, "trusted_directory", lambda path: None)
    policy = tmp_path / "policy-rc.d"
    policy.write_bytes(b"#!/bin/sh\n# operator\nexit 0\n")
    policy.chmod(0o755)
    inode = policy.stat().st_ino
    policy_context, lock_context = module.deny_service_actions, module.installation_lock
    monkeypatch.setattr(module, "deny_service_actions", lambda: policy_context(policy))
    monkeypatch.setattr(module, "installation_lock", lambda: lock_context(tmp_path / "lock"))
    temporary = module.tempfile.TemporaryDirectory
    monkeypatch.setattr(module.tempfile, "TemporaryDirectory", lambda **kwargs: temporary(dir=tmp_path))
    candidates = [metadata(REGISTRY), metadata()]
    baseline = {"linux-image-" + RELEASE: ("ii ", VERSION, "arm64"), "systemd": ("ii ", "249", "arm64")}
    after = {**baseline, **{item["Package"]: ("ii ", item["Version"], item["Architecture"]) for item in candidates}}
    state = SimpleNamespace(module=module, candidates=candidates, baseline=baseline, after=after, calls=[],
                            fault=None, policy=policy, inode=inode, installed=False, snapshots=0)

    def run(arguments, **kwargs):
        state.calls.append(arguments)
        assert module.MARKER in policy.read_bytes()
        assert kwargs["env"]["NEEDRESTART_MODE"] == "l"
        output = ""
        if arguments == ["/usr/bin/dpkg", "--audit"]:
            output = "unfinished package" if state.fault == "audit" else ""
        elif arguments[0] == "/usr/bin/dpkg-query":
            state.snapshots += 1
            packages = state.after if state.installed else state.baseline
            if state.fault == "before-race" and state.snapshots > 1:
                packages = {**packages, "foreign": ("ii ", "1", "arm64")}
            output = "".join(name + "\t" + "\t".join(values) + "\n" for name, values in packages.items())
        elif "--simulate" in arguments:
            output = "".join(f'{action} {item["Package"]} ({item["Version"]} Ubuntu [arm64])\n'
                             for item in candidates for action in ("Inst", "Conf"))
            if state.fault == "plan":
                output += "Inst linux-image-6.8.0-1-generic (6.8.0-1.2 Ubuntu [arm64])\n"
        elif arguments[1] == "download":
            package = arguments[-1].split("=", 1)[0]
            archive = Path(kwargs["cwd"]) / (package + ".deb")
            archive.write_bytes((package + "-archive").encode())
            if state.fault == "hash":
                archive.write_bytes(b"x" * archive.stat().st_size)
            if state.fault == "size":
                archive.write_bytes(b"x")
            if state.fault == "files":
                archive.with_suffix(".other").write_bytes(b"unexpected")
            if state.fault == "link":
                held = archive.with_suffix(".held")
                archive.rename(held)
                archive.symlink_to(held)
        elif arguments[0] == "/usr/bin/dpkg-deb":
            package = Path(arguments[2]).stem
            item = next(item for item in candidates if item["Package"] == package)
            output = "".join(f"{key}: {item[key]}\n" for key in ("Package", "Version", "Architecture"))
            if state.fault == "identity":
                output = output.replace("arm64", "amd64")
        elif arguments[0] == "/usr/bin/dpkg" and "--install" in arguments:
            if state.fault == "dpkg":
                raise subprocess.CalledProcessError(1, arguments)
            state.installed = True
        if state.fault == "update" and arguments == ["/usr/bin/apt-get", "update"]:
            raise subprocess.CalledProcessError(1, arguments)
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(module.subprocess, "run", run)

    def admit(name, environment):
        assert name == EXTRA
        assert state.calls[-1] == ["/usr/bin/apt-get", "update"]
        assert module.MARKER in policy.read_bytes()
        if state.fault == "candidate":
            raise RuntimeError("No candidate")
        return candidates

    state.admit = admit
    yield state
    assert policy.read_bytes() == b"#!/bin/sh\n# operator\nexit 0\n"
    assert policy.stat().st_ino == inode
    assert policy.stat().st_nlink == 1
    assert not list(tmp_path.glob(".shiri-policy-*"))


def test_strict_transaction_installs_only_hashed_files_and_restores_policy(transaction):
    state = transaction
    state.module.install([EXTRA, REGISTRY], exact_candidate=state.admit)
    actual = next(call for call in state.calls if call[0] == "/usr/bin/dpkg" and "--install" in call)
    assert actual[-2].endswith(REGISTRY + ".deb") and actual[-1].endswith(EXTRA + ".deb")
    assert "--refuse-configure-any" in actual and "--refuse-depends" in actual
    assert "--no-triggers" not in actual
    assert not any(call[:2] == ["/usr/bin/apt-get", "install"] for call in state.calls)
    assert state.after["linux-image-" + RELEASE] == state.baseline["linux-image-" + RELEASE]


@pytest.mark.parametrize("fault", ["audit", "update", "candidate", "plan", "hash", "size", "files", "link", "identity", "before-race", "dpkg"])
def test_preflight_and_install_failures_preserve_policy_and_never_expand_request(transaction, fault):
    transaction.fault = fault
    with pytest.raises((RuntimeError, subprocess.CalledProcessError)):
        transaction.module.install([EXTRA, REGISTRY], exact_candidate=transaction.admit)
    assert not transaction.installed
    if fault != "dpkg":
        assert not any(call[0] == "/usr/bin/dpkg" and "--install" in call for call in transaction.calls)


@pytest.mark.parametrize("field,value", [("SHA256", ""), ("Size", "0"), ("Size", "536870913"),
                                         ("Version", "--evil"), ("Architecture", "../arm64"),
                                         ("Package", "linux-generic")])
def test_missing_or_unsafe_archive_identity_never_downloads(transaction, field, value):
    transaction.candidates[-1][field] = value
    with pytest.raises(RuntimeError):
        transaction.module.install([EXTRA, REGISTRY], exact_candidate=transaction.admit)
    assert not any("download" in call for call in transaction.calls)
    assert not transaction.installed


def test_existing_extra_package_state_is_preserved_without_reinstall(transaction):
    transaction.baseline[EXTRA] = ("ii ", VERSION, "arm64")
    with pytest.raises(RuntimeError, match="already has saved state"):
        transaction.module.install([EXTRA, REGISTRY], exact_candidate=transaction.admit)
    assert not any("--simulate" in call or "download" in call or "--install" in call for call in transaction.calls)


@pytest.mark.parametrize("mutation", ["image-version", "foreign-add", "pending-trigger", "target-version"])
def test_postcheck_rejects_unrelated_or_unfinished_changes(transaction, mutation):
    if mutation == "image-version":
        transaction.after["linux-image-" + RELEASE] = ("ii ", "other", "arm64")
    elif mutation == "foreign-add":
        transaction.after["foreign"] = ("ii ", "1", "arm64")
    elif mutation == "pending-trigger":
        transaction.after["systemd"] = ("it ", "249", "arm64")
    else:
        transaction.after[EXTRA] = ("ii ", "other", "arm64")
    with pytest.raises(RuntimeError, match="preserve other package identities"):
        transaction.module.install([EXTRA, REGISTRY], exact_candidate=transaction.admit)
    assert transaction.installed


@pytest.mark.parametrize("extra", ["Inst linux-generic (6.8 Ubuntu [arm64])\n", "Remv systemd [249]\n",
                                  "Conf systemd (249 Ubuntu [arm64])\n", "Purg systemd [249]\n",
                                  "Inst " + EXTRA + " [old] (" + VERSION + " Ubuntu [arm64])\n"])
def test_plan_rejects_image_meta_upgrade_removal_pending_configuration(extra):
    module = load("apt_dependencies")
    item = metadata()
    valid = f"Inst {EXTRA} ({VERSION} Ubuntu [arm64])\nConf {EXTRA} ({VERSION} Ubuntu [arm64])\n"
    with pytest.raises(RuntimeError):
        module.exact_package_plan(valid + extra, [item])


def test_installer_limits_module_provisioning_to_explicit_loopback_option():
    source = (Path(__file__).resolve().parents[1] / "install/install.sh").read_text()
    invocation = '/usr/bin/python3 -I "$SOURCE/install/kernel_modules.py"'
    assert source.count(invocation) == 1
    guard = 'if [[ "${SHIRI_INSTALL_LOOPBACK:-0}" == 1 ]]; then'
    start = source.index(guard)
    end = source.index("\nfi", start)
    assert start < source.index(invocation) < end
    assert start < source.index("modprobe snd-aloop") < end
    assert source.index(invocation) < source.index('bash "$SOURCE/install/build_helpers.sh"')
    assert source.index(invocation) < source.index(" -I -m venv")
