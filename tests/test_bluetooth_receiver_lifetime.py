"""Actual manual-producer END lifecycle, with only kernel/credentials replaced."""

import ast
import asyncio
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import signal
import socket
from types import SimpleNamespace
import time
from uuid import UUID, uuid4

import pytest

from shiri.runtime.system import RuntimeFailure
from shiri.runtime.timing import FLAG_AIRPLAY2, FLAG_GROUP_LEADER, Kind, Packet, RATE


ROOT = Path(__file__).resolve().parents[1]
GROUP_FILE = ROOT / "tests/linux/check_native_grouping.py"
ROUTE_FILE = ROOT / "tests/linux/check_native_bluetooth_route.py"
A = "b6786543-7eb2-443d-83b1-65b984123a76"
GROUP = "894a68d5-3cd8-44bd-9667-34f558249b84"
HEALTHY_PREIMAGE = "1b71e0f399506b138e7a29112186f329dd56d043be6b29f04926ef44d43f5a7c"


def require(condition, message):
    if not condition:
        raise RuntimeFailure(message)


def undo_idle_await(producer):
    end = [
        node
        for node in ast.walk(producer)
        if isinstance(node, ast.If) and ast.unparse(node.test) == "action['action'] == 'end'"
    ]
    assert len(end) == 1
    assert (
        ast.unparse(end[0].body[-2])
        == "await hold_bluetooth_receiver_after_end(config, connection, path, state, stop)"
    )
    assert isinstance(end[0].body[-1], ast.Break)
    end[0].body.pop(-2)


def functions(path, names, environment, *, restore_end=False):
    tree = ast.parse(path.read_text())
    definitions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]
    assert {node.name for node in definitions} == set(names)
    if restore_end:
        assert names == {"producer"}
        undo_idle_await(definitions[0])
    exec(compile(ast.Module(body=definitions, type_ignores=[]), str(path), "exec"), environment)


class Connection:
    def __init__(self):
        self.closed = False

    def setblocking(self, value):
        assert value is False

    def close(self):
        self.closed = True


