#!/usr/bin/env python3
"""Explicit supervised real encrypted AirPlay input/output lifecycle gate.

Actual OwnTone file sender -> actual zone Shairport -> production native
worker/OwnTone -> actual network AirPlay2 -> terminal Shairport decoded PCM.
Everything runs on a fresh disconnected virtual LAN, with no physical audio
devices or speakers. OwnTone sends type96/PTP. iPhone type103 and acoustics
remain separate manual acceptance boundaries. See the operator README.
"""
from __future__ import annotations

# Manual bounded kernel/file observations between awaits.
# ruff: noqa: ASYNC240, E402
import argparse
import asyncio
from contextlib import suppress
from datetime import datetime, timezone
from fractions import Fraction
import json
import logging
import os
from pathlib import Path
import secrets
import signal
import stat
import sys
import time
from uuid import uuid4
import wave

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

import numpy as np
from aiortc import AudioStreamTrack, RTCConfiguration, RTCPeerConnection, RTCRtpSender, RTCSessionDescription
from av import AudioFrame

from shiri.domain import Room, SpeakerRef
from shiri.runtime.audio import AudioWorker
from shiri.runtime.backend import OwnToneClient
from shiri.runtime.broker import Broker, REQUIRED_OWNTONE_VERSION
from shiri.runtime.configuration import quote, write_private
from shiri.runtime.identities import DaemonIdentities
from shiri.runtime.layout import directory, file_owner
from shiri.runtime.receiver_readiness import wait_receiver_ready
from shiri.runtime.system import Runner, RuntimeFailure, atomic_json, boot_id, root_directory
from shiri.runtime.units import Bind, VIEW
from shiri.settings import Settings

import isolated_group_lan as lan_module
from native_network_observer import NativeObserver
from native_network_profile import admit, digest, idle_installation, require, template

RATE = 48000
SOURCE_PORT = 3929                 # Exact supported owned slot6 HTTP port.
ROOM_SLOT, TERMINAL_SLOT = 7, 6
MAX_INNER_SECONDS = 330


def stamp():
    return datetime.now(timezone.utc).isoformat()


def speech_request(session_id, action, **fields):
    payload = {**fields, "session_id": session_id, "request_id": "network-"+uuid4().hex, "action": action}
    AudioWorker.identity(payload)
    return payload


