"""Privileged asynchronous orchestration, separate from the rootless product API.

Each room converges serially to its desired definition. Shared sender reservations
cover partial starts; bounded retries isolate a failed receiver from healthy rooms.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import signal
import stat
import sys
import time
from uuid import UUID, uuid4

from pydantic import ValidationError

from shiri.domain import Room, SpeakerRef, local_audio_device_key, speaker_key, validate_local_audio_device
from shiri.rpc import AdmissionRefused, RpcError, StreamReply, call_rpc, serve_rpc
from shiri.speech_stream import open_speech, serve_speech
from shiri.readiness import transport_fingerprint
from shiri.settings import RuntimeConfig
from shiri.source import SourceToken
from .backend import OwnToneClient, OwnToneRejected
from .configuration import backend_configs
from .latency import latency_plan, room_buffer_ms
from .identities import DaemonIdentities
from .local_devices import LocalDevices
from .alsa_identity import PCMIdentityError, inventory as alsa_inventory, resolve as resolve_pcm
from .layout import admit_bus_socket, directory as private_directory, file_owner, prepare_discovery, prepare_room_view
from .units import Bind, PCMExec, UnitManager, UnitSpec, VIEW, new_unit
from .bind_policy import trusted_file
from .network import DHCP_HOOK, NetworkManager
from .speech_endpoint import retire as retire_speech_endpoint
from .unix_directory import PinnedUnixDirectory
from .system import OwnedProcess, Runner, RuntimeFailure, atomic_json, read_json, root_directory

log = logging.getLogger(__name__)
REQUIRED_OWNTONE_VERSION = "29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-resample1-framed1-alsa1-speech1-ready1-anchor1-jitter1-owner1-balance1-transition1-bed1-event1-idle1-drain1-startupmeta1-coldmusic1-outputclock1-duck1-warm1"
_OWNTONE_VERSION_PATTERN = re.compile(r"(?<![\w.-])" + re.escape(REQUIRED_OWNTONE_VERSION) + r"(?![\w.-])")
# The pinned receiver appends these feature tokens after its backend marker.
# Its sysconfdir path is removed before matching, so path text cannot qualify.
_SHAIRPORT_TIMED_PATTERN = re.compile(
    r"-shiri-timed3-startup1-volume2(?=$|\s|(?:-soxr)?(?:-convolution)?(?:-metadata)?(?:-mqtt)?(?:-dbus)?(?:-mpris)?$)"
)


def now():
    return datetime.now(timezone.utc).isoformat()


class Superseded(Exception):
    pass


@dataclass
class RuntimeRoom:
    desired: Room
    directory: Path
    status: str = "stopped"
    error: str | None = None
    receiver: dict | None = None
    client: OwnToneClient | None = None
    processes: dict[str, OwnedProcess] = field(default_factory=dict)
    task: asyncio.Task | None = None
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    control_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    applied: Room | None = None
    current_volume: int = 50
    selected_ids: list[str] = field(default_factory=list)
    outputs: list[dict] = field(default_factory=list)
    player: dict = field(default_factory=dict)
    last_health_at: str | None = None
    failures: int = 0
    retry_at: float = 0
    restart_required: bool = False
    removing: bool = False
    reserved_slot: int | None = None
    backend_definition: Room | None = None
    held_speakers: set[tuple] = field(default_factory=set)
    active_local_device: str | None = None
    phone_volume_update: dict | None = None
    phone_volume_next: dict | None = None
    worker_sockets: dict[str, Path] = field(default_factory=dict)
    signal_server: object | None = None
    launch_generation: str | None = None
    native_volume_receipts: dict[str, dict] = field(default_factory=dict)
    local_pin: object | None = None
    held_local_node: str | None = None
    bluetooth_admission: object | None = None
    bluetooth_handoff: object | None = None
    bluetooth_rpc_directory: PinnedUnixDirectory | None = None
    timing: tuple[int, int] = (40, 140)
    active_timing: tuple[int, int] | None = None
    gain_pending: bool = False
    phone_volume_revision: int | None = None
    receiver_volume: dict | None = None
    activity: dict = field(default_factory=dict)
    speech_streams: dict = field(default_factory=dict)

    def snapshot(self):
        offsets = {speaker.id: speaker.offset_ms for speaker in self.desired.speakers}
        balances = {speaker.id: speaker.balance_percent for speaker in self.desired.speakers}
        clocks = {speaker.id: speaker.airplay_timing for speaker in self.desired.speakers}
        outputs = [dict(output, requested_offset_ms=offsets.get(output["id"]),
                        requested_balance_percent=balances.get(output["id"]),
                        requested_airplay_timing=clocks.get(output["id"])) for output in self.outputs]
        return {
            "room_id": self.desired.id,
            "status": self.status,
            "error": self.error,
            "receiver_ip": self.receiver.get("ip") if self.receiver else None,
            "owntone_url": self.client.base_url if self.client else None,
            "last_health_at": self.last_health_at,
            "volume": self.current_volume,
            "volume_settings_pending": self.gain_pending,
            "receiver_volume": self.receiver_volume,
            "phone_volume_update": self.phone_volume_update,
            "selected_ids": self.selected_ids,
            "outputs": outputs,
            "player": self.player,
            "activity": self.activity,
            "timing": {"output_buffer_ms": self.timing[0], "common_relay_delay_ms": self.timing[1],
                       "active": self.active_timing == self.timing and self.client is not None},
            "processes": [
                {"name": process.name, "pid": process.process.pid, "alive": process.alive}
                for process in self.processes.values()
            ],
            "retry_in_seconds": max(0, round(self.retry_at - asyncio.get_running_loop().time())),
        }


class Broker:
    def __init__(self, config: RuntimeConfig, *, runner: Runner | None = None):
        self.config, self.runner = config, runner or Runner()
        self.network: NetworkManager | None = None
        self.identities: DaemonIdentities | None = None
        self.unit_manager: UnitManager | None = None
        self.local_devices: LocalDevices | None = None
        self.bluealsa = None
        self.rooms: dict[str, RuntimeRoom] = {}
        self.sender: dict | None = None
        self.sender_processes: dict[str, OwnedProcess] = {}
        self.sender_users: set[str] = set()
        self.sender_lock = asyncio.Lock()
        self.config_lock = asyncio.Lock()
        self.slot_locks = [asyncio.Lock() for _ in range(config.max_rooms)]
        self.speaker_lock = asyncio.Lock()
        self.speaker_leases: dict[tuple, str] = {}
        self.local_node_leases: dict[str, str] = {}
        self.sessions: dict[str, str] = {}
        self.session_generations: dict[str, object] = {}
        self.pending_sessions: set[str] = set()
        self._session_operations: dict[str, set[object]] = {}
        self.ready = False
        self.error: str | None = "Runtime is initializing"
        self.versions: dict[str, str] = {}
        self._lock_file = None
        self._monitor: asyncio.Task | None = None
        self._server = None
        self._closing = False
        self._password = ""

    def binary(self, name: str) -> str:
        if self.config.binary_dir:
            for directory in [
                self.config.binary_dir,
                self.config.binary_dir / "sbin",
                self.config.binary_dir / "bin",
            ]:
                path = directory / name
                if path.is_file() and os.access(path, os.X_OK):
                    return str(path)
            raise RuntimeFailure(f"Required binary {name} is missing from the configured binary directory")
        path = shutil.which(name)
        if not path:
            raise RuntimeFailure(f"Required binary {name} is missing; run the installer")
        return path

    async def preflight(self):
        if sys.platform != "linux" or os.geteuid() != 0:
            raise RuntimeFailure(
                "The real audio runtime requires a root Linux broker; the Mac hosts the control app only"
            )
        for name in ["ip", "dhclient", "dbus-daemon", "systemd-run", "journalctl", "ping"]:
            if not shutil.which(name):
                raise RuntimeFailure(f"Required system command {name} is missing")
        for name in ["nqptp", "shairport-sync", "airptpd", "owntone", "avahi-daemon"]:
            trusted_file(Path(self.binary(name)), executable=True)
        for name in ["nqptp", "shairport-sync"]:
            result = await self.runner.run([self.binary(name), "-V"])
            self.versions[name] = (result.stdout or result.stderr).strip()
        shairport = self.versions["shairport-sync"]
        receiver_features = shairport.split("-sysconfdir:", 1)[0]
        if "AirPlay2" not in receiver_features or not _SHAIRPORT_TIMED_PATTERN.search(receiver_features):
            raise RuntimeFailure(
                "Shairport Sync must include AirPlay 2 and the shiri-timed3 private PCM backend "
                "with bounded clock sampling and recovery, synchronous selected-output preparation and exact receive-thread cleanup; rebuild pinned backends using install/build_backends.sh"
            )
        pinned_shairport = False
        if self.config.binary_dir:
            manifest = self.config.binary_dir / "share" / "shiri" / "backends.json"
            build = read_json(manifest, {})
            pinned_shairport = (
                build.get("shairport") == "7bad231c18368dbd26f298577f6210e36e4b0797"
                and "7bad231" in shairport
            )
        if "5.5.2" not in shairport and not pinned_shairport:
            raise RuntimeFailure(
                "This runtime requires the verified Shairport Sync 5.5.2 configuration; install pinned binaries"
            )
        smi = [re.search(r"smi\d+", self.versions[name]) for name in ["nqptp", "shairport-sync"]]
        if not all(smi) or smi[0].group() != smi[1].group():
            raise RuntimeFailure("NQPTP and Shairport shared-memory interfaces do not match")
        result = await self.runner.run([self.binary("owntone"), "--version"])
        self.versions["owntone"] = (result.stdout or result.stderr).strip()
        if not _OWNTONE_VERSION_PATTERN.search(self.versions["owntone"]):
            raise RuntimeFailure(
                f"This runtime requires OwnTone {REQUIRED_OWNTONE_VERSION} with volume, timing, source, PCM, transport, offset, native buffer, converter reset, framed output, partial-write preservation, late speech mixing, cold speech readiness, fresh first-anchor deadline admission bounded speech jitter reserve, exact voice retirement, saved speaker balance, bounded exact source admission, paused-source speech output and framed metadata event acknowledgement and idle speech output without input refill, natural speech drain and acknowledged startup metadata and timed music input without legacy refill and stable identity speaker clock selection and bounded per-voice duck envelopes; "
                "rebuild pinned backends using install/build_backends.sh"
            )
        if not DHCP_HOOK.is_file() or not os.access(DHCP_HOOK, os.X_OK):
            raise RuntimeFailure("The private namespace DHCP hook is missing")
        self.identities = DaemonIdentities(self.config.daemon_identity_file).load()
        if not Path("/sys/fs/cgroup/cgroup.controllers").is_file():
            raise RuntimeFailure("Daemon isolation requires unified cgroup v2")
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            raise RuntimeFailure("Daemon recovery requires Linux process-handle signaling")
        result = await self.runner.run([self.binary("avahi-daemon"), "--version"])
        self.versions["avahi-daemon"] = (result.stdout or result.stderr).strip()
        if "0.8-shiri-user1" not in self.versions["avahi-daemon"]:
            raise RuntimeFailure("Private discovery requires the pinned Avahi 0.8-shiri-user1 backend")
        trusted_file(self.bind_policy_helper(), executable=True)
        trusted_file(self.pcm_exec_helper(), executable=True)
        trusted_file(Path(__file__).with_name("launch_gate.py"))
        trusted_file(Path(__file__).with_name("log_reader.py"))
        await self.runner.run(
            [
                sys.executable,
                "-c",
                "import aiortc; import av; import numpy",
            ]
        )

    @staticmethod
    def _singleton_lock(path: Path, owner: int):
        """Hold only an unchanged owner-private regular inode; never repair it."""
        descriptor = original = None

        def validate(info):
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != owner or stat.S_IMODE(info.st_mode) != 0o600):
                raise RuntimeFailure(
                    "Broker singleton lock must be an unchanged owner-only 0600 regular file with one link; "
                    "stop owned services and inspect the existing lock before repairing its metadata"
                )

        def unchanged():
            held = os.fstat(descriptor)
            named = path.lstat()
            validate(held)
            validate(named)
            if ((named.st_dev, named.st_ino) != (held.st_dev, held.st_ino)
                    or (original is not None and original != (held.st_dev, held.st_ino))):
                raise RuntimeFailure("Broker singleton lock was replaced; preserve both inodes for inspection")

        try:
            try:
                previous = path.lstat()
                validate(previous)
                original = previous.st_dev, previous.st_ino
            except FileNotFoundError:
                pass
            flags = os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            if original is None:
                flags |= os.O_CREAT | os.O_EXCL
            descriptor = os.open(path, flags, 0o600)
            unchanged()
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            unchanged()
            handle = os.fdopen(descriptor, "a")
            descriptor = None
            return handle
        except BlockingIOError as exc:
            raise RuntimeFailure("Another Shiri runtime broker owns this installation") from exc
        except OSError as exc:
            raise RuntimeFailure("Broker singleton lock cannot be admitted safely; preserve it for inspection") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    async def start(self, *, serve=True):
        allowed, socket_gid = {0}, None
        with suppress(KeyError):
            account = pwd.getpwnam("shiri")
            allowed.add(account.pw_uid)
            if sys.platform == "linux" and os.geteuid() == 0:
                socket_gid = account.pw_gid
        if sys.platform == "linux" and os.geteuid() == 0:
            root_directory(self.config.runtime_dir, 0o750)
            root_directory(self.config.runtime_state_dir)
            if socket_gid is not None:
                os.chown(self.config.runtime_dir, 0, socket_gid)
        else:
            self.config.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
        self._lock_file = self._singleton_lock(
            self.config.runtime_dir / "broker.lock", 0 if sys.platform == "linux" else os.geteuid(),
        )
        try:
            await self.preflight()
            self.network = NetworkManager(self.config.runtime_state_dir, self.runner)
            if (self.identities.installation_id != self.network.installation_id
                    or self.identities.runtime_state_dir != self.config.runtime_state_dir
                    or self.identities.runtime_dir != self.config.runtime_dir):
                raise RuntimeFailure("Daemon identities belong to another installation or state directory")
            self.unit_manager = UnitManager(self.runner, self.network, bind_policy_helper=self.bind_policy_helper(),
                                            pcm_exec_helper=self.pcm_exec_helper())
            self.runner.unit_manager = self.unit_manager
            await self.unit_manager.connect()
            await self.network.recover()
            self.local_devices = LocalDevices(self.config.runtime_state_dir / "local-devices.json",
                                             self.network.installation_id)
            credential_file = self.config.runtime_state_dir / "backend-credential.json"
            credential = read_json(credential_file, {})
            self._password = credential.get("password") or secrets.token_urlsafe(32)
            atomic_json(credential_file, {"password": self._password})
            self.ready, self.error = True, None
        except RuntimeFailure as exc:
            self.ready, self.error = False, str(exc)
            log.error("Runtime preflight/recovery failed: %s", exc)
        if serve:
            self._server = await serve_rpc(
                self.config.runtime_socket, self.dispatch, allowed_uids=allowed, socket_gid=socket_gid
            )
        self._monitor = asyncio.create_task(self._health_monitor(), name="runtime-health")
        return self.snapshot()

    def snapshot(self):
        return {
            "ready": self.ready,
            "simulation": False,
            "error": self.error,
            "versions": self.versions,
            "rooms": [room.snapshot() for room in self.rooms.values()],
        }

    async def dispatch(self, operation: str, payload: dict):
        try:
            if operation == "health":
                return self.snapshot()
            if operation == "diagnostics":
                return await self.diagnostics(payload.get("room_id"))
            if operation == "interfaces":
                if self.network is None:
                    return {"interfaces": [], "error": self.error}
                return {"interfaces": await self.network.interfaces()}
            if operation == "reconcile":
                return await self.reconcile(payload)
            if operation in {"local_devices", "bind_local_device"}:
                if self.local_devices is None or not self.ready:
                    raise RpcError("runtime_unavailable", "Start the audio runtime before selecting local speakers")
                if operation == "local_devices":
                    return self.local_devices.inventory()
                if set(payload) != {"selection_id", "binding", "conversion"}:
                    raise RpcError("invalid_request", "Select one inventoried device, binding and conversion preference")
                async with self.config_lock:
                    return self.local_devices.bind(payload["selection_id"], payload["binding"],
                                                   conversion=payload["conversion"])
            if (
                operation == "speech"
                and payload.get("action") == "close"
                and payload.get("room_id") not in self.rooms
            ):
                previous = self.sessions.get(payload.get("session_id"))
                if previous and previous != payload.get("room_id"):
                    raise RpcError("session_conflict", "Speech session belongs to another room")
                self._forget_session(payload.get("session_id"))
                return {"ok": True, "closed": True}
            room = self._room(payload.get("room_id"))
            if operation == "ack_phone_volume":
                return await self.ack_phone_volume(room, payload)
            if operation == "phone_volume":
                return await self.set_volume(room, payload.get("volume"), pending=True)
            if operation == "outputs":
                client = self._client(room)
                room.outputs = await client.outputs(self._receiver_names())
                return {"outputs": room.snapshot()["outputs"], "room_id": room.desired.id}
            if operation == "set_outputs":
                raw = payload.get("speakers")
                if not isinstance(raw, list):
                    raise RpcError("invalid_request", "speakers must be a list of stable output identities")
                speakers = [SpeakerRef.model_validate(item) for item in raw]
                return await self.set_outputs(room, speakers)
            if operation == "set_volume":
                return await self.set_volume(room, payload.get("volume"))
            if operation == "player":
                client = self._client(room)
                action = payload.get("action")
                if action is None or action == "status":
                    room.player = await client.request("GET", "/api/player")
                    return {"player": room.player}
                if action not in {"play", "pause", "stop", "toggle", "next", "previous"}:
                    raise RpcError("invalid_request", "Unsupported playback action")
                await client.request("PUT", f"/api/player/{action}")
                return {"ok": True}
            if operation == "speech":
                return await self.speech(room, payload)
            if operation == "speech-stream":
                return await self.open_speech(room, payload)
            if operation == "warm":
                return await self.warm(room, payload)
            raise RpcError("unknown_operation", "Unsupported runtime operation")
        except (ValidationError, ValueError, TypeError) as exc:
            raise RpcError("invalid_request", str(exc)) from exc
        except RuntimeFailure as exc:
            raise RpcError("backend_unavailable", str(exc)) from exc

    def _room(self, room_id):
        if not isinstance(room_id, str) or room_id not in self.rooms or self.rooms[room_id].removing:
            raise RpcError("room_not_found", "Room is not known to the audio runtime")
        return self.rooms[room_id]

    def _client(self, room: RuntimeRoom):
        if not room.client or room.status not in {"running", "degraded"}:
            raise RpcError("room_not_running", "Start the room and wait for its audio runtime")
        return room.client

    def _receiver_names(self):
        return {room.desired.airplay_name for room in self.rooms.values()}

    async def reconcile(self, payload):
        if self._closing:
            raise RpcError("runtime_unavailable", "Audio runtime is shutting down")
        definitions = payload.get("rooms")
        if not isinstance(definitions, list) or len(definitions) > self.config.max_rooms:
            raise RpcError("invalid_request", "rooms must be a list within the configured room limit")
        desired = [Room.model_validate(item) for item in definitions]
        ids, slots, names, interfaces, speakers, cards = set(), set(), set(), set(), set(), set()
        playback_nodes = set()
        for room in desired:
            if room.slot >= self.config.max_rooms:
                raise RpcError("invalid_configuration", "Room slot exceeds the configured hardware limit")
            if room.id in ids or room.slot in slots or room.airplay_name in names:
                raise RpcError(
                    "invalid_configuration", "Room identities, slots and advertised names must be unique"
                )
            ids.add(room.id)
            slots.add(room.slot)
            names.add(room.airplay_name)
            if room.enabled:
                interfaces.add(room.interface)
            if room.local_audio_device:
                device = self._physical_device(room.local_audio_device)
                if device in cards:
                    raise RpcError(
                        "invalid_configuration", "A local sound device cannot be shared between rooms"
                    )
                cards.add(device)
                if room.enabled and not device.startswith("bluealsa:"):
                    # SUBDEV does not have an independent kernel character
                    # node. Per-room device isolation must reserve the entire
                    # playback node, including the BlueALSA bridge's Loopback.
                    pin = self._resolve_local_pin(room.local_audio_device, room.slot)
                    try:
                        boundary = pin.playback_node
                    finally:
                        pin.close()
                    if boundary in playback_nodes:
                        raise RpcError("invalid_configuration",
                                       "Room output isolation requires separate ALSA playback device nodes")
                    playback_nodes.add(boundary)
            for speaker in room.speakers:
                key = self._speaker_key(room, speaker)
                if key in speakers:
                    raise RpcError(
                        "invalid_configuration", "A physical speaker cannot be assigned to multiple rooms"
                    )
                if speaker.name in {definition.airplay_name for definition in desired}:
                    raise RpcError("invalid_configuration", "A Shiri receiver cannot be used as a speaker")
                speakers.add(key)
        if len(interfaces) > 1:
            raise RpcError("invalid_configuration", "Enabled rooms must share the same LAN interface")
        async with self.config_lock:
            # Stale API revisions must not change another room's common clock.
            effective = [self.rooms[item.id].desired
                         if item.id in self.rooms and item.revision < self.rooms[item.id].desired.revision
                         else item for item in desired]
            plan = latency_plan(effective)
            for definition in effective:
                room = self.rooms.get(definition.id)
                if room is not None and definition.revision < room.desired.revision:
                    # A phone event ACK may advance intent while an older API
                    # reconciliation is already in flight. Equal revisions still
                    # replace the complete definition after a volume-only ACK.
                    continue
                completed = room is not None and room.task is not None and room.task.done()
                timing = (room_buffer_ms(definition), plan.common_horizon_ms)
                timing_changed = room is not None and room.timing != timing
                changed = room is None or room.desired != definition or completed or timing_changed
                if room is None:
                    directory = self.config.runtime_state_dir / "rooms" / definition.id
                    room = RuntimeRoom(definition, directory, current_volume=definition.volume)
                    stored = read_json(directory / "phone-volume.json")
                    if isinstance(stored, dict) and stored.get("version") == 2:
                        receipts = stored.get("native_receipts")
                        if (not isinstance(receipts, dict) or len(receipts) > 256
                                or any(not re.fullmatch(r"[0-9a-f]{32}", event_id)
                                       or not isinstance(receipt, dict)
                                       or not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("fingerprint")))
                                       or type(receipt.get("accepted")) is not bool
                                       for event_id, receipt in receipts.items())):
                            raise RuntimeFailure("Invalid native volume receipt state; inspection required")
                        room.native_volume_receipts = receipts
                        following = stored.get("next")
                        stored = stored.get("pending")
                        if stored is not None and following is not None:
                            stored = {**stored, "next": following}
                    if stored is not None:
                        if (
                            not isinstance(stored, dict)
                            or not re.fullmatch(r"[a-f0-9]{32}", str(stored.get("id")))
                            or type(stored.get("volume")) is not int
                            or not 0 <= stored["volume"] <= 100
                            or type(stored.get("base_revision")) is not int
                            or stored["base_revision"] < 1
                        ):
                            raise RuntimeFailure("Invalid pending phone volume state; inspection required")
                        room.phone_volume_update = stored
                        following = stored.pop("next", None)
                        if following is not None:
                            if (
                                not isinstance(following, dict)
                                or not re.fullmatch(r"[a-f0-9]{32}", str(following.get("id")))
                                or type(following.get("volume")) is not int
                                or not 0 <= following["volume"] <= 100
                                or type(following.get("base_revision")) is not int
                                or following["base_revision"] < 1
                            ):
                                raise RuntimeFailure("Invalid queued phone volume state; inspection required")
                            room.phone_volume_next = following
                        if stored["base_revision"] == definition.revision:
                            room.current_volume = (following or stored)["volume"]
                    self.rooms[definition.id] = room
                    room.task = asyncio.create_task(self._room_loop(room), name=f"room-{definition.id}")
                else:
                    if (
                        definition.volume != room.desired.volume
                        or definition.revision != room.desired.revision
                    ):
                        room.current_volume = definition.volume
                    room.desired, room.removing = definition, False
                    if completed:
                        room.task = asyncio.create_task(self._room_loop(room), name=f"room-{definition.id}")
                room.timing = timing
                if timing_changed and (room.client or room.processes):
                    # An administrative buffer/group change retires the old
                    # program incarnation. Speech never enters this path.
                    room.restart_required = True
                if not self.ready and definition.enabled:
                    room.status, room.error = "error", self.error
                else:
                    if not definition.enabled and (room.processes or room.receiver):
                        room.status = "stopping"
                    elif (
                        room.client
                        and room.backend_definition
                        and (self._material(room.backend_definition) != self._material(definition)
                             or timing_changed)
                    ):
                        room.status = "starting"
                    elif definition.enabled and room.status == "stopped":
                        room.status = "starting"
                    if changed:
                        room.wake.set()
            for room_id, room in self.rooms.items():
                if room_id not in ids:
                    room.removing = True
                    room.desired = room.desired.model_copy(update={"enabled": False})
                    room.wake.set()
        return self.snapshot()

    def _speaker_key(self, room: Room, speaker: SpeakerRef):
        return speaker_key(speaker, self._physical_device(room.local_audio_device))

    def _physical_device(self, device: str | None):
        if not device:
            return None
        canonical = local_audio_device_key(device)
        match = re.fullmatch(r"hw:CARD=(\d+),DEV=(\d+),SUBDEV=(\d+)", canonical)
        if match:
            try:
                identity = Path(f"/proc/asound/card{match[1]}/id").read_text().strip()
            except OSError:
                return canonical
            return local_audio_device_key(f"hw:{identity},{match[2]},{match[3]}")
        return canonical

    def _playback_device(self, device: str | None):
        """Validate named hardware without discarding PCM conversion.

        hw and plughw reserve the same physical endpoint, but plughw also
        requests ALSA rate/format conversion when opening that endpoint.
        """
        try:
            validate_local_audio_device(device)
        except ValueError as exc:
            raise RuntimeFailure(str(exc)) from exc
        canonical = self._physical_device(device)
        if device and device.startswith("plughw:"):
            return "plughw:" + canonical.removeprefix("hw:")
        return canonical

    def _resolve_local_pin(self, device: str | None, slot: int):
        if not device:
            return None
        canonical = self._playback_device(device)
        try:
            if canonical.startswith("shiri:device="):
                if self.local_devices is None:
                    raise RuntimeFailure("Local speaker bindings are unavailable; start the audio runtime")
                return self.local_devices.resolve(canonical)
            if canonical.startswith("bluealsa:"):
                # Bluetooth receives admitted PCM descriptors. It does not
                # reserve a Loopback substream or gain ALSA/control access.
                return None
            # Direct physical ALSA names are not durable hardware identity.
            # Only an actual snd_aloop virtual instance has a constrained named
            # exception for synthetic testing and the owned Bluetooth bridge.
            requested = canonical
            wanted = re.fullmatch(r"(?:plug)?hw:CARD=([A-Za-z0-9_-]+),DEV=(\d+),SUBDEV=(\d+)", requested)
            matches = [candidate for candidate in alsa_inventory()
                       if wanted and candidate.card_id == wanted[1]
                       and candidate.device == int(wanted[2]) and candidate.subdevice == int(wanted[3])
                       and candidate.fingerprint and candidate.fingerprint.get("kind") == "virtual"
                       and candidate.fingerprint.get("binding") == "loopback"]
            if len(matches) != 1:
                raise RuntimeFailure("Physical local speakers require a verified device inventory binding; "
                                     "named cards alone can change destination after reconnect")
            return resolve_pcm(matches[0].fingerprint, conversion=requested.startswith("plughw:"))
        except PCMIdentityError as exc:
            raise RuntimeFailure(str(exc)) from exc

    async def _reserve_local_pin(self, room: RuntimeRoom, pin):
        if pin is None:
            return
        async with self.speaker_lock:
            if self.local_node_leases.get(pin.playback_node) not in {None, room.desired.id}:
                raise RuntimeFailure("Local playback device is still owned by another room; retrying after its unit stops")
            self.local_node_leases[pin.playback_node] = room.desired.id
            room.local_pin, room.held_local_node = pin, pin.playback_node

    async def _release_local_pin(self, room: RuntimeRoom):
        released = room.held_local_node
        async with self.speaker_lock:
            if room.held_local_node and self.local_node_leases.get(room.held_local_node) == room.desired.id:
                self.local_node_leases.pop(room.held_local_node)
            room.held_local_node = None
            if room.local_pin:
                room.local_pin.close()
                room.local_pin = None
        if released:
            for waiting in self.rooms.values():
                if waiting is not room and waiting.desired.enabled and waiting.status in {"error", "degraded"}:
                    waiting.wake.set()

    def _material(self, definition: Room):
        return (definition.interface, definition.slot, definition.airplay_name,
                definition.local_audio_device, room_buffer_ms(definition),
                self._output_clocks(definition))

    @staticmethod
    def _output_clocks(definition: Room):
        return tuple(sorted((speaker.id, speaker.airplay_timing) for speaker in definition.speakers
                            if speaker.airplay_timing != "auto"))

    @staticmethod
    def _output_assignment(definition: Room):
        return [speaker.model_dump(exclude={"balance_percent"}) for speaker in definition.speakers]

    async def _reserve_speakers(self, room: RuntimeRoom, speakers: list[SpeakerRef], local_device):
        keys = {speaker_key(speaker, local_device) for speaker in speakers}
        async with self.speaker_lock:
            if any(self.speaker_leases.get(key) not in {None, room.desired.id} for key in keys):
                raise RuntimeFailure("Speaker is still being released by another room; retrying")
            for key in keys:
                self.speaker_leases[key] = room.desired.id
            # Keep old and newly reserved keys until acknowledged readback or a
            # verified process stop. Failed/cancelled HTTP calls can have applied.
            room.held_speakers.update(keys)
        return keys

    async def _commit_speakers(self, room: RuntimeRoom, keys: set[tuple]):
        async with self.speaker_lock:
            released = room.held_speakers - keys
            for key in released:
                if self.speaker_leases.get(key) == room.desired.id:
                    self.speaker_leases.pop(key)
            room.held_speakers = set(keys)
        if released:
            for other in self.rooms.values():
                if other is not room and other.desired.enabled and other.status == "degraded":
                    other.wake.set()

    async def _release_speakers(self, room: RuntimeRoom):
        await self._commit_speakers(room, set())

    def _check_start(self, room: RuntimeRoom, starting: Room):
        if (
            self._closing
            or not room.desired.enabled
            or room.removing
            or self._material(room.desired) != self._material(starting)
            or room.active_timing != room.timing
        ):
            raise Superseded()

    def _require_alive(self, processes: dict, phase: str):
        failed = []
        for name, process in processes.items():
            if not process.alive:
                code = process.process.returncode
                detail = f"exit code {code}" if code is not None else "live process identity unavailable"
                if isinstance(code, int) and code < 0:
                    with suppress(ValueError):
                        detail += f" ({signal.Signals(-code).name})"
                failed.append(f"{name}: {detail}")
        if failed:
            raise RuntimeFailure(f"Audio backend failed {phase}: {'; '.join(failed)}")

    async def _room_loop(self, room: RuntimeRoom):
        while not self._closing:
            await room.wake.wait()
            room.wake.clear()
            if self._closing:
                return
            gain_only = False
            try:
                if not room.desired.enabled or room.removing:
                    await self._stop_room(room)
                    room.status, room.error = "stopped", None
                    room.applied = room.desired
                    if room.removing:
                        return
                    continue
                if not self.ready:
                    room.status, room.error = "error", self.error
                    continue
                applying = room.desired
                backend_definition = room.backend_definition or room.applied
                material_changed = backend_definition and self._material(backend_definition) != self._material(applying)
                if room.client and (material_changed or room.restart_required):
                    await self._stop_room(room)
                if not room.client:
                    applying = room.desired
                    startup = await self._start_room(room)
                    if startup is not None:
                        applying, _master = startup
                else:
                    async with room.control_lock:
                        # Reconcile advances desired intent outside this lock.
                        # Every backend ACK below belongs to this exact snapshot.
                        applying, master = room.desired, room.current_volume
                        self._check_start(room, room.backend_definition or applying)
                        gain_only = bool(room.applied is not None and (room.status == "running" or room.gain_pending)
                                         and self._output_assignment(room.applied) == self._output_assignment(applying))
                        await self._sync_worker_intent(room)
                        if gain_only:
                            await room.client.volume_settings(master, applying.speakers)
                            room.status, room.error = "running", None
                        else:
                            await self._restore_outputs(room, definition=applying, volume=master)
                        await self._receiver_applied_master(room, applying, master)
                if room.status == "running":
                    room.applied = applying
                    room.gain_pending = False
                if room.desired == applying:
                    room.failures, room.retry_at, room.restart_required = 0, 0, False
                else:
                    # Preserve the acknowledged old definition until the newer
                    # edit converges; never label old gains as the newer intent.
                    room.wake.set()
            except Superseded:
                await self._stop_room(room)
                room.wake.set()
            except asyncio.CancelledError:
                await asyncio.shield(self._stop_room(room))
                raise
            except (RuntimeFailure, RpcError, OSError) as exc:
                room.error = str(exc)
                log.exception("Room %s failed", room.desired.id)
                if gain_only:
                    # An ambiguous gain ACK may already have applied. Retain
                    # the live route and lease, then retry the same saved gains.
                    room.gain_pending = True
                    room.status = "degraded"
                else:
                    try:
                        await self._stop_room(room)
                    except (RuntimeFailure, OSError) as cleanup_error:
                        room.error += f"; cleanup pending: {cleanup_error}"
                    room.status = "error"
                room.failures += 1
                room.retry_at = asyncio.get_running_loop().time() + min(60, 2 ** min(room.failures, 6))
            finally:
                room.last_health_at = now()

    def _account(self, owner: str, role: str, slot: int | None = None):
        if self.identities is None:
            raise RuntimeFailure("Managed daemon identities have not been validated")
        return self.identities.account(f"sender.{role}" if owner == "sender" else f"slot{slot}.{role}")

    def bind_policy_helper(self):
        return (self.config.bind_policy_helper
                or (self.config.binary_dir or Path("/opt/shiri")) / "libexec/shiri-bind-policy")

    def pcm_exec_helper(self):
        return (self.config.pcm_exec_helper
                or (self.config.binary_dir or Path("/opt/shiri")) / "libexec/shiri-pcm-exec")

    def _hidden_paths(self):
        # Include configured candidate paths, not only production defaults.
        return tuple(sorted({"/run/dbus/system_bus_socket",
                             str(self.config.api_token_file.parent), str(self.config.daemon_identity_file.parent),
                             str(self.config.api_token_file), str(self.config.daemon_identity_file),
                             str(self.config.state_dir), str(self.config.runtime_dir)}))

    def _environment(self, *, bus=False):
        environment = [f"PYTHONPATH={Path(__file__).resolve().parents[2]}"]
        if self.config.binary_dir:
            prefix = self.config.binary_dir
            environment.append(f"LD_LIBRARY_PATH={prefix}/lib:{prefix}/lib/{os.uname().machine}-linux-gnu")
        if bus:
            environment.append(f"DBUS_SYSTEM_BUS_ADDRESS=unix:path={VIEW}/bus/bus.sock")
        return tuple(environment)

    async def _start_process(self, key: str, name: str, command: list[str], directory: Path,
                             *, account: dict, namespace=None, binds=(), devices=(), ptp=False, bus=False, host_bus=False,
                             listen_port=None, extra_environment=()):
        if self.unit_manager is None:
            raise RuntimeFailure("Privileged daemon launches require the owned system-manager boundary")
        owner = key.partition(":")[0]
        pcm = None
        if devices:
            # The bridge uses only stdlib and system GI; invoking the canonical
            # system interpreter avoids a symlink in the held-executable seam.
            if name == "local-output":
                command = [str(Path(command[0]).resolve(strict=True)), *command[1:]]
            pcm = PCMExec(str(self.pcm_exec_helper()), tuple(command))
            command = list(pcm.command())
        service = UnitSpec(
            new_unit(self.network.installation_tag, owner, name), name,
            account["name"], account["name"], tuple(command),
            namespace=f"/run/netns/{namespace}" if namespace else "",
            binds=tuple(binds) + tuple(Bind(device, device, True) for device in devices),
            devices=tuple(devices), supplementary_groups=("audio",) if devices else (),
            environment=self._environment(bus=bus) + tuple(extra_environment),
            inaccessible=tuple(path for path in self._hidden_paths()
                               if not (host_bus and path == "/run/dbus/system_bus_socket")), ptp=ptp, listen_port=listen_port,
            pcm_exec=pcm,
        )
        return await self.unit_manager.start(key, service, directory / "logs" / f"{name}.log")

    async def _remember_process(self, key: str, process):
        identity = await process.coherent_identity()
        self.network.remember_process(key, process, identity=identity)

    async def _refresh_processes(self, key: str, processes: dict):
        for name, process in processes.items():
            await self._remember_process(f"{key}:{name}", process)

    async def _stop_reserved_units(self, owner: str):
        # A partial launch can fail before the in-memory process map receives
        # its handle. Durable intents must be released before network/outputs.
        for key, entry in reversed(list(self.network.manifest["processes"].items())):
            if key.startswith(owner + ":"):
                await self.runner.stop_saved(entry)
                self.network.forget_process(key)

    def _retire_speech_endpoint(self, room: RuntimeRoom):
        # Caller holds the room lifecycle lock and has proved all exact room
        # units stopped. A socket left by SIGKILL must not block the next
        # OwnTone launch; an unexpected path must never be deleted.
        parent = room.directory / "overlay"
        try:
            parent.lstat()
        except FileNotFoundError:
            return
        if self.network is None:
            raise RuntimeFailure("Speech endpoint retirement requires exact room unit ownership")
        enclosing = room.directory.lstat()
        if (not stat.S_ISDIR(enclosing.st_mode) or enclosing.st_uid != 0
                or stat.S_IMODE(enclosing.st_mode) != 0o700):
            raise RuntimeFailure("Speech endpoint retirement requires the root-private room directory")
        definition = room.backend_definition or room.desired
        output = self._account(definition.id, "output", definition.slot)
        audio = self._account(definition.id, "audio", definition.slot)
        retire_speech_endpoint(parent, output["uid"], audio["gid"])

    async def _start_namespace_services(self, key: str, record: dict, directory: Path, *, slot=None):
        private_directory(directory)
        account = self._account(key, "discovery", slot)
        clients = ([self._account("room", "output", item) for item in range(self.config.max_rooms)]
                   if key == "sender" else [self._account(key, "receiver", slot)])
        discovery_binds = prepare_discovery(directory / "discovery", account, clients, record)
        processes = {}
        try:
            processes["dbus"] = await self._start_process(
                f"{key}:dbus", "dbus",
                [shutil.which("dbus-daemon"), "--config-file", str(VIEW / "config" / "dbus.conf"),
                 "--nofork", "--nopidfile"], directory, account=account,
                namespace=record["namespace"], binds=[discovery_binds[0], Bind(
                    str(directory / "discovery" / "config" / "dbus.conf"), str(VIEW / "config" / "dbus.conf"))],
            )
            await asyncio.sleep(0.2)
            if not processes["dbus"].alive:
                raise RuntimeFailure("Private D-Bus exited during startup")
            admit_bus_socket(directory / "discovery" / "bus" / "bus.sock", account)
            processes["avahi"] = await self._start_process(
                f"{key}:avahi", "avahi",
                [self.binary("avahi-daemon"), "--file", str(VIEW / "config" / "avahi.conf"),
                 "--no-drop-root", "--no-chroot", "--debug"], directory,
                account=account, namespace=record["namespace"], bus=True,
                binds=[*discovery_binds, Bind(str(directory / "discovery" / "config" / "avahi.conf"),
                                            str(VIEW / "config" / "avahi.conf"))],
            )
            await asyncio.sleep(0.5)
            if not processes["avahi"].alive:
                raise RuntimeFailure("Private Avahi exited during startup")
            await self._refresh_processes(key, processes)
            return processes
        except BaseException:
            for name, process in reversed(list(processes.items())):
                await asyncio.shield(process.stop())
                self.network.forget_process(f"{key}:{name}")
            raise

    async def _ensure_sender(self, room: RuntimeRoom):
        async with self.sender_lock:
            self.sender_users.add(room.desired.id)
            if self.sender:
                if self.sender["parent"] != room.desired.interface:
                    raise RuntimeFailure("Another room is using a different sender LAN interface")
                if (
                    set(self.sender_processes) != {"dbus", "avahi", "airptpd"}
                    or not all(process.alive for process in self.sender_processes.values())
                    or not await self.network.healthy(self.sender)
                ):
                    raise RuntimeFailure("Shared sender timing services are recovering")
                return self.sender
            try:
                self.sender = await self.network.create_sender(room.desired.interface)
                directory = self.config.runtime_state_dir / "sender"
                self.sender_processes = await self._start_namespace_services("sender", self.sender, directory)
                self.sender_processes["airptpd"] = await self._start_process(
                    "sender:airptpd",
                    "airptpd",
                    [self.binary("airptpd"), "-f", "-v"], directory,
                    account=self._account("sender", "timing"), namespace=self.sender["namespace"], ptp=True,
                    binds=[Bind(str(private_directory(directory / "shm", self._account("sender", "timing"), mode=0o755)),
                                "/dev/shm", True)],
                )
                await asyncio.sleep(0.4)
                if not self.sender_processes["airptpd"].alive:
                    raise RuntimeFailure("Sender PTP daemon exited during startup")
                await self._remember_process("sender:airptpd", self.sender_processes["airptpd"])
                return self.sender
            except BaseException:
                self.sender_users.discard(room.desired.id)
                await asyncio.shield(self._stop_sender())
                raise

    def _room_password(self, room_id: str):
        return hmac.new(self._password.encode(), room_id.encode(), hashlib.sha256).hexdigest()

    def _worker_socket(self, room: RuntimeRoom, name="audio"):
        return room.worker_sockets.get(name, room.directory / f"{name}.sock")

    async def _worker_rpc(self, room: RuntimeRoom, name, operation, payload, *, timeout=15.0, socket=None):
        if name != 'bluetooth-output':
            return await call_rpc(socket or self._worker_socket(room, name), operation, payload, timeout=timeout)
        directory, generation = room.bluetooth_rpc_directory, room.launch_generation
        if (directory is None or not isinstance(generation, str)
                or re.fullmatch(r'[0-9a-f]{32}', generation) is None or generation == '0' * 32
                or directory.path != (room.directory / 'bridge-state' / generation)):
            raise RuntimeFailure('Bluetooth worker RPC requires this exact live room launch')
        with directory.address('bridge.sock') as address:
            result = await call_rpc(address, operation, payload, timeout=timeout)
            if room.bluetooth_rpc_directory is not directory or room.launch_generation != generation:
                raise RuntimeFailure('Bluetooth worker RPC completed after its exact launch retired')
            return result

    async def _start_bluetooth(self, room, bridge, output, generation):
        from .bluealsa import BlueALSA, HandoffServer
        from .socket_publication import discard, prepare, publish
        if self.bluealsa is None:
            self.bluealsa = BlueALSA(service_uid=0)
        room.bluetooth_admission = await self.bluealsa.admit(
            room.desired.local_audio_device, room.desired.id, generation,
        )
        private_directory(room.directory / "bridge-state")
        state = private_directory(room.directory / "bridge-state" / generation, bridge)
        room.bluetooth_rpc_directory = PinnedUnixDirectory(
            state, uid=bridge['uid'], gid=bridge['gid'], mode=0o700,
        )
        private_directory(room.directory / "bridge-handoff")
        handoff = private_directory(room.directory / "bridge-handoff" / generation,
                                    {"uid": 0, "gid": bridge["gid"]}, mode=0o750)
        room.bluetooth_handoff = HandoffServer(handoff / "pcm.sock", room.bluetooth_admission, bridge["gid"])
        room.worker_sockets["bluetooth-output"] = state / "bridge.sock"
        process = await self._start_process(
            f"{room.desired.id}:bluetooth-output", "bluetooth-output",
            [sys.executable, "-m", "shiri.runtime.bluetooth_output",
             "--input", str(VIEW / "state" / "final-pcm.sock"),
             "--handoff", str(VIEW / "handoff" / "pcm.sock"), "--room-id", room.desired.id,
             "--generation", generation, "--output-uid", str(output["uid"]),
             "--socket", str(VIEW / "state" / "bridge.sock")], room.directory,
            account=bridge, binds=[Bind(str(state), str(VIEW / "state"), True),
                                   Bind(str(handoff), str(VIEW / "handoff"))],
        )
        room.processes["bluetooth-output"] = process
        room.bluetooth_handoff.authorize(bridge["uid"], process.process.pid)
        await room.bluetooth_handoff.wait_delivered(timeout=3)
        await self._wait_worker(room, "bluetooth-output", self._worker_socket(room, "bluetooth-output"))
        private_directory(room.directory / "bridge-published")
        record, listener_fd, directory_fd = prepare(
            state / "final-pcm.sock", room.directory / "bridge-published" / generation,
            uid=bridge["uid"], initial_gid=bridge["gid"], gid=output["gid"],
        )
        try:
            process.entry["socket_publication"] = record
            # Persist the inode and destination before rename. A SIGKILL at
            # either side of publication leaves exact recovery authority.
            try:
                await self._remember_process(f"{room.desired.id}:bluetooth-output", process)
            except BaseException:
                # The listener has not moved; discard this new empty root dir.
                discard(record)
                raise
            socket = publish(state / "final-pcm.sock", record, listener_fd, directory_fd)
        finally:
            os.close(listener_fd)
            os.close(directory_fd)
        return Bind(str(socket), str(VIEW / "bridge" / "final-pcm.sock"))

    async def _start_room(self, room: RuntimeRoom):
        starting = room.desired
        local_device = self._playback_device(starting.local_audio_device)
        bluetooth = bool(local_device and local_device.startswith("bluealsa:"))
        if bluetooth and not _OWNTONE_VERSION_PATTERN.search(self.versions.get("owntone", "")):
            raise RuntimeFailure("Bluetooth output requires the reviewed framed backend; the host-bus bridge is disabled")
        pin = self._resolve_local_pin(starting.local_audio_device, starting.slot)
        room.status, room.error = "starting", None
        try:
            if (room.processes or room.receiver or room.reserved_slot is not None or room.local_pin
                    or room.bluetooth_admission):
                await self._stop_room(room)
            await self.slot_locks[starting.slot].acquire()
            room.reserved_slot = starting.slot
            async with room.control_lock:
                # Recovery may already have removed the durable process map,
                # leaving no in-memory handles but an old filesystem socket.
                await self._stop_reserved_units(starting.id)
                self._retire_speech_endpoint(room)
            await self._reserve_local_pin(room, pin)
        except BaseException:
            if pin and room.local_pin is not pin:
                pin.close()
            raise
        room.backend_definition = starting
        room.active_timing = room.timing
        output_buffer_ms, relay_delay_ms = room.active_timing
        room.active_local_device = (local_device if local_device and local_device.startswith("bluealsa:")
                                    else pin.pcm if pin else local_device)
        self._check_start(room, starting)
        interfaces = await self.network.interfaces()
        candidate = next((item for item in interfaces if item["name"] == starting.interface), None)
        if not candidate or not candidate["eligible"]:
            raise RuntimeFailure(
                candidate["reason"] if candidate else "The configured LAN interface is unavailable"
            )
        sender = await self._ensure_sender(room)
        self._check_start(room, starting)
        room.receiver = await self.network.create_receiver(starting)
        self._check_start(room, starting)
        audio = self._account(starting.id, "audio", starting.slot)
        receiver = self._account(starting.id, "receiver", starting.slot)
        output = self._account(starting.id, "output", starting.slot)
        timing = self._account(starting.id, "timing", starting.slot)
        room.launch_generation = uuid4().hex
        generation = room.launch_generation
        endpoint = None
        bluetooth_bind = None
        if bluetooth:
            bridge = self._account(starting.id, "bridge", starting.slot)
            bluetooth_bind = await self._start_bluetooth(room, bridge, output, generation)
            self._check_start(room, starting)
            endpoint = room.bluetooth_admission.endpoint
        password = self._room_password(starting.id)
        own_url = f"http://{sender['api_ip']}:{3869 + starting.slot * 10}"
        shairport, owntone = backend_configs(
            starting.model_copy(update={"local_audio_device": "shiri" if pin else local_device if bluetooth else None}),
            room.directory, room.receiver, all_receiver_names=sorted(self._receiver_names()), password=password,
            view_directory=VIEW, output_state_directory=VIEW / "state", own_username=output["name"],
            audio_uid=audio["uid"], music_socket=VIEW / "input" / "music.sock",
            output_buffer_ms=output_buffer_ms,
            speech_output={"socket": VIEW / "overlay" / "speech.sock", "peer_uid": audio["uid"],
                           "room_id": starting.id, "launch_generation": generation},
            pcm_identity_file=VIEW / "credentials" / "pcm-identity.json" if pin else None,
            **({"framed_output": {"socket": VIEW / "bridge" / "final-pcm.sock", "peer_uid": bridge["uid"],
                                  "room_id": starting.id, "launch_generation": generation,
                                  "rate": endpoint.rate, "channels": endpoint.channels,
                                  "format_code": endpoint.format_code}} if bluetooth else {}),
        )
        prepare_room_view(room.directory, audio, receiver, output)
        output_binds, output_environment = [bluetooth_bind] if bluetooth_bind else [], []
        if pin:
            from .alsa_configuration import render_pcm_config
            identity = room.directory / "credentials" / "pcm-identity.json"
            pin.validate()
            atomic_json(identity, pin.manifest)
            file_owner(identity, output)
            alsa = room.directory / "config" / "alsa.conf"
            alsa.write_text(render_pcm_config(pin, conversion=pin.pcm.startswith("plughw:")), encoding="utf-8")
            file_owner(alsa, output)
            output_binds = [Bind(str(identity), str(VIEW / "credentials" / "pcm-identity.json")),
                            Bind(str(alsa), str(VIEW / "config" / "alsa.conf"))]
            output_environment = [f"ALSA_CONFIG_PATH={VIEW}/config/alsa.conf"]
        signal_account = {"uid": 0, "gid": audio["gid"]}
        signals = private_directory(room.directory / "signals", signal_account, mode=0o750)

        async def dispatch_signal(operation, payload):
            if room.launch_generation != generation:
                raise RpcError("stale_launch", "This room audio launch has ended")
            if operation != "native-volume":
                raise RpcError("unknown_operation", "This socket only accepts this room's native volume events")
            try:
                return await self._native_volume(room, payload)
            except (ValidationError, ValueError, TypeError) as exc:
                raise RpcError("invalid_request", "Native volume request is malformed") from exc
            except RuntimeFailure as exc:
                raise RpcError("backend_unavailable", str(exc)) from exc

        room.signal_server = await serve_rpc(
            signals / "events.sock", dispatch_signal, mode=0o660,
            allowed_uids={0, audio["uid"]}, socket_gid=audio["gid"],
        )
        credential = room.directory / "credentials" / "owntone.json"
        atomic_json(credential, {"password": password})
        file_owner(credential, audio)
        room.worker_sockets["audio"] = room.directory / "control" / "audio.sock"
        room.processes.update(await self._start_namespace_services(
            starting.id, room.receiver, room.directory, slot=starting.slot,
        ))
        room_shm = private_directory(room.directory / "shm", timing, mode=0o755)
        room.processes["nqptp"] = await self._start_process(
            f"{starting.id}:nqptp", "nqptp", [self.binary("nqptp"), "-v"], room.directory,
            account=timing, namespace=room.receiver["namespace"], ptp=True,
            binds=[Bind(str(room_shm), "/dev/shm", True)],
        )
        await asyncio.sleep(0.2)
        self._check_start(room, starting)
        room.processes["owntone"] = await self._start_process(
            f"{starting.id}:owntone", "owntone",
            [self.binary("owntone"), "-f", "-c", str(VIEW / "config" / "owntone.conf"),
             "--mdns-no-rsp", "--mdns-no-daap", "--mdns-no-web", "--mdns-no-cname"], room.directory,
            account=output, namespace=sender["namespace"],
            devices=[pin.playback_node, f"/dev/snd/controlC{pin.manifest['card_index']}"] if pin else (), bus=True,
            extra_environment=output_environment,
            listen_port=3869 + starting.slot * 10,
            binds=[Bind(str(owntone), str(VIEW / "config" / "owntone.conf")),
                   Bind(str(room.directory / "output"), str(VIEW / "state"), True),
                   Bind(str(room.directory / "pipes"), str(VIEW / "pipes"), True),
                   Bind(str(room.directory / "overlay"), str(VIEW / "overlay"), True),
                   Bind(str(self.config.runtime_state_dir / "sender" / "discovery" / "bus"), str(VIEW / "bus")),
                   Bind(str(self.config.runtime_state_dir / "sender" / "shm"), "/dev/shm"), *output_binds],
        )
        if bluetooth:
            await self._worker_rpc(room, "bluetooth-output", "authorize-peer",
                           {"pid": room.processes["owntone"].process.pid}, timeout=2)
            self._check_start(room, starting)
        room.client = OwnToneClient(own_url, password=password)
        deadline = asyncio.get_running_loop().time() + 30
        last_error = "No control response"
        while asyncio.get_running_loop().time() < deadline:
            self._check_start(room, starting)
            self._require_alive(room.processes, "while waiting for OwnTone")
            try:
                room.player = await room.client.request("GET", "/api/player")
                break
            except RuntimeFailure as exc:
                last_error = str(exc)
                await asyncio.sleep(0.5)
        else:
            raise RuntimeFailure(f"OwnTone did not become ready within its startup deadline: {last_error}")
        await room.client.volume(room.current_volume)
        room.processes["audio"] = await self._start_process(
            f"{starting.id}:audio", "audio",
            [sys.executable, "-m", "shiri.runtime.audio", "--room-dir", str(VIEW),
             "--socket", str(VIEW / "control" / "audio.sock"), "--room-id", starting.id,
             "--native-uid", str(receiver["uid"]), "--native-socket", str(VIEW / "input" / "music.sock"),
             "--own-url", own_url, "--own-password-file", str(VIEW / "credentials" / "owntone.json"),
             "--speech-socket", str(VIEW / "overlay" / "speech.sock"), "--speech-launch-generation", generation,
             "--output-uid", str(output["uid"]), "--output-buffer-ms", str(output_buffer_ms),
             "--relay-delay-ms", str(relay_delay_ms),
             "--signal-socket", str(VIEW / "signals" / "events.sock"),
             "--signal-generation", generation, "--control-revision", str(starting.revision), "--control-volume", str(room.current_volume)],
            room.directory, account=audio, binds=[
                Bind(str(room.directory / "pipes"), str(VIEW / "pipes"), True),
                Bind(str(room.directory / "control"), str(VIEW / "control"), True),
                Bind(str(room.directory / "input"), str(VIEW / "input"), True),
                Bind(str(room.directory / "overlay"), str(VIEW / "overlay")),
                Bind(str(credential), str(VIEW / "credentials" / "owntone.json")),
                Bind(str(signals), str(VIEW / "signals")),
            ],
        )
        self._check_start(room, starting)
        await self._wait_worker(room, "audio", self._worker_socket(room))
        async with room.control_lock:
            self._check_start(room, starting)
            prepared, master = await self._finish_start_outputs(room)
            self._check_start(room, starting)
            if room.desired != prepared or room.current_volume != master:
                raise Superseded()
            if room.status != "running":
                raise RuntimeFailure(room.error or "Configured speaker group is unavailable before receiver startup")
            # Empty saved routes remain available for setup/discovery. Native
            # ARM already refuses music until at least one output is ready.
            # Configured groups must be fully ACKed before a phone can connect.
            room.status, room.error = "starting", None
        room.processes["shairport"] = await self._start_process(
            f"{starting.id}:shairport", "shairport",
            [self.binary("shairport-sync"), "-v", "-c", str(VIEW / "config" / "shairport.conf")],
            room.directory, account=receiver, namespace=room.receiver["namespace"], bus=True,
            binds=[Bind(str(shairport), str(VIEW / "config" / "shairport.conf")),
                   Bind(str(room.directory / "input"), str(VIEW / "input")),
                   Bind(str(room.directory / "metadata"), str(VIEW / "metadata"), True),
                   Bind(str(room.directory / "discovery" / "bus"), str(VIEW / "bus")),
                   Bind(str(room_shm), "/dev/shm")],
        )
        from .receiver_readiness import wait_receiver_ready
        await wait_receiver_ready(room.processes["shairport"], room.receiver, receiver)
        self._check_start(room, starting)
        self._require_alive(room.processes, "after starting the AirPlay receiver")
        await self._refresh_processes(starting.id, room.processes)
        async with room.control_lock:
            self._check_start(room, starting)
            applied = await self._finish_start_outputs(room)
        self._check_start(room, starting)
        room.last_health_at = now()
        return applied

    async def _finish_start_outputs(self, room: RuntimeRoom):
        # Material startup checks permit newer administrative/gain intent.
        # Return exactly the definition acknowledged by final selection, so
        # the outer actor cannot label it with the pre-start revision.
        definition, volume = room.desired, room.current_volume
        await self._sync_worker_intent(room)
        await self._restore_outputs(room, definition=definition, volume=volume)
        await self._receiver_applied_master(room, definition, volume)
        return definition, volume

    async def _wait_worker(self, room: RuntimeRoom, name: str, socket: Path):
        for _ in range(20):
            try:
                health = await self._worker_rpc(room, name, "health", {}, timeout=1, socket=socket)
                if health.get("ready") is False or health.get("error"):
                    raise RuntimeFailure(health.get("error") or f"Room {name} is not ready")
                return
            except RpcError as exc:
                if not room.processes[name].alive:
                    raise RuntimeFailure(f"Room {name} worker exited during startup") from exc
                await asyncio.sleep(0.1)
        raise RuntimeFailure(f"Room {name} worker did not become ready")

    async def _restore_outputs(self, room: RuntimeRoom, *, definition: Room | None = None, volume: int | None = None):
        # Startup may use the latest intent after its material startup checks.
        # Snapshot both gains and assignment before discovery can yield.
        definition = definition or room.desired
        volume = room.current_volume if volume is None else volume
        speakers = list(definition.speakers)
        backend_definition = room.backend_definition or definition
        if self._output_clocks(definition) != self._output_clocks(backend_definition):
            raise Superseded()
        await self._retire_speech_streams(room)
        room.outputs = await room.client.outputs(self._receiver_names())
        try:
            keys = await self._reserve_speakers(
                room, speakers, room.active_local_device or backend_definition.local_audio_device
            )
            await room.client.volume_settings(volume, speakers)
            await room.client.select(speakers, room.outputs)
            await self._commit_speakers(room, keys)
            room.selected_ids = [speaker.id for speaker in speakers]
            room.status, room.error = "running", None
        except RuntimeFailure as exc:
            room.status, room.error = "degraded", str(exc)
            # Never select a different device or silently retain an old room
            # assignment when the complete requested group is unavailable.
            await room.client.select([], room.outputs)
            await self._release_speakers(room)
            room.selected_ids = []

    async def _stop_sender(self):
        for name, process in reversed(list(self.sender_processes.items())):
            await process.stop()
            self.network.forget_process(f"sender:{name}")
            self.sender_processes.pop(name, None)
        if self.network:
            await self._stop_reserved_units("sender")
            await self.network.remove("sender")
        self.sender = None

    async def _stop_room(self, room: RuntimeRoom):
        async with room.control_lock:
            await self._stop_room_locked(room)

    async def _stop_room_locked(self, room: RuntimeRoom):
        room.gain_pending = False
        if room.processes or room.receiver or room.client or room.bluetooth_admission:
            room.status = "stopping"
        room.launch_generation = None
        # Stopping the exact processes below is the final backstop when a
        # broken stream cannot acknowledge retirement.
        await self._retire_speech_streams(room, stopping=True)
        for session_id, room_id in list(self.sessions.items()):
            if room_id == room.desired.id:
                self._forget_session(session_id)
                self.pending_sessions.discard(session_id)
        if room.signal_server:
            room.signal_server.close()
            await room.signal_server.wait_closed()
            room.signal_server = None
        if room.bluetooth_handoff:
            room.bluetooth_handoff.close()
        for name, process in reversed(list(room.processes.items())):
            await process.stop()
            self.network.forget_process(f"{room.desired.id}:{name}")
            room.processes.pop(name, None)
        if self.network:
            await self._stop_reserved_units(room.desired.id)
        room.speech_streams.clear()
        if room.bluetooth_rpc_directory is not None:
            room.bluetooth_rpc_directory.close()
            room.bluetooth_rpc_directory = None
        self._retire_speech_endpoint(room)
        if room.bluetooth_admission:
            await room.bluetooth_admission.close(verified_unit_stopped=True)
            room.bluetooth_admission = None
        room.bluetooth_handoff = None
        room.worker_sockets.clear()
        await self._release_local_pin(room)
        if room.client:
            await room.client.close()
            room.client = None
        # Every room daemon has exited, including its OwnTone output sessions.
        # Network cleanup may still fail, but the physical outputs are released.
        await self._release_speakers(room)
        room.backend_definition = None
        room.active_local_device = None
        if self.network:
            await self.network.remove(f"receiver:{room.desired.id}")
        room.receiver, room.selected_ids = None, []
        async with self.sender_lock:
            self.sender_users.discard(room.desired.id)
            if not self.sender_users and self.network:
                await self._stop_sender()
        if room.reserved_slot is not None:
            self.slot_locks[room.reserved_slot].release()
            room.reserved_slot = None

    async def set_outputs(self, room: RuntimeRoom, speakers: list[SpeakerRef]):
        client = self._client(room)
        keys = [self._speaker_key(room.desired, speaker) for speaker in speakers]
        if len(keys) != len(set(keys)):
            raise RpcError("invalid_request", "Speaker identities cannot be duplicated")
        for other in self.rooms.values():
            if other.desired.id != room.desired.id:
                owned = {self._speaker_key(other.desired, speaker) for speaker in other.desired.speakers}
                if owned.intersection(keys):
                    raise RpcError("speaker_conflict", "Speaker is assigned to another room")
        async with room.control_lock:
            if room.client is not client:
                raise RpcError("room_not_running", "The room changed while applying speakers")
            self._client(room)
            proposed = room.desired.model_copy(update={"speakers": speakers})
            if self._output_clocks(proposed) != self._output_clocks(room.backend_definition or room.desired):
                raise RpcError("configuration_requires_reconcile",
                               "Save this speaker configuration so the room can restart with its selected clock settings")
            proposed_plan = latency_plan([proposed if other is room else other.desired
                                          for other in self.rooms.values() if not other.removing])
            if (room_buffer_ms(proposed), proposed_plan.common_horizon_ms) != room.timing:
                raise RpcError("configuration_requires_reconcile",
                               "Save this speaker configuration so all grouped zones can update their timing together")
            if transport_fingerprint(proposed) != transport_fingerprint(room.desired):
                await self._retire_speech_streams(room)
            outputs = await client.outputs(self._receiver_names())
            try:
                definition = room.backend_definition or room.desired
                leases = await self._reserve_speakers(
                    room, speakers, room.active_local_device or definition.local_audio_device
                )
                await client.volume_settings(room.current_volume, speakers)
                result = await client.select(speakers, outputs)
                await self._commit_speakers(room, leases)
            except (RuntimeFailure, asyncio.CancelledError) as exc:
                room.status, room.error = (
                    "degraded",
                    str(exc) or "Speaker update interrupted; restoring saved configuration",
                )
                room.wake.set()
                raise
            room.selected_ids = [speaker.id for speaker in speakers]
            room.desired = room.desired.model_copy(update={"speakers": speakers})
            room.outputs = await client.outputs(self._receiver_names())
            room.status, room.error = "running", None
        return {**result, "selected_ids": room.selected_ids}

    async def _sync_worker_intent(self, room: RuntimeRoom):
        if room.launch_generation and "audio" in room.processes:
            await call_rpc(self._worker_socket(room), "control-intent",
                           {"revision": room.desired.revision}, timeout=2)

    def _native_receipt(self, room, event_id, fingerprint, accepted):
        previous = dict(room.native_volume_receipts)
        room.native_volume_receipts[event_id] = {"fingerprint": fingerprint, "accepted": accepted}
        while len(room.native_volume_receipts) > 256:
            room.native_volume_receipts.pop(next(iter(room.native_volume_receipts)))
        try:
            self._save_phone_volume(room)
        except (OSError, RuntimeFailure):
            room.native_volume_receipts = previous
            raise
        return {"ok": True, "durable": True, "acknowledged": True, "event_id": str(UUID(hex=event_id)), "accepted": accepted}

    async def _native_volume(self, room: RuntimeRoom, payload: dict):
        if (set(payload) != {"launch_generation", "event_id", "base_revision", "token", "generation", "volume"}
                or payload.get("launch_generation") != room.launch_generation or not room.launch_generation):
            raise RpcError("stale_launch", "Native volume does not belong to this exact room launch")
        token = SourceToken.model_validate(payload["token"])
        if token.zone_id != room.desired.id:
            raise RpcError("forbidden", "Native volume belongs to another room")
        event_id = UUID(payload["event_id"]).hex
        generation, base, volume = payload["generation"], payload["base_revision"], payload["volume"]
        if (type(generation) is not int or not 1 <= generation <= 2**63 - 1
                or type(base) is not int or base < 1 or type(volume) is not int or not 0 <= volume <= 100
                or token.epoch > 2**63 - 1):
            raise RpcError("invalid_request", "Native volume identity or bounds are invalid")
        body = {"incarnation": UUID(token.incarnation).hex, "session_id": UUID(token.session_id).hex,
                "epoch": token.epoch, "generation": generation, "volume": volume}
        if body["session_id"] == "0" * 32:
            raise RpcError("invalid_request", "Native volume needs a nonzero admitted session")
        fingerprint = hashlib.sha256(json.dumps(
            {"body": body, "base_revision": base}, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        async with room.control_lock:
            saved = room.native_volume_receipts.get(event_id)
            if saved:
                if saved["fingerprint"] != fingerprint:
                    raise RpcError("conflict", "Native event identity was reused for different data")
                return {"ok": True, "durable": True, "acknowledged": True, "event_id": str(UUID(hex=event_id)),
                        "accepted": saved["accepted"]}
            if base != room.desired.revision or not room.desired.enabled or not room.client:
                return self._native_receipt(room, event_id, fingerprint, False)
            try:
                ack = await room.client.request("POST", "/api/player/shiri-volume", json=body)
            except OwnToneRejected as exc:
                if exc.status_code == 409:
                    return self._native_receipt(room, event_id, fingerprint, False)
                raise
            if ack != body:
                raise RuntimeFailure("OwnTone did not acknowledge the exact native volume identity")
            # An API reconciliation can advance intent during the bounded HTTP
            # await. Its actor will restore that newer intent after this lock.
            if base != room.desired.revision:
                room.wake.set()
                return self._native_receipt(room, event_id, fingerprint, False)
            old_pending, old_next, old_volume = (room.phone_volume_update, room.phone_volume_next, room.current_volume)
            update = {"id": event_id, "volume": volume, "base_revision": base}
            if room.phone_volume_update:
                room.phone_volume_next = update
            else:
                room.phone_volume_update = update
            room.current_volume = volume
            try:
                return self._native_receipt(room, event_id, fingerprint, True)
            except (OSError, RuntimeFailure):
                room.phone_volume_update, room.phone_volume_next, room.current_volume = old_pending, old_next, old_volume
                raise

    async def _receiver_applied_master(self, room: RuntimeRoom, definition: Room, volume: int):
        # A failed selection or superseded HTTP ACK cannot publish feedback
        # under a newer worker revision or overwrite the receiver's defaults.
        if room.status != "running" or room.desired != definition or room.current_volume != volume:
            return
        if room.applied == definition and room.applied.volume == volume:
            return  # An identical wake must not erase a pending sender notification.
        notify = bool(room.applied is not None and room.applied.volume != definition.volume
                      and room.phone_volume_revision != definition.revision)
        await self._receiver_master(room, notify=notify)

    async def _receiver_master(self, room: RuntimeRoom, *, notify: bool):
        # Feedback is diagnostic and cannot fail an acknowledged gain change
        # or tear down the running music route. The worker coalesces edits and
        # reports bounded event-channel failures separately in its health.
        if not room.launch_generation or "audio" not in room.processes:
            return
        try:
            await call_rpc(self._worker_socket(room), "receiver-master",
                           {"revision": room.desired.revision, "volume": room.current_volume,
                            "notify": notify}, timeout=1)
        except (RpcError, OSError, TimeoutError):
            room.receiver_volume = {"status": "worker_control_unavailable",
                                    "revision": room.desired.revision, "volume": room.current_volume}

    async def set_volume(self, room: RuntimeRoom, value, *, pending=False):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
            raise RpcError("invalid_request", "Volume must be an integer from 0 to 100")
        if not pending:
            self._client(room)
        async with room.control_lock:
            if not pending:
                await self._sync_worker_intent(room)
            if pending:
                update = {"id": uuid4().hex, "volume": value, "base_revision": room.desired.revision}
                # Keep the event being committed stable until acknowledged.
                # Subsequent slider moves coalesce into one durable next event.
                if room.phone_volume_update:
                    room.phone_volume_next = update
                else:
                    room.phone_volume_update = update
                self._save_phone_volume(room)
                room.current_volume = value
            if room.client:
                await room.client.volume(value)
            elif not pending:
                raise RpcError("room_not_running", "The room has stopped")
            changed = room.current_volume != value
            room.current_volume = value
            if not pending:
                await self._receiver_master(room, notify=changed)
        return {"ok": True, "volume": value, "pending": not bool(room.client)}

    def _save_phone_volume(self, room):
        update = room.phone_volume_update
        if update and room.phone_volume_next:
            update = {**update, "next": room.phone_volume_next}
        if room.native_volume_receipts:
            update = {"version": 2, "pending": room.phone_volume_update,
                      "next": room.phone_volume_next, "native_receipts": room.native_volume_receipts}
        atomic_json(room.directory / "phone-volume.json", update)

    async def ack_phone_volume(self, room: RuntimeRoom, payload: dict):
        accepted, volume = payload.get("accepted"), payload.get("volume")
        revision = payload.get("committed_revision")
        if type(accepted) is not bool or type(volume) is not int or not 0 <= volume <= 100:
            raise RpcError(
                "invalid_request", "A phone volume acknowledgment needs accepted and the stored volume"
            )
        async with room.control_lock:
            update = room.phone_volume_update
            if not update or update["id"] != payload.get("update_id"):
                return {"ok": True, "stale": True}
            if accepted and volume != update["volume"]:
                raise RpcError("invalid_request", "Accepted phone volume does not match the durable update")
            if revision is not None and (type(revision) is not int or revision < 1):
                raise RpcError("invalid_request", "Stored room revision must be a positive integer")
            if accepted and room.phone_volume_next and revision is None:
                raise RpcError("invalid_request", "Queued phone volume needs the committed room revision")
            if not accepted:
                room.current_volume = volume
                if room.client:
                    await room.client.volume(volume)
            if revision is not None and revision >= room.desired.revision:
                room.desired = room.desired.model_copy(update={"volume": volume, "revision": revision})
                await self._sync_worker_intent(room)
                if accepted:
                    room.phone_volume_revision = revision
                await self._receiver_master(room, notify=not accepted)
            room.phone_volume_update = room.phone_volume_next if accepted else None
            room.phone_volume_next = None
            if room.phone_volume_update:
                room.phone_volume_update["base_revision"] = max(
                    room.phone_volume_update["base_revision"], revision
                )
            self._save_phone_volume(room)
            return {"ok": True, "stale": False}

    def _forget_session(self, session_id, generation=None):
        if generation is not None and self.session_generations.get(session_id) is not generation:
            return
        self.sessions.pop(session_id, None)
        self.session_generations.pop(session_id, None)

    async def _retire_speech_streams(self, room, *, stopping=False):
        outcomes = await asyncio.gather(*(close() for close in tuple(room.speech_streams.values())),
                                        return_exceptions=True)
        if not stopping and any(isinstance(result, BaseException) for result in outcomes):
            raise RuntimeFailure("Speech retirement is unconfirmed; retain the current output assignment")

    async def open_speech(self, room, payload):
        """Freeze one routing decision, then relay bounded PCM without control RPCs."""
        session_id = payload.get("session_id")
        if (not isinstance(session_id, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", session_id)):
            raise RpcError("invalid_request", "A bounded speech session id is required")
        async with room.control_lock:
            if session_id in self.sessions:
                raise RpcError("session_conflict", "Speech session already has an admitted owner")
            if len(self.sessions) >= 32:
                raise RpcError("session_limit", "Too many retained speech sessions")
            client = self._client(room)
            launch = room.launch_generation
            fingerprint = transport_fingerprint(room.desired)
            checked_definition = room.desired
            selected = frozenset(room.selected_ids)
            if (not room.desired.enabled or not launch or not selected
                    or selected != {speaker.id for speaker in room.desired.speakers}
                    or payload.get("transport_fingerprint") != fingerprint):
                raise RpcError("session_conflict", "The admitted speech room routing has changed")
            generation = object()
            self.sessions[session_id] = room.desired.id
            self.session_generations[session_id] = generation
            self.pending_sessions.add(session_id)
            downstream = None
            admission_refused = False

            def validate():
                nonlocal checked_definition
                # Room intent is replaced as a complete model. Hash only a new
                # definition, never PCM frames on a stable admitted route.
                if room.desired is not checked_definition:
                    if transport_fingerprint(room.desired) != fingerprint:
                        raise RpcError("session_conflict", "The admitted speech room routing has retired")
                    checked_definition = room.desired
                if (self._closing or self.rooms.get(room.desired.id) is not room or room.removing
                        or not room.desired.enabled or room.client is not client
                        or room.launch_generation != launch
                        or frozenset(room.selected_ids) != selected
                        or self.session_generations.get(session_id) is not generation):
                    raise RpcError("session_conflict", "The admitted speech room or launch has retired")

            async def close():
                if downstream is None and not admission_refused:
                    # The worker may own BEGIN even when its initial reply was
                    # lost. Keep routing fenced until this exact launch stops;
                    # never send cancellation to an unidentified future voice.
                    raise RpcError("audio_unavailable", "Speech admission retirement is unconfirmed")
                result = await downstream.close() if downstream is not None else None
                # Keep an unconfirmed retirement registered until this launch
                # stops. A later output edit must not erase that uncertainty.
                if room.speech_streams.get(session_id) is close:
                    room.speech_streams.pop(session_id)
                if self.session_generations.get(session_id) is generation:
                    self._forget_session(session_id, generation)
                    self.pending_sessions.discard(session_id)
                return result

            # Admission itself can acquire a voice before its reply arrives.
            # Register its retirement owner before that first network await.
            room.speech_streams[session_id] = close
            try:
                message = {key: value for key, value in payload.items()
                           if key not in {"room_id", "transport_fingerprint"}}
                message["duck_gain"] = room.desired.duck_gain
                downstream = await open_speech(self._worker_socket(room), message)
                validate()
                prepared = {**downstream.prepared, "launch_generation": launch,
                            "transport_fingerprint": fingerprint, "admitted_room_id": room.desired.id}
                return StreamReply(prepared,
                                   lambda reader, writer: serve_speech(reader, writer, downstream,
                                                                      validate=validate), close)
            except BaseException as exc:
                admission_refused = isinstance(exc, AdmissionRefused)
                if downstream is not None or admission_refused:
                    await close()
                raise

    async def speech(self, room: RuntimeRoom, payload: dict):
        if payload.get("action", "offer") not in {"offer", "control", "close"}:
            raise RpcError("invalid_request", "Speech control supports offer, control, or close")
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", session_id
        ):
            raise RpcError("invalid_request", "A bounded speech session id is required")
        previous = self.sessions.get(session_id)
        if previous is not None and previous != room.desired.id:
            raise RpcError("session_conflict", "Speech session belongs to another room")
        if session_id in room.speech_streams:
            # A refused RTC request must not rewrite a binary stream's
            # generation and indirectly revoke its next PCM frame.
            raise RpcError("session_conflict", "Generated speech is owned by its admitted binary connection")
        if payload.get("action") == "close" and not room.processes.get("audio"):
            self._forget_session(session_id)
            return {"ok": True, "closed": True}
        if payload.get("action") != "close":
            self._client(room)
            if not room.selected_ids:
                raise RpcError("no_speakers", "Assign available speakers before sending speech")
        if previous is None and len(self.sessions) >= 32:
            raise RpcError("session_limit", "Too many retained speech sessions")
        message = {key: value for key, value in payload.items() if key != "room_id"}
        message["duck_gain"] = room.desired.duck_gain
        generation = self.session_generations.get(session_id)
        if payload.get("action", "offer") in {"offer", "close"} or generation is None:
            generation = object()
        self.session_generations[session_id] = generation
        self.sessions[session_id] = room.desired.id
        operation = object()
        self._session_operations.setdefault(session_id, set()).add(operation)
        self.pending_sessions.add(session_id)
        try:
            result = await call_rpc(self._worker_socket(room), "speech", message, timeout=20)
        except BaseException:
            if previous is None:
                self._forget_session(session_id, generation)
            raise
        finally:
            operations = self._session_operations.get(session_id)
            if operations is not None:
                operations.discard(operation)
                if not operations:
                    self._session_operations.pop(session_id, None)
                    self.pending_sessions.discard(session_id)
        if payload.get("action") == "close":
            self._forget_session(session_id, generation)
        return result

    async def warm(self, room: RuntimeRoom, payload: dict):
        if payload.get("action") not in {"acquire", "release", "observe"}:
            raise RpcError("invalid_request", "Unknown room warm action")
        lease_id = payload.get("lease_id")
        if not isinstance(lease_id, str) or not re.fullmatch(r"[0-9a-f]{32}", lease_id):
            raise RpcError("invalid_request", "An exact room warm lease ID is required")
        keys = {"room_id", "action", "lease_id", "transport_fingerprint", "launch_generation"}
        if payload["action"] == "acquire":
            keys.add("deadline_monotonic_ns")
        if (set(payload) != keys or not isinstance(payload.get("transport_fingerprint"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", payload["transport_fingerprint"])):
            raise RpcError("invalid_request", "Room warm control requires only its exact routing and expiry fields")
        if payload["action"] == "acquire":
            deadline = payload["deadline_monotonic_ns"]
            now = time.monotonic_ns()
            if type(deadline) is not int or not now < deadline <= now+300_000_000_000:
                raise RpcError("invalid_request", "Room warm expiry must be within five minutes")
        expected_launch = payload.get("launch_generation")
        if expected_launch is not None and (not isinstance(expected_launch, str)
                or not re.fullmatch(r"[0-9a-f]{32}", expected_launch)):
            raise RpcError("invalid_request", "An exact room launch generation is required")
        async with room.control_lock:
            generation = room.launch_generation
            if expected_launch is not None and expected_launch != generation:
                raise RpcError("session_conflict", "The warmed room launch has changed")
            if payload.get("action") == "release" and not room.processes.get("audio"):
                return {"state": "released", "launch_generation": generation}
            if payload.get("action") != "release":
                self._client(room)
                if (not room.desired.enabled or not room.selected_ids
                        or payload.get("transport_fingerprint") != transport_fingerprint(room.desired)):
                    raise RpcError("session_conflict", "The warmed room routing has changed")
            message = {key: value for key, value in payload.items()
                       if key not in {"room_id", "transport_fingerprint", "launch_generation"}}
            result = await call_rpc(self._worker_socket(room), "warm", message, timeout=8)
            if room.launch_generation != generation:
                raise RpcError("session_conflict", "Room launch changed during warming")
            return {**result, "launch_generation": generation}

    async def _health_monitor(self):
        while not self._closing:
            await asyncio.sleep(5)
            sender_healthy = True
            sender = self.sender
            if sender:
                try:
                    sender_healthy = all(
                        process.alive for process in self.sender_processes.values()
                    ) and await self.network.healthy(sender)
                except RuntimeFailure:
                    sender_healthy = False
            if not sender_healthy and self.sender is sender:
                for room in list(self.rooms.values()):
                    if room.desired.enabled:
                        room.restart_required = True
                        room.wake.set()
            await asyncio.gather(*(self._probe_room(room) for room in list(self.rooms.values())))

    async def _probe_room(self, room):
        client, receiver, launch_generation = room.client, room.receiver, room.launch_generation

        def current():
            # Probes must not hold control_lock across slow I/O. Fence each
            # result instead: stop/restart may retire this launch at any await.
            return (
                not self._closing and not room.removing and room.desired.enabled
                and room.status in {"running", "degraded"}
                and room.client is client and room.receiver is receiver
                and room.launch_generation == launch_generation
            )

        try:
            if (room.removing and room.task and room.task.done()
                    and not room.processes and not room.receiver and not room.bluetooth_admission):
                self.rooms.pop(room.desired.id, None)
                return
            if not room.desired.enabled:
                if (
                    room.status == "error"
                    and asyncio.get_running_loop().time() >= room.retry_at
                    and (
                        room.processes
                        or room.receiver
                        or room.bluetooth_admission
                        or room.reserved_slot is not None
                        or room.desired.id in self.sender_users
                    )
                ):
                    room.wake.set()
                return
            if room.status == "error" and asyncio.get_running_loop().time() >= room.retry_at:
                room.wake.set()
            elif room.status in {"running", "degraded"}:
                if room.bluetooth_admission:
                    try:
                        await asyncio.wait_for(room.bluetooth_admission.check(), timeout=8)
                    except asyncio.TimeoutError as exc:
                        raise RuntimeFailure("Bluetooth endpoint validation exceeded its deadline") from exc
                    if not current():
                        return
                if room.local_pin:
                    try:
                        room.local_pin.validate()
                    except PCMIdentityError as exc:
                        raise RuntimeFailure(str(exc)) from exc
                healthy = all(
                    process.alive for process in room.processes.values()
                ) and await self.network.healthy(receiver)
                if not current():
                    return
                if not healthy:
                    room.restart_required = True
                    room.wake.set()
                    return
                try:
                    player = await client.request("GET", "/api/player")
                    if not current():
                        return
                    room.player = player
                    for name in ["audio", "bluetooth-output"]:
                        if name in room.processes:
                            observed_sessions = {
                                session_id: self.session_generations.get(session_id)
                                for session_id, owner in self.sessions.items()
                                if name == "audio" and owner == room.desired.id
                            }
                            health = await self._worker_rpc(room, name, "health", {}, timeout=2)
                            if not current():
                                return
                            if health.get("ready") is False or health.get("error"):
                                raise RuntimeFailure(
                                    health.get("error") or f"Room {name} worker is not ready"
                                )
                            if name == "audio":
                                room.activity = {"music_active": health.get("music_media_recent") is True,
                                                 "speech_active": health.get("speech_session_id") is not None,
                                                 "observed_monotonic_ns": time.monotonic_ns()}
                                room.receiver_volume = dict(health.get("receiver_volume") or {})
                                active_session = health.get("speech_session_id")
                                for session_id, generation in observed_sessions.items():
                                    if (
                                        self.sessions.get(session_id) == room.desired.id
                                        and self.session_generations.get(session_id) is generation
                                        and session_id != active_session
                                        and session_id not in self.pending_sessions
                                    ):
                                        self._forget_session(session_id)
                    if room.status == "degraded" and (not room.gain_pending or asyncio.get_running_loop().time() >= room.retry_at):
                        room.wake.set()
                except (RuntimeFailure, RpcError) as exc:
                    if not current():
                        return
                    room.error = str(exc)
                    room.restart_required = True
                    room.wake.set()
                room.last_health_at = now()
        except (RuntimeFailure, OSError) as exc:
            if not current():
                return
            room.error = str(exc)
            room.restart_required = True
            room.wake.set()

    async def diagnostics(self, room_id=None):
        result = self.snapshot()
        if room_id is not None:
            room = self._room(room_id)
            logs = {}
            for path in sorted((room.directory / "logs").glob("*.log")):
                with path.open("rb") as stream:
                    stream.seek(0, os.SEEK_END)
                    stream.seek(max(0, stream.tell() - 8192))
                    logs[path.stem] = stream.read(8192).decode(errors="replace")
            result["logs"] = logs
            launch = room.launch_generation
            if launch and room.processes.get("audio"):
                try:
                    health = await self._worker_rpc(room, "audio", "health", {}, timeout=2)
                    if room.launch_generation == launch:
                        result["music_startup"] = health.get("music_startup")
                except (RpcError, OSError, RuntimeFailure):
                    result["music_startup"] = {"error": "Audio diagnostics are unavailable"}
        result["capabilities"] = {
            "airplay_input": True,
            "cast_input": False,
            "ownTone_timing": True,
            "native_outputs": ["airplay1", "airplay2", "chromecast"],
            "bluetooth": "Exact paired BlueALSA A2DP device required; descriptor bridge integration and physical playback remain validation gates",
            "cross_protocol_sync": "Best effort; calibrate OwnTone offsets, no universal phase-accurate guarantee",
        }
        return result

    async def close(self):
        self._closing = True
        if self._monitor:
            self._monitor.cancel()
            with suppress(asyncio.CancelledError):
                await self._monitor
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for room in self.rooms.values():
            if room.task:
                room.task.cancel()
        errors = []
        for room in self.rooms.values():
            if room.task:
                try:
                    await room.task
                except asyncio.CancelledError:
                    pass
                except (RuntimeFailure, OSError) as exc:
                    errors.append(str(exc))
            try:
                await self._stop_room(room)
            except (RuntimeFailure, OSError) as exc:
                errors.append(str(exc))
        if self.network and not self.sender_users:
            try:
                await self._stop_sender()
            except (RuntimeFailure, OSError) as exc:
                errors.append(str(exc))
        if self.unit_manager:
            self.unit_manager.close()
        if self._lock_file:
            self._lock_file.close()
            self._lock_file = None
        if errors:
            raise RuntimeFailure("Runtime shutdown cleanup remains pending: " + "; ".join(errors))


async def run_broker(config=None):
    config = config or RuntimeConfig.from_env()
    broker = Broker(config)
    done = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in [signal.SIGTERM, signal.SIGINT]:
        loop.add_signal_handler(sig, done.set)
    try:
        await broker.start()
        await done.wait()
    finally:
        await broker.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run_broker())


if __name__ == "__main__":
    main()
