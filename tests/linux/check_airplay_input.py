#!/usr/bin/env python3
"""Synthetic AirPlay 2 input check, prepared locally; run explicitly on Linux.

OwnTone source -> candidate Shairport -> actual Loopback slot7 -> real mixer
FIFO. No house output is assigned. This is not a stock-iPhone, native grouping,
Cast input or physical-speaker acceptance test. Credentials remain private.

Use the candidate Python environment with this checkout installed/importable:
  sudo /opt/shiri-v2/.venv/bin/python /opt/shiri-v2/tests/linux/check_airplay_input.py
"""

import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import wave

import numpy as np

from shiri.domain import Room, SpeakerRef
from shiri.rpc import call_rpc
from shiri.runtime.backend import OwnToneClient
from shiri.runtime.broker import Broker
from shiri.runtime.configuration import isolated_command, quote, write_private
from shiri.runtime.system import Runner, RuntimeFailure, atomic_json, root_directory
from shiri.settings import Settings

STATE = Path("/var/lib/shiri-v2-test-runtime")
RUN = Path("/run/shiri-v2-test")
BINARIES = Path("/opt/shiri-v2-deps")
RESULT = Path("/tmp/shiri-v2-airplay-result.json")
ROOM_ID = "b6786543-7eb2-443d-83b1-65b984123a76"
NAME = "Shiri validation"
SLOT, RATE, SOURCE_PORT = 7, 48000, 4169
SOURCE_KEY = "validation:airplay-source"


def require(condition, message):
    if not condition:
        raise RuntimeFailure(message)


def closed_slot():
    for direction in ("pcm0p", "pcm0c", "pcm1p", "pcm1c"):
        path = Path(f"/proc/asound/Loopback/{direction}/sub{SLOT}/status")
        require(path.read_text().strip() == "closed", f"Loopback slot7 already in use: {path}")


async def host_snapshot():
    runner = Runner()
    links = await runner.json(["ip", "-j", "-d", "link", "show"])
    addresses = await runner.json(["ip", "-j", "addr", "show"])
    namespaces = await runner.run(["ip", "netns", "list"])
    return {
        "links": {
            link["ifname"]: [link["ifindex"], link.get("address"), link.get("ifalias"),
                             link.get("linkinfo", {}).get("info_kind")]
            for link in links
        },
        "addresses": {
            link["ifname"]: sorted(
                [address["family"], address["local"], address["prefixlen"]]
                for address in link.get("addr_info", [])
            )
            for link in addresses
        },
        "namespaces": sorted(line.split()[0] for line in namespaces.stdout.splitlines() if line.split()),
    }


async def eventually(action, description, *, timeout=40):
    end = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < end:
        try:
            result = await action()
            if result is not None and result is not False:
                return result
        except RuntimeFailure as exc:
            last_error = str(exc)
        await asyncio.sleep(0.05)
    raise RuntimeFailure(f"Timed out waiting for {description}" + (f": {last_error}" if last_error else ""))


def generate_tone(path):
    # Sixty seconds, 48kHz stereo S16LE. No microphones or real music are used.
    mono = (8192 * np.sin(2 * np.pi * 440 * np.arange(RATE) / RATE)).astype("<i2")
    second = np.column_stack((mono, mono)).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(RATE)
        for _ in range(60):
            output.writeframesraw(second)
    path.chmod(0o600)


def source_config(directory, sender, password):
    media = directory / "media"
    cache = directory / "cache"
    media.mkdir(mode=0o700)
    cache.mkdir(mode=0o700)
    wav = media / "validation-440hz.wav"
    generate_tone(wav)
    config = directory / "source.conf"
    write_private(config, f"""general {{
  uid = "root"
  db_path = {quote(directory / 'source-songs.db')}
  cache_dir = {quote(cache)}
  logfile = "/dev/stdout"
  loglevel = info
  admin_password = {quote(password)}
  trusted_networks = {{ {quote(sender['api_host_ip'] + '/32')}, {quote(sender['api_ip'] + '/32')} }}
  websocket_port = 0
  ipv6 = no
  speaker_autoselect = no
  high_resolution_clock = yes
  start_buffer_ms = 500
}}
library {{
  name = "Shiri isolated validation source"
  port = {SOURCE_PORT}
  directories = {{ {quote(media)} }}
  follow_symlinks = false
  pipe_autostart = false
}}
audio {{ type = "disabled" }}
mpd {{
 port = 0
 http_port = 0
}}
airplay {quote(NAME)} {{ airplay2_disable = false reconnect = false }}
""")
    return config, wav


