"""Exact Bluetooth PCM admission and descriptor-only room handoff.

Only the root broker connects to the host D-Bus. Maintained PCM1.OpenRestricted
returns a writable PCM pipe and a restricted per-PCM SEQPACKET controller. A room receives those two
capabilities, never a system-bus connection, audio group or shared ALSA node.
The caller must stop its exact owned worker before releasing a handed-off lease.

Primary API: bluez-alsa 1a84465dd860d1be9dcf62339c6273e9e0632dd2,
doc/org.bluealsa.PCM1.7.rst and src/bluealsa-dbus.c:498-603. Pairing, codec
selection and physical latency are deliberately outside descriptor admission.
"""
from __future__ import annotations

import array
import asyncio
from contextlib import suppress
from dataclasses import asdict, dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
from uuid import UUID

from dbus_next import Message, MessageType, Variant
from dbus_next.aio import MessageBus

from shiri.deadline import bounded as _bounded

from .system import RuntimeFailure
from .unix_directory import PinnedUnixDirectory, identity as directory_identity

HOST_BUS = "unix:path=/run/dbus/system_bus_socket"
SERVICE = "org.bluealsa"
PCM_INTERFACE = "org.bluealsa.PCM1"
OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"
BUS_SERVICE = "org.freedesktop.DBus"
BUS_PATH = "/org/freedesktop/DBus"
MAX_OBJECTS = 256
MAX_HANDOFF = 4096
FORMATS = {0x8210: ("S16LE", 2), 0x8318: ("S24LE", 3),
           0x8418: ("S24_32LE", 4), 0x8420: ("S32LE", 4)}


def _uuid(value: str) -> str:
    try:
        result = UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuntimeFailure("Bluetooth admission needs an exact room and launch identity") from exc
    if result.int == 0:
        raise RuntimeFailure("Bluetooth admission cannot use an empty identity")
    return str(result)


def exact_mac(device: str) -> str:
    match = (re.fullmatch(r"bluealsa:DEV=([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}),PROFILE=a2dp", device)
             if isinstance(device, str) else None)
    if match is None or match[1].upper() in {"00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"}:
        raise RuntimeFailure("Bluetooth output requires one exact A2DP speaker address")
    return match[1].upper()


def _close_fds(descriptors):
    for descriptor in set(descriptors):
        if type(descriptor) is int and descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)


def _discard_reply(reply):
    descriptors, reply.unix_fds = reply.unix_fds, []
    _close_fds(descriptors)


