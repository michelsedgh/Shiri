"""Kernel-fixture ownership regressions; no namespace/link/process is created."""
import copy
import importlib.util
import ctypes
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("isolated_lan_review", ROOT / "tests/linux/isolated_group_lan.py")
lan_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lan_module)


class Kernel:
    def __init__(self, lan, *, tagged=True):
        self.lan, self.commands, self.namespace_inode, self.namespace_pids = lan, [], 101, ""
        alias = lan.alias if tagged else ""
        self.host = [
            {"ifname": lan.interface, "ifindex": 71, "address": lan.mac, "ifalias": alias,
             "linkinfo": {"info_kind": "bridge"}},
            {"ifname": lan.host_veth, "ifindex": 72, "address": "02:01:02:03:04:05", "ifalias": alias,
             "master": lan.interface, "linkinfo": {"info_kind": "veth"}},
        ]
        self.peer = [{"ifname": lan.peer_veth, "ifindex": 73, "address": "02:05:04:03:02:01",
                      "ifalias": lan.alias, "linkinfo": {"info_kind": "veth"}}]
        lan.record["namespace"] = {"name": lan.namespace, "st_dev": 1, "st_ino": 101}
        for entry in self.host + self.peer:
            lan.record["links"][entry["ifname"]] = {
                "ifindex": entry["ifindex"], "kind": entry["linkinfo"]["info_kind"],
                "address": entry["address"], "namespace": lan.namespace if entry in self.peer else None,
                "tagged": True if entry in self.peer else tagged,
            }

    async def json(self, args):
        return copy.deepcopy(self.peer if args[:3] == ["ip", "netns", "exec"] else self.host)

    async def run(self, args, *, timeout):
        assert timeout == 5
        if args[:3] == ["ip", "netns", "pids"]:
            return SimpleNamespace(stdout=self.namespace_pids)
        self.commands.append(args)
        if args == ["ip", "link", "delete", self.lan.host_veth]:
            self.host = [link for link in self.host if link["ifname"] != self.lan.host_veth]
            self.peer = []
        elif args == ["ip", "link", "delete", self.lan.interface]:
            self.host = [link for link in self.host if link["ifname"] != self.lan.interface]
        elif args != ["ip", "netns", "delete", self.lan.namespace]:
            raise AssertionError(f"Unexpected mutation: {args}")
        return SimpleNamespace(stdout="")


@pytest.fixture
def owned_lan(tmp_path, monkeypatch):
    monkeypatch.setattr(lan_module, "boot_id", lambda: "boot-A")
    root = tmp_path / "lan"
    root.mkdir()
    lan = lan_module.IsolatedLan(root)
    kernel = Kernel(lan)
    lan.runner = kernel

    class NamespacePath:
        def __truediv__(self, name):
            assert name == lan.namespace
            return self

        def stat(self):
            return SimpleNamespace(st_dev=1, st_ino=kernel.namespace_inode)

    monkeypatch.setattr(lan_module, "Path", lambda path: NamespacePath())
    lan.save()
    return lan, kernel


@pytest.mark.asyncio
async def test_cleanup_removes_only_exact_fixture_after_all_resources_are_verified(owned_lan):
    lan, kernel = owned_lan
    await lan.close()
    assert kernel.commands == [["ip", "link", "delete", lan.host_veth],
                               ["ip", "netns", "delete", lan.namespace],
                               ["ip", "link", "delete", lan.interface]]
    assert not kernel.host and not kernel.peer
    assert lan.record["cleaned"] and lan.record["namespace"] is None and not lan.record["links"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("ifindex", 999), ("address", "02:99:99:99:99:99"),
                                        ("ifalias", "someone-else"), ("linkinfo", {"info_kind": "dummy"})])
async def test_replaced_link_is_preserved_and_cleanup_cannot_delete_other_fixture_links(owned_lan, field, value):
    lan, kernel = owned_lan
    kernel.host[0][field] = value
    original = (lan.root / "isolated-lan.json").read_bytes()
    with pytest.raises(lan_module.RuntimeFailure, match="identity changed"):
        await lan.close()
    assert not kernel.commands
    assert (lan.root / "isolated-lan.json").read_bytes() == original
    assert len(kernel.host) == 2 and len(kernel.peer) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["namespace", "process", "boot", "child", "missing_link", "malformed_record"])
