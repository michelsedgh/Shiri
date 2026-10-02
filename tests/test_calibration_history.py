"""Durable evidence/receipt tests, independent of NumPy and physical hardware.

Known marker summaries exercise transaction/restart guards. Waveform analysis
and its independent recorded-PCM fixtures live in test_calibration.py.
"""
import asyncio
import builtins
from copy import deepcopy
import hashlib
import json
import secrets
import sqlite3
import time

import httpx
import pytest

from shiri.api import create_app
from shiri.calibration import (BURST_SECONDS, INTERVAL, MARKERS, PROBE_SECONDS, RATE,
                               START, CalibrationSession, fingerprint, summary)
from shiri.domain import Conflict, NotFound, RoomCreate, RoomPatch, SpeakerRef, ValidationIssue
from shiri.runtime_port import SimulatedRuntime
from shiri.settings import Settings
from shiri.store import SCHEMA_VERSION, Store

TOKEN = "durable-calibration-admin-" * 3


def outputs(store):
    room = store.create_room(RoomCreate(name="Kitchen", interface="sim0"))
    return store.assign_speakers(room.id, [SpeakerRef(id="101", name="Reference", protocol="airplay2"),
                                         SpeakerRef(id="202", name="Target", protocol="chromecast")], room.revision)


def session(store, room):
    entry = CalibrationSession(secrets.token_urlsafe(24), room.id, "202", "101", room.revision,
                               fingerprint(room), room.speakers[1].offset_ms, "Known shared ADC",
                               "Synthetic equal distances", 500, 0.0)
    entry.result = summary([], entry.previous_offset_ms)
    entry.probe_metadata = {"sample_rate": RATE, "markers": MARKERS, "duration_seconds": PROBE_SECONDS,
                            "seed": entry.seed, "marker_seconds": BURST_SECONDS, "first_marker_seconds": START,
                            "interval_seconds": INTERVAL, "numpy_version": "test-summary-fixture",
                            "sha256": hashlib.sha256(b"fixture only; no waveform supplied").hexdigest()}
    return store.save_calibration(entry.record(), 0, expected_room_revision=room.revision)


def marker_record(take, lag=23):
    digest = hashlib.sha256(f"distinct synthetic capture {take}".encode()).hexdigest()
    return {"sha256": digest, "pcm_sha256": digest, "sample_rate": RATE, "frames": 6 * RATE,
            "duration_seconds": 6.0,
            "markers": [{"marker": index, "startup": index == 0, "accepted": True,
                         "at_seconds": START + index * INTERVAL, "lag_samples": round(lag * RATE / 1000),
                         "lag_ms": lag, "confidence": .99, "reference_polarity": 1, "target_polarity": 1}
                        for index in range(MARKERS)],
            "drift": {"ms_per_minute": 0.0, "uncertainty_ms_per_minute": 60 * 1000 / RATE / 3.9,
                      "span_seconds": 3.9, "residual_mad_ms": 0.0}}


def append(store, record, take):
    changed = deepcopy(record)
    changed["recordings"].append(marker_record(take))
    changed["result"] = summary(changed["recordings"], changed["previous_offset_ms"])
    return store.save_calibration(changed, record["generation"])


def candidate(store, room):
    record = session(store, room)
    for take in range(3):
        record = append(store, record, take)
    assert record["result"]["candidate_offset_ms"] == room.speakers[1].offset_ms - 23
    return record


def test_valid_v1_database_migrates_once_preserving_rooms_profiles_receipts_and_events(tmp_path):
    path = tmp_path / "state.sqlite3"
    with Store(path) as original:
        room = outputs(original)
        room, _, committed = original.apply_phone_volume(room.id, 27, room.revision, "native-volume-1")
        room = original.update_speaker_offset(room.id, "202", 17, room.revision)
        events = original.list_events()
        original._connection.execute("DROP TABLE speaker_balances")
        original._connection.execute("DROP TABLE calibration_sessions")
        original._connection.execute("PRAGMA user_version=1")
    with Store(path) as migrated:
        assert migrated._connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 3
        assert migrated.get_room(room.id) == room
        assert migrated.list_events() == events
        assert migrated.list_calibrations(room.id) == []
        assert migrated._connection.execute("SELECT committed_revision FROM phone_volume_receipts").fetchone()[0] == committed
        assert migrated._connection.execute("SELECT offset_ms FROM speaker_profiles WHERE identity_key='202'").fetchone()[0] == 17
        migrated.initialize()
    with Store(path) as reopened:
        assert reopened.get_room(room.id) == room

