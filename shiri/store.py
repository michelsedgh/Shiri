"""Transactional room configuration with explicit revisions and speaker ownership.

Each instance serializes its connection. SQLite's write transaction also
serializes independent instances and processes; invariants are backed by unique
constraints, not by an in-memory view of the last request.
"""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator
from datetime import datetime, timezone
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from uuid import UUID, uuid4

from pydantic import ValidationError

from .domain import (
    MAX_ROOMS, AirplayTiming, Conflict, Event, NotFound, Room, RoomCreate, RoomPatch, SpeakerRef,
    ValidationIssue, local_audio_device_key, speaker_key, validate_local_audio_device,
)

SCHEMA_VERSION = 4
MAX_EVENTS = 10_000
MAX_PHONE_RECEIPTS = 10_000
_SCHEMA_V2 = (
    """CREATE TABLE rooms (
        id TEXT PRIMARY KEY NOT NULL,
        slot INTEGER UNIQUE NOT NULL CHECK (slot >= 0 AND slot < 8),
        name TEXT NOT NULL,
        name_key TEXT UNIQUE NOT NULL,
        airplay_name TEXT NOT NULL,
        airplay_name_key TEXT UNIQUE NOT NULL,
        nobly_room_id TEXT UNIQUE,
        interface TEXT NOT NULL,
        local_audio_device TEXT,
        enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
        volume INTEGER NOT NULL CHECK (volume >= 0 AND volume <= 100),
        duck_gain REAL NOT NULL CHECK (duck_gain >= 0 AND duck_gain <= 1),
        revision INTEGER NOT NULL CHECK (revision >= 1)
    )""",
    """CREATE TABLE room_speakers (
        room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
        identity_family TEXT NOT NULL,
        identity_key TEXT NOT NULL,
        output_id TEXT NOT NULL,
        position INTEGER NOT NULL CHECK (position >= 0),
        name TEXT NOT NULL,
        protocol TEXT NOT NULL CHECK (protocol IN ('airplay1', 'airplay2', 'chromecast', 'alsa', 'pulseaudio')),
        offset_ms INTEGER NOT NULL DEFAULT 0 CHECK (offset_ms >= -2000 AND offset_ms <= 2000),
        PRIMARY KEY (identity_family, identity_key),
        UNIQUE (room_id, position)
    )""",
    """CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        message TEXT NOT NULL,
        room_id TEXT,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE speaker_profiles (
        identity_family TEXT NOT NULL,
        identity_key TEXT NOT NULL,
        offset_ms INTEGER NOT NULL CHECK (offset_ms >= -2000 AND offset_ms <= 2000),
        PRIMARY KEY (identity_family, identity_key)
    )""",
    """CREATE TABLE phone_volume_receipts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT UNIQUE NOT NULL,
        room_id TEXT NOT NULL,
        volume INTEGER NOT NULL CHECK (volume >= 0 AND volume <= 100),
        committed_revision INTEGER NOT NULL CHECK (committed_revision >= 1),
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE calibration_sessions (
        id TEXT PRIMARY KEY NOT NULL,
        room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
        generation INTEGER NOT NULL CHECK (generation >= 1),
        created_at REAL NOT NULL CHECK (created_at > 0),
        expires_at REAL NOT NULL CHECK (expires_at >= created_at),
        applied_revision INTEGER CHECK (applied_revision >= 1),
        evidence TEXT NOT NULL CHECK (length(evidence) <= 262144)
    )""",
)

_SCHEMA_V3 = (*_SCHEMA_V2,
    """CREATE TABLE speaker_balances (
        identity_family TEXT NOT NULL,
        identity_key TEXT NOT NULL,
        balance_percent INTEGER NOT NULL CHECK (balance_percent >= 0 AND balance_percent <= 100),
        PRIMARY KEY (identity_family, identity_key)
    )""",
)

_SCHEMA = (*_SCHEMA_V3,
    """CREATE TABLE speaker_airplay_timing (
        identity_family TEXT NOT NULL CHECK (identity_family = 'owntone'),
        identity_key TEXT NOT NULL,
        airplay_timing TEXT NOT NULL CHECK (airplay_timing IN ('auto', 'ptp', 'ntp')),
        PRIMARY KEY (identity_family, identity_key)
    )""",
)


def _sql_tokens(statement: str) -> tuple[str, ...]:
    """Normalize SQL syntax without folding case-sensitive string literals."""
    tokens = re.findall(
        r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^]]*\]|"
        r"/\*[\s\S]*?\*/|--[^\n]*|[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?|<=|>=|<>|!=|==|[^\s]",
        statement,
    )
    normalized = []
    for token in tokens:
        if token.startswith(("/*", "--")):
            continue
        if token.startswith("'"):
            normalized.append(token)
        elif token.startswith(('"', "`", "[")):
            normalized.append(token[1:-1].lower())
        else:
            normalized.append("=" if token == "==" else token.lower())
    return tuple(normalized)


def _unwrap(tokens: tuple[str, ...]) -> tuple[str, ...]:
    while len(tokens) >= 2 and tokens[0] == "(" and tokens[-1] == ")":
        depth = 0
        for index, token in enumerate(tokens):
            depth += (token == "(") - (token == ")")
            if depth == 0 and index != len(tokens) - 1:
                return tokens
        tokens = tokens[1:-1]
    return tokens


def _checks(statement: str) -> frozenset[tuple[str, ...]]:
    tokens, checks = _sql_tokens(statement), set()
    for index, token in enumerate(tokens):
        if token != "check" or index + 1 >= len(tokens) or tokens[index + 1] != "(":
            continue
        depth = 1
        for end in range(index + 2, len(tokens)):
            depth += (tokens[end] == "(") - (tokens[end] == ")")
            if depth == 0:
                checks.add(_unwrap(tokens[index + 2:end]))
                break
    return frozenset(checks)


