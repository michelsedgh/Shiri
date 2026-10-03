"""AirPlay timing is durable speaker intent, independent of room gain and discovery."""

import sqlite3

import httpx
import pytest
from pydantic import ValidationError

from shiri.api import create_app
from shiri.calibration import fingerprint
from shiri.domain import Conflict, RoomCreate, RoomPatch, SpeakerRef, ValidationIssue
from shiri.rpc import RpcError
from shiri.runtime_port import SimulatedRuntime
from shiri.settings import Settings
from shiri.store import SCHEMA_VERSION, Store


def speaker(**changes):
    return SpeakerRef.model_validate({"id": "101", "name": "Speaker", "protocol": "airplay2", **changes})


@pytest.mark.parametrize("invalid", [None, True, 1, [], {}, "PTP", "", "receiver-clock"])
def test_timing_mode_is_an_exact_enum(invalid):
    with pytest.raises(ValidationError):
        speaker(airplay_timing=invalid)


@pytest.mark.parametrize("protocol", ["airplay1", "chromecast", "alsa", "pulseaudio"])
@pytest.mark.parametrize("mode", ["ptp", "ntp"])
def test_explicit_native_timing_rejects_other_protocols(protocol, mode):
    with pytest.raises(ValidationError, match="AirPlay 2"):
        speaker(protocol=protocol, airplay_timing=mode)
    assert speaker(protocol=protocol).airplay_timing == "auto"


def test_timing_survives_restart_room_reassignment_and_preserves_other_settings(tmp_path):
    path = tmp_path / "state.sqlite3"
    with Store(path) as store:
        first = store.create_room(RoomCreate(name="Kitchen", interface="eth0"))
        first = store.update_room(first.id, RoomPatch(volume=27), first.revision)
        first = store.assign_speakers(first.id, [speaker(offset_ms=17, balance_percent=65)], first.revision)
        saved_revision = first.revision
        first = store.update_speaker_airplay_timing(first.id, "101", "ntp", first.revision)
        assert first.revision == saved_revision + 1
        assert first.volume == 27
        assert first.speakers[0].offset_ms == 17 and first.speakers[0].balance_percent == 65
        events = store.list_events()
        assert store.update_speaker_airplay_timing(first.id, "101", "ntp", first.revision) == first
        assert store.list_events() == events
        with pytest.raises(Conflict):
            store.update_speaker_airplay_timing(first.id, "101", "ptp", saved_revision)
    with Store(path) as store:
        assert store.get_room(first.id) == first
        first = store.assign_speakers(first.id, [], first.revision)
        second = store.create_room(RoomCreate(name="Office", interface="eth0"))
        second = store.assign_speakers(second.id, [speaker(name="Renamed speaker")], second.revision)
        assert second.speakers[0].airplay_timing == "ntp"
        assert second.speakers[0].offset_ms == 17 and second.speakers[0].balance_percent == 65
        second = store.update_speaker_airplay_timing(second.id, "101", "auto", second.revision)
        second = store.assign_speakers(second.id, [], second.revision)
        second = store.assign_speakers(second.id, [speaker()], second.revision)
        assert second.speakers[0].airplay_timing == "auto"
        assert store.get_room(first.id).volume == 27


def test_retained_timing_does_not_apply_to_other_output_protocols(tmp_path):
    with Store(tmp_path / "state.sqlite3") as store:
        room = store.create_room(RoomCreate(name="Kitchen", interface="eth0"))
        room = store.assign_speakers(room.id, [speaker(airplay_timing="ntp")], room.revision)
        room = store.assign_speakers(room.id, [], room.revision)
        room = store.assign_speakers(room.id, [speaker(protocol="chromecast")], room.revision)
        assert room.speakers[0].airplay_timing == "auto"
        with pytest.raises(ValidationIssue, match="AirPlay 2"):
            store.update_speaker_airplay_timing(room.id, "101", "ntp", room.revision)
        room = store.assign_speakers(room.id, [], room.revision)
        room = store.assign_speakers(room.id, [speaker()], room.revision)
        assert room.speakers[0].airplay_timing == "ntp"


@pytest.mark.parametrize("invalid", [None, True, 1, [], {}, "PTP", "unknown"])
def test_store_rejects_invalid_modes_without_revising_saved_intent(tmp_path, invalid):
    with Store(tmp_path / "state.sqlite3") as store:
        room = store.create_room(RoomCreate(name="Kitchen", interface="eth0"))
        room = store.assign_speakers(room.id, [speaker()], room.revision)
        events = store.list_events()
        with pytest.raises(ValidationIssue):
            store.update_speaker_airplay_timing(room.id, "101", invalid, room.revision)
        assert store.get_room(room.id) == room
        assert store.list_events() == events


@pytest.mark.parametrize("version", [1, 2, 3])
def test_old_schema_migration_preserves_room_revision_profiles_and_events(tmp_path, version):
    path = tmp_path / "state.sqlite3"
    with Store(path) as store:
        room = store.create_room(RoomCreate(name="Kitchen", interface="eth0"))
        room = store.assign_speakers(room.id, [speaker(offset_ms=31)], room.revision)
        room = store.update_room(room.id, RoomPatch(volume=27), room.revision)
        if version == 3:
            room = store.update_speaker_balance(room.id, "101", 65, room.revision)
        events = store.list_events()
        store._connection.execute("DROP TABLE speaker_airplay_timing")
        if version < 3:
            store._connection.execute("DROP TABLE speaker_balances")
        if version < 2:
            store._connection.execute("DROP TABLE calibration_sessions")
        store._connection.execute(f"PRAGMA user_version={version}")
    with Store(path) as store:
        assert store.get_room(room.id) == room
        assert store.list_events() == events
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        store.initialize()
        assert store.get_room(room.id).speakers[0].airplay_timing == "auto"


