"""Exercise the actual END observation class against recorded counter states."""

import ast
from copy import deepcopy
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from shiri.runtime.system import RuntimeFailure


ROOT = Path(__file__).resolve().parents[1]
MANUAL = ROOT / "tests/linux/check_native_bluetooth_route.py"
GROUP = ROOT / "tests/linux/check_native_grouping.py"
HEALTHY_AST = "1b71e0f399506b138e7a29112186f329dd56d043be6b29f04926ef44d43f5a7c"
PRODUCER_FILE = "270d02c249af63c51e5f884ffa08490dcd8ea14ad675b7c167f0d79ea7c4f10a"


def require(condition, message):
    if not condition:
        raise RuntimeFailure(message)


def proof():
    tree = ast.parse(MANUAL.read_text())
    definition = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                      and node.name == "EndedTransportSilence")
    environment = {"deepcopy": deepcopy, "require": require, "route": SimpleNamespace(MAX_PACKETS=64000)}
    exec(compile(ast.Module(body=[definition], type_ignores=[]), str(MANUAL), "exec"), environment)
    return environment["EndedTransportSilence"]


@pytest.mark.parametrize("before", [True, -1, "2", 2**63])
def test_original_flush_observation_is_typed_and_bounded(before):
    _, _, receiver = sample()
    with pytest.raises(RuntimeFailure):
        proof()(before, receiver)


def sample():
    # BT132's final authenticated bridge sample: open socket, no queued PCM,
    # completed DropSync, and counters that stopped with the last RTP callback.
    bridge = {
        "ready": True, "error": None, "peer_authorized": True, "active": True,
        "uses_system_bus": False, "uses_alsa_devices": False,
        "frames_forwarded": 1284480, "frames_discarded": 0, "flushes": 3,
        "queue_bytes": 0, "last_output_at": 10457.779060111,
    }
    health = {"ready": True, "error": None, "source": {"ready": True, "owner": None}}
    receiver = {
        "session_id": "fbb96d72-4860-4b75-9208-9f4e43f96045", "epoch": 1,
        "incarnation": "2cec251b-c0b1-4579-b727-02ebd7559476", "generation": 2,
        "stage": "ended_idle", "finished": False,
        "native_end_idle": {"generation": 4, "source_retired": True, "descriptor_closed": True,
                            "receiver_unit_held": True, "observed_monotonic_ns": 10457770000000,
                            "deadline_monotonic_ns": 10577770000000},
    }
    return bridge, health, receiver


def observe(guard, bridge, health, receiver, now, *, packets=2675, chunks=2675):
    return guard.observe(bridge, health, receiver, packets, chunks, now)


def test_recorded_bt132_open_socket_qualifies_only_after_measured_unchanged_pcm_rtp_window():
    bridge, health, receiver = sample()
    guard = proof()(2, receiver)
    assert observe(guard, bridge, health, receiver, 10457.8) is None
    assert observe(guard, bridge, health, receiver, 10458.299) is None
    retired = observe(guard, bridge, health, receiver, 10458.301)
    assert retired["kind"] == "actual_framed_session_idle_after_completed_drop"
    assert retired["socket_retained"] is True and retired["source_owner_idle"] is True
    assert retired["last_packet"] == 2674 and retired["first_block"] == 2675
    assert retired["unchanged_frames_forwarded"] == 1284480
    assert retired["unchanged_frames_discarded"] == 0 and retired["queue_bytes"] == 0
    assert retired["completed_flushes"] == 3 and retired["no_new_transport_seconds"] >= .5


def test_closed_socket_still_needs_completed_drop_and_measured_pcm_transport_idle():
    bridge, health, receiver = sample()
    bridge["active"] = False
    guard = proof()(2, receiver)
    assert observe(guard, bridge, health, receiver, 10457.8) is None
    assert observe(guard, bridge, health, receiver, 10458.31)["kind"] == (
        "actual_framed_session_ended_after_completed_drop"
    )


@pytest.mark.parametrize("change", ["pcm", "discard", "flush", "output_clock", "rtp", "decoded"])
def test_any_new_output_or_transport_observation_restarts_the_whole_idle_window(change):
    bridge, health, receiver = sample()
    guard = proof()(2, receiver)
    assert observe(guard, bridge, health, receiver, 10457.8) is None
    packets = chunks = 2675
    if change == "pcm":
        bridge["frames_forwarded"] += 480
    elif change == "discard":
        bridge["frames_discarded"] += 480
    elif change == "flush":
        bridge["flushes"] += 1
    elif change == "output_clock":
        bridge["last_output_at"] = 10458.2
    elif change == "rtp":
        packets += 1
    else:
        chunks += 1
    assert observe(guard, bridge, health, receiver, 10458.2, packets=packets, chunks=chunks) is None
    assert observe(guard, bridge, health, receiver, 10458.31, packets=packets, chunks=chunks) is None
    assert observe(guard, bridge, health, receiver, 10458.71, packets=packets, chunks=chunks)


