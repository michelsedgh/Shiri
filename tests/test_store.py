from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pytest

from shiri.domain import Conflict, NotFound, RoomCreate, RoomPatch, SpeakerRef, ValidationIssue
from shiri.migration import import_legacy, plan_legacy
from shiri.store import SCHEMA_VERSION, Store


@pytest.fixture
def store(tmp_path):
    database = Store(tmp_path / "rooms.sqlite3")
    yield database
    database.close()


def create(store, name="Kitchen", **kwargs):
    return store.create_room(RoomCreate(name=name, interface="eth0", **kwargs))


def output(output_id="1", protocol="airplay2", **kwargs):
    return SpeakerRef(id=output_id, name=f"Speaker {output_id}", protocol=protocol, **kwargs)


def assert_sqlite_full(error):
    # Python 3.10 exposes the precise SQLite message, while 3.11+ additionally
    # attaches the numeric result code. Both verify genuine storage exhaustion.
    code = getattr(error, "sqlite_errorcode", None)
    if code is None:
        assert str(error) == "database or disk is full"
    else:
        assert code == getattr(sqlite3, "SQLITE_FULL", 13)


def test_persisted_rooms_are_disabled_by_default_and_slots_are_reused(store):
    first = create(store)
    assert first.enabled is False and first.slot == 0 and first.airplay_name == first.name
    second = create(store, "Bedroom")
    store.delete_room(first.id, first.revision)
    replacement = create(store, "Office")
    assert replacement.slot == first.slot
    assert second.slot == 1


def test_database_has_durable_transactions_foreign_keys_and_schema_version(store):
    connection = store._connection
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert Path(store.path).stat().st_mode & 0o777 == 0o600


def test_names_and_external_bindings_have_independent_explicit_uniqueness(store):
    first = create(store, "Straße", airplay_name="Receiver", nobly_room_id="Living/UPPER")
    for options in ({"name": "STRASSE"}, {"name": "Other", "airplay_name": "receiver"},
                    {"name": "Other", "nobly_room_id": "Living/UPPER"}):
        with pytest.raises(Conflict):
            create(store, **options)
    other = create(store, "Other", nobly_room_id="living/upper")
    assert other.nobly_room_id != first.nobly_room_id


def test_invalid_revisions_and_stale_updates_cannot_overwrite(store):
    first = create(store)
    changed = store.update_room(first.id, RoomPatch(volume=70), first.revision)
    with pytest.raises(Conflict):
        store.update_room(first.id, RoomPatch(volume=20), first.revision)
    for revision in (True, "2", 0):
        with pytest.raises(ValidationIssue):
            store.update_room(first.id, RoomPatch(volume=20), revision)
    assert store.get_room(first.id) == changed


def test_empty_patch_checks_revision_without_creating_an_extra_write(store):
    first = create(store)
    assert store.update_room(first.id, RoomPatch(), first.revision).revision == 1
    assert len(store.list_events()) == 1


def test_returned_speaker_list_is_a_snapshot_not_an_unsaved_database_mutation(store):
    room = create(store)
    room = store.assign_speakers(room.id, [output()], room.revision)
    room.speakers.clear()
    assert store.get_room(room.id).speakers == [output()]


def test_room_snapshots_batch_profiles_and_preserve_room_speaker_order(store):
    expected = []
    for index in range(8):
        room = create(store, f"Room {index}", local_audio_device=f"hw:CARD=Speaker{index}")
        speakers = [output(str(index * 10 + 3), airplay_timing="ntp", balance_percent=63, offset_ms=-17),
                    output("0", "alsa", balance_percent=29),
                    output(str(index * 10 + 2), "chromecast", offset_ms=51)]
        expected.append(store.assign_speakers(room.id, speakers, room.revision))
    statements = []
    store._connection.set_trace_callback(statements.append)
    try:
        assert store.list_rooms() == expected
        reads = [statement for statement in statements if statement.lstrip().upper().startswith(("SELECT", "PRAGMA"))]
        assert len(reads) == 2  # Bounded independently of rooms and speaker count.
        statements.clear()
        assert store.get_room(expected[3].id) == expected[3]
        reads = [statement for statement in statements if statement.lstrip().upper().startswith(("SELECT", "PRAGMA"))]
        assert len(reads) == 2
    finally:
        store._connection.set_trace_callback(None)


def test_batched_room_and_profile_reads_share_one_sqlite_snapshot(store):
    room = create(store)
    room = store.assign_speakers(room.id, [output(balance_percent=23)], room.revision)
    changed = []
    with Store(store.path) as other:
        def between_reads(statement):
            if "FROM room_speakers AS s" in statement and not changed:
                changed.append(other.update_speaker_balance(room.id, "1", 89, room.revision))
        store._connection.set_trace_callback(between_reads)
        try:
            assert store.list_rooms() == [room]
        finally:
            store._connection.set_trace_callback(None)
        assert store.list_rooms() == changed


def test_resolve_nobly_uses_exact_binding_and_includes_disabled_room_profiles(store):
    room = create(store, nobly_room_id="Kitchen/UPPER")
    room = store.assign_speakers(room.id, [output(balance_percent=42, airplay_timing="ptp")], room.revision)
    assert store.resolve_nobly("Kitchen/UPPER") == room
    for value in ("kitchen/upper", "Kitchen/UPPER ", "missing"):
        with pytest.raises(NotFound):
            store.resolve_nobly(value)