class DescriptorBus:
    """A scoped D-Bus connection whose abandoned replies cannot leak FDs.

    dbus-next 0.2.3's call() drops a canceled reply without closing unix_fds.
    Use its public serial/send/handler APIs and retain exact reply ownership.
    Only shutdown inspects the pinned dependency's partial unmarshaller to close
    FDs already received for an incomplete message; no private method dispatch.
    """
    def __init__(self, bus):
        self.bus, self.pending, self.closed = bus, {}, False
        bus.add_message_handler(self._received)

    @classmethod
    async def connect(cls, timeout=3.0):
        if os.geteuid() != 0:
            raise RuntimeFailure("Only the root broker may admit host Bluetooth PCM")
        bus = MessageBus(bus_address=HOST_BUS, negotiate_unix_fd=True)
        connection = cls(bus)
        try:
            await _bounded(bus.connect(), timeout)
            return connection
        except BaseException:
            await connection.close()
            raise

    def _received(self, reply):
        if reply.message_type in {MessageType.METHOD_RETURN, MessageType.ERROR}:
            expected = self.pending.pop(reply.reply_serial, None)
            if expected is not None:
                future, sender = expected
                if not future.done() and reply.sender == sender and not self.closed:
                    future.set_result(reply)
                    return True
                if not future.done():
                    future.set_exception(RuntimeFailure("Bluetooth service reply identity changed"))
            if reply.unix_fds or expected is not None:
                _discard_reply(reply)
                return True
            # connect() owns its initial Hello reply. Do not swallow replies
            # handled by the library before our own calls have been registered.
            return False
        if reply.unix_fds:
            _discard_reply(reply)
        return False

    async def request(self, destination, path, interface, member, *, signature="", body=None, timeout=3.0):
        if self.closed:
            raise RuntimeFailure("Bluetooth service connection has closed")
        message = Message(destination=destination, path=path, interface=interface, member=member,
                          signature=signature, body=body or [], serial=self.bus.next_serial())
        future = asyncio.get_running_loop().create_future()
        self.pending[message.serial] = (future, destination)
        transferred = False
        try:
            async def exchange():
                await self.bus.send(message)
                return await future
            reply = await _bounded(exchange(), timeout)
            if reply.message_type != MessageType.METHOD_RETURN:
                _discard_reply(reply)
                raise RuntimeFailure(f"Bluetooth service rejected {member}")
            transferred = True
            return reply
        except asyncio.TimeoutError as exc:
            raise RuntimeFailure("Bluetooth service exceeded its request deadline") from exc
        finally:
            self.pending.pop(message.serial, None)
            if not transferred and future.done() and not future.cancelled():
                with suppress(Exception):
                    _discard_reply(future.result())
            if not future.done():
                future.cancel()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        for future, _sender in self.pending.values():
            if not future.done():
                future.set_exception(RuntimeFailure("Bluetooth service connection has closed"))
            elif not future.cancelled():
                with suppress(Exception):
                    _discard_reply(future.result())
        self.pending.clear()
        # Shutdown is synchronous with respect to event-loop readers. Nothing
        # can deliver a new descriptor after the reader has been removed; an
        # outer cancellation cannot interrupt halfway through FD cleanup.
        with suppress(Exception):
            self.bus.disconnect()
        descriptor = getattr(self.bus, "_fd", None)
        if descriptor is not None:
            with suppress(Exception):
                asyncio.get_running_loop().remove_reader(descriptor)
                asyncio.get_running_loop().remove_writer(descriptor)
        # The pinned 0.2.3 dependency does not close its socket or partial
        # descriptor list. This narrow shutdown seam is independently tested.
        unmarshaller = getattr(self.bus, "_unmarshaller", None)
        if unmarshaller is not None:
            _close_fds(getattr(unmarshaller, "unix_fds", []))
            unmarshaller.unix_fds = []
        with suppress(Exception):
            self.bus._sock.close()
        with suppress(Exception):
            self.bus._finalize(None)


def _property(properties, name, signature):
    value = properties.get(name)
    if not isinstance(value, Variant) or value.signature != signature:
        raise RuntimeFailure(f"Bluetooth PCM has no valid {name} property")
    return value.value


@dataclass(frozen=True)
class PCMEndpoint:
    owner: str
    path: str
    device_path: str
    mac: str
    sequence: int
    format_code: int
    format: str
    bytes_per_sample: int
    channels: int
    rate: int
    codec: str
    synchronous_drop: bool = False
    restricted_controller: bool = False

    @property
    def frame_bytes(self):
        return self.bytes_per_sample * self.channels


