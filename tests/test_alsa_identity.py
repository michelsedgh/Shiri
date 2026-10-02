"""Fake kernel descriptor inventory plus real held-directory/socket-inode tests.

UNIX socket nodes supply actual removable VFS inodes through one explicit test
seam; they do not stand in for real character devices in production. No physical
USB device, PCM stream, driver, macOS setting or VM is opened or modified.
"""
from copy import deepcopy
import os
from pathlib import Path
import shutil
import socket
import stat
import tempfile
from types import SimpleNamespace

import pytest

from shiri.runtime import alsa_identity as identity

BOOT = "00000000-0000-4000-8000-000000000001"
MANIFEST_KEYS = {"version", "boot_id", "card_index", "device", "subdevice", "sysfs_path", "sysfs_st_dev", "sysfs_st_ino",
                 "node_path", "node_st_dev", "node_st_ino", "node_st_rdev"}


class FakeKernel:
    def __init__(self, base):
        self.base = Path(base)
        self.sys = self.base / "sys"
        self.dev = self.base / "dev"
        self.proc = self.base / "proc/asound"
        self.boot = self.base / "boot_id"
        self.sockets = []
        self.original_node_snapshot = identity._node_snapshot
        for directory in [self.sys / "class/sound", self.dev / "snd", self.proc]:
            directory.mkdir(parents=True)
        self.boot.write_text(BOOT + "\n")

    @property
    def roots(self):
        return {"sys_root": self.sys, "dev_root": self.dev, "proc_root": self.proc, "boot_id_path": self.boot}

    def attr(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(value) + "\n")

    def link(self, path, target):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target)

    def bus(self, parent, bus, driver):
        bus_root = self.sys / "bus" / bus
        driver_root = bus_root / "drivers" / driver
        driver_root.mkdir(parents=True, exist_ok=True)
        self.link(parent / "subsystem", bus_root)
        self.link(parent / "driver", driver_root)
        return driver_root

    def card(self, parent, index, *, name="USB", device=0, subs=1):
        card = parent / "sound" / f"card{index}"
        self.attr(card / "id", name)
        self.attr(card / "number", index)
        self.link(self.sys / "class/sound" / f"card{index}", card)
        pcm_name = f"pcmC{index}D{device}p"
        pcm = card / pcm_name
        self.attr(pcm / "dev", f"116:{index * 32 + device * 2}")
        self.link(self.sys / "class/sound" / pcm_name, pcm)
        node = self.dev / "snd" / pcm_name
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(node))
        self.sockets.append(sock)
        stream = self.proc / f"card{index}" / f"pcm{device}p"
        for sub in range(subs):
            self.attr(stream / f"sub{sub}" / "info", f"card: {index}\ndevice: {device}\nsubdevice: {sub}\nstream: PLAYBACK")
        return card

    def usb(self, index, *, serial=" RAW serial-1 ", bus=2, port="1", interface="00", device=0, subs=1,
            name="USB", root_hub="0200", controller="0000:00:14.0", vid="1234", pid="5678"):
        host = self.sys / "devices/pci0000:00" / controller
        root = host / f"usb{bus}"
        self.attr(root / "bcdUSB", root_hub)
        parent = root / f"{bus}-{port}"
        for attribute, value in {"idVendor": vid, "idProduct": pid, "bConfigurationValue": 1,
                                 "devpath": port, "product": "USB speaker"}.items():
            self.attr(parent / attribute, value)
        if serial is not None:
            self.attr(parent / "serial", serial)
        bus_link = self.sys / "bus/usb/devices" / parent.name
        if not bus_link.is_symlink():
            self.link(bus_link, parent)
        audio = parent / f"{bus}-{port}:1.{int(interface, 16)}"
        self.attr(audio / "bInterfaceNumber", interface)
        return self.card(audio, index, name=name, device=device, subs=subs)

    def pci(self, index, *, path="pci0000:00/0000:00:1f.3", driver="snd_hda_intel", bus="pci", name="PCH"):
        parent = self.sys / "devices" / path
        parent.mkdir(parents=True)
        self.bus(parent, bus, driver)
        return self.card(parent, index, name=name)

    def loopback(self, index=0, *, instance=0, device=1, subs=8, name="Loopback", module="snd_aloop"):
        parent = self.sys / "devices/platform" / f"snd_aloop.{instance}"
        parent.mkdir(parents=True)
        driver = self.bus(parent, "platform", "snd_aloop")
        module_root = self.sys / "module" / module
        module_root.mkdir(parents=True)
        self.link(driver / "module", module_root)
        return self.card(parent, index, name=name, device=device, subs=subs)

    def remove_card(self, card, *, unplug_usb=False):
        usb = next((parent for parent in card.parents if (parent / "idVendor").exists()), None)
        index = int(card.name[4:])
        for pcm in card.glob("pcm*p"):
            (self.sys / "class/sound" / pcm.name).unlink()
            (self.dev / "snd" / pcm.name).unlink()
        (self.sys / "class/sound" / card.name).unlink()
        shutil.rmtree(self.proc / card.name)
        shutil.rmtree(card)
        if unplug_usb and usb is not None:
            (self.sys / "bus/usb/devices" / usb.name).unlink()
            shutil.rmtree(usb)
        assert index >= 0

    def socket_node_snapshot(self, path, expected_rdev):
        observed = path.lstat()
        if not stat.S_ISSOCK(observed.st_mode):
            return self.original_node_snapshot(path, expected_rdev)
        return {"node_path": str(path), "node_st_dev": observed.st_dev, "node_st_ino": observed.st_ino,
                "node_st_rdev": expected_rdev}