def test_enabled_network_conflict_rolls_back_settings_revision_and_event(store):
    first, second = create(store), create(store, "Bedroom")
    first = store.update_room(first.id, RoomPatch(enabled=True), first.revision)
    second = store.update_room(second.id, RoomPatch(interface="eth1"), second.revision)
    events = store.list_events()
    with pytest.raises(Conflict, match="same LAN interface"):
        store.update_room(second.id, RoomPatch(enabled=True, volume=91), second.revision)
    assert store.get_room(second.id) == second
    assert store.list_events() == events
    first = store.update_room(first.id, RoomPatch(enabled=False), first.revision)
    assert store.update_room(second.id, RoomPatch(enabled=True), second.revision).enabled


def test_independent_stores_cannot_enable_conflicting_networks(tmp_path):
    path = tmp_path / "lan.sqlite3"
    with Store(path) as first, Store(path) as second:
        rooms = [first.create_room(RoomCreate(name=f"Room {index}", interface=f"eth{index}")) for index in range(2)]
        barrier = threading.Barrier(2)
        def enable(store, room):
            barrier.wait()
            try:
                return store.update_room(room.id, RoomPatch(enabled=True), room.revision)
            except Conflict:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(enable, store, room) for store, room in zip((first, second), rooms, strict=True)]
            assert sum(future.result() is not None for future in futures) == 1
        assert sum(room.enabled for room in first.list_rooms()) == 1


def test_old_conflicting_networks_can_be_opened_and_disabled_for_recovery(tmp_path):
    path = tmp_path / "old-lan.sqlite3"
    with Store(path) as store:
        rooms = [store.create_room(RoomCreate(name=f"Room {index}", interface=f"eth{index}")) for index in range(2)]
        store._connection.execute("UPDATE rooms SET enabled=1")
    with Store(path) as store:
        assert all(room.enabled for room in store.list_rooms())
        assert not store.update_room(rooms[1].id, RoomPatch(enabled=False), rooms[1].revision).enabled


def test_failed_name_change_rolls_back_revision_and_audit_event(store):
    kitchen = create(store)
    bedroom = create(store, "Bedroom")
    events = store.list_events()
    with pytest.raises(Conflict):
        store.update_room(bedroom.id, RoomPatch(name="Kitchen", volume=99), bedroom.revision)
    assert store.get_room(bedroom.id) == bedroom
    assert store.get_room(kitchen.id) == kitchen
    assert store.list_events() == events


def test_speakers_remain_exclusive_while_rooms_are_disabled(store):
    first, second = create(store), create(store, "Bedroom")
    first = store.assign_speakers(first.id, [output()], first.revision)
    assert first.enabled is False
    with pytest.raises(Conflict):
        store.assign_speakers(second.id, [output(protocol="airplay1")], second.revision)
    with pytest.raises(Conflict):
        store.assign_speakers(second.id, [output(protocol="chromecast")], second.revision)
    assert store.get_room(second.id).speakers == []


def test_local_id_zero_can_route_to_distinct_cards_but_not_share_one_card(store):
    first = create(store, local_audio_device="hw:CARD=SpeakerA")
    second = create(store, "Bedroom", local_audio_device="hw:CARD=SpeakerB")
    with pytest.raises(Conflict):
        create(store, "Office", local_audio_device="hw:CARD=SpeakerA")
    first = store.assign_speakers(first.id, [output("0", "alsa")], first.revision)
    second = store.assign_speakers(second.id, [output("0", "alsa")], second.revision)
    with pytest.raises(Conflict):
        store.update_room(second.id, RoomPatch(local_audio_device="hw:CARD=SpeakerA"), second.revision)
    assert store.get_room(second.id) == second
    assert store.get_room(first.id) == first


def test_unconfigured_local_assignment_is_rejected_without_database_changes(store):
    room = create(store)
    with pytest.raises(ValidationIssue):
        store.assign_speakers(room.id, [output("0", "alsa")], room.revision)
    assert store.get_room(room.id) == room


@pytest.mark.parametrize("first_device,second_device", [
    ("hw:CARD=SpeakerA", "plughw:SpeakerA,0,0"),
    ("bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp", "bluealsa:DEV=aa:bb:cc:dd:ee:ff,PROFILE=a2dp"),
])
def test_local_route_aliases_cannot_bypass_exclusive_ownership(store, first_device, second_device):
    first = create(store, local_audio_device=first_device)
    second = create(store, "Bedroom")
    with pytest.raises(Conflict):
        create(store, "Office", local_audio_device=second_device)
    with pytest.raises(Conflict):
        store.update_room(second.id, RoomPatch(local_audio_device=second_device), second.revision)
    assert store.get_room(first.id) == first
    assert store.get_room(second.id) == second


def test_clearing_or_deleting_configured_device_releases_its_reservation(store):
    first = create(store, local_audio_device="hw:CARD=SpeakerA")
    first = store.update_room(first.id, RoomPatch(local_audio_device=None), first.revision)
    second = create(store, "Bedroom", local_audio_device="plughw:SpeakerA")
    store.delete_room(second.id, second.revision)
    first = store.update_room(first.id, RoomPatch(local_audio_device="hw:SpeakerA,0,0"), first.revision)
    assert first.local_audio_device == "hw:SpeakerA,0,0"


