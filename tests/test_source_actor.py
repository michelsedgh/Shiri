"""Exercise the runtime write boundary under takeover and delayed transport work."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
from uuid import uuid4

import pytest
from pydantic import ValidationError

from shiri.domain import Conflict, ValidationIssue
from shiri.runtime.source import SourceActor


class Handle:
    def __init__(self, *, wait=None, ignore_cancel=False, on_close=None):
        self.wait = wait
        self.ignore_cancel = ignore_cancel
        self.on_close = on_close
        self.closed_tokens = []
        self.volumes = []

    async def quiesce(self, token):
        self.closed_tokens.append(token)
        if self.wait:
            while not self.wait.is_set():
                try:
                    await self.wait.wait()
                except asyncio.CancelledError:
                    if not self.ignore_cancel:
                        raise
        if self.on_close:
            await self.on_close(token)

    def set_volume(self, token, volume):
        self.volumes.append((token, volume))


def actor(**kwargs):
    fences = []
    return SourceActor(str(uuid4()), fences.append, **kwargs), fences


async def settle():
    # Complete the scheduled transport callback and its done/watchdog handlers.
    for _ in range(4):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_abort_closes_exact_resource_when_retirement_is_cancelled_before_it_starts():
    import socket

    class SocketHandle(Handle):
        def __init__(self, connection):
            super().__init__()
            self.connection = connection
            self.aborted = []

        def abort(self, token):
            self.aborted.append(token)
            self.connection.close()

    old_socket, old_peer = socket.socketpair()
    new_socket, new_peer = socket.socketpair()
    old_peer.settimeout(1)
    new_peer.settimeout(1)
    owner, _ = actor()
    first, successor = SocketHandle(old_socket), SocketHandle(new_socket)
    try:
        old = await owner.admit_native("airplay2", "old", first)
        current = await owner.admit_native("chromecast", "successor", successor)
        task = next(iter(owner._revocations))
        task.cancel()  # quiesce() has not executed its first instruction.
        await settle()
        assert not first.closed_tokens
        assert first.aborted == [old]
        assert old_peer.recv(1) == b""
        assert owner.write(current, lambda: new_socket.sendall(b"new"))
        assert new_peer.recv(3) == b"new"
        assert not owner.write(old, lambda: pytest.fail("Cancelled old producer remained writable"))
        await owner.close()
    finally:
        for connection in (old_socket, old_peer, new_socket, new_peer):
            connection.close()


@pytest.mark.asyncio
async def test_competing_protocols_fence_buffered_and_final_pcm_before_transport_closes():
    queued, submitted = [], []
    def fence(_token):
        queued.clear()
    owner = SourceActor(str(uuid4()), fence, revoke_timeout=0.02)
    unblock = asyncio.Event()
    airplay = Handle(wait=unblock)
    cast = Handle()
    old = await owner.admit_native("airplay2", "airplay-connection-1", airplay)
    assert owner.write(old, lambda: queued.append((old, b"old music")))
    late_mixed_packet = queued[0]
    new = await owner.admit_native("chromecast", "cast-connection-1", cast)
    assert not queued
    assert not airplay.closed_tokens  # Shutdown has not even had a turn yet.
    assert not owner.write(late_mixed_packet[0], lambda: submitted.append(late_mixed_packet[1]))
    assert owner.write(new, lambda: submitted.append(b"new music"))
    assert not await owner.end_native(old)
    assert not await owner.volume_native(old, 5)
    assert submitted == [b"new music"]
    assert owner.snapshot()["owner"] == new.model_dump()
    unblock.set()
    await owner.close()
    assert airplay.closed_tokens == [old]
    assert cast.closed_tokens == [new]


@pytest.mark.asyncio
async def test_delayed_disconnect_callback_cannot_stop_its_successor():
    owner, fences = actor()
    async def delayed_callback(token):
        assert not await owner.end_native(token)
        assert not await owner.volume_native(token, 0)
    first = Handle(on_close=delayed_callback)
    second = Handle()
    old = await owner.admit_native("chromecast", "old", first)
    new = await owner.admit_native("airplay2", "new", second)
    await settle()
    assert owner.owns(new)
    assert first.closed_tokens == [old]
    assert not second.volumes
    assert fences == [old, new]
    await owner.close()


@pytest.mark.asyncio
async def test_idempotence_requires_the_same_exact_handle_and_session():
    owner, fences = actor()
    source = Handle()
    token = await owner.admit_native("airplay2", "session", source)
    assert await owner.admit_native("airplay2", "session", source) == token
    with pytest.raises(Conflict, match="identity cannot be reused"):
        await owner.admit_native("airplay2", "session", Handle())
    with pytest.raises(Conflict, match="own exact transport"):
        await owner.admit_native("airplay2", "different-incarnation", source)
    assert fences == [token]
    await owner.close()


@pytest.mark.asyncio
async def test_cross_zone_events_never_become_implicit_admission():
    one, _ = actor()
    two, _ = actor()
    token = await one.admit_native("airplay2", "session", Handle())
    assert not two.write(token, lambda: pytest.fail("Wrong-zone PCM was admitted"))
    with pytest.raises(ValidationIssue, match="another exact zone"):
        await two.end_native(token)
    with pytest.raises(ValidationIssue, match="another exact zone"):
        await two.volume_native(token, 100)
    await one.close()
    await two.close()


@pytest.mark.asyncio
async def test_restart_rejects_old_token_even_with_same_native_id_and_epoch():
    old_actor, _ = actor()
    old = await old_actor.admit_native("airplay2", "reusable-native-id", Handle())
    await old_actor.close()
    fresh = SourceActor(old.zone_id, lambda _token: None)
    new = await fresh.admit_native("airplay2", "reusable-native-id", Handle())
    assert old.epoch == new.epoch == 1
    assert old.incarnation != new.incarnation
    assert not fresh.write(old, lambda: pytest.fail("Old token matched restarted actor"))
    assert not await fresh.end_native(old)
    assert not await fresh.volume_native(old, 0)
    assert fresh.owns(new)
    await fresh.close()


@pytest.mark.asyncio
async def test_external_token_and_snapshot_mutation_cannot_change_live_owner():
    owner, _ = actor()
    token = await owner.admit_native("airplay2", "exact", Handle())
    copy = token.model_copy(deep=True)
    with pytest.raises(ValidationError, match="frozen"):
        token.session_id = "mutated"
    # Even an in-process adapter bypassing Pydantic's frozen setter cannot
    # mutate the actor's private copy of its returned token.
    object.__setattr__(token, "session_id", "mutated")
    state = owner.snapshot()
    state["owner"]["session_id"] = "also-mutated"
    assert owner.owns(copy)
    assert not owner.owns(token)
    await owner.close()


@pytest.mark.asyncio
async def test_persistence_failure_does_not_clear_previous_program():
    entries = []
    def persist(state):
        if entries:
            raise OSError("storage full")
        entries.append(state)
    owner, fences = actor(persist=persist)
    first = Handle()
    old = await owner.admit_native("airplay2", "first", first)
    with pytest.raises(OSError, match="storage full"):
        await owner.admit_native("chromecast", "second", Handle())
    assert owner.owns(old)
    assert fences == [old]
    assert not first.closed_tokens
    assert entries[0].owner == old
    await owner.close()


@pytest.mark.asyncio
async def test_failed_fence_fails_closed_and_retires_old_exact_transport():
    fences = []
    def fence(token):
        if token and fences:
            raise RuntimeError("Cannot clear music queue")
        fences.append(token)
    owner = SourceActor(str(uuid4()), fence)
    first = Handle()
    old = await owner.admit_native("airplay2", "first", first)
    with pytest.raises(RuntimeError, match="Cannot clear"):
        await owner.admit_native("chromecast", "second", Handle())
    assert owner.snapshot()["owner"] is None
    assert not owner.snapshot()["ready"]
    assert not owner.write(old, lambda: pytest.fail("Broken fence still writes music"))
    with pytest.raises(Conflict, match="unavailable"):
        await owner.admit_native("airplay2", "third", Handle())
    await settle()
    assert first.closed_tokens == [old]
    await owner.close()


@pytest.mark.asyncio
async def test_uncooperative_retirement_is_bounded_and_keeps_admission_backpressure():
    release = asyncio.Event()
    owner, _ = actor(revoke_timeout=0.005, max_pending_revocations=1)
    slow = Handle(wait=release, ignore_cancel=True)
    old = await owner.admit_native("airplay2", "slow", slow)
    new = await owner.admit_native("chromecast", "current", Handle())
    await asyncio.sleep(0.02)
    assert owner.snapshot()["retirement_failures"] == 1
    assert owner.snapshot()["pending_revocations"] == 1
    assert not owner.owns(old)
    assert owner.owns(new)
    with pytest.raises(Conflict, match="still closing"):
        await owner.admit_native("airplay2", "third", Handle())
    await asyncio.wait_for(owner.close(), timeout=0.1)
    assert not owner.owns(new)
    release.set()
    await settle()
    assert owner.snapshot()["pending_revocations"] == 0


@pytest.mark.asyncio
async def test_final_write_and_admission_are_atomic_across_threads():
    owner, fences = actor()
    old = await owner.admit_native("airplay2", "first", Handle())
    in_write, release_write = threading.Event(), threading.Event()
    writes = []
    def final_write():
        in_write.set()
        assert release_write.wait(1)
        writes.append("old-before-grant")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(owner.write, old, final_write)
        assert await asyncio.to_thread(in_write.wait, 1)
        # Another thread releases the final write while admission waits for
        # the same media lock; no write can slip after the new grant fence.
        threading.Timer(0.02, release_write.set).start()
        new = await owner.admit_native("chromecast", "second", Handle())
        assert future.result(timeout=1)
    assert fences == [old, new]
    assert not owner.write(old, lambda: writes.append("forbidden-old-after-grant"))
    assert owner.write(new, lambda: writes.append("new-after-grant"))
    assert writes == ["old-before-grant", "new-after-grant"]
    await owner.close()


@pytest.mark.asyncio
async def test_volume_has_exact_token_and_ending_silences_program_writes():
    owner, fences = actor()
    handle = Handle()
    token = await owner.admit_native("airplay2", "first", handle)
    assert await owner.volume_native(token, 77)
    assert handle.volumes == [(token, 77)]
    assert owner.snapshot()["volume"] == 77
    assert await owner.end_native(token)
    assert fences == [token, None]
    assert not owner.write(token, lambda: pytest.fail("Ended token submitted audio"))
    assert not owner.write(None, lambda: pytest.fail("Missing token submitted audio"))
    assert not owner.owns(None)
    assert not await owner.end_native(token)
    await owner.close()


@pytest.mark.asyncio
async def test_write_rejects_async_effect_instead_of_leaving_unfenced_work():
    owner, _ = actor()
    token = await owner.admit_native("airplay2", "first", Handle())
    called = []
    async def effect():
        called.append(True)
    with pytest.raises(TypeError, match="must be synchronous"):
        owner.write(token, effect)
    await asyncio.sleep(0)
    assert not called
    await owner.close()


@pytest.mark.parametrize("kwargs", [
    {"revoke_timeout": 0}, {"revoke_timeout": -1}, {"revoke_timeout": float("inf")},
    {"revoke_timeout": float("nan")}, {"revoke_timeout": True},
    {"max_pending_revocations": 0}, {"max_pending_revocations": True},
    {"max_pending_revocations": 1.5},
])
def test_unbounded_or_invalid_retirement_limits_are_rejected(kwargs):
    with pytest.raises(ValueError, match="finite positive"):
        actor(**kwargs)


@pytest.mark.asyncio
async def test_output_barrier_blocks_all_program_writes_until_exact_ack():
    waiting, ack = asyncio.Event(), asyncio.Event()
    barriers = []
    async def barrier(previous, next_token):
        barriers.append((previous, next_token))
        waiting.set()
        await ack.wait()
    owner, fences = actor(barrier=barrier)
    admission = asyncio.create_task(owner.admit_native("airplay2", "first", Handle()))
    await waiting.wait()
    assert fences == [None]
    assert owner.snapshot()["transitioning"]
    assert not owner.snapshot()["ready"]
    pending = barriers[-1][1]
    assert not owner.write(pending, lambda: pytest.fail("Unacknowledged new route wrote PCM"))
    ack.set()
    assert await admission == pending
    assert owner.write(pending, lambda: None)
    assert fences == [None, pending]
    await owner.close()


@pytest.mark.asyncio
async def test_same_owner_seek_flush_requires_barrier_without_new_epoch_or_transport_close():
    barriers = []
    async def barrier(previous, next_token):
        barriers.append((previous, next_token))
    owner, fences = actor(barrier=barrier)
    native = Handle()
    token = await owner.admit_native("airplay2", "first", native)
    assert await owner.volume_native(token, 73)
    assert await owner.flush_native(token)
    assert barriers == [(None, token), (token, token)]
    assert fences == [None, token, None, token]
    assert owner.snapshot()["owner"] == token.model_dump()
    assert owner.snapshot()["epoch"] == token.epoch
    assert owner.snapshot()["volume"] == 73
    assert not native.closed_tokens
    assert await owner.end_native(token)
    assert barriers[-1] == (token, None)
    assert not await owner.flush_native(token)
    await owner.close()


@pytest.mark.asyncio
async def test_output_barrier_timeout_never_grants_or_reopens_a_later_owner():
    release = asyncio.Event()
    invocations = []
    async def barrier(previous, next_token):
        invocations.append((previous, next_token))
        if previous is None:
            return
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue
    owner, fences = actor(barrier=barrier, barrier_timeout=0.005)
    native = Handle()
    old = await owner.admit_native("airplay2", "old", native)
    with pytest.raises(TimeoutError, match="did not acknowledge"):
        await owner.admit_native("chromecast", "new", Handle())
    assert owner.snapshot()["owner"] is None
    assert not owner.snapshot()["ready"]
    assert fences == [None, old, None]
    assert not owner.write(old, lambda: pytest.fail("Timed-out route continued writing"))
    with pytest.raises(Conflict, match="unavailable"):
        await owner.admit_native("airplay2", "after-timeout", Handle())
    release.set()
    await settle()
    assert len(invocations) == 2
    assert owner.snapshot()["owner"] is None
    assert native.closed_tokens == [old]
    await owner.close()


@pytest.mark.asyncio
async def test_cancelled_takeover_fails_closed_and_cancels_the_outstanding_barrier():
    waiting = asyncio.Event()
    cancelled = asyncio.Event()
    async def barrier(previous, _next_token):
        if previous is None:
            return
        waiting.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    owner, _ = actor(barrier=barrier)
    native = Handle()
    old = await owner.admit_native("airplay2", "old", native)
    takeover = asyncio.create_task(owner.admit_native("chromecast", "new", Handle()))
    await waiting.wait()
    takeover.cancel()
    with pytest.raises(asyncio.CancelledError):
        await takeover
    await cancelled.wait()
    await settle()
    assert not owner.owns(old)
    assert not owner.snapshot()["ready"]
    assert native.closed_tokens == [old]
    await owner.close()


@pytest.mark.parametrize("bad", [None, object(), type("Incomplete", (), {"quiesce": lambda: None})()])
@pytest.mark.asyncio
async def test_missing_native_hooks_reject_before_clearing_the_program(bad):
    owner, fences = actor()
    with pytest.raises(ValueError, match="exact quiesce and volume"):
        await owner.admit_native("airplay2", "bad", bad)
    assert not fences
    await owner.close()


@pytest.mark.asyncio
async def test_idle_speech_has_an_atomic_separate_write_fence():
    waiting, ack = asyncio.Event(), asyncio.Event()
    async def barrier(_previous, _next):
        waiting.set()
        await ack.wait()
    owner, _ = actor(barrier=barrier)
    submitted=[]
    assert owner.write_idle(lambda: submitted.append("idle speech"))
    admission=asyncio.create_task(owner.admit_native("airplay2","music",Handle()))
    await waiting.wait()
    assert not owner.write_idle(lambda: submitted.append("idle during transition"))
    ack.set()
    token=await admission
    assert not owner.write_idle(lambda: submitted.append("idle after music grant"))
    assert owner.write(token,lambda: submitted.append("music plus speech"))
    assert await owner.end_native(token)
    assert owner.write_idle(lambda: submitted.append("idle after music end"))
    await owner.close()
    assert not owner.write_idle(lambda: submitted.append("idle after shutdown"))
    assert submitted==["idle speech","music plus speech","idle after music end"]