class Harness:
    def __init__(self, tmp_path, *, patched=True, eof=True, idle=True, remaining_seconds=180):
        self.directory = tmp_path
        self.eof = eof
        self.connection = Connection()
        self.packets = []
        self.grant = None
        self.stop = None
        self.status = tmp_path / "status.json"
        self.command = tmp_path / "command.json"
        self.config = {
            "uid": 501,
            "gid": 20,
            "socket": "/reviewed-test-only",
            "status": str(self.status),
            "command": str(self.command),
            "group": GROUP,
            "common_start_ns": 0,
            "arrival_lead_ns": 220_000_000,
            "leader": True,
            "wide_bracket": False,
            "duration_seconds": 180,
        }
        if idle:
            self.config["bluetooth_receiver_idle"] = {
                "version": 1,
                "room_id": A,
                "started_monotonic_ns": 0,
                "deadline_monotonic_ns": 0,
            }
        self.config_path = tmp_path / "config.json"
        fake_asyncio = SimpleNamespace(
            Event=self.event,
            get_running_loop=lambda: self,
            wait_for=asyncio.wait_for,
            TimeoutError=asyncio.TimeoutError,
        )
        environment = {
            "asyncio": fake_asyncio,
            "json": json,
            "Path": Path,
            "os": SimpleNamespace(geteuid=lambda: 501, getegid=lambda: 20, getpid=lambda: 123),
            "socket": SimpleNamespace(
                AF_UNIX=socket.AF_UNIX,
                SOCK_SEQPACKET=socket.SOCK_SEQPACKET,
                socket=lambda *_args: self.connection,
            ),
            "signal": signal,
            "time": time,
            "UUID": UUID,
            "uuid4": uuid4,
            "replace": replace,
            "A": A,
            "MAX_DURATION": 90,
            "FLAGS": FLAG_AIRPLAY2,
            "FLAG_GROUP_LEADER": FLAG_GROUP_LEADER,
            "Packet": Packet,
            "Kind": Kind,
            "RATE": RATE,
            "require": require,
            "RuntimeFailure": RuntimeFailure,
            "atomic_json": self.publish,
            "bracketed_packet": self.pcm,
            "send_native_packet": self.send_pcm,
            "observation": SimpleNamespace(redact_exception=lambda value: str(value)),
        }
        functions(GROUP_FILE, {"retire_producer", "hold_bluetooth_receiver_after_end"}, environment)
        functions(GROUP_FILE, {"producer"}, environment, restore_end=not patched)
        self.environment = environment
        # AST extraction prepares the fixture; it must not consume the actual
        # producer's declared delivery calendar or bounded receiver lifetime.
        now = time.monotonic_ns()
        self.config["common_start_ns"] = now + 220_000_000
        if idle:
            started = now - int((180 - remaining_seconds) * 1e9)
            self.config["bluetooth_receiver_idle"].update(
                started_monotonic_ns=started, deadline_monotonic_ns=started + 180_000_000_000
            )
        self.publish(self.config_path, self.config)
        self.publish(
            self.command,
            {"generation": 2, "action": "run", "common_start_ns": self.config["common_start_ns"]},
        )

    def event(self):
        self.stop = asyncio.Event()
        return self.stop

    def add_signal_handler(self, name, callback):
        assert name in {signal.SIGTERM, signal.SIGINT}
        assert callback.__self__ is self.stop

    @staticmethod
    def publish(path, state):
        Path(path).write_text(json.dumps(state))

    async def sock_connect(self, connection, path):
        assert connection is self.connection and path == "/reviewed-test-only"

    async def sock_sendall(self, connection, raw):
        assert connection is self.connection and not connection.closed
        packet = Packet.decode(raw)
        self.packets.append(packet)
        if packet.kind is Kind.BEGIN:
            self.grant = replace(packet, kind=Kind.GRANT, epoch=1, generation=1, incarnation=uuid4().bytes)

    async def sock_recv(self, connection, size):
        assert connection is self.connection and size == 4096
        if self.packets[-1].kind is Kind.BEGIN:
            return self.grant.encode()
        assert self.packets[-1].kind is Kind.END
        return b"" if self.eof else b"unexpected reply"

    @staticmethod
    def pcm(grant, group, frame, sequence, start, *_args, **_kwargs):
        return replace(
            grant,
            kind=Kind.PCM,
            frames=960,
            frame_index=frame,
            sequence=sequence,
            pcm=bytes(960 * 4),
            presentation_ns=start,
        )

    async def send_pcm(self, loop, connection, packet, target, *, stats):
        assert loop is self
        await self.sock_sendall(connection, packet)
        self.publish(self.command, {"generation": 4, "action": "end"})
        return 0

    def launch(self):
        return asyncio.create_task(self.environment["producer"](self.config_path))

    async def wait_idle(self, task):
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if task.done():
                await task
                raise AssertionError("Producer ended instead of remaining an idle receiver")
            if self.status.exists() and json.loads(self.status.read_text()).get("stage") == "ended_idle":
                return json.loads(self.status.read_text())
            await asyncio.sleep(0.001)
        raise AssertionError("No exact receiver-idle status")


