"""Native connection control is finite and never admits speech or fake media."""
import asyncio
import time
from uuid import uuid4

import pytest

from shiri.rpc import RpcError
from test_speech_startup import native_controller


class WarmClient:
    def __init__(self, original):
        self.original = original
        self.requests = []
        self.entered = asyncio.Event()
        self.release_entered = asyncio.Event()
        self.release_gate = None
        self.refresh_gate = None
        self.refresh_entered = asyncio.Event()
        self.connected = False
        self.terminal = False
        self.pending = True
        self.prepared = 0
        self.expires = 0
        self.corrupt = None

    def connect(self):
        self.connected = True
        self.pending = False
        self.prepared = time.monotonic_ns()

    async def request(self, method, path, *, json):
        self.requests.append((method, path, dict(json)))
        if path != "/api/player/shiri-warm":
            return await self.original.request(method, path, json=json)
        action = json["action"]
        if action == "acquire":
            self.expires = json["deadline_monotonic_ns"]
            self.entered.set()
            if self.refresh_gate is not None:
                self.refresh_entered.set()
                await self.refresh_gate.wait()
        elif action == "deadline":
            self.expires = json["deadline_monotonic_ns"]
        elif action == "release":
            self.release_entered.set()
            if self.release_gate is not None:
                await self.release_gate.wait()
            self.connected = self.pending = False
            self.terminal = True
        result = {**json, "connected": self.connected, "pending": self.pending,
                  "terminal": self.terminal, "prepared_monotonic_ns": self.prepared,
                  "expires_monotonic_ns": self.expires, "output_count": 1}
        if self.corrupt and action == "acquire":
            self.corrupt(result)
        return result


async def setup():
    native, writer, original, overlay = await native_controller()
    client = WarmClient(original)
    native.client = client
    return native, writer, client, overlay


async def test_connection_setup_has_no_speech_or_pcm_and_source_waits_only_until_connection():
    native, writer, client, overlay = await setup()
    lease = uuid4().hex
    warm = asyncio.create_task(native.warm_connection(lease, time.monotonic_ns() + 60_000_000_000))
    try:
        await asyncio.wait_for(client.entered.wait(), 1)
        old = native.mixer.route
        source = asyncio.create_task(native._transition(old))
        await asyncio.sleep(.02)
        assert not any(path == "/api/player/shiri-source" for _method, path, _body in client.requests)
        assert not writer.packets and not overlay.owner and not overlay.sent and not overlay.controls
        client.connect()
        result = await asyncio.wait_for(warm, 1)
        assert result["state"] == "connected" and result["connected"] is True
        assert result["scope"] == "connection_only" and result["acoustic_ready"] is None
        assert result["lease_id"] == lease and result["output_count"] == 1
        await asyncio.wait_for(source, 1)
        assert client.requests[-1][1] == "/api/player/shiri-source"
        assert not writer.packets and not overlay.owner and not overlay.sent and not overlay.controls
    finally:
        await native.release_warm_connection(lease)
        await native.close()


async def test_observe_disconnect_is_degraded_without_reconnect_or_music_mutation():
    native, writer, client, overlay = await setup()
    lease = uuid4().hex
    client.connect()
    try:
        await native.warm_connection(lease, time.monotonic_ns() + 60_000_000_000)
        source_before = native.actor.snapshot()
        client.connected = False
        before = len(client.requests)
        result = await native.observe_warm_connection(lease)
        assert result["state"] == "degraded" and result["connected"] is False
        assert client.requests[before:][0][2]["action"] == "observe"
        assert len(client.requests) == before + 1 and native.actor.snapshot() == source_before
        assert not writer.packets and not overlay.owner and not overlay.controls
    finally:
        await native.release_warm_connection(lease)
        await native.close()