def test_v2_calibration_history_migrates_with_exact_evidence_preserved(tmp_path):
    path = tmp_path / "state.sqlite3"
    with Store(path) as store:
        room = outputs(store)
        record = candidate(store, room)
        assert all("balance_percent" not in speaker for speaker in record["configuration"]["speakers"])
        store._connection.execute("DROP TABLE speaker_balances")
        store._connection.execute("PRAGMA user_version=2")
    with Store(path) as store:
        assert store.get_calibration(room.id, record["id"]) == record
        room = store.get_room(room.id)
        assert all(speaker.balance_percent == 100 for speaker in room.speakers)


def test_invalid_v1_intent_is_preserved_before_migration(tmp_path):
    path = tmp_path / "state.sqlite3"
    with Store(path) as store:
        outputs(store)
        store._connection.execute("DROP TABLE calibration_sessions")
        store._connection.execute("DROP TABLE speaker_balances")
        store._connection.execute("PRAGMA user_version=1")
        store._connection.execute("UPDATE rooms SET name_key='noncanonical'")
    before = path.read_bytes()
    with pytest.raises(ValidationIssue, match="preserved"):
        Store(path)
    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name='calibration_sessions'").fetchall()


async def test_authenticated_export_and_exact_rollback_survive_restart_without_numpy(tmp_path, monkeypatch):
    settings = Settings(state_dir=tmp_path)
    with Store(settings.database) as store:
        room = outputs(store)
        record = candidate(store, room)
        changed, record = store.commit_calibration_offset(room.id, record["id"], room.revision, record["generation"])
        assert changed.speakers[1].offset_ms == -23
    original_import = builtins.__import__
    def no_numpy(name, *args, **kwargs):
        if name == "numpy" or name.startswith("numpy."):
            raise ImportError("Optional analysis dependency intentionally absent")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", no_numpy)
    app = create_app(settings, runtime=SimulatedRuntime(), token=TOKEN)
    endpoint = f"/api/v1/rooms/{room.id}/calibration/{record['id']}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shiri.test") as client:
        assert (await client.get(endpoint)).status_code == 401
        client.headers["Authorization"] = "Bearer " + TOKEN
        listed = (await client.get(f"/api/v1/rooms/{room.id}/calibration")).json()["sessions"]
        assert len(listed) == 1 and listed[0]["status"] == "offset_saved"
        exported = (await client.get(endpoint + "/export")).json()
        assert exported["applied_revision"] == changed.revision
        assert exported["probe"]["sha256"] == record["probe_metadata"]["sha256"]
        assert len(exported["recordings"]) == 3 and exported["verification"] is None
        assert not (await client.get("/api/v1/state")).json()["capabilities"]["calibration_analysis"]
        response = await client.post(endpoint + "/rollback", json={"expected_revision": changed.revision,
                                                                   "expected_generation": exported["generation"]})
        assert response.status_code == 200, response.text
        assert response.json()["room"]["speakers"][1]["offset_ms"] == 0
        assert response.json()["calibration"]["status"] == "rolled_back"
        assert (await client.get(endpoint.replace(room.id, "00000000-0000-4000-8000-000000000099"))).status_code == 404
    app.state.service.store.close()
    with Store(settings.database) as reopened:
        assert reopened.get_calibration(room.id, record["id"])["rolled_back"] is True
        assert reopened.get_room(room.id).speakers[1].offset_ms == 0