def test_concurrent_configured_device_reservation_has_one_winner(tmp_path):
    path = tmp_path / "shared.sqlite3"
    with Store(path) as first, Store(path) as second:
        barrier = threading.Barrier(2)
        def reserve(database, name, device):
            barrier.wait()
            try:
                create(database, name, local_audio_device=device)
                return True
            except Conflict:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda data: reserve(*data), [
                (first, "Kitchen", "hw:CARD=SpeakerA"), (second, "Bedroom", "plughw:SpeakerA,0,0"),
            ]))
        assert sorted(results) == [False, True]
        assert len(first.list_rooms()) == 1


def test_local_calibration_belongs_to_the_physical_card(store):
    room = create(store, local_audio_device="hw:CARD=SpeakerA")
    room = store.assign_speakers(room.id, [output("0", "alsa")], room.revision)
    room = store.update_speaker_offset(room.id, "0", 100, room.revision)
    room = store.update_room(room.id, RoomPatch(local_audio_device="hw:CARD=SpeakerB"), room.revision)
    assert room.speakers[0].offset_ms == 0
    room = store.update_room(room.id, RoomPatch(local_audio_device="hw:CARD=SpeakerA"), room.revision)
    assert room.speakers[0].offset_ms == 100


def test_partial_speaker_reassignment_is_fully_rolled_back_on_conflict(store):
    first, second = create(store), create(store, "Bedroom")
    first = store.assign_speakers(first.id, [output("1")], first.revision)
    second = store.assign_speakers(second.id, [output("2")], second.revision)
    with pytest.raises(Conflict):
        store.assign_speakers(second.id, [output("3"), output("1")], second.revision)
    assert store.get_room(second.id) == second
    assert store.get_room(first.id) == first
    third = create(store, "Office")
    assert store.assign_speakers(third.id, [output("3")], third.revision).speakers == [output("3")]


def test_capacity_and_enabled_deletion_are_explicit_conflicts(tmp_path):
    with Store(tmp_path / "limited.sqlite3", max_rooms=1) as database:
        room = create(database)
        with pytest.raises(Conflict):
            create(database, "Bedroom")
        room = database.update_room(room.id, RoomPatch(enabled=True), room.revision)
        with pytest.raises(Conflict):
            database.delete_room(room.id, room.revision)
        room = database.update_room(room.id, RoomPatch(enabled=False), room.revision)
        database.delete_room(room.id, room.revision)
        with pytest.raises(NotFound):
            database.get_room(room.id)


def test_concurrent_store_instances_cannot_both_update_the_same_revision(tmp_path):
    path = tmp_path / "shared.sqlite3"
    with Store(path) as first, Store(path) as second:
        room = create(first)
        barrier = threading.Barrier(2)
        def change(database, volume):
            barrier.wait()
            try:
                return database.update_room(room.id, RoomPatch(volume=volume), room.revision).volume
            except Conflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda pair: change(*pair), [(first, 65), (second, 35)]))
        assert results.count("conflict") == 1
        current = first.get_room(room.id)
        assert current.revision == 2 and current.volume in {35, 65}


def test_concurrent_speaker_ownership_has_only_one_successful_writer(tmp_path):
    with Store(tmp_path / "shared.sqlite3") as first, Store(tmp_path / "shared.sqlite3") as second:
        rooms = [create(first), create(first, "Bedroom")]
        barrier = threading.Barrier(2)
        def assign(database, room):
            barrier.wait()
            try:
                database.assign_speakers(room.id, [output()], room.revision)
                return True
            except Conflict:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda pair: assign(*pair), [(first, rooms[0]), (second, rooms[1])]))
        assert sorted(results) == [False, True]
        assert sum(bool(room.speakers) for room in first.list_rooms()) == 1


def test_committed_configuration_survives_process_exit_without_cleanup(tmp_path):
    path = tmp_path / "crash.sqlite3"
    script = "from shiri.store import Store; from shiri.domain import RoomCreate; import os,sys; s=Store(sys.argv[1]); s.create_room(RoomCreate(name='Kitchen',interface='eth0')); os._exit(0)"
    subprocess.run([sys.executable, "-c", script, str(path)], check=True, timeout=10)
    with Store(path) as database:
        room = database.list_rooms()[0]
        assert room.name == "Kitchen" and room.revision == 1
    script = "from shiri.store import Store; import os,sys; s=Store(sys.argv[1]); s._connection.execute('BEGIN IMMEDIATE'); s._connection.execute('UPDATE rooms SET volume=99'); os._exit(0)"
    subprocess.run([sys.executable, "-c", script, str(path)], check=True, timeout=10)
    with Store(path) as database:
        assert database.get_room(room.id).volume == 50


def test_real_sqlite_full_failure_rolls_back_configuration_and_event(store):
    room = create(store)
    pages = store._connection.execute("PRAGMA page_count").fetchone()[0]
    store._connection.execute(f"PRAGMA max_page_count={pages}")
    for length in (2000, 1):
        for _ in range(1000):
            try:
                store.record_event("x", "x" * length)
            except sqlite3.OperationalError as exc:
                assert_sqlite_full(exc)
                break
        else:
            pytest.fail("Expected a bounded SQLite full-disk failure")
    with pytest.raises(sqlite3.OperationalError) as failure:
        store.update_room(room.id, RoomPatch(volume=51), room.revision)
    assert_sqlite_full(failure.value)
    assert store.get_room(room.id) == room
    assert all(event.kind != "room.updated" for event in store.list_events(1000))


