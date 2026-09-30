"""Control boundary checks with a real SQLite store and ASGI HTTP requests."""
import asyncio
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from shiri.api import MAX_BODY, create_app
from shiri.auth import Auth
from shiri.rpc import RpcError
from shiri.domain import RoomPatch
from shiri.runtime_port import SimulatedRuntime
from shiri.runtime.broker import Broker, RuntimeRoom
from shiri.settings import Settings

TOKEN = "test-admin-token-" * 4


@pytest.fixture
async def client(tmp_path):
    app = create_app(Settings(state_dir=tmp_path), runtime=SimulatedRuntime(), token=TOKEN)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shiri.test",
                                 headers={"Authorization": "Bearer " + TOKEN}) as client:
        yield client
    app.state.service.store.close()


async def room(client, **changes):
    response = await client.post("/api/v1/rooms", json={"name": "Living room", "interface": "sim0", **changes})
    assert response.status_code == 201, response.text
    return response.json()["room"]


async def enabled(client, r):
    result = await client.patch(f"/api/v1/rooms/{r['id']}", json={"expected_revision": r["revision"], "changes": {"enabled": True}})
    assert result.status_code == 200, result.text
    return result.json()["room"]


async def test_auth_origin_session_and_logout(client):
    client.headers.pop("Authorization")
    assert (await client.get("/api/v1/state")).status_code == 401
    assert (await client.get("/api/v1/health/live")).status_code == 200
    assert (await client.post("/api/v1/session", json={"token": TOKEN}, headers={"Origin": "https://evil.test"})).status_code == 403
    result = await client.post("/api/v1/session", json={"token": TOKEN})
    assert result.status_code == 200
    assert "HttpOnly" in result.headers["set-cookie"] and "SameSite=strict" in result.headers["set-cookie"]
    assert (await client.get("/api/v1/state")).status_code == 200
    assert (await client.delete("/api/v1/session")).status_code == 200
    assert (await client.get("/api/v1/state")).status_code == 401


async def test_failed_login_is_rate_limited(client):
    client.headers.pop("Authorization")
    for _ in range(10):
        assert (await client.post("/api/v1/session", json={"token": "incorrect"})).status_code == 401
    assert (await client.post("/api/v1/session", json={"token": TOKEN})).status_code == 429


@pytest.mark.parametrize("cookie", ["1.a.é", "bad", "9999999999999999999999999." + "a" * 32 + "." + "b" * 64])
async def test_malformed_cookie_does_not_crash_auth(client, cookie):
    client.headers.pop("Authorization")
    # httpx headers must be ASCII; Auth also rejects a non-ASCII signature directly.
    assert Auth(TOKEN).session_valid(cookie) is False


async def test_revisions_ownership_and_saved_intent(client):
    a = await enabled(client, await room(client))
    b = await enabled(client, await room(client, name="Kitchen"))
    endpoint = f"/api/v1/rooms/{a['id']}/speakers"
    response = await client.put(endpoint, json={"expected_revision": a["revision"], "speaker_ids": ["101"]})
    assert response.status_code == 200
    a = response.json()["room"]
    assert response.json()["runtime_accepted"] is True
    response = await client.put(f"/api/v1/rooms/{b['id']}/speakers", json={"expected_revision": b["revision"], "speaker_ids": ["101"]})
    assert response.status_code == 409
    response = await client.patch(f"/api/v1/rooms/{a['id']}", json={"expected_revision": a["revision"] - 1, "changes": {"volume": 5}})
    assert response.status_code == 409
    state = (await client.get("/api/v1/state")).json()
    assert state["runtime"]["simulation"] is True
    observed = next(r for r in state["rooms"] if r["id"] == a["id"])
    assert next(s for s in observed["outputs"] if s["id"] == "101")["assigned_room_id"] == a["id"]
    assert next(s for s in observed["outputs"] if s["id"] == "101")["selected"] is True


async def test_offsets_survive_deselect_reselect_through_http(client):
    r = await enabled(client, await room(client))
    endpoint = f"/api/v1/rooms/{r['id']}/speakers"
    r = (await client.put(endpoint, json={"expected_revision": r["revision"], "speaker_ids": ["101"]})).json()["room"]
    r = (await client.patch(endpoint + "/101/offset", json={"expected_revision": r["revision"], "offset_ms": 137})).json()["room"]
    r = (await client.put(endpoint, json={"expected_revision": r["revision"], "speaker_ids": []})).json()["room"]
    r = (await client.put(endpoint, json={"expected_revision": r["revision"], "speaker_ids": ["101"]})).json()["room"]
    assert r["speakers"][0]["offset_ms"] == 137


