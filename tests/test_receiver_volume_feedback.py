"""Actual control sockets and native actor; no phone UI or crypto claims."""
import asyncio
from dataclasses import replace
from pathlib import Path
import socket
import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from shiri.rpc import RpcError
from shiri.runtime.native import NativeController, NativeHandle, NativeMixer
from shiri.runtime.receiver_volume import BIND, BOUND, SET, REPLY, Message, SIZE, ZERO, OK, STALE, UNAVAILABLE
from shiri.runtime.timing import FLAG_AIRPLAY2, Kind, Packet


class Writer:
    reader_present = True
    written_bytes = dropped_bytes = 0
    def reset(self, owner):
        self.owner = owner
    def close(self):
        pass


@pytest.fixture
async def native():
    async def request(method, path, *, json):
        return json
    mixer = NativeMixer(Path("/unused"), writer=Writer())
    controller = NativeController(str(uuid4()), mixer, SimpleNamespace(request=request), control_volume=23)
    await controller.initialize()
    try:
        yield controller
    finally:
        await controller.close()


async def raw_receive(right):
    loop = asyncio.get_running_loop()
    if right.type == socket.SOCK_STREAM:
        data = b""
        while len(data) < SIZE:
            chunk = await loop.sock_recv(right, SIZE-len(data))
            if not chunk:
                return data
            data += chunk
        return data
    return await loop.sock_recv(right, SIZE+1)


async def connection(native):
    try:
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    except OSError:
        # macOS lacks Unix SEQPACKET; portable lifecycle cases use one fixed
        # message per write. Linux runs the actual production packet socket.
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    left.setblocking(False)
    right.setblocking(False)
    native.receiver_volume.connection = left
    task = asyncio.create_task(native.receiver_volume.run(left))
    native.receiver_volume.task = task
    loop = asyncio.get_running_loop()
    await loop.sock_sendall(right, Message(BIND).encode())
    bind = Message.decode(await asyncio.wait_for(raw_receive(right), 1))
    assert bind.kind == BOUND and bind.volume == 23 and not bind.notify
    return right, task


async def receive(right):
    return Message.decode(await asyncio.wait_for(raw_receive(right), 1))


async def answer(right, packet, status=OK):
    await asyncio.get_running_loop().sock_sendall(right, replace(packet, kind=REPLY, status=status).encode())
    await asyncio.sleep(0)


@pytest.mark.parametrize("field,value", [("volume", -1), ("volume", 101), ("volume", True),
                                            ("revision", 0), ("revision", 2**63), ("notify", 1),
                                            ("session", b"a"*16), ("epoch", 1), ("epoch", True), ("generation", False)])
def test_invalid_wire_fields(field, value):
    with pytest.raises(ValueError):
        replace(Message(SET), **{field: value}).encode()


def test_fixed_wire_bounds_and_reserved_bytes():
    packet = Message(SET, uuid4().bytes, uuid4().bytes, 2, 7, 11, 39, True)
    assert len(packet.encode()) == SIZE == 80 and Message.decode(packet.encode()) == packet
    for index in [0, 6, 7, *range(72, 80)]:
        data = bytearray(packet.encode())
        data[index] ^= 1
        with pytest.raises(ValueError):
            Message.decode(data)
    for length in [0, 79, 81]:
        with pytest.raises(ValueError):
            Message.decode(bytes(length))


async def test_idle_master_updates_next_connect_default_without_notification(native):
    right, _task = await connection(native)
    first = await receive(right)
    assert first.session == ZERO and not first.notify
    await answer(right, first)
    native.control_intent(2)
    native.receiver_volume.queue(2, 0, True)
    packet = await receive(right)
    assert packet.revision == 2 and packet.volume == 0 and packet.session == ZERO and not packet.notify
    await answer(right, packet)
    right.close()


async def test_latest_edit_coalesces_and_old_ack_never_reports_new_revision(native):
    right, _task = await connection(native)
    packet = await receive(right)
    native.control_intent(2)
    native.receiver_volume.queue(2, 12, True)
    native.control_intent(3)
    native.receiver_volume.queue(3, 69, True)
    await answer(right, packet)
    latest = await receive(right)
    assert (latest.revision, latest.volume) == (3, 69)
    assert native.receiver_volume.result['status'] == 'queued'
    await answer(right, latest)
    await asyncio.sleep(.01)
    assert native.receiver_volume.result['revision'] == 3
    right.close()