def test_phone_receipt_replay_keeps_original_commit_after_newer_ui_intent(store):
    original = create(store)
    phone, accepted, committed = store.apply_phone_volume(original.id, 25, original.revision, "phone-one")
    assert accepted and committed == phone.revision == original.revision + 1
    newer = store.update_room(phone.id, RoomPatch(volume=90), phone.revision)
    replay, accepted, committed = store.apply_phone_volume(original.id, 25, original.revision, "phone-one")
    assert accepted and replay == newer and committed == phone.revision
    assert replay.volume == 90 and replay.revision > committed
    # The next coalesced phone event must rebase on its actual predecessor,
    # not on the later UI edit returned alongside the replay acknowledgment.
    current, accepted, committed = store.apply_phone_volume(original.id, 30, committed, "phone-two")
    assert not accepted and current == newer and committed == newer.revision
    assert store._connection.execute("SELECT count(*) FROM phone_volume_receipts").fetchone()[0] == 1
    assert sum(event.kind == "room.phone_volume" for event in store.list_events()) == 1


def test_phone_receipt_and_original_acknowledgment_survive_restart(store):
    original = create(store)
    phone, _, committed = store.apply_phone_volume(original.id, 0, original.revision, "muted-phone")
    newer = store.update_room(phone.id, RoomPatch(volume=100), phone.revision)
    path = store.path
    store.close()
    with Store(path) as reopened:
        current, accepted, replay_revision = reopened.apply_phone_volume(original.id, 0, original.revision, "muted-phone")
        assert accepted and current == newer and replay_revision == committed


def test_committed_phone_receipt_survives_abrupt_process_exit(tmp_path):
    path = tmp_path / "phone-crash.sqlite3"
    script = (
        "from shiri.store import Store; from shiri.domain import RoomCreate; import os,sys; "
        "s=Store(sys.argv[1]); r=s.create_room(RoomCreate(name='Kitchen',interface='eth0')); "
        "s.apply_phone_volume(r.id,25,r.revision,'lost-ack-before-exit'); os._exit(0)"
    )
    subprocess.run([sys.executable, "-c", script, str(path)], check=True, timeout=10)
    with Store(path) as reopened:
        phone = reopened.list_rooms()[0]
        assert phone.volume == 25 and phone.revision == 2
        newer = reopened.update_room(phone.id, RoomPatch(volume=80), phone.revision)
        replay, accepted, committed = reopened.apply_phone_volume(phone.id, 25, 1, "lost-ack-before-exit")
        assert accepted and replay == newer and committed == 2


def test_stale_phone_event_is_rejected_without_receipt_or_state_changes(store):
    original = create(store)
    newer = store.update_room(original.id, RoomPatch(volume=80), original.revision)
    events = store.list_events()
    current, accepted, committed = store.apply_phone_volume(original.id, 80, original.revision, "late-phone")
    assert not accepted and current == newer and committed == newer.revision
    assert store.list_events() == events
    assert store._connection.execute("SELECT count(*) FROM phone_volume_receipts").fetchone()[0] == 0


def test_phone_event_id_collision_cannot_change_another_room_or_volume(store):
    first, second = create(store), create(store, "Bedroom")
    phone, _, _ = store.apply_phone_volume(first.id, 25, first.revision, "unique-phone")
    events = store.list_events()
    for room, volume in [(phone, 26), (second, 25)]:
        with pytest.raises(Conflict, match="event id"):
            store.apply_phone_volume(room.id, volume, room.revision, "unique-phone")
    assert store.get_room(first.id) == phone and store.get_room(second.id) == second
    assert store.list_events() == events


def test_same_volume_phone_event_still_establishes_a_causal_revision(store):
    original = create(store)
    phone, accepted, committed = store.apply_phone_volume(original.id, original.volume, original.revision, "same-volume")
    assert accepted and committed == phone.revision == original.revision + 1
    with pytest.raises(Conflict):
        store.update_room(original.id, RoomPatch(volume=20), original.revision)


@pytest.mark.parametrize("volume,event_id", [(True, "x"), (25.0, "x"), (-1, "x"), (101, "x"), (25, ""), (25, " x"), (25, "x\n"), (25, "x" * 129)])
def test_phone_event_inputs_are_strict_and_bounded(store, volume, event_id):
    room = create(store)
    with pytest.raises(ValidationIssue):
        store.apply_phone_volume(room.id, volume, room.revision, event_id)
    assert store.get_room(room.id) == room


def test_concurrent_duplicate_phone_events_commit_once(tmp_path):
    path = tmp_path / "phone.sqlite3"
    with Store(path) as first, Store(path) as second:
        original = create(first)
        barrier = threading.Barrier(2)
        def apply(database):
            barrier.wait()
            return database.apply_phone_volume(original.id, 25, original.revision, "retry-same-event")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(apply, [first, second]))
        assert all(accepted and committed == 2 and room.revision == 2 for room, accepted, committed in results)
        assert first._connection.execute("SELECT count(*) FROM phone_volume_receipts").fetchone()[0] == 1
        assert sum(event.kind == "room.phone_volume" for event in first.list_events()) == 1