async def test_invalid_inputs_are_rejected_without_creating_room(client):
    for data in [{"name": "Bad", "interface": "lo"}, {"name": "Bad", "interface": "sim0", "volume": 80},
                 {"name": "Bad", "interface": "sim0", "nobly_room_id": " room-id "}]:
        result = await client.post("/api/v1/rooms", json=data)
        assert result.status_code == 422
        assert result.json()["code"] == "invalid_request"
    assert (await client.get("/api/v1/state")).json()["rooms"] == []


async def test_nobly_exact_binding_never_falls_back(client):
    await room(client, nobly_room_id="UPPER/room")
    body = {"session_id": "speech-1", "request_id": "request-1", "sdp": "offer", "action": "offer"}
    missing = await client.post("/api/v1/nobly/rooms/upper/room/speech", json=body)
    assert missing.status_code == 404
    disabled = await client.post("/api/v1/nobly/rooms/UPPER/room/speech", json=body)
    assert disabled.status_code == 409


@pytest.mark.parametrize("action", ["control", "close"])
async def test_nobly_followups_require_the_stable_admitted_room_route(client, action):
    body = {"session_id": "speech-1", "request_id": "followup-1", "action": action}
    response = await client.post("/api/v1/nobly/rooms/rebound/speech", json=body)
    assert response.status_code == 400
    assert "admitted_room_id" in response.json()["error"]
    assert "/api/v1/rooms/" in response.json()["error"]


@pytest.fixture
async def admitted(client, monkeypatch, tmp_path):
    service = client._transport.app.state.service
    old = await enabled(client, await room(client, nobly_room_id="exact/binding"))
    new = await enabled(client, await room(client, name="Other room"))
    for definition, output in ((old, "101"), (new, "202")):
        response = await client.put(f"/api/v1/rooms/{definition['id']}/speakers",
                                    json={"expected_revision": definition["revision"], "speaker_ids": [output]})
        assert response.status_code == 200
    old, new = service.store.get_room(old["id"]), service.store.get_room(new["id"])
    broker = Broker(Settings(state_dir=tmp_path / "review", runtime_state_dir=tmp_path / "runtime",
                             runtime_dir=tmp_path / "run"))
    for definition in (old, new):
        broker.rooms[definition.id] = RuntimeRoom(
            definition, tmp_path / definition.id, status="running", client=object(),
            processes={"audio": object()}, selected_ids=[speaker.id for speaker in definition.speakers],
        )
    review = SimpleNamespace(service=service, broker=broker, old=old, new=new, calls=[],
                             entered=asyncio.Event(), release=asyncio.Event(), pause=False, lose_next=False)
    async def worker(socket, operation, message, **_):
        review.calls.append((socket.parent.name, message["action"], message["session_id"]))
        if review.pause:
            review.entered.set()
            await review.release.wait()
        return {"ok": True, "type": "answer", "sdp": "answer"}
    monkeypatch.setattr("shiri.runtime.broker.call_rpc", worker)
    original = service.runtime.call
    async def runtime(operation, payload=None):
        if operation == "speech":
            result = await broker.speech(broker.rooms[payload["room_id"]], payload)
            if review.lose_next:
                review.lose_next = False
                raise RpcError("runtime_unavailable", "Admission acknowledgment lost after broker acceptance")
            return result
        return await original(operation, payload)
    monkeypatch.setattr(service.runtime, "call", runtime)
    yield review


def offer(session="connection-A"):
    return {"session_id": session, "request_id": "offer-" + session, "action": "offer", "sdp": "offer"}


async def rebind(client, review):
    old = review.service.store.get_room(review.old.id)
    response = await client.patch(f"/api/v1/rooms/{old.id}",
                                  json={"expected_revision": old.revision, "changes": {"nobly_room_id": None}})
    assert response.status_code == 200
    new = review.service.store.get_room(review.new.id)
    response = await client.patch(f"/api/v1/rooms/{new.id}",
                                  json={"expected_revision": new.revision, "changes": {"nobly_room_id": "exact/binding"}})
    assert response.status_code == 200


async def test_nobly_binding_edit_waits_for_inflight_admission_ack(client, admitted):
    admitted.pause = True
    admission = asyncio.create_task(client.post("/api/v1/nobly/rooms/exact/binding/speech", json=offer()))
    await asyncio.wait_for(admitted.entered.wait(), 1)
    edit = asyncio.create_task(client.patch(f"/api/v1/rooms/{admitted.old.id}", json={
        "expected_revision": admitted.old.revision, "changes": {"nobly_room_id": None},
    }))
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(edit), 0.1)
        assert admitted.service.store.get_room(admitted.old.id).nobly_room_id == "exact/binding"
        admitted.release.set()
        response = await asyncio.wait_for(admission, 1)
        assert response.status_code == 200 and response.json()["admitted_room_id"] == admitted.old.id
        assert (await asyncio.wait_for(edit, 1)).status_code == 200
        assert admitted.calls == [(admitted.old.id, "offer", "connection-A")]
    finally:
        admitted.release.set()
        await asyncio.wait_for(asyncio.gather(admission, edit, return_exceptions=True), 1)