@pytest.fixture
def kernel(monkeypatch):
    # Short names respect macOS' actual AF_UNIX pathname length restriction.
    with tempfile.TemporaryDirectory(prefix="shiri-alsa-", dir="/tmp") as base:
        fixture = FakeKernel(base)
        monkeypatch.setattr(identity, "_node_snapshot", fixture.socket_node_snapshot)
        yield fixture
        for sock in fixture.sockets:
            sock.close()


def test_usb_uses_raw_serial_interface_and_subdevice_not_udev_composite_or_card_name(kernel):
    card = kernel.usb(4, interface="02", subs=2)
    kernel.attr(card / "ID_SERIAL", "Manufacturer_Model_RAW_serial-1")
    found = identity.inventory(**kernel.roots)
    assert len(found) == 2
    assert found[1].fingerprint == {"version": 1, "kind": "usb", "binding": "serial", "vid": "1234", "pid": "5678",
                                    "serial": " RAW serial-1 ", "interface": "02", "configuration": 1, "device": 0, "subdevice": 1}
    assert found[1].can_bind_by == ["serial", "port"]
    assert found[0].fingerprint["serial"] == " RAW serial-1 "  # Descriptor whitespace is identity.
    assert set(found[0].snapshot) == MANIFEST_KEYS


def test_serial_resolves_across_card_name_number_port_and_usb_bus_reordering_without_fallback(kernel):
    card = kernel.usb(4, name="USB_1", bus=2)
    fingerprint = identity.inventory(**kernel.roots)[0].fingerprint
    kernel.remove_card(card, unplug_usb=True)
    kernel.usb(1, name="USB", bus=9, port="3.2")
    with identity.resolve(fingerprint, **kernel.roots) as pin:
        assert pin.pcm == "plughw:CARD=USB,DEV=0,SUBDEV=0"
        assert pin.manifest["card_index"] == 1
        assert pin.manifest == pin.candidate.snapshot
        fd = pin.fd
        assert os.fstat(fd).st_ino == pin.manifest["sysfs_st_ino"]
    with pytest.raises(OSError):
        os.fstat(fd)
    with pytest.raises(identity.PCMIdentityError, match="closed"):
        pin.validate()


@pytest.mark.parametrize("serial", [None, ""])
def test_absent_serial_requires_explicit_physical_port_binding(kernel, serial):
    kernel.usb(0, serial=serial)
    candidate = identity.inventory(**kernel.roots)[0]
    assert candidate.fingerprint is None and candidate.can_bind_by == ["port"]
    assert candidate.port_fingerprint["controller"] == "/devices/pci0000:00/0000:00:14.0"
    assert candidate.port_fingerprint["port"] == "1"
    assert candidate.port_fingerprint["root_hub"] == "0200"
    with identity.resolve(candidate.port_fingerprint, conversion=False, **kernel.roots) as pin:
        assert pin.pcm == "hw:CARD=USB,DEV=0,SUBDEV=0"


