"""Exact endpoint admission and real descriptor lifecycle; no Bluetooth I/O."""
import asyncio
import os
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from dbus_next import Message, MessageType, Variant
import pytest

from shiri.runtime import bluealsa as ba
from shiri.runtime.system import RuntimeFailure

MAC = "AA:BB:CC:DD:EE:FF"
DEVICE = f"bluealsa:DEV={MAC},PROFILE=a2dp"
PATH = "/org/bluealsa/hci0/dev_AA_BB_CC_DD_EE_FF/a2dpsrc/sink"


def inventory(*, adapter=0, **changes):
    properties = {"Device": Variant("o", f"/org/bluez/hci{adapter}/dev_AA_BB_CC_DD_EE_FF"),
                  "Transport": Variant("s", "A2DP-source"), "Mode": Variant("s", "sink"),
                  "Sequence": Variant("u", 12), "Format": Variant("q", 0x8210),
                  "Channels": Variant("y", 2), "Rate": Variant("u", 48000), "Codec": Variant("s", "SBC"),
                  "SynchronousDrop": Variant("b", True), "RestrictedController": Variant("b", True)}
    properties.update(changes)
    path = PATH.replace("hci0", f"hci{adapter}")
    return {path: {ba.PCM_INTERFACE: properties}}


@pytest.fixture
def descriptors():
    if sys.platform != "linux":
        pytest.skip("Real BlueALSA descriptor admission requires Linux AF_UNIX SOCK_SEQPACKET")
    read, write = os.pipe()
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        yield read, write, client, server
    finally:
        for fd in (read, write):
            try:
                os.close(fd)
            except OSError:
                pass
        client.close()
        server.close()


def closed(fd):
    with pytest.raises(OSError):
        os.fstat(fd)


class FakeTransport:
    def __init__(self, descriptors):
        _read, write, client, _server = descriptors
        self.original = [write, client.fileno()]
        self.received = []
        self.objects = inventory()
        self.owner, self.uid, self.closed = ":1.22", 0, False
        self.open_body = [0, 1]
        self.opened = asyncio.Event()
        self.block_open = None
        self.before_reply = None

    async def request(self, destination, path, interface, member, **kwargs):
        if member == "GetNameOwner":
            return Message(message_type=MessageType.METHOD_RETURN, signature="s", body=[self.owner], reply_serial=1)
        if member == "GetConnectionUnixUser":
            return Message(message_type=MessageType.METHOD_RETURN, signature="u", body=[self.uid], reply_serial=1)
        if member == "GetManagedObjects":
            return Message(message_type=MessageType.METHOD_RETURN, signature="a{oa{sa{sv}}}",
                           body=[self.objects], reply_serial=1)
        assert member == "OpenRestricted" and destination == ":1.22" and path == PATH
        self.opened.set()
        if self.block_open:
            await self.block_open.wait()
        fds = [os.dup(value) for value in self.original]
        self.received.extend(fds)
        if self.before_reply:
            self.before_reply()
        return Message(message_type=MessageType.METHOD_RETURN, signature="hh", body=self.open_body,
                       unix_fds=fds, reply_serial=1)

    async def close(self):
        self.closed = True


async def admission(descriptors):
    transport = FakeTransport(descriptors)
    async def factory():
        return transport
    manager = ba.BlueALSA(transport_factory=factory)
    result = await manager.admit(DEVICE, str(uuid4()), uuid4().hex)
    return manager, transport, result


@pytest.mark.parametrize("device", [None, 123, "bluealsa", "bluealsa:DEV=00:00:00:00:00:00,PROFILE=a2dp",
                                   "bluealsa:DEV=FF:FF:FF:FF:FF:FF,PROFILE=a2dp",
                                   DEVICE + ",SRV=other", DEVICE.replace("a2dp", "sco")])
def test_only_exact_a2dp_device_identifiers_can_be_admitted(device):
    with pytest.raises(RuntimeFailure):
        ba.exact_mac(device)


