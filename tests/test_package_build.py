"""Offline package builds with the installer's actual pip commands.

Ordinary checks need no package index. For the real wheel/hash/build cases,
set SHIRI_BUILD_WHEELHOUSE to a directory of the seven verified universal
wheels from install/build_requirements.lock. Tests create disposable venvs;
they never execute the privileged installer or download a dependency.
"""
from email import message_from_bytes
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import threading
import zipfile

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version
import pytest


ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "install/build_requirements.lock"


def requirements():
    logical = LOCK.read_text().replace("\\\n", " ")
    found = {}
    for line in logical.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([a-zA-Z0-9-]+)==([^\s]+)\s+--hash=sha256:([a-f0-9]{64})", line.strip())
        assert match is not None, "Every build dependency needs an exact version and wheel hash"
        name, version, digest = match.groups()
        name = canonicalize_name(name)
        assert name not in found, "Duplicate build dependency"
        found[name] = version, digest
    return found


def installer_command(kind, python):
    lines = (ROOT / "install/install.sh").read_text().splitlines()
    target = {"tools": '"$SOURCE/install/build_requirements.lock"',
              "runtime": '"$SOURCE/install/requirements.lock"', "source": '"$SOURCE"'}[kind]
    matches = [line for line in lines if line.startswith('"$PREFIX/venv/bin/python" -m pip ')
               and target in line]
    assert len(matches) == 1, "Test the exact package command published by the installer"
    command = shlex.split(matches[0])
    command[0] = str(python)
    return [value.replace("$SOURCE", str(ROOT)) for value in command]


def test_build_system_pin_and_hash_lock_agree_without_unbounded_requirements():
    build_system = (ROOT / "pyproject.toml").read_text().split("[project]", 1)[0]
    pin = re.search(r'^requires = \["hatchling==([^\"]+)"\]$', build_system, re.MULTILINE)
    assert pin is not None and requirements()["hatchling"][0] == pin.group(1)
    assert 'build-backend = "hatchling.build"' in build_system


def test_package_step_cannot_resolve_an_implicit_backend_or_runtime_dependency():
    command = installer_command("source", "/unused/python")
    assert {"--no-index", "--no-build-isolation", "--no-deps"}.issubset(command)
    tools = installer_command("tools", "/unused/python")
    assert {"--require-hashes", "--only-binary=:all:"}.issubset(tools)
    runtime = installer_command("runtime", "/unused/python")
    assert {"--require-hashes", "--only-binary=:all:"}.issubset(runtime)


@pytest.fixture(scope="module")
def wheelhouse():
    configured = os.environ.get("SHIRI_BUILD_WHEELHOUSE")
    if not configured:
        pytest.skip("Supply SHIRI_BUILD_WHEELHOUSE for actual offline pip/build checks")
    path = Path(configured).resolve(strict=True)
    wheels = {}
    for wheel in path.glob("*.whl"):
        with zipfile.ZipFile(wheel) as archive:
            metadata = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            assert len(metadata) == 1
            info = message_from_bytes(archive.read(metadata[0]))
        name = canonicalize_name(info["Name"])
        if name not in requirements():
            continue
        assert name not in wheels, "Supply one pinned universal wheel per build dependency"
        assert wheel.name.endswith("-py3-none-any.whl")
        assert (info["Version"], hashlib.sha256(wheel.read_bytes()).hexdigest()) == requirements()[name]
        wheels[name] = wheel, info
    assert set(wheels) == set(requirements()), "Incomplete verified build wheelhouse"
    return path, wheels


@pytest.fixture
def build_environment(tmp_path, wheelhouse):
    prefix = tmp_path / "venv"
    result = subprocess.run([sys.executable, "-m", "venv", str(prefix)], capture_output=True, text=True,
                            timeout=30)
    assert result.returncode == 0, result.stderr
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("PIP_") and key not in {"PYTHONPATH", "PYTHONHOME"}}
    env.update(PIP_CONFIG_FILE=os.devnull, PIP_NO_INDEX="1", PIP_FIND_LINKS=str(wheelhouse[0]),
               PIP_DISABLE_PIP_VERSION_CHECK="1")
    return prefix / "bin/python", env


def run(command, env):
    return subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("version", ["3.10", "3.14"])
def test_verified_wheels_close_the_build_dependency_graph_on_supported_python(wheelhouse, version):
    _, wheels = wheelhouse
    for _name, (_wheel, info) in wheels.items():
        assert Version(version) in SpecifierSet(info["Requires-Python"] or "")
        for value in info.get_all("Requires-Dist", []):
            dependency = Requirement(value)
            environment = {"python_version": version, "python_full_version": version + ".0", "extra": ""}
            if dependency.marker and not dependency.marker.evaluate(environment):
                continue
            assert canonicalize_name(dependency.name) in wheels, value
            locked_version = wheels[canonicalize_name(dependency.name)][1]["Version"]
            assert Version(locked_version) in dependency.specifier, value


