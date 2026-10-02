"""Durable hardware bindings, separate from changeable ALSA card names.

The unprivileged API selects a current inventory item. Only the broker records
its validated physical fingerprint, and each launch resolves and pins the
current kernel instance again. Neither a caller nor a saved card number can
choose a privileged filesystem path.
"""

from __future__ import annotations

from contextlib import suppress
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from uuid import UUID, uuid4

from .system import RuntimeFailure, atomic_json, root_directory

PREFIX = "shiri:device="
MAX_BINDINGS = 128
MAX_INVENTORY = 512
MAX_STATE_BYTES = 256 * 1024
ROOT_UID = 0


def binding_id(value: str) -> str:
    if not isinstance(value, str) or not value.startswith(PREFIX):
        raise RuntimeFailure("Choose a verified local speaker from the device inventory")
    raw = value.removeprefix(PREFIX)
    try:
        canonical = str(UUID(raw))
    except (ValueError, AttributeError) as exc:
        raise RuntimeFailure("The local speaker binding is invalid") from exc
    if raw != canonical:
        raise RuntimeFailure("The local speaker binding must use its exact saved identifier")
    return canonical


def _label(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 256 or value != value.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise RuntimeFailure("The local speaker label is invalid")
    return value


def _json(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise RuntimeFailure("The local device inventory contains invalid identity data") from exc


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate device registry key")
        result[key] = value
    return result


class LocalDevices:
    def __init__(self, path: Path, installation_id: str, *, inventory_fn=None,
                 resolve_fn=None, validate_fn=None):
        if inventory_fn is None or resolve_fn is None or validate_fn is None:
            from .alsa_identity import inventory, resolve, validate_fingerprint
            inventory_fn = inventory_fn or inventory
            resolve_fn = resolve_fn or resolve
            validate_fn = validate_fn or validate_fingerprint
        self.path = path
        self.installation_id = str(UUID(installation_id))
        self._inventory, self._resolve, self._validate = inventory_fn, resolve_fn, validate_fn

    def _load(self):
        root_directory(self.path.parent)
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return {"version": 1, "installation_id": self.installation_id, "bindings": {}}
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != ROOT_UID or info.st_nlink != 1
                or info.st_mode & 0o077 or info.st_size > MAX_STATE_BYTES):
            raise RuntimeFailure("Local device bindings need an intact root-only registry; preserve and inspect it")
        try:
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor) as stream:
                current = os.fstat(stream.fileno())
                if ((current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
                        or current.st_uid != ROOT_UID or current.st_nlink != 1
                        or current.st_mode & 0o077 or not stat.S_ISREG(current.st_mode)):
                    raise RuntimeFailure("Local device bindings changed during open")
                data = json.loads(stream.read(MAX_STATE_BYTES + 1), object_pairs_hook=_unique_object)
            if (not isinstance(data, dict) or set(data) != {"version", "installation_id", "bindings"}
                    or type(data["version"]) is not int or data["version"] != 1
                    or data["installation_id"] != self.installation_id
                    or not isinstance(data["bindings"], dict) or len(data["bindings"]) > MAX_BINDINGS):
                raise ValueError("Wrong device registry schema or installation")
            fingerprints = set()
            for identifier, record in data["bindings"].items():
                binding_id(PREFIX + identifier)
                if (not isinstance(record, dict) or set(record) != {"fingerprint", "label", "conversion"}
                        or type(record["conversion"]) is not bool):
                    raise ValueError("Wrong device binding schema")
                _label(record["label"])
                fingerprint = self._validate(record["fingerprint"])
                if fingerprint != record["fingerprint"]:
                    raise ValueError("Noncanonical physical fingerprint")
                key = _json(fingerprint)
                if key in fingerprints:
                    raise ValueError("Duplicate physical binding")
                fingerprints.add(key)
            return data
        except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError) as exc:
            raise RuntimeFailure("Local device bindings are invalid; preserve and inspect the registry") from exc

    def _candidates(self):
        candidates = list(self._inventory())
        if len(candidates) > MAX_INVENTORY:
            raise RuntimeFailure("Local audio inventory exceeds its device bound")
        indexed = {}
        for candidate in candidates:
            _label(candidate.label)
            bindings = list(candidate.can_bind_by)
            if (not bindings or any(not isinstance(item, str) for item in bindings)
                    or len(bindings) != len(set(bindings))
                    or any(item not in {"serial", "port", "path", "loopback"} for item in bindings)):
                raise RuntimeFailure("Local audio inventory has an invalid physical binding mode")
            description = {"snapshot": candidate.snapshot, "card_id": candidate.card_id,
                           "fingerprint": candidate.fingerprint, "port_fingerprint": candidate.port_fingerprint}
            encoded = _json(description)
            if len(encoded) > 8192:
                raise RuntimeFailure("A local device identity exceeds its size bound")
            selection = hashlib.sha256(encoded.encode()).hexdigest()
            if selection in indexed:
                raise RuntimeFailure("Local audio inventory is ambiguous")
            indexed[selection] = candidate
        return indexed

    def inventory(self):
        saved = self._load()["bindings"]
        devices = []
        for selection, candidate in self._candidates().items():
            modes = []
            for mode in candidate.can_bind_by:
                fingerprint = self._fingerprint(candidate, mode)
                identifier = next((key for key, record in saved.items()
                                   if record["fingerprint"] == fingerprint), None)
                modes.append({"binding": mode, "device": PREFIX + identifier if identifier else None})
            devices.append({"selection_id": selection, "label": candidate.label,
                            "can_bind_by": list(candidate.can_bind_by), "bindings": modes})
        return {"devices": devices}

    def _fingerprint(self, candidate, binding):
        if binding not in candidate.can_bind_by:
            raise RuntimeFailure("This device cannot use that binding; refresh the speaker inventory")
        fingerprint = candidate.port_fingerprint if binding == "port" else candidate.fingerprint
        if not isinstance(fingerprint, dict) or fingerprint.get("binding") != binding:
            raise RuntimeFailure("The selected device has no unique physical identity for that binding")
        return self._validate(fingerprint)

    def bind(self, selection_id: str, binding: str, *, conversion: bool = True):
        if (not isinstance(selection_id, str) or not re.fullmatch(r"[0-9a-f]{64}", selection_id)
                or type(conversion) is not bool):
            raise RuntimeFailure("Select a current local speaker and its supported binding mode")
        candidate = self._candidates().get(selection_id)
        if candidate is None:
            raise RuntimeFailure("The local audio device changed; refresh the speaker inventory before selecting it")
        fingerprint = self._fingerprint(candidate, binding)
        pin = self._resolve(fingerprint, conversion=conversion)
        try:
            pin.validate()
            if pin.manifest != candidate.snapshot:
                raise RuntimeFailure("The local audio device reconnected during selection; refresh its inventory")
            state = self._load()
            for identifier, saved in state["bindings"].items():
                if saved["fingerprint"] == fingerprint:
                    if saved["conversion"] != conversion:
                        raise RuntimeFailure("This speaker already has a saved PCM conversion setting; use its existing binding")
                    return {"device": PREFIX + identifier, "label": saved["label"], "binding": binding}
            if len(state["bindings"]) >= MAX_BINDINGS:
                raise RuntimeFailure("Local device registry is full; inspect obsolete bindings before adding another")
            identifier = str(uuid4())
            state["bindings"][identifier] = {"fingerprint": fingerprint, "label": candidate.label,
                                           "conversion": conversion}
            if len(_json(state).encode()) > MAX_STATE_BYTES:
                raise RuntimeFailure("Local device registry exceeds its size bound")
            pin.validate()
            atomic_json(self.path, state)
            return {"device": PREFIX + identifier, "label": candidate.label, "binding": binding}
        finally:
            pin.close()

    def resolve(self, device: str):
        record = self._load()["bindings"].get(binding_id(device))
        if record is None:
            raise RuntimeFailure("The saved local speaker binding is missing; choose and verify the speaker again")
        pin = self._resolve(record["fingerprint"], conversion=record["conversion"])
        try:
            pin.validate()
        except BaseException:
            with suppress(Exception):
                pin.close()
            raise
        return pin

    def record(self, device: str):
        from copy import deepcopy
        record = self._load()["bindings"].get(binding_id(device))
        if record is None:
            raise RuntimeFailure("The local speaker binding is not present in this installation")
        return deepcopy(record)
