#!/usr/bin/python3
"""Supply snd-aloop for an already running Ubuntu generic kernel only.

An available module (including a built-in driver) requires no package change.
Missing drivers on other distributions or custom kernels need explicit operator
provisioning. This helper never selects a kernel image or a kernel metapackage.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys


ENVIRONMENT = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C.UTF-8"}
GENERIC_RELEASE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+-[0-9]+-generic(?:-64k)?")


def command(arguments, *, environment=ENVIRONMENT):
    return subprocess.run(arguments, env=environment, check=True, capture_output=True, text=True).stdout


def available():
    # Ignore an operator 'install' replacement: a successful shell command is
    # not evidence that the actual loopback driver exists. This never loads it.
    result = subprocess.run(
        ["/usr/sbin/modprobe", "--ignore-install", "--dry-run", "snd-aloop"],
        env=ENVIRONMENT, check=False, capture_output=True, text=True,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(f"Cannot probe snd-aloop: {result.stderr.strip()}")
    return result.returncode == 0


def ubuntu_id(path=Path("/etc/os-release")):
    with path.open(encoding="utf-8") as handle:
        content = handle.read(4097)
    if len(content) > 4096:
        raise RuntimeError("Unrecognized oversized operating-system identity")
    identity = None
    for line in content.splitlines():
        if line.startswith("ID="):
            if identity is not None:
                raise RuntimeError("Ambiguous operating-system identity")
            fields = shlex.split(line[3:], comments=True)
            if len(fields) != 1:
                raise RuntimeError("Unrecognized operating-system identity")
            identity = fields[0]
    return identity == "ubuntu"


def control_fields(text):
    """Read one Debian control paragraph, refusing duplicates/ambiguity."""
    result = {}
    last = None
    for line in text.strip().splitlines():
        if line.startswith((" ", "\t")) and last is not None:
            result[last] += "\n" + line
            continue
        if not line or ": " not in line:
            raise RuntimeError("Ambiguous APT candidate metadata")
        key, value = line.split(": ", 1)
        if key in result:
            raise RuntimeError("Duplicate APT candidate metadata")
        result[key] = value
        last = key
    return result


def package_candidate(package, environment, *, architecture):
    policy = command(["/usr/bin/apt-cache", "policy", package], environment=environment)
    versions = re.findall(r"^  Candidate: (\S+)$", policy, re.MULTILINE)
    if len(versions) != 1 or versions[0] == "(none)":
        raise RuntimeError(f"No exact running-kernel package candidate for {package}; provision snd-aloop explicitly")
    version = versions[0]
    if not re.fullmatch(r"[0-9][A-Za-z0-9.+:~_-]*", version):
        raise RuntimeError("Invalid APT candidate version")
    metadata = control_fields(command(
        ["/usr/bin/apt-cache", "show", "--no-all-versions", f"{package}={version}"],
        environment=environment,
    ))
    if (metadata.get("Package"), metadata.get("Version"), metadata.get("Architecture")) != (
        package, version, architecture,
    ):
        raise RuntimeError("APT candidate does not match the exact running-kernel package/version/native architecture")
    return metadata


def candidate(package, environment, release):
    """Called after APT refresh, under the dependency install lock/policy."""
    if os.uname().release != release or not ubuntu_id():
        raise RuntimeError("Operating system/running kernel changed during module admission")
    architecture = command(["/usr/bin/dpkg", "--print-architecture"], environment=environment).strip()
    metadata = package_candidate(package, environment, architecture=architecture)
    source = metadata.get("Source", "").split(" ", 1)[0]
    if not re.fullmatch(r"linux(?:-hwe-[0-9]+\.[0-9]+)?", source):
        raise RuntimeError("APT candidate is not a supported Ubuntu kernel-source package")
    images = subprocess.run(
        ["/usr/bin/dpkg-query", "--show", "--showformat=${binary:Package}\t${db:Status-Status}\t${Version}\t${Architecture}\n",
         f"linux-image-{release}", f"linux-image-unsigned-{release}"],
        env=environment, check=False, capture_output=True, text=True,
    )
    installed = []
    if images.returncode not in (0, 1):
        raise RuntimeError("Cannot inspect the exact running-kernel image package")
    for line in images.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 4 or fields[0].split(":", 1)[0] not in {
            f"linux-image-{release}", f"linux-image-unsigned-{release}",
        }:
            raise RuntimeError("Unrecognized running-kernel image package identity")
        if fields[1] == "installed":
            installed.append(fields[2:])
    if installed != [[metadata["Version"], architecture]]:
        raise RuntimeError("Exact extra-modules candidate must match the already installed running-kernel image version")
    candidates = [metadata]
    if re.search(r"(?:^|,\s*)wireless-regdb(?:\s*\([^)]*\))?(?=,|$)", metadata.get("Depends", "")):
        state = subprocess.run(
            ["/usr/bin/dpkg-query", "--show", "--showformat=${db:Status-Status}\n", "wireless-regdb"],
            env=environment, check=False, capture_output=True, text=True,
        )
        if state.returncode == 1 or (state.returncode == 0 and state.stdout.strip() == "not-installed"):
            registry = package_candidate("wireless-regdb", environment, architecture="all")
            if registry.get("Source", "wireless-regdb").split(" ", 1)[0] != "wireless-regdb":
                raise RuntimeError("Wireless registry candidate has an unexpected source")
            candidates.insert(0, registry)
        elif state.returncode != 0 or state.stdout.strip() != "installed":
            raise RuntimeError("Wireless registry has unfinished package state; recover it explicitly")
    return candidates


def dependency_installer():
    # Python -I deliberately excludes the checkout from sys.path. Load only the
    # reviewed adjacent helper rather than a same-named import from the host.
    source = Path(__file__).resolve().with_name("apt_dependencies.py")
    spec = importlib.util.spec_from_file_location("shiri_install_apt_dependencies", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ensure_loopback():
    if os.geteuid() != 0 or sys.platform != "linux":
        raise RuntimeError("Kernel-module provisioning requires root on Linux")
    release = os.uname().release
    if available():
        return
    if not ubuntu_id() or not GENERIC_RELEASE.fullmatch(release):
        raise RuntimeError(
            f"snd-aloop is missing for {release}; automatic provisioning supports Ubuntu generic kernels only. "
            "Provision the driver for this exact kernel explicitly; Shiri will not replace the kernel."
        )
    package = f"linux-modules-extra-{release}"
    dependency_installer().install(
        [package, "wireless-regdb"], exact_candidate=lambda name, environment: candidate(name, environment, release),
    )
    if os.uname().release != release or not available():
        raise RuntimeError(f"Installed {package}, but snd-aloop is still unavailable for the unchanged running kernel")


if __name__ == "__main__":
    try:
        ensure_loopback()
    except (RuntimeError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc)) from exc