async def test_active_source_generation_fences_and_phone_echo_does_not_create_intent(native):
    handle = NativeHandle(native)
    grant = await native.begin(Packet(Kind.BEGIN, uuid4().bytes, flags=FLAG_AIRPLAY2), handle)
    right, _task = await connection(native)
    first = await receive(right)
    await answer(right, first)
    native.control_intent(2)
    native.receiver_volume.queue(2, 39, True)
    packet = await receive(right)
    assert packet.notify and packet.session != ZERO and packet.generation == 1
    await native.message(replace(grant, kind=Kind.VOLUME, frames=39), handle)
    assert native.events == []
    await native.message(replace(grant, kind=Kind.VOLUME, frames=41), handle)
    assert native.events[-1]['volume'] == 41
    await answer(right, packet)
    flushed = await native.message(replace(grant, kind=Kind.FLUSH, generation=2), handle)
    # Generation one echoes cannot suppress a generation two phone edit.
    before = len(native.events)
    await native.message(replace(flushed, kind=Kind.VOLUME, frames=39), handle)
    assert len(native.events) >= before and native.events[-1]['volume'] == 39
    right.close()


async def test_wrong_reply_disconnects_feedback_and_retains_music(native):
    handle = NativeHandle(native)
    grant = await native.begin(Packet(Kind.BEGIN, uuid4().bytes, flags=FLAG_AIRPLAY2), handle)
    right, task = await connection(native)
    packet = await receive(right)
    await answer(right, replace(packet, revision=packet.revision+1))
    await asyncio.wait_for(task, 1)
    assert native.actor.owns(handle.token) and not handle.closed
    assert native.receiver_volume.result['status'] == 'control_disconnected'
    assert grant.session_id == handle.token.session_id
    right.close()


async def test_feedback_timeout_and_shutdown_bound_without_audio_retirement(native):
    handle = NativeHandle(native)
    await native.begin(Packet(Kind.BEGIN, uuid4().bytes, flags=FLAG_AIRPLAY2), handle)
    native.receiver_volume.deadline = .03
    right, task = await connection(native)
    await receive(right)
    await asyncio.wait_for(task, .5)
    assert native.actor.owns(handle.token) and not handle.closed
    right.close()
    native.receiver_volume.deadline = 1
    right, task = await connection(native)
    await receive(right)
    await asyncio.wait_for(native.receiver_volume.close(), .5)
    assert task.done() and native.actor.owns(handle.token)
    right.close()


async def test_queue_requires_current_revision_and_explicit_origin(native):
    native.control_intent(3)
    for revision, volume, notify in [(2, 30, True), (3, True, True), (3, 101, False), (3, 30, 1)]:
        with pytest.raises(RpcError):
            native.receiver_volume.queue(revision, volume, notify)
    native.receiver_volume.queue(3, 30, False)
    assert native.receiver_volume.volume == 30 and not native.receiver_volume.notify


async def test_retry_unavailable_is_bounded_to_three_per_source_edit(native):
    right, task = await connection(native)
    for _ in range(3):
        packet = await receive(right)
        await answer(right, packet, UNAVAILABLE)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.get_running_loop().sock_recv(right, SIZE+1), .6)
    native.receiver_volume.queue(1, 45, True)
    packet = await receive(right)
    assert packet.volume == 45
    await answer(right, packet)
    right.close()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.skipif(not hasattr(socket, "SO_PEERCRED"), reason="Linux production peer credentials and SEQPACKET")
async def test_real_listener_checks_peer_uid_and_closes_replaced_binding(native, tmp_path):
    path = tmp_path / "volume.sock"
    lane = native.receiver_volume
    await lane.listen(path, os.getuid()+1)
    rejected = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    rejected.setblocking(False)
    loop = asyncio.get_running_loop()
    await loop.sock_connect(rejected, str(path))
    assert await asyncio.wait_for(loop.sock_recv(rejected, 80), .5) == b""
    rejected.close()
    await lane.close()
    lane.closed = False
    lane.server = lane.task = lane.listener = None
    await lane.listen(path, os.getuid())
    admitted = []
    for _ in range(2):
        receiver = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        receiver.setblocking(False)
        await loop.sock_connect(receiver, str(path))
        await loop.sock_sendall(receiver, Message(BIND).encode())
        bound = await receive(receiver)
        assert bound.kind == BOUND
        packet = await receive(receiver)
        await answer(receiver, packet)
        admitted.append(receiver)
    assert await asyncio.wait_for(loop.sock_recv(admitted[0], 80), .5) == b""
    for receiver in admitted:
        receiver.close()
    await asyncio.wait_for(lane.close(), .5)
    assert not path.exists()


