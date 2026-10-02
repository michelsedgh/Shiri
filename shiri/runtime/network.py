"""Deterministic namespaces and DHCP with a durable ownership boundary.

The installation identity and namespace inode are checked before cleanup. A DHCP
release additionally requires our exact macvlan MAC; ipvlan/host leases are never
released. Names alone never authorize deleting an existing host interface.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import hashlib
import ipaddress
import logging
import os
from pathlib import Path
import re
import signal
import stat
from uuid import UUID, uuid4

from .system import Runner, RuntimeFailure, atomic_json, boot_id, process_args, process_birth, read_json
from .units import UNIT_RE, UnitManager

log = logging.getLogger(__name__)
DHCP_ROOT = Path("/etc/dhcp/shiri")
DHCP_HOOK = DHCP_ROOT / "dhclient-script"


def stable_mac(installation_id: str, role: str) -> str:
    octets = hashlib.sha256(f"{installation_id}:{role}".encode()).digest()[:5]
    return "02:" + ":".join(f"{octet:02x}" for octet in octets)


class NetworkManager:
    def __init__(self, state_dir: Path, runner: Runner):
        self.state_dir, self.runner = state_dir, runner
        # Capture while the broker is still in the host namespace. The DHCP
        # hook cannot inspect PID 1 under the service's restricted capabilities.
        try:
            self.host_netns = os.readlink("/proc/self/ns/net")
        except OSError:
            self.host_netns = None
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.manifest_path = state_dir / "ownership.json"
        # Only a missing file creates an installation. An existing malformed
        # object must preserve its bytes and stable MAC identity for inspection.
        self.manifest = read_json(self.manifest_path)
        if self.manifest is None and not self.manifest_path.exists():
            installation_id = str(uuid4())
            self.manifest = {
                "version": 1,
                "installation_id": installation_id,
                "networks": {},
                "processes": {},
            }
            self.save()
        if not isinstance(self.manifest, dict):
            raise RuntimeFailure("Invalid runtime ownership manifest; recovery requires inspection")
        installation_id = self.manifest.get("installation_id")
        try:
            UUID(installation_id)
        except (ValueError, TypeError) as exc:
            raise RuntimeFailure(
                "Invalid installation identity in runtime state; recovery requires inspection"
            ) from exc
        if (
            self.manifest.get("version") != 1
            or not isinstance(self.manifest.get("networks"), dict)
            or not isinstance(self.manifest.get("processes"), dict)
        ):
            raise RuntimeFailure("Unsupported runtime ownership manifest")
        self.installation_id = installation_id
        self.installation_tag = UUID(installation_id).hex[:8]
        self._validate_records()

    def _validate_records(self):
        for key, record in self.manifest["networks"].items():
            if not isinstance(key, str) or not isinstance(record, dict):
                raise RuntimeFailure("Invalid network ownership record")
            room_id = None
            try:
                if key != "sender":
                    if not key.startswith("receiver:"):
                        raise ValueError("Unknown network role")
                    room_id = str(UUID(key.removeprefix("receiver:")))
                    if key != f"receiver:{room_id}":
                        raise ValueError("Noncanonical room identity")
                parent = record.get("parent")
                if (
                    not isinstance(parent, str)
                    or not parent
                    or len(parent) > 15
                    or any(
                        char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
                        for char in parent
                    )
                ):
                    raise ValueError("Invalid parent interface")
                expected = self.new_record(key, parent, room_id=room_id)
                for name, value in expected.items():
                    if name not in {"inode", "boot_id", "lan_tagged"} and record.get(name) != value:
                        raise ValueError(f"Network {name} identity changed")
                if record.get("boot_id") is not None and not isinstance(record["boot_id"], str):
                    raise ValueError("Invalid namespace boot identity")
                if "lan_tagged" in record and type(record["lan_tagged"]) is not bool:
                    raise ValueError("Invalid LAN creation phase")
                if "parent_ifindex" in record and (
                    type(record["parent_ifindex"]) is not int or record["parent_ifindex"] <= 0
                ):
                    raise ValueError("Invalid parent interface identity")
                inode = record.get("inode")
                if inode is not None and (type(inode) is not int or inode <= 0):
                    raise ValueError("Invalid namespace inode")
                if "ip" in record:
                    ipaddress.IPv4Address(record["ip"])
                if "api_host_interface" in record:
                    if key != "sender" or record.get("api_host_interface") != f"sh{self.installation_tag}":
                        raise ValueError("Invalid control link")
                    if (
                        record.get("api_peer_interface") != f"sn{self.installation_tag}"
                        or record.get("api_alias") != f"shiri:{self.installation_id}:api"
                        or record.get("api_prefix") != 30
                    ):
                        raise ValueError("Invalid control link identity")
                    host = ipaddress.IPv4Address(record["api_host_ip"])
                    peer = ipaddress.IPv4Address(record["api_ip"])
                    if int(peer) != int(host) + 1:
                        raise ValueError("Invalid control address pair")
                    for field, role in [("api_host_mac", "api-host"), ("api_peer_mac", "api-peer")]:
                        if field in record and record[field] != stable_mac(self.installation_id, role):
                            raise ValueError("Invalid control MAC identity")
                    if "api_tagged" in record and type(record["api_tagged"]) is not bool:
                        raise ValueError("Invalid control link creation phase")
            except (ValueError, TypeError, KeyError) as exc:
                raise RuntimeFailure(f"Invalid network ownership record {key}: {exc}") from exc
        for key, entry in self.manifest["processes"].items():
            if isinstance(entry, dict) and entry.get("kind") == "systemd-unit":
                if not isinstance(key, str):
                    raise RuntimeFailure("Invalid daemon reservation key")
                UnitManager.validate_saved(entry)
                if entry.get("gate", {}).get("directory") not in {
                    None, str(self.state_dir / "launch-gates" / entry["unit"]),
                }:
                    raise RuntimeFailure("Daemon gate belongs to another installation's state directory")
                owner, separator, role = key.partition(":")
                try:
                    canonical_owner = 'sender' if owner == 'sender' else str(UUID(owner))
                    unit_owner = 'sender' if owner == 'sender' else UUID(owner).hex
                except ValueError:
                    raise RuntimeFailure("Daemon reservation has an invalid canonical room owner") from None
                identity = UNIT_RE.fullmatch(entry['unit'])
                if (not separator or owner != canonical_owner or entry.get("name") != role
                        or identity['installation'] != self.installation_tag
                        or identity['owner'] != unit_owner or identity['role'] != role):
                    raise RuntimeFailure("Daemon unit reservation belongs to another installation or room")
                publication = entry.get("socket_publication")
                if publication:
                    directory = Path(publication["directory"])
                    if directory.parent != self.state_dir / "rooms" / owner / "bridge-published":
                        raise RuntimeFailure("Bridge socket publication belongs to another room or installation")
                continue
            if (
                not isinstance(key, str)
                or not isinstance(entry, dict)
                or type(entry.get("pid")) is not int
                or entry["pid"] <= 1
                or entry.get("pgid") != entry["pid"]
                or not isinstance(entry.get("birth"), str)
                or not entry["birth"]
                or not isinstance(entry.get("name"), str)
                or not isinstance(entry.get("log_path"), str)
            ):
                raise RuntimeFailure("Invalid process ownership record")
            if any(name in entry and not isinstance(entry[name], str) for name in ["boot_id", "executable"]):
                raise RuntimeFailure("Invalid process executable/boot identity")
            if "argv" in entry and (
                not isinstance(entry["argv"], list) or not all(isinstance(arg, str) for arg in entry["argv"])
            ):
                raise RuntimeFailure("Invalid process command identity")

    def save(self):
        atomic_json(self.manifest_path, self.manifest)

    def remember_process(self, key: str, process, *, identity=None):
        identity = identity if identity is not None else process.identity()
        if identity.get("kind") == "systemd-unit":
            self.remember_unit(key, identity)
            return
        if not identity.get("boot_id") or not identity.get("executable") or not identity.get("argv"):
            raise RuntimeFailure("A backend process exited before its complete identity could be recorded")
        self.manifest.setdefault("processes", {})[key] = identity
        self.save()

    def reserve_unit(self, key: str, entry: dict):
        UnitManager.validate_saved(entry)
        if key in self.manifest["processes"]:
            raise RuntimeFailure("An earlier daemon reservation still owns this room role")
        self.manifest["processes"][key] = entry
        self.save()

    def remember_unit(self, key: str, entry: dict):
        UnitManager.validate_saved(entry)
        previous = self.manifest["processes"].get(key)
        if (not previous or previous.get("kind") != "systemd-unit"
                or previous.get("unit") != entry["unit"]
                or (previous.get("invocation_id") and previous["invocation_id"] != entry["invocation_id"])):
            raise RuntimeFailure("Daemon unit ownership changed while recording its identity")
        self.manifest["processes"][key] = entry
        self.save()

    def forget_unit(self, key: str):
        self.forget_process(key)

    def forget_process(self, key: str):
        self.manifest.setdefault("processes", {}).pop(key, None)
        self.save()

    async def namespace_exists(self, name: str) -> bool:
        result = await self.runner.run(["ip", "netns", "list"], check=True)
        listed = any(line.split()[0] == name for line in result.stdout.splitlines() if line.split())
        return listed and await asyncio.to_thread(os.path.ismount, f"/run/netns/{name}")

    def namespace_inode(self, name: str):
        try:
            return Path("/run/netns", name).stat().st_ino
        except OSError:
            return None

    async def link(self, namespace: str | None, name: str) -> dict:
        args = ["ip", "-j", "-d", "link", "show", "dev", name]
        links = await self.runner.json(
            (["ip", "netns", "exec", namespace] + args) if namespace else args, check=False
        )
        return links[0] if links else {}

    async def ipv4(self, namespace: str, name: str) -> str:
        links = await self.runner.json(
            ["ip", "netns", "exec", namespace, "ip", "-j", "-4", "addr", "show", "dev", name]
        )
        for link in links:
            for address in link.get("addr_info", []):
                if address.get("family") == "inet":
                    return address["local"]
        return ""

    async def owned_macvlan(self, record: dict, *, on_host=False) -> bool:
        link = await self.link(None if on_host else record["namespace"], record["interface"])
        return (
            link.get("linkinfo", {}).get("info_kind") == "macvlan"
            and link.get("address", "").lower() == record["mac"]
            and link.get("ifalias") == record["alias"]
        )

    async def owned_namespace(self, record: dict) -> bool:
        namespace = record["namespace"]
        if not await self.namespace_exists(namespace):
            return False
        if not record.get("boot_id") or record["boot_id"] != boot_id():
            return False
        inode = self.namespace_inode(namespace)
        if record.get("inode") is not None:
            return inode == record["inode"]
        # Recover a crash between namespace creation and recording its inode only
        # when it is empty, or contains the exact interface we created and tagged.
        if await self.owned_macvlan(record):
            return True
        links = await self.runner.json(["ip", "netns", "exec", namespace, "ip", "-j", "link", "show"])
        return all(link.get("ifname") == "lo" for link in links) and not await self.namespace_pids(namespace)

    async def namespace_pids(self, namespace: str) -> list[int]:
        result = await self.runner.run(["ip", "netns", "pids", namespace], check=False)
        return [int(token) for token in result.stdout.split() if token.isdigit() and int(token) > 1]

    async def healthy(self, record: dict) -> bool:
        if not await self.owned_namespace(record) or not await self.owned_macvlan(record):
            return False
        if record.get("role") == "sender":
            await self.validate_sender_ephemeral_range(record)
        if not await self.ipv4(record["namespace"], record["interface"]):
            return False
        try:
            pid = int((await asyncio.to_thread(Path(record["pid_file"]).read_text)).strip())
        except (OSError, ValueError):
            return False
        return bool(
            process_birth(pid)
            and record["pid_file"] in process_args(pid)
            and pid in await self.namespace_pids(record["namespace"])
        )

    async def validate_sender_ephemeral_range(self, record: dict):
        # listen() without an explicit bind can autoallocate a port without
        # passing the cgroup bind hook. Never allow that allocation to reach
        # another room's fixed authenticated HTTP endpoint.
        if record.get("role") != "sender" or not await self.owned_namespace(record):
            raise RuntimeFailure("Cannot validate an unowned sender namespace")
        result = await self.runner.run([
            "ip", "netns", "exec", record["namespace"], "/usr/bin/cat",
            "/proc/sys/net/ipv4/ip_local_port_range",
        ])
        values = result.stdout.split()
        if (len(values) != 2 or any(not value.isdecimal() for value in values)
                or not 1024 <= int(values[0]) <= int(values[1]) <= 65535
                or (int(values[0]) <= 3939 and int(values[1]) >= 3869)):
            raise RuntimeFailure("Sender automatic port range overlaps room control ports; refusing daemon launch")
        return tuple(map(int, values))

    def new_record(self, role: str, parent: str, *, room_id: str | None = None):
        suffix = UUID(room_id).hex[:12] if room_id else self.installation_tag
        namespace = f"shiri_rx_{self.installation_tag}_{suffix}" if room_id else f"shiri_ot_{suffix}"
        interface = f"sr{suffix}" if room_id else f"so{suffix}"
        digest = hashlib.sha256(f"{self.installation_id}:{role}".encode()).hexdigest()[:16]
        return {
            "role": role,
            "namespace": namespace,
            "interface": interface,
            "parent": parent,
            "mac": stable_mac(self.installation_id, role),
            "alias": f"shiri:{self.installation_id}:{role}",
            "inode": None,
            "boot_id": boot_id(),
            "lan_tagged": False,
            "lease_file": f"/var/lib/dhcp/dhclient-shiri-{digest}.leases",
            "pid_file": f"/run/dhclient-shiri-{digest}.pid",
            "dhcp_config": str(DHCP_ROOT / f"{digest}.conf"),
        }

    async def _create_lan(self, key: str, record: dict):
        existing = self.manifest["networks"].get(key)
        if existing:
            await self.remove(key)
        if await self.namespace_exists(record["namespace"]):
            raise RuntimeFailure(
                f"Namespace {record['namespace']} exists without matching ownership; refusing takeover"
            )
        if await self.link(None, record["interface"]):
            raise RuntimeFailure(
                f"LAN interface name {record['interface']} is already in use; refusing replacement"
            )
        parent = await self.link(None, record["parent"])
        if type(parent.get("ifindex")) is not int or parent["ifindex"] <= 0:
            raise RuntimeFailure("The LAN parent interface has no verifiable kernel identity")
        record["parent_ifindex"] = parent["ifindex"]
        # Reserve intent before the first system mutation for crash recovery.
        self.manifest["networks"][key] = record
        self.save()
        await self.runner.run(["ip", "netns", "add", record["namespace"]])
        record["inode"] = self.namespace_inode(record["namespace"])
        self.save()
        ns = ["ip", "netns", "exec", record["namespace"]]
        await self.runner.run(ns + ["ip", "link", "set", "lo", "up"])
        await self.runner.run(
            [
                "ip",
                "link",
                "add",
                record["interface"],
                "link",
                record["parent"],
                "address",
                record["mac"],
                "alias",
                record["alias"],
                "type",
                "macvlan",
                "mode",
                "bridge",
            ]
        )
        # NEWLINK alias is silently ignored on some iproute2/kernel versions.
        # Explicit SET and readback are required before the link is ready.
        await self.runner.run(["ip", "link", "set", record["interface"], "alias", record["alias"]])
        if not await self.owned_macvlan(record, on_host=True):
            raise RuntimeFailure("The new LAN interface did not retain its ownership identity")
        record["lan_tagged"] = True
        self.save()
        await self.runner.run(["ip", "link", "set", record["interface"], "netns", record["namespace"]])
        await self.runner.run(ns + ["ip", "link", "set", record["interface"], "up"])
        record["ip"] = await self.acquire_dhcp(record)
        self.save()
        return record

    async def create_receiver(self, room):
        key = f"receiver:{room.id}"
        record = self.new_record(key, room.interface, room_id=room.id)
        try:
            return await self._create_lan(key, record)
        except BaseException:
            await asyncio.shield(self.remove(key))
            raise

    async def _api_subnet(self):
        routes = await self.runner.json(["ip", "-j", "-4", "route", "show", "table", "all"])
        occupied = []
        for route in routes:
            if route.get("dst") in {None, "default"}:
                continue
            with suppress(ValueError):
                occupied.append(ipaddress.ip_network(route["dst"], strict=False))
        digest = hashlib.sha256(self.installation_id.encode()).digest()
        for step in range(256):
            subnet = ipaddress.ip_network(f"10.{128 + digest[0] % 64}.{(digest[1] + step) % 256}.0/30")
            if not any(subnet.overlaps(route) for route in occupied):
                return subnet
        raise RuntimeFailure("No unused private subnet available for the OwnTone control link")

    async def create_sender(self, parent: str):
        record = self.new_record("sender", parent)
        try:
            await self._create_lan("sender", record)
            subnet = await self._api_subnet()
            host, peer = f"sh{self.installation_tag}", f"sn{self.installation_tag}"
            if await self.link(None, host) or await self.link(None, peer):
                raise RuntimeFailure("OwnTone control interface name is already in use")
            record.update(
                api_host_interface=host,
                api_peer_interface=peer,
                api_host_ip=str(subnet[1]),
                api_ip=str(subnet[2]),
                api_prefix=subnet.prefixlen,
                api_alias=f"shiri:{self.installation_id}:api",
                api_host_mac=stable_mac(self.installation_id, "api-host"),
                api_peer_mac=stable_mac(self.installation_id, "api-peer"),
                api_tagged=False,
            )
            self.save()
            await self.runner.run(
                [
                    "ip",
                    "link",
                    "add",
                    host,
                    "address",
                    record["api_host_mac"],
                    "type",
                    "veth",
                    "peer",
                    "name",
                    peer,
                    "address",
                    record["api_peer_mac"],
                ]
            )
            await self.runner.run(["ip", "link", "set", host, "alias", record["api_alias"]])
            host_link = await self.link(None, host)
            if (
                host_link.get("ifalias") != record["api_alias"]
                or host_link.get("linkinfo", {}).get("info_kind") != "veth"
                or host_link.get("address", "").lower() != record["api_host_mac"]
            ):
                raise RuntimeFailure("The new control link did not retain its ownership identity")
            record["api_tagged"] = True
            self.save()
            await self.runner.run(["ip", "link", "set", peer, "netns", record["namespace"]])
            await self.runner.run(
                ["ip", "addr", "add", f"{record['api_host_ip']}/{subnet.prefixlen}", "dev", host]
            )
            await self.runner.run(["ip", "link", "set", host, "up"])
            ns = ["ip", "netns", "exec", record["namespace"]]
            await self.runner.run(
                ns + ["ip", "addr", "add", f"{record['api_ip']}/{subnet.prefixlen}", "dev", peer]
            )
            await self.runner.run(ns + ["ip", "link", "set", peer, "up"])
            await self.validate_sender_ephemeral_range(record)
            return record
        except BaseException:
            await asyncio.shield(self.remove("sender"))
            raise

    async def acquire_dhcp(self, record: dict):
        if not DHCP_HOOK.is_file() or not os.access(DHCP_HOOK, os.X_OK):
            raise RuntimeFailure("Private namespace DHCP hook is missing; run the Shiri installer")
        lease, pidfile, config = await asyncio.to_thread(self._prepare_dhcp_files, record)
        await self.runner.run(
            [
                "ip",
                "netns",
                "exec",
                record["namespace"],
                "dhclient",
                "-4",
                "-1",
                "-v",
                "-e",
                self._dhclient_namespace_environment(),
                "-cf",
                str(config),
                "-lf",
                str(lease),
                "-pf",
                str(pidfile),
                "-sf",
                str(DHCP_HOOK),
                record["interface"],
            ],
            timeout=18,
        )
        address = await self.ipv4(record["namespace"], record["interface"])
        if not address:
            raise RuntimeFailure("DHCP completed without an IPv4 address")
        await self.preflight_gateway(record)
        return address

    def _dhclient_namespace_environment(self):
        if not isinstance(self.host_netns, str) or not re.fullmatch(r"net:\[[1-9][0-9]*\]", self.host_netns):
            raise RuntimeFailure("Cannot prove the broker's host network namespace identity")
        return f"SHIRI_HOST_NETNS={self.host_netns}"

    def _prepare_dhcp_files(self, record: dict):
        lease, pidfile = Path(record["lease_file"]), Path(record["pid_file"])
        lease.parent.mkdir(parents=True, exist_ok=True)
        lease.touch(mode=0o600, exist_ok=True)
        lease.chmod(0o600)
        if pidfile.exists():
            try:
                pid = int(pidfile.read_text().strip())
            except ValueError:
                pid = 0
            if process_birth(pid):
                raise RuntimeFailure(
                    "An existing DHCP client still owns this identity; recovery must complete first"
                )
            pidfile.unlink()
        config = Path(record["dhcp_config"])
        config.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        config.write_text(f"timeout 12;\nretry 10;\nsend dhcp-client-identifier 1:{record['mac']};\n")
        config.chmod(0o600)
        return lease, pidfile, config

    async def preflight_gateway(self, record: dict):
        ns = ["ip", "netns", "exec", record["namespace"]]
        routes = await self.runner.json(
            ns + ["ip", "-j", "-4", "route", "show", "default", "dev", record["interface"]]
        )
        gateways = [route["gateway"] for route in routes if route.get("gateway")]
        if not gateways:
            return  # Local-only LANs can still reach their speakers.
        result = await self.runner.run(
            ns + ["ping", "-c", "1", "-W", "1", gateways[0]], timeout=3, check=False
        )
        if not result.returncode:
            return
        neighbors = await self.runner.json(ns + ["ip", "-j", "neigh", "show", "dev", record["interface"]])
        if any(
            neighbor.get("dst") == gateways[0]
            and neighbor.get("lladdr")
            and not set(neighbor.get("state", [])).intersection({"FAILED", "INCOMPLETE"})
            for neighbor in neighbors
        ):
            return  # ICMP can be blocked; resolved ARP still proves the bridge.
        raise RuntimeFailure(
            "DHCP works but the gateway cannot resolve via ARP; check VM bridging and secondary MACs"
        )

    async def release_dhcp(self, record: dict) -> bool:
        if not await self.owned_namespace(record) or not await self.owned_macvlan(record):
            return False
        if not await asyncio.to_thread(Path(record["lease_file"]).exists):
            return False
        pidfile = Path(record["pid_file"])
        if await asyncio.to_thread(pidfile.exists):
            with suppress(ValueError):
                pid = int((await asyncio.to_thread(pidfile.read_text)).strip())
                if process_birth(pid) and (
                    pid not in await self.namespace_pids(record["namespace"])
                    or record["pid_file"] not in process_args(pid)
                ):
                    log.error("Refusing DHCP release for a pidfile owned by a different process")
                    return False
        result = await self.runner.run(
            [
                "ip",
                "netns",
                "exec",
                record["namespace"],
                "dhclient",
                "-4",
                "-r",
                "-v",
                "-e",
                self._dhclient_namespace_environment(),
                "-cf",
                record["dhcp_config"],
                "-lf",
                record["lease_file"],
                "-pf",
                record["pid_file"],
                "-sf",
                str(DHCP_HOOK),
                record["interface"],
            ],
            timeout=8,
            check=False,
        )
        if result.returncode:
            log.warning("Owned DHCP client could not complete its release for %s", record["namespace"])
        return result.returncode == 0

    async def remove(self, key: str):
        record = self.manifest["networks"].get(key)
        if not record:
            return
        namespace = record["namespace"]
        if await self.namespace_exists(namespace):
            if not await self.owned_namespace(record):
                raise RuntimeFailure(
                    f"Namespace identity changed for {namespace}; refusing destructive cleanup"
                )
            try:
                await self.release_dhcp(record)
            except RuntimeFailure as exc:
                log.warning("DHCP release failed: %s", exc)
            await self._stop_dhcp_survivors(record)
            for _ in range(20):
                if not await self.namespace_pids(namespace):
                    break
                await asyncio.sleep(0.1)
            else:
                raise RuntimeFailure(f"Unrecorded processes remain in {namespace}; resources stay reserved")
            await self.runner.run(["ip", "netns", "delete", namespace])
        else:
            await asyncio.to_thread(self._remove_inactive_namespace_file, namespace)
        await self._remove_host_link(
            record["interface"],
            "macvlan",
            record["alias"],
            mac=record["mac"],
            record=record,
            creating=record.get("lan_tagged") is False,
        )
        host = record.get("api_host_interface")
        if host:
            await self._remove_host_link(
                host,
                "veth",
                record["api_alias"],
                mac=record.get("api_host_mac"),
                record=record,
                creating=record.get("api_tagged") is False,
            )
        self.manifest["networks"].pop(key, None)
        self.save()

    async def _stop_dhcp_survivors(self, record):
        """Stop only the exact private DHCP client through a captured pidfd.

        Room units were already proven empty. Unknown namespace occupants do
        not become signal authority merely because the namespace is ours.
        """
        pids = await self.namespace_pids(record["namespace"])
        if not pids:
            return
        try:
            descriptor = os.open(record["pid_file"], os.O_RDONLY | os.O_NOFOLLOW)
            try:
                info = os.fstat(descriptor)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1):
                    raise RuntimeFailure("DHCP process reservation is unsafe; resources stay reserved")
                pid = int(os.read(descriptor, 32).strip())
            finally:
                os.close(descriptor)
        except (OSError, ValueError, KeyError) as exc:
            raise RuntimeFailure("Unrecorded namespace occupants require inspection; nothing was signalled") from exc
        if pids != [pid]:
            raise RuntimeFailure("Unrecorded namespace occupants require inspection; nothing was signalled")
        birth = process_birth(pid)
        args = process_args(pid)
        if (not birth or not args or Path(args[0]).name != "dhclient"
                or record["pid_file"] not in args):
            raise RuntimeFailure("Namespace DHCP identity changed; nothing was signalled")
        handle, namespace_fd = None, None
        try:
            handle = os.pidfd_open(pid, 0)
            namespace_fd = os.open(f"/proc/{pid}/ns/net", os.O_RDONLY)
            if (process_birth(pid) != birth or os.fstat(namespace_fd).st_ino != record["inode"]
                    or not await self.owned_namespace(record)):
                raise RuntimeFailure("DHCP namespace or process changed before signal admission")
            signal.pidfd_send_signal(handle, signal.SIGTERM)
            await asyncio.sleep(0.3)
            with suppress(ProcessLookupError):
                signal.pidfd_send_signal(handle, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            raise RuntimeFailure("Cannot stop the captured owned DHCP client; resources stay reserved") from exc
        finally:
            for descriptor in [handle, namespace_fd]:
                if descriptor is not None:
                    os.close(descriptor)

    async def _remove_host_link(
        self,
        name: str,
        kind: str,
        alias: str,
        *,
        mac: str | None = None,
        record: dict | None = None,
        creating=False,
    ):
        # Destroying a namespace removes its veth peer asynchronously. Never use
        # a transient/incomplete dump as proof of ownership or lose the durable
        # reservation; a disappearing interface is successful cleanup.
        for _ in range(20):
            link = await self.link(None, name)
            if not link:
                return
            intermediate = bool(
                creating
                and record
                and mac
                and not link.get("ifalias")
                and record.get("boot_id")
                and record["boot_id"] == boot_id()
                and record.get("inode")
                and (kind != "macvlan" or link.get("link_index") == record.get("parent_ifindex"))
            )
            if (
                (link.get("ifalias") == alias or intermediate)
                and link.get("linkinfo", {}).get("info_kind") == kind
                and (mac is None or link.get("address", "").lower() == mac)
            ):
                await self.runner.run(["ip", "link", "delete", name], check=False)
            await asyncio.sleep(0.1)
        log.error(
            "Host link cleanup ownership unresolved: name=%s kind=%s alias=%s mac=%s observed=%r",
            name,
            kind,
            alias,
            mac,
            link,
        )
        raise RuntimeFailure(
            f"Host interface {name} remains or its ownership changed; resources stay reserved"
        )

    def _remove_inactive_namespace_file(self, namespace: str):
        # A service mount namespace may disappear during a crash while the empty
        # filesystem placeholder survives. It is owned by our reserved manifest;
        # never unmount/delete an active or substituted namespace handle.
        path = Path("/run/netns", namespace)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if not os.path.ismount(path) and stat.S_ISREG(info.st_mode) and info.st_uid == 0:
            path.unlink()

    async def recover(self):
        for key, entry in list(self.manifest.get("processes", {}).items()):
            await self.runner.stop_saved(entry)
            self.forget_process(key)
        for key in list(self.manifest["networks"]):
            await self.remove(key)

    async def interfaces(self):
        links = await self.runner.json(["ip", "-j", "-d", "link", "show"])
        addresses = await self.runner.json(["ip", "-j", "-4", "addr", "show"])
        ipv4 = {
            item["ifname"]: [
                address["local"] for address in item.get("addr_info", []) if address.get("family") == "inet"
            ]
            for item in addresses
        }
        candidates = []
        for link in links:
            name, kind = link.get("ifname", ""), link.get("linkinfo", {}).get("info_kind", "")
            if (
                name == "lo"
                or name.startswith(("docker", "veth", "virbr", "br-"))
                or kind in {"macvlan", "ipvlan", "veth", "tun", "wireguard", "dummy"}
                or link.get("link_type") != "ether"
            ):
                continue
            reason = ""
            if await asyncio.to_thread(Path("/sys/class/net", name, "wireless").exists):
                reason = "Wi-Fi generally cannot carry additional macvlan MACs; use a bridged wired adapter"
            elif "UP" not in link.get("flags", []):
                reason = "Interface is down"
            candidates.append(
                {
                    "name": name,
                    "ipv4": ipv4.get(name, []),
                    "up": "UP" in link.get("flags", []),
                    "eligible": not reason,
                    "reason": reason,
                    "mac": link.get("address", ""),
                }
            )
        return candidates
