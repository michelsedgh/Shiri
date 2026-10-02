"""Device selection must survive reboot without following a reused card name."""
from copy import deepcopy
import json
import os
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from shiri.api import create_app
from shiri.domain import RoomCreate, RoomPatch, local_audio_device_key
from shiri.rpc import RpcError
from shiri.runtime import local_devices as devices
from shiri.runtime.alsa_identity import validate_fingerprint
from shiri.runtime.system import RuntimeFailure
from shiri.runtime_port import SimulatedRuntime
from shiri.settings import Settings

FINGERPRINT = {"version": 1, "kind": "virtual", "binding": "loopback", "instance": 0,
               "device": 1, "subdevice": 7}
TOKEN = "test-device-admin-" * 4


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr(devices, "ROOT_UID", os.getuid())
    # Portable tests exercise registry files with the test user's credentials;
    # Linux root-directory and real-kernel hardware pins have separate checks.
    monkeypatch.setattr(devices, "root_directory", lambda path: path.mkdir(mode=0o700, exist_ok=True))
    candidate = SimpleNamespace(label="Loopback validation output", card_id="Loopback",
                                fingerprint=deepcopy(FINGERPRINT), port_fingerprint=None,
                                can_bind_by=["loopback"], snapshot={"boot_id": str(uuid4()), "node_st_ino": 10})
    observations, pins = [candidate], []

    class Pin:
        def __init__(self, fingerprint, conversion):
            self.fingerprint, self.conversion = deepcopy(fingerprint), conversion
            self.manifest, self.closed = deepcopy(candidate.snapshot), False
            self.pcm = "plughw:CARD=Reordered,DEV=1,SUBDEV=7"
            self.playback_node = "/dev/snd/pcmC5D1p"
            self.failure = False

        def validate(self):
            if self.failure:
                raise RuntimeFailure("The pinned sound card reconnected")

        def close(self):
            self.closed = True

    def resolve(fingerprint, *, conversion):
        pin = Pin(fingerprint, conversion)
        pins.append(pin)
        return pin

    store = devices.LocalDevices(tmp_path / "local-devices.json", str(uuid4()),
                                  inventory_fn=lambda: observations, resolve_fn=resolve,
                                  validate_fn=validate_fingerprint)
    return store, candidate, observations, pins


def select(registry):
    return registry[0].inventory()["devices"][0]["selection_id"]


def test_enrollment_is_durable_idempotent_and_hides_private_kernel_identifiers(registry):
    store, candidate, _, pins = registry
    inventory = store.inventory()
    assert "fingerprint" not in inventory["devices"][0]
    assert "snapshot" not in inventory["devices"][0]
    first = store.bind(select(registry), "loopback")
    assert store.bind(select(registry), "loopback") == first
    assert all(pin.closed for pin in pins)
    assert store.path.stat().st_mode & 0o777 == 0o600
    restarted = devices.LocalDevices(store.path, store.installation_id,
                                      inventory_fn=store._inventory, resolve_fn=store._resolve,
                                      validate_fn=store._validate)
    candidate.card_id = "Reordered"
    candidate.snapshot["node_st_ino"] += 1
    pin = restarted.resolve(first["device"])
    assert pin.fingerprint == FINGERPRINT and pin.pcm.startswith("plughw:")
    assert not pin.closed
    pin.close()
    assert restarted.inventory()["devices"][0]["bindings"][0]["device"] == first["device"]


def test_stale_inventory_and_mid_enrollment_reconnect_never_publish_binding(registry):
    store, candidate, _, pins = registry
    old = select(registry)
    candidate.snapshot["node_st_ino"] += 1
    with pytest.raises(RuntimeFailure, match="changed"):
        store.bind(old, "loopback")
    assert not store.path.exists() and not pins
    original = store._resolve

    def replace_instance(*args, **kwargs):
        pin = original(*args, **kwargs)
        pin.manifest["node_st_ino"] += 1
        return pin
    store._resolve = replace_instance
    with pytest.raises(RuntimeFailure, match="reconnected"):
        store.bind(select(registry), "loopback")
    assert not store.path.exists() and pins[-1].closed


def test_missing_binding_and_missing_device_never_fall_back_to_card_label(registry):
    store, _, _, pins = registry
    with pytest.raises(RuntimeFailure, match="missing"):
        store.resolve("shiri:device=" + str(uuid4()))
    enrolled = store.bind(select(registry), "loopback")

    def absent(*args, **kwargs):
        raise RuntimeFailure("The verified device is absent")
    store._resolve = absent
    with pytest.raises(RuntimeFailure, match="absent"):
        store.resolve(enrolled["device"])
    assert all(pin.closed for pin in pins)


def test_conversion_changes_cannot_mutate_a_running_binding(registry):
    store, _, _, pins = registry
    enrolled = store.bind(select(registry), "loopback", conversion=True)
    before = store.path.read_bytes()
    with pytest.raises(RuntimeFailure, match="conversion"):
        store.bind(select(registry), "loopback", conversion=False)
    assert store.path.read_bytes() == before and pins[-1].closed
    assert store.record(enrolled["device"])["conversion"] is True