def select_endpoint(objects: dict, mac: str, owner: str) -> PCMEndpoint:
    if (not isinstance(objects, dict) or len(objects) > MAX_OBJECTS
            or not isinstance(owner, str) or re.fullmatch(r":[0-9]+\.[0-9]+", owner) is None):
        raise RuntimeFailure("Bluetooth service returned an invalid bounded inventory")
    candidates = []
    address = mac.replace(":", "_")
    for path, interfaces in objects.items():
        if not isinstance(interfaces, dict) or PCM_INTERFACE not in interfaces:
            continue
        properties = interfaces[PCM_INTERFACE]
        if not isinstance(properties, dict) or len(properties) > 64:
            raise RuntimeFailure("Bluetooth PCM properties are malformed")
        device_path = _property(properties, "Device", "o")
        match = re.fullmatch(r"/org/bluez/hci([0-9]+)/dev_([0-9A-Fa-f]{2}(?:_[0-9A-Fa-f]{2}){5})", device_path)
        if match is None or match[2].upper() != address:
            continue
        if (_property(properties, "Transport", "s") != "A2DP-source"
                or _property(properties, "Mode", "s") != "sink"):
            continue
        expected_path = f"/org/bluealsa/hci{match[1]}/dev_{match[2]}/"
        if (not isinstance(path, str) or not path.startswith(expected_path)
                or re.fullmatch(r"/[A-Za-z0-9_/]+", path) is None or len(path) > 256):
            raise RuntimeFailure("Bluetooth PCM does not belong to its exact device path")
        rate_name = "Rate" if "Rate" in properties else "Sampling"
        rate = _property(properties, rate_name, "u")
        if "Rate" in properties and "Sampling" in properties and _property(properties, "Sampling", "u") != rate:
            raise RuntimeFailure("Bluetooth PCM reports conflicting sample rates")
        format_code = _property(properties, "Format", "q")
        channels = _property(properties, "Channels", "y")
        sequence = _property(properties, "Sequence", "u")
        codec = _property(properties, "Codec", "s")
        synchronous_drop = _property(properties, "SynchronousDrop", "b")
        if type(synchronous_drop) is not bool or not synchronous_drop:
            raise RuntimeFailure("Bluetooth output requires a maintained BlueALSA with completed synchronous Drop")
        restricted_controller = _property(properties, "RestrictedController", "b")
        if type(restricted_controller) is not bool or not restricted_controller:
            raise RuntimeFailure("Bluetooth output requires a maintained restricted PCM controller")
        if (type(rate) is not int or not 8000 <= rate <= 192000
                or type(channels) is not int or not 1 <= channels <= 2
                or type(format_code) is not int or format_code not in FORMATS
                or type(sequence) is not int or not 0 <= sequence <= 2**32 - 1
                or not isinstance(codec, str) or not 1 <= len(codec) <= 64
                or any(ord(value) < 32 or ord(value) == 127 for value in codec)):
            raise RuntimeFailure("Bluetooth PCM format or connection identity is unsupported")
        format_name, width = FORMATS[format_code]
        candidates.append(PCMEndpoint(owner, path, device_path, mac, sequence, format_code,
                                      format_name, width, channels, rate, codec, synchronous_drop,
                                      restricted_controller))
    if len(candidates) != 1:
        raise RuntimeFailure("Bluetooth speaker must expose one unambiguous connected A2DP playback PCM")
    return candidates[0]


def validate_pcm_fds(descriptors):
    if (not isinstance(descriptors, (list, tuple)) or len(descriptors) != 2
            or any(type(value) is not int or value < 0 for value in descriptors)
            or descriptors[0] == descriptors[1]):
        raise RuntimeFailure("Bluetooth admission requires exactly two distinct PCM descriptors")
    pcm, control = descriptors
    if (not stat.S_ISFIFO(os.fstat(pcm).st_mode)
            or fcntl.fcntl(pcm, fcntl.F_GETFL) & os.O_ACCMODE != os.O_WRONLY):
        raise RuntimeFailure("Bluetooth PCM descriptor is not a writable stream pipe")
    with socket.socket(fileno=os.dup(control)) as controller:
        if (controller.family != socket.AF_UNIX or controller.type != socket.SOCK_SEQPACKET
                or not controller.getpeername() == ""):
            raise RuntimeFailure("Bluetooth controller is not the selected local SEQPACKET channel")
    for descriptor in descriptors:
        os.set_inheritable(descriptor, False)
        os.set_blocking(descriptor, False)