async def test_failed_or_cancelled_refresh_preserves_existing_aggregate_and_can_shorten_bound():
    native, _writer, client, _overlay = await setup()
    lease = uuid4().hex
    client.connect()
    first = time.monotonic_ns() + 30_000_000_000
    await native.warm_connection(lease, first)
    client.refresh_gate = asyncio.Event()
    pending = asyncio.create_task(native.warm_connection(lease, first + 20_000_000_000))
    try:
        await asyncio.wait_for(client.refresh_entered.wait(), 1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not client.release_entered.is_set() and client.connected and not client.terminal
        shortened = await native.adjust_warm_deadline(lease, first)
        assert shortened["connected"] and shortened["expires_monotonic_ns"] == first
        assert client.requests[-1][2]["action"] == "deadline"
    finally:
        await native.release_warm_connection(lease)
        await native.close()


async def test_initial_cancellation_survives_repeated_cancel_until_exact_retirement():
    native, writer, client, overlay = await setup()
    lease = uuid4().hex
    client.release_gate = asyncio.Event()
    pending = asyncio.create_task(native.warm_connection(lease, time.monotonic_ns() + 60_000_000_000))
    try:
        await asyncio.wait_for(client.entered.wait(), 1)
        pending.cancel()
        await asyncio.wait_for(client.release_entered.wait(), 1)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
        client.release_gate.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        body = client.requests[-1][2]
        assert body["action"] == "release" and body["warm_id"] == lease
        assert body["lease_generation"] == 1 and client.terminal
        assert not writer.packets and not overlay.owner and not overlay.controls
    finally:
        client.release_gate.set()
        await native.close()


@pytest.mark.parametrize("bad", [True, None, -1, 0, 1.5, "60", 2**63])
async def test_invalid_deadline_does_not_allocate_identity_or_touch_backend(bad):
    native, _writer, client, _overlay = await setup()
    try:
        with pytest.raises(RpcError):
            await native.warm_connection(uuid4().hex, bad)
        assert not client.requests and native._warm_generation == 0 and native._warm_identity is None
    finally:
        await native.close()


@pytest.mark.parametrize("bad", ["0" * 32, "A" * 32, "bad", None, 123])
async def test_invalid_id_does_not_allocate_identity_or_touch_backend(bad):
    native, _writer, client, _overlay = await setup()
    try:
        with pytest.raises(RpcError):
            await native.warm_connection(bad, time.monotonic_ns() + 60_000_000_000)
        assert not client.requests and native._warm_generation == 0
    finally:
        await native.close()


@pytest.mark.parametrize("corrupt", [
    lambda reply: reply.update(warm_id="f" * 32),
    lambda reply: reply.update(launch_generation="f" * 32),
    lambda reply: reply.update(lease_generation=True),
    lambda reply: reply.update(connected=1),
    lambda reply: reply.update(output_count=True),
    lambda reply: reply.update(output_count=129),
    lambda reply: reply.update(prepared_monotonic_ns=0),
    lambda reply: reply.update(expires_monotonic_ns=0),
    lambda reply: reply.update(ready=True),
])
async def test_bad_ack_never_claims_connection_and_retires_exact_original_identity(corrupt):
    native, writer, client, overlay = await setup()
    client.connect()
    client.corrupt = corrupt
    lease = uuid4().hex
    try:
        with pytest.raises(RpcError):
            await native.warm_connection(lease, time.monotonic_ns() + 60_000_000_000)
        assert client.requests[-1][2]["action"] == "release"
        assert client.requests[-1][2]["warm_id"] == lease and client.terminal
        assert not native._warm_connected and not writer.packets and not overlay.owner
    finally:
        await native.close()


async def test_terminal_lease_and_stale_release_cannot_replace_successor():
    native, _writer, client, _overlay = await setup()
    first, second = uuid4().hex, uuid4().hex
    client.connect()
    try:
        await native.warm_connection(first, time.monotonic_ns() + 60_000_000_000)
        await native.release_warm_connection(first)
        before = len(client.requests)
        with pytest.raises(RpcError):
            await native.warm_connection(first, time.monotonic_ns() + 60_000_000_000)
        assert len(client.requests) == before
        client.terminal = False
        client.connect()
        result = await native.warm_connection(second, time.monotonic_ns() + 60_000_000_000)
        assert result["lease_generation"] == 2
        before = len(client.requests)
        with pytest.raises(RpcError):
            await native.release_warm_connection(first)
        assert len(client.requests) == before and native._warm_identity == (second, 2)
    finally:
        await native.release_warm_connection(second)
        await native.close()


async def test_release_before_first_acquire_records_exact_terminal_identity():
    native, _writer, client, _overlay = await setup()
    lease = uuid4().hex
    try:
        result = await native.release_warm_connection(lease)
        assert result["terminal"] and not result["connected"]
        assert client.requests[0][2]["action"] == "release"
        assert client.requests[0][2]["warm_id"] == lease
        before = len(client.requests)
        with pytest.raises(RpcError):
            await native.warm_connection(lease, time.monotonic_ns() + 60_000_000_000)
        assert len(client.requests) == before
    finally:
        await native.close()


async def test_music_recent_uses_committed_media_age_and_resets_on_fence():
    native, _writer, _client, _overlay = await setup()
    clock = [5_000_000_000]
    native.mixer.now_ns = lambda: clock[0]
    try:
        assert native.mixer.health()["music_media_recent"] is False
        native.mixer._last_native_ns = clock[0]
        assert native.mixer.health()["music_media_recent"] is True
        clock[0] += 1_000_000_000
        assert native.mixer.health()["music_media_recent"] is True
        clock[0] += 1
        assert native.mixer.health()["music_media_recent"] is False
        native.mixer.fence(None)
        assert native.mixer.health()["music_media_recent"] is False
    finally:
        await native.close()