def test_address_case_normalizes_and_owner_inventory_are_exact():
    assert ba.exact_mac(DEVICE.lower().replace("profile", "PROFILE").replace("dev", "DEV")) == MAC
    endpoint = ba.select_endpoint(inventory(), MAC, ":1.22")
    assert endpoint.owner == ":1.22" and endpoint.frame_bytes == 4 and endpoint.rate == 48000
    assert endpoint.sequence == 12 and endpoint.device_path.endswith("dev_AA_BB_CC_DD_EE_FF")
    assert endpoint.synchronous_drop is True
    assert endpoint.restricted_controller is True


@pytest.mark.parametrize("capability", [None, False, "wrong_type", "integer_bool"])
@pytest.mark.parametrize("property_name", ["SynchronousDrop", "RestrictedController"])
async def test_stock_or_untyped_capability_is_rejected_before_pcm_open(capability, property_name):
    objects = inventory()
    properties = objects[PATH][ba.PCM_INTERFACE]
    if capability is None:
        properties.pop(property_name)
    elif capability == "wrong_type":
        properties[property_name] = Variant("s", "true")
    elif capability == "integer_bool":
        # D-Bus normally rejects this too. The admission boundary must require
        # an actual bool rather than trust truthiness of a cached property.
        properties[property_name].value = 1
    else:
        properties[property_name] = Variant("b", capability)
    transport = SimpleNamespace(closed=False, members=[])
    async def request(destination, path, interface, member, **kwargs):
        transport.members.append(member)
        if member == "GetNameOwner":
            return Message(message_type=MessageType.METHOD_RETURN, signature="s", body=[":1.22"], reply_serial=1)
        if member == "GetConnectionUnixUser":
            return Message(message_type=MessageType.METHOD_RETURN, signature="u", body=[0], reply_serial=1)
        assert member == "GetManagedObjects", "Unsupported stock daemon must never receive PCM Open"
        return Message(message_type=MessageType.METHOD_RETURN, signature="a{oa{sa{sv}}}",
                       body=[objects], reply_serial=1)
    async def close():
        transport.closed = True
    async def factory():
        return transport
    transport.request, transport.close = request, close
    manager = ba.BlueALSA(transport_factory=factory)
    with pytest.raises(RuntimeFailure, match="SynchronousDrop|synchronous Drop|RestrictedController|restricted PCM"):
        await manager.admit(DEVICE, str(uuid4()), uuid4().hex)
    assert transport.closed and not manager.leases
    assert not {"Open", "OpenRestricted"}.intersection(transport.members)


@pytest.mark.parametrize("field,value", [("Rate", Variant("u", 0)), ("Rate", Variant("u", 192001)),
                                        ("Format", Variant("q", 0xFFFF)), ("Channels", Variant("y", 0)),
                                        ("Channels", Variant("y", 3)), ("Codec", Variant("s", "")),
                                        ("Sequence", Variant("s", "12"))])
def test_malformed_caps_cannot_be_silently_defaulted(field, value):
    with pytest.raises(RuntimeFailure):
        ba.select_endpoint(inventory(**{field: value}), MAC, ":1.22")


@pytest.mark.parametrize("format_code", list(ba.FORMATS))
def test_every_admitted_format_has_explicit_physical_sample_width(format_code):
    endpoint = ba.select_endpoint(inventory(Format=Variant("q", format_code)), MAC, ":1.22")
    assert (endpoint.format, endpoint.bytes_per_sample) == ba.FORMATS[format_code]


def test_same_mac_on_two_adapters_is_ambiguous_even_if_path_or_sequence_differs():
    with pytest.raises(RuntimeFailure, match="unambiguous"):
        ba.select_endpoint(inventory() | inventory(adapter=1), MAC, ":1.22")


def test_inventory_cannot_use_a_counterpart_or_another_devices_pcm_path():
    for objects in [inventory(Mode=Variant("s", "source")), inventory(Transport=Variant("s", "A2DP-sink"))]:
        with pytest.raises(RuntimeFailure, match="unambiguous"):
            ba.select_endpoint(objects, MAC, ":1.22")
    with pytest.raises(RuntimeFailure, match="exact device path"):
        ba.select_endpoint({PATH.replace("hci0", "hci1"): inventory()[PATH]}, MAC, ":1.22")