def test_concurrent_distinct_phone_events_cannot_overwrite_same_revision(tmp_path):
    path = tmp_path / "phone.sqlite3"
    with Store(path) as first, Store(path) as second:
        original = create(first)
        barrier = threading.Barrier(2)
        def apply(database, volume, identity):
            barrier.wait()
            return database.apply_phone_volume(original.id, volume, original.revision, identity)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda args: apply(*args), [(first, 25, "one"), (second, 75, "two")]))
        assert sorted(accepted for _, accepted, _ in results) == [False, True]
        assert first.get_room(original.id).revision == 2
        assert first._connection.execute("SELECT count(*) FROM phone_volume_receipts").fetchone()[0] == 1


def test_phone_receipt_full_storage_rolls_back_receipt_room_and_event(store):
    original = create(store)
    events = store.list_events()
    # Exhaust SQLite's page budget specifically during receipt insertion,
    # after the room update and event insert have already executed.
    store._connection.execute("CREATE TABLE receipt_storage_pressure(payload BLOB)")
    store._connection.execute("CREATE TRIGGER receipt_pressure BEFORE INSERT ON phone_volume_receipts BEGIN INSERT INTO receipt_storage_pressure VALUES (zeroblob(1048576)); END")
    pages = store._connection.execute("PRAGMA page_count").fetchone()[0]
    store._connection.execute(f"PRAGMA max_page_count={pages}")
    with pytest.raises(sqlite3.OperationalError) as failure:
        store.apply_phone_volume(original.id, 25, original.revision, "full-storage")
    assert_sqlite_full(failure.value)
    assert store.get_room(original.id) == original and store.list_events() == events
    assert store._connection.execute("SELECT count(*) FROM phone_volume_receipts").fetchone()[0] == 0
    assert store._connection.execute("SELECT count(*) FROM receipt_storage_pressure").fetchone()[0] == 0


def test_phone_receipts_are_bounded_and_evicted_replays_remain_stale(store, monkeypatch):
    monkeypatch.setattr("shiri.store.MAX_PHONE_RECEIPTS", 3)
    original = current = create(store)
    for number in range(5):
        current, accepted, committed = store.apply_phone_volume(current.id, number, current.revision, f"event-{number}")
        assert accepted and committed == current.revision
    rows = store._connection.execute("SELECT event_id FROM phone_volume_receipts ORDER BY id").fetchall()
    assert [row[0] for row in rows] == ["event-2", "event-3", "event-4"]
    replay, accepted, committed = store.apply_phone_volume(original.id, 0, original.revision, "event-0")
    assert not accepted and replay == current and committed == current.revision


def test_unknown_schema_is_preserved_without_reinitialization(tmp_path):
    path = tmp_path / "unknown.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE important(value TEXT)")
        connection.execute("INSERT INTO important VALUES ('preserve me')")
        connection.execute("PRAGMA user_version=999")
    original = path.read_bytes()
    with pytest.raises(ValidationIssue):
        Store(path)
    assert path.read_bytes() == original
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT value FROM important").fetchone()[0] == "preserve me"


def test_unversioned_nonempty_database_is_never_claimed(tmp_path):
    path = tmp_path / "other.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE other(value TEXT)")
    with pytest.raises(ValidationIssue):
        Store(path)


@pytest.mark.parametrize("corruption", [
    "name_key", "airplay_name_key", "speaker_key", "speaker_position", "speaker_name",
    "local_device_conflict", "profile_family", "profile_key", "profile_offset", "missing_profile",
    "receipt_event", "receipt_room", "receipt_future_revision", "enabled",
])
def test_startup_rejects_logically_corrupt_intent_without_repairing_bytes(tmp_path, corruption):
    path = tmp_path / "corrupt.sqlite3"
    with Store(path) as database:
        first = create(database, local_audio_device="hw:SpeakerA")
        second = create(database, "Bedroom", local_audio_device="hw:SpeakerB")
        first = database.assign_speakers(first.id, [output("100")], first.revision)
        database.apply_phone_volume(first.id, 30, first.revision, "receipt-A")
    connection = sqlite3.connect(path)
    try:
        if corruption in {"name_key", "airplay_name_key"}:
            connection.execute(f"UPDATE rooms SET {corruption}=? WHERE id=?", ("inconsistent-key", first.id))
        elif corruption == "speaker_key":
            connection.execute("UPDATE room_speakers SET identity_key='999' WHERE room_id=?", (first.id,))
        elif corruption == "speaker_position":
            connection.execute("UPDATE room_speakers SET position=8 WHERE room_id=?", (first.id,))
        elif corruption == "speaker_name":
            connection.execute("UPDATE room_speakers SET name=' padded speaker ' WHERE room_id=?", (first.id,))
        elif corruption == "local_device_conflict":
            connection.execute("UPDATE rooms SET local_audio_device='plughw:SpeakerA,0,0' WHERE id=?", (second.id,))
        elif corruption == "profile_family":
            connection.execute("UPDATE speaker_profiles SET identity_family='unknown'")
        elif corruption == "profile_key":
            connection.execute("UPDATE speaker_profiles SET identity_key='0100'")
        elif corruption == "profile_offset":
            connection.execute("UPDATE speaker_profiles SET offset_ms=123")
        elif corruption == "missing_profile":
            connection.execute("DELETE FROM speaker_profiles")
        elif corruption == "receipt_event":
            connection.execute("UPDATE phone_volume_receipts SET event_id=' padded event '")
        elif corruption == "receipt_room":
            connection.execute("UPDATE phone_volume_receipts SET room_id='not-a-room-uuid'")
        elif corruption == "receipt_future_revision":
            connection.execute("UPDATE phone_volume_receipts SET committed_revision=1000")
        else:
            connection.execute("PRAGMA ignore_check_constraints=ON")
            connection.execute("UPDATE rooms SET enabled=2 WHERE id=?", (first.id,))
        connection.commit()
    finally:
        connection.close()
    original = path.read_bytes()
    with pytest.raises(ValidationIssue, match="database preserved"):
        Store(path)
    assert path.read_bytes() == original, "Rejected data must not be repaired or reset"


