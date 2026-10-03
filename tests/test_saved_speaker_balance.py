"""Durable physical trims stay independent from the one room master."""
import sqlite3
import pytest
from pydantic import ValidationError
from shiri.domain import Conflict, RoomCreate, RoomPatch, SpeakerRef, ValidationIssue
from shiri.store import SCHEMA_VERSION, Store, _SCHEMA_V2
from shiri.calibration import fingerprint

def speaker(**changes):
    return SpeakerRef(id="101", name="Speaker", protocol="airplay2", **changes)

def test_trim_survives_phone_mute_restart_removal_and_room_reassignment(tmp_path):
    path = tmp_path / "state.sqlite3"
    with Store(path) as store:
        room = store.create_room(RoomCreate(name="Kitchen", interface="eth0"))
        room = store.assign_speakers(room.id, [speaker(offset_ms=17)], room.revision)
        room = store.update_speaker_balance(room.id, "101", 60, room.revision)
        assert room.volume == 50
        for index, volume in enumerate((15, 0, 85, 100, 1)):
            room, accepted, _ = store.apply_phone_volume(room.id, volume, room.revision, f"phone-{index}")
            assert accepted and room.volume == volume and room.speakers[0].balance_percent == 60
        room = store.assign_speakers(room.id, [], room.revision)
        store.delete_room(room.id, room.revision)
    with Store(path) as store:
        room = store.create_room(RoomCreate(name="Office", interface="eth0"))
        room = store.assign_speakers(room.id, [speaker()], room.revision)
        assert room.volume == 50
        assert room.speakers[0].balance_percent == 60 and room.speakers[0].offset_ms == 17
        room = store.assign_speakers(room.id, [], room.revision)
        room = store.assign_speakers(room.id, [speaker(balance_percent=100)], room.revision)
        assert room.speakers[0].balance_percent == 100

def test_balance_stale_revision_and_invalid_numbers_do_not_change_saved_intent(tmp_path):
    with Store(tmp_path / "state.sqlite3") as store:
        room = store.create_room(RoomCreate(name="Kitchen", interface="eth0"))
        room = store.assign_speakers(room.id, [speaker()], room.revision)
        with pytest.raises(Conflict):
            store.update_speaker_balance(room.id, "101", 70, room.revision - 1)
        for invalid in (True, "80", -1, 101, 0.5):
            with pytest.raises(ValidationIssue):
                store.update_speaker_balance(room.id, "101", invalid, room.revision)
            with pytest.raises(ValidationError):
                speaker(balance_percent=invalid)
        assert store.get_room(room.id) == room
        events = store.list_events()
        assert store.update_speaker_balance(room.id, "101", 100, room.revision) == room
        assert store.list_events() == events

@pytest.mark.parametrize("version", [1, 2])
def test_actual_old_database_migrates_without_changing_master_revision_or_profile(tmp_path, version):
    path = tmp_path / "state.sqlite3"
    with Store(path) as store:
        room = store.create_room(RoomCreate(name="Kitchen", interface="eth0"))
        room = store.assign_speakers(room.id, [speaker(offset_ms=23)], room.revision)
        room = store.update_room(room.id, RoomPatch(volume=27), room.revision)
        events = store.list_events()
        store._connection.execute("DROP TABLE speaker_airplay_timing")
        store._connection.execute("DROP TABLE speaker_balances")
        if version == 1:
            store._connection.execute("DROP TABLE calibration_sessions")
        store._connection.execute(f"PRAGMA user_version={version}")
    with Store(path) as store:
        assert store.get_room(room.id) == room
        assert store.list_events() == events
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        room = store.update_speaker_balance(room.id, "101", 65, room.revision)
        assert room.volume == 27 and room.speakers[0].offset_ms == 23

def test_corrupt_old_database_is_preserved_before_balance_migration(tmp_path):
    path = tmp_path / "state.sqlite3"
    with sqlite3.connect(path) as connection:
        for statement in _SCHEMA_V2:
            connection.execute(statement)
        connection.execute("PRAGMA user_version=2")
        connection.execute("INSERT INTO speaker_profiles VALUES ('bogus', '101', 0)")
    before = path.read_bytes()
    with pytest.raises(ValidationIssue):
        Store(path)
    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='speaker_balances'").fetchone() is None

def test_default_balance_preserves_old_calibration_configuration_and_trim_changes_it():
    from shiri.domain import Room
    room = Room(id="d794c12e-9f9f-40f8-b41d-79ad9c3f3e4a", slot=0, name="Kitchen", airplay_name="Kitchen", interface="eth0", speakers=[speaker()])
    old = room.model_dump(mode="json", exclude={"revision", "enabled", "volume"})
    old["speakers"][0].pop("balance_percent")
    old["speakers"][0].pop("airplay_timing")
    assert fingerprint(room) == old
    changed = room.model_copy(update={"speakers": [speaker(balance_percent=65)]})
    assert fingerprint(changed)["speakers"][0]["balance_percent"] == 65

def test_local_trim_tracks_the_physical_card_and_survives_card_replacement(tmp_path):
    with Store(tmp_path / "state.sqlite3") as store:
        room = store.create_room(RoomCreate(name="Kitchen", interface="eth0", local_audio_device="hw:CARD=SpeakerA"))
        room = store.assign_speakers(room.id, [SpeakerRef(id="0", name="Local speaker", protocol="alsa")], room.revision)
        room = store.update_speaker_balance(room.id, "0", 65, room.revision)
        room = store.update_room(room.id, RoomPatch(local_audio_device="hw:CARD=SpeakerB"), room.revision)
        assert room.volume == 50 and room.speakers[0].balance_percent == 100
        room = store.update_speaker_balance(room.id, "0", 80, room.revision)
        room = store.update_room(room.id, RoomPatch(local_audio_device="hw:CARD=SpeakerA"), room.revision)
        assert room.volume == 50 and room.speakers[0].balance_percent == 65
