"""Exact fixture configuration and bounded failure-log regressions.

These tests stop at the daemon launch boundary and do not create namespaces,
services, or connections. They are not an encrypted network gate receipt.
"""
import ast
import os
from pathlib import Path
import secrets
import stat
from types import SimpleNamespace
from uuid import uuid4
import wave

import numpy as np
import pytest

from shiri.runtime.configuration import quote, write_private
from shiri.runtime.units import Bind, VIEW


PATH = Path(__file__).with_name("linux")/"check_native_network.py"
TREE = ast.parse(PATH.read_text(), filename=str(PATH))
FUNCTIONS = [node for node in TREE.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))
             and node.name in {"log_tail", "Rig"}]
assert {node.name for node in FUNCTIONS} == {"log_tail", "Rig"}
NAMESPACE = {"os": os, "stat": stat, "uuid4": uuid4, "wave": wave, "np": np,
             "secrets": secrets, "quote": quote, "write_private": write_private,
             "Bind": Bind, "VIEW": VIEW, "RATE": 48000, "SOURCE_PORT": 3929, "TERMINAL_SLOT": 6}
exec(compile(ast.Module(body=FUNCTIONS, type_ignores=[]), str(PATH), "exec"), NAMESPACE)
Rig, log_tail = NAMESPACE["Rig"], NAMESPACE["log_tail"]


def test_regular_log_tail_is_bounded_and_keeps_latest_failure(tmp_path):
    path = tmp_path/"owntone.log"
    data = b"old debug line\n"*2000+b"actual startup failure\n"
    path.write_bytes(data)
    assert log_tail(path) == data[-8192:].decode()


def test_log_tail_does_not_follow_link(tmp_path):
    target = tmp_path/"target"
    target.write_text("unowned content")
    link = tmp_path/"owntone.log"
    link.symlink_to(target)
    assert log_tail(link) == {"read_error": "OSError"}


def test_log_tail_rejects_fifo_without_waiting_for_writer(tmp_path):
    path = tmp_path/"owntone.log"
    os.mkfifo(path, 0o600)
    assert log_tail(path) == {"read_error": "NonRegularFile"}


@pytest.mark.asyncio
async def test_exact_source_config_uses_its_writable_state_mount(tmp_path, monkeypatch):
    class LaunchBoundary(Exception):
        pass

    launches = []
    account = {"name": "shiri-output-6", "uid": os.getuid(), "gid": os.getgid()}

    async def start_process(*args, **kwargs):
        launches.append((args, kwargs))
        raise LaunchBoundary

    def local_directory(path, account=None, *, mode=0o700):
        path.mkdir(parents=True, exist_ok=True, mode=mode)
        return path

    # Only privileged ownership helpers and the service launch are substituted.
    # The actual fixture writes its WAV and config and constructs actual binds.
    monkeypatch.setitem(NAMESPACE, "root_directory", local_directory)
    monkeypatch.setitem(NAMESPACE, "directory", local_directory)
    monkeypatch.setitem(NAMESPACE, "file_owner", lambda *args, **kwargs: None)
    sender = {"namespace": "fixture_sender", "api_ip": "198.18.254.2", "api_host_ip": "198.18.254.1"}
    broker = SimpleNamespace(sender=sender, sender_users=set(), _account=lambda *args: account,
                             _start_process=start_process, binary=lambda _: "/opt/shiri/bin/owntone",
                             config=SimpleNamespace(runtime_state_dir=tmp_path/"runtime"))
    rig = Rig(broker, tmp_path/"rig", "isolated_fixture")
    with pytest.raises(LaunchBoundary):
        await rig.start_source()
    assert len(launches) == 1
    args, kwargs = launches[0]
    text = (rig.root/"source/config/owntone.conf").read_text()
    assert f"logfile = {quote(VIEW/'state/owntone.log')}" in text
    assert "/dev/stdout" not in text
    assert kwargs["account"] == account and kwargs["listen_port"] == 3929
    assert kwargs["namespace"] == sender["namespace"]
    assert Bind(str(rig.root/"source/state"), str(VIEW/"state"), True) in kwargs["binds"]
    assert args[2][0] == "/opt/shiri/bin/owntone"


@pytest.mark.asyncio
async def test_source_failure_keeps_bounded_journal_and_daemon_file(tmp_path):
    async def diagnostics(_room_id):
        return {"production": "kept"}

    broker = SimpleNamespace(diagnostics=diagnostics, sender_processes={})
    rig = Rig(broker, tmp_path/"rig", "isolated_fixture")
    state = rig.root/"source/state"
    state.mkdir(parents=True)
    daemon = state/"owntone.log"
    daemon_data = b"scanning\n"*2000+b"actual source transport failure\n"
    daemon.write_bytes(daemon_data)
    journal = tmp_path/"source-journal.log"
    journal.write_text("systemd exact owned unit exited\n")
    rig.source_processes["owntone"] = SimpleNamespace(log_path=journal)
    record = await rig.diagnostics()
    assert record["production"] == "kept"
    assert record["fixture_logs"]["source"]["owntone"] == journal.read_text()
    assert record["fixture_logs"]["source"]["owntone_file"] == daemon_data[-8192:].decode()