@pytest.mark.parametrize("table,before,after", [
    ("rooms", "name_key TEXT UNIQUE NOT NULL", "name_key TEXT NOT NULL"),
    ("rooms", "name TEXT NOT NULL", "name TEXT"),
    ("rooms", "CHECK (slot >= 0 AND slot < 8)", "CHECK (slot >= 0 AND slot < 9)"),
    ("rooms", "CHECK (enabled IN (0, 1))", "CHECK (enabled IN (0, 1, 2))"),
    ("rooms", "name_key TEXT UNIQUE NOT NULL", "name_key TEXT UNIQUE ON CONFLICT REPLACE NOT NULL"),
    ("room_speakers", "REFERENCES rooms(id) ON DELETE CASCADE", "REFERENCES rooms(id)"),
    ("room_speakers", "UNIQUE (room_id, position)", "CHECK (position >= 0)"),
    ("speaker_profiles", "CHECK (offset_ms >= -2000 AND offset_ms <= 2000)", "CHECK (offset_ms >= -3000 AND offset_ms <= 3000)"),
    ("phone_volume_receipts", "event_id TEXT UNIQUE NOT NULL", "event_id TEXT NOT NULL"),
    ("phone_volume_receipts", "PRIMARY KEY AUTOINCREMENT", "PRIMARY KEY"),
])
def test_startup_rejects_missing_or_changed_mandatory_schema_semantics(tmp_path, table, before, after):
    reference = tmp_path / "reference.sqlite3"
    with Store(reference) as database:
        statements = dict(database._connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall())
    path = tmp_path / "altered.sqlite3"
    connection = sqlite3.connect(path)
    try:
        assert before in statements[table]
        for name, statement in statements.items():
            connection.execute(statement.replace(before, after) if name == table else statement)
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        connection.commit()
    finally:
        connection.close()
    original = path.read_bytes()
    with pytest.raises(ValidationIssue, match="schema or constraints; database preserved"):
        Store(path)
    assert path.read_bytes() == original


def test_schema_audit_accepts_harmless_sql_format_and_extra_nonunique_index(tmp_path):
    reference = tmp_path / "reference.sqlite3"
    with Store(reference) as database:
        statements = [row[0] for row in database._connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )]
    path = tmp_path / "formatted.sqlite3"
    connection = sqlite3.connect(path)
    try:
        for statement in statements:
            statement = statement.replace("CREATE TABLE", "create /* harmless */ table").replace("CHECK", "check")
            statement = statement.replace("INTEGER", "integer").replace("TEXT", "text").replace("REAL", "real")
            statement = statement.replace("duck_gain >= 0", '"duck_gain"  >=  0')
            connection.execute(statement)
        connection.execute("CREATE INDEX history_by_room ON events(room_id)")
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        connection.commit()
    finally:
        connection.close()
    with Store(path) as database:
        first = create(database)
        with pytest.raises(Conflict):
            create(database, "KITCHEN", airplay_name="Different receiver")
        assert database.get_room(first.id).name == "Kitchen"


def test_corrupt_key_rejection_preserves_committed_wal_and_main_database(tmp_path):
    path = tmp_path / "wal.sqlite3"
    with Store(path) as database:
        first = create(database)
        database._connection.execute("UPDATE rooms SET name_key='inconsistent' WHERE id=?", (first.id,))
        wal = Path(str(path) + "-wal")
        assert wal.exists() and wal.stat().st_size > 0
        original_main, original_wal = path.read_bytes(), wal.read_bytes()
        with pytest.raises(ValidationIssue, match="identity keys"):
            Store(path)
        assert path.read_bytes() == original_main and wal.read_bytes() == original_wal


@pytest.mark.parametrize("device", ["hw:7", "plughw:7,0,0", "hw:CARD=7,DEV=1,SUBDEV=2"])
@pytest.mark.parametrize("abrupt_exit", [False, True])
def test_existing_numeric_card_intent_requires_operator_repair_without_rewriting(
    tmp_path, device, abrupt_exit,
):
    path = tmp_path / "numeric-card.sqlite3"
    if abrupt_exit:
        script = (
            "from shiri.store import Store; from shiri.domain import RoomCreate; import os,sys; "
            "s=Store(sys.argv[1]); s.create_room(RoomCreate(name='Kitchen',interface='eth0')); "
            "s._connection.execute('UPDATE rooms SET local_audio_device=?',(sys.argv[2],)); os._exit(0)"
        )
        subprocess.run([sys.executable, "-c", script, str(path), device], check=True)
    else:
        with Store(path) as database:
            create(database)
            database._connection.execute("UPDATE rooms SET local_audio_device=?", (device,))
    durable_files = [path, Path(str(path) + "-wal")]
    original = {file: file.read_bytes() for file in durable_files if file.exists()}
    if abrupt_exit:
        assert len(original) == 2 and original[durable_files[1]]
    for _ in range(2):
        with pytest.raises(ValidationIssue, match="operator repair.*Numeric ALSA CARD.*KitchenDAC.*database preserved"):
            Store(path)
        assert all(file.exists() and file.read_bytes() == data for file, data in original.items())