class BlueALSA:
    def __init__(self, *, transport_factory=DescriptorBus.connect, service_uid=0):
        if type(service_uid) is not int or service_uid < 0:
            raise RuntimeFailure("Bluetooth daemon must have one explicit trusted UID")
        self.transport_factory, self.service_uid = transport_factory, service_uid
        self.leases = {}
        self.lock = asyncio.Lock()

    async def admit(self, device, room_id, generation):
        mac, room_id, generation = exact_mac(device), _uuid(room_id), _uuid(generation)
        token = object()
        async with self.lock:
            if mac in self.leases:
                raise RuntimeFailure("Bluetooth speaker remains owned until its exact output worker stops")
            self.leases[mac] = token
        transport, descriptors, completed = None, [], False
        try:
            transport = await self.transport_factory()
            owner = await _owner(transport)
            reply = await transport.request(BUS_SERVICE, BUS_PATH, BUS_SERVICE, "GetConnectionUnixUser",
                                            signature="s", body=[owner])
            try:
                if reply.signature != "u" or reply.body != [self.service_uid] or reply.unix_fds:
                    raise RuntimeFailure("Bluetooth daemon is not the trusted service identity")
            finally:
                _discard_reply(reply)
            endpoint = await _endpoint(transport, mac, owner)
            reply = await transport.request(owner, endpoint.path, PCM_INTERFACE, "OpenRestricted")
            try:
                if (reply.signature != "hh" or reply.body != [0, 1]
                        or any(type(value) is not int for value in reply.body)):
                    raise RuntimeFailure("Bluetooth PCM OpenRestricted returned a malformed descriptor envelope")
                descriptors, reply.unix_fds = reply.unix_fds, []
                validate_pcm_fds(descriptors)
            finally:
                _discard_reply(reply)
            if await _owner(transport) != owner or await _endpoint(transport, mac, owner) != endpoint:
                raise RuntimeFailure("Bluetooth speaker changed while admitting its PCM")
            admission = PCMAdmission(self, token, transport, endpoint, descriptors, room_id, generation)
            completed = True
            return admission
        finally:
            if not completed:
                _close_fds(descriptors)
                if transport:
                    await transport.close()
                async with self.lock:
                    if self.leases.get(mac) is token:
                        self.leases.pop(mac)


async def _owner(transport):
    reply = await transport.request(BUS_SERVICE, BUS_PATH, BUS_SERVICE, "GetNameOwner", signature="s", body=[SERVICE])
    try:
        if (reply.signature != "s" or len(reply.body) != 1 or reply.unix_fds
                or not isinstance(reply.body[0], str) or re.fullmatch(r":[0-9]+\.[0-9]+", reply.body[0]) is None):
            raise RuntimeFailure("Bluetooth daemon has no valid unique service owner")
        return reply.body[0]
    finally:
        _discard_reply(reply)


async def _endpoint(transport, mac, owner):
    reply = await transport.request(owner, "/org/bluealsa", OBJECT_MANAGER, "GetManagedObjects")
    try:
        if reply.signature != "a{oa{sa{sv}}}" or len(reply.body) != 1 or reply.unix_fds:
            raise RuntimeFailure("Bluetooth service returned malformed PCM inventory")
        return select_endpoint(reply.body[0], mac, owner)
    finally:
        _discard_reply(reply)