def test_duplicate_raw_serial_disallows_serial_but_different_ports_remain_explicit(kernel):
    kernel.usb(0, serial="DUP", port="1", name="USB")
    kernel.usb(1, serial="DUP", port="2", name="USB_1")
    found = identity.inventory(**kernel.roots)
    assert len(found) == 2 and all(c.fingerprint is None and c.can_bind_by == ["port"] for c in found)
    wanted = {**found[0].port_fingerprint, "binding": "serial", "serial": "DUP"}
    for field in ["port", "controller", "root_hub"]:
        del wanted[field]
    with pytest.raises(identity.PCMIdentityError, match="missing or ambiguous"):
        identity.resolve(wanted, **kernel.roots)
    assert found[0].port_fingerprint != found[1].port_fingerprint


def test_multiple_pcm_subdevices_on_one_usb_device_are_not_duplicate_serials(kernel):
    kernel.usb(0, subs=4)
    found = identity.inventory(**kernel.roots)
    assert len(found) == 4 and all(c.fingerprint is not None for c in found)
    assert {c.fingerprint["subdevice"] for c in found} == {0, 1, 2, 3}


def test_physical_port_ignores_boot_bus_number_but_not_controller_devpath_or_root_hub(kernel):
    card = kernel.usb(0, serial=None, bus=2, port="2.3")
    fingerprint = identity.inventory(**kernel.roots)[0].port_fingerprint
    kernel.remove_card(card)
    card = kernel.usb(3, serial=None, bus=12, port="2.3", name="USB_3")
    with identity.resolve(fingerprint, **kernel.roots) as pin:
        assert pin.manifest["card_index"] == 3
    kernel.remove_card(card)
    kernel.usb(0, serial=None, bus=15, port="2.3", root_hub="0300")
    with pytest.raises(identity.PCMIdentityError):
        identity.resolve(fingerprint, **kernel.roots)


def test_usb2_usb3_roots_of_one_controller_do_not_collapse_to_one_port(kernel):
    kernel.usb(0, serial=None, bus=2, port="1", root_hub="0200")
    kernel.usb(1, serial=None, bus=3, port="1", root_hub="0300", name="USB_1")
    found = identity.inventory(**kernel.roots)
    assert all(c.can_bind_by == ["port"] for c in found)
    assert found[0].port_fingerprint != found[1].port_fingerprint


def test_existing_pin_rejects_unplug_replug_same_serial_before_new_resolution(kernel):
    card = kernel.usb(0)
    fingerprint = identity.inventory(**kernel.roots)[0].fingerprint
    pin = identity.resolve(fingerprint, **kernel.roots)
    held = os.fstat(pin.fd)
    kernel.remove_card(card)
    kernel.usb(0)
    assert os.fstat(pin.fd).st_ino == held.st_ino  # Actual old VFS directory remains pinned.
    with pytest.raises(identity.PCMIdentityError, match="disappeared, changed"):
        pin.validate()
    pin.close()
    with identity.resolve(fingerprint, **kernel.roots) as replacement:
        assert replacement.manifest["sysfs_st_ino"] != held.st_ino


def test_same_sysfs_instance_rejects_replaced_playback_node_inode(kernel):
    kernel.usb(0)
    candidate = identity.inventory(**kernel.roots)[0]
    pin = identity.resolve(candidate.fingerprint, **kernel.roots)
    node = Path(pin.playback_node)
    retained_node = node.with_name("retained-old-socket")
    node.rename(retained_node)  # Prevent accidental inode reuse in this test.
    replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    replacement.bind(str(node))
    kernel.sockets.append(replacement)
    with pytest.raises(identity.PCMIdentityError, match="changed"):
        pin.validate()
    assert node.stat().st_ino != pin.manifest["node_st_ino"]
    pin.close()


def test_pin_rejects_changed_boot_card_selector_interface_and_duplicate_serial(kernel):
    card = kernel.usb(0)
    candidate = identity.inventory(**kernel.roots)[0]
    with identity.resolve(candidate.fingerprint, **kernel.roots) as pin:
        kernel.attr(card / "id", "USB_CHANGED")
        with pytest.raises(identity.PCMIdentityError):
            pin.validate()
        kernel.attr(card / "id", "USB")
        kernel.boot.write_text("00000000-0000-4000-8000-000000000002\n")
        with pytest.raises(identity.PCMIdentityError):
            pin.validate()
        kernel.boot.write_text(BOOT + "\n")
        kernel.usb(1, serial=" RAW serial-1 ", port="2", name="USB_1")
        with pytest.raises(identity.PCMIdentityError):
            pin.validate()


