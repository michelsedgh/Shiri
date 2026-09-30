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
import logging
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import signal
import sys
from uuid import uuid4

from pydantic import ValidationError

from shiri.domain import Room, SpeakerRef, local_audio_device_key, speaker_key, validate_local_audio_device
from shiri.rpc import RpcError, call_rpc, serve_rpc
from shiri.settings import RuntimeConfig
from .backend import OwnToneClient
from .configuration import backend_configs, isolated_command, isolated_runtime
from .network import DHCP_HOOK, NetworkManager
from .system import OwnedProcess, Runner, RuntimeFailure, atomic_json, read_json, root_directory

log = logging.getLogger(__name__)


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

    def snapshot(self):
        offsets = {speaker.id: speaker.offset_ms for speaker in self.desired.speakers}
        outputs = [dict(output, requested_offset_ms=offsets.get(output["id"])) for output in self.outputs]
        return {
            "room_id": self.desired.id,
            "status": self.status,
            "error": self.error,
            "receiver_ip": self.receiver.get("ip") if self.receiver else None,
            "owntone_url": self.client.base_url if self.client else None,
            "last_health_at": self.last_health_at,
            "volume": self.current_volume,
            "phone_volume_update": self.phone_volume_update,
            "selected_ids": self.selected_ids,
            "outputs": outputs,
            "player": self.player,
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
        self.rooms: dict[str, RuntimeRoom] = {}
        self.sender: dict | None = None
        self.sender_processes: dict[str, OwnedProcess] = {}
        self.sender_users: set[str] = set()
        self.sender_lock = asyncio.Lock()
        self.config_lock = asyncio.Lock()
        self.slot_locks = [asyncio.Lock() for _ in range(config.max_rooms)]
        self.speaker_lock = asyncio.Lock()
        self.speaker_leases: dict[tuple, str] = {}
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
        for name in ["ip", "dhclient", "unshare", "mount", "dbus-daemon", "avahi-daemon", "ping"]:
            if not shutil.which(name):
                raise RuntimeFailure(f"Required system command {name} is missing")
        for name in ["nqptp", "shairport-sync", "airptpd", "owntone"]:
            self.binary(name)
        for name in ["nqptp", "shairport-sync"]:
            result = await self.runner.run([self.binary(name), "-V"])
            self.versions[name] = (result.stdout or result.stderr).strip()
        shairport = self.versions["shairport-sync"]
        if "AirPlay2" not in shairport or "ALSA" not in shairport:
            raise RuntimeFailure("Shairport Sync must include AirPlay 2 and ALSA support")
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
        if not re.search(r"(?<![\w.-])29\.3-shiri-swvol1(?![\w.-])", self.versions["owntone"]):
            raise RuntimeFailure(
                "This runtime requires OwnTone 29.3-shiri-swvol1 with per-session software volume; "
                "rebuild pinned backends using install/build_backends.sh"
            )
        if not DHCP_HOOK.is_file() or not os.access(DHCP_HOOK, os.X_OK):
            raise RuntimeFailure("The private namespace DHCP hook is missing")
        info = Path("/proc/asound/Loopback/pcm0p/info")
        try:
            info_text = await asyncio.to_thread(info.read_text)
        except OSError as exc:
            raise RuntimeFailure(
                "ALSA Loopback is unavailable; load snd-aloop and check VM sound/kernel modules"
            ) from exc
        match = re.search(r"subdevices_count:\s*(\d+)", info_text)
        if not match or int(match.group(1)) < self.config.max_rooms:
            raise RuntimeFailure("ALSA Loopback does not provide the configured number of room slots")
        await self.runner.run(
            [
                sys.executable,
                "-c",
                'import gi; gi.require_version("Gst", "1.0"); gi.require_version("GstAudio", "1.0"); '
                "from gi.repository import Gst, GstAudio; Gst.init(None); GstAudio.AudioInfo(); "
                "assert all(Gst.ElementFactory.find(name) for name in "
                '("alsasrc", "alsasink", "audiomixer", "audioconvert", "audioresample", "appsrc", "appsink")); '
                "import aiortc; import av; import numpy",
            ]
        )

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
        descriptor = os.open(
            self.config.runtime_dir / "broker.lock",
            os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW,
            0o600,
        )
        if sys.platform == "linux" and os.geteuid() == 0 and os.fstat(descriptor).st_uid != 0:
            os.close(descriptor)
            raise RuntimeFailure("Broker singleton lock is not root-owned")
        self._lock_file = os.fdopen(descriptor, "a")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_file.close()
            self._lock_file = None
            raise RuntimeFailure("Another Shiri runtime broker owns this installation") from exc
        try:
            await self.preflight()
            self.network = NetworkManager(self.config.runtime_state_dir, self.runner)
            await self.network.recover()
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
            for definition in desired:
                room = self.rooms.get(definition.id)
                if room is not None and definition.revision < room.desired.revision:
                    # A phone event ACK may advance intent while an older API
                    # reconciliation is already in flight. Equal revisions still
                    # replace the complete definition after a volume-only ACK.
                    continue
                completed = room is not None and room.task is not None and room.task.done()
                changed = room is None or room.desired != definition or completed
                if room is None:
                    directory = self.config.runtime_state_dir / "rooms" / definition.id
                    room = RuntimeRoom(definition, directory, current_volume=definition.volume)
                    stored = read_json(directory / "phone-volume.json")
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
                if not self.ready and definition.enabled:
                    room.status, room.error = "error", self.error
                else:
                    if not definition.enabled and (room.processes or room.receiver):
                        room.status = "stopping"
                    elif (
                        room.client
                        and room.backend_definition
                        and self._material(room.backend_definition) != self._material(definition)
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

    def _material(self, definition: Room):
        return definition.interface, definition.slot, definition.airplay_name, definition.local_audio_device

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
                if other.desired.enabled and other.status == "degraded":
                    other.wake.set()

    async def _release_speakers(self, room: RuntimeRoom):
        await self._commit_speakers(room, set())

    def _check_start(self, room: RuntimeRoom, starting: Room):
        if (
            self._closing
            or not room.desired.enabled
            or room.removing
            or self._material(room.desired) != self._material(starting)
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
                material_changed = room.applied and self._material(room.applied) != self._material(
                    room.desired
                )
                if room.client and (material_changed or room.restart_required):
                    await self._stop_room(room)
                if not room.client:
                    await self._start_room(room)
                else:
                    async with room.control_lock:
                        await room.client.volume(room.current_volume)
                        await self._restore_outputs(room)
                room.applied = room.desired
                room.failures, room.retry_at, room.restart_required = 0, 0, False
            except Superseded:
                await self._stop_room(room)
                room.wake.set()
            except asyncio.CancelledError:
                await asyncio.shield(self._stop_room(room))
                raise
            except (RuntimeFailure, RpcError, OSError) as exc:
                room.error = str(exc)
                log.exception("Room %s failed", room.desired.id)
                try:
                    await self._stop_room(room)
                except (RuntimeFailure, OSError) as cleanup_error:
                    room.error += f"; cleanup pending: {cleanup_error}"
                room.status = "error"
                room.failures += 1
                room.retry_at = asyncio.get_running_loop().time() + min(60, 2 ** min(room.failures, 6))
            finally:
                room.last_health_at = now()

    async def _start_process(self, key: str, name: str, command: list[str], directory: Path):
        process = await self.runner.start(name, command, directory / "logs" / f"{name}.log")
        try:
            await self._remember_process(key, process)
        except BaseException:
            await asyncio.shield(process.stop())
            raise
        return process

    async def _remember_process(self, key: str, process):
        identity = await process.coherent_identity()
        self.network.remember_process(key, process, identity=identity)

    async def _refresh_processes(self, key: str, processes: dict):
        for name, process in processes.items():
            await self._remember_process(f"{key}:{name}", process)

    async def _start_namespace_services(self, key: str, record: dict, directory: Path):
        runtime = directory / "isolation"
        dbus, avahi, _ = isolated_runtime(runtime, record["namespace"], record["interface"])
        processes = {}
        try:
            processes["dbus"] = await self._start_process(
                f"{key}:dbus",
                "dbus",
                ["dbus-daemon", "--config-file", str(dbus), "--nofork", "--nopidfile"],
                directory,
            )
            await asyncio.sleep(0.2)
            if not processes["dbus"].alive:
                raise RuntimeFailure("Private D-Bus exited during startup")
            processes["avahi"] = await self._start_process(
                f"{key}:avahi",
                "avahi",
                isolated_command(
                    runtime,
                    record["namespace"],
                    ["avahi-daemon", "--file", str(avahi), "--no-drop-root", "--no-chroot", "--debug"],
                ),
                directory,
            )
            await asyncio.sleep(0.5)
            if not processes["avahi"].alive:
                raise RuntimeFailure("Private Avahi exited during startup")
            # Exec wrappers have now settled; persist the actual executable and
            # argv rather than an intermediate unshare/shell command line.
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
                    isolated_command(
                        directory / "isolation",
                        self.sender["namespace"],
                        [self.binary("airptpd"), "-f", "-v"],
                    ),
                    directory,
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

    async def _start_room(self, room: RuntimeRoom):
        starting = room.desired
        local_device = self._playback_device(starting.local_audio_device)
        room.status, room.error = "starting", None
        if room.processes or room.receiver or room.reserved_slot is not None:
            await self._stop_room(room)
        await self.slot_locks[starting.slot].acquire()
        room.reserved_slot = starting.slot
        room.backend_definition = starting
        room.active_local_device = local_device
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
        shairport, owntone = backend_configs(
            starting.model_copy(update={"local_audio_device": room.active_local_device}),
            room.directory,
            room.receiver,
            sender,
            broker_socket=self.config.runtime_socket,
            all_receiver_names=sorted(self._receiver_names()),
            password=self._password,
        )
        room.processes = await self._start_namespace_services(starting.id, room.receiver, room.directory)
        room.processes["nqptp"] = await self._start_process(
            f"{starting.id}:nqptp",
            "nqptp",
            isolated_command(
                room.directory / "isolation", room.receiver["namespace"], [self.binary("nqptp"), "-v"]
            ),
            room.directory,
        )
        await asyncio.sleep(0.2)
        self._check_start(room, starting)
        if starting.local_audio_device and starting.local_audio_device.lower().startswith("bluealsa"):
            room.processes["local-output"] = await self._start_process(
                f"{starting.id}:local-output",
                "local-output",
                [
                    sys.executable,
                    "-m",
                    "shiri.runtime.local_output",
                    "--capture",
                    f"hw:Loopback,0,{starting.slot}",
                    "--device",
                    starting.local_audio_device,
                    "--socket",
                    str(room.directory / "local-output.sock"),
                ],
                room.directory,
            )
            await self._wait_worker(room, "local-output", room.directory / "local-output.sock")
            self._check_start(room, starting)
        room.processes["owntone"] = await self._start_process(
            f"{starting.id}:owntone",
            "owntone",
            isolated_command(
                self.config.runtime_state_dir / "sender" / "isolation",
                sender["namespace"],
                [
                    self.binary("owntone"),
                    "-f",
                    "-c",
                    str(owntone),
                    "--mdns-no-rsp",
                    "--mdns-no-daap",
                    "--mdns-no-web",
                    "--mdns-no-cname",
                ],
            ),
            room.directory,
        )
        room.client = OwnToneClient(
            f"http://{sender['api_ip']}:{3869 + starting.slot * 10}", password=self._password
        )
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
            f"{starting.id}:audio",
            "audio",
            [
                sys.executable,
                "-m",
                "shiri.runtime.audio",
                "--room-dir",
                str(room.directory),
                "--capture",
                f"hw:Loopback,1,{starting.slot}",
                "--socket",
                str(room.directory / "audio.sock"),
            ],
            room.directory,
        )
        self._check_start(room, starting)
        await self._wait_worker(room, "audio", room.directory / "audio.sock")
        room.processes["shairport"] = await self._start_process(
            f"{starting.id}:shairport",
            "shairport",
            isolated_command(
                room.directory / "isolation",
                room.receiver["namespace"],
                [self.binary("shairport-sync"), "-v", "-c", str(shairport)],
            ),
            room.directory,
        )
        await asyncio.sleep(0.3)
        self._require_alive(room.processes, "after starting the AirPlay receiver")
        await self._refresh_processes(starting.id, room.processes)
        async with room.control_lock:
            await self._restore_outputs(room)
        self._check_start(room, starting)
        room.last_health_at = now()

    async def _wait_worker(self, room: RuntimeRoom, name: str, socket: Path):
        for _ in range(20):
            try:
                health = await call_rpc(socket, "health", {}, timeout=1)
                if health.get("ready") is False or health.get("error"):
                    raise RuntimeFailure(health.get("error") or f"Room {name} is not ready")
                return
            except RpcError as exc:
                if not room.processes[name].alive:
                    raise RuntimeFailure(f"Room {name} worker exited during startup") from exc
                await asyncio.sleep(0.1)
        raise RuntimeFailure(f"Room {name} worker did not become ready")

    async def _restore_outputs(self, room: RuntimeRoom):
        speakers = list(room.desired.speakers)
        definition = room.backend_definition or room.desired
        room.outputs = await room.client.outputs(self._receiver_names())
        try:
            keys = await self._reserve_speakers(
                room, speakers, room.active_local_device or definition.local_audio_device
            )
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
            await self.network.remove("sender")
        self.sender = None

    async def _stop_room(self, room: RuntimeRoom):
        async with room.control_lock:
            await self._stop_room_locked(room)

    async def _stop_room_locked(self, room: RuntimeRoom):
        if room.processes or room.receiver or room.client:
            room.status = "stopping"
        for session_id, room_id in list(self.sessions.items()):
            if room_id == room.desired.id:
                self._forget_session(session_id)
        for name, process in reversed(list(room.processes.items())):
            await process.stop()
            self.network.forget_process(f"{room.desired.id}:{name}")
            room.processes.pop(name, None)
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
            outputs = await client.outputs(self._receiver_names())
            try:
                definition = room.backend_definition or room.desired
                leases = await self._reserve_speakers(
                    room, speakers, room.active_local_device or definition.local_audio_device
                )
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

    async def set_volume(self, room: RuntimeRoom, value, *, pending=False):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
            raise RpcError("invalid_request", "Volume must be an integer from 0 to 100")
        if not pending:
            self._client(room)
        async with room.control_lock:
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
            room.current_volume = value
        return {"ok": True, "volume": value, "pending": not bool(room.client)}

    def _save_phone_volume(self, room):
        update = room.phone_volume_update
        if update and room.phone_volume_next:
            update = {**update, "next": room.phone_volume_next}
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

    async def speech(self, room: RuntimeRoom, payload: dict):
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", session_id
        ):
            raise RpcError("invalid_request", "A bounded speech session id is required")
        previous = self.sessions.get(session_id)
        if previous is not None and previous != room.desired.id:
            raise RpcError("session_conflict", "Speech session belongs to another room")
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
        if payload.get("action") in {"offer", "close"} or generation is None:
            generation = object()
        self.session_generations[session_id] = generation
        self.sessions[session_id] = room.desired.id
        operation = object()
        self._session_operations.setdefault(session_id, set()).add(operation)
        self.pending_sessions.add(session_id)
        try:
            result = await call_rpc(room.directory / "audio.sock", "speech", message, timeout=20)
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

    async def _health_monitor(self):
        while not self._closing:
            await asyncio.sleep(5)
            sender_healthy = True
            if self.sender:
                try:
                    sender_healthy = all(
                        process.alive for process in self.sender_processes.values()
                    ) and await self.network.healthy(self.sender)
                except RuntimeFailure:
                    sender_healthy = False
            if not sender_healthy:
                for room in list(self.rooms.values()):
                    if room.desired.enabled:
                        room.restart_required = True
                        room.wake.set()
            await asyncio.gather(*(self._probe_room(room) for room in list(self.rooms.values())))

    async def _probe_room(self, room):
        try:
            if room.removing and room.task and room.task.done() and not room.processes and not room.receiver:
                self.rooms.pop(room.desired.id, None)
                return
            if not room.desired.enabled:
                if (
                    room.status == "error"
                    and asyncio.get_running_loop().time() >= room.retry_at
                    and (
                        room.processes
                        or room.receiver
                        or room.reserved_slot is not None
                        or room.desired.id in self.sender_users
                    )
                ):
                    room.wake.set()
                return
            if room.status == "error" and asyncio.get_running_loop().time() >= room.retry_at:
                room.wake.set()
            elif room.status in {"running", "degraded"}:
                if not all(
                    process.alive for process in room.processes.values()
                ) or not await self.network.healthy(room.receiver):
                    room.restart_required = True
                    room.wake.set()
                    return
                try:
                    room.player = await room.client.request("GET", "/api/player")
                    for name, socket in [("audio", "audio.sock"), ("local-output", "local-output.sock")]:
                        if name in room.processes:
                            observed_sessions = {
                                session_id: self.session_generations.get(session_id)
                                for session_id, owner in self.sessions.items()
                                if name == "audio" and owner == room.desired.id
                            }
                            health = await call_rpc(room.directory / socket, "health", {}, timeout=2)
                            if health.get("ready") is False or health.get("error"):
                                raise RuntimeFailure(
                                    health.get("error") or f"Room {name} worker is not ready"
                                )
                            if name == "audio":
                                active_session = health.get("speech_session_id")
                                for session_id, generation in observed_sessions.items():
                                    if (
                                        self.sessions.get(session_id) == room.desired.id
                                        and self.session_generations.get(session_id) is generation
                                        and session_id != active_session
                                        and session_id not in self.pending_sessions
                                    ):
                                        self._forget_session(session_id)
                    if room.status == "degraded":
                        room.wake.set()
                except (RuntimeFailure, RpcError) as exc:
                    room.error = str(exc)
                    room.restart_required = True
                    room.wake.set()
                room.last_health_at = now()
        except (RuntimeFailure, OSError) as exc:
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
        result["capabilities"] = {
            "airplay_input": True,
            "cast_input": False,
            "ownTone_timing": True,
            "native_outputs": ["airplay1", "airplay2", "chromecast"],
            "bluetooth": "Configured BlueALSA card required; pairing and VM Bluetooth passthrough are external setup",
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
