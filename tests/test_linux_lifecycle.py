"""Explicit opt-in lifecycle/crash checks against a dedicated real installation.

Run as root on Linux with pinned backends, eight snd-aloop substreams, an idle
slot 7 and a bridged wired LAN interface. Required environment:

    SHIRI_LINUX_LIFECYCLE_TESTS=1
    SHIRI_TEST_INTERFACE=eth0
    SHIRI_TEST_PREFIX=shiri-test-lifecycle
    SHIRI_TEST_STATE_DIR=/var/lib/shiri-test-lifecycle
    SHIRI_TEST_BINARY_DIR=/opt/shiri/backends  # Optional; otherwise use PATH.

The persistent test installation identity is retained, not recreated each
cycle. These checks advertise a test AirPlay receiver but select no speakers,
play no program audio and never use production runtime/state defaults.
"""

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import sys
import time
from uuid import UUID, uuid5

import pytest

from shiri.domain import Room
from shiri.rpc import RpcError, call_rpc
from shiri.runtime.broker import Broker
from shiri.runtime.network import NetworkManager
from shiri.runtime.system import Runner, atomic_json, process_birth, root_directory
from shiri.settings import Settings

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("SHIRI_LINUX_LIFECYCLE_TESTS") != "1",
    reason="Opt in to dedicated Linux lifecycle checks with SHIRI_LINUX_LIFECYCLE_TESTS=1",
)
SLOT = 7
CYCLES = 50


def require_closed_slot():
    for direction in ("pcm0p", "pcm0c", "pcm1p", "pcm1c"):
        path = Path(f"/proc/asound/Loopback/{direction}/sub{SLOT}/status")
        assert path.read_text().strip() == "closed", f"Loopback slot {SLOT} is in use: {path}"


async def kernel_snapshot():
    runner = Runner()
    links = await runner.json(["ip", "-j", "-d", "link", "show"])
    addresses = await runner.json(["ip", "-j", "addr", "show"])
    namespaces = await runner.run(["ip", "netns", "list"])
    return {
        "links": {
            link["ifname"]: {
                "ifindex": link["ifindex"], "address": link.get("address"),
                "alias": link.get("ifalias"), "kind": link.get("linkinfo", {}).get("info_kind"),
            }
            for link in links
        },
        "addresses": {
            link["ifname"]: sorted(
                (address["family"], address["local"], address["prefixlen"])
                for address in link.get("addr_info", [])
            )
            for link in addresses
        },
        "namespaces": sorted(line.split()[0] for line in namespaces.stdout.splitlines() if line.split()),
    }


@dataclass
class Installation:
    base: Path
    config: Settings
    definition: Room
    installation_id: str

    def manifest(self):
        return json.loads((self.config.runtime_state_dir / "ownership.json").read_text())

    def environment(self):
        env = {
            **os.environ,
            "SHIRI_STATE_DIR": str(self.config.state_dir),
            "SHIRI_RUNTIME_STATE_DIR": str(self.config.runtime_state_dir),
            "SHIRI_RUNTIME_DIR": str(self.config.runtime_dir),
            "SHIRI_RUNTIME_SOCKET": str(self.config.runtime_socket),
            "SHIRI_SIMULATION": "0",
            "SHIRI_MAX_ROOMS": str(self.config.max_rooms),
        }
        if self.config.binary_dir:
            env["SHIRI_BINARY_DIR"] = str(self.config.binary_dir)
        else:
            env.pop("SHIRI_BINARY_DIR", None)
        return env

    def report(self, name, evidence):
        atomic_json(self.base / "reports" / f"{name}.json", {
            "installation_id": self.installation_id,
            "interface": self.definition.interface,
            "slot": SLOT,
            **evidence,
        })


