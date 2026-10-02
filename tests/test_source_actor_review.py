"""Independent rejected-effect and partial-volume ownership regressions."""
import asyncio
import socket
from uuid import uuid4

import pytest

from shiri.domain import Conflict
from shiri.runtime.source import SourceActor


class Native:
    def __init__(self, *, partial_failure=False, asynchronous=False):
        self.actual_volume = 50
        self.partial_failure = partial_failure
        self.asynchronous = asynchronous
        self.late_effects = []
        self.closed = []

    async def quiesce(self, token):
        self.closed.append(token)

    def set_volume(self, _token, volume):
        if self.asynchronous:
            async def late():
                self.late_effects.append(volume)
            return asyncio.create_task(late())
        self.actual_volume = volume
        if self.partial_failure:
            raise OSError("Volume hook failed after changing the native gain")


@pytest.mark.parametrize("idle", [False, True])
async def test_rejected_returned_task_cannot_run_its_effect_after_a_successor_grant(idle):
    owner = SourceActor(str(uuid4()), lambda _token: None)
    first = Native()
    token = None if idle else await owner.admit_native("airplay2", "first", first)
    effects = []
    returned = []
    async def late_effect():
        effects.append("stale unfenced effect")
    def wrongly_asynchronous_callback():
        work = asyncio.create_task(late_effect())
        returned.append(work)
        return work
    try:
        with pytest.raises(TypeError, match="must be synchronous"):
            if idle:
                owner.write_idle(wrongly_asynchronous_callback)
            else:
                owner.write(token, wrongly_asynchronous_callback)
        next_token = await owner.admit_native("chromecast", "successor", Native())
        await asyncio.sleep(0)
        assert returned[0].cancelled()
        assert effects == []
        assert owner.owns(next_token)
    finally:
        await owner.close()


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_volume_failure_fails_closed_and_retires_only_its_exact_native_handle(asynchronous):
    fences = []
    owner = SourceActor(str(uuid4()), fences.append)
    native = Native(partial_failure=not asynchronous, asynchronous=asynchronous)
    token = await owner.admit_native("airplay2", "native-incarnation", native)
    expected_error = TypeError if asynchronous else OSError
    try:
        with pytest.raises(expected_error):
            await owner.volume_native(token, 77)
        snapshot = owner.snapshot()
        assert snapshot["ready"] is False and snapshot["error"]
        assert snapshot["owner"] is None
        assert not owner.owns(token)
        assert not owner.write(token, lambda: pytest.fail("Unknown-gain program submitted PCM"))
        assert not owner.write_idle(lambda: pytest.fail("Failed route admitted idle/TTS output"))
        assert not await owner.volume_native(token, 1)
        with pytest.raises(Conflict, match="unavailable"):
            await owner.admit_native("chromecast", "next", Native())
        await asyncio.sleep(0)
        assert native.late_effects == []
        if not asynchronous:
            assert native.actual_volume == 77, "Fixture must expose a real partial effect"
        assert fences[-1] is None
    finally:
        await owner.close()
    assert native.closed == [token]


async def test_retirement_deadline_closes_only_the_exact_native_socket_in_cancellation_finally():
    # Real local socket pairs exercise the adapter's cleanup contract, rather
    # than treating a canceled Python task as proof that its connection closed.
    writers = []
    async def pair():
        left, right = socket.socketpair()
        for item in [left, right]:
            item.setblocking(False)
        _reader, writer = await asyncio.open_connection(sock=left)
        peer_reader, peer_writer = await asyncio.open_connection(sock=right)
        writers.extend([writer, peer_writer])
        return writer, peer_reader
    class SocketNative(Native):
        def __init__(self, writer, *, wait=False):
            super().__init__()
            self.writer = writer
            self.wait = wait
            self.finalized = asyncio.Event()
        async def quiesce(self, token):
            self.closed.append(token)
            try:
                if self.wait:
                    await asyncio.Event().wait()
            finally:
                self.writer.close()
                await self.writer.wait_closed()
                self.finalized.set()
    owner = SourceActor(str(uuid4()), lambda _token: None, revoke_timeout=.02)
    try:
        old_writer, old_peer = await pair()
        current_writer, current_peer = await pair()
        old_native = SocketNative(old_writer, wait=True)
        current_native = SocketNative(current_writer)
        old = await owner.admit_native("airplay2", "old-native-socket", old_native)
        current = await owner.admit_native("chromecast", "new-native-socket", current_native)
        await asyncio.wait_for(old_native.finalized.wait(), .5)
        assert await asyncio.wait_for(old_peer.read(), .5) == b""
        assert old_native.closed == [old] and not current_native.closed
        assert not owner.write(old, lambda: pytest.fail("Retired socket regained ownership"))
        assert owner.write(current, lambda: current_writer.write(b"new"))
        assert await asyncio.wait_for(current_peer.readexactly(3), .5) == b"new"
        assert owner.owns(current)
    finally:
        await owner.close()
        for writer in writers:
            writer.close()
        await asyncio.gather(*(writer.wait_closed() for writer in writers), return_exceptions=True)