class PCMAdmission:
    def __init__(self, manager, token, transport, endpoint, descriptors, room_id, generation):
        self.manager, self.token, self.transport, self.endpoint = manager, token, transport, endpoint
        self.descriptors, self.room_id, self.generation = tuple(descriptors), room_id, generation
        self.worker_pid, self.closed, self.error = None, False, None

    def envelope(self):
        return {"version": 1, "room_id": self.room_id, "generation": self.generation,
                "endpoint": asdict(self.endpoint)}

    async def check(self):
        if self.closed or self.error:
            raise RuntimeFailure(self.error or "Bluetooth PCM lease has closed")
        try:
            if (await _owner(self.transport) != self.endpoint.owner
                    or await _endpoint(self.transport, self.endpoint.mac, self.endpoint.owner) != self.endpoint):
                raise RuntimeFailure("Bluetooth speaker reconnected or changed its admitted PCM")
            validate_pcm_fds(self.descriptors)
        except Exception as exc:
            self.error = str(exc) or "Bluetooth PCM validation failed"
            raise
        return self.envelope()

    async def close(self, *, verified_unit_stopped=False):
        if self.closed:
            return
        if self.worker_pid is not None and not verified_unit_stopped:
            raise RuntimeFailure("Stop the exact Bluetooth worker before releasing its PCM lease")
        self.closed = True
        _close_fds(self.descriptors)
        try:
            await self.transport.close()
        finally:
            async with self.manager.lock:
                if self.manager.leases.get(self.endpoint.mac) is self.token:
                    self.manager.leases.pop(self.endpoint.mac)


def peer_identity(peer):
    if hasattr(socket, "SO_PEERCRED"):
        return struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[:2]
    # macOS tests can prove descriptor lifecycle and same-UID denial but cannot
    # establish the production Linux PID contract with getpeereid alone.
    raise RuntimeFailure("Bluetooth handoff requires Linux kernel PID and UID credentials")


async def _descriptor_ready(descriptor, write=False, timeout=3.0):
    loop, future = asyncio.get_running_loop(), asyncio.get_running_loop().create_future()
    deadline = loop.time() + timeout
    def ready():
        if not future.done():
            if loop.time() >= deadline:
                future.set_exception(asyncio.TimeoutError())
            else:
                future.set_result(None)
    def expired():
        if not future.done():
            future.set_exception(asyncio.TimeoutError())
    add, remove = (loop.add_writer, loop.remove_writer) if write else (loop.add_reader, loop.remove_reader)
    add(descriptor, ready)
    timer = loop.call_later(max(0, timeout), expired)
    try:
        # Direct Future cancellation cannot be swallowed by an already-ready
        # child inside the Python3.10 wait_for implementation.
        await future
    finally:
        timer.cancel()
        remove(descriptor)


async def _socket_ready(peer, write=False, timeout=3.0):
    await _descriptor_ready(peer.fileno(), write=write, timeout=timeout)


async def _receive(peer, deadline):
    while True:
        if asyncio.get_running_loop().time() >= deadline:
            raise asyncio.TimeoutError
        try:
            data, controls, flags, _address = peer.recvmsg(
                MAX_HANDOFF, socket.CMSG_SPACE(16 * array.array("i").itemsize), getattr(socket, "MSG_CMSG_CLOEXEC", 0))
            fds = []
            for level, kind, contents in controls:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    values = array.array("i")
                    values.frombytes(contents[:len(contents) - len(contents) % values.itemsize])
                    fds.extend(values)
            if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or not data:
                _close_fds(fds)
                raise RuntimeFailure("Bluetooth handoff packet was truncated or closed")
            return data, fds
        except BlockingIOError:
            await _socket_ready(peer, timeout=max(0.001, deadline - asyncio.get_running_loop().time()))


async def _send(peer, data, descriptors, deadline):
    controls = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", descriptors))] if descriptors else []
    while True:
        if asyncio.get_running_loop().time() >= deadline:
            raise asyncio.TimeoutError
        try:
            count = peer.sendmsg([data], controls)
            if count != len(data):
                raise RuntimeFailure("Bluetooth handoff did not send one complete descriptor packet")
            return
        except BlockingIOError:
            await _socket_ready(peer, write=True, timeout=max(0.001, deadline - asyncio.get_running_loop().time()))


def _decode(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=unique,
                          parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("constant")))
    except (ValueError, UnicodeError) as exc:
        raise RuntimeFailure("Bluetooth handoff JSON is malformed") from exc