def test_actual_offline_build_and_install_preserves_package_entrypoint_and_web_assets(build_environment):
    python, env = build_environment
    installed = run(installer_command("tools", python), env)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    built = run(installer_command("source", python), env)
    assert built.returncode == 0, built.stdout + built.stderr
    inspected = run([str(python), "-I", "-c", """
from importlib.metadata import distribution
import json
d = distribution('shiri-audio')
print(json.dumps({'version': d.version, 'entrypoints': {e.name:e.value for e in d.entry_points},
                  'files': sorted(str(p) for p in d.files)}))
"""], env)
    assert inspected.returncode == 0, inspected.stderr
    metadata = json.loads(inspected.stdout)
    assert metadata["version"] == "2.0.0" and metadata["entrypoints"]["shiri"] == "shiri.cli:main"
    assert {"shiri/web/index.html", "shiri/web/app.js", "shiri/web/style.css",
            "shiri/runtime/native.py", "shiri/runtime/receiver_probe.py"}.issubset(metadata["files"])


def test_tampered_build_wheel_is_refused_before_backend_install(build_environment, wheelhouse, tmp_path):
    python, env = build_environment
    tampered = tmp_path / "tampered"
    tampered.mkdir()
    for name, (wheel, _) in wheelhouse[1].items():
        destination = tampered / wheel.name
        shutil.copyfile(wheel, destination)
        if name == "hatchling":
            with destination.open("ab") as stream:
                stream.write(b"unexpected wheel bytes")
    env["PIP_FIND_LINKS"] = str(tampered)
    refused = run(installer_command("tools", python), env)
    assert refused.returncode != 0
    assert "DO NOT MATCH THE HASHES" in refused.stdout + refused.stderr
    absent = run([str(python), "-I", "-c", "import importlib.util; assert importlib.util.find_spec('hatchling') is None"], env)
    assert absent.returncode == 0, absent.stderr


def test_missing_build_backend_fails_offline_without_installing_a_replacement(build_environment):
    python, env = build_environment
    requests = []

    class BuildIndex(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_error(503, "No implicit build resolution")

        def log_message(self, *args):
            pass

    index = ThreadingHTTPServer(("127.0.0.1", 0), BuildIndex)
    serving = threading.Thread(target=lambda: index.serve_forever(poll_interval=.05), daemon=True)
    serving.start()
    env.pop("PIP_NO_INDEX")
    env["PIP_INDEX_URL"] = f"http://127.0.0.1:{index.server_port}/simple/"
    try:
        refused = run(installer_command("source", python), env)
        assert refused.returncode != 0 and "hatchling" in refused.stdout + refused.stderr
        assert requests == [], "A missing build tool must not trigger index resolution"
    finally:
        index.shutdown()
        index.server_close()
        serving.join(timeout=1)
        assert not serving.is_alive()
    absent = run([str(python), "-I", "-c", "import importlib.util; assert importlib.util.find_spec('hatchling') is None"], env)
    assert absent.returncode == 0, absent.stderr


def test_runtime_source_distribution_cannot_launch_an_unpinned_isolated_build(build_environment, tmp_path):
    python, env = build_environment
    sources = tmp_path / "source-only"
    sources.mkdir()
    archive_path = sources / "shiri_build_probe-1.0.tar.gz"
    # A correctly hashed sdist would otherwise ask pip for this build backend.
    metadata = b'[build-system]\nrequires=["shiri-unpinned-backend==1"]\nbuild-backend="backend"\n'
    with tarfile.open(archive_path, "w:gz") as archive:
        info = tarfile.TarInfo("shiri_build_probe-1.0/pyproject.toml")
        info.size = len(metadata)
        archive.addfile(info, io.BytesIO(metadata))
    lock = tmp_path / "source-only.lock"
    lock.write_text("shiri-build-probe==1.0 --hash=sha256:"
                    + hashlib.sha256(archive_path.read_bytes()).hexdigest() + "\n")
    command = installer_command("runtime", python)
    command[command.index("-r") + 1] = str(lock)
    env["PIP_FIND_LINKS"] = str(sources)
    refused = run(command, env)
    assert refused.returncode != 0
    assert "No matching distribution found" in refused.stdout + refused.stderr
    assert "Installing build dependencies" not in refused.stdout + refused.stderr