def log_tail(path):
    """Read at most 8KiB from an exact regular log, without following links."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return {"read_error": "NonRegularFile"}
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell()-8192))
            return stream.read(8192).decode(errors="replace")
    except OSError as exc:
        return {"read_error": type(exc).__name__}


async def eventually(action, description, *, timeout=30, interval=0.1):
    end, last = time.monotonic()+timeout, None
    while time.monotonic() < end:
        try:
            value = await action()
            if value is not None and value is not False:
                return value
        except RuntimeFailure as exc:
            last = str(exc)
        await asyncio.sleep(interval)
    raise RuntimeFailure("Timed out waiting for " + description + (": " + last if last else ""))


async def host_snapshot(runner):
    links, addresses, routes, namespaces = await asyncio.gather(
        runner.json(["ip", "-j", "-d", "link", "show"]),
        runner.json(["ip", "-j", "addr", "show"]),
        runner.json(["ip", "-j", "route", "show", "table", "all"]),
        runner.run(["ip", "netns", "list"], timeout=5))
    return {"links": {v["ifname"]: [v["ifindex"], v.get("address"), v.get("ifalias"),
                                   v.get("linkinfo", {}).get("info_kind")] for v in links},
            "addresses": {v["ifname"]: sorted([a["family"], a["local"], a["prefixlen"]]
                                           for a in v.get("addr_info", [])) for v in addresses},
            "routes": sorted(routes, key=lambda v: json.dumps(v, sort_keys=True)),
            "namespaces": sorted(line.split()[0] for line in namespaces.stdout.splitlines() if line.split())}


class IsolatedBroker(Broker):
    def __init__(self, config, parent_namespace):
        node, current = (Path("/run/netns")/parent_namespace).stat(), Path("/proc/self/ns/net").stat()
        require((node.st_dev, node.st_ino) == (current.st_dev, current.st_ino),
                "Broker must run inside its exact supervised disconnected parent")
        self.parent_namespace = parent_namespace
        super().__init__(config)

    async def _start_process(self, *args, namespace=None, **kwargs):
        return await super()._start_process(*args, namespace=namespace or self.parent_namespace, **kwargs)


class SpeechTone(AudioStreamTrack):
    """Real paced WebRTC 880Hz voice; it does not inject native music PCM."""
    def __init__(self):
        super().__init__()
        self.samples, self.started, self.audible = 0, None, True

    async def recv(self):
        if self.started is None:
            self.started = time.monotonic()
        await asyncio.sleep(max(0, self.started+self.samples/RATE-time.monotonic()))
        frame = AudioFrame(format="s16", layout="mono", samples=960)
        positions = self.samples+np.arange(960)
        values = ((1500*np.sin(2*np.pi*880*positions/RATE)).astype("<i2")
                  if self.audible else np.zeros(960, dtype="<i2"))
        frame.planes[0].update(values.tobytes())
        frame.sample_rate, frame.pts, frame.time_base = RATE, self.samples, Fraction(1, RATE)
        self.samples += 960
        return frame


class Rig:
    def __init__(self, broker, root, interface):
        self.broker, self.root, self.interface = broker, root, interface
        self.room_id, self.terminal_id, self.source_id = (str(uuid4()) for _ in range(3))
        self.room_name = "Shiri network zone " + uuid4().hex[:8]
        self.terminal_name = "Shiri terminal output " + uuid4().hex[:8]
        self.terminal_record = self.observer = self.source_client = None
        self.terminal_processes, self.source_processes, self.peers = {}, {}, {}
        self.room = self.target = self.source_target = self.queue_id = None
        self.stable_units = None
        self.fixture_sender_reserved = False
        self.speech_cleanup_errors = []

    def identity(self, processes):
        # Unit invocation+cgroup ownership is authoritative; PID is diagnostic.
        return {name: {key: process.entry.get(key) for key in
                       ("unit", "invocation_id", "cgroup_inode", "boot_id", "pid")}
                for name, process in processes.items()}

    async def health(self):
        return await self.broker._worker_rpc(self.room, "audio", "health", {}, timeout=2)

    async def diagnostics(self):
        record = await self.broker.diagnostics(self.room_id)
        extra = {}
        for category, processes in [("terminal", self.terminal_processes),
                                    ("source", self.source_processes), ("sender", self.broker.sender_processes)]:
            extra[category] = {}
            for name, process in processes.items():
                extra[category][name] = log_tail(process.log_path)
        # OwnTone opens its configured logfile itself. A systemd journal socket
        # cannot be reopened through /dev/stdout, so retain both bounded logs.
        extra["source"]["owntone_file"] = log_tail(self.root/"source/state/owntone.log")
        record["fixture_logs"] = extra
        record["fixture_speech_cleanup_errors"] = self.speech_cleanup_errors[-8:]
        return record

    async def healthy(self, *, stable=True):
        health = await self.health()
        require(health.get("ready") is True and not health.get("error")
                and not health.get("native_ingress_fault") and not health.get("source", {}).get("error"),
                "Actual production native worker is not healthy")
        require(health.get("dropped_bytes") == 0, "Actual timed FIFO dropped PCM")
        require(self.room.status == "running" and self.room.selected_ids == [self.target["id"]],
                "Production room no longer owns only the exact terminal network output")
        require(all(p.alive for p in self.room.processes.values())
                and all(p.alive for p in self.terminal_processes.values())
                and all(p.alive for p in self.source_processes.values()), "A real transport unit exited")
        if stable and self.stable_units is not None:
            require(self.identity(self.room.processes) == self.stable_units,
                    "Normal playback unexpectedly rebuilt the production zone")
        require(not self.observer.errors, "Actual terminal capture protocol fault: " + repr(self.observer.errors))
        require(self.observer.task is not None and not self.observer.task.done(),
                "Actual terminal socket acceptor exited")
        return health

    async def start(self):
        definition = Room(id=self.room_id, slot=ROOM_SLOT, name=self.room_name,
                          airplay_name=self.room_name, interface=self.interface, enabled=True, volume=50)
        await self.broker.reconcile({"rooms": [definition.model_dump()]})

        async def room_ready():
            room = self.broker.rooms[self.room_id]
            return room if room.status == "running" and room.client and self.broker.sender else None
        self.room = await eventually(room_ready, "actual empty production room", timeout=60)
        await self.start_terminal()

        async def discover_terminal():
            outputs = await self.room.client.outputs({self.room_name})
            matches = [v for v in outputs if v["name"] == self.terminal_name]
            require(len(matches) <= 1, "Terminal receiver name is ambiguous")
            if not matches:
                return None
            target = matches[0]
            require(target["id"] == str(int(self.terminal_record["mac"].replace(":", ""), 16))
                    and target["protocol"] == "airplay2" and not target["requires_auth"],
                    "Output is not the exact fixture MAC-derived native AirPlay2 receiver")
            return target
        self.target = await eventually(discover_terminal, "actual terminal AirPlay2 discovery", timeout=40)
        selected = self.room.desired.model_copy(update={"revision": 2, "speakers": [SpeakerRef(
            id=self.target["id"], name=self.terminal_name, protocol="airplay2")]})
        await self.broker.reconcile({"rooms": [selected.model_dump()]})
        await eventually(lambda: self.room_selected(), "production room assigned network speaker", timeout=45)
        require(self.room.timing == (500, 600), "Actual route differs from production minimum AirPlay B500/H600")
        await self.start_source()
        self.stable_units = self.identity(self.room.processes)
        await self.healthy()

    async def room_selected(self):
        room = self.broker.rooms[self.room_id]
        self.room = room
        return room if room.status == "running" and room.selected_ids == [self.target["id"]] else None

    async def start_terminal(self):
        b, root = self.broker, self.root/"terminal"
        root_directory(root)
        definition = Room(id=self.terminal_id, slot=TERMINAL_SLOT, name=self.terminal_name,
                          airplay_name=self.terminal_name, interface=self.interface, enabled=True)
        self.terminal_record = await b.network.create_receiver(definition)
        receiver = b._account(self.terminal_id, "receiver", TERMINAL_SLOT)
        timing = b._account(self.terminal_id, "timing", TERMINAL_SLOT)
        root_directory(root/"config")
        directory(root/"input", {"uid": 0, "gid": receiver["gid"]}, mode=0o750)
        self.observer = NativeObserver(root/"input/music.sock", receiver["uid"], receiver["gid"])
        await self.observer.start()
        # Match production's explicit metadata FIFO. This upstream pipe-enabled
        # build initializes metadata even when enabled=no; omitting its path
        # would dereference NULL before the RTSP listener starts.
        metadata = root/"input/metadata.pipe"
        os.mkfifo(metadata, 0o600)
        file_owner(metadata, receiver, mode=0o600, fifo=True)
        config = root/"config/shairport.conf"
        write_private(config, f'''general = {{
  name = {quote(self.terminal_name)};
  service_type = "airplay2";
  interface = {quote(self.terminal_record["interface"])};
  port = 7000;
  output_backend = "shiri";
  mdns_backend = "avahi";
  interpolation = "soxr";
  ignore_volume_control = "yes";
  default_airplay_volume = 0.0;
  audio_backend_buffer_desired_length_in_seconds = 0.15;
}};
shiri = {{ socket = {quote(VIEW/"input/music.sock")}; peer_uid = 0;
  volume_socket = {quote(VIEW/"input/volume.sock")};
  output_rate = 48000; output_format = "S16_LE"; output_channels = 2; }};
metadata = {{ enabled = "yes"; include_cover_art = "no";
  pipe_name = {quote(VIEW/"input/metadata.pipe")}; pipe_timeout = 100; }};
''')
        file_owner(config, receiver)
        self.terminal_processes.update(await b._start_namespace_services(
            self.terminal_id, self.terminal_record, root, slot=TERMINAL_SLOT))
        shm = directory(root/"shm", timing, mode=0o755)
        self.terminal_processes["nqptp"] = await b._start_process(
            f"{self.terminal_id}:nqptp", "nqptp", [b.binary("nqptp"), "-v"], root,
            account=timing, namespace=self.terminal_record["namespace"], ptp=True,
            binds=[Bind(str(shm), "/dev/shm", True)])
        await asyncio.sleep(0.25)
        self.terminal_processes["shairport"] = await b._start_process(
            f"{self.terminal_id}:shairport", "shairport",
            [b.binary("shairport-sync"), "-v", "-c", str(VIEW/"config/shairport.conf")], root,
            account=receiver, namespace=self.terminal_record["namespace"], bus=True,
            binds=[Bind(str(config), str(VIEW/"config/shairport.conf")),
                   Bind(str(root/"input"), str(VIEW/"input")),
                   Bind(str(root/"discovery/bus"), str(VIEW/"bus")), Bind(str(shm), "/dev/shm")])
        await wait_receiver_ready(self.terminal_processes["shairport"], self.terminal_record, receiver)
        await b._refresh_processes(self.terminal_id, self.terminal_processes)

    async def start_source(self):
        b, root, sender = self.broker, self.root/"source", self.broker.sender
        # The extra real sender fixture owns a reference independently of the
        # production zone. Paired recovery must keep its namespace/PTP/bus
        # alive, just as it does for a healthy second production zone.
        b.sender_users.add(self.source_id)
        self.fixture_sender_reserved = True
        root_directory(root)
        output = b._account(self.source_id, "output", TERMINAL_SLOT)
        root_directory(root/"config")
        directory(root/"state", output)
        directory(root/"state/cache", output)
        directory(root/"media", {"uid": 0, "gid": output["gid"]}, mode=0o750)
        wav = root/"media/probe-440.wav"
        values = (8192*np.sin(2*np.pi*440*np.arange(RATE)/RATE)).astype("<i2")
        second = np.column_stack((values, values)).astype("<i2").tobytes()
        with wave.open(str(wav), "wb") as stream:
            stream.setnchannels(2)
            stream.setsampwidth(2)
            stream.setframerate(RATE)
            for _ in range(180):
                stream.writeframesraw(second)
        file_owner(wav, output)
        password = secrets.token_urlsafe(32)
        config = root/"config/owntone.conf"
        write_private(config, f'''general {{
  uid = {quote(output["name"])}
  db_path = {quote(VIEW/"state/songs.db")}
  cache_dir = {quote(VIEW/"state/cache")}
  logfile = {quote(VIEW/"state/owntone.log")}
  loglevel = debug
  admin_password = {quote(password)}
  trusted_networks = {{ {quote(sender["api_host_ip"]+"/32")}, {quote(sender["api_ip"]+"/32")} }}
  websocket_port = 0
  ipv6 = no
  speaker_autoselect = no
  high_resolution_clock = yes
  start_buffer_ms = 500
}}
library {{ name = "Shiri network source fixture" port = {SOURCE_PORT}
  directories = {{ {quote(VIEW/"media")} }} follow_symlinks = false pipe_autostart = false }}
audio {{ type = "disabled" }}
mpd {{ port = 0 http_port = 0 }}
airplay {quote(self.room_name)} {{ airplay2_disable = false reconnect = false }}
airplay {quote(self.terminal_name)} {{ exclude = true }}
''')
        file_owner(config, output)
        self.source_processes["owntone"] = await b._start_process(
            f"{self.source_id}:owntone", "owntone",
            [b.binary("owntone"), "-f", "-c", str(VIEW/"config/owntone.conf"),
             "--mdns-no-rsp", "--mdns-no-daap", "--mdns-no-web", "--mdns-no-cname"], root,
            account=output, namespace=sender["namespace"], bus=True, listen_port=SOURCE_PORT,
            binds=[Bind(str(config), str(VIEW/"config/owntone.conf")),
                   Bind(str(root/"state"), str(VIEW/"state"), True),
                   Bind(str(root/"media"), str(VIEW/"media")),
                   Bind(str(b.config.runtime_state_dir/"sender/discovery/bus"), str(VIEW/"bus")),
                   Bind(str(b.config.runtime_state_dir/"sender/shm"), "/dev/shm")])
        await b._refresh_processes(self.source_id, self.source_processes)
        self.source_client = OwnToneClient(f'http://{sender["api_ip"]}:{SOURCE_PORT}', password=password)

        async def ready():
            return await self.source_client.request("GET", "/api/player")
        await eventually(ready, "actual source OwnTone HTTP", timeout=30)

        async def discover():
            outputs = await self.source_client.outputs(set())
            matches = [v for v in outputs if v["name"] == self.room_name]
            require(len(matches) <= 1, "Input receiver is ambiguous")
            if not matches:
                return None
            v = matches[0]
            require(v["id"] == str(int(self.room.receiver["mac"].replace(":", ""), 16))
                    and v["protocol"] == "airplay2" and not v["requires_auth"],
                    "Source did not discover the exact room AP2 receiver")
            return v
        self.source_target = await eventually(discover, "actual zone AirPlay2 discovery", timeout=40)
        await self.select_source()
        await self.source_client.volume(50)

        async def library():
            value = await self.source_client.request("GET", "/api/library")
            require(value.get("songs", 0) <= 1, "Source scanned unintended media")
            return value if value.get("songs") == 1 and not value.get("updating") else None
        await eventually(library, "private generated WAV scan", timeout=35)

    async def select_source(self):
        await self.source_client.select([SpeakerRef(id=self.source_target["id"], name=self.room_name,
                                                   protocol="airplay2")],
                                        await self.source_client.outputs(set()))

    async def queue(self):
        result = await self.source_client.request("POST", "/api/queue/items/add", params={
            "expression": "media_kind is music", "limit": "1", "playback": "start", "clear": "true"})
        require(result.get("count") == 1 and len(result.get("items", [])) == 1
                and result["items"][0].get("path") == str(VIEW/"media/probe-440.wav"),
                "Source did not queue exactly its generated private tone")
        self.queue_id = result["items"][0]["id"]
        return result

    async def actual_music(self, label, *, timeout=15, since_ns=None):
        since_ns = time.monotonic_ns() if since_ns is None else since_ns
        started = time.monotonic_ns()
        async def observe():
            await self.healthy()
            players = await asyncio.gather(self.source_client.request("GET", "/api/player"),
                                          self.room.client.request("GET", "/api/player"))
            spectrum = self.observer.registry.spectrum(since_ns=since_ns)
            if spectrum and spectrum["music_440_amplitude"] > 20 and all(p.get("state") == "play" for p in players):
                require(players[0].get("item_id") == self.queue_id,
                        "Actual source changed from its exact queued synthetic item")
                source_outputs, zone_outputs = await asyncio.gather(
                    self.source_client.outputs(set()), self.room.client.outputs({self.room_name}))
                require({v["id"] for v in source_outputs if v["selected"]} == {self.source_target["id"]}
                        and {v["id"] for v in zone_outputs if v["selected"]} == {self.target["id"]},
                        "Actual network transport selected an unintended output")
                require(spectrum["minimum_presentation_lead_ns"] > 0,
                        "Actual terminal network PCM missed its original presentation deadline")
                return {"spectrum": spectrum, "elapsed_ms": (time.monotonic_ns()-started)/1e6,
                        "source_progress_ms": players[0].get("item_progress_ms"),
                        "zone_progress_ms": players[1].get("item_progress_ms")}
            return None
        return await eventually(observe, label, timeout=timeout)

    async def close_speech(self, session, peer):
        errors = []
        try:
            try:
                result = await self.broker.speech(self.room, speech_request(session, "close"))
                require(result.get("ok") is True, "Speech worker did not acknowledge exact close")
            except Exception as exc:
                errors.append(f"worker {type(exc).__name__}: {exc}")
        finally:
            try:
                await peer.close()
            except Exception as exc:
                errors.append(f"peer {type(exc).__name__}: {exc}")
        if errors:
            # Retain the exact peer for the final cleanup retry. A rejected
            # close must never turn into a successful fixture receipt.
            failure = "Speech fixture close failed: "+"; ".join(errors)
            self.speech_cleanup_errors.append({"session_id": session, "error": failure})
            self.speech_cleanup_errors[:] = self.speech_cleanup_errors[-8:]
            raise RuntimeFailure(failure)
        self.peers.pop(session, None)

    async def speech(self, *, label, paused=False, baseline=None):
        peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        tone, session = SpeechTone(), "network-"+uuid4().hex
        transceiver = peer.addTransceiver(tone, direction="sendonly")
        codecs = [v for v in RTCRtpSender.getCapabilities("audio").codecs if v.mimeType.lower() == "audio/opus"]
        require(bool(codecs), "Real speech peer has no Opus codec")
        transceiver.setCodecPreferences(codecs)
        self.peers[session] = peer
        before = await self.health()
        source_before = await self.source_client.request("GET", "/api/player")
        try:
            await peer.setLocalDescription(await peer.createOffer())
            answer = await self.broker.speech(self.room, speech_request(session, "offer",
                        sdp=peer.localDescription.sdp, type=peer.localDescription.type))
            await peer.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))
            start = time.monotonic_ns()
            async def voice():
                await self.healthy()
                spectrum = self.observer.registry.spectrum(since_ns=start)
                return spectrum if spectrum and spectrum["speech_880_amplitude"] > 20 else None
            spectrum = await eventually(voice, "actual decoded "+label, timeout=15)
            # Get at least one full second after startup to measure steady
            # ducking rather than a single initial packet or play flag.
            await asyncio.sleep(1.5)
            spectrum = self.observer.registry.spectrum(since_ns=time.monotonic_ns()-1_000_000_000)
            require(spectrum is not None and spectrum["speech_880_amplitude"] > 20,
                    "Terminal network did not retain actual voice audio")
            require(spectrum["minimum_presentation_lead_ns"] > 0,
                    "Actual terminal speech PCM missed its original presentation deadline")
            after = await self.healthy()
            source_after = await self.source_client.request("GET", "/api/player")
            require(before["source"]["owner"] == after["source"]["owner"]
                    and before.get("native_generation") == after.get("native_generation"),
                    "Speech changed the original music owner/generation")
            require(source_after.get("item_id") == source_before.get("item_id")
                    and source_after.get("state") == source_before.get("state"),
                    "Speech paused, resumed or replaced upstream music")
            if paused:
                require(spectrum["music_440_amplitude"] < 10
                        and after.get("native_blocks") == before.get("native_blocks"),
                        "Paused voice consumed or fabricated music PCM")
            else:
                require(baseline is not None and spectrum["music_440_amplitude"] > 5
                        and 0.12 < spectrum["music_440_amplitude"]/baseline < 0.55,
                        "Actual music was not continuously ducked under voice")
                require(source_after.get("item_progress_ms", 0) > source_before.get("item_progress_ms", 0)+500,
                        "Upstream source progress did not advance during speech")
            return {"spectrum": spectrum, "source_owner_preserved": True,
                    "source_state": source_after.get("state"), "native_blocks_before": before.get("native_blocks"),
                    "native_blocks_after": after.get("native_blocks"), "paused": paused}
        finally:
            tone.audible = False
            primary = sys.exc_info()[1]
            try:
                await self.close_speech(session, peer)
            except Exception as exc:
                if primary is not None:
                    raise primary from exc
                raise

    async def release(self):
        await self.source_client.request("PUT", "/api/player/stop")
        await self.source_client.select([], await self.source_client.outputs(set()))
        # Selection is deliberately retired for the cold fixture. Restoring it
        # uses the production selection path; no UI volume wake is involved.
        await self.room.client.select([], await self.room.client.outputs({self.room_name}))
        async def released():
            health = await self.health()
            return health if health["source"]["owner"] is None and self.observer.registry.current is None else None
        await eventually(released, "actual input END and terminal transport release", timeout=20)
        await self.broker._restore_outputs(self.room)
        await self.select_source()

    async def close(self):
        errors = []
        for session, peer in list(self.peers.items()):
            try:
                await self.close_speech(session, peer)
            except Exception as exc:
                errors.append(f"speech {session}: {type(exc).__name__}: {exc}")
        if self.source_client:
            await self.source_client.close()
        for owner, processes in [(self.source_id, self.source_processes),
                                 (self.terminal_id, self.terminal_processes)]:
            for name, process in reversed(list(processes.items())):
                try:
                    await process.stop()
                    self.broker.network.forget_process(f"{owner}:{name}")
                    processes.pop(name)
                except Exception as exc:
                    errors.append(f"{owner}:{name}: {type(exc).__name__}: {exc}")
            # Include partial launches durably registered before a handle was
            # returned. Never remove a namespace with an unresolved unit.
            try:
                await self.broker._stop_reserved_units(owner)
            except Exception as exc:
                errors.append(f"reserved {owner}: {type(exc).__name__}: {exc}")
        if self.terminal_record and not self.terminal_processes and not any(
                key.startswith(self.terminal_id+":") for key in self.broker.network.manifest["processes"]):
            try:
                await self.broker.network.remove("receiver:"+self.terminal_id)
            except Exception as exc:
                errors.append("terminal network: "+str(exc))
        if self.fixture_sender_reserved and not self.source_processes and not any(
                key.startswith(self.source_id+":") for key in self.broker.network.manifest["processes"]):
            self.broker.sender_users.discard(self.source_id)
            self.fixture_sender_reserved = False
        if self.observer:
            try:
                await self.observer.close()
            except Exception as exc:
                errors.append("terminal observer: "+str(exc))
        if errors:
            raise RuntimeFailure("Fixture exact cleanup remains pending: "+"; ".join(errors))


async def exercise(rig, report, *, fault_case):
    began = time.monotonic_ns()
    await rig.queue()
    report["cold_start"] = await rig.actual_music("first actual decoded cold music", since_ns=began)
    await asyncio.sleep(2)
    baseline = rig.observer.registry.spectrum(since_ns=time.monotonic_ns()-1_000_000_000)
    require(baseline and baseline["music_440_amplitude"] > 20, "No steady actual music baseline")
    report["baseline"] = baseline
    report["playing_speech"] = await rig.speech(label="music plus speech", baseline=baseline["music_440_amplitude"])
    await asyncio.sleep(2)
    restored = await rig.actual_music("music gain recovery after speech")
    require(0.7 < restored["spectrum"]["music_440_amplitude"]/baseline["music_440_amplitude"] < 1.4,
            "Actual terminal music gain did not recover after voice")
    report["music_restored"] = restored

    # OwnTone source volume goes through actual AirPlay SET_PARAMETER, native
    # receipt and production room master; no direct worker volume injection.
    await rig.source_client.volume(30)
    async def volume_seen():
        health = await rig.healthy()
        player = await rig.room.client.request("GET", "/api/player")
        return health if rig.room.current_volume == 30 and player.get("volume") == 30 else None
    await eventually(volume_seen, "actual AirPlay source volume reflected by room", timeout=15)
    report["airplay_volume"] = {"requested": 30, "room": rig.room.current_volume,
                                "native_events": (await rig.health()).get("native_volume_events")}
    source_before_reverse = await rig.source_client.request("GET", "/api/player")
    owner_before_reverse = (await rig.health())["source"]["owner"]
    await rig.broker.set_volume(rig.room, 60)
    async def reverse_seen():
        health = await rig.healthy()
        receipt = health.get("receiver_volume", {})
        return receipt if (receipt.get("status") == "delivered" and receipt.get("volume") == 60
                           and receipt.get("revision") == rig.room.desired.revision
                           and receipt.get("sender_notified") is True and rig.room.current_volume == 60) else None
    reverse = await eventually(reverse_seen, "actual encrypted AP2 volume event acknowledgement", timeout=15)
    source_after_reverse = await rig.source_client.request("GET", "/api/player")
    require(source_after_reverse.get("state") == source_before_reverse.get("state") == "play"
            and source_after_reverse.get("item_id") == source_before_reverse.get("item_id")
            and (await rig.health())["source"]["owner"] == owner_before_reverse,
            "Reverse event delivery changed music ownership or playback")
    report["reverse_airplay_volume"] = {"room": 60, "actual_receiver_ack": reverse,
        "source_observed_volume_before": source_before_reverse.get("volume"),
        "source_observed_volume_after": source_after_reverse.get("volume"),
        "event_delivery_ack_only": True, "sender_application_verified": False,
        "source_limitation": "Pinned OwnTone airplay_events.c acknowledges unknown dvlc with 200 but does not apply device-volume",
        "iphone_slider_acceptance": "manual_pending"}
    await rig.source_client.volume(50)
    async def volume_restored():
        await rig.healthy()
        return rig.room.current_volume == 50
    await eventually(volume_restored, "actual room volume restored", timeout=15)

    await rig.source_client.request("PUT", "/api/player/pause")
    await asyncio.sleep(2)
    paused = await rig.healthy()
    owner = paused["source"]["owner"]
    blocks = paused.get("native_blocks")
    pause_started = time.monotonic_ns()
    for _ in range(31):
        await asyncio.sleep(1)
        health = await rig.healthy()
        player = await rig.source_client.request("GET", "/api/player")
        require(player.get("state") == "pause", "Source did not remain paused for31s")
        require(health["source"]["owner"] == owner and health.get("native_blocks") == blocks,
                "Paused native source changed ownership or generated music PCM")
    report["long_pause"] = {"duration_ms": (time.monotonic_ns()-pause_started)/1e6,
                            "nonzero_music_owner_preserved": owner is not None, "native_blocks": blocks,
                            "same_zone_units": True}
    report["paused_speech"] = await rig.speech(label="speech after31s pause", paused=True)
    resumed = time.monotonic_ns()
    await rig.source_client.request("PUT", "/api/player/play")
    report["resume"] = await rig.actual_music("actual pause resume without volume change", since_ns=resumed)
    before_flushes = rig.observer.registry.flushes
    seeked = time.monotonic_ns()
    await rig.source_client.request("PUT", "/api/player/seek", params={"seek_ms": "1000"})
    report["seek_flush"] = await rig.actual_music("actual seek/FLUSH recovery", since_ns=seeked)
    report["seek_flush"]["terminal_flushes_before"] = before_flushes
    report["seek_flush"]["terminal_flushes_after"] = rig.observer.registry.flushes

    await rig.release()
    began = time.monotonic_ns()
    await rig.queue()
    report["cold_reconnect"] = await rig.actual_music("second actual cold network reconnect", since_ns=began)
    await rig.healthy()
    report["normal_zone_units"] = rig.stable_units
    await rig.release()
    report["cold_idle_speech"] = await rig.speech(label="cold idle speech without a phone", paused=True)

    if fault_case:
        await rig.release()
        fixed = {"terminal": rig.identity(rig.terminal_processes), "source": rig.identity(rig.source_processes)}
        previous = rig.identity(rig.room.processes)
        original_incarnation = (await rig.health())["source"]["incarnation"]
        rig.observer.hold_next_begin_seconds = 4.5
        fault_started = time.monotonic_ns()
        # Observe the native3s failure while the actual source HTTP request is
        # still pending. Waiting for its separate4s client timeout first would
        # lose the evidence that the production actor fenced before GRANT.
        queue_task = asyncio.create_task(rig.queue())
        queue_error = None
        failures = []
        deadline_failure = None
        async def recovered():
            nonlocal deadline_failure
            room = rig.broker.rooms[rig.room_id]
            rig.room = room
            if room.status != "running" or rig.identity(room.processes) == previous:
                if rig.identity(room.processes) == previous and "audio" in room.processes:
                    with suppress(Exception):
                        unhealthy = await rig.health()
                        if unhealthy.get("ready") is False and unhealthy.get("source", {}).get("error"):
                            if deadline_failure is None:
                                deadline_failure = {"at_ns": time.monotonic_ns(),
                                                    "source_error": unhealthy["source"]["error"],
                                                    "source_operation_generation": unhealthy.get("source_operation_generation")}
                if room.error:
                    failures.append({"at_ns": time.monotonic_ns(), "status": room.status, "error": room.error})
                return None
            health = await rig.health()
            if health.get("ready") and health["source"]["incarnation"] != original_incarnation:
                return health
            return None
        try:
            fresh = await eventually(recovered, "actual bounded paired recovery from held network SETUP", timeout=45)
        finally:
            if not queue_task.done():
                queue_task.cancel()
            queued_results = await asyncio.gather(queue_task, return_exceptions=True)
            if isinstance(queued_results[0], BaseException):
                queue_error = type(queued_results[0]).__name__
        require(rig.observer.held_begins and rig.observer.hold_next_begin_seconds == 0,
                "Fault fixture did not hold an actual terminal native SETUP admission")
        hold = rig.observer.held_begins[-1]
        require(hold.get("finished_ns", 0)-hold["started_ns"] >= 4_000_000_000,
                "Terminal lost-reply fixture did not span the native3s deadline")
        require(deadline_failure is not None and 0 < deadline_failure["at_ns"]-hold["started_ns"] < 4_000_000_000,
                "Actual source admission did not fail closed within its native deadline before the held reply")
        require(fixed == {"terminal": rig.identity(rig.terminal_processes), "source": rig.identity(rig.source_processes)},
                "Failed route recovery rebuilt unrelated fixture units")
        # A new source attempt must use the fresh production worker/token and
        # must remain healthy after the old real network reply was released.
        rig.stable_units = rig.identity(rig.room.processes)
        await rig.select_source()
        began = time.monotonic_ns()
        await rig.queue()
        retry = await rig.actual_music("fresh native owner after actual setup failure", since_ns=began, timeout=20)
        await asyncio.sleep(2)
        await rig.healthy()
        report["held_setup_reply"] = {"hold": hold, "queue_error_type": queue_error,
            "recovery_elapsed_ms": (time.monotonic_ns()-fault_started)/1e6,
            "old_zone_units": previous, "fresh_zone_units": rig.stable_units,
            "fresh_incarnation": fresh["source"]["incarnation"], "observed_failures": failures[-32:],
            "native_deadline_failure": deadline_failure,
            "unrelated_fixture_units_preserved": True, "retry": retry}


async def inner(profile_path, *, parent_namespace, original_netns_fd, run_directory, fault_case):
    profile = admit(profile_path)
    root, report = Path(run_directory), {"started_at": stamp(), "passed": False, "scope": "real encrypted AirPlay2 type96/PTP",
        "physical_speaker_verified": False, "iphone_type103_verified": False, "rootless_api_verified": False,
        "cleanup": {}, "profile": str(profile_path), "boot_id": boot_id()}
    require(root.parent == Path(profile["work"]) and root.is_dir(), "Inner gate is outside its exact private run directory")
    digest(root/"supervisor-result.json")
    supervisor = json.loads((root/"supervisor-result.json").read_text())
    current = Path("/proc/self/ns/net").stat()
    require(supervisor.get("namespace") == parent_namespace and supervisor.get("run_directory") == str(root)
            and supervisor.get("boot_id") == boot_id() and supervisor.get("installation_id") == profile["installation_id"]
            and supervisor.get("profile_sha256") == digest(profile_path)
            and supervisor.get("namespace_identity") == [current.st_dev, current.st_ino],
            "Inner gate does not match its exact root supervisor receipt")
    lan, broker, rig, errors = lan_module.IsolatedLan(root/"lan"), None, None, []
    try:
        await lan.start(original_netns_fd=original_netns_fd, parent_namespace=parent_namespace)
        root_directory(root/"api")
        root_directory(root/"credentials")
        token = root/"credentials/api-token"
        descriptor = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(secrets.token_urlsafe(32))
        settings = Settings(state_dir=root/"api", runtime_state_dir=Path(profile["runtime_state_dir"]),
                            runtime_dir=Path(profile["runtime_dir"]), runtime_socket=Path(profile["runtime_dir"])/"runtime.sock",
                            api_token_file=token, binary_dir=Path(profile["binary_dir"]),
                            daemon_identity_file=Path(profile["identity_file"]))
        broker = IsolatedBroker(settings, parent_namespace)
        await broker.start(serve=False)
        require(broker.ready, broker.error or "Actual production Broker did not become ready")
        require(REQUIRED_OWNTONE_VERSION.endswith("-balance1-transition1-bed1"),
                "Gate cannot run an earlier production version contract")
        report["versions"] = broker.versions
        rig = Rig(broker, root/"rig", lan.interface)
        root_directory(rig.root)
        await rig.start()
        report["route"] = {"zone_receiver": rig.room.receiver, "terminal_receiver": rig.terminal_record,
                           "source_target": rig.source_target, "terminal_output": rig.target,
                           "production_timing": list(rig.room.timing), "terminal_fixture_peer_uid": 0}
        await exercise(rig, report, fault_case=fault_case)
        report["capture"] = rig.observer.registry.snapshot()
        report["terminal_protocol_errors"] = list(rig.observer.errors)
        require(rig.observer.control_binds > 0, "Final receiver terminal did not bind its authenticated volume1 control lane")
        report["terminal_fixture_control_binds"] = rig.observer.control_binds
        report["actual_owned_units"] = {"zone": rig.identity(rig.room.processes),
                                        "terminal": rig.identity(rig.terminal_processes),
                                        "source": rig.identity(rig.source_processes),
                                        "sender": rig.identity(broker.sender_processes)}
        require(all("/dev/snd/" not in p.entry.get("properties", {}).get("DeviceAllow", "")
                    for p in [*rig.room.processes.values(), *rig.terminal_processes.values(),
                              *rig.source_processes.values(), *broker.sender_processes.values()]),
                "Real network gate unexpectedly granted a physical audio device")
        atomic_json(root/"successful-diagnostics.json", await rig.diagnostics())
        report["healthy_succeeded"] = True
    except BaseException as exc:
        report["failure"] = {"type": type(exc).__name__, "message": str(exc)}
        if broker:
            with suppress(Exception):
                atomic_json(root/"failure-diagnostics.json", await rig.diagnostics() if rig else await broker.diagnostics())
    finally:
        if rig:
            with suppress(Exception):
                report["capture_final"] = rig.observer.registry.snapshot() if rig.observer else None
            try:
                await rig.close()
                report["cleanup"]["fixture_units_stopped"] = True
            except Exception as exc:
                errors.append("fixture: "+str(exc))
        if broker:
            try:
                await broker.close()
                report["cleanup"]["production_units_stopped"] = True
            except Exception as exc:
                errors.append("broker: "+str(exc))
        try:
            idle_installation(DaemonIdentities(Path(profile["identity_file"])).load())
            await lan.close()
            report["cleanup"]["owned_lan_removed"] = True
        except Exception as exc:
            errors.append("LAN: "+str(exc))
        try:
            admit(profile_path)
            report["cleanup"]["exact_installation_map_and_build_preserved"] = True
        except Exception as exc:
            errors.append("admission recheck: "+str(exc))
        report.update(cleanup_errors=errors, finished_at=stamp(), passed=report.get("healthy_succeeded") is True and not errors)
        atomic_json(root/"inner-result.json", report)
    return 0 if report["passed"] else 1


async def stop_child(process):
    if process.returncode is not None:
        return
    with suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), 20)
    except asyncio.TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        await asyncio.wait_for(process.wait(), 5)


async def supervise(profile_path, *, fault_case):
    profile = admit(profile_path)
    runner, child, log, held, original, inode, baseline = Runner(), None, None, None, None, None, None
    name = "shiri_group_run_"+uuid4().hex[:8]
    root = Path(profile["work"])/("network-"+uuid4().hex)
    node, errors = Path("/run/netns")/name, []
    report = {"started_at": stamp(), "passed": False, "boot_id": boot_id(), "namespace": name,
              "installation_id": profile["installation_id"], "run_directory": str(root),
              "profile_sha256": digest(profile_path), "cleanup": {}}
    try:
        root_directory(root.parent)
        root.mkdir(mode=0o700)
        baseline = await host_snapshot(runner)
        original = os.open("/proc/self/ns/net", os.O_RDONLY | os.O_CLOEXEC)
        original_identity = lan_module.namespace_identity(original)
        require(not node.exists(), "Fresh supervisor namespace name unexpectedly exists")
        atomic_json(root/"supervisor-result.json", report)
        await runner.run(["ip", "netns", "add", name], timeout=5)
        held = os.open(node, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        inode = lan_module.namespace_identity(held)
        require(inode != original_identity, "Supervisor parent is the original host namespace")
        report["namespace_identity"] = list(inode)
        report["original_namespace_identity"] = list(original_identity)
        atomic_json(root/"supervisor-result.json", report)
        await runner.run(["ip", "netns", "exec", name, "ip", "link", "set", "lo", "up"], timeout=5)
        log = (root/"inner.log").open("xb")
        child = await asyncio.create_subprocess_exec("/usr/bin/nsenter", f"--net=/proc/self/fd/{held}", "--",
            sys.executable, str(Path(__file__).resolve()), "--profile", str(profile_path),
            "--inner", "--parent-namespace", name, "--original-netns-fd", str(original),
            "--run-directory", str(root), *( ["--fault-case"] if fault_case else []),
            env={**os.environ, "PYTHONPATH": str(PROJECT), "PYTHONDONTWRITEBYTECODE": "1"},
            stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=asyncio.subprocess.STDOUT,
            pass_fds=(held, original))
        await asyncio.wait_for(child.wait(), MAX_INNER_SECONDS)
        result = json.loads((root/"inner-result.json").read_text())
        require(result["started_at"] >= report["started_at"] and result["boot_id"] == report["boot_id"],
                "Inner gate receipt is stale or from another boot")
        report["inner_report"] = str(root/"inner-result.json")
        report["inner_passed"] = result.get("passed") is True and child.returncode == 0
        report["inner_failure"] = result.get("failure")
    except BaseException as exc:
        report["failure"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        if child and child.returncode is None:
            try:
                await stop_child(child)
            except Exception as exc:
                errors.append("exact direct child: "+str(exc))
        if log:
            log.close()
        if inode:
            try:
                admit(profile_path)
                require(boot_id() == report["boot_id"] and lan_module.namespace_identity(held) == inode,
                        "Parent boot/held namespace changed before cleanup")
                current = node.stat()
                require((current.st_dev, current.st_ino) == inode, "Named parent namespace was replaced")
                pids = await runner.run(["ip", "netns", "pids", name], timeout=5)
                links = await runner.json(["ip", "netns", "exec", name, "ip", "-j", "link"])
                require(not pids.stdout.strip() and all(v["ifname"] == "lo" for v in links),
                        "Parent still owns PIDs or links; preserve it for exact recovery")
                await runner.run(["ip", "netns", "delete", name], timeout=5)
                report["cleanup"]["exact_parent_removed"] = True
            except Exception as exc:
                errors.append("parent: "+str(exc))
        for descriptor in (held, original):
            if descriptor is not None:
                os.close(descriptor)
        if baseline is not None:
            try:
                require(await host_snapshot(runner) == baseline, "Original host network changed")
                report["cleanup"]["original_host_network_preserved"] = True
            except Exception as exc:
                errors.append("original host: "+str(exc))
        report.update(cleanup_errors=errors, finished_at=stamp(),
                      passed=report.get("inner_passed") is True and not errors
                      and report["cleanup"].get("exact_parent_removed") is True
                      and report["cleanup"].get("original_host_network_preserved") is True)
        if root.is_dir():
            atomic_json(root/"supervisor-result.json", report)
        print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--print-profile", action="store_true")
    parser.add_argument("--binary-dir", type=Path)
    parser.add_argument("--identity-file", type=Path)
    parser.add_argument("--work", type=Path)
    parser.add_argument("--fault-case", action="store_true")
    parser.add_argument("--inner", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--parent-namespace", help=argparse.SUPPRESS)
    parser.add_argument("--original-netns-fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--run-directory", help=argparse.SUPPRESS)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.print_profile:
        require(all((args.binary_dir, args.identity_file, args.work)), "Profile template needs native prefix, immutable map and private work")
        print(json.dumps(template(project=PROJECT, binaries=args.binary_dir, identity_file=args.identity_file, work=args.work), indent=2))
        return 0
    require(args.profile is not None, "Run only with an independently reviewed boot-bound root profile")
    async def run():
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, task.cancel)
        if args.inner:
            require(args.parent_namespace and args.original_netns_fd is not None and args.run_directory,
                    "Inner gate requires the exact supervisor-held parent and host namespace")
            return await inner(args.profile, parent_namespace=args.parent_namespace,
                               original_netns_fd=args.original_netns_fd, run_directory=args.run_directory,
                               fault_case=args.fault_case)
        return await supervise(args.profile, fault_case=args.fault_case)
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