@pytest.mark.parametrize("rollback", [False, True])
def test_receipt_sql_failure_rolls_back_profile_revision_history_and_event(tmp_path, rollback):
    with Store(tmp_path / "state.sqlite3") as store:
        room = outputs(store)
        record = candidate(store, room)
        if rollback:
            room, record = store.commit_calibration_offset(room.id, record["id"], room.revision, record["generation"])
        profiles = [tuple(row) for row in store._connection.execute("SELECT * FROM speaker_profiles ORDER BY identity_key")]
        events = store.list_events()
        store._connection.execute("CREATE TRIGGER receipt_failure BEFORE UPDATE ON calibration_sessions BEGIN SELECT RAISE(ABORT,'receipt persistence failed'); END")
        with pytest.raises(Conflict):
            store.commit_calibration_offset(room.id, record["id"], room.revision, record["generation"], rollback=rollback)
        store._connection.execute("DROP TRIGGER receipt_failure")
        assert store.get_room(room.id) == room
        assert store.get_calibration(room.id, record["id"]) == record
        assert store.list_events() == events
        assert [tuple(row) for row in store._connection.execute("SELECT * FROM speaker_profiles ORDER BY identity_key")] == profiles


def test_independent_stores_cannot_commit_stale_candidate_or_overwrite_evidence(tmp_path):
    path = tmp_path / "state.sqlite3"
    with Store(path) as first, Store(path) as second:
        room = outputs(first)
        record = candidate(first, room)
        fresh = append(second, record, 3)
        with pytest.raises(Conflict, match="evidence changed"):
            first.commit_calibration_offset(room.id, record["id"], room.revision, record["generation"])
        with pytest.raises(Conflict, match="changed in another"):
            first.save_calibration(record, record["generation"])
        changed, saved = second.commit_calibration_offset(room.id, fresh["id"], room.revision, fresh["generation"])
        with pytest.raises(Conflict, match="current revision"):
            first.commit_calibration_offset(room.id, fresh["id"], room.revision, fresh["generation"])
        assert first.get_room(room.id) == changed
        assert first.get_calibration(room.id, saved["id"]) == saved


async def test_api_requires_exact_reviewed_generation_even_if_room_revision_unchanged(tmp_path):
    settings = Settings(state_dir=tmp_path)
    app = create_app(settings, runtime=SimulatedRuntime(), token=TOKEN)
    store = app.state.service.store
    room = outputs(store)
    record = candidate(store, room)
    append(store, record, 3)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shiri.test",
                                 headers={"Authorization": "Bearer " + TOKEN}) as client:
        endpoint = f"/api/v1/rooms/{room.id}/calibration/{record['id']}/apply"
        stale = await client.post(endpoint, json={"expected_revision": room.revision, "expected_generation": record["generation"]})
        assert stale.status_code == 409 and "evidence changed" in stale.json()["error"]
        missing = await client.post(endpoint, json={"expected_revision": room.revision})
        assert missing.status_code == 422
    assert store.get_room(room.id) == room
    store.close()


def test_retained_receipt_does_not_override_a_new_manual_profile_after_restart(tmp_path):
    path = tmp_path / "state.sqlite3"
    with Store(path) as first:
        room = outputs(first)
        record = candidate(first, room)
        changed, record = first.commit_calibration_offset(room.id, record["id"], room.revision, record["generation"])
        changed = first.update_speaker_offset(room.id, "101", 7, changed.revision)
    with Store(path) as second:
        with pytest.raises(Conflict, match="stale calibration"):
            second.commit_calibration_offset(room.id, record["id"], changed.revision, record["generation"], rollback=True)
        assert second.get_room(room.id) == changed
        assert second.get_calibration(room.id, record["id"])["rolled_back"] is False


def test_active_and_saved_history_are_bounded_and_expiry_does_not_clear_offsets(tmp_path, monkeypatch):
    with Store(tmp_path / "state.sqlite3") as store:
        room = outputs(store)
        records = [session(store, room) for _ in range(16)]
        with pytest.raises(Conflict, match="Too many"):
            session(store, room)
        assert len(store.list_calibrations(room.id)) == 16
        later = time.time() + 3601
        monkeypatch.setattr("shiri.store.time.time", lambda: later)
        assert store.list_calibrations(room.id) == []
        with pytest.raises(NotFound, match="expired"):
            store.get_calibration(room.id, records[0]["id"])
        new = candidate(store, room)
        assert len(store.list_calibrations(room.id)) == 1
        changed, saved = store.commit_calibration_offset(room.id, new["id"], room.revision, new["generation"])
        assert saved["expires_at"] == pytest.approx(later + 90 * 24 * 3600)
        monkeypatch.setattr("shiri.store.time.time", lambda: later + 90 * 24 * 3600 + 1)
        assert store.list_calibrations(room.id) == []
        assert store.get_room(room.id) == changed  # History expiry never changes intent.