def drain(fd):
    chunks = []
    # Bounded per-tick consumption; the writer itself is nonblocking/bounded.
    for _ in range(16):
        try:
            chunk = os.read(fd, 65536)
        except BlockingIOError:
            break
        if not chunk:
            break
        chunks.append(chunk)
    return chunks


class Capture:
    def __init__(self, fd):
        self.fd, self.chunks, self.total = fd, [], 0
        self.done = asyncio.Event()

    async def run(self):
        while not self.done.is_set():
            for chunk in drain(self.fd):
                self.total += len(chunk)
                require(self.total <= 16 * 1024 * 1024, "PCM observation exceeded its size bound")
                self.chunks.append(chunk)
            await asyncio.sleep(0.002)


def analyze(chunks):
    require(bool(chunks), "No candidate FIFO PCM was observed")
    require(all(len(chunk) % 4 == 0 for chunk in chunks), "FIFO PCM did not contain complete S16 stereo frames")
    spectra, peaks, amplitudes, frames = [], [], [], 0
    channel_error = 0
    for chunk in chunks:
        pcm = np.frombuffer(chunk, dtype="<i2").reshape(-1, 2).astype(np.float64)
        frames += len(pcm)
        channel_error = max(channel_error, int(np.max(np.abs(pcm[:, 0] - pcm[:, 1]))))
        if len(pcm) < 960:
            continue
        mono = pcm[:, 0]
        # A second reader may consume intervening FIFO writes. Analyze actual
        # read blocks, rather than inventing a continuous sample timeline.
        power = np.abs(np.fft.rfft((mono - np.mean(mono)) * np.hanning(len(mono)))) ** 2
        frequencies = np.fft.rfftfreq(len(mono), d=1 / RATE)
        total = float(power.sum())
        if total <= 0:
            continue
        peaks.append(float(frequencies[int(np.argmax(power))]))
        spectra.append(float(power[(frequencies >= 380) & (frequencies <= 500)].sum() / total))
        amplitudes.append(float(np.sqrt(np.mean(mono ** 2))))
    require(frames >= RATE // 4, "Less than 250ms of actual PCM was observed")
    require(bool(spectra), "No measurable PCM blocks were observed")
    measured = {
        "format": "S16LE", "sample_rate": RATE, "channels": 2,
        "observed_frames": frames, "observed_bytes": frames * 4,
        "analyzed_blocks": len(spectra), "median_peak_hz": float(np.median(peaks)),
        "median_440_band_energy_fraction": float(np.median(spectra)),
        "median_rms": float(np.median(amplitudes)), "max_channel_difference": channel_error,
    }
    require(415 <= measured["median_peak_hz"] <= 465, f"PCM peak is inconsistent with 440Hz: {measured}")
    require(measured["median_440_band_energy_fraction"] >= 0.65, f"440Hz does not dominate actual PCM: {measured}")
    require(measured["median_rms"] > 20, f"Observed PCM is effectively silent: {measured}")
    require(channel_error <= 3, f"Stereo channels differ unexpectedly: {measured}")
    return measured


async def check():
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(), "passed": False,
        "scope": "Synthetic OwnTone AirPlay2 -> Shairport -> actual ALSA Loopback7 -> mixer FIFO",
        "stock_phone_verified": False, "physical_speaker_output_verified": False,
        "cast_input_verified": False, "native_multi_zone_grouping_verified": False,
        "room_id": ROOM_ID, "room_name": NAME, "slot": SLOT,
        "cleanup": {"source_stopped": False, "broker_closed": False, "empty_manifest": False},
    }
    broker = None
    source = None
    client = None
    reader = None
    capture = None
    capture_task = None
    directory = None
    baseline = None
    successful_body = False
    cleanup_errors = []
    try:
        require(sys.platform == "linux" and os.geteuid() == 0, "Run explicitly as root on Linux")
        root_directory(STATE)
        root_directory(RUN, 0o750)
        manifest_path = STATE / "ownership.json"
        require(manifest_path.is_file(), "Existing candidate installation manifest is required; do not recreate it")
        manifest = json.loads(manifest_path.read_text())
        identity = manifest.get("installation_id", "")
        require(identity.startswith("b265"), "The known b265 candidate installation identity does not match")
        require(not manifest.get("networks") and not manifest.get("processes"), "Candidate retains resources; inspect before starting")
        report["installation_id"] = identity
        closed_slot()
        baseline = await host_snapshot()
        directory = Path(tempfile.mkdtemp(prefix="airplay-source-", dir=STATE))
        directory.chmod(0o700)
        report["artifacts"] = {"directory": str(directory), "source_log": str(directory / "logs" / "validation-source.log")}
        config = Settings(
            state_dir=STATE / "unused-api", runtime_state_dir=STATE, runtime_dir=RUN,
            runtime_socket=RUN / "runtime.sock", binary_dir=BINARIES,
        )
        broker = Broker(config)
        health = await broker.start(serve=True)
        require(health["ready"], health.get("error") or "Candidate broker is not ready")
        report["versions"] = health["versions"]
        require(broker.network.installation_id == identity, "Installation identity changed")
        room = Room(id=ROOM_ID, slot=SLOT, name=NAME, airplay_name=NAME, interface="enp0s1",
                    enabled=True, volume=20, speakers=[], local_audio_device=None)
        await broker.reconcile({"rooms": [room.model_dump()]})
        async def room_ready():
            state = broker.rooms[ROOM_ID]
            require(state.status not in {"error", "degraded"}, state.error or "Room startup failed")
            return state if state.status == "running" else None
        state = await eventually(room_ready, "candidate room startup", timeout=65)
        require(state.selected_ids == [], "Candidate must have no assigned physical outputs")
        report["receiver"] = {key: state.receiver[key] for key in ["namespace", "interface", "mac", "ip", "alias"]}
        audio_socket = state.directory / "audio.sock"
        before = await call_rpc(audio_socket, "health", timeout=2)
        require(before["ready"] and not before["music_active"] and not before["audio_active"], "Candidate was not idle")
        require(before["speech_session_id"] is None, "Candidate has an unexpected speech producer")
        report["audio_before"] = before
        fifo = state.directory / "pipes" / "audio.pipe"
        require(stat.S_ISFIFO(fifo.lstat().st_mode), "Candidate output is not an actual FIFO")
        reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        require(not drain(reader), "Candidate emitted audio before synthetic playback")
        capture = Capture(reader)
        capture_task = asyncio.create_task(capture.run())
        source_file, wav = source_config(directory, broker.sender, broker._password)
        report["artifacts"]["source_wav"] = str(wav)
        source = await broker._start_process(
            SOURCE_KEY, "validation-source",
            isolated_command(STATE / "sender" / "isolation", broker.sender["namespace"], [
                broker.binary("owntone"), "-f", "-c", str(source_file),
                "--mdns-no-rsp", "--mdns-no-daap", "--mdns-no-web", "--mdns-no-cname",
            ]), directory,
        )
        client = OwnToneClient(f"http://{broker.sender['api_ip']}:{SOURCE_PORT}", password=broker._password)
        async def source_ready():
            require(source.alive, "Synthetic OwnTone source exited; inspect its private log")
            return await client.request("GET", "/api/player")
        report["source_initial_player"] = await eventually(source_ready, "synthetic source control", timeout=35)
        # Update the exact durable identity after unshare/shell exec settles.
        await broker._remember_process(SOURCE_KEY, source)
        await client.select([], await client.outputs(set()))
        async def discover():
            outputs = await client.outputs(set())
            matches = [output for output in outputs if output["name"] == NAME]
            require(len(matches) <= 1, "Candidate receiver name is ambiguous; refusing selection")
            if not matches:
                return None
            selected = matches[0]
            require(selected["protocol"] == "airplay2", "Candidate was not discovered as AirPlay2; refusing RAOP fallback")
            # The pinned receiver takes its AirPlay2 device ID from its first
            # non-loopback MAC. Its private namespace contains only lo and the
            # owned receiver interface. OwnTone converts this ID to decimal.
            expected_id = str(int(state.receiver["mac"].replace(":", ""), 16))
            require(selected["id"] == expected_id, "Receiver name matches but physical identity differs; refusing selection")
            require(not selected["requires_auth"], "Candidate requires unprovided device authorization")
            return selected
        target = await eventually(discover, "exact AirPlay2 candidate discovery", timeout=40)
        report["source_target"] = {key: target[key] for key in ["id", "name", "protocol", "supported_formats"]}
        await client.volume(20)
        speaker = SpeakerRef(id=target["id"], name=NAME, protocol="airplay2")
        await client.select([speaker], await client.outputs(set()))
        async def library_ready():
            library = await client.request("GET", "/api/library")
            require(library.get("songs", 0) <= 1, "Synthetic source indexed unexpected media")
            return library if library.get("songs") == 1 and not library.get("updating") else None
        report["source_library"] = await eventually(library_ready, "the private tone scan", timeout=35)
        queued = await client.request("POST", "/api/queue/items/add", params={
            "expression": "media_kind is music", "limit": "1", "playback": "start", "clear": "true",
        })
        require(queued.get("count") == 1 and len(queued.get("items", [])) == 1, "Private tone was not queued exactly once")
        require(queued["items"][0].get("path") == str(wav), "Queued media is not the generated private tone")
        report["queue_item"] = {key: queued["items"][0].get(key) for key in ["id", "path", "length_ms", "media_kind"]}
        async def music_started():
            observed = await call_rpc(audio_socket, "health", timeout=2)
            require(observed["ready"] and not observed["error"], "Candidate audio worker failed")
            return observed if observed["music_active"] and observed["audio_active"] else None
        report["audio_started"] = await eventually(music_started, "the real music-start hook", timeout=25)
        async def source_playing():
            observed = await client.request("GET", "/api/player")
            report["source_starting_player"] = observed
            return observed if observed.get("state") == "play" else None
        report["source_playing_player"] = await eventually(source_playing, "source playback after AirPlay session setup", timeout=20)
        samples = []
        observation_started = time.monotonic()
        while time.monotonic() - observation_started < 10:
            await asyncio.sleep(0.25)
            require(not capture_task.done(), "FIFO observation task ended unexpectedly")
            require(source.alive and all(process.alive for process in state.processes.values()), "An actual backend exited during playback")
            player = await client.request("GET", "/api/player")
            report["source_last_player"] = player
            outputs = await client.outputs(set())
            require(player.get("state") == "play", "Synthetic source did not continue playing")
            require({output["id"] for output in outputs if output["selected"]} == {target["id"]}, "Source output selection changed")
            require(state.selected_ids == [], "Candidate acquired a physical speaker")
            samples.append({"at_seconds": round(time.monotonic() - observation_started, 3),
                            "progress_ms": player.get("item_progress_ms"), "observed_bytes": capture.total})
        report["source_progress"] = samples
        require(samples[-1]["progress_ms"] > samples[0]["progress_ms"] + 5000, "Source program timeline did not advance")
        playing_audio = await call_rpc(audio_socket, "health", timeout=2)
        require(playing_audio["written_bytes"] > before["written_bytes"], "Mixer did not write actual FIFO PCM")
        require(playing_audio["music_active"] and playing_audio["audio_active"] and playing_audio["fifo_reader"], "Actual music hook/FIFO state is inconsistent")
        report["audio_playing"] = playing_audio
        candidate_outputs = await state.client.outputs(set())
        require(not any(output["selected"] for output in candidate_outputs), "Candidate OwnTone selected a physical output")
        report["candidate_selected_outputs"] = []
        await client.request("PUT", "/api/player/stop")
        async def music_stopped():
            observed = await call_rpc(audio_socket, "health", timeout=2)
            return observed if not observed["music_active"] and not observed["audio_active"] else None
        stopped = await eventually(music_stopped, "the real music-stop hook", timeout=20)
        await asyncio.sleep(0.3)
        settled = await call_rpc(audio_socket, "health", timeout=2)
        await asyncio.sleep(0.3)
        idle = await call_rpc(audio_socket, "health", timeout=2)
        require(idle["written_bytes"] == settled["written_bytes"], "Idle mixer retained a replay/write backlog")
        report["audio_stopped"] = {"hook": stopped, "settled": settled, "idle": idle}
        report["source_stopped_player"] = await client.request("GET", "/api/player")
        require(report["source_stopped_player"].get("state") == "stop", "Source did not acknowledge stopped playback")
        capture.done.set()
        await asyncio.wait_for(capture_task, 2)
        report["pcm"] = analyze(capture.chunks)
        pcm_path = directory / "observed-fifo-s16le-stereo-48000.pcm"
        with pcm_path.open("wb") as output:
            for chunk in capture.chunks:
                output.write(chunk)
        pcm_path.chmod(0o600)
        report["artifacts"]["observed_pcm"] = str(pcm_path)
        report["pcm"]["sha256"] = hashlib.sha256(pcm_path.read_bytes()).hexdigest()
        report["pcm"]["observation_note"] = "Observed FIFO blocks; candidate OwnTone may also consume writes. No full-rate/physical-output claim."
        successful_body = True
    except BaseException as exc:
        # Do not serialize configs, HTTP headers or the private backend password.
        message = str(exc)
        if broker and broker._password:
            message = message.replace(broker._password, "[redacted]")
        report["failure"] = {"type": type(exc).__name__, "message": message}
    finally:
        if capture_task and not capture_task.done():
            capture.done.set()
            capture_task.cancel()
            await asyncio.gather(capture_task, return_exceptions=True)
        if client:
            with suppress(Exception):
                await client.request("PUT", "/api/player/stop")
                await client.select([], await client.outputs(set()))
            try:
                await client.close()
            except Exception as exc:
                cleanup_errors.append(f"source control close: {exc}")
        if reader is not None:
            os.close(reader)
        if source:
            try:
                await asyncio.wait_for(source.stop(), 10)
                require(not source.alive, "Synthetic source survived stop")
                broker.network.forget_process(SOURCE_KEY)
                report["cleanup"]["source_stopped"] = True
            except Exception as exc:
                cleanup_errors.append(f"source: {exc}")
        if broker:
            try:
                await asyncio.wait_for(broker.close(), 45)
                report["cleanup"]["broker_closed"] = True
            except Exception as exc:
                cleanup_errors.append(f"broker: {exc}")
        if baseline is not None:
            try:
                current = json.loads((STATE / "ownership.json").read_text())
                require(current["installation_id"] == report["installation_id"], "Installation identity changed during cleanup")
                require(not current["networks"] and not current["processes"], "Manifest retained live resources")
                closed_slot()
                require(await host_snapshot() == baseline, "Host links, addresses or namespaces changed after cleanup")
                report["cleanup"]["empty_manifest"] = True
                report["cleanup"]["slot_closed"] = True
                report["cleanup"]["host_preserved"] = True
            except Exception as exc:
                cleanup_errors.append(f"verification: {exc}")
        if cleanup_errors:
            report["cleanup_errors"] = cleanup_errors
        if directory is not None and capture is not None:
            try:
                pcm_path = directory / "observed-fifo-s16le-stereo-48000.pcm"
                with pcm_path.open("wb") as output:
                    for chunk in capture.chunks:
                        output.write(chunk)
                pcm_path.chmod(0o600)
                report["artifacts"]["observed_pcm"] = str(pcm_path)
                report["captured_bytes"] = capture.total
            except Exception as exc:
                cleanup_errors.append(f"PCM artifact: {exc}")
                report["cleanup_errors"] = cleanup_errors
        report["passed"] = successful_body and not cleanup_errors
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        atomic_json(RESULT, report)
        print(json.dumps({"passed": report["passed"], "result": str(RESULT),
                          "failure": report.get("failure"), "cleanup_errors": report.get("cleanup_errors")}), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(asyncio.run(check()))
