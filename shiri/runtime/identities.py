"""Read-only validation of installer-provisioned, immutable daemon identities."""

from __future__ import annotations

import grp
import json
import os
from pathlib import Path
import pwd
import stat
from uuid import UUID

from .system import RuntimeFailure

IDENTITY_VERSION = 2
PURPOSE = "Shiri managed daemon v1"


def expected_accounts(version=IDENTITY_VERSION):
    names = {"sender.discovery": "shiri-discovery", "sender.timing": "shiri-timing"}
    for slot in range(8):
        for role in ["receiver", "output", "audio", "discovery", "timing"]:
            names[f"slot{slot}.{role}"] = f"shiri-{role}-{slot}"
    if version == 2:
        names.update({f"slot{slot}.bridge": f"shiri-bridge-{slot}" for slot in range(8)})
    return names


class DaemonIdentities:
    def __init__(self, path: Path):
        self.path, self.accounts = path, {}
        self.installation_id = None
        self.runtime_state_dir = None
        self.runtime_dir = None

    def load(self):
        try:
            if not self.path.is_absolute() or ".." in self.path.parts:
                raise RuntimeFailure("Daemon UID map path is not canonical")
            for ancestor in reversed([self.path.parent, *self.path.parent.parents]):
                parent = ancestor.lstat()
                if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0
                        or parent.st_mode & 0o022):
                    raise RuntimeFailure("Daemon UID map has an unsafe ancestor")
            info = self.path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0
                    or info.st_mode & 0o077 or info.st_size > 65536):
                raise RuntimeFailure("Daemon UID map must be an unlinked root-only regular file")
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor) as stream:
                current = os.fstat(stream.fileno())
                if ((current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
                        or current.st_uid != 0 or current.st_mode & 0o077
                        or current.st_nlink != 1 or not stat.S_ISREG(current.st_mode)):
                    raise RuntimeFailure("Daemon UID map changed while opening")
                data = json.load(stream)
            if (not isinstance(data, dict) or type(data.get("version")) is not int
                    or data["version"] != IDENTITY_VERSION
                    or set(data) != {"version", "installation_id", "runtime_state_dir", "runtime_dir", "accounts"}
                    or not isinstance(data.get("accounts"), dict)
                    or set(data.get("accounts", {})) != set(expected_accounts())):
                raise RuntimeFailure("Daemon UID map needs the stopped, quiescent v2 service-installer migration")
            self.installation_id = str(UUID(data["installation_id"]))
            self.runtime_state_dir = Path(data["runtime_state_dir"])
            self.runtime_dir = Path(data["runtime_dir"])
            if not self.runtime_state_dir.is_absolute() or ".." in self.runtime_state_dir.parts:
                raise RuntimeFailure("Daemon UID map has an invalid owning state directory")
            if not self.runtime_dir.is_absolute() or ".." in self.runtime_dir.parts:
                raise RuntimeFailure("Daemon UID map has an invalid owning broker lock directory")
            self.accounts = data["accounts"]
            self.validate()
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise RuntimeFailure("Daemon identities are unavailable; run the reviewed service installer") from exc
        return self

    def validate(self):
        seen_uids, seen_gids = set(), set()
        api = pwd.getpwnam("shiri")
        if api.pw_uid == 0 or api.pw_gid == 0:
            raise RuntimeFailure("The API identity must not share root credentials")
        for role, name in expected_accounts().items():
            saved = self.accounts[role]
            account, group = pwd.getpwnam(name), grp.getgrnam(name)
            if (not isinstance(saved, dict) or set(saved) != {"name", "uid", "gid"} or saved.get("name") != name
                    or type(saved.get("uid")) is not int or type(saved.get("gid")) is not int
                    or account.pw_uid != saved["uid"] or account.pw_gid != saved["gid"]
                    or group.gr_gid != saved["gid"] or account.pw_uid == 0 or account.pw_gid == 0
                    or account.pw_uid == api.pw_uid or account.pw_gid == api.pw_gid
                    or account.pw_uid in seen_uids or account.pw_gid in seen_gids
                    or account.pw_shell not in {"/usr/sbin/nologin", "/sbin/nologin", "/bin/false"}
                    or account.pw_gecos != f"{PURPOSE} {role} {self.installation_id}"
                    or account.pw_dir != f"/nonexistent/shiri/{name}"):
                raise RuntimeFailure("A managed daemon identity changed; stop owned units before repairing the account map")
            # Unexpected supplementary groups must not silently grant host file
            # or device privileges. Units add only exact required groups later.
            extras = {item.gr_name for item in grp.getgrall() if name in item.gr_mem and item.gr_gid != account.pw_gid}
            if extras or group.gr_mem:
                raise RuntimeFailure("A daemon account has unexpected shared group membership")
            seen_uids.add(account.pw_uid)
            seen_gids.add(account.pw_gid)

    def account(self, role: str):
        try:
            return self.accounts[role]
        except KeyError as exc:
            raise RuntimeFailure("Unknown managed daemon role") from exc
