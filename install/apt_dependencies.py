#!/usr/bin/python3
"""Install named dependencies without package-triggered host service restarts.

Use the system interpreter in isolated mode. A root-owned install lock serializes
this temporary policy with other Shiri dependency installations. A replaced
policy is retained for operator recovery rather than overwriting another actor.
"""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile


MARKER = b"# Shiri temporary dependency policy; recovery backup: "


def package_states(environment):
    result = subprocess.run(
        ["/usr/bin/dpkg-query", "--show", "--showformat=${binary:Package}\t${db:Status-Abbrev}\t${Version}\t${Architecture}\n"],
        env=environment, check=True, capture_output=True, text=True,
    )
    states = {}
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 4 or fields[0] in states or not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9-]+)?", fields[0]):
            raise RuntimeError("Unrecognized installed-package identity snapshot")
        states[fields[0]] = tuple(fields[1:])
    if not states:
        raise RuntimeError("Empty installed-package identity snapshot")
    return states


def exact_package_plan(text, candidates):
    """No image, missing dependency, upgrade, removal or pending configure."""
    counts = {item["Package"]: [0, 0] for item in candidates}
    for line in text.splitlines():
        if line.startswith(("Inst ", "Conf ")):
            matches = []
            for item in candidates:
                name = re.escape(item["Package"]) + r"(?::" + re.escape(item["Architecture"]) + r")?"
                if re.fullmatch(line[:5] + name + r" \(" + re.escape(item["Version"]) + r"(?: [^\r\n]+)?\)", line):
                    matches.append(item["Package"])
            if len(matches) != 1:
                raise RuntimeError("Exact package plan would install/upgrade/configure an unadmitted package")
            counts[matches[0]][0 if line.startswith("Inst ") else 1] += 1
        elif line.startswith(("Remv ", "Purg ")):
            raise RuntimeError("Exact package plan would remove a package")
    if any(value != [1, 1] for value in counts.values()):
        raise RuntimeError("APT did not propose exactly the new admitted packages")


def install_exact_files(packages, candidates, environment, initial):
    """Download verified archives; dpkg never resolves new dependencies.

    Simulation is an admission check, not a concurrency guarantee. The actual
    mutation uses only these archives without APT's dependency solver, so a race
    cannot expand this request to another package or a kernel image. Missing
    dependencies fail rather than being installed. Trusted package maintainer
    scripts still run; arbitrary concurrent root actions cannot be prevented.
    """
    if (not isinstance(candidates, list) or not candidates
            or any(not isinstance(item, dict) for item in candidates)):
        raise RuntimeError("Incomplete exact-package candidate list")
    admitted = {}
    for metadata in candidates:
        package = metadata.get("Package", "")
        version, architecture = metadata.get("Version", ""), metadata.get("Architecture", "")
        digest, size = metadata.get("SHA256", ""), metadata.get("Size", "")
        if (package not in packages or package in admitted
                or not re.fullmatch(r"[0-9][A-Za-z0-9.+:~_-]*", version)
                or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", architecture)
                or not re.fullmatch(r"[a-f0-9]{64}", digest)
                or not re.fullmatch(r"[0-9]+", size)
                or not 0 < int(size) <= 512 * 1024 * 1024):
            raise RuntimeError("Incomplete exact-package candidate identity/hash/size")
        admitted[package] = ("ii ", version, architecture)
    if packages[0] not in admitted:
        raise RuntimeError("The required exact package was not admitted")
    for name in initial:
        if name.split(":", 1)[0] in admitted:
            raise RuntimeError("Exact package already has saved state; investigate missing module without replacing it")
    plan = subprocess.run(
        ["/usr/bin/apt-get", "--simulate", "--no-upgrade", "--no-remove", "--no-install-recommends",
         "install", *[f'{item["Package"]}={item["Version"]}' for item in candidates]],
        env=environment, check=True, capture_output=True, text=True,
    )
    exact_package_plan(plan.stdout, candidates)
    trusted_directory(Path("/var/cache"))
    with tempfile.TemporaryDirectory(prefix=".shiri-exact-package-", dir="/var/cache") as directory:
        archives = []
        for metadata in candidates:
            package, version, architecture = (metadata[key] for key in ("Package", "Version", "Architecture"))
            private = Path(directory) / package
            private.mkdir(mode=0o700)
            subprocess.run(
                ["/usr/bin/apt-get", "download", f"{package}={version}"],
                cwd=private, env=environment, check=True,
            )
            files = list(private.iterdir())
            if len(files) != 1 or files[0].suffix != ".deb":
                raise RuntimeError("Exact package download produced unexpected files")
            archive = files[0]
            info = archive.lstat()
            root_metadata(info)
            if info.st_size != int(metadata["Size"]):
                raise RuntimeError("Exact package archive size does not match APT metadata")
            with archive.open("rb") as handle:
                checksum = hashlib.sha256()
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    checksum.update(block)
            if checksum.hexdigest() != metadata["SHA256"]:
                raise RuntimeError("Exact package archive SHA256 does not match APT metadata")
            fields = subprocess.run(
                ["/usr/bin/dpkg-deb", "--field", str(archive), "Package", "Version", "Architecture"],
                env=environment, check=True, capture_output=True, text=True,
            )
            expected = f"Package: {package}\nVersion: {version}\nArchitecture: {architecture}\n"
            if fields.stdout != expected:
                raise RuntimeError("Downloaded archive is not the admitted exact package/version/architecture")
            archives.append(archive)
        if package_states(environment) != initial:
            raise RuntimeError("Installed packages changed during exact-package admission; nothing was installed")
        subprocess.run(
            ["/usr/bin/dpkg", "--refuse-configure-any", "--refuse-depends",
             "--refuse-depends-version", "--refuse-conflicts", "--refuse-breaks", "--refuse-downgrade",
             "--refuse-overwrite", "--refuse-overwrite-dir", "--refuse-architecture", "--refuse-bad-version",
             "--skip-same-version", "--install", *map(str, archives)],
            env=environment, check=True,
        )
    after = package_states(environment)
    observed = {name: value for name, value in after.items() if name.split(":", 1)[0] in admitted}
    if ({name.split(":", 1)[0]: value for name, value in observed.items()} != admitted
            or len(observed) != len(admitted)
            or {name: value for name, value in after.items() if name not in observed} != initial):
        raise RuntimeError("Exact package install did not preserve other package identities; inspect dpkg state before retrying")