def test_history_limit_evicts_oldest_saved_evidence_before_active_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr("shiri.calibration.MAX_HISTORY", 3)
    with Store(tmp_path / "state.sqlite3") as store:
        room = outputs(store)
        retained = []
        for _ in range(2):
            record = candidate(store, room)
            room, record = store.commit_calibration_offset(room.id, record["id"], room.revision, record["generation"])
            room, record = store.commit_calibration_offset(room.id, record["id"], room.revision, record["generation"], rollback=True)
            retained.append(record)
        active = session(store, room)
        newest = session(store, room)
        assert len(store.list_calibrations(room.id)) == 3
        with pytest.raises(NotFound):
            store.get_calibration(room.id, retained[0]["id"])
        assert store.get_calibration(room.id, active["id"])["applied_revision"] is None
        assert store.get_calibration(room.id, newest["id"])["generation"] == 1


@pytest.mark.parametrize("corruption", ["candidate", "identity", "receipt", "raw_pcm", "generation"])
def test_startup_rejects_corrupt_evidence_without_repairing_database(tmp_path, corruption):
    path = tmp_path / "state.sqlite3"
    with Store(path) as store:
        room = outputs(store)
        record = candidate(store, room)
        if corruption == "receipt":
            _room, record = store.commit_calibration_offset(room.id, record["id"], room.revision, record["generation"])
            record["applied_offset_ms"] = 71
        elif corruption == "candidate":
            record["result"]["candidate_offset_ms"] = 71
        elif corruption == "identity":
            record["configuration"]["speakers"][0]["name"] = "Altered reference"
        elif corruption == "raw_pcm":
            record["recordings"][0]["raw_pcm"] = "unexpected hidden audio"
        else:
            record["generation"] += 1
        store._connection.execute("UPDATE calibration_sessions SET evidence=? WHERE id=?", (json.dumps(record), record["id"]))
    # Configuration snapshots need not match a currently edited room, but their
    # fingerprint must be internally well formed. An altered valid display name
    # remains historical data, so use a malformed canonical output ID instead.
    if corruption == "identity":
        with sqlite3.connect(path) as connection:
            record["configuration"]["speakers"][0]["id"] = "0101"
            connection.execute("UPDATE calibration_sessions SET evidence=? WHERE id=?", (json.dumps(record), record["id"]))
    before = path.read_bytes()
    with pytest.raises(ValidationIssue, match="preserved"):
        Store(path)
    assert path.read_bytes() == before


def test_room_deletion_cascades_evidence_and_delete_session_keeps_saved_profile(tmp_path):
    with Store(tmp_path / "state.sqlite3") as store:
        room = outputs(store)
        record = candidate(store, room)
        room, record = store.commit_calibration_offset(room.id, record["id"], room.revision, record["generation"])
        store.delete_calibration(room.id, record["id"])
        assert store.get_room(room.id).speakers[1].offset_ms == -23
        fresh = session(store, room)
        store.delete_room(room.id, room.revision)
        assert store._connection.execute("SELECT count(*) FROM calibration_sessions").fetchone()[0] == 0
        with pytest.raises(NotFound):
            store.get_calibration(room.id, fresh["id"])


async def test_import_started_in_other_service_cannot_resurrect_deleted_session(tmp_path, monkeypatch):
    from shiri.service import RoomService
    with Store(tmp_path / "state.sqlite3") as store:
        room = outputs(store)
        record = session(store, room)
        service = RoomService(store, SimulatedRuntime())
        entered, release = asyncio.Event(), asyncio.Event()
        original = service._store
        async def pause_save(method, *args, **kwargs):
            if method == "save_calibration":
                entered.set()
                await release.wait()
            return await original(method, *args, **kwargs)
        monkeypatch.setattr(service, "_store", pause_save)
        entry = CalibrationSession.from_record(record)
        entry.recordings = [marker_record(0)]
        entry.result = summary(entry.recordings, entry.previous_offset_ms)
        work = asyncio.create_task(service._save_calibration(entry))
        await entered.wait()
        store.delete_calibration(room.id, record["id"])
        release.set()
        with pytest.raises(Conflict, match="removed or expired"):
            await work
        assert store.list_calibrations(room.id) == []


