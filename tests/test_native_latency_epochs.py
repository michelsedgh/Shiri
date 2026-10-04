"""Actual policy, signal and epoch coroutines; external OS facts simulated."""

from collections import deque
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from shiri.domain import Room, SpeakerRef
from shiri.runtime.latency import latency_plan
from shiri.runtime.native import NativeMixer
from shiri.runtime.speech_output import SpeechOutput
from shiri.runtime.system import RuntimeFailure

np = pytest.importorskip("numpy")
pytest.importorskip("aiortc")
ROOT = Path(__file__).parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tests/linux" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


group = load("epoch_actual_group", "check_native_grouping.py")
epoch = load("epoch_actual_policy", "native_latency_epochs.py")
probe = load("epoch_actual_marker", "native_latency_probe.py")


def rooms(offset):
    return [
        Room(
            id=identifier,
            slot=slot,
            name=f"Epoch{slot}",
            airplay_name=f"Epoch{slot}",
            interface="fixture0",
            enabled=True,
            volume=100,
            duck_gain=0.2,
            local_audio_device=f"hw:CARD=Loopback,DEV={device},SUBDEV=7",
            speakers=[
                SpeakerRef(
                    id="0", name="Local", protocol="alsa", offset_ms=offset if identifier == group.A else 0
                )
            ],
        )
        for identifier, slot, device in ((group.A, 6, 1), (group.B, 7, 0))
    ]


def healths(definitions):
    plan = latency_plan(definitions)
    result = {}
    for room in definitions:
        output = SpeechOutput(Path("/private/epoch-test/speech.sock"), room.id, "a" * 32, 1234)
        writer = SimpleNamespace(reader_present=True, written_bytes=0, dropped_bytes=0)
        mixer = NativeMixer(
            Path("/private/epoch-test/music.fifo"),
            writer=writer,
            speech_output=output,
            now_ns=lambda: 10_000_000_000,
            relay_delay_ns=plan.common_horizon_ns,
            output_buffer_ms=plan.for_room(room.id).output_buffer_ms,
        )
        result[room.id] = {**mixer.health(), "source": {"ready": True, "owner": None}}
    return result


def pcm(gain=1, voice=0):
    positions = np.arange(960)
    mono = 8192 * gain * np.sin(2 * np.pi * 440 * positions / 48000) + voice * np.sin(
        2 * np.pi * 880 * positions / 48000
    )
    return np.repeat(mono.astype("<i2")[:, None], 2, axis=1).tobytes()


def final_guard(chunks, times):
    capture = SimpleNamespace(
        chunks=chunks,
        captured_at=times,
        absolute={at: round(at * 1e9) for at in times},
        max_packet_gap=0.02,
        last_packet_at=None,
        poll=lambda: None,
    )
    guard = group.FinalPcmGuard(capture)
    guard.begin(0)
    guard.preserve_reference(group.observation.spectrum([pcm()] * 8, 48000, minimum_seconds=0.1))
    return guard


@pytest.mark.parametrize(
    "offset,expected_h,expected_a", [(-2000, 2140, 2040), (0, 140, 40), (2000, 140, 40)]
)
def test_generic_epoch_freeze_uses_actual_current_workers_saved_intent_and_exact_local_offsets(
    offset, expected_h, expected_a
):
    phase = epoch.Phase(str(uuid4()), offset, "native")
    definitions = rooms(offset)
    observed = healths(definitions)
    result = epoch.plan_receipt(phase, definitions, observed, before_pcm=True)
    assert result["common_horizon_ns"] == expected_h * 1_000_000
    assert result["output_buffers_ms"] == {group.A: expected_a, group.B: 40}
    for health in observed.values():
        health["source"]["owner"] = {"session_id": "now-playing"}
        health["native_blocks"] = 10
    assert epoch.plan_receipt(phase, definitions, observed, before_pcm=False) == result
    with pytest.raises(RuntimeFailure, match="precede owner/producer PCM"):
        epoch.plan_receipt(phase, definitions, observed, before_pcm=True)