class HandoffServer:
    """One exact launched worker can claim one descriptor pair; no replay."""
    def __init__(self, path: Path, admission: PCMAdmission, gid: int, *, root_uid=0):
        self.path, self.admission, self.root_uid = Path(path), admission, root_uid
        self.parent, self.socket_descriptor = None, None
        parent = self.path.parent.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != root_uid or parent.st_mode & 0o022
                or parent.st_gid != gid or os.geteuid() != root_uid
                or self.path.exists() or self.path.is_symlink()):
            raise RuntimeFailure("Bluetooth handoff needs a new socket below a protected root directory")
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            self.parent = PinnedUnixDirectory(self.path.parent, uid=root_uid, gid=gid,
                                              mode=stat.S_IMODE(parent.st_mode))
            with self.parent.address(self.path.name) as address:
                self.listener.bind(str(address))
            # Hold the created filesystem inode before metadata/listen can
            # fail. A replacement entry can never inherit this rollback proof.
            self.socket_descriptor = os.open(self.path.name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC,
                                             dir_fd=self.parent.descriptor)
            created = os.fstat(self.socket_descriptor)
            if not stat.S_ISSOCK(created.st_mode) or created.st_nlink != 1:
                raise RuntimeFailure('Bluetooth handoff did not create its exact socket')
            self.parent.verify()
            os.chown(self.path.name, root_uid, gid, dir_fd=self.parent.descriptor, follow_symlinks=False)
            os.chmod(self.path.name, 0o660, dir_fd=self.parent.descriptor)
            created = os.stat(self.path.name, dir_fd=self.parent.descriptor, follow_symlinks=False)
            if not stat.S_ISSOCK(created.st_mode) or created.st_nlink != 1:
                raise RuntimeFailure('Bluetooth handoff did not create its exact socket')
            self.socket_identity = directory_identity(created)
            self.inode = created.st_ino
            self.listener.listen(1)
            self.listener.setblocking(False)
        except BaseException as original:
            self.listener.close()
            rollback_error = None
            try:
                if self.socket_descriptor is not None:
                    self._unlink_created_socket()
            except BaseException as exc:
                rollback_error = exc
            finally:
                self._close_pins()
            if rollback_error is not None:
                raise original from rollback_error
            raise
        self.expected = None
        self.delivered, self.closed, self.inflight = False, False, False

    def authorize(self, uid: int, pid: int):
        if (self.expected is not None or type(uid) is not int or uid <= 0
                or type(pid) is not int or pid <= 0):
            raise RuntimeFailure("Bluetooth handoff needs one exact launched worker identity")
        self.expected = (pid, uid)
        self.admission.worker_pid = pid

    async def wait_delivered(self, timeout=3.0):
        if self.closed or self.delivered or self.inflight or self.expected is None:
            raise RuntimeFailure("Bluetooth handoff is unavailable or has already been claimed")
        self.inflight = True
        deadline = asyncio.get_running_loop().time() + timeout
        peer = None
        try:
            peer, _address = await _bounded(asyncio.get_running_loop().sock_accept(self.listener), timeout,
                                           abandoned=lambda accepted: accepted[0].close())
            peer.setblocking(False)
            if peer_identity(peer) != self.expected:
                raise RuntimeFailure("Bluetooth handoff peer does not own this exact worker launch")
            data, fds = await _receive(peer, deadline)
            try:
                expected = {"version": 1, "room_id": self.admission.room_id, "generation": self.admission.generation}
                request = _decode(data)
                if (fds or not isinstance(request, dict) or type(request.get("version")) is not int
                        or request != expected):
                    raise RuntimeFailure("Bluetooth handoff belongs to another room or launch")
            finally:
                _close_fds(fds)
            await _bounded(self.admission.check(), max(0.001, deadline - asyncio.get_running_loop().time()))
            encoded = json.dumps(self.admission.envelope(), separators=(",", ":")).encode()
            await _send(peer, encoded, self.admission.descriptors, deadline)
            self.delivered = True
        finally:
            if peer:
                peer.close()
            self.close()

    def _unlink_created_socket(self, expected=None):
        self.parent.verify()
        held = os.fstat(self.socket_descriptor)
        if (not stat.S_ISSOCK(held.st_mode)
                or (expected is not None and directory_identity(held) != expected)):
            raise RuntimeFailure('Bluetooth handoff socket changed credentials; refusing unlink')
        with suppress(FileNotFoundError):
            current = os.stat(self.path.name, dir_fd=self.parent.descriptor, follow_symlinks=False)
            if (not stat.S_ISSOCK(current.st_mode) or current.st_nlink != 1
                    or directory_identity(current) != directory_identity(held)):
                raise RuntimeFailure('Bluetooth handoff socket was replaced; refusing unlink')
            os.unlink(self.path.name, dir_fd=self.parent.descriptor)

    def _close_pins(self):
        descriptor, self.socket_descriptor = self.socket_descriptor, None
        if descriptor is not None:
            os.close(descriptor)
        if self.parent is not None:
            self.parent.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.listener.close()
        try:
            self._unlink_created_socket(self.socket_identity)
        finally:
            self._close_pins()