@pytest.mark.asyncio
async def test_actual_original_producer_exits_on_end_while_corrected_receiver_holds_same_unit(tmp_path):
    original_dir = tmp_path / "original"
    original_dir.mkdir()
    original = Harness(original_dir, patched=False)
    old_task = original.launch()
    await asyncio.wait_for(old_task, 1)
    assert [packet.kind for packet in original.packets] == [Kind.BEGIN, Kind.PCM, Kind.END]
    assert json.loads(original.status.read_text())["stage"] == "finished"
    assert original.connection.closed
    corrected_dir = tmp_path / "corrected"
    corrected_dir.mkdir()
    corrected = Harness(corrected_dir)
    task = corrected.launch()
    try:
        status = await corrected.wait_idle(task)
        assert status["finished"] is False and status["frames"] == 960
        assert status["native_end_idle"]["source_retired"] is True
        assert status["native_end_idle"]["descriptor_closed"] is True
        assert corrected.connection.closed and not task.done()
        packets = deepcopy(corrected.packets)
        await asyncio.sleep(0.01)
        assert corrected.packets == packets  # No fake music or new admitted source while idle.
        corrected.stop.set()  # Existing producer signal event; no second stop-event race.
        await asyncio.wait_for(task, 1)
        assert json.loads(corrected.status.read_text())["finished"] is True
    finally:
        corrected.stop.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_default_profile_retains_original_end_and_exit_behavior(tmp_path):
    harness = Harness(tmp_path, idle=False)
    await asyncio.wait_for(harness.launch(), 1)
    state = json.loads(harness.status.read_text())
    assert state["stage"] == "finished" and state["finished"] is True
    assert "native_end_idle" not in state


@pytest.mark.asyncio
async def test_calendar_and_idle_deadline_start_after_slow_source_preparation(tmp_path, monkeypatch):
    prepare = functions
    completed = []

    def slow_prepare(path, names, environment, **options):
        prepare(path, names, environment, **options)
        if names == {"producer"}:
            time.sleep(0.18)  # Deliberately exceeds the unchanged 150ms cadence bound.
            completed.append(time.monotonic_ns())

    monkeypatch.setitem(globals(), "functions", slow_prepare)
    harness = Harness(tmp_path)
    profile = harness.config["bluetooth_receiver_idle"]
    assert profile["started_monotonic_ns"] >= completed[0]
    assert harness.config["common_start_ns"] == profile["started_monotonic_ns"] + 220_000_000
    assert profile["deadline_monotonic_ns"] - profile["started_monotonic_ns"] == 180_000_000_000
    task = harness.launch()
    try:
        await harness.wait_idle(task)
        assert [packet.kind for packet in harness.packets] == [Kind.BEGIN, Kind.PCM, Kind.END]
    finally:
        harness.stop.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_missing_exact_eof_is_refused_before_any_idle_permission(tmp_path):
    harness = Harness(tmp_path, eof=False)
    with pytest.raises(RuntimeFailure, match="END did not close"):
        await asyncio.wait_for(harness.launch(), 1)
    state = json.loads(harness.status.read_text())
    assert state["error"] and "native_end_idle" not in state


@pytest.mark.asyncio
async def test_hold_has_original_absolute_wall_limit_and_refuses_timeout(tmp_path):
    harness = Harness(tmp_path, remaining_seconds=0.06)
    task = harness.launch()
    await harness.wait_idle(task)
    with pytest.raises(RuntimeFailure, match="declared lifetime"):
        await asyncio.wait_for(task, 1)
    assert json.loads(harness.status.read_text())["error"]


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_room",
        "wrong_duration",
        "boolean_version",
        "changed_deadline",
        "unsupported_profile",
        "wrong_generation",
    ],
)
@pytest.mark.asyncio
async def test_idle_permission_rejects_retargeted_or_unbounded_profile(tmp_path, mutation):
    harness = Harness(tmp_path)
    config = deepcopy(harness.config)
    state = {
        "stage": "streaming",
        "finished": False,
        "frames": 960,
        "commands": [{"generation": 4, "action": "end"}],
    }
    if mutation == "wrong_room":
        config["bluetooth_receiver_idle"]["room_id"] = "other"
    elif mutation == "wrong_duration":
        config["duration_seconds"] = 420
    elif mutation == "boolean_version":
        config["bluetooth_receiver_idle"]["version"] = True
    elif mutation == "changed_deadline":
        config["bluetooth_receiver_idle"]["deadline_monotonic_ns"] += 1
    elif mutation == "unsupported_profile":
        config["music_startup"] = "unexpected"
    else:
        state["commands"][-1]["generation"] = 3
    with pytest.raises(RuntimeFailure):
        await harness.environment["hold_bluetooth_receiver_after_end"](
            config, harness.connection, harness.status, state, asyncio.Event()
        )
    assert not harness.connection.closed and "native_end_idle" not in state