def _schema_signature(connection: sqlite3.Connection, table: str) -> tuple:
    columns = tuple((row[1], row[2].upper(), row[3],
                     _unwrap(_sql_tokens(row[4])) if row[4] is not None else None, row[5])
                    for row in connection.execute(f"PRAGMA table_info({table})"))
    unique = set()
    for index in connection.execute(f"PRAGMA index_list({table})"):
        if index[2]:
            # Index names are SQLite-generated or harmless implementation detail.
            # Collation, sort direction and partial uniqueness change semantics.
            name = index[1].replace("'", "''")
            key = tuple((row[2], row[3], row[4])
                        for row in connection.execute(f"PRAGMA index_xinfo('{name}')") if row[5])
            unique.add((bool(index[4]), key))
    foreign_keys = tuple(tuple(row)[1:] for row in connection.execute(f"PRAGMA foreign_key_list({table})"))
    sql = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    tokens = _sql_tokens(sql[0]) if sql else ()
    conflict_policies = frozenset(tokens[index + 2] for index in range(len(tokens) - 2)
                                 if tokens[index:index + 2] == ("on", "conflict") and tokens[index + 2] != "abort")
    return columns, frozenset(unique), foreign_keys, _checks(sql[0]) if sql else frozenset(), "autoincrement" in tokens, conflict_policies


@lru_cache(maxsize=4)
def _managed_signatures(version=SCHEMA_VERSION) -> dict[str, tuple]:
    # Use SQLite's interpretation of the managed schema, not brittle DDL text.
    connection = sqlite3.connect(":memory:")
    try:
        schema = {1: _SCHEMA_V2[:-1], 2: _SCHEMA_V2, 3: _SCHEMA_V3, 4: _SCHEMA}[version]
        for statement in schema:
            connection.execute(statement)
        return {statement.split()[2]: _schema_signature(connection, statement.split()[2]) for statement in schema}
    finally:
        connection.close()