def test_legacy_sampling_property_is_supported_but_conflicting_rate_is_rejected():
    objects = inventory(Sampling=Variant("u", 44100))
    properties = objects[PATH][ba.PCM_INTERFACE]
    properties.pop("Rate")
    assert ba.select_endpoint(objects, MAC, ":1.22").rate == 44100
    properties["Rate"] = Variant("u", 48000)
    with pytest.raises(RuntimeFailure, match="conflicting"):
        ba.select_endpoint(objects, MAC, ":1.22")


def test_descriptor_types_and_direction_are_kernel_validated(descriptors):
    read, write, client, _server = descriptors
    ba.validate_pcm_fds([write, client.fileno()])
    assert not os.get_inheritable(write) and not os.get_blocking(write)
    for invalid in [[read, client.fileno()], [write, write], [write], [True, client.fileno()]]:
        with pytest.raises(RuntimeFailure):
            ba.validate_pcm_fds(invalid)
    one, two = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeFailure, match="SEQPACKET"):
            ba.validate_pcm_fds([write, one.fileno()])
    finally:
        one.close()
        two.close()


async def test_admission_keeps_a_real_pipe_and_closes_it_only_after_owned_worker_stop(descriptors):
    read, _write, _client, _server = descriptors
    manager, transport, admitted = await admission(descriptors)
    os.write(admitted.descriptors[0], b"target audio")
    assert os.read(read, 12) == b"target audio"
    admitted.worker_pid = os.getpid()
    with pytest.raises(RuntimeFailure, match="exact Bluetooth worker"):
        await admitted.close()
    with pytest.raises(RuntimeFailure, match="remains owned"):
        await manager.admit(DEVICE, str(uuid4()), uuid4().hex)
    assert not transport.closed and manager.leases
    await admitted.close(verified_unit_stopped=True)
    await admitted.close()
    for fd in transport.received:
        closed(fd)
    assert transport.closed and not manager.leases


@pytest.mark.parametrize("fault", ["untrusted-user", "malformed-handles", "service-replaced", "reconnected",
                                   "drop-downgrade", "controller-downgrade"])
async def test_failed_admission_closes_all_real_received_fds_and_releases_only_its_reservation(descriptors, fault):
    transport = FakeTransport(descriptors)
    if fault == "untrusted-user":
        transport.uid = 1000
    elif fault == "malformed-handles":
        transport.open_body = [1, 0]
    elif fault == "service-replaced":
        transport.before_reply = lambda: setattr(transport, "owner", ":1.23")
    elif fault == "drop-downgrade":
        transport.before_reply = lambda: setattr(transport, "objects", inventory(SynchronousDrop=Variant("b", False)))
    elif fault == "controller-downgrade":
        transport.before_reply = lambda: setattr(transport, "objects", inventory(RestrictedController=Variant("b", False)))
    else:
        transport.before_reply = lambda: setattr(transport, "objects", inventory(Sequence=Variant("u", 13)))
    async def factory():
        return transport
    manager = ba.BlueALSA(transport_factory=factory)
    with pytest.raises(RuntimeFailure):
        await manager.admit(DEVICE, str(uuid4()), uuid4().hex)
    assert transport.closed and not manager.leases
    for fd in transport.received:
        closed(fd)


async def test_cancellation_during_open_is_not_an_available_speaker_lease(descriptors):
    transport = FakeTransport(descriptors)
    transport.block_open = asyncio.Event()
    async def factory():
        return transport
    manager = ba.BlueALSA(transport_factory=factory)
    task = asyncio.create_task(manager.admit(DEVICE, str(uuid4()), uuid4().hex))
    await transport.opened.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert transport.closed and not manager.leases


async def test_reconnected_endpoint_failure_stays_sticky_even_if_it_returns(descriptors):
    _manager, transport, admitted = await admission(descriptors)
    transport.objects = inventory(Sequence=Variant("u", 13))
    with pytest.raises(RuntimeFailure, match="reconnected"):
        await admitted.check()
    transport.objects = inventory()
    with pytest.raises(RuntimeFailure, match="reconnected"):
        await admitted.check()
    await admitted.close()


@pytest.mark.parametrize("property_name,message", [("SynchronousDrop", "completed synchronous Drop"),
                                                   ("RestrictedController", "restricted PCM controller")])
