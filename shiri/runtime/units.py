"""Exact system-manager authority for capability-limited daemon services.

The broker stays in the host mount namespace. PID and executable observations
are diagnostics only for these services; their durable authority is a unique
unit intent, immutable launch properties, boot, invocation and cgroup.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field, replace
import ctypes
import json
import os
from pathlib import Path
import re
import signal
import stat
from types import SimpleNamespace
from uuid import uuid4

from shiri.deadline import bounded

from .bind_policy import BindPolicy, proof as bind_proof, trusted_file, validate_proof
from .system import RuntimeFailure, atomic_json, boot_id, process_birth, root_directory
from .socket_publication import discard as discard_socket, validate as validate_socket

DESTINATION = "org.freedesktop.systemd1"
MANAGER_PATH = "/org/freedesktop/systemd1"
UNIT_INTERFACE = "org.freedesktop.systemd1.Unit"
SERVICE_INTERFACE = "org.freedesktop.systemd1.Service"
VIEW = Path("/run/shiri-worker")
# The standalone launch gate and journal reader use this same bounded grammar.
UNIT_RE = re.compile(
    r"shiri-(?P<installation>[0-9a-f]{8})-(?P<owner>sender|[0-9a-f]{32})-"
    r"(?P<role>[a-z0-9-]{1,32})-(?P<nonce>[0-9a-f]{32})\.service"
)
USER_RE = re.compile(r"shiri-(?:receiver|output|audio|discovery|timing|bridge)(?:-[0-7])?")
SAFE_PATH = re.compile(r"/[A-Za-z0-9_./-]+")
ROLE_USERS = {"dbus": "discovery", "avahi": "discovery", "shairport": "receiver",
              "owntone": "output", "audio": "audio", "local-output": "output",
              "bluetooth-output": "bridge",
              "nqptp": "timing", "airptpd": "timing"}
CGROUP2_SUPER_MAGIC = 0x63677270
SYSCALL_POLICY_V1 = "~@mount @reboot @swap @module @raw-io @debug"
SYSCALL_POLICY_V2 = SYSCALL_POLICY_V1 + " process_vm_readv process_vm_writev"


def is_cgroup2(descriptor):
    # Linux statfs starts with __fsword_t f_type. Allocate more than the full
    # native structure and inspect only that public first field; no guessed
    # ABI offsets are used for its architecture-dependent remaining fields.
    library = ctypes.CDLL(None, use_errno=True)
    function = library.fstatfs
    function.argtypes, function.restype = [ctypes.c_int, ctypes.c_void_p], ctypes.c_int
    result = ctypes.create_string_buffer(256)
    if function(descriptor, result):
        raise OSError(ctypes.get_errno(), "Cannot identify the held control-group filesystem")
    return ctypes.c_long.from_buffer(result).value == CGROUP2_SUPER_MAGIC


def path_text(path: Path | str) -> str:
    value = str(path)
    if not SAFE_PATH.fullmatch(value) or ".." in Path(value).parts:
        raise RuntimeFailure("A daemon service path is not a canonical absolute path")
    return value


@dataclass(frozen=True)
class Bind:
    source: str
    target: str
    writable: bool = False

    def __post_init__(self):
        path_text(self.source)
        path_text(self.target)


@dataclass(frozen=True)
class LaunchGate:
    directory: str
    inode: int
    nonce: str
    python: str
    script: str
    payload: tuple[str, ...]

    def __post_init__(self):
        for path in [self.directory, self.python, self.script]:
            path_text(path)
        if (type(self.inode) is not int or self.inode <= 0 or not re.fullmatch(r"[0-9a-f]{32}", self.nonce)
                or not self.payload or not Path(self.payload[0]).is_absolute()
                or any(not isinstance(arg, str) or not arg or re.search(r"[\x00-\x20$%;]", arg)
                       for arg in self.payload)):
            raise RuntimeFailure("Invalid daemon launch gate identity")

    def command(self, unit):
        return (self.python, "-I", self.script, "--unit", unit, "--nonce", self.nonce,
                "--release", str(VIEW / "gate/ready.json"), "--", *self.payload)

    def record(self):
        return {"directory": self.directory, "inode": self.inode, "nonce": self.nonce,
                "python": self.python, "script": self.script, "payload": list(self.payload)}


@dataclass(frozen=True)
class PCMExec:
    helper: str
    payload: tuple[str, ...]

    def __post_init__(self):
        path_text(self.helper)
        if (not self.payload or not Path(self.payload[0]).is_absolute()
                or any(not isinstance(arg, str) or not arg or re.search(r"[\x00-\x20$%;]", arg)
                       for arg in self.payload)):
            raise RuntimeFailure("Invalid filtered PCM daemon payload")

    def command(self):
        return (self.helper, "--", *self.payload)

    def record(self):
        return {"helper": self.helper, "payload": list(self.payload)}


@dataclass(frozen=True)
class UnitSpec:
    name: str
    role: str
    user: str
    group: str
    command: tuple[str, ...]
    namespace: str = ""
    binds: tuple[Bind, ...] = ()
    environment: tuple[str, ...] = ()
    devices: tuple[str, ...] = ()
    supplementary_groups: tuple[str, ...] = ()
    inaccessible: tuple[str, ...] = ("/run/dbus/system_bus_socket",)
    ptp: bool = False
    listen_port: int | None = None
    gate: LaunchGate | None = None
    pcm_exec: PCMExec | None = None

    def __post_init__(self):
        identity = UNIT_RE.fullmatch(self.name)
        if (not identity or not re.fullmatch(r"[a-z0-9-]{1,32}", self.role)
                or identity['role'] != self.role):
            raise RuntimeFailure("Invalid owned daemon unit identity")
        required = ROLE_USERS.get(self.role)
        if (required is None or not re.fullmatch(r"shiri-" + required + r"(?:-[0-7])?", self.user)
                or self.group != self.user):
            raise RuntimeFailure("Daemon units require a fixed managed non-root identity")
        if not self.command or not Path(self.command[0]).is_absolute():
            raise RuntimeFailure("Daemon executable must be an explicit absolute path")
        if any(not isinstance(arg, str) or not arg or re.search(r"[\x00-\x20$%;]", arg)
               for arg in self.command):
            raise RuntimeFailure("Daemon argv contains an unsupported expansion or control character")
        path_text(self.command[0])
        if self.namespace:
            path_text(self.namespace)
            if not self.namespace.startswith("/run/netns/shiri_"):
                raise RuntimeFailure("Daemon namespace is outside the owned namespace boundary")
        for item in self.environment:
            if not re.fullmatch(r"[A-Z_][A-Z_0-9]*=[A-Za-z0-9_./:=,-]*", item):
                raise RuntimeFailure("Unsafe daemon environment")
        if self.devices and self.role not in {"owntone", "local-output"}:
            raise RuntimeFailure("Only an exact local output may receive ALSA device access")
        if any(item != "audio" for item in self.supplementary_groups) or (self.supplementary_groups and not self.devices):
            raise RuntimeFailure("Only explicitly required audio device credentials are supported")
        for device in self.devices:
            path_text(device)
            if not device.startswith("/dev/snd/"):
                raise RuntimeFailure("Daemon device admission is limited to exact ALSA nodes")
            if (Path(device).name.startswith("control") and self.pcm_exec is None):
                raise RuntimeFailure("ALSA control access requires the inherited ioctl filter launcher")
        for hidden in self.inaccessible:
            path_text(hidden)
        if ((self.role == "owntone" and (type(self.listen_port) is not int
                                           or self.listen_port not in range(3869, 3940, 10)))
                or (self.role != "owntone" and self.listen_port is not None)):
            raise RuntimeFailure("OwnTone requires its exact room HTTP bind port")
        if self.ptp and self.role not in {"airptpd", "nqptp"}:
            raise RuntimeFailure("Only PTP helpers may receive the low-port bind capability")
        if self.gate and (self.role != "owntone" or self.command != self.gate.command(self.name)
                          or not self.gate.directory.endswith(f"/launch-gates/{self.name}")
                          or Bind(self.gate.directory, str(VIEW / "gate")) not in self.binds):
            raise RuntimeFailure("Daemon gate does not match the immutable payload and read-only mount")
        if self.pcm_exec and (self.role not in {"owntone", "local-output"} or not self.devices
                              or (self.gate.payload if self.gate else self.command) != self.pcm_exec.command()):
            raise RuntimeFailure("PCM filter launcher does not match the immutable output payload")
        if self.pcm_exec:
            controls = [re.fullmatch(r"/dev/snd/controlC([0-9]+)", device) for device in self.devices]
            pcms = [re.fullmatch(r"/dev/snd/pcmC([0-9]+)D[0-9]+" + ("p" if self.role == "owntone" else "c"), device)
                    for device in self.devices]
            controls, pcms = [item for item in controls if item], [item for item in pcms if item]
            if (len(self.devices) != 2 or len(controls) != 1 or len(pcms) != 1
                    or controls[0].group(1) != pcms[0].group(1)):
                raise RuntimeFailure("Filtered local output requires one exact PCM and its matching card control node")
        if self.role == "bluetooth-output":
            if (not re.fullmatch(r"shiri-bridge-[0-7]", self.user)
                    or self.namespace or self.devices or self.supplementary_groups or self.ptp
                    or self.gate or self.pcm_exec or self.listen_port is not None
                    or any(item.startswith(("DBUS_", "ALSA_")) for item in self.environment)
                    or "/run/dbus/system_bus_socket" not in self.inaccessible
                    or any(item.target.startswith("/run/dbus") for item in self.binds)):
                raise RuntimeFailure("Bluetooth bridge requires FD-only device access and an inaccessible host bus")

    @property
    def description(self):
        return f"Shiri owned daemon {self.name}"

    def properties(self):
        properties = {
            "Description": self.description,
            "User": self.user, "Group": self.group,
            "NoNewPrivileges": "yes",
            "CapabilityBoundingSet": "CAP_NET_BIND_SERVICE" if self.ptp else "",
            "AmbientCapabilities": "CAP_NET_BIND_SERVICE" if self.ptp else "",
            "ProtectSystem": "strict", "ProtectHome": "yes", "PrivateTmp": "yes",
            "PrivateDevices": "yes", "ProtectControlGroups": "yes",
            "ProtectKernelTunables": "yes", "ProtectKernelModules": "yes",
            "ProtectKernelLogs": "yes", "ProtectProc": "invisible",
            "RestrictSUIDSGID": "yes", "LockPersonality": "yes", "RestrictNamespaces": "yes",
            "RestrictRealtime": "no" if self.ptp else "yes",
            "RestrictAddressFamilies": "AF_UNIX" if self.role == "bluetooth-output" else "AF_UNIX AF_INET AF_INET6 AF_NETLINK",
            "SystemCallFilter": SYSCALL_POLICY_V2,
            "KillMode": "control-group", "TimeoutStopSec": "6s",
            "Delegate": "no",
            "SendSIGKILL": "yes", "Restart": "no", "TasksMax": "128",
            "CollectMode": "inactive-or-failed",
            "MemoryMax": "768M", "UMask": "0022" if self.ptp else "0077", "DevicePolicy": "strict",
            "StandardOutput": "journal", "StandardError": "journal",
            "LimitCORE": "0", "LimitMEMLOCK": "8388608" if self.ptp else "0",
            "LimitRTPRIO": "5" if self.ptp else "0",
            "InaccessiblePaths": " ".join(self.inaccessible),
        }
        if self.listen_port is not None and self.gate is None:
            properties["SocketBindDeny"] = "any"
            properties["SocketBindAllow"] = f"ipv4:tcp:{self.listen_port} ipv4:udp"
        if self.namespace:
            properties["NetworkNamespacePath"] = self.namespace
        if self.binds:
            writable = [f"{item.source}:{item.target}:norbind" for item in self.binds if item.writable]
            readonly = [f"{item.source}:{item.target}:norbind" for item in self.binds if not item.writable]
            if writable:
                properties["BindPaths"] = " ".join(writable)
            if readonly:
                properties["BindReadOnlyPaths"] = " ".join(readonly)
        if self.environment:
            properties["Environment"] = " ".join(self.environment)
        if self.supplementary_groups:
            properties["SupplementaryGroups"] = " ".join(self.supplementary_groups)
        properties["DeviceAllow"] = " ".join([
            "/dev/null rw", "/dev/zero rw", "/dev/random r", "/dev/urandom r",
            *(f"{item} rw" for item in self.devices),
        ])
        return properties

    def intent(self):
        return {
            "kind": "systemd-unit", "unit": self.name, "name": self.role,
            "policy_version": 5 if self.role == "bluetooth-output" else 4 if self.pcm_exec else 3 if self.gate else 2,
            "boot_id": boot_id(), "user": self.user, "group": self.group,
            "argv": list(self.command), "namespace": self.namespace,
            "description": self.description,
            "properties": self.properties(), "invocation_id": None,
            "control_group": None, "cgroup_inode": None, "listen_port": self.listen_port,
            **({"gate": self.gate.record()} if self.gate else {}),
            **({"pcm_exec": self.pcm_exec.record()} if self.pcm_exec else {}),
        }


def new_unit(installation_tag: str, owner: str, role: str) -> str:
    if (not isinstance(installation_tag, str) or not re.fullmatch(r"[0-9a-f]{8}", installation_tag)
            or not isinstance(owner, str)
            or not re.fullmatch(r"sender|[0-9a-f]{32}|[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", owner)
            or not isinstance(role, str) or role not in ROLE_USERS):
        raise RuntimeFailure("Invalid installation or room unit owner")
    owner = owner.replace("-", "")
    return f"shiri-{installation_tag}-{owner}-{role}-{uuid4().hex}.service"


def decode_bus_reply(text: str):
    try:
        value = json.loads(text)
        if not isinstance(value, dict) or not isinstance(value.get("type"), str):
            raise ValueError("Missing typed D-Bus result")
        return value["data"]
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeFailure("System manager returned malformed D-Bus JSON") from exc


def decode_properties(reply):
    if not isinstance(reply, list) or len(reply) != 1 or not isinstance(reply[0], dict):
        raise RuntimeFailure("System manager returned malformed property map")
    properties = {}
    for key, value in reply[0].items():
        if not isinstance(key, str) or not isinstance(value, dict) or "data" not in value:
            raise RuntimeFailure("System manager returned an untyped property")
        properties[key] = value["data"]
    return properties


@dataclass
class OwnedUnit:
    name: str
    manager: UnitManager
    entry: dict
    log_path: Path
    process: SimpleNamespace = field(default_factory=lambda: SimpleNamespace(pid=0, returncode=None))
    current: dict = field(default_factory=dict)
    monitor: asyncio.Task | None = None
    logger: object | None = None
    error: str | None = None
    policy_checked_at: float = 0

    @property
    def alive(self):
        return self.error is None and self.current.get("ActiveState") in {"active", "activating"} and bool(self.process.pid)

    def identity(self):
        return dict(self.entry, log_path=str(self.log_path))

    async def coherent_identity(self):
        await self.manager.refresh(self)
        return self.identity()

    async def stop(self, grace=6.0):
        await self.manager.stop_saved(self.entry)
        if self.monitor:
            self.monitor.cancel()
            with suppress(asyncio.CancelledError):
                await self.monitor
        if self.logger:
            await self.logger.stop()
        self.process.returncode = 0
        self.current = {"ActiveState": "inactive"}


class UnitManager:
    def __init__(self, runner, manifest, *, bus=None, bind_policy_helper=None, pcm_exec_helper=None):
        self.runner, self.manifest = runner, manifest
        self.bus = bus
        self.connection_lock = asyncio.Lock()
        self.bind_policy = BindPolicy(bind_policy_helper) if bind_policy_helper else None
        self.pcm_exec_helper = pcm_exec_helper

    async def connect(self):
        async with self.connection_lock:
            if self.bus is None:
                try:
                    from dbus_next.aio import MessageBus
                    candidate = MessageBus(bus_address="unix:path=/run/dbus/system_bus_socket")
                    try:
                        self.bus = await bounded(candidate.connect(), 5)
                    except BaseException:
                        # The constructor owns the candidate's socket before
                        # Hello completes. An abandoned/cancelled connection
                        # must not leak it or replace the cached shared bus.
                        with suppress(Exception):
                            candidate.disconnect()
                        raise
                except (OSError, ImportError, asyncio.TimeoutError) as exc:
                    raise RuntimeFailure("Cannot connect to the system manager's owned D-Bus endpoint") from exc
        return self.bus

    def close(self):
        if self.bus is not None:
            self.bus.disconnect()
            self.bus = None

    async def _call(self, path: str, interface: str, method: str, signature: str, *values, check=True):
        from dbus_next import Message, MessageType, Variant
        bus = await self.connect()
        try:
            reply = await bounded(bus.call(Message(
                destination=DESTINATION, path=path, interface=interface,
                member=method, signature=signature, body=list(values),
            )), 5)
        except (OSError, asyncio.TimeoutError) as exc:
            raise RuntimeFailure("System manager request did not complete within its deadline") from exc
        if reply.message_type == MessageType.ERROR:
            detail = f"{reply.error_name}: {reply.body[:1]}"
            if check:
                raise RuntimeFailure("System manager rejected the owned daemon operation: " + detail)
            return None, detail

        def normalize(value):
            if isinstance(value, Variant):
                return {"type": value.signature, "data": normalize(value.value)}
            if isinstance(value, dict):
                return {key: normalize(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [normalize(item) for item in value]
            if isinstance(value, bytes):
                return list(value)
            return value
        return normalize(reply.body), None

    async def inspect(self, name: str):
        if not UNIT_RE.fullmatch(name):
            raise RuntimeFailure("Refusing to inspect an invalid owned unit name")

        async def snapshot():
            identity = ("Id", "LoadState", "Transient", "InvocationID")

            async def properties(path, interface):
                reply, _ = await self._call(path, "org.freedesktop.DBus.Properties", "GetAll", "s", interface)
                return decode_properties(reply)

            for attempt in range(2):
                result, error = await self._call(
                    MANAGER_PATH, DESTINATION + ".Manager", "GetUnit", "s", name, check=False,
                )
                if error:
                    if "org.freedesktop.systemd1.NoSuchUnit" in error or "not loaded" in error:
                        return None
                    raise RuntimeFailure("Cannot inspect daemon unit through the system manager")
                if not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], str):
                    raise RuntimeFailure("System manager returned an invalid unit object")
                path = result[0]

                before = await properties(path, UNIT_INTERFACE)
                service = await properties(path, SERVICE_INTERFACE)
                after = await properties(path, UNIT_INTERFACE)
                # GetUnit does not load units. A later GetAll-by-path can load
                # a new not-found stub after transient-unit garbage collection.
                # Never combine one invocation's Unit properties with another
                # invocation's Service properties. Retry only once, within the
                # original transaction's15-second maximum.
                if any(before.get(key) != after.get(key) for key in identity):
                    if attempt == 0:
                        continue
                    raise RuntimeFailure("Daemon identity changed during inspection; resources stay reserved")
                if any(key in service and service[key] != after.get(key) for key in identity):
                    raise RuntimeFailure("Daemon interfaces disagree on identity; resources stay reserved")
                observed = after | service
                if observed.get("LoadState") == "not-found":
                    invocation = observed.get("InvocationID")
                    absent = (
                        observed.get("Id") == name
                        and observed.get("ActiveState") == "inactive"
                        and observed.get("SubState") == "dead"
                        and observed.get("Transient") is False
                        and type(observed.get("MainPID")) is int and observed["MainPID"] == 0
                        and type(observed.get("ControlPID")) is int and observed["ControlPID"] == 0
                        and observed.get("ControlGroup") == ""
                        and observed.get("User") == "" and observed.get("Group") == ""
                        and observed.get("ExecStart") == []
                        and type(invocation) is list and len(invocation) == 16
                        and all(type(value) is int and value == 0 for value in invocation)
                    )
                    if not absent:
                        raise RuntimeFailure("Unloaded daemon lacks a complete absence proof; resources stay reserved")
                    # This is manager absence only. stop_saved still proves the
                    # exact saved kernel cgroup empty before releasing ownership.
                    return None
                return observed

        try:
            return await bounded(snapshot(), 15)
        except asyncio.TimeoutError as exc:
            raise RuntimeFailure("System manager inspection exceeded its15-second transaction deadline") from exc

    @staticmethod
    def validate_saved(entry):
        if (not isinstance(entry, dict) or entry.get("kind") != "systemd-unit"
                or not isinstance(entry.get("unit"), str) or not UNIT_RE.fullmatch(entry["unit"])
                or not isinstance(entry.get("user"), str) or not USER_RE.fullmatch(entry["user"])
                or entry.get("group") != entry["user"]
                or not isinstance(entry.get("argv"), list) or not entry["argv"]
                or not all(isinstance(arg, str) for arg in entry["argv"])
                or not isinstance(entry.get("properties"), dict)
                or entry.get("description") != f"Shiri owned daemon {entry['unit']}"
                or not isinstance(entry.get("boot_id"), str) or not entry["boot_id"]):
            raise RuntimeFailure("Invalid durable daemon unit ownership record")
        properties = entry["properties"]
        policy_version = entry.get("policy_version", 1)
        if type(policy_version) is not int or policy_version not in {1, 2, 3, 4, 5}:
            raise RuntimeFailure("Unknown daemon launch policy version; resources stay reserved")
        if (policy_version == 5) != (entry.get("name") == "bluetooth-output"):
            raise RuntimeFailure("Daemon role does not match the immutable policy version")
        if "socket_publication" in entry:
            if policy_version != 5:
                raise RuntimeFailure("A socket publication requires the isolated bridge policy")
            validate_socket(entry["socket_publication"])
        try:
            binds = []
            for name in ["BindPaths", "BindReadOnlyPaths"]:
                for mount in properties.get(name, "").split():
                    source, target, flag = mount.split(":")
                    if flag != "norbind":
                        raise ValueError("Unsupported mount flag")
                    binds.append(Bind(source, target, name == "BindPaths"))
            devices = properties.get("DeviceAllow", "").split()
            if len(devices) % 2:
                raise ValueError("Invalid device list")
            admitted = [devices[index] for index in range(0, len(devices), 2)
                        if devices[index].startswith("/dev/snd/")]
            gate = None
            if policy_version == 3 or (policy_version == 4 and entry["name"] == "owntone"):
                record = entry.get("gate")
                if not isinstance(record, dict) or set(record) != {"directory", "inode", "nonce", "python", "script", "payload"}:
                    raise ValueError("Missing immutable gate identity")
                if not isinstance(record["payload"], list):
                    raise ValueError("Invalid gate payload")
                gate = LaunchGate(record["directory"], record["inode"], record["nonce"], record["python"],
                                  record["script"], tuple(record["payload"]))
            elif "gate" in entry or "bind_policy" in entry:
                raise ValueError("Unexpected gate in a legacy policy")
            pcm = None
            if policy_version == 4:
                record = entry.get("pcm_exec")
                if not isinstance(record, dict) or set(record) != {"helper", "payload"} or not isinstance(record["payload"], list):
                    raise ValueError("Missing immutable PCM ioctl launcher")
                pcm = PCMExec(record["helper"], tuple(record["payload"]))
            elif "pcm_exec" in entry:
                raise ValueError("Unexpected PCM ioctl launcher in a legacy policy")
            spec = UnitSpec(entry["unit"], entry["name"], entry["user"], entry["group"],
                            tuple(entry["argv"]), entry.get("namespace", ""), tuple(binds),
                            tuple(properties.get("Environment", "").split()), tuple(admitted),
                            tuple(properties.get("SupplementaryGroups", "").split()),
                            tuple(properties.get("InaccessiblePaths", "").split()),
                            properties.get("CapabilityBoundingSet") == "CAP_NET_BIND_SERVICE", entry.get("listen_port"), gate, pcm)
            supported = spec.properties()
            if policy_version == 1:
                # Read-only recovery for the exact predeployment v1 policy.
                # New launches always publish v2; no caller can choose v1.
                supported["SystemCallFilter"] = SYSCALL_POLICY_V1
            if supported != properties:
                raise ValueError("Unit policy does not match the supported launch boundary")
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise RuntimeFailure("Malformed daemon launch policy; resources stay reserved") from exc
        if "policy_fence" in entry and not isinstance(entry["policy_fence"], dict):
            raise RuntimeFailure("Invalid daemon policy observation")
        invocation = entry.get("invocation_id")
        if invocation is not None and (not isinstance(invocation, str) or not re.fullmatch(r"[0-9a-f]{32}", invocation)):
            raise RuntimeFailure("Invalid daemon invocation identity")
        group = entry.get("control_group")
        if group is not None and group != f"/system.slice/{entry['unit']}":
            raise RuntimeFailure("Invalid daemon control group")
        inode = entry.get("cgroup_inode")
        if inode is not None and (type(inode) is not int or inode <= 0):
            raise RuntimeFailure("Invalid daemon kernel cgroup identity")
        if "bind_policy" in entry:
            policy = entry["bind_policy"]
            if (not isinstance(policy, dict) or type(policy.get("cgroup_dev")) is not int
                    or policy["cgroup_dev"] <= 0 or not inode):
                raise RuntimeFailure("Invalid durable socket policy identity")
            validate_proof(policy, boot=entry["boot_id"], port=entry["listen_port"],
                           device=policy["cgroup_dev"], inode=inode)

    def verify(self, entry: dict, actual: dict):
        self.validate_saved(entry)
        if entry["boot_id"] != boot_id():
            raise RuntimeFailure("Daemon unit belongs to another boot; refusing authority")
        commands = actual.get("ExecStart")
        expected = [entry["argv"][0], entry["argv"], False]
        if (actual.get("Id") != entry["unit"] or actual.get("Transient") is not True
                or actual.get("Description") != entry["description"]
                or actual.get("User") != entry["user"] or actual.get("Group") != entry["group"]
                or actual.get("NetworkNamespacePath", "") != entry.get("namespace", "")
                or not isinstance(commands, list) or len(commands) != 1
                or commands[0][:3] != expected
                or actual.get("KillMode") != "control-group"
                or actual.get("NoNewPrivileges") is not True
                or actual.get("CapabilityBoundingSet") != (1024 if entry["properties"].get("CapabilityBoundingSet") else 0)
                or actual.get("AmbientCapabilities") != (1024 if entry["properties"].get("AmbientCapabilities") else 0)):
            raise RuntimeFailure("Daemon unit launch properties changed; resources stay reserved")
        properties = entry["properties"]
        expected_policy = {
            "ProtectSystem": "strict", "ProtectHome": "yes", "PrivateTmp": True,
            "PrivateDevices": True, "ProtectControlGroups": True,
            "ProtectKernelTunables": True, "ProtectKernelModules": True,
            "ProtectKernelLogs": True, "ProtectProc": "invisible",
            "RestrictSUIDSGID": True, "LockPersonality": True, "RestrictNamespaces": 0,
            "RestrictRealtime": properties.get("RestrictRealtime") == "yes",
            "DevicePolicy": "strict", "Delegate": False, "TasksMax": 128, "MemoryMax": 768 * 1024 * 1024,
            "TimeoutStopUSec": 6_000_000, "UMask": int(properties["UMask"], 8),
            "Environment": properties.get("Environment", "").split(),
            "SupplementaryGroups": properties.get("SupplementaryGroups", "").split(),
            "Restart": "no", "SendSIGKILL": True,
            "StandardOutput": "journal", "StandardError": "journal",
            "InaccessiblePaths": properties["InaccessiblePaths"].split(),
            "LimitCORE": 0, "LimitMEMLOCK": int(properties["LimitMEMLOCK"]),
            "LimitRTPRIO": int(properties["LimitRTPRIO"]),
        }
        for key, expected_value in expected_policy.items():
            if actual.get(key) != expected_value:
                raise RuntimeFailure(f"Daemon service policy {key} changed; resources stay reserved")
        for key in ["BindPaths", "BindReadOnlyPaths"]:
            expected_binds = [item.split(":")[:2] + [False, 0]
                              for item in properties.get(key, "").split()]
            if actual.get(key) != expected_binds:
                raise RuntimeFailure("Daemon mount boundary changed; resources stay reserved")
        device_words = properties["DeviceAllow"].split()
        expected_devices = sorted([device_words[index:index + 2] for index in range(0, len(device_words), 2)])
        if sorted(actual.get("DeviceAllow", [])) != expected_devices:
            raise RuntimeFailure("Daemon device boundary changed; resources stay reserved")
        if entry.get("listen_port") is not None and entry.get("policy_version", 1) < 3:
            expected = sorted([[2, 6, 1, entry["listen_port"]], [2, 17, 0, 0]])
            if (sorted(actual.get("SocketBindAllow", [])) != expected
                    or actual.get("SocketBindDeny") != [[0, 0, 0, 0]]):
                raise RuntimeFailure("OwnTone room HTTP bind isolation changed; resources stay reserved")
        elif actual.get("SocketBindAllow", []) or actual.get("SocketBindDeny", []):
            raise RuntimeFailure("Unexpected daemon bind policy; resources stay reserved")
        families = actual.get("RestrictAddressFamilies")
        if (not isinstance(families, list) or len(families) != 2 or families[0] is not True
                or set(families[1]) != ({"AF_UNIX"} if entry["name"] == "bluetooth-output" else
                                      {"AF_UNIX", "AF_INET", "AF_INET6", "AF_NETLINK"})):
            raise RuntimeFailure("Daemon socket-family boundary changed; resources stay reserved")
        forbidden = actual.get("SystemCallFilter")
        required_calls = {"mount", "umount2", "ptrace", "reboot"}
        if entry.get("policy_version", 1) >= 2:
            required_calls |= {"process_vm_readv", "process_vm_writev"}
        if (not isinstance(forbidden, list) or len(forbidden) != 2 or forbidden[0] is not False
                or not isinstance(forbidden[1], list)
                or not required_calls.issubset(forbidden[1])):
            raise RuntimeFailure("Daemon syscall boundary is incomplete; resources stay reserved")
        if entry.get("policy_fence"):
            for key, expected_value in entry["policy_fence"].items():
                if actual.get(key) != expected_value:
                    raise RuntimeFailure(f"Daemon immutable property {key} changed; resources stay reserved")
        invocation = actual.get("InvocationID")
        if isinstance(invocation, list):
            invocation = bytes(invocation).hex()
        if (not isinstance(invocation, str) or not re.fullmatch(r"[0-9a-f]{32}", invocation)
                or invocation == "0" * 32):
            raise RuntimeFailure("System manager has no valid daemon invocation identity")
        if entry.get("invocation_id") and invocation != entry["invocation_id"]:
            raise RuntimeFailure("Daemon invocation was replaced; refusing to stop it")
        group = actual.get("ControlGroup")
        if group and group != f"/system.slice/{entry['unit']}":
            raise RuntimeFailure("Daemon control group changed; resources stay reserved")
        if entry.get("control_group") and group and group != entry["control_group"]:
            raise RuntimeFailure("Daemon control group ownership changed")
        return invocation, group

    async def start(self, key: str, spec: UnitSpec, log_path: Path):
        if spec.role == "local-output":
            raise RuntimeFailure("Legacy host-bus audio bridge launch is disabled; use the isolated Bluetooth FD profile")
        # Refuse collisions before writing an intent or asking the manager to
        # start anything. The system manager also rejects a second transient unit.
        if await self.inspect(spec.name) is not None:
            raise RuntimeFailure("The proposed daemon unit already exists; nothing was replaced")
        if spec.devices:
            spec = self.prepare_pcm(spec)
        if spec.role == "owntone":
            if self.bind_policy is None:
                raise RuntimeFailure("OwnTone requires a broker-owned verified kernel socket policy")
            spec = self.prepare_gate(spec)
        gate_fd = self.gate_directory({"gate": spec.gate.record()})[1] if spec.gate else None
        try:
            entry = spec.intent()
            entry["log_path"] = str(log_path)
            self.validate_saved(entry)
            self.manifest.reserve_unit(key, entry)
        except BaseException:
            # No system-manager launch happened. Remove only our newly created
            # empty directory; never forget an older colliding reservation or
            # remove a directory whose name now refers to a different inode.
            if gate_fd is not None:
                self.discard_unreserved_gate(spec.gate, gate_fd)
            raise
        finally:
            if gate_fd is not None:
                os.close(gate_fd)
        args = ["/usr/bin/systemd-run", "--quiet", f"--unit={spec.name}", "--service-type=exec"]
        for property_name, value in spec.properties().items():
            if property_name in {"SocketBindAllow", "SocketBindDeny"}:
                args.extend(f"--property={property_name}={rule}" for rule in value.split())
            elif property_name == "DeviceAllow":
                words = value.split()
                args.extend(f"--property=DeviceAllow={words[index]} {words[index + 1]}"
                            for index in range(0, len(words), 2))
            else:
                args.append(f"--property={property_name}={value}")
        args.extend(["--", *spec.command])
        unit = OwnedUnit(spec.role, self, entry, log_path)
        try:
            await self.runner.run(args, timeout=12)
            for _ in range(40):
                await self.refresh(unit)
                if unit.alive:
                    break
                await asyncio.sleep(0.05)
            else:
                raise RuntimeFailure(f"{spec.role} did not become an active owned service")
            self.manifest.remember_unit(key, unit.identity())
            if spec.gate:
                descriptor = self.open_cgroup(unit.entry)
                try:
                    unit.entry["bind_policy"] = await self.bind_policy.run(
                        "attach", descriptor, unit.entry["boot_id"], spec.listen_port,
                    )
                    # Observe invocation again while the exact cgroup FD is
                    # held. A replacement never receives a release intended
                    # for the original unit, even after attach succeeded.
                    current = await self.inspect(spec.name)
                    if current is None:
                        raise RuntimeFailure("Gated daemon disappeared before socket policy publication")
                    self.verify(unit.entry, current)
                    checked = self.open_cgroup(unit.entry)
                    try:
                        if os.fstat(checked).st_ino != os.fstat(descriptor).st_ino:
                            raise RuntimeFailure("Gated daemon cgroup changed before release")
                    finally:
                        os.close(checked)
                    await self.bind_policy.run("verify", descriptor, unit.entry["boot_id"], spec.listen_port,
                                               unit.entry["bind_policy"])
                    self.manifest.remember_unit(key, unit.identity())
                    self.publish_gate(unit.entry, descriptor)
                    unit.policy_checked_at = asyncio.get_running_loop().time()
                finally:
                    os.close(descriptor)
            log_script = trusted_file(Path(__file__).with_name("log_reader.py"))
            log_python = trusted_file(Path("/usr/bin/python3").resolve(strict=True), executable=True)
            parent, birth = os.getpid(), process_birth(os.getpid())
            if not birth:
                raise RuntimeFailure("Cannot bind the journal reader to this broker process")
            unit.logger = await self.runner.start(
                "journal-reader", [str(log_python), "-I", str(log_script), "--parent", str(parent),
                                   "--birth", birth, "--unit", spec.name], log_path,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C.UTF-8"},
            )

            async def monitor():
                while True:
                    try:
                        await self.refresh(unit)
                    except RuntimeFailure as exc:
                        unit.error = str(exc)
                        return
                    await asyncio.sleep(0.5)
            unit.monitor = asyncio.create_task(monitor(), name=f"unit-{spec.role}")
            return unit
        except BaseException:
            # A timed-out/cancelled StartTransientUnit may have completed. Keep
            # the intent until exact verification proves rollback succeeded.
            await asyncio.shield(unit.stop())
            self.manifest.forget_unit(key)
            raise

    def prepare_gate(self, spec):
        if spec.gate:
            raise RuntimeFailure("A caller cannot supply its own privileged launch gate")
        script = trusted_file(Path(__file__).with_name("launch_gate.py"))
        python = trusted_file(Path("/usr/bin/python3").resolve(strict=True), executable=True)
        directory = self.manifest.state_dir / "launch-gates" / spec.name
        root_directory(directory.parent)
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)  # Broker umask may otherwise deny the worker traversal.
        info = directory.lstat()
        gate = LaunchGate(str(directory), info.st_ino, uuid4().hex, str(python), str(script), spec.command)
        return replace(spec, command=gate.command(spec.name), gate=gate,
                       binds=(*spec.binds, Bind(str(directory), str(VIEW / "gate"))))

    def prepare_pcm(self, spec):
        if self.pcm_exec_helper is None:
            raise RuntimeFailure("Local output requires the trusted inherited PCM ioctl filter launcher")
        helper = str(trusted_file(self.pcm_exec_helper, executable=True))
        if spec.pcm_exec:
            if spec.pcm_exec.helper != helper:
                raise RuntimeFailure("Caller-selected PCM filter does not match the trusted runtime helper")
            payload = spec.pcm_exec.payload
        else:
            payload = spec.command
        trusted_file(Path(payload[0]), executable=True)
        if spec.pcm_exec:
            return spec
        pcm = PCMExec(helper, payload)
        return replace(spec, command=pcm.command(), pcm_exec=pcm)

    @staticmethod
    def gate_directory(entry):
        record = entry["gate"]
        directory = Path(record["directory"])
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if info.st_uid != 0 or info.st_mode & 0o022 or info.st_ino != record["inode"]:
            os.close(descriptor)
            raise RuntimeFailure("Daemon launch gate directory was replaced")
        return directory, descriptor

    @staticmethod
    def discard_unreserved_gate(gate, descriptor):
        directory = Path(gate.directory)
        held = os.fstat(descriptor)
        parent = os.open(directory.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            try:
                named = os.stat(directory.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                raise RuntimeFailure("Unreserved launch gate was moved; preserve it for inspection") from None
            if ((named.st_dev, named.st_ino) != (held.st_dev, held.st_ino) or held.st_ino != gate.inode
                    or held.st_uid != 0 or not stat.S_ISDIR(named.st_mode) or os.listdir(descriptor)):
                raise RuntimeFailure("Unreserved launch gate changed; preserve it for inspection")
            os.rmdir(directory.name, dir_fd=parent)
        finally:
            os.close(parent)

    def publish_gate(self, entry, descriptor):
        directory, gate_fd = self.gate_directory(entry)
        try:
            if os.listdir(gate_fd):
                raise RuntimeFailure("Daemon launch gate already contains a release; refusing publication")
            info = os.fstat(descriptor)
            bind_proof(entry["bind_policy"], descriptor=descriptor, boot=entry["boot_id"], port=entry["listen_port"])
            atomic_json(directory / "ready.json", {
                "unit": entry["unit"], "nonce": entry["gate"]["nonce"], "boot_id": entry["boot_id"],
                "invocation_id": entry["invocation_id"], "cgroup_dev": info.st_dev, "cgroup_inode": info.st_ino,
            }, mode=0o644)
        finally:
            os.close(gate_fd)

    async def refresh(self, unit: OwnedUnit):
        actual = await self.inspect(unit.entry["unit"])
        if actual is None:
            unit.current, unit.process.returncode = {"ActiveState": "inactive"}, 0
            return
        invocation, group = self.verify(unit.entry, actual)
        unit.entry.update(invocation_id=invocation, control_group=group or unit.entry.get("control_group"))
        if group:
            try:
                descriptor = self.open_cgroup(unit.entry)
            except FileNotFoundError:
                if actual.get("ActiveState") in {"active", "activating"}:
                    raise RuntimeFailure("An active daemon has no kernel-owned cgroup") from None
            else:
                try:
                    unit.entry["cgroup_inode"] = os.fstat(descriptor).st_ino
                finally:
                    os.close(descriptor)
        if "policy_fence" not in unit.entry:
            # Preserve complex kernel-normalized masks/lists exactly instead of
            # reconstructing seccomp expansion or device major/minor mappings.
            keys = ["SystemCallFilter", "RestrictAddressFamilies", "InaccessiblePaths",
                    "DeviceAllow", "LimitCORE", "LimitMEMLOCK", "LimitRTPRIO",
                    "SendSIGKILL", "Restart", "StandardOutput", "StandardError"]
            if any(key not in actual for key in keys):
                raise RuntimeFailure("System manager omitted a daemon security property")
            unit.entry["policy_fence"] = {key: actual[key] for key in keys}
        unit.current = actual
        unit.process.pid = actual.get("MainPID", 0)
        unit.process.returncode = None if actual.get("ActiveState") in {"active", "activating"} else actual.get("ExecMainStatus", 1)
        if (unit.entry.get("bind_policy") and unit.alive
                and asyncio.get_running_loop().time() - unit.policy_checked_at >= 10):
            if self.bind_policy is None:
                raise RuntimeFailure("Cannot verify the admitted daemon kernel socket policy")
            descriptor = self.open_cgroup(unit.entry)
            try:
                await self.bind_policy.run("verify", descriptor, unit.entry["boot_id"], unit.entry["listen_port"],
                                           unit.entry["bind_policy"])
                unit.policy_checked_at = asyncio.get_running_loop().time()
            finally:
                os.close(descriptor)

    @staticmethod
    def open_cgroup(entry):
        group = entry.get("control_group")
        if not group:
            raise FileNotFoundError("Daemon never acquired a cgroup")
        path = Path("/sys/fs/cgroup") / group.lstrip("/")
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if (not stat.S_ISDIR(info.st_mode)
                or not is_cgroup2(descriptor)
                or (entry.get("cgroup_inode") and info.st_ino != entry["cgroup_inode"])):
            os.close(descriptor)
            raise RuntimeFailure("Daemon cgroup was replaced; resources stay reserved")
        return descriptor

    @staticmethod
    def cgroup_read(descriptor, filename):
        handle = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
        try:
            data = os.read(handle, 16385)
            if len(data) > 16384:
                raise RuntimeFailure("Daemon cgroup data exceeds its owned task bound")
            return data.decode("ascii")
        finally:
            os.close(handle)

    @staticmethod
    def cgroup_write(descriptor, filename, value):
        handle = os.open(filename, os.O_WRONLY | os.O_NOFOLLOW, dir_fd=descriptor)
        try:
            if os.write(handle, value) != len(value):
                raise RuntimeFailure("Cannot complete owned cgroup control operation")
        finally:
            os.close(handle)

    def cgroup_empty(self, entry):
        group = entry.get("control_group")
        if not group:
            return True
        try:
            descriptor = self.open_cgroup(entry)
        except FileNotFoundError:
            return True
        except OSError as exc:
            raise RuntimeFailure("Cannot prove daemon control-group termination") from exc
        try:
            return "populated 0" in self.cgroup_events(descriptor)
        finally:
            os.close(descriptor)

    def cgroup_events(self, descriptor):
        try:
            return self.cgroup_read(descriptor, "cgroup.events").splitlines()
        except FileNotFoundError:
            # This core file cannot be removed individually from a live v2
            # cgroup. Kernel cgroup_destroy_locked checks population is zero
            # before removing its files. Hold that exact filesystem/inode;
            # do not infer anything about a replacement at its former name.
            if is_cgroup2(descriptor):
                return ["populated 0", "frozen 0"]
            raise RuntimeFailure("A non-kernel control group lost its population proof") from None

    def write_control(self, descriptor, filename, value):
        try:
            self.cgroup_write(descriptor, filename, value)
            return True
        except FileNotFoundError:
            if "populated 0" in self.cgroup_events(descriptor):
                return False
            raise

    async def stop_saved(self, entry: dict):
        await self._stop_saved(entry)
        if entry.get("socket_publication"):
            discard_socket(entry["socket_publication"])
        if entry.get("gate"):
            try:
                directory, descriptor = self.gate_directory(entry)
            except FileNotFoundError:
                return
            try:
                names = os.listdir(descriptor)
                if set(names) - {"ready.json"}:
                    raise RuntimeFailure("Unexpected files in an owned launch gate; reservation retained")
                if "ready.json" in names:
                    os.unlink("ready.json", dir_fd=descriptor)
                directory.rmdir()
            finally:
                os.close(descriptor)

    async def _stop_saved(self, entry: dict):
        self.validate_saved(entry)
        actual = await self.inspect(entry["unit"])
        if actual is None:
            if not self.cgroup_empty(entry):
                raise RuntimeFailure("Daemon unit vanished with a populated control group")
            return
        invocation, group = self.verify(entry, actual)
        entry.update(invocation_id=invocation, control_group=group or entry.get("control_group"))
        try:
            descriptor = self.open_cgroup(entry)
        except FileNotFoundError:
            if actual.get("ActiveState") in {"inactive", "failed"}:
                return
            raise RuntimeFailure("Cannot prove an active daemon's cgroup ownership") from None
        pidfds = []
        frozen = False
        try:
            inode = os.fstat(descriptor).st_ino
            entry["cgroup_inode"] = inode
            if "populated 0" in self.cgroup_events(descriptor):
                return
            # Holding the directory FD binds every operation to this kernel
            # cgroup even if its name is deleted/reused concurrently. Freeze
            # the non-delegated group before capturing exact process handles.
            frozen = True
            if not self.write_control(descriptor, "cgroup.freeze", b"1"):
                return
            for _ in range(30):
                events = self.cgroup_events(descriptor)
                if "frozen 1" in events or "populated 0" in events:
                    break
                await asyncio.sleep(0.05)
            else:
                raise RuntimeFailure("Owned daemon cgroup did not quiesce; no process was signalled")
            current = await self.inspect(entry["unit"])
            if current is None:
                if "populated 0" in self.cgroup_events(descriptor):
                    return
                raise RuntimeFailure("Daemon unit disappeared before its frozen cgroup was proven empty")
            self.verify(entry, current)
            proof = self.open_cgroup(entry)
            try:
                if os.fstat(proof).st_ino != inode:
                    raise RuntimeFailure("Daemon cgroup changed before signal admission")
            finally:
                os.close(proof)
            # Worker units are explicitly non-delegated. Unexpected nested
            # cgroups are an ownership anomaly, not permission for broad kills.
            if any(stat.S_ISDIR(os.stat(name, dir_fd=descriptor, follow_symlinks=False).st_mode)
                   for name in os.listdir(descriptor)):
                raise RuntimeFailure("Daemon has an unexpected nested cgroup; no process was signalled")
            try:
                pids = self.cgroup_read(descriptor, "cgroup.procs").splitlines()
            except FileNotFoundError:
                if "populated 0" in self.cgroup_events(descriptor):
                    return
                raise
            if len(pids) > 128 or any(not item.isdecimal() or int(item) <= 1 for item in pids):
                raise RuntimeFailure("Owned daemon process set is invalid; no process was signalled")
            for pid in pids:
                with suppress(ProcessLookupError):
                    pidfds.append(os.pidfd_open(int(pid), 0))
            # A manager replacement is detected before any captured handle is
            # signalled. pidfds and the held cgroup can never target reused PIDs
            # or a replacement cgroup after this admission check.
            current = await self.inspect(entry["unit"])
            if current is None:
                if "populated 0" in self.cgroup_events(descriptor):
                    return
                raise RuntimeFailure("Daemon invocation disappeared before signal admission")
            self.verify(entry, current)
            for handle in pidfds:
                with suppress(ProcessLookupError):
                    signal.pidfd_send_signal(handle, signal.SIGTERM)
            if not self.write_control(descriptor, "cgroup.freeze", b"0"):
                return
            frozen = False
            for _ in range(60):
                if "populated 0" in self.cgroup_events(descriptor):
                    return
                await asyncio.sleep(0.1)
            current = await self.inspect(entry["unit"])
            if current is None:
                raise RuntimeFailure("Daemon unit disappeared before the final owned-group termination")
            self.verify(entry, current)
            proof = self.open_cgroup(entry)
            try:
                if os.fstat(proof).st_ino != inode:
                    raise RuntimeFailure("Daemon cgroup changed before final termination")
            finally:
                os.close(proof)
            # cgroup.kill is atomic for the originally opened kernel group,
            # including descendants/forks. No name-based manager kill is used.
            if not self.write_control(descriptor, "cgroup.kill", b"1"):
                return
            for _ in range(20):
                if "populated 0" in self.cgroup_events(descriptor):
                    return
                await asyncio.sleep(0.1)
            raise RuntimeFailure("Daemon cgroup still owns processes; resources remain reserved")
        except OSError as exc:
            raise RuntimeFailure("Cannot safely terminate the owned daemon cgroup; resources remain reserved") from exc
        finally:
            if frozen:
                with suppress(OSError):
                    self.write_control(descriptor, "cgroup.freeze", b"0")
            for handle in pidfds:
                os.close(handle)
            os.close(descriptor)