@pytest.mark.parametrize("change", ["queued_pcm", "uncompleted_drop"])
def test_pending_final_output_never_qualifies_merely_because_rtp_has_stopped(change):
    bridge, health, receiver = sample()
    if change == "queued_pcm":
        bridge["queue_bytes"] = 1920
    else:
        bridge["flushes"] = 2
    guard = proof()(2, receiver)
    assert observe(guard, bridge, health, receiver, 10457.8) is None
    assert observe(guard, bridge, health, receiver, 10467.8) is None
    bridge["queue_bytes"] = 0
    bridge["flushes"] = 3
    assert observe(guard, bridge, health, receiver, 10468.0) is None
    assert observe(guard, bridge, health, receiver, 10468.51)


@pytest.mark.parametrize("change", ["source_owner", "source_unready", "worker_error", "receiver_exit",
                                    "end_receipt", "new_session", "unowned_peer", "system_bus", "alsa"])
def test_exact_retired_source_held_receiver_and_descriptor_authority_are_required_each_time(change):
    bridge, health, receiver = sample()
    guard = proof()(2, receiver)
    assert observe(guard, bridge, health, receiver, 10457.8) is None
    if change == "source_owner":
        health["source"]["owner"] = {"session_id": "successor"}
    elif change == "source_unready":
        health["source"]["ready"] = False
    elif change == "worker_error":
        health["error"] = "fault"
    elif change == "receiver_exit":
        receiver["finished"] = True
    elif change == "end_receipt":
        receiver["native_end_idle"]["observed_monotonic_ns"] += 1
    elif change == "new_session":
        receiver["session_id"] = "other"
    elif change == "unowned_peer":
        bridge["peer_authorized"] = False
    elif change == "system_bus":
        bridge["uses_system_bus"] = True
    else:
        bridge["uses_alsa_devices"] = True
    with pytest.raises(RuntimeFailure):
        observe(guard, bridge, health, receiver, 10458.31)


@pytest.mark.parametrize("field,value", [("frames_forwarded", True), ("frames_discarded", -1),
                                        ("flushes", "3"), ("queue_bytes", None),
                                        ("last_output_at", float("nan")), ("last_output_at", 10460),
                                        ("active", 1)])
def test_malformed_counter_and_clock_evidence_is_refused(field, value):
    bridge, health, receiver = sample()
    guard = proof()(2, receiver)
    bridge[field] = value
    with pytest.raises(RuntimeFailure):
        observe(guard, bridge, health, receiver, 10458.31)


@pytest.mark.parametrize("change", ["frames", "packets", "decoded", "output_clock", "observed_clock"])
def test_counter_or_clock_rollback_cannot_fabricate_a_silence_window(change):
    bridge, health, receiver = sample()
    guard = proof()(2, receiver)
    assert observe(guard, bridge, health, receiver, 10457.8) is None
    packets = chunks = 2675
    now = 10458.31
    if change == "frames":
        bridge["frames_forwarded"] -= 480
    elif change == "packets":
        packets -= 1
    elif change == "decoded":
        chunks -= 1
    elif change == "output_clock":
        bridge["last_output_at"] -= .01
    else:
        now = 10457.79
    with pytest.raises(RuntimeFailure):
        observe(guard, bridge, health, receiver, now, packets=packets, chunks=chunks)


def test_all_daemon_identity_and_producer_guards_preserve_the_reviewed_bytes():
    assert hashlib.sha256(GROUP.read_bytes()).hexdigest() == PRODUCER_FILE
    tree = ast.parse(MANUAL.read_text())
    exercise = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "exercise")
    healthy = next(node for node in ast.walk(exercise) if isinstance(node, ast.AsyncFunctionDef)
                   and node.name == "healthy")
    for item in ast.walk(healthy):
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            assert not getattr(item, "type_params", [])
            if "type_params" not in item._fields:
                item._fields = (*item._fields, "type_params")
                item.type_params = []
    fingerprint = hashlib.sha256(ast.dump(healthy, include_attributes=False).encode()).hexdigest()
    assert fingerprint == HEALTHY_AST
    retired = next(node for node in ast.walk(exercise) if isinstance(node, ast.AsyncFunctionDef)
                   and node.name == "retired")
    assert "end_idle.observe(bridge, health, receiver" in ast.unparse(retired)
    assert "await healthy()" in ast.unparse(exercise)