async def test_lost_capability_stays_failed_until_exact_lease_retirement(descriptors, property_name, message):
    manager, transport, admitted = await admission(descriptors)
    transport.objects = inventory(**{property_name: Variant("b", False)})
    with pytest.raises(RuntimeFailure, match=message):
        await admitted.check()
    transport.objects = inventory()
    with pytest.raises(RuntimeFailure, match=message):
        await admitted.check()
    assert manager.leases and not admitted.closed
    await admitted.close()
    assert not manager.leases and transport.closed


async def test_ready_descriptor_and_parent_cancel_in_same_turn_never_suppress_cancel(monkeypatch):
    loop, callbacks, removed = asyncio.get_running_loop(), [], []
    monkeypatch.setattr(loop, "add_writer", lambda fd, callback: callbacks.append((fd, callback)))
    monkeypatch.setattr(loop, "remove_writer", lambda fd: removed.append(fd))
    task = asyncio.create_task(ba._descriptor_ready(12345, write=True, timeout=0.5))
    await asyncio.sleep(0)
    assert len(callbacks) == 1
    callbacks[0][1]()
    # Python3.10 wait_for swallowed this cancel if its inner Future was already
    # ready; the direct readiness Future must preserve the parent's cancel.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert removed == [12345]


async def test_completed_socket_accept_abandoned_by_cancel_closes_exact_socket():
    loop = asyncio.get_running_loop()
    accepted, other = socket.socketpair()
    future = loop.create_future()
    task = asyncio.create_task(ba._bounded(future, None, abandoned=lambda result: result[0].close()))
    try:
        await asyncio.sleep(0)
        future.set_result((accepted, "test address"))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert accepted.fileno() == -1 and other.fileno() >= 0
    finally:
        accepted.close()
        other.close()


async def test_late_completed_resource_is_retired_without_success():
    accepted, other = socket.socketpair()
    future = asyncio.get_running_loop().create_future()
    future.set_result((accepted, "test address"))
    try:
        with pytest.raises(asyncio.TimeoutError):
            await ba._bounded(future, 0, abandoned=lambda result: result[0].close())
        assert accepted.fileno() == -1 and other.fileno() >= 0
    finally:
        accepted.close()
        other.close()


async def test_parent_cancellation_joins_inflight_operation_before_return():
    started, stopped = asyncio.Event(), asyncio.Event()
    async def operation():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    task = asyncio.create_task(ba._bounded(operation(), 0.5))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()


class FakeBus:
    def __init__(self):
        self.handlers, self.messages, self.serial = [], [], 0
        self.sent = asyncio.Event()
        self._sock, self.peer = socket.socketpair()
        self._fd = self._sock.fileno()
        self._unmarshaller = SimpleNamespace(unix_fds=[])
        self._finalize = Mock()
    def add_message_handler(self, handler):
        self.handlers.append(handler)
    def next_serial(self):
        self.serial += 1
        return self.serial
    def send(self, message):
        self.messages.append(message)
        self.sent.set()
        done = asyncio.get_running_loop().create_future()
        done.set_result(None)
        return done
    def disconnect(self):
        pass
    def deliver(self, message):
        return self.handlers[0](message)


async def test_abandoned_dbus_reply_and_partial_unmarshal_fds_are_closed(descriptors):
    _read, write, client, _server = descriptors
    bus = FakeBus()
    transport = ba.DescriptorBus(bus)
    try:
        with pytest.raises(RuntimeFailure, match="deadline"):
            await transport.request(":1.22", PATH, ba.PCM_INTERFACE, "Open", timeout=0.01)
        leaked = [os.dup(write), os.dup(client.fileno())]
        reply = Message(message_type=MessageType.METHOD_RETURN, signature="hh", body=[0, 1], unix_fds=leaked,
                        sender=":1.22", reply_serial=bus.messages[0].serial)
        assert bus.deliver(reply) is True
        for fd in leaked:
            closed(fd)
        partial = os.dup(write)
        bus._unmarshaller.unix_fds.append(partial)
        await transport.close()
        closed(partial)
        assert not transport.pending and bus._sock.fileno() == -1
        bus._finalize.assert_called_once_with(None)
    finally:
        await transport.close()
        bus.peer.close()


