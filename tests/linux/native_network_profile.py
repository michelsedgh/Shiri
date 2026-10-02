"""Independent boot-bound admission for the real AirPlay network gate.

This does not import, alter, or extend the historical native_lab profile. The
operator reviews and stages the printed profile only after installing the
final composed candidate into an idle, disposable Linux guest.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import sys

from shiri.runtime.bind_policy import trusted_file
from shiri.runtime.identities import DaemonIdentities
from shiri.runtime.system import RuntimeFailure, boot_id

PATCHES = {
    "shairport_native_startup_patch": "bab272cab5764fc3ebb0f8169b6a94b8318b78f63e09fd7e368058901b9a71d8",
    "owntone_speaker_balance_patch": "e61e28af5bdaefaa49d355681469ebb8cf23247b8a18e5c3a4261186d2d963a0",
    "owntone_native_transition_patch": "912922fb7853d25fb031d0258effeb33f3332a971b84f01e01c79014a194e8a7",
    "shairport_receiver_volume_patch": "26003aa1b8df99c5de6eecc21074bf259158e64baefd41a35c704145d5957bbd",
    "shairport_bounded_events_patch": "a7ffecbe2fe0da846b12ec34b2303a6279d5ad4f7ba4c2312234db0c634d7ecd",
    "owntone_paused_speech_patch": "eb2f9ceb0e58f3c92d82c682cd177b98d3b0a48d4848aa5b430f3761703da928",
    "owntone_event_ack_patch": "08ead94d976619985445e8177756ee08be82d50a6b03432faadcdd281fbc3f73",
}
REQUIRED_SOURCE = {"tests/linux/check_native_network.py", "tests/linux/native_network_profile.py",
                   "tests/linux/native_network_observer.py", "tests/linux/isolated_group_lan.py"}
REQUIRED_BINARY = {"bin/nqptp", "bin/shairport-sync", "sbin/airptpd", "sbin/avahi-daemon",
                   "sbin/owntone", "libexec/shiri-bind-policy", "libexec/shiri-pcm-exec",
                   "share/shiri/backends.json", "share/shiri/runtime-helpers.json"}


def require(value, message):
    if not value:
        raise RuntimeFailure(message)


def digest(path):
    path = trusted_file(Path(path))
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(descriptor)
        h = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            while block := stream.read(1024*1024):
                h.update(block)
        after, named = os.fstat(descriptor), path.lstat()
        def fields(i):
            return i.st_dev, i.st_ino, i.st_size, i.st_mtime_ns, i.st_ctime_ns
        require(fields(before) == fields(after) == fields(named), "Pinned gate file changed while reading")
        return h.hexdigest()
    finally:
        os.close(descriptor)


def path(value):
    require(isinstance(value, str) and re.fullmatch(r"/[A-Za-z0-9_./-]+", value)
            and ".." not in Path(value).parts, "Gate path must be a canonical absolute path")
    return Path(value)


def inventory(project, binaries):
    source_names = REQUIRED_SOURCE | {str(p.relative_to(project)) for p in (project/"shiri").rglob("*.py")}
    binary_names = REQUIRED_BINARY | {str(p.relative_to(binaries)) for p in (binaries/"lib").rglob("*")
                                      if p.is_file() and not p.is_symlink()}
    source = {name: digest(project/name) for name in sorted(source_names)}
    binary = {name: digest(binaries/name) for name in sorted(binary_names)}
    # Library symlinks are part of the build contract. Pin both their link text
    # and the trusted final regular inode; never silently follow a new target.
    links = {}
    for item in sorted((binaries/"lib").rglob("*")):
        if item.is_symlink():
            links[str(item.relative_to(binaries))] = {"target": os.readlink(item),
                                                     "resolved": str(item.resolve(strict=True)),
                                                     "sha256": digest(item.resolve(strict=True))}
    return {"source": source, "binaries": binary, "library_links": links}


def idle_installation(identities):
    manifest_path = identities.runtime_state_dir/"ownership.json"
    digest(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    require(manifest.get("installation_id") == identities.installation_id
            and manifest.get("networks") == {} and manifest.get("processes") == {},
            "The exact managed gate installation must have no owned networks or processes")
    return manifest


def template(*, project, binaries, identity_file, work):
    require(sys.platform == "linux" and os.geteuid() == 0 and boot_id(),
            "Gate profile is staged by Linux root after the disposable guest starts")
    project, binaries, identity_file, work = map(lambda p: path(str(p)), (project, binaries, identity_file, work))
    require(project == Path(__file__).resolve().parents[2], "Profile project differs from this exact staged gate")
    identity = DaemonIdentities(identity_file).load()
    idle_installation(identity)
    digest(binaries/"share/shiri/backends.json")
    build = json.loads((binaries/"share/shiri/backends.json").read_text())
    require(build.get("owntone") == "d6fb3edf5831de38134ebd92fcf09a730ddd37aa"
            and build.get("shairport") == "7bad231c18368dbd26f298577f6210e36e4b0797"
            and all(build.get(key) == value for key, value in PATCHES.items()),
            "Network gate requires the reviewed balance, transition, bounded event exchange and paused-speech build")
    # The ordinary Broker still validates truthful executable versions. This
    # profile adds an independent exact-byte installation contract, not a
    # version fallback or a change to production preflight.
    require(str(work).startswith("/var/lib/") and work not in {identity.runtime_state_dir, identity.runtime_dir}
            and identity.runtime_state_dir not in work.parents and identity.runtime_dir not in work.parents,
            "Gate work directory must be separate from immutable installation state/run paths")
    return {"version": 1, "purpose": "Shiri real encrypted AirPlay network gate",
            "boot_id": boot_id(), "hostname": socket.gethostname(),
            "machine_id": Path("/etc/machine-id").read_text().strip(),
            "project": str(project), "binary_dir": str(binaries), "work": str(work),
            "identity_file": str(identity_file), "identity_sha256": digest(identity_file),
            "installation_id": identity.installation_id,
            "runtime_state_dir": str(identity.runtime_state_dir), "runtime_dir": str(identity.runtime_dir),
            "python": sys.executable, "python_sha256": digest(Path(sys.executable).resolve(strict=True)),
            "inventory": inventory(project, binaries), "patches": PATCHES}


def admit(profile_path, *, require_idle=True):
    profile_path = trusted_file(path(str(profile_path)))
    info = profile_path.lstat()
    require(stat.S_IMODE(info.st_mode) == 0o600 and info.st_size < 1024*1024,
            "Gate profile must be a bounded root-private 0600 file")
    digest(profile_path)
    data = json.loads(profile_path.read_text())
    expected = {"version", "purpose", "boot_id", "hostname", "machine_id", "project", "binary_dir", "work",
                "identity_file", "identity_sha256", "installation_id", "runtime_state_dir", "runtime_dir",
                "python", "python_sha256", "inventory", "patches"}
    require(set(data) == expected and data["version"] == 1
            and data["purpose"] == "Shiri real encrypted AirPlay network gate"
            and sys.platform == "linux" and os.geteuid() == 0
            and data["boot_id"] == boot_id() and data["hostname"] == socket.gethostname()
            and data["machine_id"] == Path("/etc/machine-id").read_text().strip()
            and data["python"] == sys.executable
            and data["python_sha256"] == digest(Path(sys.executable).resolve(strict=True))
            and data["patches"] == PATCHES, "Gate profile does not match this exact boot/interpreter/reviewed build")
    project, binaries = path(data["project"]), path(data["binary_dir"])
    require(project == Path(__file__).resolve().parents[2], "Gate source path differs from admitted staged source")
    require(data["inventory"] == inventory(project, binaries), "Gate source/native/library bytes changed after admission")
    identity = DaemonIdentities(path(data["identity_file"])).load()
    require(data["identity_sha256"] == digest(identity.path)
            and data["installation_id"] == identity.installation_id
            and path(data["runtime_state_dir"]) == identity.runtime_state_dir
            and path(data["runtime_dir"]) == identity.runtime_dir,
            "Gate must preserve the exact provisioned installation map and owning paths")
    work = path(data["work"])
    require(str(work).startswith("/var/lib/") and work not in {identity.runtime_state_dir, identity.runtime_dir}
            and identity.runtime_state_dir not in work.parents and identity.runtime_dir not in work.parents,
            "Gate work directory overlaps the immutable installation boundary")
    if require_idle:
        idle_installation(identity)
    return data
