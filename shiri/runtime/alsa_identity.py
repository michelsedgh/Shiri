"""Stable ALSA intent and exact current-kernel playback instance pins.

Serial bindings use raw USB sysfs descriptors, never udev ID_SERIAL or a card
label. Port bindings are an explicit physical-port contract; moving a serial-less
device requires rebinding. Named ALSA CARD IDs are only current launch selectors.
The opened-PCM backend guard must independently validate the manifest against
the actual ALSA stream and underlying character FDs before emitting audio.

Kernel interfaces: Documentation/ABI/{stable,testing}/sysfs-bus-usb and
sound/core/{init,pcm}.c; ALSA card IDs are mutable and substreams are enumerated
in /proc/asound/cardN/pcmDp/subS/info. No playback PCM is opened here.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from uuid import UUID

MAX_CARDS = 256
MAX_ENDPOINTS = 2048
MAX_SCAN = 16384
MAX_ATTRIBUTE = 8192
INT64_MAX = (1 << 63) - 1
DEFAULT_BOOT_ID = Path("/proc/sys/kernel/random/boot_id")


class PCMIdentityError(ValueError):
    """An identity is malformed, ambiguous, missing or no longer the same instance."""


class _InventoryLimit(PCMIdentityError):
    pass


def _text(value, maximum=256, *, raw=False):
    return (isinstance(value, str) and 1 <= len(value) <= maximum
            and (raw or value == value.strip())
            and not any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value))


def _stable_path(value, prefix="/devices/"):
    return (_text(value, 1024) and value.startswith(prefix)
            and str(PurePosixPath(value)) == value and ".." not in PurePosixPath(value).parts
            and "\\" not in value and not any(re.fullmatch(r"(?:card|usb)\d+", part) for part in PurePosixPath(value).parts))


def validate_fingerprint(value: dict) -> dict:
    """Strict JSON-only canonical identity; no names, aliases or fallback hints."""
    if not isinstance(value, dict):
        raise PCMIdentityError("ALSA physical identity must be an object")
    try:
        encoded = json.dumps(value, allow_nan=False)
        if len(encoded) > MAX_ATTRIBUTE:
            raise PCMIdentityError("ALSA physical identity exceeds its bounded size")
        result = json.loads(encoded)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise PCMIdentityError("ALSA physical identity must contain bounded JSON values") from exc
    common = {"version", "kind", "binding", "device", "subdevice"}
    if (type(result.get("version")) is not int or result["version"] != 1
            or any(type(result.get(key)) is not int or not 0 <= result[key] <= 255 for key in ["device", "subdevice"])):
        raise PCMIdentityError("ALSA identity version/device/subdevice is invalid")
    kind, binding = result.get("kind"), result.get("binding")
    if kind == "usb":
        common |= {"vid", "pid", "interface", "configuration"}
        if (any(not isinstance(result.get(key), str) or not re.fullmatch(r"[0-9a-f]{4}", result[key]) for key in ["vid", "pid"])
                or not isinstance(result.get("interface"), str) or not re.fullmatch(r"[0-9a-f]{2}", result["interface"])
                or type(result.get("configuration")) is not int or not 1 <= result["configuration"] <= 255):
            raise PCMIdentityError("USB identity requires canonical raw VID/PID/interface/configuration")
        if binding == "serial":
            common |= {"serial"}
            if not _text(result.get("serial"), raw=True):
                raise PCMIdentityError("USB serial must be a nonempty exact raw descriptor")
        elif binding == "port":
            common |= {"controller", "port", "root_hub"}
            if (not _stable_path(result.get("controller")) or not isinstance(result.get("port"), str)
                    or not re.fullmatch(r"[1-9][0-9]{0,2}(?:\.[1-9][0-9]{0,2}){0,15}", result["port"])
                    or any(int(part) > 255 for part in result["port"].split("."))
                    or not isinstance(result.get("root_hub"), str) or not re.fullmatch(r"[0-9a-f]{4}", result["root_hub"])):
                raise PCMIdentityError("USB port binding requires stable controller, devpath and root-hub USB version")
        else:
            raise PCMIdentityError("USB identity must explicitly bind by serial or physical port")
    elif kind in {"pci", "platform"} and binding == "path":
        common |= {"device_path", "driver"}
        if (not _stable_path(result.get("device_path")) or not _stable_path(result.get("driver"), f"/bus/{kind}/drivers/")
                or len(PurePosixPath(result["driver"]).parts) != 5):
            raise PCMIdentityError("Platform/PCI identity requires an explicit stable device and driver path")
    elif kind == "virtual" and binding == "loopback":
        common |= {"instance"}
        if (type(result.get("instance")) is not int or not 0 <= result["instance"] <= 255
                or result["device"] > 1 or result["subdevice"] > 7):
            raise PCMIdentityError("Virtual identity is constrained to an explicit snd_aloop instance and loopback pair")
    else:
        raise PCMIdentityError("Unsupported ALSA identity kind/binding")
    if set(result) != common:
        raise PCMIdentityError("ALSA identity contains missing or unrecognized fields")
    return result


def _read(path, *, optional=False):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if optional:
            return None
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise PCMIdentityError("Kernel identity attribute is not a regular sysfs/proc file")
        raw = os.read(fd, MAX_ATTRIBUTE + 1)
        if len(raw) > MAX_ATTRIBUTE:
            raise PCMIdentityError("Kernel identity attribute exceeds its bounded size")
        text = raw.decode("utf-8", errors="strict")
        return text[:-1] if text.endswith("\n") else text
    finally:
        os.close(fd)


def _names(path, pattern, maximum):
    found = []
    with os.scandir(path) as entries:
        for count, entry in enumerate(entries):
            if count >= MAX_SCAN:
                raise _InventoryLimit("Kernel audio inventory exceeds its bounded directory scan")
            if re.fullmatch(pattern, entry.name):
                found.append(entry.name)
                if len(found) > maximum:
                    raise _InventoryLimit("Kernel audio inventory exceeds its bounded endpoint capacity")
    return sorted(found)


def _relative(path, root):
    resolved = path.resolve(strict=True)
    relative = resolved.relative_to(root)
    return "/" + relative.as_posix()


def _boot_id(path):
    value = _read(path)
    if not _text(value, 36) or str(UUID(value)) != value:
        raise PCMIdentityError("Linux boot identity is unavailable or noncanonical")
    return value


def _node_snapshot(path, expected_rdev):
    observed = path.lstat()
    if not stat.S_ISCHR(observed.st_mode) or observed.st_rdev != expected_rdev:
        raise PCMIdentityError("Playback node is not the exact kernel-advertised character device")
    if (type(observed.st_dev) is not int or not 0 < observed.st_dev <= INT64_MAX
            or any(type(value) is not int or not 0 < value <= INT64_MAX for value in [observed.st_ino, observed.st_rdev])):
        raise PCMIdentityError("Playback node identity is outside manifest bounds")
    return {"node_path": str(path), "node_st_dev": observed.st_dev, "node_st_ino": observed.st_ino, "node_st_rdev": observed.st_rdev}


def _driver(device, root, bus):
    value = _relative(device / "driver", root)
    if not _stable_path(value, f"/bus/{bus}/drivers/") or len(PurePosixPath(value).parts) != 5:
        raise PCMIdentityError("Kernel audio driver is not a canonical bus driver")
    return value


def _physical(card, root):
    ancestors = [parent for parent in [card, *card.parents] if parent.is_relative_to(root)]
    # USB interfaces may be separated from the ALSA card by sound/ directories.
    interface = next((parent for parent in ancestors if (parent / "bInterfaceNumber").exists()), None)
    if interface is not None:
        device = next((parent for parent in ancestors[ancestors.index(interface) + 1:]
                       if (parent / "idVendor").exists() and (parent / "idProduct").exists()), None)
        if device is None:
            raise PCMIdentityError("USB audio interface lacks its physical device descriptors")
        vid, pid = _read(device / "idVendor"), _read(device / "idProduct")
        number = _read(interface / "bInterfaceNumber")
        configuration = _read(device / "bConfigurationValue")
        if not re.fullmatch(r"[1-9][0-9]{0,2}", configuration):
            raise PCMIdentityError("USB configuration descriptor is invalid")
        base = {"version": 1, "kind": "usb", "vid": vid, "pid": pid, "interface": number, "configuration": int(configuration)}
        serial = _read(device / "serial", optional=True)
        if serial == "":
            serial = None
        if serial is not None and not _text(serial, raw=True):
            raise PCMIdentityError("Raw USB serial descriptor is invalid")
        root_hub = next((parent for parent in ancestors[ancestors.index(device) + 1:]
                         if re.fullmatch(r"usb[0-9]+", parent.name)), None)
        if root_hub is None:
            raise PCMIdentityError("USB device lacks an identifiable root hub")
        controller = _relative(root_hub.parent, root)
        port_base = {**base, "binding": "port", "controller": controller,
                     "port": _read(device / "devpath"), "root_hub": _read(root_hub / "bcdUSB")}
        label = _read(device / "product", optional=True)
        return base, serial, port_base, str(device), label
    # Recognize real snd_aloop platform ownership, never the ALSA card's label.
    for parent in ancestors:
        match = re.fullmatch(r"snd_aloop\.(0|[1-9][0-9]{0,2})", parent.name)
        if match and _relative(parent / "subsystem", root) == "/bus/platform" and _driver(parent, root, "platform") == "/bus/platform/drivers/snd_aloop":
            module = _relative(parent / "driver/module", root)
            if module != "/module/snd_aloop":
                raise PCMIdentityError("Loopback platform driver is not the snd_aloop kernel module")
            return {"version": 1, "kind": "virtual", "binding": "loopback", "instance": int(match[1])}, None, None, str(parent), "Virtual ALSA Loopback"
    for parent in ancestors:
        if not (parent / "subsystem").is_symlink():
            continue
        bus = _relative(parent / "subsystem", root)
        if bus not in {"/bus/pci", "/bus/platform"}:
            continue
        kind = bus.rsplit("/", 1)[1]
        return {"version": 1, "kind": kind, "binding": "path", "device_path": _relative(parent, root),
                "driver": _driver(parent, root, kind)}, None, None, str(parent), None
    raise PCMIdentityError("No supported stable physical audio identity is exposed by sysfs")


def _serial_instances(root):
    """Count physical USB descriptors, including devices without an audio card.

    A malformed/unbound second audio card must not make a duplicated factory
    serial appear unique. Missing bus inventory disables automatic serial mode.
    """
    devices = root / "bus/usb/devices"
    if not devices.is_dir():
        return {}
    result = {}
    for name in _names(devices, r"(?:usb\d+|\d+-\d+(?:\.\d+)*)", MAX_SCAN):
        try:
            device = (devices / name).resolve(strict=True)
            if not device.is_relative_to(root / "devices"):
                continue
            vid, pid, serial = [_read(device / attr, optional=True) for attr in ["idVendor", "idProduct", "serial"]]
            if (not isinstance(vid, str) or not re.fullmatch(r"[0-9a-f]{4}", vid)
                    or not isinstance(pid, str) or not re.fullmatch(r"[0-9a-f]{4}", pid) or not _text(serial, raw=True)):
                continue
            result.setdefault((vid, pid, serial), set()).add(str(device))
        except (OSError, ValueError, UnicodeError):
            continue
    return result


@dataclass(frozen=True)
class PlaybackCandidate:
    label: str
    card_id: str
    card_index: int
    device: int
    subdevice: int
    fingerprint: dict | None
    port_fingerprint: dict | None
    can_bind_by: list[str]
    snapshot: dict
    # Root-internal provenance for deduplicating one USB device's multiple PCMs.
    _physical_path: str
    _serial_key: tuple | None


def inventory(*, sys_root=Path("/sys"), dev_root=Path("/dev"), proc_root=Path("/proc/asound"), boot_id_path=DEFAULT_BOOT_ID):
    """Inventory playback endpoints; malformed/disconnected cards are unavailable.

    Nondefault roots are dependency seams for filesystem tests, not evidence of
    genuine hardware or permission to bypass the backend's SYSFS_MAGIC guard.
    """
    root = Path(sys_root).resolve()
    dev = Path(dev_root).resolve()
    proc = Path(proc_root).resolve()
    sound = root / "class/sound"
    if not sound.exists():
        return []
    boot = _boot_id(Path(boot_id_path))
    candidates = []
    for card_name in _names(sound, r"card(?:0|[1-9][0-9]{0,2})", MAX_CARDS):
        try:
            index = int(card_name[4:])
            if index >= MAX_CARDS:
                continue
            card = (sound / card_name).resolve(strict=True)
            if not card.is_relative_to(root / "devices") or card.name != card_name:
                continue
            fd = os.open(card, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                card_stat = os.fstat(fd)
                if (not stat.S_ISDIR(card_stat.st_mode) or not 0 < card_stat.st_dev <= INT64_MAX
                        or not 0 < card_stat.st_ino <= INT64_MAX):
                    raise PCMIdentityError("Kernel card directory identity is outside manifest bounds")
                card_id = _read(card / "id")
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", card_id) or _read(card / "number") != str(index):
                    continue
                physical, serial, port, physical_path, label = _physical(card, root)
                card_candidates = []
                for pcm_name in _names(card, rf"pcmC{index}D(?:0|[1-9][0-9]{{0,2}})p", 256):
                    device = int(re.fullmatch(r"pcmC\d+D(\d+)p", pcm_name)[1])
                    if device > 255:
                        continue
                    pcm = card / pcm_name
                    if pcm.is_symlink() or not pcm.is_dir() or (sound / pcm_name).resolve(strict=True) != pcm:
                        raise PCMIdentityError("Playback sysfs node is not owned by the selected card")
                    advertised = _read(pcm / "dev")
                    if not re.fullmatch(r"(?:0|[1-9][0-9]{0,9}):(?:0|[1-9][0-9]{0,9})", advertised):
                        raise PCMIdentityError("Kernel playback device number is invalid")
                    major, minor = map(int, advertised.split(":"))
                    expected_rdev = os.makedev(major, minor)
                    if (not 0 < expected_rdev <= INT64_MAX or os.major(expected_rdev) != major
                            or os.minor(expected_rdev) != minor):
                        raise PCMIdentityError("Kernel playback device number cannot be represented exactly")
                    node = _node_snapshot(dev / "snd" / pcm_name, expected_rdev)
                    stream = proc / card_name / f"pcm{device}p"
                    for sub_name in _names(stream, r"sub(?:0|[1-9][0-9]{0,2})", 256):
                        subdevice = int(sub_name[3:])
                        if subdevice > 255:
                            continue
                        info = _read(stream / sub_name / "info")
                        fields = dict(re.findall(r"^([a-z_]+): ([^\n]*)$", info, flags=re.MULTILINE))
                        if any(fields.get(key) != value for key, value in {"card": str(index), "device": str(device), "subdevice": str(subdevice), "stream": "PLAYBACK"}.items()):
                            raise PCMIdentityError("Kernel PCM subdevice does not match the selected playback endpoint")
                        base = {**physical, "device": device, "subdevice": subdevice}
                        fingerprint = validate_fingerprint({**base, "binding": "serial", "serial": serial}) if physical["kind"] == "usb" and serial is not None else validate_fingerprint(base) if physical["kind"] != "usb" else None
                        port_fingerprint = validate_fingerprint({**port, "device": device, "subdevice": subdevice}) if port else None
                        snapshot = {"version": 1, "boot_id": boot, "card_index": index, "device": device, "subdevice": subdevice,
                                    "sysfs_path": str(card), "sysfs_st_dev": card_stat.st_dev, "sysfs_st_ino": card_stat.st_ino, **node}
                        display = label if _text(label, 128) else card_id
                        serial_key = (physical["vid"], physical["pid"], serial) if physical["kind"] == "usb" and serial is not None else None
                        if len(card_candidates) + len(candidates) >= MAX_ENDPOINTS:
                            raise _InventoryLimit("Kernel audio inventory exceeds its bounded endpoint capacity")
                        card_candidates.append(PlaybackCandidate(f"{display} · {card_id} · PCM {device}/{subdevice}", card_id, index, device, subdevice,
                                                                 fingerprint, port_fingerprint, [], snapshot, physical_path, serial_key))
                current = (sound / card_name).resolve(strict=True).stat()
                if current.st_dev != card_stat.st_dev or current.st_ino != card_stat.st_ino or card.stat().st_ino != card_stat.st_ino:
                    continue
                candidates.extend(card_candidates)
            finally:
                os.close(fd)
        except _InventoryLimit:
            raise
        except (OSError, ValueError, OverflowError, UnicodeError):
            # Admission cannot substitute a guessed name for an unstable card.
            continue
    if _boot_id(Path(boot_id_path)) != boot:
        raise PCMIdentityError("Linux boot identity changed during audio inventory")
    serial_devices = _serial_instances(root)
    identities = Counter(json.dumps(value, sort_keys=True) for c in candidates for value in [c.fingerprint, c.port_fingerprint] if value)
    result = []
    for candidate in candidates:
        fingerprint = candidate.fingerprint
        if fingerprint and (candidate._serial_key and len(serial_devices.get(candidate._serial_key, set())) != 1
                            or identities[json.dumps(fingerprint, sort_keys=True)] != 1):
            fingerprint = None
        port = candidate.port_fingerprint
        if port and identities[json.dumps(port, sort_keys=True)] != 1:
            port = None
        modes = [value["binding"] for value in [fingerprint, port] if value]
        if not modes:
            continue
        result.append(PlaybackCandidate(candidate.label, candidate.card_id, candidate.card_index, candidate.device, candidate.subdevice,
                                        fingerprint, port, modes, candidate.snapshot, candidate._physical_path, candidate._serial_key))
    return result


class PinnedPCM:
    """An exact current instance, held until its owning service unit stops."""
    def __init__(self, candidate, fingerprint, *, conversion, roots):
        self.fingerprint = validate_fingerprint(fingerprint)
        self.candidate = candidate
        self.manifest = dict(candidate.snapshot)
        self.playback_node = candidate.snapshot["node_path"]
        self.pcm = f"{'plughw' if conversion else 'hw'}:CARD={candidate.card_id},DEV={candidate.device},SUBDEV={candidate.subdevice}"
        self._roots = roots
        self.fd = os.open(candidate.snapshot["sysfs_path"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            self.validate()
        except BaseException:
            self.close()
            raise

    def validate(self):
        if self.fd is None:
            raise PCMIdentityError("ALSA instance pin is closed")
        held = os.fstat(self.fd)
        if (held.st_dev != self.manifest["sysfs_st_dev"] or held.st_ino != self.manifest["sysfs_st_ino"]
                or not stat.S_ISDIR(held.st_mode)):
            raise PCMIdentityError("Held sysfs directory no longer matches the selected card instance")
        matches = [c for c in inventory(**self._roots) if self.fingerprint in [c.fingerprint, c.port_fingerprint]]
        if len(matches) != 1 or matches[0].snapshot != self.manifest or matches[0].card_id != self.candidate.card_id:
            raise PCMIdentityError("The selected ALSA device disappeared, changed or became ambiguous; playback must stop")
        return self

    def close(self):
        if self.fd is not None:
            fd, self.fd = self.fd, None
            os.close(fd)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def resolve(fingerprint, conversion=True, **roots):
    """Resolve stable intent uniquely; never fall back to another name or port."""
    wanted = validate_fingerprint(fingerprint)
    if type(conversion) is not bool:
        raise PCMIdentityError("ALSA conversion preference must be a boolean")
    matches = [c for c in inventory(**roots) if wanted in [c.fingerprint, c.port_fingerprint]]
    if len(matches) != 1:
        raise PCMIdentityError("Configured ALSA physical device is missing or ambiguous; reconnect it or explicitly rebind")
    return PinnedPCM(matches[0], wanted, conversion=conversion, roots=roots)