@pytest.mark.parametrize("corruption", ["mode", "identity", "orphan"])
def test_corrupt_timing_profile_is_rejected_without_rewriting_database(tmp_path, corruption):
    path = tmp_path / "state.sqlite3"
    with Store(path) as store:
        room = store.create_room(RoomCreate(name="Kitchen", interface="eth0"))
        store.assign_speakers(room.id, [speaker(airplay_timing="ntp")], room.revision)
        store._connection.execute("PRAGMA ignore_check_constraints=ON")
        if corruption == "mode":
            store._connection.execute("UPDATE speaker_airplay_timing SET airplay_timing='bogus'")
        elif corruption == "identity":
            store._connection.execute("UPDATE speaker_airplay_timing SET identity_key='0101'")
        else:
            store._connection.execute("INSERT INTO speaker_airplay_timing VALUES ('owntone','999','ntp')")
    before = path.read_bytes()
    with pytest.raises(ValidationIssue, match="preserved"):
        Store(path)
    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_default_timing_preserves_calibration_but_explicit_choice_changes_fingerprint(tmp_path):
    with Store(tmp_path / "state.sqlite3") as store:
        room = store.create_room(RoomCreate(name="Kitchen", interface="eth0"))
        room = store.assign_speakers(room.id, [speaker()], room.revision)
        legacy = room.model_dump(mode="json", exclude={"revision", "enabled", "volume"})
        legacy["speakers"][0].pop("balance_percent")
        legacy["speakers"][0].pop("airplay_timing")
        assert fingerprint(room) == legacy
        changed = store.update_speaker_airplay_timing(room.id, "101", "ntp", room.revision)
        assert fingerprint(changed)["speakers"][0]["airplay_timing"] == "ntp"
        assert fingerprint(changed) != legacy


class PendingRuntime(SimulatedRuntime):
    unavailable = False

    async def call(self, operation, payload=None):
        if operation == "reconcile" and self.unavailable:
            raise RpcError("runtime_unavailable", "Controlled reconciliation failure")
        return await super().call(operation, payload)


async def test_http_timing_revision_validation_discovery_and_pending_intent(tmp_path):
    runtime = PendingRuntime()
    token = "airplay-timing-contract-long-test-token"
    app = create_app(Settings(state_dir=tmp_path), runtime=runtime, token=token)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shiri.test",
                                     headers={"Authorization": f"Bearer {token}"}) as client:
            room = (await client.post("/api/v1/rooms", json={"name": "Timing room", "interface": "sim0"})).json()["room"]
            root = f"/api/v1/rooms/{room['id']}"
            room = (await client.patch(root, json={"expected_revision": room["revision"], "changes": {"enabled": True}})).json()["room"]
            room = (await client.put(root + "/speakers", json={"expected_revision": room["revision"], "speaker_ids": ["101", "202"]})).json()["room"]
            revision, master = room["revision"], room["volume"]
            endpoint = root + "/speakers/101/airplay-timing"
            result = await client.patch(endpoint, json={"expected_revision": revision, "airplay_timing": "ntp"})
            assert result.status_code == 200, result.text
            room = result.json()["room"]
            assert room["revision"] == revision + 1 and room["volume"] == master
            assert room["speakers"][0]["airplay_timing"] == "ntp"
            assert (await client.patch(endpoint, json={"expected_revision": revision, "airplay_timing": "ptp"})).status_code == 409
            for invalid in [None, True, 1, [], {}, "PTP", "", "receiver-clock"]:
                result = await client.patch(endpoint, json={"expected_revision": room["revision"], "airplay_timing": invalid})
                assert result.status_code == 422, result.text
            assert (await client.patch(root + "/speakers/202/airplay-timing", json={"expected_revision": room["revision"], "airplay_timing": "ntp"})).status_code == 400
            assert (await client.patch(root + "/speakers/999/airplay-timing", json={"expected_revision": room["revision"], "airplay_timing": "ntp"})).status_code == 404
            discovery = (await client.get(root + "/speakers")).json()
            assert discovery["room_id"] == room["id"]
            discovered = discovery["outputs"]
            assert next(item for item in discovered if item["id"] == "101")["requested_airplay_timing"] == "ntp"
            assert next(item for item in discovered if item["id"] == "202")["requested_airplay_timing"] is None
            room = (await client.put(root + "/speakers", json={"expected_revision": room["revision"], "speaker_ids": []})).json()["room"]
            room = (await client.put(root + "/speakers", json={"expected_revision": room["revision"], "speaker_ids": ["101"]})).json()["room"]
            assert room["speakers"][0]["airplay_timing"] == "ntp"
            runtime.unavailable = True
            result = await client.patch(endpoint, json={"expected_revision": room["revision"], "airplay_timing": "ptp"})
            assert result.status_code == 200, result.text
            assert result.json()["runtime_accepted"] is False
            assert result.json()["room"]["speakers"][0]["airplay_timing"] == "ptp"
            assert app.state.service.store.get_room(room["id"]).speakers[0].airplay_timing == "ptp"
    finally:
        app.state.service.store.close()