@pytest.mark.parametrize(
    "fault",
    [
        "h",
        "b",
        "h_bool",
        "b_bool",
        "pcm",
        "pcm_bool",
        "owner",
        "owner_missing",
        "ready",
        "error",
        "error_missing",
        "source_ready",
        "missing",
        "extra",
        "offset",
        "protocol",
    ],
)
def test_pre_pcm_epoch_freeze_refuses_wrong_actual_plan_source_and_endpoint(fault):
    definitions = rooms(-2000)
    observed = healths(definitions)
    value = observed[group.A]
    if fault == "h":
        value["timing_relay_delay_ms"] = 1000
    elif fault == "b":
        value["output_buffer_ms"] = 500
    elif fault == "h_bool":
        value["timing_relay_delay_ms"] = True
    elif fault == "b_bool":
        value["output_buffer_ms"] = True
    elif fault == "pcm":
        value["native_blocks"] = 1
    elif fault == "pcm_bool":
        value["native_blocks"] = False
    elif fault == "owner":
        value["source"]["owner"] = {"session_id": "old"}
    elif fault == "owner_missing":
        value["source"].pop("owner")
    elif fault == "ready":
        value["ready"] = False
    elif fault == "error":
        value["error"] = "fault"
    elif fault == "error_missing":
        value.pop("error")
    elif fault == "source_ready":
        value["source"]["ready"] = False
    elif fault == "missing":
        observed.pop(group.B)
    elif fault == "extra":
        observed["other"] = deepcopy(value)
    else:
        update = {"offset_ms": 0} if fault == "offset" else {"protocol": "airplay"}
        definitions[0] = definitions[0].model_copy(
            update={"speakers": [definitions[0].speakers[0].model_copy(update=update)]}
        )
    with pytest.raises((RuntimeFailure, ValueError)):
        epoch.plan_receipt(epoch.Phase(str(uuid4()), -2000, "native"), definitions, observed, before_pcm=True)