async def test_ambiguous_or_foreign_resources_block_all_kernel_deletion(owned_lan, monkeypatch, damage):
    lan, kernel = owned_lan
    if damage == "namespace":
        kernel.namespace_inode = 202
    elif damage == "process":
        kernel.namespace_pids = "99999\n"
    elif damage == "boot":
        monkeypatch.setattr(lan_module, "boot_id", lambda: "boot-B")
    elif damage == "child":
        kernel.host.append({"ifname": "foreign-child", "ifindex": 79, "address": "02:33:33:33:33:33",
                            "link_index": 71, "linkinfo": {"info_kind": "macvlan"}})
    elif damage == "missing_link":
        kernel.peer.clear()
    else:
        del lan.record["links"][lan.host_veth]["namespace"]
    with pytest.raises((lan_module.RuntimeFailure, KeyError)):
        await lan.close()
    assert not kernel.commands
    assert not lan.record.get("cleaned")


@pytest.mark.asyncio
async def test_interrupted_before_alias_set_can_cleanup_only_captured_exact_link_identity(owned_lan):
    lan, kernel = owned_lan
    for name in [lan.interface, lan.host_veth]:
        lan.record["links"][name]["tagged"] = False
    for link in kernel.host:
        link["ifalias"] = ""
    await lan.close()
    assert len(kernel.commands) == 3 and lan.record["cleaned"]


@pytest.mark.asyncio
async def test_untagged_intermediate_link_does_not_adopt_a_foreign_alias(owned_lan):
    lan, kernel = owned_lan
    lan.record["links"][lan.interface]["tagged"] = False
    kernel.host[0]["ifalias"] = "foreign-owner"
    with pytest.raises(lan_module.RuntimeFailure, match="identity changed"):
        await lan.close()
    assert not kernel.commands


@pytest.mark.asyncio
async def test_unknown_boot_cannot_authorize_cleanup_even_when_both_reads_are_none(owned_lan, monkeypatch):
    lan, kernel = owned_lan
    lan.record['boot_id'] = None
    monkeypatch.setattr(lan_module, 'boot_id', lambda: None)
    with pytest.raises(lan_module.RuntimeFailure, match='boot changed'):
        await lan.close()
    assert not kernel.commands