async def test_dependency_hello_reply_is_not_swallowed_by_descriptor_cleanup():
    bus = FakeBus()
    transport = ba.DescriptorBus(bus)
    try:
        reply = Message(message_type=MessageType.METHOD_RETURN, signature="s", body=[":1.2"],
                        sender=ba.BUS_SERVICE, reply_serial=1)
        assert bus.deliver(reply) is False
    finally:
        await transport.close()
        bus.peer.close()


async def test_wrong_service_owner_reply_is_rejected_and_its_fds_are_closed(descriptors):
    _read, write, client, _server = descriptors
    bus = FakeBus()
    transport = ba.DescriptorBus(bus)
    try:
        task = asyncio.create_task(transport.request(":1.22", PATH, ba.PCM_INTERFACE, "Open"))
        await asyncio.wait_for(bus.sent.wait(), 1)
        fds = [os.dup(write), os.dup(client.fileno())]
        reply = Message(message_type=MessageType.METHOD_RETURN, signature="hh", body=[0, 1], unix_fds=fds,
                        sender=":1.23", reply_serial=bus.messages[0].serial)
        bus.deliver(reply)
        with pytest.raises(RuntimeFailure, match="reply identity"):
            await task
        for fd in fds:
            closed(fd)
    finally:
        await transport.close()
        bus.peer.close()


@pytest.fixture
def handoff_path():
    # Short Unix paths also exercise SCM_RIGHTS on macOS. Linux kernel peer
    # credentials are injected explicitly in portable tests below.
    from tempfile import TemporaryDirectory
    with TemporaryDirectory(prefix="shiri-fds-", dir="/tmp") as directory:
        os.chmod(directory, 0o700)
        yield Path(directory) / "admission.sock"


async def test_one_exact_room_launch_receives_two_real_fds_without_system_bus_access(
    handoff_path, descriptors, monkeypatch,
):
    _manager, _transport, admitted = await admission(descriptors)
    monkeypatch.setattr(ba, "peer_identity", lambda _peer: (os.getpid(), 1001))
    server = ba.HandoffServer(handoff_path, admitted, os.getgid(), root_uid=os.geteuid())
    server.authorize(1001, os.getpid())
    receiving = asyncio.create_task(ba.receive_handoff(handoff_path, admitted.room_id, admitted.generation,
                                                      expected_root_uid=1001))
    try:
        await server.wait_delivered()
        endpoint, transferred = await receiving
        assert endpoint == admitted.endpoint and len(transferred) == 2
        assert all(not os.get_inheritable(fd) for fd in transferred)
        os.write(transferred[0], b"only this PCM")
        assert os.read(descriptors[0], 13) == b"only this PCM"
        ba._close_fds(transferred)
        with pytest.raises(RuntimeFailure, match="claimed"):
            await server.wait_delivered()
        assert not handoff_path.exists()
    finally:
        server.close()
        await asyncio.gather(receiving, return_exceptions=True)
        await admitted.close(verified_unit_stopped=True)


@pytest.mark.parametrize("field", ["synchronous_drop", "restricted_controller"])
@pytest.mark.parametrize("capability", [None, False, 1, "true"])
async def test_invalid_capability_receipt_closes_transferred_fds_but_retains_root_lease(
    handoff_path, descriptors, monkeypatch, field, capability,
):
    manager, _transport, admitted = await admission(descriptors)
    original_envelope, original_receive = admitted.envelope, ba._receive
    received = []
    def envelope():
        result = original_envelope()
        if capability is None:
            result["endpoint"].pop(field)
        else:
            result["endpoint"][field] = capability
        return result
    async def receive(peer, deadline):
        data, fds = await original_receive(peer, deadline)
        received.extend(fds)
        return data, fds
    monkeypatch.setattr(admitted, "envelope", envelope)
    monkeypatch.setattr(ba, "_receive", receive)
    monkeypatch.setattr(ba, "peer_identity", lambda _peer: (os.getpid(), 1001))
    server = ba.HandoffServer(handoff_path, admitted, os.getgid(), root_uid=os.geteuid())
    server.authorize(1001, os.getpid())
    receiving = asyncio.create_task(ba.receive_handoff(handoff_path, admitted.room_id, admitted.generation,
                                                      expected_root_uid=1001))
    try:
        await server.wait_delivered()
        with pytest.raises(RuntimeFailure, match="capabilities"):
            await receiving
        assert len(received) == 2
        for fd in received:
            closed(fd)
        assert manager.leases and not admitted.closed
        for fd in admitted.descriptors:
            os.fstat(fd)
    finally:
        server.close()
        await asyncio.gather(receiving, return_exceptions=True)
        await admitted.close(verified_unit_stopped=True)


