"""Balance controls keep master volume and saved physical identity separate."""

import httpx

from shiri.api import create_app
from shiri.runtime_port import SimulatedRuntime
from shiri.settings import Settings


async def test_balance_http_contract_does_not_change_master_and_survives_reassignment(tmp_path):
    app = create_app(Settings(state_dir=tmp_path), runtime=SimulatedRuntime(), token="balance-contract-test-repeated-for-token-length")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shiri.test",
                                     headers={"Authorization": "Bearer balance-contract-test-repeated-for-token-length"}) as client:
            room = (await client.post("/api/v1/rooms", json={"name": "Balance room", "interface": "sim0"})).json()["room"]
            root = f"/api/v1/rooms/{room['id']}"
            room = (await client.patch(root, json={"expected_revision": room["revision"], "changes": {"enabled": True}})).json()["room"]
            room = (await client.put(root + "/speakers", json={"expected_revision": room["revision"], "speaker_ids": ["101"]})).json()["room"]
            initial_master = room["volume"]
            saved_revision = room["revision"]
            result = await client.patch(root + "/speakers/101/balance", json={"expected_revision": saved_revision, "balance_percent": 55})
            assert result.status_code == 200, result.text
            room = result.json()["room"]
            assert room["volume"] == initial_master
            assert room["speakers"][0]["balance_percent"] == 55
            stale = await client.patch(root + "/speakers/101/balance", json={"expected_revision": saved_revision, "balance_percent": 10})
            assert stale.status_code == 409
            room = (await client.put(root + "/speakers", json={"expected_revision": room["revision"], "speaker_ids": []})).json()["room"]
            room = (await client.put(root + "/speakers", json={"expected_revision": room["revision"], "speaker_ids": ["101"]})).json()["room"]
            assert room["speakers"][0]["balance_percent"] == 55
            assert room["volume"] == initial_master
            for invalid in [-1, 101, 1.5, True, "55"]:
                result = await client.patch(root + "/speakers/101/balance", json={"expected_revision": room["revision"], "balance_percent": invalid})
                assert result.status_code == 422, result.text
            observed = (await client.get("/api/v1/state")).json()["rooms"][0]
            assert observed["volume"] == initial_master
            assert observed["speakers"][0]["balance_percent"] == 55
            output = next(item for item in observed["outputs"] if item["id"] == "101")
            assert output["balance_percent"] == 55
            assert output["volume"] == initial_master * 55 // 100
    finally:
        app.state.service.store.close()