class Store:
    def __init__(self, path: str | Path, max_rooms: int = MAX_ROOMS):
        if type(max_rooms) is not int or not 1 <= max_rooms <= MAX_ROOMS:
            raise ValidationIssue("Room capacity must be between 1 and 8")
        self.path = str(path) if str(path) == ":memory:" else str(Path(path).expanduser().resolve())
        self.max_rooms = max_rooms
        self._lock = threading.RLock()
        self._closed = False
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._validate_existing_file()
        self._connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._initialize()
            if self.path != ":memory:":
                os.chmod(self.path, 0o600)
                mode = self._connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                if mode.lower() != "wal":
                    raise ValidationIssue("Database could not enable WAL journaling")
        except BaseException:
            self._connection.close()
            self._closed = True
            raise

    def _validate_existing_file(self) -> None:
        if self.path == ":memory:":
            return
        main = Path(self.path)
        if not main.exists() or main.stat().st_size == 0:
            companions = [Path(self.path + suffix) for suffix in ("-wal", "-shm", "-journal")]
            if any(path.exists() or path.is_symlink() for path in companions):
                raise ValidationIssue("Missing or empty database has retained SQLite companions; all files preserved")
            return
        # A failed last writable connection can checkpoint a crashed database's
        # committed WAL on close. Validate with WAL-aware read-only access first,
        # never immutable mode (which would ignore committed WAL intent).
        connection = sqlite3.connect(Path(self.path).as_uri() + "?mode=ro", uri=True, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1, 2, 3, SCHEMA_VERSION}:
                raise ValidationIssue(f"Unsupported Shiri database schema version {version}; database preserved")
            if version == 0:
                existing = connection.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view','trigger') AND name NOT GLOB 'sqlite_*'").fetchall()
                if existing:
                    raise ValidationIssue("Unversioned nonempty database cannot be initialized; database preserved")
            else:
                self._validate_database(connection, version)
        finally:
            connection.rollback()
            connection.close()

    def _initialize(self) -> None:
        with self._transaction() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1, 2, 3, SCHEMA_VERSION}:
                raise ValidationIssue(f"Unsupported Shiri database schema version {version}; database preserved")
            if version == 0:
                existing = connection.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view','trigger') AND name NOT GLOB 'sqlite_*'").fetchall()
                if existing:
                    raise ValidationIssue("Unversioned nonempty database cannot be initialized; database preserved")
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif version in {1, 2, 3}:
                # Audit the previous managed schema and all saved intent before
                # adding anything. Failed validation never repairs an old DB.
                self._validate_database(connection, version)
                if version == 1:
                    connection.execute(_SCHEMA_V2[-1])
                if version < 3:
                    connection.execute(_SCHEMA_V3[-1])
                connection.execute(_SCHEMA[-1])
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self._validate_database(connection)

    @staticmethod
    def _validate_database(connection: sqlite3.Connection, version=SCHEMA_VERSION) -> None:
        for table, expected in _managed_signatures(version).items():
            if _schema_signature(connection, table) != expected:
                raise ValidationIssue(f"Invalid {table} schema or constraints; database preserved")
        objects = {(row[0], row[1]) for row in connection.execute(
            "SELECT name,type FROM sqlite_master WHERE type IN ('table','view','trigger') "
            "AND NOT (type='table' AND name IN ('sqlite_sequence','sqlite_stat1','sqlite_stat4'))"
        )}
        if objects != {(table, "table") for table in _managed_signatures(version)}:
            raise ValidationIssue("Unmanaged database tables, views or triggers; database preserved")
        check = connection.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok" or connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValidationIssue("Database integrity check failed; database preserved")
        Store._audit_intent(connection)
        if version >= 2:
            from .calibration import MAX_ACTIVE, MAX_HISTORY
            if (connection.execute("SELECT 1 FROM calibration_sessions LIMIT 1 OFFSET ?", (MAX_HISTORY,)).fetchone()
                    or connection.execute("SELECT count(*) FROM calibration_sessions WHERE applied_revision IS NULL").fetchone()[0] > MAX_ACTIVE):
                raise ValidationIssue("Stored calibration history exceeds its bounded capacity; database preserved")
            for row in connection.execute("SELECT * FROM calibration_sessions"):
                record = Store._calibration_record(row)
                room = Store._get_room(connection, record["room_id"])
                if record["room_revision"] > room.revision or record["applied_revision"] is not None and record["applied_revision"] > room.revision:
                    raise ValidationIssue("Stored calibration receipt is newer than its room; database preserved")
                reference = connection.execute("SELECT revision FROM rooms WHERE id=?", (record["reference_room_id"],)).fetchone()
                if reference is not None and record["reference_revision"] > reference["revision"]:
                    raise ValidationIssue("Stored calibration reference is newer than its room; database preserved")

    @staticmethod
    def _audit_intent(connection: sqlite3.Connection) -> None:
        """Reject logical corruption without repairing or rewriting saved intent."""
        rows = connection.execute("SELECT * FROM rooms ORDER BY slot").fetchall()
        if len(rows) > MAX_ROOMS:
            raise ValidationIssue("Stored room capacity is invalid; database preserved")
        names, airplay_names, external_ids, local_devices, outputs = set(), set(), set(), set(), set()
        revisions, assigned_offsets = {}, {}
        for row in rows:
            room = Store._get_room(connection, row["id"])
            revisions[room.id] = room.revision
            if (type(row["enabled"]) is not int or row["enabled"] not in {0, 1}
                    or row["name"] != room.name or row["airplay_name"] != room.airplay_name
                    or row["name_key"] != room.name.casefold() or row["airplay_name_key"] != room.airplay_name.casefold()
                    or room.name.casefold() in names or room.airplay_name.casefold() in airplay_names
                    or room.nobly_room_id is not None and room.nobly_room_id in external_ids):
                raise ValidationIssue("Stored room identity keys are inconsistent; database preserved")
            names.add(room.name.casefold())
            airplay_names.add(room.airplay_name.casefold())
            if room.nobly_room_id is not None:
                external_ids.add(room.nobly_room_id)
            if room.local_audio_device:
                key = local_audio_device_key(room.local_audio_device)
                if key in local_devices:
                    raise ValidationIssue("Stored local audio devices conflict; database preserved")
                local_devices.add(key)
            saved = connection.execute("SELECT * FROM room_speakers WHERE room_id=? ORDER BY position", (room.id,)).fetchall()
            for position, (speaker, output) in enumerate(zip(room.speakers, saved, strict=True)):
                key = speaker_key(speaker, room.local_audio_device)
                if (output["position"] != position or output["name"] != speaker.name
                        or (output["identity_family"], output["identity_key"]) != key or key in outputs):
                    raise ValidationIssue("Stored speaker ownership keys are inconsistent; database preserved")
                outputs.add(key)
                assigned_offsets[key] = speaker.offset_ms
        matched_profiles = set()
        try:
            for profile in connection.execute("SELECT * FROM speaker_profiles"):
                family, key, offset = profile["identity_family"], profile["identity_key"], profile["offset_ms"]
                if family == "owntone":
                    SpeakerRef(id=key, name="Stored profile", protocol="airplay2", offset_ms=offset)
                elif family == "local":
                    if local_audio_device_key(key) != key or type(offset) is not int or not -2000 <= offset <= 2000:
                        raise ValueError("Invalid local profile")
                else:
                    raise ValueError("Unknown profile identity family")
                identity = (family, key)
                if identity in assigned_offsets:
                    if assigned_offsets[identity] != offset:
                        raise ValueError("Assigned offset differs from its retained profile")
                    matched_profiles.add(identity)
        except (ValueError, ValidationError) as exc:
            raise ValidationIssue("Stored speaker profiles are invalid; database preserved") from exc
        if matched_profiles != assigned_offsets.keys():
            raise ValidationIssue("Stored speaker calibration does not match its profile; database preserved")
        if connection.execute("PRAGMA user_version").fetchone()[0] >= 3:
            for balance in connection.execute("SELECT * FROM speaker_balances"):
                if (type(balance["balance_percent"]) is not int or not 0 <= balance["balance_percent"] <= 100
                        or connection.execute("SELECT 1 FROM speaker_profiles WHERE identity_family=? AND identity_key=?",
                                              (balance["identity_family"], balance["identity_key"])).fetchone() is None):
                    raise ValidationIssue("Stored speaker balance is invalid; database preserved")
        if connection.execute("PRAGMA user_version").fetchone()[0] >= 4:
            for timing in connection.execute("SELECT * FROM speaker_airplay_timing"):
                try:
                    if timing["identity_family"] != "owntone":
                        raise ValueError("AirPlay timing requires a network speaker")
                    SpeakerRef(id=timing["identity_key"], name="Stored timing profile", protocol="airplay2",
                               airplay_timing=timing["airplay_timing"])
                    if connection.execute("SELECT 1 FROM speaker_profiles WHERE identity_family=? AND identity_key=?",
                                          (timing["identity_family"], timing["identity_key"])).fetchone() is None:
                        raise ValueError("AirPlay timing has no retained speaker profile")
                except (ValueError, ValidationError) as exc:
                    raise ValidationIssue("Stored AirPlay timing is invalid; database preserved") from exc
        if (connection.execute("SELECT 1 FROM phone_volume_receipts LIMIT 1 OFFSET ?", (MAX_PHONE_RECEIPTS,)).fetchone()
                or connection.execute("SELECT 1 FROM events LIMIT 1 OFFSET ?", (MAX_EVENTS,)).fetchone()):
            raise ValidationIssue("Stored event history exceeds its bounded capacity; database preserved")
        for receipt in connection.execute("SELECT * FROM phone_volume_receipts"):
            event_id = receipt["event_id"]
            try:
                valid_room_id = str(UUID(receipt["room_id"])) == receipt["room_id"]
            except (ValueError, TypeError, AttributeError):
                valid_room_id = False
            if (not valid_room_id or not isinstance(event_id, str) or not 1 <= len(event_id) <= 128
                    or event_id != event_id.strip() or any(ord(char) < 32 or ord(char) == 127 for char in event_id)
                    or type(receipt["volume"]) is not int or not 0 <= receipt["volume"] <= 100
                    or type(receipt["committed_revision"]) is not int or receipt["committed_revision"] < 1
                    or receipt["room_id"] in revisions and receipt["committed_revision"] > revisions[receipt["room_id"]]):
                raise ValidationIssue("Stored phone volume receipt is invalid; database preserved")

    def initialize(self) -> None:
        """Recheck an opened store; initialization is also performed at construction."""
        self._initialize()

    @contextmanager
    def _transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._closed:
                raise ValidationIssue("Room store is closed")
            connection = self._connection
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield connection
                connection.commit()
            except BaseException as exc:
                connection.rollback()
                if isinstance(exc, sqlite3.IntegrityError):
                    raise self._integrity_error(exc) from exc
                raise

    @staticmethod
    def _integrity_error(exc: sqlite3.IntegrityError) -> Conflict:
        text = str(exc)
        if "rooms.name_key" in text:
            return Conflict("A room with this name already exists")
        if "rooms.airplay_name_key" in text:
            return Conflict("An AirPlay receiver with this name already exists")
        if "rooms.nobly_room_id" in text:
            return Conflict("This Nobly room id is already bound to another room")
        if "room_speakers.identity_family" in text:
            return Conflict("A selected speaker is already assigned to another room")
        return Conflict("The proposed configuration conflicts with an existing room or speaker assignment")

    @staticmethod
    def _revision(expected_revision: int) -> None:
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValidationIssue("expected_revision must be a positive integer")

    @staticmethod
    def _get_room(connection: sqlite3.Connection, room_id: str) -> Room:
        row = connection.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
        if row is None:
            raise NotFound("Room not found")
        try:
            validate_local_audio_device(row["local_audio_device"])
        except ValueError as exc:
            raise ValidationIssue(
                f"Stored local audio device requires operator repair: {exc}; database preserved"
            ) from exc
        try:
            speakers = [SpeakerRef(id=output["output_id"], name=output["name"], protocol=output["protocol"], offset_ms=output["offset_ms"],
                                   balance_percent=Store._speaker_balance(connection, (output["identity_family"], output["identity_key"])),
                                   airplay_timing=Store._speaker_airplay_timing(connection, (output["identity_family"], output["identity_key"]), output["protocol"]))
                        for output in connection.execute("SELECT * FROM room_speakers WHERE room_id=? ORDER BY position", (room_id,)).fetchall()]
            return Room(
                id=row["id"], slot=row["slot"], name=row["name"], airplay_name=row["airplay_name"],
                nobly_room_id=row["nobly_room_id"], interface=row["interface"], local_audio_device=row["local_audio_device"],
                enabled=bool(row["enabled"]), volume=row["volume"], duck_gain=float(row["duck_gain"]),
                speakers=speakers, revision=row["revision"],
            )
        except ValidationError as exc:
            raise ValidationIssue("Stored room data failed validation; database preserved") from exc

    @classmethod
    def _current(cls, connection: sqlite3.Connection, room_id: str, expected_revision: int) -> Room:
        cls._revision(expected_revision)
        room = cls._get_room(connection, room_id)
        if room.revision != expected_revision:
            raise Conflict(f"Room was changed by another request; expected revision {expected_revision}, current revision {room.revision}")
        return room

    @staticmethod
    def _speaker_balance(connection: sqlite3.Connection, identity: tuple[str, str]) -> int:
        if connection.execute("PRAGMA user_version").fetchone()[0] < 3:
            return 100
        row = connection.execute("SELECT balance_percent FROM speaker_balances WHERE identity_family=? AND identity_key=?", identity).fetchone()
        return row[0] if row else 100

    @staticmethod
    def _speaker_airplay_timing(connection: sqlite3.Connection, identity: tuple[str, str], protocol: str) -> AirplayTiming:
        if protocol != "airplay2" or connection.execute("PRAGMA user_version").fetchone()[0] < 4:
            return "auto"
        row = connection.execute("SELECT airplay_timing FROM speaker_airplay_timing WHERE identity_family=? AND identity_key=?", identity).fetchone()
        return row[0] if row else "auto"

    def list_rooms(self) -> list[Room]:
        with self._transaction(write=False) as connection:
            ids = [row[0] for row in connection.execute("SELECT id FROM rooms ORDER BY slot").fetchall()]
            return [self._get_room(connection, room_id) for room_id in ids]

    def get_room(self, room_id: str) -> Room:
        with self._transaction(write=False) as connection:
            return self._get_room(connection, room_id)

    def create_room(self, creation: RoomCreate) -> Room:
        if not isinstance(creation, RoomCreate):
            raise ValidationIssue("Room creation must be a validated RoomCreate")
        with self._transaction() as connection:
            room = self._insert_room(connection, creation)
            self._event(connection, "room.created", f"Created room {room.name}", room.id)
            return room

    def _insert_room(self, connection: sqlite3.Connection, creation: RoomCreate, *, room_id: str | None = None) -> Room:
        used = {row[0] for row in connection.execute("SELECT slot FROM rooms").fetchall()}
        if len(used) >= self.max_rooms:
            raise Conflict(f"Maximum of {self.max_rooms} rooms reached")
        free = next((slot for slot in range(self.max_rooms) if slot not in used), None)
        if free is None:
            raise Conflict(f"Maximum of {self.max_rooms} rooms reached")
        room = Room(id=room_id or str(uuid4()), slot=free, name=creation.name,
                    airplay_name=creation.airplay_name if creation.airplay_name is not None else creation.name,
                    nobly_room_id=creation.nobly_room_id, interface=creation.interface,
                    local_audio_device=creation.local_audio_device)
        self._ensure_local_device_available(connection, room.local_audio_device)
        connection.execute(
            "INSERT INTO rooms VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (room.id, room.slot, room.name, room.name.casefold(), room.airplay_name, room.airplay_name.casefold(),
             room.nobly_room_id, room.interface, room.local_audio_device, int(room.enabled), room.volume, room.duck_gain, room.revision),
        )
        return room

    @staticmethod
    def _ensure_local_device_available(connection: sqlite3.Connection, device: str | None, *, room_id: str | None = None) -> None:
        if device is None:
            return
        try:
            key = local_audio_device_key(device)
            for row in connection.execute("SELECT id, local_audio_device FROM rooms WHERE local_audio_device IS NOT NULL").fetchall():
                if row["id"] != room_id and local_audio_device_key(row["local_audio_device"]) == key:
                    raise Conflict("This local audio device is already configured for another room")
        except ValueError as exc:
            raise ValidationIssue("Stored local audio device failed validation; database preserved") from exc

    def update_room(self, room_id: str, patch: RoomPatch, expected_revision: int) -> Room:
        if not isinstance(patch, RoomPatch):
            raise ValidationIssue("Room update must be a validated RoomPatch")
        with self._transaction() as connection:
            current = self._current(connection, room_id, expected_revision)
            updates = patch.model_dump(exclude_unset=True)
            if "airplay_name" in updates and updates["airplay_name"] is None:
                updates["airplay_name"] = updates.get("name", current.name)
            try:
                room = Room.model_validate({**current.model_dump(), **updates})
            except ValidationError as exc:
                raise ValidationIssue(str(exc)) from exc
            if room == current:
                return current
            room = Room.model_validate({**room.model_dump(), "revision": current.revision + 1})
            # A local card change changes the physical ownership key atomically.
            if room.local_audio_device != current.local_audio_device and room.speakers:
                speakers = []
                for speaker in room.speakers:
                    if speaker.protocol in {"alsa", "pulseaudio"}:
                        profile = connection.execute("SELECT offset_ms FROM speaker_profiles WHERE identity_family=? AND identity_key=?", speaker_key(speaker, room.local_audio_device)).fetchone()
                        speaker = SpeakerRef.model_validate({**speaker.model_dump(), "offset_ms": profile[0] if profile else 0,
                                                             "balance_percent": self._speaker_balance(connection, speaker_key(speaker, room.local_audio_device))})
                    speakers.append(speaker)
                room = Room.model_validate({**room.model_dump(), "speakers": speakers})
                self._write_speakers(connection, room)
            self._update_row(connection, room)
            self._event(connection, "room.updated", f"Updated room {room.name} to revision {room.revision}", room.id)
            return room

    @staticmethod
    def _update_row(connection: sqlite3.Connection, room: Room) -> None:
        Store._ensure_local_device_available(connection, room.local_audio_device, room_id=room.id)
        connection.execute(
            "UPDATE rooms SET name=?, name_key=?, airplay_name=?, airplay_name_key=?, nobly_room_id=?, interface=?, local_audio_device=?, enabled=?, volume=?, duck_gain=?, revision=? WHERE id=?",
            (room.name, room.name.casefold(), room.airplay_name, room.airplay_name.casefold(), room.nobly_room_id,
             room.interface, room.local_audio_device, int(room.enabled), room.volume, room.duck_gain, room.revision, room.id),
        )

    def apply_phone_volume(self, room_id: str, volume: int, expected_revision: int, event_id: str) -> tuple[Room, bool, int]:
        """Atomically commit a phone event and retain its original acknowledgment.

        A lost acknowledgment replay cannot rebase queued phone events onto a
        newer UI revision. Receipts preserve the original causal commit even
        when this method returns a more recent current room definition.
        """
        self._revision(expected_revision)
        if type(volume) is not int or not 0 <= volume <= 100:
            raise ValidationIssue("Phone volume must be an integer from 0 to 100")
        if (
            not isinstance(event_id, str) or not 1 <= len(event_id) <= 128
            or event_id != event_id.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in event_id)
        ):
            raise ValidationIssue("Phone volume event id must be an exact nonempty identifier of at most 128 characters")
        with self._transaction() as connection:
            receipt = connection.execute("SELECT * FROM phone_volume_receipts WHERE event_id=?", (event_id,)).fetchone()
            if receipt is not None and (receipt["room_id"] != room_id or receipt["volume"] != volume):
                raise Conflict("Phone volume event id was already used for a different room or volume")
            current = self._get_room(connection, room_id)
            if receipt is not None:
                return current, True, receipt["committed_revision"]
            if current.revision != expected_revision:
                return current, False, current.revision
            room = Room.model_validate({**current.model_dump(), "volume": volume, "revision": current.revision + 1})
            self._update_row(connection, room)
            self._event(connection, "room.phone_volume", f"Set {room.name} phone volume to {volume} at revision {room.revision}", room.id)
            connection.execute(
                "INSERT INTO phone_volume_receipts(event_id, room_id, volume, committed_revision, created_at) VALUES (?, ?, ?, ?, ?)",
                (event_id, room.id, volume, room.revision, datetime.now(timezone.utc).isoformat(timespec="milliseconds")),
            )
            connection.execute(
                "DELETE FROM phone_volume_receipts WHERE id <= (SELECT id FROM phone_volume_receipts ORDER BY id DESC LIMIT 1 OFFSET ?)",
                (MAX_PHONE_RECEIPTS,),
            )
            return room, True, room.revision

    def assign_speakers(self, room_id: str, speakers: list[SpeakerRef], expected_revision: int) -> Room:
        if not isinstance(speakers, list) or any(not isinstance(speaker, SpeakerRef) for speaker in speakers):
            raise ValidationIssue("Speaker assignments must be a list of validated SpeakerRef objects")
        with self._transaction() as connection:
            current = self._current(connection, room_id, expected_revision)
            resolved = []
            for speaker in speakers:
                explicit = speaker.model_fields_set
                try:
                    key = speaker_key(speaker, current.local_audio_device)
                except ValueError as exc:
                    raise ValidationIssue(str(exc)) from exc
                profile = connection.execute("SELECT offset_ms FROM speaker_profiles WHERE identity_family=? AND identity_key=?", key).fetchone()
                if profile is not None and "offset_ms" not in speaker.model_fields_set:
                    speaker = SpeakerRef.model_validate({**speaker.model_dump(), "offset_ms": profile[0]})
                if "balance_percent" not in explicit:
                    speaker = SpeakerRef.model_validate({**speaker.model_dump(), "balance_percent": self._speaker_balance(connection, key)})
                if "airplay_timing" not in explicit:
                    speaker = SpeakerRef.model_validate({**speaker.model_dump(), "airplay_timing": self._speaker_airplay_timing(connection, key, speaker.protocol)})
                resolved.append(speaker)
            speakers = resolved
            try:
                room = Room.model_validate({**current.model_dump(), "speakers": speakers})
            except ValidationError as exc:
                raise ValidationIssue(str(exc)) from exc
            if room == current:
                return current
            room = Room.model_validate({**room.model_dump(), "revision": current.revision + 1})
            self._write_speakers(connection, room)
            self._update_row(connection, room)
            self._event(connection, "room.speakers", f"Assigned {len(speakers)} speakers to {room.name}", room.id)
            return room

    @staticmethod
    def _write_speakers(connection: sqlite3.Connection, room: Room) -> None:
        connection.execute("DELETE FROM room_speakers WHERE room_id=?", (room.id,))
        for position, speaker in enumerate(room.speakers):
            family, identity = speaker_key(speaker, room.local_audio_device)
            connection.execute("INSERT INTO room_speakers VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                               (room.id, family, identity, speaker.id, position, speaker.name, speaker.protocol, speaker.offset_ms))
            connection.execute("INSERT INTO speaker_profiles VALUES (?, ?, ?) ON CONFLICT(identity_family, identity_key) DO UPDATE SET offset_ms=excluded.offset_ms",
                               (family, identity, speaker.offset_ms))
            connection.execute("INSERT INTO speaker_balances VALUES (?, ?, ?) ON CONFLICT(identity_family, identity_key) DO UPDATE SET balance_percent=excluded.balance_percent",
                               (family, identity, speaker.balance_percent))
            if speaker.protocol == "airplay2":
                connection.execute("INSERT INTO speaker_airplay_timing VALUES (?, ?, ?) ON CONFLICT(identity_family, identity_key) DO UPDATE SET airplay_timing=excluded.airplay_timing",
                                   (family, identity, speaker.airplay_timing))

    def update_speaker_airplay_timing(self, room_id: str, speaker_id: str, airplay_timing: AirplayTiming, expected_revision: int) -> Room:
        if type(airplay_timing) is not str or airplay_timing not in {"auto", "ptp", "ntp"}:
            raise ValidationIssue("AirPlay timing must be auto, ptp or ntp")
        with self._transaction() as connection:
            current = self._current(connection, room_id, expected_revision)
            matches = [speaker for speaker in current.speakers if speaker.id == speaker_id]
            if not matches:
                raise NotFound("Speaker is not assigned to this room")
            if len(matches) != 1:
                raise Conflict("Speaker id is ambiguous across protocols; use a protocol-specific assignment")
            if matches[0].protocol != "airplay2":
                raise ValidationIssue("Choose an AirPlay timing mode only for an AirPlay 2 speaker")
            if matches[0].airplay_timing == airplay_timing:
                return current
            speakers = [SpeakerRef.model_validate({**speaker.model_dump(), "airplay_timing": airplay_timing})
                        if speaker.id == speaker_id else speaker for speaker in current.speakers]
            room = Room.model_validate({**current.model_dump(), "speakers": speakers, "revision": current.revision + 1})
            self._write_speakers(connection, room)
            self._update_row(connection, room)
            self._event(connection, "speaker.airplay_timing", f"Set {matches[0].name} AirPlay timing to {airplay_timing}", room.id)
            return room

    def update_speaker_balance(self, room_id: str, speaker_id: str, balance_percent: int, expected_revision: int) -> Room:
        if type(balance_percent) is not int or not 0 <= balance_percent <= 100:
            raise ValidationIssue("Speaker balance must be an integer from 0 to 100")
        with self._transaction() as connection:
            current = self._current(connection, room_id, expected_revision)
            matches = [speaker for speaker in current.speakers if speaker.id == speaker_id]
            if not matches:
                raise NotFound("Speaker is not assigned to this room")
            if len(matches) != 1:
                raise Conflict("Speaker id is ambiguous across protocols; use a protocol-specific assignment")
            if matches[0].balance_percent == balance_percent:
                return current
            speakers = [speaker.model_copy(update={"balance_percent": balance_percent}) if speaker.id == speaker_id else speaker for speaker in current.speakers]
            room = Room.model_validate({**current.model_dump(), "speakers": speakers, "revision": current.revision + 1})
            self._write_speakers(connection, room)
            self._update_row(connection, room)
            self._event(connection, "speaker.balance", f"Set {matches[0].name} balance to {balance_percent}%", room.id)
            return room

    def update_speaker_offset(self, room_id: str, speaker_id: str, offset_ms: int, expected_revision: int) -> Room:
        if type(offset_ms) is not int or not -2000 <= offset_ms <= 2000:
            raise ValidationIssue("Speaker delay must be an integer between -2000 and 2000 milliseconds")
        with self._transaction() as connection:
            current = self._current(connection, room_id, expected_revision)
            return self._set_speaker_offset(connection, current, speaker_id, offset_ms)

    @classmethod
    def _set_speaker_offset(cls, connection, current, speaker_id, offset_ms):
        matches = [speaker for speaker in current.speakers if speaker.id == speaker_id]
        if not matches:
            raise NotFound("Speaker is not assigned to this room")
        if len(matches) != 1:
            raise Conflict("Speaker id is ambiguous across protocols; use a protocol-specific assignment")
        if matches[0].offset_ms == offset_ms:
            return current
        speakers = [SpeakerRef.model_validate({**speaker.model_dump(), "offset_ms": offset_ms}) if speaker.id == speaker_id else speaker for speaker in current.speakers]
        room = Room.model_validate({**current.model_dump(), "speakers": speakers, "revision": current.revision + 1})
        cls._write_speakers(connection, room)
        cls._update_row(connection, room)
        cls._event(connection, "speaker.offset", f"Set {matches[0].name} delay to {offset_ms} ms", room.id)
        return room

    @staticmethod
    def _calibration_record(row):
        from .calibration import validate_record
        try:
            record = json.loads(row["evidence"])
            record = validate_record(record)
            if any(record[key] != row[key] for key in ["id", "room_id", "generation", "created_at", "expires_at", "applied_revision"]):
                raise ValueError("Receipt columns do not match their measurement evidence")
        except (TypeError, ValueError, KeyError, RecursionError) as exc:
            raise ValidationIssue("Stored calibration evidence is invalid; database preserved") from exc
        return record

    @classmethod
    def _get_calibration(cls, connection, room_id, session_id):
        cls._get_room(connection, room_id)
        row = connection.execute("SELECT * FROM calibration_sessions WHERE room_id=? AND id=?", (room_id, session_id)).fetchone()
        if row is None or row["expires_at"] <= time.time():
            raise NotFound("Calibration session does not exist in this room or has expired")
        return cls._calibration_record(row)

    @staticmethod
    def _write_calibration(connection, record):
        from .calibration import validate_record
        record = validate_record(record)
        connection.execute(
            "INSERT INTO calibration_sessions(id,room_id,generation,created_at,expires_at,applied_revision,evidence) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET generation=excluded.generation,expires_at=excluded.expires_at,applied_revision=excluded.applied_revision,evidence=excluded.evidence",
            (record["id"], record["room_id"], record["generation"], record["created_at"], record["expires_at"], record["applied_revision"],
             json.dumps(record, allow_nan=False, separators=(",", ":"))),
        )

    def get_calibration(self, room_id, session_id):
        with self._transaction(write=False) as connection:
            return self._get_calibration(connection, room_id, session_id)

    def list_calibrations(self, room_id):
        with self._transaction(write=False) as connection:
            self._get_room(connection, room_id)
            return [self._calibration_record(row) for row in connection.execute(
                "SELECT * FROM calibration_sessions WHERE room_id=? AND expires_at>? ORDER BY created_at,id", (room_id, time.time()))]

    def save_calibration(self, record, expected_generation, *, expected_room_revision=None, expected_reference_revision=None):
        """Persist evidence under a cross-process generation/configuration guard.

        This operation cannot manufacture an apply/rollback receipt. Those are
        written only by commit_calibration_offset in the profile transaction.
        """
        from .calibration import MAX_ACTIVE, MAX_HISTORY, validate_record, fingerprint
        record = validate_record(record)
        record = json.loads(json.dumps(record, allow_nan=False))
        if type(expected_generation) is not int or expected_generation < 0 or record["generation"] != expected_generation:
            raise ValidationIssue("Calibration generation must match the reviewed evidence")
        with self._transaction() as connection:
            connection.execute("DELETE FROM calibration_sessions WHERE expires_at<=?", (time.time(),))
            room = self._get_room(connection, record["room_id"])
            if expected_room_revision is not None:
                self._current(connection, room.id, expected_room_revision)
            existing = connection.execute("SELECT * FROM calibration_sessions WHERE id=?", (record["id"],)).fetchone()
            if existing is None:
                if expected_generation != 0:
                    raise Conflict("Calibration session was removed or expired; reload the evidence")
                if (record["applied_revision"] is not None or record["recordings"] or record["verification_recordings"]
                        or record["rolled_back"] or record["application"] is not None):
                    raise ValidationIssue("New measurement sessions must begin without saved corrections or recordings")
                if room.revision != record["room_revision"] or fingerprint(room) != record["configuration"]:
                    raise Conflict("Room changed; reload before starting a calibration session")
                reference = self._calibration_reference(connection, record, expected_reference_revision)
                if reference.revision != record["reference_revision"]:
                    raise Conflict("Reference room changed; reload before starting a calibration session")
                if connection.execute("SELECT count(*) FROM calibration_sessions WHERE applied_revision IS NULL").fetchone()[0] >= MAX_ACTIVE:
                    raise Conflict("Too many calibration sessions; delete an existing session before starting another")
                if connection.execute("SELECT count(*) FROM calibration_sessions").fetchone()[0] >= MAX_HISTORY:
                    oldest = connection.execute("SELECT id FROM calibration_sessions WHERE applied_revision IS NOT NULL ORDER BY created_at,id LIMIT 1").fetchone()
                    if oldest is None:
                        raise Conflict("Calibration history is full; export and delete an old session")
                    connection.execute("DELETE FROM calibration_sessions WHERE id=?", (oldest[0],))
            else:
                saved = self._calibration_record(existing)
                if saved["room_id"] != record["room_id"] or saved["generation"] != expected_generation:
                    raise Conflict("Calibration evidence changed in another request; reload before continuing")
                immutable = set(saved) - {"generation", "recordings", "result", "verification_recordings", "verification_backend", "application"}
                if any(saved[key] != record[key] for key in immutable):
                    raise Conflict("Calibration receipts and identities may change only with an atomic offset commit")
                for key in ["recordings", "verification_recordings", "verification_backend"]:
                    if record[key][:len(saved[key])] != saved[key] or not len(saved[key]) <= len(record[key]) <= len(saved[key]) + 1:
                        raise Conflict("Recorded evidence is append-only; reload before importing again")
                if expected_room_revision is not None and fingerprint(room) != self._calibration_configuration(record):
                    raise Conflict("Speaker configuration changed; start a new calibration session")
                if expected_room_revision is not None:
                    self._calibration_reference(connection, record, expected_reference_revision)
            record["generation"] += 1
            self._write_calibration(connection, record)
            return record

    @staticmethod
    def _calibration_configuration(record):
        config = json.loads(json.dumps(record["configuration"]))
        if record["applied_revision"] is not None and not record["rolled_back"]:
            for speaker in config["speakers"]:
                if speaker["id"] == record["target_id"]:
                    speaker["offset_ms"] = record["applied_offset_ms"]
        return config

    @classmethod
    def _calibration_reference(cls, connection, record, expected_revision=None):
        from .calibration import fingerprint
        try:
            room = cls._get_room(connection, record["reference_room_id"])
        except NotFound as exc:
            raise Conflict("The reference room was removed; export this evidence and start a new measurement") from exc
        if expected_revision is not None:
            cls._current(connection, room.id, expected_revision)
        expected = (cls._calibration_configuration(record) if room.id == record["room_id"] else record["reference_configuration"])
        if fingerprint(room) != expected:
            raise Conflict("Reference speaker identities or saved settings changed; this calibration is stale")
        return room

    def commit_calibration_offset(self, room_id, session_id, expected_revision, expected_generation, *, rollback=False, expected_reference_revision=None):
        """Commit profile, room revision, event and durable rollback receipt together."""
        from .calibration import HISTORY_SECONDS, fingerprint
        with self._transaction() as connection:
            room = self._current(connection, room_id, expected_revision)
            record = self._get_calibration(connection, room_id, session_id)
            if record["generation"] != expected_generation:
                raise Conflict("Calibration evidence changed; reload before changing timing")
            if room.enabled:
                raise Conflict("Turn this room off before applying or rolling back a calibration correction")
            if record["rolled_back"]:
                raise Conflict("This calibration correction was already rolled back")
            if rollback:
                if record["applied_revision"] is None:
                    raise Conflict("This calibration has no saved correction to roll back")
                offset = record["previous_offset_ms"]
            else:
                if record["applied_revision"] is not None:
                    raise Conflict("This calibration correction was already saved")
                if record["result"]["status"] != "candidate_correction":
                    raise Conflict("Calibration has insufficient stable evidence for a correction")
                offset = record["result"]["candidate_offset_ms"]
            if fingerprint(room) != self._calibration_configuration(record):
                raise Conflict("Speaker identities or saved settings changed; do not apply this stale calibration")
            if record["reference_room_id"] != room_id and expected_reference_revision is None:
                raise Conflict("Review the current reference room revision before changing cross-zone timing")
            self._calibration_reference(connection, record, expected_reference_revision)
            updated = self._set_speaker_offset(connection, room, record["target_id"], offset)
            if rollback:
                record["rolled_back"] = True
            else:
                record["applied_offset_ms"] = offset
                record["applied_revision"] = updated.revision
                record["expires_at"] = time.time() + HISTORY_SECONDS
            record["generation"] += 1
            record["application"] = {"runtime_accepted": False, "state": "saved_room_off",
                                     "pending_reason": "Runtime acknowledgment has not completed",
                                     "message": "Delay saved; enable the room, verify OwnTone readback and import fresh post-change recordings"}
            self._write_calibration(connection, record)
            self._event(connection, "calibration.rollback" if rollback else "calibration.applied",
                        f"{'Restored' if rollback else 'Saved'} {record['target_id']} delay to {offset} ms with measurement receipt {session_id}", room_id)
            return updated, record

    def delete_calibration(self, room_id, session_id):
        with self._transaction() as connection:
            self._get_calibration(connection, room_id, session_id)
            connection.execute("DELETE FROM calibration_sessions WHERE room_id=? AND id=?", (room_id, session_id))

    def delete_room(self, room_id: str, expected_revision: int) -> None:
        with self._transaction() as connection:
            room = self._current(connection, room_id, expected_revision)
            if room.enabled:
                raise Conflict("Disable the room and release its runtime before deleting it")
            connection.execute("DELETE FROM rooms WHERE id=?", (room.id,))
            self._event(connection, "room.deleted", f"Deleted room {room.name}", room.id)

    @staticmethod
    def _event(connection: sqlite3.Connection, kind: str, message: str, room_id: str | None) -> None:
        created_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        connection.execute("INSERT INTO events(kind, message, room_id, created_at) VALUES (?, ?, ?, ?)",
                           (kind, message, room_id, created_at))
        connection.execute("DELETE FROM events WHERE id <= (SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET ?)", (MAX_EVENTS,))

    def record_event(self, kind: str, message: str, room_id: str | None = None) -> None:
        if not isinstance(kind, str) or not kind or len(kind) > 64 or not isinstance(message, str) or not message or len(message) > 2048:
            raise ValidationIssue("Event kind and message must be nonempty strings within 64 and 2048 characters")
        with self._transaction() as connection:
            self._event(connection, kind, message, room_id)

    def list_events(self, limit: int = 100) -> list[Event]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValidationIssue("Event limit must be between 1 and 1000")
        with self._transaction(write=False) as connection:
            return [Event(**dict(row)) for row in connection.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def import_legacy(self, source_path: str | Path, dry_run: bool = True):
        from .migration import import_legacy
        return import_legacy(source_path, dry_run=dry_run, store=self)

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.close()
