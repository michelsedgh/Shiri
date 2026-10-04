"""Independent control-boundary regressions; no hardware or live audio is touched."""

import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from shiri.domain import Room
from shiri.rpc import RpcError
from shiri.runtime.broker import Broker, RuntimeRoom
from shiri.runtime.configuration import backend_configs
from shiri.settings import Settings


@pytest.mark.parametrize("slot", [0, 7])
def test_unauthenticated_auxiliary_owntone_listeners_are_disabled(tmp_path, slot):
    # OwnTone29.3 MPD checks library.password, not general.admin_password:
    # https://github.com/owntone/owntone-server/blob/29.3/src/mpd.c
    room = Room(id=str(uuid4()), slot=slot, name="Review room", airplay_name="Review input", interface="eth0")
    _receiver, config = backend_configs(
        room, tmp_path, {"interface": "srreview"},
        all_receiver_names=[room.airplay_name],
        password="review-private-password", audio_uid=1234,
    )
    text = config.read_text()
    mpd = re.search(r"(?m)^mpd\s*\{([^}]*)\}", text)
    assert mpd, "Explicitly disable unused MPD rather than relying on backend defaults"
    assert re.search(r"\bport\s*=\s*0\b", mpd[1]), "MPD exposes control without the API admin token"
    assert re.search(r"\bhttp_port\s*=\s*0\b", mpd[1]), "The unused MPD artwork server must also be disabled"
    assert re.search(r"\bwebsocket_port\s*=\s*0\b", text), "The unauthenticated separate notify listener is unused"


def runtime_room(directory, slot):
    definition = Room(id=str(uuid4()), slot=slot, name=f"Room {slot}", airplay_name=f"Room {slot} input",
                      interface="eth0", enabled=True)
    return RuntimeRoom(definition, directory / definition.id, status="running",
                       client=SimpleNamespace(), selected_ids=["101"], processes={"audio": SimpleNamespace()})


async def test_late_failed_offer_cannot_remove_a_reused_session_owner(tmp_path, monkeypatch):
    broker = Broker(Settings(runtime_dir=tmp_path / "run", runtime_state_dir=tmp_path / "runtime"))
    a, b = runtime_room(tmp_path, 0), runtime_room(tmp_path, 1)
    broker.rooms = {a.desired.id: a, b.desired.id: b}
    entered, release = asyncio.Event(), asyncio.Event()

    async def audio_rpc(_socket, _operation, payload, **_kwargs):
        if payload.get("request_id") == "old-offer":
            entered.set()
            await release.wait()
            raise RpcError("runtime_unavailable", "Old negotiation ended after close")
        return {"ok": True, "session_id": payload["session_id"]}

    monkeypatch.setattr("shiri.runtime.broker.call_rpc", audio_rpc)
    old = asyncio.create_task(broker.speech(a, {"session_id": "reused-session", "request_id": "old-offer", "action": "offer"}))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await broker.speech(a, {"session_id": "reused-session", "request_id": "close-old", "action": "close"})
        await broker.speech(a, {"session_id": "reused-session", "request_id": "new-offer", "action": "offer"})
        release.set()
        with pytest.raises(RpcError):
            await asyncio.wait_for(old, 1)
        assert broker.sessions.get("reused-session") == a.desired.id, "Old failure discarded the new session's routing reservation"
        with pytest.raises(RpcError) as conflict:
            await broker.speech(b, {"session_id": "reused-session", "request_id": "other-room", "action": "offer"})
        assert conflict.value.code == "session_conflict"
    finally:
        release.set()
        await asyncio.gather(old, return_exceptions=True)


async def test_late_close_cannot_release_the_replacement_offer(tmp_path, monkeypatch):
    broker = Broker(Settings(runtime_dir=tmp_path / "run", runtime_state_dir=tmp_path / "runtime"))
    room = runtime_room(tmp_path, 0)
    broker.rooms = {room.desired.id: room}
    entered, release = asyncio.Event(), asyncio.Event()

    async def audio_rpc(_socket, _operation, payload, **_kwargs):
        if payload.get("action") == "close":
            # The worker releases its old ownership before awaiting peer close.
            entered.set()
            await release.wait()
        return {"ok": True, "session_id": payload["session_id"]}

    monkeypatch.setattr("shiri.runtime.broker.call_rpc", audio_rpc)
    await broker.speech(room, {"session_id": "reused-session", "request_id": "old-offer", "action": "offer"})
    close = asyncio.create_task(broker.speech(room, {"session_id": "reused-session", "request_id": "close-old", "action": "close"}))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await broker.speech(room, {"session_id": "reused-session", "request_id": "new-offer", "action": "offer"})
        release.set()
        await asyncio.wait_for(close, 1)
        assert broker.sessions.get("reused-session") == room.desired.id, "Old close discarded a later offer's reservation"
    finally:
        release.set()
        await asyncio.gather(close, return_exceptions=True)


