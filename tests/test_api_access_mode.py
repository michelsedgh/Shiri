"""Intentional LAN access leaves real audio and the browser origin boundary intact."""
import httpx
import pytest

from shiri.api import create_app
from shiri.runtime_port import SocketRuntime
from shiri.settings import Settings


@pytest.mark.parametrize("value, enabled", [(None, False), ("0", False), ("", False),
                                          ("true", False), ("yes", False), ("1", True)])
def test_unauthenticated_access_requires_explicit_environment_opt_in(monkeypatch, value, enabled):
    if value is None:
        monkeypatch.delenv("SHIRI_ALLOW_UNAUTHENTICATED", raising=False)
    else:
        monkeypatch.setenv("SHIRI_ALLOW_UNAUTHENTICATED", value)
    monkeypatch.delenv("SHIRI_SIMULATION", raising=False)
    settings = Settings.from_env()
    assert settings.allow_unauthenticated is enabled
    assert settings.simulation is False


@pytest.mark.parametrize("value", ["1", 1, None])
def test_unauthenticated_access_rejects_ambiguous_programmatic_settings(value):
    with pytest.raises(ValueError, match="explicitly enabled"):
        Settings(allow_unauthenticated=value)


@pytest.mark.parametrize("allow_unauthenticated", [False, True])
async def test_real_api_authentication_is_required_by_default(tmp_path, monkeypatch, allow_unauthenticated):
    token = "local-test-admin-token-12345678901234567890"
    settings = Settings(state_dir=tmp_path, api_token_file=tmp_path / "missing-token",
                        allow_unauthenticated=allow_unauthenticated)
    calls = []

    async def rpc(socket, operation, payload=None):
        calls.append((socket, operation))
        if operation == "interfaces":
            return {"interfaces": ["eth0"]}
        assert operation == "health"
        return {"ready": True, "simulation": False, "rooms": []}

    monkeypatch.setattr("shiri.runtime_port.call_rpc", rpc)
    # Opt-in access must also work without reading or replacing installation credentials.
    app = create_app(settings, token=None if allow_unauthenticated else token)
    try:
        assert isinstance(app.state.service.runtime, SocketRuntime)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shiri.test") as client:
            response = await client.get("/api/v1/state")
            assert response.status_code == (200 if allow_unauthenticated else 401)
            if not allow_unauthenticated:
                assert calls == []
                response = await client.get("/api/v1/state", headers={"Authorization": "Bearer " + token})
                assert response.status_code == 200
            assert response.json()["runtime"]["simulation"] is False
            ready = await client.get("/api/v1/health/ready")
            assert ready.json() == {"ready": True, "simulation": False}
        assert calls and all(socket == settings.runtime_socket for socket, _ in calls)
        assert not settings.api_token_file.exists()
    finally:
        app.state.service.store.close()


async def test_unauthenticated_real_api_keeps_same_origin_mutation_guard(tmp_path, monkeypatch):
    async def rpc(socket, operation, payload=None):
        assert operation in {"health", "reconcile"}
        return {"ready": True, "simulation": False, "rooms": []}

    monkeypatch.setattr("shiri.runtime_port.call_rpc", rpc)
    app = create_app(Settings(state_dir=tmp_path, allow_unauthenticated=True,
                              api_token_file=tmp_path / "missing-token"))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shiri.test") as client:
            body = {"name": "Living room", "interface": "eth0"}
            cross_origin = await client.post("/api/v1/rooms", json=body, headers={"Origin": "https://evil.test"})
            assert cross_origin.status_code == 403
            assert app.state.service.store.list_rooms() == []
            response = await client.post("/api/v1/rooms", json=body, headers={"Origin": "http://shiri.test"})
            assert response.status_code == 201
            room = response.json()["room"]
            patch = {"expected_revision": room["revision"], "changes": {"volume": 23}}
            endpoint = f"/api/v1/rooms/{room['id']}"
            cross_origin = await client.patch(endpoint, json=patch, headers={"Origin": "https://evil.test"})
            assert cross_origin.status_code == 403
            assert app.state.service.store.get_room(room["id"]).volume == room["volume"]
            response = await client.patch(endpoint, json=patch, headers={"Origin": "http://shiri.test"})
            assert response.status_code == 200
            assert response.json()["room"]["volume"] == 23
            assert response.json()["runtime"]["simulation"] is False
    finally:
        app.state.service.store.close()