async def test_rebound_nobly_close_cannot_touch_successor_and_stable_uuid_closes_original(client, admitted):
    response = await client.post("/api/v1/nobly/rooms/exact/binding/speech", json=offer())
    assert response.json()["admitted_room_id"] == admitted.old.id
    await rebind(client, admitted)
    successor = await client.post("/api/v1/nobly/rooms/exact/binding/speech", json=offer("connection-B"))
    assert successor.json()["admitted_room_id"] == admitted.new.id
    close = {"session_id": "connection-A", "request_id": "close-A", "action": "close"}
    rejected = await client.post("/api/v1/nobly/rooms/exact/binding/speech", json=close)
    assert rejected.status_code == 400
    assert admitted.broker.sessions == {"connection-A": admitted.old.id, "connection-B": admitted.new.id}
    wrong_room = await client.post(f"/api/v1/rooms/{admitted.new.id}/speech", json=close)
    assert wrong_room.status_code == 409 and wrong_room.json()["code"] == "session_conflict"
    original = await client.post(f"/api/v1/rooms/{admitted.old.id}/speech", json=close)
    assert original.status_code == 200 and original.json()["admitted_room_id"] == admitted.old.id
    assert admitted.broker.sessions == {"connection-B": admitted.new.id}
    assert admitted.calls == [(admitted.old.id, "offer", "connection-A"),
                              (admitted.new.id, "offer", "connection-B"),
                              (admitted.old.id, "close", "connection-A")]


async def test_lost_nobly_admission_ack_retry_cannot_retarget_retained_session(client, admitted):
    admitted.lose_next = True
    lost = await client.post("/api/v1/nobly/rooms/exact/binding/speech", json=offer())
    assert lost.status_code == 503 and admitted.broker.sessions == {"connection-A": admitted.old.id}
    await rebind(client, admitted)
    retry = await client.post("/api/v1/nobly/rooms/exact/binding/speech", json=offer())
    assert retry.status_code == 409 and retry.json()["code"] == "session_conflict"
    assert admitted.calls == [(admitted.old.id, "offer", "connection-A")]
    assert admitted.broker.sessions == {"connection-A": admitted.old.id}


async def test_storage_error_cannot_be_reported_as_saved(client, monkeypatch):
    service = client._transport.app.state.service
    def fail(_):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(service.store, "create_room", fail)
    response = await client.post("/api/v1/rooms", json={"name": "Living", "interface": "sim0"})
    assert response.status_code == 503
    assert response.json()["code"] == "storage_error"
    assert service.store.list_rooms() == []


async def test_runtime_outage_preserves_saved_pending_intent_and_allows_removal(client, monkeypatch):
    service = client._transport.app.state.service
    r = await enabled(client, await room(client))
    endpoint = f"/api/v1/rooms/{r['id']}/speakers"
    r = (await client.put(endpoint, json={"expected_revision": r["revision"], "speaker_ids": ["101"]})).json()["room"]
    async def unavailable(*_):
        raise RpcError("runtime_unavailable", "Broker offline")
    monkeypatch.setattr(service.runtime, "call", unavailable)
    result = await client.put(endpoint, json={"expected_revision": r["revision"], "speaker_ids": []})
    assert result.status_code == 200
    assert result.json()["runtime_accepted"] is False
    assert result.json()["room"]["speakers"] == []
    state = (await client.get("/api/v1/state")).json()
    assert state["runtime"]["ready"] is False
    assert state["rooms"][0]["runtime"]["status"] == "error"
    assert (await client.get("/api/v1/health/ready")).status_code == 503


async def test_unknown_backend_protocol_is_visible_unassignable(client, monkeypatch):
    service = client._transport.app.state.service
    r = await enabled(client, await room(client))
    original = service.runtime.call
    async def backend(operation, payload=None):
        if operation == "outputs":
            return {"outputs": [{"id": "777", "name": "Unknown FIFO", "protocol": None, "assignable": False}]}
        return await original(operation, payload)
    monkeypatch.setattr(service.runtime, "call", backend)
    state = (await client.get("/api/v1/state")).json()
    assert state["rooms"][0]["outputs"][0]["assignable"] is False
    response = await client.put(f"/api/v1/rooms/{r['id']}/speakers", json={"expected_revision": r["revision"], "speaker_ids": ["777"]})
    assert response.status_code == 409