async def test_web_notification_never_retargets_a_successor_phone_session(native):
    old = NativeHandle(native)
    await native.begin(Packet(Kind.BEGIN, uuid4().bytes, flags=FLAG_AIRPLAY2), old)
    native.receiver_volume.queue(1, 39, True)
    old_packet = native.receiver_volume.packet()
    assert old_packet.notify
    successor = NativeHandle(native)
    await native.begin(Packet(Kind.BEGIN, uuid4().bytes, flags=FLAG_AIRPLAY2), successor)
    packet = native.receiver_volume.packet()
    assert packet.session != old_packet.session and not packet.notify
    assert packet.volume == 39
    # A fresh edit deliberately targets the current admitted sender.
    native.receiver_volume.queue(1, 41, True)
    assert native.receiver_volume.packet().notify


async def test_idle_edit_updates_default_and_does_not_notify_a_future_phone(native):
    native.receiver_volume.queue(1, 39, True)
    new = NativeHandle(native)
    await native.begin(Packet(Kind.BEGIN, uuid4().bytes, flags=FLAG_AIRPLAY2), new)
    packet = native.receiver_volume.packet()
    assert packet.volume == 39 and not packet.notify


async def test_idle_feedback_eof_is_detected_without_new_volume_edit(native):
    right, task = await connection(native)
    packet = await receive(right)
    await answer(right, packet)
    right.close()
    await asyncio.wait_for(task, 1)
    assert native.receiver_volume.result['status'] == 'control_disconnected'


async def test_feedback_does_not_require_python311_timeout_context(native, monkeypatch):
    monkeypatch.delattr(asyncio, "timeout", raising=False)
    right, _task = await connection(native)
    packet = await receive(right)
    await answer(right, packet)
    native.receiver_volume.queue(1, 12, False)
    packet = await receive(right)
    assert packet.volume == 12
    await answer(right, packet)
    right.close()


async def active_feedback(native):
    handle = NativeHandle(native)
    grant = await native.begin(Packet(Kind.BEGIN, uuid4().bytes, flags=FLAG_AIRPLAY2), handle)
    right, task = await connection(native)
    first = await receive(right)
    return handle, grant, right, task, first


async def wait_feedback(predicate):
    from test_runtime_review import wait_until
    await wait_until(predicate)


async def test_same_master_revision_retains_pending_notification_and_exact_ack_clears_it(native):
    _handle, _grant, right, _task, first = await active_feedback(native)
    native.control_intent(2)
    native.receiver_volume.queue(2, 69, True)
    pending = native.receiver_volume.pending_notification
    native.control_intent(3)
    assert not native.receiver_volume.applied_intent and not native.receiver_volume.notify
    assert native.receiver_volume.pending_notification is pending
    native.receiver_volume.queue(3, 69, False)  # Trim/name/duck ACK with unchanged master.
    await answer(right, first)
    packet = await receive(right)
    assert packet.revision == 3 and packet.volume == 69 and packet.notify
    await answer(right, packet)
    await wait_feedback(lambda: native.receiver_volume.pending_notification is None)
    assert native.receiver_volume.result["sender_notified"]
    native.control_intent(4)
    native.receiver_volume.queue(4, 69, False)
    newest = await receive(right)
    assert newest.revision == 4 and not newest.notify
    await answer(right, newest)
    right.close()


async def test_old_revision_ack_satisfies_only_the_same_retained_pending_command(native):
    _handle, _grant, right, _task, first = await active_feedback(native)
    await answer(right, first)
    native.control_intent(2)
    native.receiver_volume.queue(2, 69, True)
    old = await receive(right)
    pending = native.receiver_volume.pending_notification
    native.control_intent(3)
    native.receiver_volume.queue(3, 69, False)
    assert native.receiver_volume.pending_notification is pending
    await answer(right, old)
    newest = await receive(right)
    assert newest.revision == 3 and not newest.notify and newest.volume == 69
    assert native.receiver_volume.pending_notification is None
    assert native.receiver_volume.result["status"] == "queued", "Old ACK must not report a newer revision delivered"
    await answer(right, newest)
    right.close()


