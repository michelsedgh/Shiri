#!/usr/bin/env python3
# ruff: noqa: ASYNC240
"""Opt-in actual BlueALSA + private D-Bus/mock BlueZ; no adapter or host bus.

Run as root on Linux with --binary and its independently recorded SHA256. The
daemon uses nobody, no supplementary groups, a private STATE_DIRECTORY and the
test-only Unix-socket guard. The binary must support STATE_DIRECTORY. This is
an actual SBC-thread/PCM export check, not physical Bluetooth acceptance.
Use a whole-process watchdog; no deployment, pairing or service restart occurs.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import hashlib
import json
import math
import os
from pathlib import Path
import pwd
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

from dbus_next import Message, MessageType, Variant
from dbus_next.aio import MessageBus

from shiri.runtime.bluealsa import BlueALSA, DescriptorBus, _bounded
from shiri.runtime.bluetooth_output import PCMRelay
from shiri.runtime.pcm_transport import Frame, Operation

ROOT = Path(__file__).resolve().parents[2]
ADAPTER = "/org/bluez/hci15"
MAC = "AA:BB:CC:DD:EE:FF"
DEVICE = ADAPTER + "/dev_" + MAC.replace(":", "_")
TRANSPORT = DEVICE + "/fd0"
SOURCE_UUID = "0000110a-0000-1000-8000-00805f9b34fb"
SINK_UUID = "0000110b-0000-1000-8000-00805f9b34fb"
PROPERTIES = "org.freedesktop.DBus.Properties"
OBJECTS = "org.freedesktop.DBus.ObjectManager"
MEDIA = "org.bluez.MediaTransport1"


class MockBlueZ:
    """BlueZ protocol edges only; actual BlueALSA performs all PCM/codec work."""
    def __init__(self, bus):
        self.bus, self.registered = bus, asyncio.get_running_loop().create_future()
        self.state, self.calls, self.transport_fd = "idle", [], None
        self.rtp, self.drain_task, self.rtp_packets, self.rtp_bytes = None, None, 0, 0
        self.packet_errors = []
        self.active_handle = None

    def transport_properties(self):
        return {"Device": Variant("o", DEVICE), "UUID": Variant("s", SINK_UUID), "Codec": Variant("y", 0),
                "State": Variant("s", self.state), "Volume": Variant("q", 127), "Delay": Variant("q", 0)}

    def inventory(self):
        return {ADAPTER: {"org.bluez.Adapter1": {"Address": Variant("s", "12:34:56:78:90:AB"),
                                               "UUIDs": Variant("as", [])}, "org.bluez.Media1": {}},
                DEVICE: {"org.bluez.Device1": {"Address": Variant("s", MAC),
                                               "Adapter": Variant("o", ADAPTER),
                                               "Connected": Variant("b", True),
                                               "Name": Variant("s", "Private synthetic SBC sink")}},
                TRANSPORT: {MEDIA: self.transport_properties()}}

    def handle(self, message):
        if message.message_type != MessageType.METHOD_CALL:
            return False
        self.calls.append((message.interface, message.member))
        if (message.interface == "org.freedesktop.DBus.Introspectable"
                and message.member == "Introspect" and message.path == "/"):
            # BlueALSA gets BlueZ's unique owner from this real method reply
            # before admitting calls to its MediaEndpoint1 sender filter.
            # Export only the ObjectManager edge implemented by this mock.
            xml = ('<node><interface name="org.freedesktop.DBus.ObjectManager">'
                   '<method name="GetManagedObjects"><arg name="objects" '
                   'type="a{oa{sa{sv}}}" direction="out"/></method></interface>'
                   '<interface name="org.freedesktop.DBus.Introspectable">'
                   '<method name="Introspect"><arg name="xml" type="s" '
                   'direction="out"/></method></interface></node>')
            return Message.new_method_return(message, "s", [xml])
        if message.interface == OBJECTS and message.member == "GetManagedObjects" and message.path == "/":
            return Message.new_method_return(message, "a{oa{sa{sv}}}", [self.inventory()])
        if message.interface == "org.bluez.Media1" and message.member == "RegisterApplication" and message.path == ADAPTER:
            if not self.registered.done():
                self.registered.set_result((message.sender, message.body[0]))
            return Message.new_method_return(message)
        if message.interface == "org.bluez.BatteryProviderManager1" and message.member == "RegisterBatteryProvider":
            return Message.new_method_return(message)
        if message.interface == PROPERTIES and message.member == "GetAll":
            properties = self.inventory().get(message.path, {}).get(message.body[0], {})
            return Message.new_method_return(message, "a{sv}", [properties])
        if message.interface == PROPERTIES and message.member == "Set" and message.path == TRANSPORT:
            return Message.new_method_return(message)
        if message.interface == MEDIA and message.path == TRANSPORT and message.member in {"Acquire", "TryAcquire"}:
            if self.transport_fd is not None:
                return Message.new_error(message, "org.bluez.Error.Failed", "One exact synthetic transport only")
            self.transport_fd, self.rtp = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            self.rtp.setblocking(False)
            self.drain_task = asyncio.create_task(self.drain())
            # The real daemon obtains the FD before consuming the active signal.
            self.active_handle = asyncio.get_running_loop().call_later(0.05, self.change_state, "active")
            return Message.new_method_return(message, "hqq", [0, 1000, 1000], [self.transport_fd.fileno()])
        if message.interface == MEDIA and message.path == TRANSPORT and message.member == "Release":
            self.change_state("idle")
            return Message.new_method_return(message)
        return Message.new_error(message, "org.freedesktop.DBus.Error.UnknownMethod", "Private mock has no such method")

    def change_state(self, state):
        self.state = state
        self.bus.send(Message.new_signal(TRANSPORT, PROPERTIES, "PropertiesChanged", "sa{sv}as",
                                        [MEDIA, {"State": Variant("s", state)}, []]))

    async def drain(self):
        while True:
            packet = await asyncio.get_running_loop().sock_recv(self.rtp, 2000)
            if not packet:
                return
            # Actual SBC source RTP: fixed12-byte RTP +1-byte SBC frame count.
            if len(packet) < 14 or packet[0] >> 6 != 2 or packet[12] & 15 == 0 or packet[13] != 0x9c:
                self.packet_errors.append("Actual SBC transport produced malformed RTP/SBC framing")
            self.rtp_packets += 1
            self.rtp_bytes += len(packet)

    async def close(self):
        if self.active_handle:
            self.active_handle.cancel()
        if self.drain_task:
            self.drain_task.cancel()
            await asyncio.gather(self.drain_task, return_exceptions=True)
        for peer in (self.rtp, self.transport_fd):
            if peer:
                peer.close()


async def request(bus, destination, path, interface, member, signature="", body=None):
    reply = await _bounded(bus.call(Message(destination=destination, path=path, interface=interface,
                                           member=member, signature=signature, body=body or [])), 3)
    if reply.message_type != MessageType.METHOD_RETURN:
        raise RuntimeError(f"Private actual daemon rejected {interface}.{member}: {reply.error_name}")
    return reply


def validated_binary(path, expected):
    path = path.resolve(strict=True)
    info = path.stat()
    content = path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
            or info.st_mode & 0o6000 or len(expected) != 64 or digest != expected):
        raise RuntimeError("Actual test binary must match the independently recorded root-owned immutable digest")
    if b"STATE_DIRECTORY" not in content:
        raise RuntimeError("Actual binary must support private STATE_DIRECTORY; no global storage fallback")
    return path, digest


async def stop(process):
    if process is None:
        return True
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await _bounded(process.wait(), 3)
        except asyncio.TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await _bounded(process.wait(), 2)
    return process.returncode is not None


async def exercise(binary, expected):
    if (sys.platform != "linux" or os.geteuid() != 0 or os.environ.get("SHIRI_PRIVATE_BLUEALSA_TEST") != "1"):
        raise RuntimeError("Explicit SHIRI_PRIVATE_BLUEALSA_TEST=1 and Linux root are required for this private check")
    if Path("/sys/class/bluetooth/hci15").exists():
        raise RuntimeError("Synthetic hci15 must be absent; this check never uses a physical adapter")
    binary, digest = validated_binary(binary, expected)
    account = pwd.getpwnam("nobody")
    if account.pw_uid <= 0:
        raise RuntimeError("An existing unprivileged nobody account is required")
    daemon = bus_process = bus = observer = mock = admission = relay = None
    report = {"ok": False, "scope": "actual binary/private D-Bus/mock BlueZ/real SBC thread; no physical adapter or host bus",
              "binary_sha256": digest, "daemon_uid": account.pw_uid, "cases": [], "cleanup": {}}
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="shiri-private-bluealsa-", dir="/tmp") as temporary:
        directory = Path(temporary)
        os.chown(directory, 0, account.pw_gid)
        directory.chmod(0o710)
        state = directory / "state"
        state.mkdir(mode=0o700)
        os.chown(state, account.pw_uid, account.pw_gid)
        launcher = directory / "unix-only"
        cc = shutil.which("cc")
        if not cc:
            raise RuntimeError("A Linux C compiler is required for the isolated socket guard")
        # Bounded preflight compile finishes before any child/bus/audio starts.
        built = subprocess.run([cc, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",  # noqa: ASYNC221
                                str(ROOT / "tests/native/private_bluealsa_exec.c"), "-o", str(launcher)],
                               capture_output=True, text=True, timeout=30)
        if built.returncode:
            raise RuntimeError(built.stderr)
        launcher.chmod(0o755)
        config = directory / "bus.conf"
        bus_path = directory / "bus.sock"
        config.write_text('<busconfig><type>system</type><listen>unix:path=' + str(bus_path) +
                          '</listen><auth>EXTERNAL</auth><policy context="default"><allow user="*"/>'
                          '<allow own="*"/><allow send_destination="*"/><allow receive_sender="*"/>'
                          '</policy></busconfig>')
        config.chmod(0o600)
        address = "unix:path=" + str(bus_path)
        log_path = directory / "bluealsa.log"
        try:
            bus_process = await asyncio.create_subprocess_exec("dbus-daemon", "--nofork", "--nopidfile",
                                                              "--config-file=" + str(config),
                                                              stdout=asyncio.subprocess.DEVNULL,
                                                              stderr=asyncio.subprocess.DEVNULL)
            async def wait_socket():
                while not bus_path.exists():
                    if bus_process.returncode is not None:
                        raise RuntimeError("Private D-Bus daemon exited during startup")
                    await asyncio.sleep(0.01)
            await _bounded(wait_socket(), 3)
            bus = MessageBus(bus_address=address, negotiate_unix_fd=True)
            await _bounded(bus.connect(), 3)
            await _bounded(bus.request_name("org.bluez"), 3)
            mock = MockBlueZ(bus)
            bus.add_message_handler(mock.handle)
            environment = {"PATH": "/usr/bin:/bin", "DBUS_SYSTEM_BUS_ADDRESS": address,
                           "DBUS_SESSION_BUS_ADDRESS": address, "STATE_DIRECTORY": str(state), "TMPDIR": str(state)}
            with log_path.open("wb") as logfile:
                daemon = await asyncio.create_subprocess_exec(str(launcher), str(binary), "--device=hci15",
                                                              "--profile=a2dp-source", "--codec=SBC",
                                                              "--initial-volume=100", "--keep-alive=0",
                                                              "--disable-realtek-usb-fix",
                                                              env=environment, user=account.pw_uid,
                                                              group=account.pw_gid, extra_groups=[],
                                                              stdout=logfile, stderr=logfile)
            owner, app_path = await _bounded(mock.registered, 5)
            status = dict(line.split(":", 1) for line in Path(f"/proc/{daemon.pid}/status").read_text().splitlines()
                          if ":" in line)
            if (int(status["Uid"].split()[1]) != account.pw_uid or int(status["CapEff"].strip(), 16)
                    or status["NoNewPrivs"].strip() != "1" or status["Seccomp"].strip() != "2"
                    or Path(f"/proc/{daemon.pid}/exe").resolve(strict=True) != binary):
                raise RuntimeError("Actual daemon did not retain the exact unprivileged guarded exec identity")
            report["guard"] = {name: status[name].strip() for name in ("Uid", "Gid", "CapEff", "NoNewPrivs", "Seccomp")}
            objects = (await request(bus, owner, app_path, OBJECTS, "GetManagedObjects")).body[0]
            endpoints = [path for path, interfaces in objects.items()
                         if "org.bluez.MediaEndpoint1" in interfaces
                         and interfaces["org.bluez.MediaEndpoint1"].get("UUID", Variant("s", "")).value == SOURCE_UUID
                         and interfaces["org.bluez.MediaEndpoint1"].get("Codec", Variant("y", 255)).value == 0]
            if not endpoints:
                raise RuntimeError("Actual binary did not export an SBC source endpoint")
            endpoint = sorted(endpoints)[0]
            capabilities = b"\xff\xff\x02\x35"
            selected = await request(bus, owner, endpoint, "org.bluez.MediaEndpoint1", "SelectConfiguration", "ay", [capabilities])
            properties = mock.transport_properties() | {"Configuration": Variant("ay", selected.body[0])}
            await request(bus, owner, endpoint, "org.bluez.MediaEndpoint1", "SetConfiguration", "oa{sv}", [TRANSPORT, properties])
            async def factory():
                connection = MessageBus(bus_address=address, negotiate_unix_fd=True)
                scoped = DescriptorBus(connection)
                try:
                    await _bounded(connection.connect(), 3)
                    return scoped
                except BaseException:
                    await scoped.close()
                    raise
            manager = BlueALSA(transport_factory=factory, service_uid=account.pw_uid)
            admission = await manager.admit("bluealsa:DEV=" + MAC + ",PROFILE=a2dp", str(uuid4()), uuid4().hex)
            receipt = await admission.check()
            introspection = (await request(bus, admission.endpoint.owner, admission.endpoint.path,
                                           "org.freedesktop.DBus.Introspectable", "Introspect")).body[0]
            if "OpenRestricted" not in introspection or "RestrictedController" not in introspection:
                raise RuntimeError("Actual exported PCM interface lacks maintained restricted capabilities")
            report["caps"] = receipt["endpoint"]
            report["cases"].append("real maintained typed properties, interface, OpenRestricted and exact pipe/controller export")
            observer = socket.socket(fileno=os.dup(admission.descriptors[1]))
            observer.setblocking(False)
            for command in (b"Drain", b"D", b"Drop", b"Pause", b"Resume", b"DropSyncExtra", b"dropsync"):
                before = time.monotonic()
                await _bounded(asyncio.get_running_loop().sock_sendall(observer, command), 0.2)
                reply = await _bounded(asyncio.get_running_loop().sock_recv(observer, 32), 0.2)
                if reply != b"Invalid":
                    raise RuntimeError("Actual restricted controller admitted a forbidden command")
                report["cases"].append({"rejected": command.decode(), "latency_ms": (time.monotonic() - before) * 1000})
            observer.close()
            observer = None
            relay = PCMRelay(admission.endpoint, admission.descriptors)
            rate, channels = admission.endpoint.rate, admission.endpoint.channels
            if admission.endpoint.format_code != 0x8210:
                raise RuntimeError("Actual SBC source must supply the independently reviewed S16 PCM format")
            frames = rate // 100
            first = 0
            for sequence in range(1, 11):
                payload = b"".join(struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * (first + n) / rate))) * channels
                                   for n in range(frames))
                relay.enqueue(Frame(Operation.DATA, admission.room_id, admission.generation, str(uuid4()),
                                    1, sequence, time.monotonic_ns(), rate, 0x8210, channels, frames, first, payload))
                first += frames
                await asyncio.sleep(frames / rate)
            async def wait_rtp():
                while mock.rtp_packets < 2:
                    relay.require_healthy()
                    await asyncio.sleep(0.01)
            await _bounded(wait_rtp(), 2)
            if mock.packet_errors:
                raise RuntimeError(mock.packet_errors[0])
            before = time.monotonic()
            await relay.discard()
            elapsed = (time.monotonic() - before) * 1000
            if elapsed >= 200 or relay.queue or relay.pump and not relay.pump.done():
                raise RuntimeError("Actual DropSync did not retire the old worker pump within its bound")
            report["cases"].append({"completed_drop_sync_ms": elapsed, "actual_sbc_rtp_packets": mock.rtp_packets,
                                    "actual_sbc_rtp_bytes": mock.rtp_bytes})
            await admission.check()
            report["ok"] = True
        except Exception as exc:
            report["error"] = str(exc)
        finally:
            async def cleanup(name, awaitable):
                try:
                    await _bounded(awaitable, 5)
                    report["cleanup"][name] = True
                except BaseException as exc:
                    report["cleanup"][name] = False
                    report.setdefault("cleanup_errors", {})[name] = str(exc) or type(exc).__name__
            if observer:
                observer.close()
            if relay:
                await cleanup("worker_fds_closed", relay.close())
            else:
                report["cleanup"]["worker_fds_closed"] = True
            if admission:
                await cleanup("lease_released", admission.close())
                report["cleanup"]["lease_released"] &= not admission.manager.leases
            else:
                report["cleanup"]["lease_released"] = True
            await cleanup("daemon_stopped", stop(daemon))
            if mock:
                await cleanup("mock_transport_closed", mock.close())
            else:
                report["cleanup"]["mock_transport_closed"] = True
            if bus:
                transport = DescriptorBus(bus)
                await cleanup("bus_client_closed", transport.close())
            else:
                report["cleanup"]["bus_client_closed"] = True
            await cleanup("private_bus_stopped", stop(bus_process))
            report["daemon_log_tail"] = log_path.read_text(errors="replace")[-6000:] if log_path.exists() else ""
        report["elapsed_seconds"] = time.monotonic() - started
    report["cleanup"]["private_directory_removed"] = not directory.exists()
    report["ok"] = report["ok"] and all(report["cleanup"].values())
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    result = asyncio.run(exercise(args.binary, args.expected_sha256))
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["ok"] else 1)