async def receive_handoff(path, room_id, generation, *, expected_root_uid=0, timeout=3.0):
    room_id, generation = _uuid(room_id), _uuid(generation)
    peer, descriptors = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET), []
    peer.setblocking(False)
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        await _bounded(asyncio.get_running_loop().sock_connect(peer, str(path)), timeout)
        if peer_identity(peer)[1] != expected_root_uid:
            raise RuntimeFailure("Bluetooth handoff server is not the root broker")
        await _send(peer, json.dumps({"version": 1, "room_id": room_id, "generation": generation}).encode(), [], deadline)
        data, descriptors = await _receive(peer, deadline)
        envelope = _decode(data)
        if (not isinstance(envelope, dict) or set(envelope) != {"version", "room_id", "generation", "endpoint"}
                or type(envelope["version"]) is not int or envelope["version"] != 1
                or envelope["room_id"] != room_id or envelope["generation"] != generation
                or not isinstance(envelope["endpoint"], dict)):
            raise RuntimeFailure("Bluetooth descriptor receipt belongs to another room or launch")
        try:
            endpoint = PCMEndpoint(**envelope["endpoint"])
        except (TypeError, ValueError) as exc:
            raise RuntimeFailure("Bluetooth descriptor receipt has invalid PCM capabilities") from exc
        if (type(endpoint.synchronous_drop) is not bool or not endpoint.synchronous_drop
                or type(endpoint.restricted_controller) is not bool or not endpoint.restricted_controller):
            raise RuntimeFailure("Bluetooth descriptor receipt lacks completed Drop and restricted controller capabilities")
        # Reuse the typed inventory validator; cached arbitrary format/rate
        # fields cannot make an unvalidated receipt look like an exact endpoint.
        properties = {"Device": Variant("o", endpoint.device_path), "Transport": Variant("s", "A2DP-source"),
                      "Mode": Variant("s", "sink"), "Sequence": Variant("u", endpoint.sequence),
                      "Format": Variant("q", endpoint.format_code), "Channels": Variant("y", endpoint.channels),
                      "Rate": Variant("u", endpoint.rate), "Codec": Variant("s", endpoint.codec),
                      "SynchronousDrop": Variant("b", endpoint.synchronous_drop),
                      "RestrictedController": Variant("b", endpoint.restricted_controller)}
        if select_endpoint({endpoint.path: {PCM_INTERFACE: properties}}, endpoint.mac, endpoint.owner) != endpoint:
            raise RuntimeFailure("Bluetooth descriptor capabilities are inconsistent")
        validate_pcm_fds(descriptors)
        result, descriptors = (endpoint, tuple(descriptors)), []
        return result
    finally:
        _close_fds(descriptors)
        peer.close()