@pytest.mark.parametrize("corruption", ["UPDATE rooms SET name_key='inconsistent'",
                                        "CREATE TRIGGER unowned AFTER INSERT ON rooms BEGIN SELECT 1; END"])
def test_rejected_crashed_database_keeps_wal_without_another_live_reader(tmp_path, corruption):
    path = tmp_path / "crashed.sqlite3"
    script = (
        "from shiri.store import Store; from shiri.domain import RoomCreate; import os,sys; "
        "s=Store(sys.argv[1]); s.create_room(RoomCreate(name='Kitchen',interface='eth0')); "
        "s._connection.execute(sys.argv[2]); os._exit(0)"
    )
    subprocess.run([sys.executable, "-c", script, str(path), corruption], check=True)
    wal = Path(str(path) + "-wal")
    assert wal.exists() and wal.stat().st_size > 0
    original_main, original_wal = path.read_bytes(), wal.read_bytes()
    with pytest.raises(ValidationIssue, match="database preserved"):
        Store(path)
    assert path.read_bytes() == original_main
    assert wal.exists() and wal.read_bytes() == original_wal, "Rejection must not checkpoint or remove committed WAL"


@pytest.mark.parametrize("main_state", ["empty", "missing"])
def test_missing_or_truncated_crashed_main_never_resets_committed_wal(tmp_path, main_state):
    path = tmp_path / "crashed-main.sqlite3"
    script = (
        "from shiri.store import Store; from shiri.domain import RoomCreate; import os,sys; "
        "s=Store(sys.argv[1]); s.create_room(RoomCreate(name='Preserve me',interface='eth0')); os._exit(0)"
    )
    subprocess.run([sys.executable, "-c", script, str(path)], check=True)
    assert Path(str(path) + "-wal").stat().st_size > 0
    if main_state == "empty":
        path.write_bytes(b"")
    else:
        path.unlink()
    sources = {file.name: file.read_bytes() for file in tmp_path.iterdir()}
    with pytest.raises(ValidationIssue, match="retained SQLite companions"):
        Store(path)
    assert {file.name: file.read_bytes() for file in tmp_path.iterdir()} == sources


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
@pytest.mark.parametrize("main_exists", [False, True])
def test_missing_or_empty_main_with_dangling_companion_is_preserved(tmp_path, suffix, main_exists):
    path = tmp_path / "dangling.sqlite3"
    if main_exists:
        path.touch()
    companion = Path(str(path) + suffix)
    companion.symlink_to(tmp_path / "unavailable-companion")
    with pytest.raises(ValidationIssue, match="retained SQLite companions"):
        Store(path)
    assert path.exists() == main_exists
    if main_exists:
        assert path.read_bytes() == b""
    assert companion.is_symlink() and not companion.exists()


def test_genuinely_new_empty_main_without_companions_can_initialize(tmp_path):
    path = tmp_path / "new.sqlite3"
    path.touch()
    with Store(path) as database:
        assert database.list_rooms() == []
        assert create(database).name == "Kitchen"


@pytest.mark.parametrize("object_sql", [
    "CREATE TABLE extra_configuration(value TEXT)",
    "CREATE TABLE sqlitex_shadow(value TEXT)",
    "CREATE VIEW derived_rooms AS SELECT * FROM rooms",
    "CREATE TRIGGER corrupt_identity AFTER INSERT ON rooms BEGIN UPDATE rooms SET name_key='wrong' WHERE id=NEW.id; END",
])
def test_startup_refuses_unmanaged_objects_that_can_bypass_saved_invariants(tmp_path, object_sql):
    path = tmp_path / "unmanaged.sqlite3"
    with Store(path):
        pass
    connection = sqlite3.connect(path)
    try:
        connection.execute(object_sql)
        connection.commit()
    finally:
        connection.close()
    original = path.read_bytes()
    with pytest.raises(ValidationIssue, match="Unmanaged database"):
        Store(path)
    assert path.read_bytes() == original


def test_receipts_and_events_for_deleted_rooms_remain_valid_history_on_restart(tmp_path):
    path = tmp_path / "history.sqlite3"
    with Store(path) as database:
        first = create(database)
        first, _, _ = database.apply_phone_volume(first.id, 25, first.revision, "old-room-phone")
        database.delete_room(first.id, first.revision)
    with Store(path) as database:
        assert database.list_rooms() == []
        assert [event.room_id for event in database.list_events()] == [first.id] * 3
        assert database._connection.execute("SELECT event_id FROM phone_volume_receipts").fetchone()[0] == "old-room-phone"


def test_calibration_offset_survives_deselection_reselection_and_restart(store):
    room = create(store)
    room = store.assign_speakers(room.id, [output()], room.revision)
    room = store.update_speaker_offset(room.id, "1", 125, room.revision)
    room = store.assign_speakers(room.id, [], room.revision)
    room = store.assign_speakers(room.id, [output()], room.revision)
    assert room.speakers[0].offset_ms == 125
    path = store.path
    store.close()
    with Store(path) as reopened:
        assert reopened.get_room(room.id).speakers[0].offset_ms == 125
        room = reopened.assign_speakers(room.id, [output(offset_ms=0)], room.revision)
        assert room.speakers[0].offset_ms == 0