def cross_summary(store):
    target = store.create_room(RoomCreate(name="Bluetooth target", interface="sim0", local_audio_device="hw:CARD=Target,DEV=0"))
    reference = store.create_room(RoomCreate(name="Wi-Fi reference", interface="sim0", local_audio_device="hw:CARD=Reference,DEV=0"))
    target = store.assign_speakers(target.id, [SpeakerRef(id="0", name="Target", protocol="alsa")], target.revision)
    reference = store.assign_speakers(reference.id, [SpeakerRef(id="0", name="Reference", protocol="alsa")], reference.revision)
    entry = CalibrationSession(secrets.token_urlsafe(24), target.id, "0", "0", target.revision,
                               fingerprint(target), 0, "Known shared ADC", "Synthetic equal distances", 500, 0.0,
                               reference_room_id=reference.id, reference_revision=reference.revision,
                               reference_configuration=fingerprint(reference), playback_context="Declared iPhone group: both zones, common music probe")
    entry.result = summary([], 0)
    entry.probe_metadata = {"sample_rate": RATE, "markers": MARKERS, "duration_seconds": PROBE_SECONDS,
                            "seed": entry.seed, "marker_seconds": BURST_SECONDS, "first_marker_seconds": START,
                            "interval_seconds": INTERVAL, "numpy_version": "test-summary-fixture",
                            "sha256": hashlib.sha256(b"cross-zone fixture summary only").hexdigest()}
    record = store.save_calibration(entry.record(), 0, expected_room_revision=target.revision,
                                    expected_reference_revision=reference.revision)
    for take in range(3):
        record = append(store, record, take)
    return target, reference, record


def test_legacy_same_room_evidence_normalizes_on_read_without_rewriting_json(tmp_path):
    path = tmp_path / "state.sqlite3"
    with Store(path) as store:
        room = outputs(store)
        record = candidate(store, room)
        legacy = {key: value for key, value in record.items() if key not in {
            "reference_room_id", "reference_revision", "reference_configuration", "playback_context"}}
        encoded = json.dumps(legacy, separators=(",", ":"))
        store._connection.execute("UPDATE calibration_sessions SET evidence=? WHERE id=?", (encoded, record["id"]))
    with Store(path) as reopened:
        normalized = reopened.get_calibration(room.id, record["id"])
        assert normalized["reference_room_id"] == room.id
        assert normalized["reference_revision"] == room.revision
        assert normalized["reference_configuration"] == fingerprint(room)
        assert reopened._connection.execute("SELECT evidence FROM calibration_sessions WHERE id=?", (record["id"],)).fetchone()[0] == encoded
        changed, receipt = reopened.commit_calibration_offset(room.id, record["id"], room.revision, record["generation"])
        assert changed.speakers[1].offset_ms == -23 and receipt["reference_room_id"] == room.id


async def test_cross_zone_receipt_survives_restart_and_changes_only_exact_target_local_profile(tmp_path):
    settings = Settings(state_dir=tmp_path)
    with Store(settings.database) as store:
        target, reference, record = cross_summary(store)
        changed, record = store.commit_calibration_offset(target.id, record["id"], target.revision, record["generation"],
                                                         expected_reference_revision=reference.revision)
        profiles = {row["identity_key"]: row["offset_ms"] for row in store._connection.execute("SELECT * FROM speaker_profiles")}
        assert profiles == {"hw:CARD=Target,DEV=0,SUBDEV=0": -23, "hw:CARD=Reference,DEV=0,SUBDEV=0": 0}
    app = create_app(settings, runtime=SimulatedRuntime(), token=TOKEN)
    endpoint = f"/api/v1/rooms/{target.id}/calibration/{record['id']}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shiri.test",
                                 headers={"Authorization": "Bearer " + TOKEN}) as client:
        exported = (await client.get(endpoint + "/export")).json()
        assert exported["reference_configuration"] == fingerprint(reference)
        assert exported["playback_context"] == record["playback_context"]
        restored = await client.post(endpoint + "/rollback", json={"expected_revision": changed.revision,
            "expected_generation": record["generation"], "expected_reference_revision": reference.revision})
        assert restored.status_code == 200, restored.text
        assert restored.json()["room"]["speakers"][0]["offset_ms"] == 0
        assert app.state.service.store.get_room(reference.id) == reference
    app.state.service.store.close()


