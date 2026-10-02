"""Offline mock edges/provenance checks; import never launches a daemon or bus."""
import hashlib
import importlib.util
from pathlib import Path
import stat
from types import SimpleNamespace
import xml.etree.ElementTree as ET

from dbus_next import Message, MessageType, Variant
import pytest

SOURCE = Path(__file__).parent / "linux/check_bluealsa_private_bus.py"
spec = importlib.util.spec_from_file_location("private_bluealsa_check", SOURCE)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def call(interface, member, *, path="/", body=None, signature=""):
    return Message(destination="org.bluez", path=path, interface=interface, member=member,
                   body=body or [], signature=signature, sender=":1.33", serial=1)


async def test_mock_exports_only_bluez_edges_and_never_fakes_bluealsa_pcm_capabilities():
    mock = fixture.MockBlueZ(SimpleNamespace())
    reply = mock.handle(call(fixture.OBJECTS, "GetManagedObjects"))
    assert reply.signature == "a{oa{sa{sv}}}"
    inventory = reply.body[0]
    assert set(inventory) == {fixture.ADAPTER, fixture.DEVICE, fixture.TRANSPORT}
    assert inventory[fixture.DEVICE]["org.bluez.Device1"]["Address"] == Variant("s", fixture.MAC)
    assert all("org.bluealsa.PCM1" not in interfaces for interfaces in inventory.values())
    refused = mock.handle(call("org.bluealsa.PCM1", "OpenRestricted", path="/org/bluealsa"))
    assert refused.message_type == MessageType.ERROR
    await mock.close()


async def test_application_registration_keeps_exact_daemon_owner_and_path():
    mock = fixture.MockBlueZ(SimpleNamespace())
    message = call("org.bluez.Media1", "RegisterApplication", path=fixture.ADAPTER,
                   signature="oa{sv}", body=[fixture.ADAPTER, {}])
    assert mock.handle(message).message_type == MessageType.METHOD_RETURN
    assert await mock.registered == (":1.33", fixture.ADAPTER)
    await mock.close()


async def test_bluez_owner_discovery_has_truthful_introspection_before_sender_filtered_calls():
    mock = fixture.MockBlueZ(SimpleNamespace())
    message = call("org.freedesktop.DBus.Introspectable", "Introspect")
    reply = mock.handle(message)
    assert reply.message_type == MessageType.METHOD_RETURN
    assert reply.reply_serial == message.serial and reply.signature == "s"
    interfaces = {element.attrib["name"]: element for element in ET.fromstring(reply.body[0])}
    assert set(interfaces) == {fixture.OBJECTS, "org.freedesktop.DBus.Introspectable"}
    edge = interfaces[fixture.OBJECTS].find("method")
    assert edge.attrib["name"] == "GetManagedObjects"
    assert edge.find("arg").attrib["type"] == "a{oa{sa{sv}}}"
    assert "org.bluealsa" not in reply.body[0]
    assert mock.handle(call("org.freedesktop.DBus.Introspectable", "Introspect",
                            path=fixture.DEVICE)).message_type == MessageType.ERROR
    await mock.close()


class Binary:
    def __init__(self, content=b"ELF\0STATE_DIRECTORY\0", *, owner=0, mode=stat.S_IFREG | 0o755):
        self.content, self.owner, self.mode = content, owner, mode
    def resolve(self, *, strict):
        assert strict
        return self
    def stat(self):
        return SimpleNamespace(st_mode=self.mode, st_uid=self.owner)
    def read_bytes(self):
        return self.content


@pytest.mark.parametrize("fault", ["changed-digest", "nonroot", "writable", "setuid", "no-private-storage"])
def test_actual_binary_gate_rejects_untrusted_or_global_storage_provenance(fault):
    binary = Binary()
    expected = hashlib.sha256(binary.content).hexdigest()
    if fault == "changed-digest":
        expected = "0" * 64
    elif fault == "nonroot":
        binary.owner = 1000
    elif fault == "writable":
        binary.mode |= 0o020
    elif fault == "setuid":
        binary.mode |= 0o4000
    else:
        binary.content = b"ELF\0"
        expected = hashlib.sha256(binary.content).hexdigest()
    with pytest.raises(RuntimeError):
        fixture.validated_binary(binary, expected)


def test_exact_root_binary_digest_and_private_storage_gate_are_admitted():
    binary = Binary()
    digest = hashlib.sha256(binary.content).hexdigest()
    assert fixture.validated_binary(binary, digest) == (binary, digest)


async def test_nonlinux_or_implicit_execution_never_launches_children(monkeypatch):
    monkeypatch.delenv("SHIRI_PRIVATE_BLUEALSA_TEST", raising=False)
    with pytest.raises(RuntimeError, match="Explicit"):
        await fixture.exercise(Path("/not-present"), "0" * 64)