@pytest.mark.parametrize("replacement", [69, 84])
async def test_old_ack_never_clears_a_replacement_notification_even_at_same_value(native, replacement):
    _handle, _grant, right, _task, first = await active_feedback(native)
    await answer(right, first)
    native.control_intent(2)
    native.receiver_volume.queue(2, 69, True)
    old = await receive(right)
    pending = native.receiver_volume.pending_notification
    native.control_intent(3)
    native.receiver_volume.queue(3, replacement, True)
    assert native.receiver_volume.pending_notification is not pending
    await answer(right, old)
    newest = await receive(right)
    assert newest.notify and newest.volume == replacement and newest.revision == 3
    assert native.receiver_volume.pending_notification is not None
    await answer(right, newest)
    await wait_feedback(lambda: native.receiver_volume.pending_notification is None)
    right.close()


async def test_phone_origin_replacement_master_cancels_pending_web_command(native):
    _handle, _grant, right, _task, first = await active_feedback(native)
    native.control_intent(2)
    native.receiver_volume.queue(2, 69, True)
    native.control_intent(3)
    native.receiver_volume.queue(3, 41, False)  # Durable phone ACK carries a different master.
    assert native.receiver_volume.pending_notification is None
    await answer(right, first)
    latest = await receive(right)
    assert latest.volume == 41 and not latest.notify
    await answer(right, latest)
    right.close()


async def test_same_value_phone_ack_can_preserve_only_redundant_original_source_feedback(native):
    handle, _grant, _right, _task, _first = await active_feedback(native)
    native.control_intent(2)
    native.receiver_volume.queue(2, 69, True)
    pending = native.receiver_volume.pending_notification
    native.control_intent(3)
    native.receiver_volume.queue(3, 69, False)  # Same wire payload as a same-master administrative ACK.
    assert native.receiver_volume.pending_notification is pending
    packet = native.receiver_volume.packet()
    assert packet.notify and packet.session == UUID(handle.token.session_id).bytes and packet.volume == 69
    _right.close()


async def test_preserved_notification_never_retargets_a_successor_source(native):
    old, _grant, right, _task, first = await active_feedback(native)
    native.control_intent(2)
    native.receiver_volume.queue(2, 69, True)
    successor = NativeHandle(native)
    await native.begin(Packet(Kind.BEGIN, uuid4().bytes, flags=FLAG_AIRPLAY2), successor)
    native.control_intent(3)
    native.receiver_volume.queue(3, 69, False)
    assert native.receiver_volume.pending_notification is None
    await answer(right, first)
    packet = await receive(right)
    assert packet.session != UUID(old.token.session_id).bytes and packet.session == UUID(successor.token.session_id).bytes
    assert packet.volume == 69 and not packet.notify
    await answer(right, packet)
    right.close()


async def test_old_generation_ack_preserves_pending_command_for_current_generation(native):
    handle, grant, right, _task, first = await active_feedback(native)
    await answer(right, first)
    native.control_intent(2)
    native.receiver_volume.queue(2, 69, True)
    old = await receive(right)
    await native.message(replace(grant, kind=Kind.FLUSH, generation=2), handle)
    await answer(right, old)
    current = await receive(right)
    assert current.generation == 2 and current.notify and current.volume == 69
    await answer(right, current)
    await wait_feedback(lambda: native.receiver_volume.pending_notification is None)
    right.close()


@pytest.mark.parametrize("failure", [STALE, UNAVAILABLE])
async def test_failed_event_ack_preserves_notification_and_new_revision_gets_bounded_retry(native, failure):
    _handle, _grant, right, _task, first = await active_feedback(native)
    await answer(right, first)
    native.control_intent(2)
    native.receiver_volume.queue(2, 69, True)
    pending = native.receiver_volume.pending_notification
    for _ in range(3):
        packet = await receive(right)
        assert packet.notify and packet.revision == 2
        await answer(right, packet, failure)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(raw_receive(right), .6)
    assert native.receiver_volume.pending_notification is pending
    native.control_intent(3)
    native.receiver_volume.queue(3, 69, False)
    newest = await receive(right)
    assert newest.revision == 3 and newest.notify
    await answer(right, newest)
    await wait_feedback(lambda: native.receiver_volume.pending_notification is None)
    right.close()
