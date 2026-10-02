#!/usr/bin/env python3
"""Synthetic AirPlay 2 plus direct-worker WebRTC TTS check; run on Linux.

OwnTone source -> candidate Shairport -> actual Loopback slot7 -> real mixer
FIFO, with real Opus WebRTC speech delivered directly to AudioWorker RPC.
No house output is assigned. This bypasses the rootless API and broker speech
routing gate; it is not a stock-phone, native grouping, Cast input, physical
speaker or API acceptance test. Credentials and SDP remain private.

Use the candidate Python environment with this checkout installed/importable:
  sudo /opt/shiri-v2/.venv/bin/python /opt/shiri-v2/tests/linux/check_airplay_tts.py
"""

import asyncio
from contextlib import suppress
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import wave
from uuid import uuid4

import numpy as np
from aiortc import AudioStreamTrack, RTCConfiguration, RTCPeerConnection, RTCRtpSender, RTCSessionDescription
from aiortc.sdp import SessionDescription
from av import AudioFrame

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
RESULT = Path("/tmp/shiri-v2-airplay-tts-result.json")
ROOM_ID = "b6786543-7eb2-443d-83b1-65b984123a76"
NAME = "Shiri validation"
SLOT, RATE, SOURCE_PORT = 7, 48000, 4169
SOURCE_KEY = "validation:airplay-source"
_lab_spec = importlib.util.spec_from_file_location('native_lab_admission', Path(__file__).with_name('native_lab.py'))
_lab_module = importlib.util.module_from_spec(_lab_spec)
_lab_spec.loader.exec_module(_lab_module)
NATIVE_LAB = _lab_module.from_environment()
if NATIVE_LAB is not None:
    STATE, RUN, BINARIES = NATIVE_LAB.state, NATIVE_LAB.run, NATIVE_LAB.binaries


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
        self.captured_at = []
        self.done = asyncio.Event()

    async def run(self):
        while not self.done.is_set():
            for chunk in drain(self.fd):
                self.total += len(chunk)
                require(self.total <= 16 * 1024 * 1024, "PCM observation exceeded its size bound")
                self.chunks.append(chunk)
                self.captured_at.append(time.monotonic())
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


class SpeechTone(AudioStreamTrack):
    """Paced 20ms mono frames; silence retains the same healthy RTP session."""

    def __init__(self):
        super().__init__()
        self.samples = 0
        self.silent = False
        self.started = None

    async def recv(self):
        if self.started is None:
            self.started = time.monotonic()
        target = self.started + self.samples / RATE
        await asyncio.sleep(max(0, target - time.monotonic()))
        frame = AudioFrame(format="s16", layout="mono", samples=960)
        positions = self.samples + np.arange(960)
        values = (np.zeros(960, dtype="<i2") if self.silent else
                  (600 * np.sin(2 * np.pi * 880 * positions / RATE)).astype("<i2"))
        frame.planes[0].update(values.tobytes())
        frame.sample_rate = RATE
        frame.pts = self.samples
        frame.time_base = Fraction(1, RATE)
        self.samples += 960
        return frame


def make_speech_peer():
    peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    tone = SpeechTone()
    transceiver = peer.addTransceiver(tone, direction="sendonly")
    opus = [codec for codec in RTCRtpSender.getCapabilities("audio").codecs
            if codec.mimeType.lower() == "audio/opus"]
    require(bool(opus), "The synthetic speech sender does not support Opus")
    transceiver.setCodecPreferences(opus)
    return peer, tone