@pytest.mark.parametrize("damage", ["symlink", "hardlink", "writable", "other-installation", "duplicate-key", "fingerprint", "nested-json"])
def test_unsafe_or_corrupt_registry_stays_unmodified(registry, tmp_path, damage):
    store, _, _, _ = registry
    store.bind(select(registry), "loopback")
    if damage == "symlink":
        original = tmp_path / "original"
        store.path.rename(original)
        store.path.symlink_to(original)
    elif damage == "hardlink":
        os.link(store.path, tmp_path / "alias")
    elif damage == "writable":
        store.path.chmod(0o622)
    elif damage == "duplicate-key":
        store.path.write_text('{"version":1,"version":1}')
    elif damage == "nested-json":
        store.path.write_text('[' * 1500 + '0' + ']' * 1500)
    else:
        state = json.loads(store.path.read_text())
        if damage == "other-installation":
            state["installation_id"] = str(uuid4())
        else:
            next(iter(state["bindings"].values()))["fingerprint"]["instance"] = True
        store.path.write_text(json.dumps(state))
    before = store.path.read_bytes()
    with pytest.raises(RuntimeFailure):
        store.inventory()
    assert store.path.read_bytes() == before


def test_disk_failure_closes_pin_and_does_not_publish_an_unrecorded_binding(registry, monkeypatch):
    store, _, _, pins = registry
    def disk_full(*args, **kwargs):
        raise OSError("No space left on device")
    monkeypatch.setattr(devices, "atomic_json", disk_full)
    with pytest.raises(OSError):
        store.bind(select(registry), "loopback")
    assert pins[-1].closed and not store.path.exists()


@pytest.mark.parametrize("value", ["shiri:device=not-a-uuid", "shiri:device=" + "A" * 32,
                                   "shiri:device=" + uuid4().hex, "shiri:device=/etc/shadow"])
def test_opaque_binding_has_exact_canonical_identifier(value):
    for model in [RoomCreate, RoomPatch]:
        with pytest.raises(ValidationError):
            model.model_validate({"local_audio_device": value, **({"name": "Room", "interface": "eth0"} if model is RoomCreate else {})})
    valid = "shiri:device=" + str(uuid4())
    assert local_audio_device_key(valid) == valid


async def test_device_enrollment_http_auth_origin_and_exact_request_validation(tmp_path):
    identifier = "shiri:device=" + str(uuid4())
    selection = "a" * 64
    calls = []
    class Runtime(SimulatedRuntime):
        async def call(self, operation, payload=None):
            if operation == "local_devices":
                return {"devices": [{"selection_id": selection, "label": "Verified USB speaker",
                                     "can_bind_by": ["serial"], "bindings": [{"binding": "serial", "device": None}]}]}
            if operation == "bind_local_device":
                calls.append(deepcopy(payload))
                return {"device": identifier, "label": "Verified USB speaker", "binding": "serial"}
            return await super().call(operation, payload)
    app = create_app(Settings(state_dir=tmp_path), runtime=Runtime(), token=TOKEN)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shiri.test") as client:
            assert (await client.get('/api/v1/local-devices')).status_code == 401
            client.headers['Authorization'] = 'Bearer ' + TOKEN
            assert (await client.get('/api/v1/local-devices')).json()['devices'][0]['label'] == 'Verified USB speaker'
            body = {'selection_id': selection, 'binding': 'serial', 'conversion': True}
            assert (await client.post('/api/v1/local-devices/bind', json=body, headers={'Origin': 'https://other.test'})).status_code == 403
            assert (await client.post('/api/v1/local-devices/bind', json={**body, 'path': '/etc/shadow'})).status_code == 422
            assert (await client.post('/api/v1/local-devices/bind', json={**body, 'conversion': 1})).status_code == 422
            response = await client.post('/api/v1/local-devices/bind', json=body)
            assert response.status_code == 201 and response.json()['device'] == identifier
            assert calls == [body]
    finally:
        app.state.service.store.close()


async def test_malformed_hardware_inventory_is_an_explicit_backend_failure(tmp_path):
    class Runtime(SimulatedRuntime):
        async def call(self, operation, payload=None):
            if operation == 'local_devices':
                return {'devices': [{'selection_id': 'a' * 64, 'label': 'Speaker', 'can_bind_by': [{}], 'bindings': [{}]}]}
            raise RpcError('unsupported', 'Unused')
    app = create_app(Settings(state_dir=tmp_path), runtime=Runtime(), token=TOKEN)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://shiri.test',
                                    headers={'Authorization': 'Bearer ' + TOKEN}) as client:
            response = await client.get('/api/v1/local-devices')
            assert response.status_code == 503 and response.json()['code'] == 'invalid_response'
    finally:
        app.state.service.store.close()