def test_pci_platform_bind_explicit_device_and_driver_path(kernel):
    kernel.pci(0)
    kernel.pci(1, path="platform/firmware-audio", driver="snd_soc_firmware", bus="platform", name="SOC")
    found = identity.inventory(**kernel.roots)
    assert found[0].fingerprint == {"version": 1, "kind": "pci", "binding": "path", "device_path": "/devices/pci0000:00/0000:00:1f.3",
                                    "driver": "/bus/pci/drivers/snd_hda_intel", "device": 0, "subdevice": 0}
    assert found[1].fingerprint["kind"] == "platform"
    assert all(c.can_bind_by == ["path"] for c in found)
    with identity.resolve(found[1].fingerprint, **kernel.roots) as pin:
        assert pin.pcm == "plughw:CARD=SOC,DEV=0,SUBDEV=0"


def test_loopback_is_constrained_to_true_driver_module_instance_pair_and_subdevice(kernel):
    kernel.loopback(0)
    found = identity.inventory(**kernel.roots)
    assert len(found) == 8
    assert found[-1].fingerprint == {"version": 1, "kind": "virtual", "binding": "loopback", "instance": 0, "device": 1, "subdevice": 7}
    with identity.resolve(found[-1].fingerprint, **kernel.roots) as pin:
        assert pin.pcm == "plughw:CARD=Loopback,DEV=1,SUBDEV=7"


def test_loopback_name_on_usb_is_not_a_virtual_hardware_identity(kernel):
    kernel.usb(0, name="Loopback")
    assert identity.inventory(**kernel.roots)[0].fingerprint["kind"] == "usb"


def test_wrong_loopback_module_cannot_be_admitted_as_virtual(kernel):
    kernel.loopback(module="snd_dummy")
    assert identity.inventory(**kernel.roots) == []


@pytest.mark.parametrize("alter", [lambda f: f.update(extra="fallback-card"), lambda f: f.update(device=True),
    lambda f: f.update(subdevice=-1), lambda f: f.update(configuration=0), lambda f: f.update(serial=""),
    lambda f: f.update(serial="bad\nserial"), lambda f: f.update(vid="ABCD"), lambda f: f.update(interface="2"),
    lambda f: f.update(version=2)])
def test_fingerprint_validation_rejects_noncanonical_unbounded_or_fallback_fields(kernel, alter):
    kernel.usb(0)
    value = deepcopy(identity.inventory(**kernel.roots)[0].fingerprint)
    alter(value)
    with pytest.raises(identity.PCMIdentityError):
        identity.validate_fingerprint(value)


def test_serial_cannot_fall_back_to_another_same_named_device(kernel):
    card = kernel.usb(0, serial="WANTED", name="USB")
    wanted = identity.inventory(**kernel.roots)[0].fingerprint
    kernel.remove_card(card)
    kernel.usb(0, serial="OTHER", name="USB")
    with pytest.raises(identity.PCMIdentityError, match="missing or ambiguous"):
        identity.resolve(wanted, **kernel.roots)


def test_production_node_check_rejects_regular_fifo_socket_and_symlink_nodes(kernel):
    for kind in ["regular", "fifo", "socket", "symlink"]:
        node = kernel.base / kind
        if kind == "regular":
            node.write_text("fake audio")
        elif kind == "fifo":
            os.mkfifo(node)
        elif kind == "socket":
            sock = socket.socket(socket.AF_UNIX)
            sock.bind(str(node))
            kernel.sockets.append(sock)
        else:
            node.symlink_to("/dev/null")
        with pytest.raises(identity.PCMIdentityError, match="character device"):
            kernel.original_node_snapshot(node, os.makedev(116, 0))


def test_inventory_rejects_wrong_substream_and_attributes_without_unsafe_guess(kernel):
    kernel.usb(0)
    info = kernel.proc / "card0/pcm0p/sub0/info"
    kernel.attr(info, "card: 1\ndevice: 0\nsubdevice: 0\nstream: PLAYBACK")
    assert identity.inventory(**kernel.roots) == []


def test_inventory_bounds_and_resolve_failure_release_owned_directory_fds(kernel, monkeypatch):
    kernel.usb(0, subs=3)
    monkeypatch.setattr(identity, "MAX_ENDPOINTS", 2)
    with pytest.raises(identity.PCMIdentityError, match="bounded endpoint"):
        identity.inventory(**kernel.roots)
    monkeypatch.setattr(identity, "MAX_ENDPOINTS", 2048)
    candidate = identity.inventory(**kernel.roots)[0]
    captured = []
    original = identity.PinnedPCM.validate
    def fail(self):
        captured.append(self.fd)
        raise identity.PCMIdentityError("Simulated last-check replacement")
    monkeypatch.setattr(identity.PinnedPCM, "validate", fail)
    with pytest.raises(identity.PCMIdentityError, match="last-check"):
        identity.resolve(candidate.fingerprint, **kernel.roots)
    for fd in captured:
        with pytest.raises(OSError):
            os.fstat(fd)
    monkeypatch.setattr(identity.PinnedPCM, "validate", original)