def stage_spectrum(chunks, *, minimum_frames=RATE // 4):
    """Fit both tones per read block, allowing arbitrary block phase/gaps."""
    require(bool(chunks), "No PCM was captured in this observation stage")
    require(all(len(chunk) % 4 == 0 for chunk in chunks), "Stage contains an incomplete stereo frame")
    music, speech, rms_values, peaks, music_energy = [], [], [], [], []
    frames = 0
    channel_error = 0
    for chunk in chunks:
        pcm = np.frombuffer(chunk, dtype="<i2").reshape(-1, 2).astype(np.float64)
        frames += len(pcm)
        channel_error = max(channel_error, int(np.max(np.abs(pcm[:, 0] - pcm[:, 1]))))
        if len(pcm) < 960:
            continue
        mono = pcm[:, 0]
        t = np.arange(len(mono)) / RATE
        basis = np.column_stack((np.sin(2 * np.pi * 440 * t), np.cos(2 * np.pi * 440 * t),
                                 np.sin(2 * np.pi * 880 * t), np.cos(2 * np.pi * 880 * t),
                                 np.ones(len(t))))
        fitted, *_ = np.linalg.lstsq(basis, mono, rcond=None)
        music.append(float(np.hypot(fitted[0], fitted[1])))
        speech.append(float(np.hypot(fitted[2], fitted[3])))
        rms_values.append(float(np.sqrt(np.mean(mono ** 2))))
        power = np.abs(np.fft.rfft((mono - np.mean(mono)) * np.hanning(len(mono)))) ** 2
        frequency = np.fft.rfftfreq(len(mono), d=1 / RATE)
        peaks.append(float(frequency[int(np.argmax(power))]))
        total_power = float(power.sum())
        music_energy.append(float(power[(frequency >= 380) & (frequency <= 500)].sum() / total_power)
                            if total_power > 0 else 0.0)
    require(frames >= minimum_frames and bool(music), "Insufficient stage PCM for tone measurement")
    require(channel_error <= 3, "Observed stereo PCM has unexpected channel differences")
    return {"observed_frames": frames, "observed_bytes": frames * 4,
            "analyzed_blocks": len(music), "median_peak_hz": float(np.median(peaks)),
            "median_440_band_energy_fraction": float(np.median(music_energy)),
            "music_440_amplitude": float(np.median(music)),
            "speech_880_amplitude": float(np.median(speech)),
            "median_rms": float(np.median(rms_values)), "max_channel_difference": channel_error}


async def wait_actual_music(capture, healthy, report, *, started, timeout=20):
    """Require a fresh sustained 440Hz signal, rather than music/play flags."""
    end = time.monotonic() + timeout
    next_chunk = len(capture.chunks)
    examined = 0
    consecutive_blocks = 0
    consecutive_frames = 0
    first_audible_at = None
    first_audible_progress = None
    last_audible_at = None
    last_quality = None
    report["music_onset"] = {"passed": False, "timeout_seconds": timeout,
                             "reference": "Start of continuity observation after the source reported play; not sender transmission or acoustic onset."}
    while time.monotonic() < end:
        await healthy()
        while next_chunk < len(capture.chunks):
            chunk = capture.chunks[next_chunk]
            captured_at = capture.captured_at[next_chunk]
            next_chunk += 1
            examined += 1
            require(len(chunk) % 4 == 0, "Actual music onset contains an incomplete stereo frame")
            if len(chunk) < 960 * 4:
                continue
            quality = stage_spectrum([chunk], minimum_frames=960)
            last_quality = quality
            fresh = time.monotonic() - captured_at <= 0.25
            audible = (fresh and quality["music_440_amplitude"] > 100
                       and quality["median_rms"] > 20
                       and 415 <= quality["median_peak_hz"] <= 465
                       and quality["median_440_band_energy_fraction"] >= 0.65
                       and quality["speech_880_amplitude"] < quality["music_440_amplitude"] * 0.1)
            if audible:
                if consecutive_blocks == 0:
                    first_audible_at = captured_at
                consecutive_blocks += 1
                consecutive_frames += quality["observed_frames"]
                last_audible_at = captured_at
            else:
                consecutive_blocks = consecutive_frames = 0
                first_audible_at = None
            if consecutive_blocks >= 3 and consecutive_frames >= RATE // 4:
                first_audible_progress = max(0, first_audible_at - started)
                report["music_onset"].update({
                    "passed": True, "seconds_after_control_play_observation": round(first_audible_progress, 6),
                    "gate_passed_after_seconds": round(time.monotonic() - started, 6),
                    "qualifying_blocks": consecutive_blocks, "qualifying_frames": consecutive_frames,
                    "examined_blocks": examined, "last_block": quality,
                    "observation_note": "Individual complete FIFO read blocks, possibly shared with OwnTone; observed PCM amount is not a full-rate or physical-output claim.",
                })
                return report["music_onset"]
        if last_audible_at is not None and time.monotonic() - last_audible_at > 0.25:
            consecutive_blocks = consecutive_frames = 0
            first_audible_at = None
        await asyncio.sleep(0.02)
    report["music_onset"].update({"examined_blocks": examined, "last_block": last_quality,
                                 "elapsed_seconds": round(time.monotonic() - started, 6)})
    raise RuntimeFailure("Actual 440Hz FIFO music did not satisfy the bounded audible-onset gate")


def owned_identities(broker, state, source):
    processes = {"validation-source": source}
    processes.update({f"sender:{name}": process for name, process in broker.sender_processes.items()})
    processes.update({f"room:{name}": process for name, process in state.processes.items()})
    require(all(process.alive for process in processes.values()), "An owned backend exited during the speech check")
    return {name: {"pid": process.process.pid, "birth": process.birth}
            for name, process in processes.items()}


async def exercise_speech(broker, state, source, client, target, queue_id, audio_socket,
                          capture, capture_task, peer, tone, identity, report):
    phase = "awaiting_actual_music"
    samples = []
    baseline_processes = owned_identities(broker, state, source)
    report["owned_processes_before_speech"] = baseline_processes
    monitor_done = asyncio.Event()
    started = time.monotonic()

    async def monitor():
        previous = None
        while not monitor_done.is_set():
            player, outputs, health, candidate_outputs = await asyncio.gather(
                client.request("GET", "/api/player"), client.outputs(set()),
                call_rpc(audio_socket, "health", timeout=2), state.client.outputs(set()),
            )
            at = time.monotonic()
            require(owned_identities(broker, state, source) == baseline_processes,
                    "An owned process restarted or changed during speech")
            require(not capture_task.done(), "FIFO capture ended unexpectedly")
            require(player.get("state") == "play", "The synthetic AirPlay source left playback during speech")
            require(player.get("item_id") == queue_id, "The music queue item changed during speech")
            require(player.get("volume") == 20, "The selected source's master volume changed")
            require({output["id"] for output in outputs if output["selected"]} == {target["id"]},
                    "Synthetic AirPlay output selection changed")
            require(state.selected_ids == [] and not any(output["selected"] for output in candidate_outputs),
                    "Candidate acquired an unexpected physical speaker")
            require(health["ready"] and not health["error"] and health["music_active"] and health["audio_active"],
                    "Receiver music hook or worker health changed during speech")
            require(health["speech_session_id"] in {None, identity["session_id"]},
                    "An unexpected speech session appeared")
            progress = player.get("item_progress_ms")
            require(type(progress) is int and progress >= 0, "Source has no valid program progress")
            if previous:
                delta = progress - previous["progress_ms"]
                elapsed_ms = (at - previous["at"]) * 1000
                require(delta >= 0, "Music progress moved backward or restarted during speech")
                require(abs(delta - elapsed_ms) < 1500, "Music progress jumped or stalled during speech")
            sample = {"at": at, "at_seconds": round(at - started, 3), "stage": phase,
                      "progress_ms": progress, "music_gain": health["music_gain"],
                      "speech_session_id": health["speech_session_id"],
                      "observed_bytes": capture.total, "written_bytes": health["written_bytes"],
                      "dropped_bytes": health["dropped_bytes"]}
            samples.append(sample)
            previous = sample
            await asyncio.sleep(0.1)

    monitor_task = asyncio.create_task(monitor())

    async def healthy():
        if monitor_task.done():
            await monitor_task  # Propagate the concrete monitor failure.
            raise RuntimeFailure("Speech continuity monitor stopped unexpectedly")
        health = await call_rpc(audio_socket, "health", timeout=2)
        require(health["ready"] and not health["error"], "Audio worker failed during speech")
        return health

    async def gain_ready(value, *, session):
        health = await healthy()
        return health if (abs(health["music_gain"] - value) <= 0.025
                          and health["speech_session_id"] == session) else None

    async def observe(label, duration):
        nonlocal phase
        phase = label
        first = len(capture.chunks)
        end = time.monotonic() + duration
        while time.monotonic() < end:
            await healthy()
            await asyncio.sleep(0.08)
        chunks = capture.chunks[first:]
        result = stage_spectrum(chunks)
        result["worker"] = await healthy()
        report.setdefault("stages", {})[label] = result
        return result

    try:
        await eventually(lambda: gain_ready(1.0, session=None), "baseline music gain", timeout=3)
        await wait_actual_music(capture, healthy, report, started=started)
        baseline = await observe("baseline", 1.5)
        require(baseline["music_440_amplitude"] > 100, "Baseline music is effectively silent")
        require(baseline["speech_880_amplitude"] < baseline["music_440_amplitude"] * 0.1,
                "Baseline already contains unexpected speech-tone energy")
        phase = "negotiating"
        await asyncio.wait_for(peer.setLocalDescription(await peer.createOffer()), 8)
        answer = await call_rpc(audio_socket, "speech", {**identity, "action": "offer",
            "sdp": peer.localDescription.sdp, "type": "offer"}, timeout=10)
        parsed = SessionDescription.parse(answer["sdp"])
        require(len(parsed.media) == 1 and parsed.media[0].kind == "audio", "Speech answer has an unexpected media layout")
        codecs = parsed.media[0].rtp.codecs
        require(bool(codecs) and all(codec.mimeType.lower() == "audio/opus" for codec in codecs),
                "The worker did not negotiate the forced Opus codec")
        await asyncio.wait_for(peer.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"])), 4)
        # SDP is used only in memory and is never included in reports/logs.
        del answer, parsed

        async def speech_ready():
            health = await gain_ready(0.2, session=identity["session_id"])
            return health if peer.connectionState == "connected" else None

        await eventually(speech_ready, "connected Opus speech and music ducking", timeout=12)
        mixed = await observe("speech_ducked", 1.5)
        ratio = mixed["music_440_amplitude"] / baseline["music_440_amplitude"]
        mixed["music_ratio_to_baseline"] = ratio
        require(0.12 < ratio < 0.35, "Real AirPlay music did not follow the requested duck gain")
        require(mixed["speech_880_amplitude"] > 150, "Decoded Opus speech did not reach the actual FIFO")
        require(mixed["speech_880_amplitude"] > baseline["speech_880_amplitude"] * 4,
                "Speech-tone energy is not distinct from the baseline")
        require(abs(mixed["worker"]["music_gain"] - 0.2) <= 0.025, "Worker duck gain differs from requested value")
        report["webrtc"] = {"codec": "audio/opus", "connection_state_during_speech": peer.connectionState,
                            "speech_samples_generated": tone.samples, "session_id": identity["session_id"]}

        phase = "silence_tail"
        tone.silent = True
        await eventually(lambda: gain_ready(1.0, session=identity["session_id"]),
                         "silence releases ducking without closing the peer", timeout=4)
        silent = await observe("silence_connected", 1.5)
        silent_ratio = silent["music_440_amplitude"] / baseline["music_440_amplitude"]
        silent["music_ratio_to_baseline"] = silent_ratio
        require(peer.connectionState == "connected", "Silence disconnected the healthy speech sender")
        require(0.85 < silent_ratio < 1.15, "Music did not restore while the same speech session sent silence")
        require(silent["speech_880_amplitude"] < max(60, mixed["speech_880_amplitude"] * 0.2),
                "Speech media retained an audible tail after the silence deadline")
        stats = await peer.getStats()
        packets = sum(stat.packetsSent for stat in stats.values() if stat.type == "outbound-rtp")
        require(packets >= 50, "Speech observation did not send sufficient actual RTP packets")
        report["webrtc"]["rtp_packets_sent_before_close"] = packets

        phase = "closing"
        await call_rpc(audio_socket, "speech", {**identity, "action": "close",
                       "request_id": f"close-{uuid4()}"}, timeout=4)
        tone.stop()
        await asyncio.wait_for(peer.close(), 4)
        require(peer.connectionState == "closed", "Healthy WebRTC sender did not close")
        await eventually(lambda: gain_ready(1.0, session=None), "closed session releases worker ownership", timeout=4)
        restored = await observe("closed_restored", 1.5)
        restored_ratio = restored["music_440_amplitude"] / baseline["music_440_amplitude"]
        restored["music_ratio_to_baseline"] = restored_ratio
        require(0.85 < restored_ratio < 1.15, "Music did not stay restored after healthy speech close")
        require(restored["speech_880_amplitude"] < max(60, mixed["speech_880_amplitude"] * 0.2),
                "Closed speech retained an audible replay tail")
        require(samples[-1]["progress_ms"] > samples[0]["progress_ms"] + 5000,
                "Actual source program progress did not advance through the speech test")
        report["owned_processes_after_speech"] = owned_identities(broker, state, source)
        report["webrtc"]["connection_state_after_close"] = peer.connectionState
        report["continuity"] = {"all_sampled_source_states_playing": True, "queue_item_unchanged": True,
                                "source_selected_only_exact_airplay2_receiver": True,
                                "owned_pids_and_births_unchanged": True, "progress_monotonic": True,
                                "sampling_note": "Control polling at about 100ms plus request latency; no sub-poll or physical continuity claim."}
    finally:
        monitor_done.set()
        if not monitor_task.done():
            monitor_task.cancel()
        outcomes = await asyncio.gather(monitor_task, return_exceptions=True)
        report["source_progress"] = [{key: value for key, value in item.items() if key != "at"}
                                     for item in samples]
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                raise outcome


async def check():
    require(NATIVE_LAB is None, 'Standalone AirPlay fixture has no admitted clean-VM network supervisor')
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(), "passed": False,
        "scope": "Synthetic OwnTone AirPlay2 -> Shairport -> ALSA Loopback7 -> mixer FIFO plus real Opus WebRTC speech via direct worker RPC",
        "rootless_api_speech_verified": False, "broker_speech_routing_verified": False,
        "stock_phone_verified": False, "physical_speaker_output_verified": False,
        "cast_input_verified": False, "native_multi_zone_grouping_verified": False,
        "room_id": ROOM_ID, "room_name": NAME, "slot": SLOT,
        "cleanup": {"source_stopped": False, "broker_closed": False, "empty_manifest": False,
                    "speech_session_closed": False, "speech_peer_closed": False},
    }
    broker = None
    source = None
    client = None
    speech_peer = None
    speech_tone = None
    speech_identity = None
    audio_socket = None
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
        directory = Path(tempfile.mkdtemp(prefix="airplay-tts-source-", dir=STATE))
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
        speaker = SpeakerRef(id=target["id"], name=NAME, protocol="airplay2")
        await client.select([speaker], await client.outputs(set()))
        # OwnTone selection can reset master volume; set and verify afterward.
        await client.volume(20)
        report["source_selected_player"] = await client.request("GET", "/api/player")
        require(report["source_selected_player"].get("volume") == 20, "Source volume was not retained after selection")
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
        speech_peer, speech_tone = make_speech_peer()
        speech_identity = {"session_id": f"airplay-tts-{uuid4()}",
                           "request_id": f"offer-{uuid4()}", "duck_gain": 0.2}
        await exercise_speech(broker, state, source, client, target, queued["items"][0]["id"],
                              audio_socket, capture, capture_task, speech_peer, speech_tone,
                              speech_identity, report)
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
        if "v=0" in message or "a=ice-" in message or "a=fingerprint:" in message:
            message = "Speech negotiation failed; SDP omitted"
        if broker and broker._password:
            message = message.replace(broker._password, "[redacted]")
        report["failure"] = {"type": type(exc).__name__, "message": message}
    finally:
        if speech_identity and audio_socket:
            try:
                await call_rpc(audio_socket, "speech", {**speech_identity, "action": "close",
                               "request_id": f"cleanup-{uuid4()}"}, timeout=4)
                report["cleanup"]["speech_session_closed"] = True
            except Exception as exc:
                cleanup_errors.append(f"speech session close: {type(exc).__name__}")
        if speech_tone:
            speech_tone.stop()
        if speech_peer:
            try:
                await asyncio.wait_for(speech_peer.close(), 4)
                require(speech_peer.connectionState == "closed", "Speech peer did not close")
                report["cleanup"]["speech_peer_closed"] = True
            except Exception as exc:
                cleanup_errors.append(f"speech peer close: {type(exc).__name__}")
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
    logging.getLogger("aiortc").setLevel(logging.WARNING)
    logging.getLogger("aioice").setLevel(logging.WARNING)
    raise SystemExit(asyncio.run(check()))