async def test_body_size_limit_and_uniform_offer_error(client):
    oversized = await client.post("/api/v1/rooms", content=b"x" * (MAX_BODY + 1), headers={"Content-Type": "application/json"})
    assert oversized.status_code == 413, oversized.text
    r = await room(client)
    missing = await client.post(f"/api/v1/rooms/{r['id']}/speech", json={"session_id": "s", "request_id": "r"})
    assert missing.status_code == 422 and missing.json()["code"] == "invalid_request"


async def test_inflight_state_does_not_restore_invalidated_cache(client, monkeypatch):
    service = client._transport.app.state.service
    entered, release = asyncio.Event(), asyncio.Event()
    original = service.runtime.call
    health_calls = 0
    async def backend(operation, payload=None):
        nonlocal health_calls
        if operation == "health":
            health_calls += 1
            if health_calls == 2:
                entered.set()
                await release.wait()
        return await original(operation, payload)
    monkeypatch.setattr(service.runtime, "call", backend)
    old = asyncio.create_task(service.state())
    await entered.wait()
    await room(client)
    release.set()
    assert (await old)["rooms"] == []
    assert len((await service.state())["rooms"]) == 1


async def test_phone_volume_is_durable_and_stale_ui_cannot_overwrite_it(client, monkeypatch):
    service = client._transport.app.state.service
    r = await enabled(client, await room(client))
    original = service.runtime.call
    pending = {"id": "phone-event-1", "volume": 18, "base_revision": r["revision"]}
    acknowledgements = []
    async def backend(operation, payload=None):
        nonlocal pending
        if operation == "health":
            result = await original(operation, payload)
            if pending:
                result["rooms"][0]["phone_volume_update"] = pending
            return result
        if operation == "ack_phone_volume":
            acknowledgements.append(payload)
            pending = None
            return {"ok": True}
        return await original(operation, payload)
    monkeypatch.setattr(service.runtime, "call", backend)
    result = await client.patch(f"/api/v1/rooms/{r['id']}", json={"expected_revision": r["revision"], "changes": {"volume": 50}})
    assert result.status_code == 409
    stored = service.store.get_room(r["id"])
    assert stored.volume == 18 and stored.revision == r["revision"] + 1
    assert acknowledgements[0]["accepted"] is True
    assert acknowledgements[0]["volume"] == 18
    assert acknowledgements[0]["committed_revision"] == stored.revision


async def test_late_phone_volume_cannot_replace_newer_saved_setting(client, monkeypatch):
    service = client._transport.app.state.service
    r = await enabled(client, await room(client))
    newest = service.store.update_room(r["id"], RoomPatch(volume=72), r["revision"])
    original = service.runtime.call
    acknowledgements = []
    async def backend(operation, payload=None):
        if operation == "health":
            result = await original(operation, payload)
            result["rooms"][0]["phone_volume_update"] = {"id": "late", "volume": 3, "base_revision": r["revision"]}
            return result
        if operation == "ack_phone_volume":
            acknowledgements.append(payload)
            return {"ok": True}
        return await original(operation, payload)
    monkeypatch.setattr(service.runtime, "call", backend)
    await service.sync_phone_volume()
    assert service.store.get_room(r["id"]).volume == 72
    assert service.store.get_room(r["id"]).revision == newest.revision
    assert acknowledgements[0]["accepted"] is False and acknowledgements[0]["volume"] == 72


async def test_phone_ack_replay_keeps_original_revision_after_newer_ui_intent(client, monkeypatch):
    from shiri.rpc import RpcError

    service = client._transport.app.state.service
    r = await enabled(client, await room(client))
    original = service.runtime.call
    pending = {"id": "lost-ack", "volume": 18, "base_revision": r["revision"]}
    acknowledgements = []

    async def backend(operation, payload=None):
        if operation == "health":
            result = await original(operation, payload)
            result["rooms"][0]["phone_volume_update"] = pending
            return result
        if operation == "ack_phone_volume":
            acknowledgements.append(payload)
            if len(acknowledgements) == 1:
                raise RpcError("unavailable", "Connection lost after SQLite commit")
            return {"ok": True}
        return await original(operation, payload)

    monkeypatch.setattr(service.runtime, "call", backend)
    await service.sync_phone_volume()
    first = service.store.get_room(r["id"])
    newest = service.store.update_room(r["id"], RoomPatch(volume=72), first.revision)
    await service.sync_phone_volume()
    assert service.store.get_room(r["id"]) == newest
    assert acknowledgements == [{"room_id": r["id"], "update_id": "lost-ack", "accepted": True,
                                  "volume": 18, "committed_revision": first.revision}] * 2
    # A coalesced second phone event follows that commit, so it cannot replace
    # a UI command committed after the first phone event.
    pending = {"id": "second-phone", "volume": 25, "base_revision": first.revision}
    await service.sync_phone_volume()
    assert service.store.get_room(r["id"]) == newest
    assert acknowledgements[-1]["accepted"] is False
    assert acknowledgements[-1]["volume"] == 72