@pytest.mark.parametrize("rollback", [False, True])
def test_cross_zone_fresh_reference_revision_cannot_override_changed_reference_profile(tmp_path, rollback):
    path = tmp_path / "state.sqlite3"
    with Store(path) as first, Store(path) as second:
        target, reference, record = cross_summary(first)
        if rollback:
            target, record = first.commit_calibration_offset(target.id, record["id"], target.revision, record["generation"],
                                                             expected_reference_revision=reference.revision)
        reference = second.update_speaker_offset(reference.id, "0", 9, reference.revision)
        with pytest.raises(Conflict, match="Reference.*stale"):
            first.commit_calibration_offset(target.id, record["id"], target.revision, record["generation"],
                                             rollback=rollback, expected_reference_revision=reference.revision)
        assert first.get_room(target.id) == target
        assert first.get_calibration(target.id, record["id"]) == record


def test_reference_deletion_preserves_durable_evidence_and_prevents_future_rollback(tmp_path):
    path = tmp_path / "state.sqlite3"
    with Store(path) as first:
        target, reference, record = cross_summary(first)
        target, record = first.commit_calibration_offset(target.id, record["id"], target.revision, record["generation"],
                                                         expected_reference_revision=reference.revision)
        first.delete_room(reference.id, reference.revision)
    with Store(path) as reopened:
        assert reopened.get_calibration(target.id, record["id"]) == record
        with pytest.raises(Conflict, match="reference room was removed"):
            reopened.commit_calibration_offset(target.id, record["id"], target.revision, record["generation"],
                                                rollback=True, expected_reference_revision=reference.revision)
        assert reopened.get_room(target.id) == target


def test_cross_zone_import_commit_rechecks_both_room_revisions_and_fingerprints(tmp_path):
    with Store(tmp_path / "state.sqlite3") as store:
        target, reference, record = cross_summary(store)
        altered = deepcopy(record)
        altered["recordings"].append(marker_record(3))
        altered["result"] = summary(altered["recordings"], 0)
        changed = store.update_room(reference.id, RoomPatch(volume=71), reference.revision)
        with pytest.raises(Conflict, match="current revision"):
            store.save_calibration(altered, record["generation"], expected_room_revision=target.revision,
                                    expected_reference_revision=reference.revision)
        saved = store.save_calibration(altered, record["generation"], expected_room_revision=target.revision,
                                       expected_reference_revision=changed.revision)
        assert len(saved["recordings"]) == 4
        changed = store.update_room(reference.id, RoomPatch(name="Changed reference setup"), changed.revision)
        altered = deepcopy(saved)
        altered["recordings"].append(marker_record(4))
        altered["result"] = summary(altered["recordings"], 0)
        with pytest.raises(Conflict, match="Reference.*stale"):
            store.save_calibration(altered, saved["generation"], expected_room_revision=target.revision,
                                    expected_reference_revision=changed.revision)
        assert store.get_calibration(target.id, saved["id"]) == saved


def test_cached_zero_drift_cannot_suppress_a_marker_measured_trend(tmp_path):
    with Store(tmp_path / "state.sqlite3") as store:
        room = outputs(store)
        record = candidate(store, room)
        altered = deepcopy(record)
        for capture in altered["recordings"]:
            for marker in capture["markers"]:
                marker["lag_samples"] += marker["marker"] * 8
                marker["lag_ms"] = marker["lag_samples"] * 1000 / RATE
        altered["result"] = summary(altered["recordings"], 0)
        assert altered["result"]["status"] == "candidate_correction", "A trusted cached zero trend would hide the rejection"
        with pytest.raises(ValidationIssue, match="preserved"):
            store.save_calibration(altered, record["generation"])
        assert store.get_calibration(room.id, record["id"]) == record