def test_calibration_changes_require_current_room_revision(store):
    room = create(store)
    assigned = store.assign_speakers(room.id, [output()], room.revision)
    with pytest.raises(Conflict):
        store.update_speaker_offset(room.id, "1", 25, room.revision)
    with pytest.raises(NotFound):
        store.update_speaker_offset(room.id, "999", 25, assigned.revision)


def legacy_file(tmp_path, zones):
    source = tmp_path / "legacy.json"
    source.write_text(json.dumps({"zones": zones, "settings": {"default_interface": "eth0"}}))
    return source


def test_legacy_migration_is_read_only_by_default_and_ids_are_stable(store, tmp_path):
    source = legacy_file(tmp_path, {"old_a": {"name": "Kitchen", "interface": "eth0", "auto_start": True,
        "lionos_room_id": "House/KITCHEN", "speakers": ["1"],
        "speaker_names": [{"id": "1", "name": "Speaker", "type": "AirPlay 2"}]}})
    original = source.read_bytes()
    plan = store.import_legacy(source)
    assert store.list_rooms() == [] and plan.applied is False
    assert plan_legacy(source).rooms[0].room_id == plan.rooms[0].room_id
    applied = store.import_legacy(source, dry_run=False)
    room = store.list_rooms()[0]
    assert applied.applied and not room.enabled and room.nobly_room_id == "House/KITCHEN"
    assert source.read_bytes() == original


def test_migration_warns_instead_of_guessing_missing_protocols(tmp_path):
    source = legacy_file(tmp_path, {"old_a": {"name": "Kitchen", "interface": "eth0", "speakers": ["1"]}})
    plan = import_legacy(source)
    assert plan.warnings and plan.rooms[0].speakers == []


@pytest.mark.parametrize("device", ["hw:7,0", "plughw:CARD=7,DEV=0"])
def test_migration_requires_operator_supplied_named_endpoint(store, tmp_path, device):
    zone = {
        "name": "Kitchen", "interface": "eth0", "local_audio_device": device, "speakers": ["0"],
        "speaker_names": [{"id": "0", "name": "Kitchen DAC", "type": "ALSA"}],
    }
    source = legacy_file(tmp_path, {"old": zone})
    original = source.read_bytes()
    for dry_run in (True, False):
        with pytest.raises(ValidationIssue, match="Numeric ALSA CARD.*stable named CARD"):
            store.import_legacy(source, dry_run=dry_run)
        assert source.read_bytes() == original and not store.list_rooms()
    # Only an operator-supplied named route permits import, using a separate
    # reviewed copy so neither planning nor application edits the source.
    repaired = tmp_path / "operator-reviewed.json"
    zone["local_audio_device"] = "plughw:CARD=KitchenDAC,DEV=0"
    repaired.write_text(json.dumps({"zones": {"old": zone}}))
    repaired_bytes = repaired.read_bytes()
    store.import_legacy(repaired, dry_run=False)
    imported = store.list_rooms()[0]
    assert not imported.enabled and imported.local_audio_device == zone["local_audio_device"]
    assert imported.speakers == [SpeakerRef(id="0", name="Kitchen DAC", protocol="alsa")]
    assert source.read_bytes() == original and repaired.read_bytes() == repaired_bytes


def test_migration_conflict_keeps_entire_destination_unchanged(store, tmp_path):
    create(store, "Already here")
    source = legacy_file(tmp_path, {"old_a": {"name": "Valid first", "interface": "eth0"},
                                    "old_b": {"name": "Already here", "interface": "eth0"}})
    original = source.read_bytes()
    for dry_run in (True, False):
        with pytest.raises(Conflict):
            store.import_legacy(source, dry_run=dry_run)
    assert len(store.list_rooms()) == 1
    assert source.read_bytes() == original


def test_migration_rejects_legacy_cross_room_speaker_claims(tmp_path):
    source = legacy_file(tmp_path, {"old_a": {"name": "Kitchen", "interface": "eth0", "speakers": ["1"]},
                                    "old_b": {"name": "Bedroom", "interface": "eth0", "speakers": ["1"]}})
    with pytest.raises(Conflict):
        plan_legacy(source)


def test_migration_rejects_configured_device_alias_collisions_even_without_speakers(store, tmp_path):
    create(store, local_audio_device="hw:CARD=SpeakerA")
    source = legacy_file(tmp_path, {"old": {"name": "Bedroom", "interface": "eth0", "local_audio_device": "plughw:SpeakerA,0,0"}})
    for dry_run in (True, False):
        with pytest.raises(Conflict, match="local audio device"):
            store.import_legacy(source, dry_run=dry_run)
    duplicate = legacy_file(tmp_path, {
        "one": {"name": "First", "interface": "eth0", "local_audio_device": "hw:SpeakerB"},
        "two": {"name": "Second", "interface": "eth0", "local_audio_device": "plughw:CARD=SpeakerB"},
    })
    with pytest.raises(Conflict, match="local audio device"):
        plan_legacy(duplicate)
    assert len(store.list_rooms()) == 1


def test_closed_store_rejects_operations(store):
    store.close()
    store.close()
    with pytest.raises(ValidationIssue):
        store.list_rooms()