@pytest.mark.parametrize("fault", ["other-pid", "other-uid", "other-room", "old-generation"])
async def test_handoff_does_not_cross_worker_process_room_or_restart_boundary(
    handoff_path, descriptors, monkeypatch, fault,
):
    _manager, _transport, admitted = await admission(descriptors)
    actual = (os.getpid() + (fault == "other-pid"), 1001 + (fault == "other-uid"))
    monkeypatch.setattr(ba, "peer_identity", lambda _peer: actual)
    server = ba.HandoffServer(handoff_path, admitted, os.getgid(), root_uid=os.geteuid())
    server.authorize(1001, os.getpid())
    room = str(uuid4()) if fault == "other-room" else admitted.room_id
    generation = str(uuid4()) if fault == "old-generation" else admitted.generation
    receiving = asyncio.create_task(ba.receive_handoff(handoff_path, room, generation,
                                                      expected_root_uid=actual[1], timeout=0.2))
    try:
        with pytest.raises(RuntimeFailure):
            await server.wait_delivered(timeout=0.2)
        result = await asyncio.gather(receiving, return_exceptions=True)
        assert isinstance(result[0], Exception) and not server.delivered
        # Even failed handoff cannot prematurely clear an already launched
        # worker's endpoint lease without proving that exact unit stopped.
        with pytest.raises(RuntimeFailure, match="exact Bluetooth worker"):
            await admitted.close()
    finally:
        server.close()
        await admitted.close(verified_unit_stopped=True)


async def test_two_concurrent_claims_cannot_transfer_a_second_descriptor_pair(handoff_path, descriptors, monkeypatch):
    _manager, _transport, admitted = await admission(descriptors)
    monkeypatch.setattr(ba, "peer_identity", lambda _peer: (os.getpid(), 1001))
    server = ba.HandoffServer(handoff_path, admitted, os.getgid(), root_uid=os.geteuid())
    server.authorize(1001, os.getpid())
    first = asyncio.create_task(server.wait_delivered(timeout=0.1))
    await asyncio.sleep(0)
    try:
        with pytest.raises(RuntimeFailure, match="claimed"):
            await server.wait_delivered(timeout=0.1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not handoff_path.exists()
    finally:
        server.close()
        await admitted.close(verified_unit_stopped=True)


@pytest.mark.skipif(sys.platform != "linux", reason="Actual SO_PEERCRED is a Linux kernel boundary")
def test_production_peer_credentials_are_kernel_pid_and_uid_not_json_fields():
    one, two = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        assert ba.peer_identity(one) == (os.getpid(), os.geteuid())
        assert ba.peer_identity(two) == (os.getpid(), os.geteuid())
    finally:
        one.close()
        two.close()


async def test_real_kernel_peer_cannot_claim_a_different_uid_even_with_correct_room_payload(
    handoff_path, descriptors,
):
    _manager, _transport, admitted = await admission(descriptors)
    server = ba.HandoffServer(handoff_path, admitted, os.getgid(), root_uid=os.geteuid())
    server.authorize(os.geteuid() + 1, os.getpid())
    receiving = asyncio.create_task(ba.receive_handoff(handoff_path, admitted.room_id, admitted.generation,
                                                      expected_root_uid=os.geteuid(), timeout=0.2))
    try:
        with pytest.raises(RuntimeFailure, match="exact worker launch"):
            await server.wait_delivered(timeout=0.2)
        result = await asyncio.gather(receiving, return_exceptions=True)
        assert isinstance(result[0], Exception) and not server.delivered
    finally:
        server.close()
        await admitted.close(verified_unit_stopped=True)