@pytest.fixture
def installation():
    assert os.geteuid() == 0, "The opted-in runtime test requires root on Linux"
    interface = os.environ.get("SHIRI_TEST_INTERFACE")
    prefix = os.environ.get("SHIRI_TEST_PREFIX", "")
    state_dir = os.environ.get("SHIRI_TEST_STATE_DIR", "")
    assert interface, "Set SHIRI_TEST_INTERFACE explicitly to the bridged wired adapter"
    assert re.fullmatch(r"shiri-test-[a-z0-9-]{1,21}", prefix), "Use an explicit shiri-test-... prefix"
    base = Path(state_dir)
    assert state_dir and base.is_absolute() and base.name == prefix, (
        "SHIRI_TEST_STATE_DIR must be absolute and its final directory must equal SHIRI_TEST_PREFIX"
    )
    root_directory(base)
    lock = (base / "test.lock").open("a")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        config = Settings(
            state_dir=base / "api", runtime_state_dir=base / "runtime", runtime_dir=base / "run",
            runtime_socket=base / "run" / "runtime.sock",
            binary_dir=Path(os.environ["SHIRI_TEST_BINARY_DIR"]) if os.environ.get("SHIRI_TEST_BINARY_DIR")
            else None,
        )
        root_directory(config.runtime_state_dir)
        network = NetworkManager(config.runtime_state_dir, Runner())
        assert not network.manifest["networks"] and not network.manifest["processes"], (
            "Dedicated test installation has retained resources; inspect its manifest before rerunning"
        )
        require_closed_slot()
        definition = Room(
            id=str(uuid5(UUID(network.installation_id), prefix)), slot=SLOT,
            name=prefix, airplay_name=f"{prefix} input", interface=interface,
        )
        yield Installation(base, config, definition, network.installation_id)
    finally:
        lock.close()


async def wait_room(snapshot, room_id, status, *, timeout=60):
    async def wait():
        while True:
            view = await snapshot()
            assert view["ready"], view.get("error")
            room = next((item for item in view["rooms"] if item["room_id"] == room_id), None)
            if room and room["status"] == status:
                return room
            if room and room["status"] in {"error", "degraded"}:
                pytest.fail(f"Room did not converge to {status}: {room}")
            await asyncio.sleep(0.02)
    return await asyncio.wait_for(wait(), timeout)


async def assert_clean(installation, baseline):
    manifest = installation.manifest()
    assert manifest["installation_id"] == installation.installation_id
    assert not manifest["networks"] and not manifest["processes"], manifest
    require_closed_slot()
    assert await kernel_snapshot() == baseline, "Host links, addresses or namespaces changed after cleanup"


async def test_fifty_enable_disable_cycles_release_owned_runtime_and_preserve_host(installation):
    baseline = await kernel_snapshot()
    service = Broker(installation.config)
    cycles = []
    first_receiver = None
    try:
        started = await service.start(serve=False)
        assert started["ready"], started["error"]
        async def snapshot():
            return service.snapshot()
        for cycle in range(CYCLES):
            before = time.monotonic()
            enabled = installation.definition.model_copy(update={"enabled": True, "revision": 2 * cycle + 1})
            await service.reconcile({"rooms": [enabled.model_dump()]})
            running = await wait_room(snapshot, enabled.id, "running")
            state = service.rooms[enabled.id]
            assert running["selected_ids"] == []
            assert running["processes"] and all(item["alive"] for item in running["processes"])
            assert state.reserved_slot == SLOT and service.slot_locks[SLOT].locked()
            ipaddress.IPv4Address(running["receiver_ip"])
            receiver = (state.receiver["namespace"], state.receiver["mac"], state.receiver["alias"])
            if first_receiver is None:
                first_receiver = receiver
            assert receiver == first_receiver, "Room receiver identity changed between cycles"
            assert await service.network.owned_macvlan(state.receiver)
            assert await service.network.owned_macvlan(service.sender)
            disabled = enabled.model_copy(update={"enabled": False, "revision": 2 * cycle + 2})
            await service.reconcile({"rooms": [disabled.model_dump()]})
            await wait_room(snapshot, enabled.id, "stopped")
            assert not state.processes and state.receiver is None and state.reserved_slot is None
            assert not service.sender_users and not service.speaker_leases
            assert not any(lock.locked() for lock in service.slot_locks)
            await assert_clean(installation, baseline)
            cycles.append({"cycle": cycle + 1, "seconds": round(time.monotonic() - before, 3),
                           "receiver_ip": running["receiver_ip"]})
            print(f"Lifecycle cycle {cycle + 1}/{CYCLES}: {cycles[-1]}", flush=True)
    finally:
        await asyncio.wait_for(service.close(), 45)
        installation.report("lifecycle", {"completed_cycles": len(cycles), "cycles": cycles,
                                          "versions": service.versions})
    assert len(cycles) == CYCLES
    await assert_clean(installation, baseline)