def test_marker_disqualified_pulse_cannot_bias_later_qualifying_coded_onset():
    trigger = 10_000_000_000
    gate = probe.MarkerGate(audible=True, trigger_ns=trigger)
    gate.push(pcm(0, 600), trigger + 50_000_000, 10.05)
    gate.push(pcm(0), trigger + 70_000_000, 10.07)
    assert gate.first_absolute is None and gate.first_callback is None
    for block in range(26):
        at = trigger + 200_000_000 + block * 20_000_000
        if gate.push(pcm(0, 600 if block // 6 % 2 == 0 else 390), at, at / 1e9):
            break
    assert gate.passed and gate.first_absolute == trigger + 200_000_000 and gate.first_callback == 10.2
    assert gate.receipt()["encoder_to_final_pts_seconds"] == 0.2


def test_later_qualifying_code_cannot_claim_missing_cold_utterance_prefix_is_complete():
    trigger = 10_000_000_000
    gate = probe.MarkerGate(audible=True, trigger_ns=trigger)
    # Known utterance begins at B500; erase its first360ms but retain later code.
    for block in range(18):
        at = trigger + 500_000_000 + block * 20_000_000
        assert gate.push(pcm(0), at, at / 1e9 + .002) is False
    for block in range(26):
        at = trigger + 860_000_000 + block * 20_000_000
        if gate.push(pcm(0, 600 if block // 6 % 2 == 0 else 390), at, at / 1e9 + .002):
            break
    receipt = gate.receipt()
    assert receipt["passed"] is True
    assert receipt["first_matching_absolute_pts_ns"] == trigger + 860_000_000
    assert receipt["cold_utterance_completeness_passed"] is False
    assert receipt["cold_utterance_completeness_status"] == "pending_finite_opus_prefix_tail_reference"


def test_stop_scope_preserves_all_earlier_queued_content_and_labels_only_later_callbacks():
    guard = final_guard([pcm(), pcm(), pcm(0)], [10.0, 10.02, 10.04])
    guard.end_at(10_030_000_000)
    guard.check(live=False)
    assert guard.checked_blocks == guard.reference_blocks == 2
    assert guard.evidence()["teardown_blocks"] == 1
    assert guard.capture is not None and guard.index == 3
    assert guard.evidence()["stop_requested_monotonic_ns"] == 10_030_000_000


def test_bad_earlier_queued_buffer_cannot_be_reclassified_by_a_stop_boundary():
    guard = final_guard([pcm(), pcm(0.2), pcm(0)], [10.0, 10.02, 10.04])
    guard.end_at(10_030_000_000)
    with pytest.raises(RuntimeFailure, match="individual untouched-zone PCM block changed"):
        guard.check(live=False)
    guard.capture.chunks[1] = pcm()
    with pytest.raises(RuntimeFailure):
        guard.check(live=False)
    with pytest.raises(RuntimeFailure):
        guard.end_at(9_000_000_000)
    assert guard.failed_block["index"] == 1 and guard.teardown_blocks == 0


def test_stop_scope_still_rejects_actual_caps_sample_frame_loss_in_the_final_queue():
    capture = probe.capture_type(group.Capture).__new__(probe.capture_type(group.Capture))
    capture.error, capture.capture_dropped, capture.needs_latency = None, 0, False
    capture.total, capture.discontinuities, capture.max_packet_gap = 0, 0, 0.02
    capture.expected_base, capture.clock_offset_ns, capture.device = 0, 0, "synthetic"
    capture.rate = capture.channels = capture.format = None
    capture.chunks, capture.captured_at, capture.absolute = (
        [],
        [],
        {10.0: 10_000_000_000, 10.04: 10_040_000_000},
    )
    capture.sequence = group.observation.PcmSequence()
    initial = {"offset": 0, "offset_end": 960, "pts": 0, "duration": 20_000_000, "discont": True}
    missing = {**initial, "offset": 1920, "offset_end": 2880, "pts": 40_000_000, "discont": False}
    capture.pending = deque(
        [(10.0, pcm(), 48000, 2, "S16LE", initial), (10.04, pcm(0), 48000, 2, "S16LE", missing)]
    )
    guard = group.FinalPcmGuard(capture)
    guard.end_at(10_030_000_000)
    with pytest.raises(RuntimeFailure, match="lost or repeated"):
        guard.check(live=False)
    assert capture.sequence.blocks == 1 and guard.teardown_blocks == 0


def alignment(delay=0):
    return {"relative_offset_ms": delay, "correlation": 0.999, "peak_prominence": 0.1, "resolution_ms": 1}


def marker(session):
    return {
        "passed": True,
        "status": "complete",
        "session_id": session,
        "same_session_for_cold_and_warm": True,
        "encoder_marker": {"first_emitted_monotonic_ns": 10_000_000_000},
        "trigger_monotonic_ns": 10_000_000_000,
        "first_matching_absolute_pts_ns": 10_200_000_000,
        "encoder_to_final_pts_seconds": 0.2,
        "encoder_to_final_callback_seconds": 0.201,
        "consecutive_frames": 19200,
        "coded_120ms_levels": ["high", "low", "high"],
        "duck_and_voice": {"passed": True},
        "following_restore": {"passed": True},
        "following_silence": {"passed": True},
    }


def reports():
    result, phases = [], epoch.phases()
    for phase in phases:
        definitions = rooms(phase.offset_ms)
        frozen = epoch.plan_receipt(phase, definitions, healths(definitions), before_pcm=True)
        source = {"session_id": str(uuid4()), "epoch": 1, "incarnation": str(uuid4())}
        target = {"session_id": str(uuid4()), "epoch": 1, "incarnation": str(uuid4())}
        stop = 60_000_000_000
        baseline = {
            "capture_offset_ms": 0,
            "horizon_half_width_ms": 27,
            "cleanup": {"exact_output_unit_stopped": True, "capture_null": True, "held_pin_closed": True},
        }
        sequence = {"mode": "sample_offsets", "verified_frames": 192000, "verified_blocks": 200}
        calendars = {
            identifier: {
                "declared_final_origin_ns": 30_000_000_000
                + frozen["common_horizon_ns"]
                + (phase.offset_ms if identifier == group.A else 0) * 1_000_000,
                "alignment": alignment(),
                "frame_continuity": deepcopy(sequence),
                "horizon": {
                    "raw_displacement_ms": 0,
                    "independent_capture_offset_ms": 0,
                    "predeclared_half_width_ms": 27,
                    "corrected_horizon_error_ms": 0,
                },
            }
            for identifier in ((group.B,) if phase.phase == "idle" else (group.A, group.B))
        }
        residue = {**alignment(), "early": alignment(), "late": alignment(), "drift_ms": 0}
        adjusted = {
            "raw_b_minus_a": alignment(-phase.offset_ms),
            "declared_offsets_ms": {group.A: phase.offset_ms, group.B: 0},
            "residual": residue,
        }
        session = str(uuid4())
        rows = []
        for kind in ("cold_idle", "warm_idle") if phase.phase == "idle" else ("native_calendar",):
            row = {
                "kind": kind,
                "offset_ms": phase.offset_ms,
                "epoch_id": phase.epoch_id,
                "passed": True,
                "status": "complete",
            }
            if phase.phase == "idle":
                row.update(marker(session))
            else:
                row.update(
                    calendar=deepcopy(calendars[group.A]),
                    configured_offset_alignment=deepcopy(adjusted),
                    markers={
                        kind: {
                            **marker(session),
                            "unchanged_native_owner": deepcopy(target),
                            "unchanged_item_id": 1,
                        }
                        for kind in ("cold_music", "warm_music")
                    },
                )
            rows.append(row)
        epoch_report = {
            **phase.receipt(),
            "passed": True,
            "frozen_plan": frozen,
            "rows": rows,
            "common_presentation_ns": 30_000_000_000,
            "independent_capture_baselines": {key: deepcopy(baseline) for key in (group.A, group.B)},
            "native_calendars": calendars,
            "configured_offset_alignment": adjusted,
            "target_original": {"source_owner": target, "player": {"item_id": 1}},
            "idle_peer": {"session_id": session},
            "music_peer_cleanup_errors": [],
            "untouched": {
                "failure": None,
                "original_capture_preserved": True,
                "source_owner": source,
                "original_unit_identities": {"owntone": {"invocation_id": str(uuid4())}},
                "control_samples": [{}, {}, {}],
                "pcm": {"failed_block": None, "untouched_reference_blocks": 200},
            },
            "stop_boundary": {"requested_monotonic_ns": stop},
            "producer_retirement": {
                key: {
                    "identity": {"invocation_id": str(uuid4())},
                    "retired": True,
                    "requested_monotonic_ns": stop + 1,
                    "verified_monotonic_ns": stop + 2,
                }
                for key in ((group.B,) if phase.phase == "idle" else (group.A, group.B))
            },
            "daemon_retirement": {
                "retired": True,
                "empty_ownership_verified": True,
                "requested_monotonic_ns": stop + 3,
                "verified_monotonic_ns": stop + 4,
                "units": {group.A: {}, group.B: {}},
            },
            "tail": {
                "both_captures_null": True,
                "pre_stop_pcm_verified": True,
                "frame_sequences_verified": True,
                "teardown_labeled": True,
                "captures": {
                    key: {
                        "null_verified_monotonic_ns": stop + 3,
                        "pcm": {"failed_block": None, "stop_requested_monotonic_ns": stop},
                        "capture": {
                            "capture_dropped_bytes": 0,
                            "observed_bytes": 768000,
                            "frame_continuity": deepcopy(sequence),
                        },
                        "artifact": {"path": f"/private/{phase.epoch_id}/{key}.pcm"},
                    }
                    for key in (group.A, group.B)
                },
            },
        }
        epoch_report.update(
            speech_latency_budget={"encoder_worker_overhead_ms": None, "idle_startup_overhead_ms": None},
            measured_signal_timeline_cleanup_passed=True,
            speech_latency_performance_passed=False,
            speech_latency_performance_status="pending_declared_and_characterized_software_budget",
            cold_utterance_completeness_passed=False,
            cold_utterance_completeness_status="pending_finite_opus_prefix_tail_reference",
        )
        epoch_report["speech_latency_measurements"] = epoch.latency_measurements(epoch_report)
        result.append(
            {
                "mode": "latency_probe",
                "passed": True,
                "cleanup_errors": [],
                "cleanup": {"empty_manifest": True},
                "native_lab": {"actual_profile": "synthetic-unit-test-facts"},
                "latency_epoch": epoch_report,
            }
        )
    return result, phases


def test_strict_aggregate_retains_all_nine_rows_actual_horizons_and_music_subcases():
    actual, expected = reports()
    result = epoch.aggregate_epoch_reports(actual, expected)
    assert result["passed"] is True and len(result["rows"]) == 9
    assert {row["actual_horizon_ns"] for row in result["rows"]} == {140_000_000, 2_140_000_000}
    assert len(result["epochs"]) == 6
    assert result["physical_speakers_verified"] is False


def test_ten_second_coded_runs_pass_integrity_without_disguising_speech_performance():
    actual, expected = reports()
    for report in actual:
        retained = report["latency_epoch"]
        for row in retained["rows"]:
            markers = row["markers"].values() if row["kind"] == "native_calendar" else [row]
            for item in markers:
                item["first_matching_absolute_pts_ns"] = item["trigger_monotonic_ns"] + 10_000_000_000
                item["encoder_to_final_pts_seconds"] = 10.0
                item["encoder_to_final_callback_seconds"] = 10.001
        retained["speech_latency_measurements"] = epoch.latency_measurements(retained)
    result = epoch.aggregate_epoch_reports(actual, expected)
    assert result["passed"] is result["measured_signal_timeline_cleanup_passed"] is True
    assert result["speech_latency_performance_passed"] is False
    assert result["speech_latency_performance_status"] == "pending_declared_and_characterized_software_budget"
    assert result["cold_utterance_completeness_passed"] is False
    assert result["cold_utterance_completeness_status"] == "pending_finite_opus_prefix_tail_reference"
    measurements = [item for report in result["epochs"] for item in report["latency_epoch"]["speech_latency_measurements"]]
    assert len(result["rows"]) == 9 and len(measurements) == 12
    assert all(item["encoder_to_qualifying_run_pts_ms"] == 10000 for item in measurements)
    assert all(item["performance_passed"] is False and item["performance_maximum_ms"] is None for item in measurements)
    assert all(item["encoder_worker_overhead_budget_ms"] is None and item["idle_startup_overhead_budget_ms"] is None
               for item in measurements)


@pytest.mark.parametrize("fault", ["performance", "status", "budget", "idle_budget", "measurement", "capture_bracket",
                                    "integrity", "cold_complete", "cold_status"])
def test_measurement_aggregate_rejects_invented_performance_or_cold_completeness_claims(fault):
    actual, expected = reports()
    selected = actual[0]["latency_epoch"]
    if fault == "performance":
        selected["speech_latency_performance_passed"] = True
    elif fault == "status":
        selected["speech_latency_performance_status"] = "passed"
    elif fault == "budget":
        selected["speech_latency_budget"]["encoder_worker_overhead_ms"] = 1
    elif fault == "idle_budget":
        selected["speech_latency_budget"]["idle_startup_overhead_ms"] = 1
    elif fault == "measurement":
        selected["speech_latency_measurements"][0]["known_buffer_plus_offset_ms"] += 1
    elif fault == "capture_bracket":
        selected["speech_latency_measurements"][0]["independent_capture_half_width_ms"] += 1
    elif fault == "integrity":
        selected["measured_signal_timeline_cleanup_passed"] = False
    elif fault == "cold_complete":
        selected["cold_utterance_completeness_passed"] = True
    else:
        selected["cold_utterance_completeness_status"] = "passed"
    with pytest.raises(RuntimeFailure):
        epoch.aggregate_epoch_reports(actual, expected)


@pytest.mark.parametrize(
    "fault",
    [
        "missing_phase",
        "duplicate_phase",
        "pending",
        "row_id",
        "row_offset",
        "h",
        "b",
        "missing_music",
        "new_peer",
        "duck",
        "restore",
        "onset",
        "earlier_bias",
        "code",
        "source_reuse",
        "unit_reuse",
        "artifact_reuse",
        "cleanup",
        "tail",
        "tail_error",
        "frame_loss",
        "producer_live",
        "daemon_live",
        "stop_relabel",
        "profile",
        "calendar",
        "calendar_signal",
        "capture_bias",
        "raw_offset",
        "residual",
        "early",
        "late",
        "drift",
        "new_item",
    ],
)
def test_aggregate_rejects_incomplete_relabeled_faulted_or_reused_epochs(fault):
    actual, expected = reports()
    selected = actual[1]["latency_epoch"]
    row = selected["rows"][0]
    warm = row["markers"]["warm_music"]
    if fault == "missing_phase":
        actual.pop()
    elif fault == "duplicate_phase":
        actual[-1] = deepcopy(actual[0])
    elif fault == "pending":
        row["status"] = "pending"
    elif fault == "row_id":
        row["epoch_id"] = expected[0].epoch_id
    elif fault == "row_offset":
        row["offset_ms"] = 0
    elif fault == "h":
        selected["frozen_plan"]["common_horizon_ns"] = 1000
    elif fault == "b":
        selected["frozen_plan"]["output_buffers_ms"][group.A] = 500
    elif fault == "missing_music":
        row["markers"].pop("cold_music")
    elif fault == "new_peer":
        warm["session_id"] = str(uuid4())
    elif fault == "duck":
        warm["duck_and_voice"]["passed"] = False
    elif fault == "restore":
        warm["following_restore"]["passed"] = False
    elif fault == "onset":
        warm["first_matching_absolute_pts_ns"] = None
    elif fault == "earlier_bias":
        warm["first_matching_absolute_pts_ns"] = 10_050_000_000
    elif fault == "code":
        warm["coded_120ms_levels"] = ["high"]
    elif fault == "source_reuse":
        selected["untouched"]["source_owner"] = deepcopy(
            actual[0]["latency_epoch"]["untouched"]["source_owner"]
        )
    elif fault == "unit_reuse":
        selected["untouched"]["original_unit_identities"] = deepcopy(
            actual[0]["latency_epoch"]["untouched"]["original_unit_identities"]
        )
    elif fault == "artifact_reuse":
        selected["tail"]["captures"][group.A]["artifact"] = deepcopy(
            actual[0]["latency_epoch"]["tail"]["captures"][group.A]["artifact"]
        )
    elif fault == "cleanup":
        actual[1]["cleanup"]["empty_manifest"] = False
    elif fault == "tail":
        selected["tail"]["both_captures_null"] = False
    elif fault == "tail_error":
        selected["tail"]["captures"][group.B]["pcm"]["failed_block"] = {"index": 10}
    elif fault == "frame_loss":
        selected["tail"]["captures"][group.B]["capture"]["observed_bytes"] += 4
    elif fault == "producer_live":
        selected["producer_retirement"][group.B]["retired"] = False
    elif fault == "daemon_live":
        selected["daemon_retirement"]["retired"] = False
    elif fault == "stop_relabel":
        selected["stop_boundary"]["requested_monotonic_ns"] = 70_000_000_000
    elif fault == "profile":
        actual[1]["native_lab"] = {"different": True}
    elif fault == "calendar":
        selected["native_calendars"][group.A]["declared_final_origin_ns"] += 1_000_000_000
    elif fault == "calendar_signal":
        selected["native_calendars"][group.A]["alignment"]["correlation"] = 0.9
    elif fault == "capture_bias":
        selected["native_calendars"][group.A]["horizon"]["independent_capture_offset_ms"] = 100
    elif fault == "raw_offset":
        selected["configured_offset_alignment"]["raw_b_minus_a"]["relative_offset_ms"] = 0
    elif fault in {"residual", "early", "late"}:
        residue = selected["configured_offset_alignment"]["residual"]
        (residue if fault == "residual" else residue[fault])["relative_offset_ms"] = 3
    elif fault == "drift":
        selected["configured_offset_alignment"]["residual"]["drift_ms"] = 3
    else:
        warm["unchanged_item_id"] = 2
    with pytest.raises((RuntimeFailure, ValueError)):
        epoch.aggregate_epoch_reports(actual, expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_final_worker", [False, True])
async def test_real_prepare_refreshes_both_actors_after_final_plan_before_any_source_or_pcm(
    monkeypatch, bad_final_worker
):
    calls = []

    def state(room):
        manifest = {"card_index": 4, "device": 1 if room.id == group.A else 0, "subdevice": 7}

        async def outputs(_available):
            return [{"id": "0", "selected": True, "offset_ms": room.speakers[0].offset_ms}]

        return SimpleNamespace(
            desired=room,
            status="running",
            processes={"owntone": object()},
            selected_ids=["0"],
            client=SimpleNamespace(outputs=outputs),
            local_pin=SimpleNamespace(manifest=manifest, validate=lambda: None),
        )

    original = {room.id: state(room) for room in rooms(0)}
    broker = SimpleNamespace(
        rooms=dict(original), network=SimpleNamespace(manifest={"processes": {group.A + ":owntone": {}}})
    )

    class Api:
        async def patch(self, identifier, changes):
            calls.append(("patch", identifier, changes))
            if not changes["enabled"]:
                value = broker.rooms[identifier]
                value.desired = value.desired.model_copy(update={"enabled": False})
                value.status, value.processes, value.local_pin = "stopped", {}, None
                broker.network.manifest["processes"] = {}
            else:
                broker.rooms = {room.id: state(room) for room in rooms(-2000)}

        async def room(self, identifier):
            room = broker.rooms[identifier].desired.model_dump(mode="json")
            return {**room, "runtime": {"status": broker.rooms[identifier].status}}

        async def request(self, method, path, *, json):
            calls.append(("offset", method, path, json))
            assert not broker.rooms[group.A].desired.enabled
            assert json["offset_ms"] == -2000
            return {"runtime_accepted": True}

    async def eventually(predicate, _description, *, timeout):
        assert timeout in (30, 65)
        result = await predicate()
        assert result is not None
        return result

    async def rpc(identifier, method, _params, *, timeout):
        assert method == "health" and timeout == 2
        result = healths([value.desired for value in broker.rooms.values()])[identifier]
        if bad_final_worker and identifier == group.B:
            result["timing_relay_delay_ms"] = 1000
        return result

    broker._worker_socket = lambda value: value.desired.id
    monkeypatch.setattr(epoch, "call_rpc", rpc)
    context = SimpleNamespace(
        phase=epoch.Phase(str(uuid4()), -2000, "native"),
        group=SimpleNamespace(
            NATIVE_LAB=object(),
            ZONES=group.ZONES,
            observation=SimpleNamespace(base=SimpleNamespace(eventually=eventually)),
        ),
        latency=SimpleNamespace(playback_closed=lambda pin: calls.append(("closed", pin))),
        api=Api(),
        broker=broker,
        states=dict(original),
        captures={},
        producers={},
        report={"independent_capture_baselines": {group.A: {}, group.B: {}}},
    )
    if bad_final_worker:
        with pytest.raises(RuntimeFailure, match="Actual epoch worker H/B"):
            await epoch.prepare(context)
        assert not hasattr(context, "frozen_plan")
    else:
        await epoch.prepare(context)
        assert all(context.states[key] is not original[key] for key in (group.A, group.B))
        assert context.frozen_plan["common_horizon_ns"] == 2_140_000_000
        assert context.frozen_plan["output_buffers_ms"] == {group.A: 2040, group.B: 40}
        assert context.report["latency_epoch"]["passed"] is False
    assert context.captures == context.producers == {}
    assert [item[0] for item in calls] == ["patch", "closed", "offset", "patch"]