def test_duplicate_descriptor_without_a_working_audio_card_still_requires_port(kernel):
    kernel.usb(0, serial="DUP", port="1")
    unavailable_card = kernel.usb(1, serial="DUP", port="2", name="USB_1")
    kernel.remove_card(unavailable_card)
    found = identity.inventory(**kernel.roots)
    assert len(found) == 1 and found[0].fingerprint is None
    assert found[0].can_bind_by == ["port"]


def test_missing_global_usb_inventory_never_claims_raw_serial_is_unique(kernel):
    kernel.usb(0)
    shutil.rmtree(kernel.sys / "bus/usb/devices")
    found = identity.inventory(**kernel.roots)
    assert len(found) == 1 and found[0].fingerprint is None and found[0].can_bind_by == ["port"]


def test_ambiguous_serial_and_physical_port_are_quarantined_without_hiding_other_devices(kernel):
    kernel.usb(0, serial="DUP", bus=2, port="1", root_hub="0200")
    wanted = identity.inventory(**kernel.roots)[0].fingerprint
    # Two same-version roots on one controller cannot be distinguished by this
    # explicit port contract. The backend must not guess between them.
    kernel.usb(1, serial="DUP", bus=3, port="1", root_hub="0200", name="USB_1")
    kernel.usb(2, serial="OTHER", bus=2, port="2", name="USB_2")
    found = identity.inventory(**kernel.roots)
    assert len(found) == 1 and found[0].card_id == "USB_2"
    assert found[0].can_bind_by == ["serial", "port"]
    with pytest.raises(identity.PCMIdentityError, match="missing or ambiguous"):
        identity.resolve(wanted, **kernel.roots)


def test_unbounded_product_label_cannot_break_enrollment_of_a_valid_identity(kernel):
    card = kernel.usb(0, name="USB_0")
    device = next(parent for parent in card.parents if (parent / "idVendor").exists())
    kernel.attr(device / "product", "Long product " * 32)
    candidate = identity.inventory(**kernel.roots)[0]
    assert candidate.label == "USB_0 · USB_0 · PCM 0/0"
    assert len(candidate.label) <= 256
    with identity.resolve(candidate.fingerprint, **kernel.roots) as pin:
        assert pin.manifest == candidate.snapshot


@pytest.mark.parametrize("advertised", ["9999999999:9999999999", "0:0", "00116:0", "116:-1"])
def test_malformed_or_unrepresentable_device_numbers_quarantine_only_the_bad_card(kernel, advertised):
    card = kernel.usb(0)
    kernel.usb(1, serial="OTHER", port="2", name="USB_1")
    kernel.attr(card / "pcmC0D0p/dev", advertised)
    found = identity.inventory(**kernel.roots)
    assert len(found) == 1 and found[0].card_id == "USB_1"


def test_physical_fingerprint_json_size_is_bounded_before_schema_audit(kernel):
    kernel.usb(0)
    fingerprint = identity.inventory(**kernel.roots)[0].fingerprint
    fingerprint["serial"] = "s" * (identity.MAX_ATTRIBUTE + 1)
    with pytest.raises(identity.PCMIdentityError, match="bounded"):
        identity.validate_fingerprint(fingerprint)


@pytest.mark.parametrize("field,value", [("st_dev", 0), ("st_dev", identity.INT64_MAX + 1),
    ("st_ino", 0), ("st_ino", identity.INT64_MAX + 1), ("st_rdev", 0), ("st_rdev", identity.INT64_MAX + 1)])
def test_manifest_node_bounds_match_the_opened_pcm_guard(field, value):
    observed = SimpleNamespace(st_mode=stat.S_IFCHR, st_dev=1, st_ino=2, st_rdev=3)
    setattr(observed, field, value)
    node = SimpleNamespace(lstat=lambda: observed)
    with pytest.raises(identity.PCMIdentityError, match="manifest bounds"):
        identity._node_snapshot(node, observed.st_rdev)