@pytest.mark.asyncio
@pytest.mark.parametrize('unsafe', ['unknown_boot', 'original_parent', 'uplink'])
async def test_parent_namespace_admission_refuses_before_creating_any_kernel_resource(tmp_path, monkeypatch, unsafe):
    monkeypatch.setattr(lan_module, 'boot_id', lambda: None if unsafe == 'unknown_boot' else 'boot-A')
    monkeypatch.setattr(lan_module.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(lan_module.shutil, 'which', lambda name: '/usr/sbin/dnsmasq')
    original, parent, named = (tmp_path/name for name in ('original-net', 'current-net', 'named-parent-net'))
    original.write_text('fake held original namespace')
    if unsafe == 'original_parent':
        parent = original
    else:
        parent.write_text('fake held disconnected namespace')
    os.link(parent, named)
    original_open = os.open
    def opened(path, *args):
        targets = {'/proc/self/ns/net': parent, '/run/netns/shiri_group_run_12345678': named}
        assert not str(path).startswith('/proc/1/')
        return original_open(targets.get(str(path), path), *args)
    monkeypatch.setattr(lan_module.os, 'open', opened)
    real_readlink = os.readlink
    def readlink(path, *args, **kwargs):
        assert not str(path).startswith('/proc/1/'), 'PID1 must not be read'
        return real_readlink(path, *args, **kwargs)
    monkeypatch.setattr(lan_module.os, 'readlink', readlink)
    monkeypatch.setattr(lan_module, 'namespace_identity', lambda fd: (os.fstat(fd).st_dev, os.fstat(fd).st_ino))
    lan = lan_module.IsolatedLan(tmp_path / 'new-lan')
    commands = []

    class UnsafeKernel:
        async def json(self, args):
            return [{'ifname': 'lo'}] + ([{'ifname': 'external-uplink'}] if unsafe == 'uplink' else [])

        async def run(self, args, **kwargs):
            commands.append(args)
            raise AssertionError('Unsafe parent must not perform a kernel mutation')

    lan.runner = UnsafeKernel()
    descriptor = original_open(original, os.O_RDONLY)
    try:
        with pytest.raises(lan_module.RuntimeFailure, match='fresh disconnected parent'):
            await lan.start(original_netns_fd=descriptor, parent_namespace='shiri_group_run_12345678')
    finally:
        os.close(descriptor)
    assert not commands
    assert not lan.record['links'] and lan.record['namespace'] is None


@pytest.mark.parametrize('bad', ['regular_file', 'closed_fd', 'not_fd'])
def test_namespace_proof_requires_genuine_open_nsfs_descriptor(tmp_path, bad):
    path = tmp_path/'ordinary-file'
    path.write_text('namespace-looking text is not authority')
    descriptor = os.open(path, os.O_RDONLY)
    try:
        if bad == 'closed_fd':
            os.close(descriptor)
        value = str(descriptor) if bad == 'not_fd' else descriptor
        with pytest.raises(lan_module.RuntimeFailure, match='namespace'):
            lan_module.namespace_identity(value)
    finally:
        if bad != 'closed_fd':
            os.close(descriptor)


@pytest.mark.parametrize('namespace_type', [lan_module.CLONE_NEWNET, 0x20000])
def test_nsfs_fd_must_be_typed_network_namespace(tmp_path, monkeypatch, namespace_type):
    monkeypatch.setattr(lan_module.sys, 'platform', 'linux')
    path = tmp_path/'simulated-nsfs-inode'
    path.write_text('held inode only; kernel metadata is mocked')
    descriptor = os.open(path, os.O_RDONLY)
    class FstatFS:
        def __call__(self, fd, buffer):
            assert fd == descriptor
            ctypes.c_long.from_buffer(buffer).value = lan_module.NSFS_MAGIC
            return 0
    monkeypatch.setattr(lan_module.ctypes, 'CDLL', lambda *_args, **_kwargs: SimpleNamespace(fstatfs=FstatFS()))
    def ioctl(fd, command):
        assert fd == descriptor and command == 0xB703
        return namespace_type
    monkeypatch.setattr(lan_module.fcntl, 'ioctl', ioctl)
    try:
        if namespace_type == lan_module.CLONE_NEWNET:
            assert lan_module.namespace_identity(descriptor) == (path.stat().st_dev, path.stat().st_ino)
        else:
            with pytest.raises(lan_module.RuntimeFailure, match='genuine held network'):
                lan_module.namespace_identity(descriptor)
    finally:
        os.close(descriptor)


@pytest.mark.parametrize('drift', [None, 'original', 'named_parent'])
def test_disconnected_parent_compares_held_fds_and_closes_only_owned_observers(tmp_path, monkeypatch, drift):
    original, current, named = (tmp_path/name for name in ('original', 'current', 'named'))
    original.write_text('original kernel identity')
    current.write_text('current kernel identity')
    if drift == 'original':
        current = original
    if drift == 'named_parent':
        named.write_text('replacement parent identity')
    else:
        os.link(current, named)
    real_open, opened = os.open, []
    original_fd = real_open(original, os.O_RDONLY)
    def observer(path, *args):
        assert str(path) in {'/proc/self/ns/net', '/run/netns/shiri_group_run_12345678'}
        descriptor = real_open(current if str(path) == '/proc/self/ns/net' else named, *args)
        opened.append(descriptor)
        return descriptor
    monkeypatch.setattr(lan_module.os, 'open', observer)
    monkeypatch.setattr(lan_module, 'namespace_identity', lambda fd: (os.fstat(fd).st_dev, os.fstat(fd).st_ino))
    try:
        if drift:
            with pytest.raises(lan_module.RuntimeFailure, match='fresh disconnected parent'):
                lan_module.disconnected_parent(original_fd, 'shiri_group_run_12345678')
        else:
            proof = lan_module.disconnected_parent(original_fd, 'shiri_group_run_12345678')
            assert proof['original']['st_ino'] == original.stat().st_ino
            assert proof['parent']['st_ino'] == current.stat().st_ino
        assert os.fstat(original_fd).st_ino == original.stat().st_ino  # Borrowed FD stays held by run_check.
        for descriptor in opened:
            with pytest.raises(OSError):
                os.fstat(descriptor)
    finally:
        os.close(original_fd)