async def wait_rpc(socket):
    async def wait():
        while True:
            try:
                return await call_rpc(socket, "health", timeout=1)
            except RpcError:
                await asyncio.sleep(0.03)
    return await asyncio.wait_for(wait(), 45)


@pytest.mark.parametrize("phase", ["sender_reserved", "room_running"])
async def test_sigkill_broker_recovers_dedicated_installation_without_touching_foreign_process(
    installation, phase,
):
    baseline = await kernel_snapshot()
    log_path = installation.base / f"broker-{phase}.log"
    sentinel = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(600)")
    process = None
    recovering = None
    saved = None
    with log_path.open("wb") as output:
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "shiri.runtime.broker", env=installation.environment(),
                stdout=output, stderr=asyncio.subprocess.STDOUT,
            )
            health = await wait_rpc(installation.config.runtime_socket)
            assert health["ready"], health["error"]
            enabled = installation.definition.model_copy(update={"enabled": True})
            await call_rpc(installation.config.runtime_socket, "reconcile", {"rooms": [enabled.model_dump()]})
            if phase == "room_running":
                async def snapshot():
                    return await call_rpc(installation.config.runtime_socket, "health", timeout=1)
                await wait_room(snapshot, enabled.id, "running")
            else:
                async def reserved():
                    while "sender" not in installation.manifest()["networks"]:  # noqa: ASYNC110 - observe durable external state.
                        await asyncio.sleep(0.005)
                await asyncio.wait_for(reserved(), 15)
            process.send_signal(signal.SIGKILL)
            await asyncio.wait_for(process.wait(), 3)
            assert process.returncode == -signal.SIGKILL
            saved = installation.manifest()
            assert saved["networks"], "SIGKILL must interrupt an owned resource phase"
            recovering = Broker(installation.config)
            health = await recovering.start(serve=False)
            assert health["ready"], health["error"]
            await assert_clean(installation, baseline)
            for entry in saved["processes"].values():
                assert process_birth(entry["pid"]) != entry["birth"], "A recorded room daemon survived recovery"
            assert sentinel.returncode is None and process_birth(sentinel.pid), "Recovery touched a foreign process"
            await recovering.reconcile({"rooms": [enabled.model_dump()]})
            async def recovered_snapshot():
                return recovering.snapshot()
            await wait_room(recovered_snapshot, enabled.id, "running")
            assert sentinel.returncode is None
        finally:
            if process is not None and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 45)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            try:
                if recovering is not None:
                    await asyncio.wait_for(recovering.close(), 45)
            finally:
                if sentinel.returncode is None:
                    with suppress(ProcessLookupError):
                        sentinel.terminate()
                await asyncio.wait_for(sentinel.wait(), 3)
    await assert_clean(installation, baseline)
    installation.report(f"recovery-{phase}", {
        "phase": phase, "recorded_processes": len(saved["processes"]),
        "recorded_networks": len(saved["networks"]), "recovered": True,
        "foreign_process_untouched": True, "versions": recovering.versions,
    })