@pytest.mark.parametrize(
    "mutation",
    [
        "flag_integer",
        "room_b",
        "no_admission",
        "other_device",
        "duration",
        "music_mode",
        "already_started",
        "explicit_lead",
    ],
)
@pytest.mark.asyncio
async def test_idle_fixture_launch_is_refused_before_any_receiver_replacement(mutation):
    environment = {"require": require, "RuntimeFailure": RuntimeFailure, "MAX_DURATION": 90, "A": A}
    functions(GROUP_FILE, {"launch_producer"}, environment)
    state = SimpleNamespace(
        desired=SimpleNamespace(id=A, local_audio_device="bluealsa:DEV=12:34:56:78:9A:BC,PROFILE=a2dp"),
        bluetooth_admission=object(),
    )
    arguments = {"duration_seconds": 180, "bluetooth_receiver_idle": True}
    start = 0
    if mutation == "flag_integer":
        arguments["bluetooth_receiver_idle"] = 1
    elif mutation == "room_b":
        state.desired.id = "5356832c-c514-4472-bb4b-4f34ed86dd07"
    elif mutation == "no_admission":
        state.bluetooth_admission = None
    elif mutation == "other_device":
        state.desired.local_audio_device = "hw:Loopback,0,7"
    elif mutation == "duration":
        arguments["duration_seconds"] = 420
    elif mutation == "music_mode":
        arguments["music_startup"] = True
    elif mutation == "already_started":
        start = 123
    else:
        arguments["arrival_lead_ns"] = 220_000_000
    with pytest.raises(RuntimeFailure, match="Idle receiver hold requires"):
        # Any later provisioning access to this bare object would fail the test.
        await environment["launch_producer"](object(), state, Path("/not-created"), start, **arguments)


def test_full_bluetooth_daemon_identity_and_alive_guard_unchanged():
    def healthy(path):
        tree = ast.parse(path.read_text())
        outer = next(
            node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "exercise"
        )
        return next(
            node
            for node in ast.walk(outer)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "healthy"
        )

    assert clock_fences()["canonical_ast_fingerprint"](healthy(ROUTE_FILE)) == HEALTHY_PREIMAGE


def clock_fences():
    path = ROOT / "tests/test_native_clock_recovery.py"
    tree = ast.parse(path.read_text())
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id in {"PRODUCER_PREIMAGES", "SOAK_OBSERVATIONS"}
            for target in node.targets
        )
    ]
    environment = {"ast": ast, "deepcopy": deepcopy, "hashlib": hashlib}
    exec(compile(ast.Module(body=assignments, type_ignores=[]), str(path), "exec"), environment)
    functions(path, {"canonical_ast_fingerprint", "restore_original_producer"}, environment)
    return environment


def test_exact_one_reviewed_end_seam_preserves_the_original_producer_ast_hash():
    environment = clock_fences()
    tree = ast.parse(GROUP_FILE.read_text())
    restored = environment["restore_original_producer"](tree, "check_native_grouping.py")
    assert (
        environment["canonical_ast_fingerprint"](restored)
        == environment["PRODUCER_PREIMAGES"]["check_native_grouping.py"]["sha256"]
    )
    modified = deepcopy(tree)
    hold = next(
        node
        for node in ast.walk(modified)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "hold_bluetooth_receiver_after_end"
    )
    hold.args[-1] = ast.Name(id="different_stop_event", ctx=ast.Load())
    with pytest.raises(AssertionError):
        environment["restore_original_producer"](modified, "check_native_grouping.py")