async def test_old_negotiation_finish_cannot_unprotect_a_pending_replacement(tmp_path, monkeypatch):
    broker = Broker(Settings(runtime_dir=tmp_path / "run", runtime_state_dir=tmp_path / "runtime"))
    room = runtime_room(tmp_path, 0)
    broker.rooms = {room.desired.id: room}
    old_entered, old_release, new_entered, new_release = (asyncio.Event() for _ in range(4))

    async def audio_rpc(_socket, _operation, payload, **_kwargs):
        if payload.get("request_id") == "old-offer":
            old_entered.set()
            await old_release.wait()
            raise RpcError("runtime_unavailable", "Old negotiation ended after close")
        if payload.get("request_id") == "new-offer":
            new_entered.set()
            await new_release.wait()
        return {"ok": True, "session_id": payload["session_id"]}

    monkeypatch.setattr("shiri.runtime.broker.call_rpc", audio_rpc)
    old = asyncio.create_task(broker.speech(room, {"session_id": "reused-session", "request_id": "old-offer", "action": "offer"}))
    new = None
    try:
        await asyncio.wait_for(old_entered.wait(), 1)
        await broker.speech(room, {"session_id": "reused-session", "request_id": "close-old", "action": "close"})
        new = asyncio.create_task(broker.speech(room, {"session_id": "reused-session", "request_id": "new-offer", "action": "offer"}))
        await asyncio.wait_for(new_entered.wait(), 1)
        old_release.set()
        with pytest.raises(RpcError):
            await asyncio.wait_for(old, 1)
        assert "reused-session" in broker.pending_sessions, "Health cleanup must still protect the replacement negotiation"
        assert broker.sessions.get("reused-session") == room.desired.id
        new_release.set()
        await asyncio.wait_for(new, 1)
        assert "reused-session" not in broker.pending_sessions
    finally:
        old_release.set()
        new_release.set()
        await asyncio.gather(*(task for task in (old, new) if task), return_exceptions=True)


@pytest.mark.parametrize("reuse_existing_id", [False, True])
async def test_delayed_idle_health_cannot_forget_a_later_offer(tmp_path, monkeypatch, reuse_existing_id):
    broker = Broker(Settings(runtime_dir=tmp_path / "run", runtime_state_dir=tmp_path / "runtime"))
    room = runtime_room(tmp_path, 0)
    room.receiver = {"namespace": "review"}
    room.processes = {"audio": SimpleNamespace(alive=True)}
    room.client = SimpleNamespace(request=AsyncMock(return_value={"state": "stop"}))
    broker.network = SimpleNamespace(healthy=AsyncMock(return_value=True))
    broker.rooms = {room.desired.id: room}
    captured, release = asyncio.Event(), asyncio.Event()

    async def audio_rpc(_socket, operation, payload, **_kwargs):
        if operation == "health":
            captured.set()
            await release.wait()
            return {"ready": True, "speech_session_id": None}
        return {"ok": True, "session_id": payload["session_id"]}

    monkeypatch.setattr("shiri.runtime.broker.call_rpc", audio_rpc)
    if reuse_existing_id:
        await broker.speech(room, {"session_id": "new-owner", "request_id": "old-offer", "action": "offer"})
    probe = asyncio.create_task(broker._probe_room(room))
    try:
        await asyncio.wait_for(captured.wait(), 1)
        await broker.speech(room, {"session_id": "new-owner", "request_id": "new-offer", "action": "offer"})
        release.set()
        await asyncio.wait_for(probe, 1)
        assert broker.sessions.get("new-owner") == room.desired.id, "The idle observation predates the current offer"
    finally:
        release.set()
        await asyncio.gather(probe, return_exceptions=True)