def root_metadata(info, *, directory=False):
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (not expected(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
            or (not directory and info.st_nlink != 1)):
        raise RuntimeError("Dependency policy paths must be root-owned, regular and not writable by other users")


def trusted_directory(path):
    for item in (Path(path), *Path(path).parents):
        root_metadata(item.lstat(), directory=True)


def same_file(path, info):
    current = path.lstat()
    return (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino)


@contextmanager
def deny_service_actions(policy=Path("/usr/sbin/policy-rc.d")):
    trusted_directory(policy.parent)
    original = None
    try:
        original = policy.lstat()
    except FileNotFoundError:
        pass
    if original is not None:
        root_metadata(original)
        handle = os.open(policy, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            if not same_file(policy, os.fstat(handle)):
                raise RuntimeError("The existing service policy changed during validation")
            existing = os.read(handle, 16385)
            if len(existing) > 16384 or MARKER in existing:
                raise RuntimeError("A previous Shiri policy needs operator recovery before installing dependencies")
        finally:
            os.close(handle)
    backup_directory = Path(tempfile.mkdtemp(prefix=".shiri-policy-", dir=policy.parent))
    backup = backup_directory / "original"
    candidate = backup_directory / "deny"
    candidate.write_bytes(b"#!/bin/sh\n" + MARKER + os.fsencode(backup) + b"\nexit 101\n")
    candidate.chmod(0o755)
    installed = candidate.stat()
    published = False
    restored = False
    try:
        if original is None:
            os.link(candidate, policy, follow_symlinks=False)
        else:
            if not same_file(policy, original):
                raise RuntimeError("The existing service policy changed; nothing was replaced")
            os.link(policy, backup, follow_symlinks=False)
            if not same_file(policy, original):
                raise RuntimeError("The existing service policy changed; nothing was replaced")
            os.replace(candidate, policy)
        published = True
        yield
    finally:
        if published:
            if not same_file(policy, installed):
                raise RuntimeError(f"Service policy changed concurrently; recover the saved policy from {backup_directory}")
            if original is None:
                policy.unlink()
            else:
                if not same_file(backup, original):
                    raise RuntimeError(f"Saved service policy changed; inspect {backup_directory} before recovery")
                os.replace(backup, policy)
            restored = True
        if not published or restored:
            shutil.rmtree(backup_directory)


@contextmanager
def installation_lock(path=Path("/run/shiri-dependencies.lock")):
    trusted_directory(path.parent)
    handle = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        root_metadata(os.fstat(handle))
        if os.fstat(handle).st_mode & 0o077:
            raise RuntimeError("The dependency installation lock must have mode 0600")
        fcntl.flock(handle, fcntl.LOCK_EX)
        if not same_file(path, os.fstat(handle)):
            raise RuntimeError("Dependency installation lock was replaced")
        yield
    finally:
        os.close(handle)


def install(packages, *, exact_candidate=None):
    if os.geteuid() != 0:
        raise RuntimeError("Dependency installation requires root")
    if not packages or any(not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", item) for item in packages):
        raise RuntimeError("Pass explicit Debian package names only")
    if exact_candidate is not None and (len(packages) > 2 or len(set(packages)) != len(packages)
                                       or not callable(exact_candidate)):
        raise RuntimeError("Exact-file installation requires at most two explicit packages and a candidate validator")
    environment = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "DEBIAN_FRONTEND": "noninteractive",
                   "NEEDRESTART_MODE": "l", "LC_ALL": "C.UTF-8"}
    with installation_lock(), deny_service_actions():
        initial = None
        if exact_candidate is not None:
            audit = subprocess.run(["/usr/bin/dpkg", "--audit"], env=environment, check=True,
                                   capture_output=True, text=True)
            if audit.stdout.strip() or audit.stderr.strip():
                raise RuntimeError("Existing dpkg state needs recovery before exact-package installation")
            initial = package_states(environment)
        subprocess.run(["/usr/bin/apt-get", "update"], env=environment, check=True)
        if exact_candidate is None:
            subprocess.run(["/usr/bin/apt-get", "install", "-y", *packages], env=environment, check=True)
        else:
            metadata = exact_candidate(packages[0], environment)
            install_exact_files(packages, metadata, environment, initial)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packages", nargs="+")
    try:
        install(parser.parse_args().packages)
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc)) from exc
