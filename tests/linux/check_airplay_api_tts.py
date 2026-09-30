#!/usr/bin/env python3
"""Manual isolated Linux candidate: rootless API speech to real final ALSA PCM.

Synthetic OwnTone AirPlay 2 source -> exact Shairport receiver -> slot 7 music
Loopback -> mixer/FIFO -> candidate OwnTone selected local output -> reverse
slot 7 capture. All speech mutations use authenticated HTTP API -> broker ->
worker. This harness never reads the mixer FIFO and selects no house output.
Requires the known b265 candidate installation, Linux root, the existing shiri
account, all four free slot 7 endpoints, and check_airplay_tts.py beside this
checkout. Run only after stopping the dedicated candidate test services.

Explicit supervised run from the staged checkout:
  sudo systemd-run --wait --pipe --collect --unit=shiri-v2-api-tts-check \
    --property=RuntimeMaxSec=240 --property=TimeoutStopSec=10 \
    --property=KillMode=control-group \
    /opt/shiri-v2/.venv/bin/python /opt/shiri-v2/tests/linux/check_airplay_api_tts.py

The whole-process watchdog also bounds a potentially stuck GStreamer shutdown
thread, which Python cannot forcibly cancel. Inspect the private JSON report
and exact owned-resource cleanup before another run; never kill unrelated
processes or aim this check at production. This is not stock-phone, physical
speaker, native grouping, Cast input or completed least-privilege acceptance.
"""

import asyncio
from collections import deque
from contextlib import suppress
from datetime import datetime, timezone
import grp
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import pwd
import secrets
import socket
import stat
import sys
import tempfile
import time
import wave
from uuid import uuid4

import httpx
import numpy as np
from aiortc import RTCSessionDescription
from aiortc.sdp import SessionDescription

import shiri
from shiri.domain import RoomCreate, SpeakerRef
from shiri.rpc import call_rpc
from shiri.runtime.backend import OwnToneClient
from shiri.runtime.broker import Broker
from shiri.runtime.configuration import isolated_command
from shiri.runtime.system import atomic_json, process_birth, root_directory
from shiri.settings import Settings
from shiri.store import Store

PROJECT = Path(shiri.__file__).resolve().parent.parent
COMPANION = PROJECT / 'tests/linux/check_airplay_tts.py'
require_companion = COMPANION.is_file()
if not require_companion:
    raise RuntimeError('Stage tests/linux/check_airplay_tts.py beside the candidate checkout first')
_spec = importlib.util.spec_from_file_location('passed_airplay_tts_check', COMPANION)
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)

STATE, RUN, BINARIES = base.STATE, base.RUN, base.BINARIES
ROOM_ID, NAME, SLOT = base.ROOM_ID, base.NAME, base.SLOT
RESULT = Path('/tmp/shiri-v2-airplay-api-tts-result.json')
API_PORT = 18083
API_BASE = f'http://127.0.0.1:{API_PORT}'
LOCAL_PLAYBACK = f'hw:Loopback,1,{SLOT}'
LOCAL_CAPTURE = f'hw:Loopback,0,{SLOT}'
SOURCE_VOLUME = 20
TEST_OUTPUT_VOLUME = 100  # Only the verified virtual Loopback output, before baseline.
SOFTWARE_VOLUME_EXPONENT = 3  # Explicit pinned OwnTone software_volume patch.
require = base.require


def redact_exception(exc, *private):
    message = str(exc)
    for value in private:
        if value:
            message = message.replace(value, '[redacted]')
    if any(value in message for value in ('v=0', 'a=ice-', 'a=fingerprint:')):
        return f'{type(exc).__name__}: speech negotiation failed; SDP omitted'
    return message


class Api:
    def __init__(self):
        self.client = httpx.AsyncClient(base_url=API_BASE, timeout=httpx.Timeout(25, connect=2),
                                       trust_env=False, headers={'Origin': API_BASE},
                                       limits=httpx.Limits(max_connections=4, max_keepalive_connections=2))

    async def request(self, method, path, *, json=None, expected=200):
        response = await asyncio.wait_for(self.client.request(method, path, json=json), 30)
        # Never dump response bodies: successful speech replies contain SDP.
        require(response.status_code == expected,
                f'API {method} {path} returned HTTP {response.status_code}, expected {expected}')
        if response.status_code == 204:
            return {}
        data = response.json()
        require(isinstance(data, dict), f'API {method} {path} returned an invalid envelope')
        return data

    async def room(self):
        view = await self.request('GET', '/api/v1/state')
        require(not view['runtime'].get('simulation'), 'Candidate API unexpectedly uses simulation')
        matches = [room for room in view['rooms'] if room['id'] == ROOM_ID]
        require(len(matches) == 1, 'Fixed slot7 room disappeared from disposable API storage')
        return matches[0]

    async def patch(self, changes):
        # Revisions can advance via the legitimate phone-volume receipt path.
        for _ in range(5):
            room = await self.room()
            response = await asyncio.wait_for(self.client.patch(f'/api/v1/rooms/{ROOM_ID}',
                                               json={'expected_revision': room['revision'], 'changes': changes}), 30)
            if response.status_code == 409:
                await asyncio.sleep(.1)
                continue
            require(response.status_code == 200, f'API room patch returned HTTP {response.status_code}')
            data = response.json()
            require(data.get('runtime_accepted') is True, 'API saved intent without broker acceptance')
            return data['room']
        raise base.RuntimeFailure('Disposable room revision did not settle for API update')

    async def close(self):
        await self.client.aclose()


def seed_database(path):
    with Store(path) as store:
        # Explicit fixed UUID keeps receiver identity scoped to the same known
        # candidate. Seven disabled definitions reserve logical slots only.
        with store._transaction() as connection:
            placeholders = [store._insert_room(connection, RoomCreate(
                name=f'API TTS disabled reservation {slot}', interface='enp0s1')) for slot in range(SLOT)]
            room = store._insert_room(connection, RoomCreate(
                name=NAME, airplay_name=NAME, nobly_room_id='candidate-api-tts-room',
                interface='enp0s1', local_audio_device=LOCAL_PLAYBACK), room_id=ROOM_ID)
        require(room.slot == SLOT and not room.enabled, 'Disposable room did not occupy disabled slot7')
        require(all(not item.enabled and item.local_audio_device is None for item in placeholders),
                'A placeholder could open an unintended audio device')
    return [item.id for item in placeholders]


class ApiProcess:
    def __init__(self, process, uid, gid, log_file):
        self.process, self.uid, self.gid, self.log_file = process, uid, gid, log_file
        self.birth = process_birth(process.pid)
        require(self.birth is not None, 'Could not capture owned API process identity')

    @property
    def alive(self):
        return self.process.returncode is None and process_birth(self.process.pid) == self.birth

    def evidence(self):
        require(self.alive, 'Owned rootless API process exited')
        text = Path(f'/proc/{self.process.pid}/status').read_text()
        fields = dict(line.split(':', 1) for line in text.splitlines() if ':' in line)
        uids, gids = [int(value) for value in fields['Uid'].split()], [int(value) for value in fields['Gid'].split()]
        require(all(value == self.uid for value in uids) and self.uid != 0, 'API did not retain the nonroot shiri UID')
        require(all(value == self.gid for value in gids), 'API did not retain the expected shiri group')
        require(int(fields['CapEff'].strip(), 16) == 0, 'Rootless API retains effective capabilities')
        return {'pid': self.process.pid, 'birth': self.birth, 'uid': self.uid, 'gid': self.gid,
                'effective_capabilities': fields['CapEff'].strip()}

    async def stop(self):
        try:
            if self.alive:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 12)
                except asyncio.TimeoutError:
                    require(self.alive, 'API identity changed before forced owned cleanup')
                    self.process.kill()
                    await asyncio.wait_for(self.process.wait(), 3)
            require(self.process.returncode is not None, 'Owned API did not finish')
        finally:
            self.log_file.close()


async def launch_api(directory, config, account, group):
    # Refuse to share another server's listener rather than accepting its health.
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', API_PORT))
    api_state = directory / 'api'
    api_state.mkdir(mode=0o700)
    os.chown(api_state, account.pw_uid, group.gr_gid)
    database = api_state / 'shiri.sqlite3'
    placeholders = seed_database(database)
    for path in api_state.iterdir():
        require(stat.S_ISREG(path.lstat().st_mode), 'Unexpected file in newly seeded API state')
        os.chown(path, account.pw_uid, group.gr_gid)
        path.chmod(0o600)
    token = secrets.token_urlsafe(48)
    token_path = directory / 'api-token'
    descriptor = os.open(token_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o640)
    with os.fdopen(descriptor, 'w') as output:
        output.write(token + '\n')
    os.chown(token_path, 0, group.gr_gid)
    token_path.chmod(0o640)
    env = {key: value for key, value in os.environ.items() if not key.startswith('SHIRI_')}
    env.update({'SHIRI_STATE_DIR': str(api_state), 'SHIRI_RUNTIME_STATE_DIR': str(STATE),
                'SHIRI_RUNTIME_DIR': str(RUN), 'SHIRI_RUNTIME_SOCKET': str(config.runtime_socket),
                'SHIRI_API_TOKEN_FILE': str(token_path), 'SHIRI_SIMULATION': '0',
                'SHIRI_HOST': '127.0.0.1', 'SHIRI_PORT': str(API_PORT),
                'SHIRI_BINARY_DIR': str(BINARIES), 'SHIRI_TRUSTED_PROXY_IPS': '127.0.0.1,::1'})
    log_path = directory / 'rootless-api.log'
    log_file = log_path.open('xb')
    log_path.chmod(0o600)
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable, '-m', 'shiri.cli', '--log-level', 'WARNING', 'serve',
            cwd=str(PROJECT), env=env, user=account.pw_uid, group=group.gr_gid, extra_groups=[],
            start_new_session=True, stdout=log_file, stderr=asyncio.subprocess.STDOUT)
    except BaseException:
        log_file.close()
        raise
    try:
        owned = ApiProcess(process, account.pw_uid, group.gr_gid, log_file)
    except BaseException:
        # Keep the just-created handle even if initial /proc identity capture
        # fails; never leave an untracked disposable API child running.
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                process.kill()
                await asyncio.wait_for(process.wait(), 3)
        log_file.close()
        raise
    return owned, token, placeholders, log_path


class PcmSequence:
    """Prove frame continuity even when a bounded Gst observer drops buffers."""
    def __init__(self):
        self.previous = None
        self.frames = 0
        self.blocks = 0
        self.mode = None
        self.initial_discontinuity = False

    def push(self, metadata, frames, rate):
        offset, end = metadata['offset'], metadata['offset_end']
        pts, duration = metadata['pts'], metadata['duration']
        offsets = offset is not None and end is not None
        timestamps = pts is not None and duration is not None
        require(offsets or timestamps, 'Final PCM lacks frame offsets and timed-buffer continuity evidence')
        if offsets:
            require(end-offset == frames, 'Final PCM offsets disagree with actual captured frame count')
        if self.previous is not None:
            require(not metadata['discont'], 'Final PCM reports a discontinuity after its initial buffer')
            previous = self.previous
            if offsets and previous['offset_end'] is not None:
                require(offset == previous['offset_end'], 'Final PCM frame offsets show lost or repeated audio')
                self.mode = 'sample_offsets'
            else:
                require(timestamps and previous['pts'] is not None and previous['duration'] is not None,
                        'Final PCM cannot verify continuity across adjacent buffers')
                require(abs(pts-previous['pts']-previous['duration']) <= 2,
                        'Final PCM PTS/duration sequence shows lost or repeated audio')
                self.mode = 'pts_duration'
        else:
            self.initial_discontinuity = metadata['discont']
            self.mode = 'sample_offsets' if offsets else 'pts_duration'
        if timestamps:
            require(abs(duration-frames*1_000_000_000/rate) <= 2,
                    'Final PCM duration disagrees with its negotiated-rate frame count')
        self.previous = dict(metadata)
        self.frames += frames
        self.blocks += 1

    def evidence(self):
        return {'mode': self.mode, 'verified_frames': self.frames, 'verified_blocks': self.blocks,
                'initial_discontinuity': self.initial_discontinuity,
                'last_buffer': self.previous}


class OutputCapture:
    """Bounded observation of ONLY the reverse slot7 ALSA output pair."""
    def __init__(self, *, start=True):
        import gi
        gi.require_version('Gst', '1.0')
        gi.require_version('GstAudio', '1.0')
        from gi.repository import Gst, GstAudio
        Gst.init(None)
        self.Gst, self.GstAudio = Gst, GstAudio
        self.pipeline = Gst.Pipeline.new('api-tts-final-output-capture')
        self.pending = deque()
        self.chunks, self.captured_at = [], []
        self.total = 0
        self.rate = self.channels = self.format = None
        self.error, self.warning = None, None
        self.capture_dropped = 0
        self.discontinuities = 0
        self.last_packet_at = None
        self.max_packet_gap = 0
        self.closed = False
        self.needs_latency = False
        self.sequence = PcmSequence()
        self.bus = self.pipeline.get_bus()
        self.bus.set_sync_handler(self._bus, None)
        def element(factory, name, **properties):
            value = Gst.ElementFactory.make(factory, name)
            require(value is not None, f'Missing final-output capture element {factory}')
            for key, setting in properties.items():
                value.set_property(key.replace('_', '-'), setting)
            self.pipeline.add(value)
            return value
        try:
            self.source = element('alsasrc', 'reverse-slot7-capture', device=LOCAL_CAPTURE,
                                  provide_clock=False, buffer_time=120000, latency_time=20000)
            # Do not force a rate or resample: the already-open output pair
            # determines its real negotiated hardware format/rate.
            caps = element('capsfilter', 'stereo-s16', caps=Gst.Caps.from_string(
                'audio/x-raw,format=S16LE,channels=2,layout=interleaved'))
            queue = element('queue', 'bounded-capture', max_size_buffers=8, max_size_bytes=65536,
                            max_size_time=200_000_000, leaky=2)
            sink = element('appsink', 'observed-output', emit_signals=True, sync=False,
                           max_buffers=2, drop=True)
            for before, after in zip([self.source, caps, queue], [caps, queue, sink], strict=True):
                require(before.link(after), 'Could not link final-output capture')
            sink.connect('new-sample', self._sample)
            if start:
                self.start()
        except BaseException:
            self.close()
            raise

    def start(self):
        require(self.pipeline.set_state(self.Gst.State.PLAYING) != self.Gst.StateChangeReturn.FAILURE,
                'Final ALSA capture could not start')

    def _bus(self, _bus, message, _data):
        if message.type == self.Gst.MessageType.ERROR:
            error, _debug = message.parse_error()
            self.error = str(error)[:2000]
        elif message.type == self.Gst.MessageType.EOS:
            self.error = 'Final ALSA capture unexpectedly ended'
        elif message.type == self.Gst.MessageType.CLOCK_LOST:
            self.error = 'Final ALSA capture clock was lost'
        elif message.type == self.Gst.MessageType.WARNING:
            warning, _debug = message.parse_warning()
            self.warning = str(warning)[:1000]
        elif message.type == self.Gst.MessageType.LATENCY:
            self.needs_latency = True
        return self.Gst.BusSyncReply.DROP

    def _sample(self, sink):
        sample = sink.emit('pull-sample')
        if sample is None:
            return self.Gst.FlowReturn.EOS
        buffer = sample.get_buffer()
        info = self.GstAudio.AudioInfo.new_from_caps(sample.get_caps())
        if info is None or info.bpf != 4 or info.channels != 2 or info.rate <= 0:
            self.error = 'Final output negotiated unsupported audio framing'
            return self.Gst.FlowReturn.ERROR
        at = time.monotonic()
        if self.last_packet_at is not None:
            self.max_packet_gap = max(self.max_packet_gap, at - self.last_packet_at)
        self.last_packet_at = at
        if buffer.has_flags(self.Gst.BufferFlags.DISCONT):
            self.discontinuities += 1
        if len(self.pending) >= 128:
            self.capture_dropped += buffer.get_size()
            return self.Gst.FlowReturn.OK
        audio_format = sample.get_caps().get_structure(0).get_value('format')
        def valid(value):
            value = int(value)
            return value if 0 <= value < (1 << 64)-1 else None
        metadata = {'offset': valid(buffer.offset), 'offset_end': valid(buffer.offset_end),
                    'pts': valid(buffer.pts), 'duration': valid(buffer.duration),
                    'discont': buffer.has_flags(self.Gst.BufferFlags.DISCONT)}
        self.pending.append((at, buffer.extract_dup(0, buffer.get_size()),
                             info.rate, info.channels, audio_format, metadata))
        return self.Gst.FlowReturn.OK

    def poll(self):
        require(self.error is None, self.error or 'Final capture failed')
        require(self.capture_dropped == 0, 'Bounded final-output observer dropped data')
        if self.needs_latency:
            self.needs_latency = False
            self.pipeline.recalculate_latency()
        try:
            while self.pending:
                at, data, rate, channels, audio_format, metadata = self.pending[0]
                require(len(data) % 4 == 0 and bool(data), 'Final ALSA output contains incomplete or empty stereo frames')
                require(audio_format == 'S16LE', 'Final ALSA output changed sample format')
                if self.rate is not None:
                    require((rate, channels, audio_format) == (self.rate, self.channels, self.format),
                            'Final ALSA output renegotiated during observation')
                require(self.total+len(data) <= 16 * 1024 * 1024, 'Final-output observation exceeded its size bound')
                self.sequence.push(metadata, len(data)//4, rate)
                self.rate, self.channels, self.format = rate, channels, audio_format
                self.total += len(data)
                self.chunks.append(data)
                self.captured_at.append(at)
                self.pending.popleft()
        except base.RuntimeFailure as exc:
            # eventually() retries runtime readiness failures. An observation
            # violation is permanent, even if later data would look healthy.
            self.error = str(exc)[:2000]
            raise
        return {'capture': LOCAL_CAPTURE, 'format': self.format, 'rate': self.rate, 'channels': self.channels,
                'observed_bytes': self.total, 'capture_dropped_bytes': self.capture_dropped,
                'discontinuities': self.discontinuities, 'max_packet_gap_seconds': self.max_packet_gap,
                'warning': self.warning, 'frame_continuity': self.sequence.evidence()}

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.pipeline.set_state(self.Gst.State.NULL)
        self.bus.set_sync_handler(None, None)


def spectrum(chunks, rate, *, minimum_seconds=.25):
    require(bool(chunks) and type(rate) is int and rate > 0, 'No negotiated final-output PCM to analyze')
    require(all(len(chunk) % 4 == 0 for chunk in chunks), 'Malformed final stereo PCM')
    music, speech, energy, peaks, rms_values = [], [], [], [], []
    frames, peak_sample, channel_error = 0, 0, 0
    for chunk in chunks:
        pcm = np.frombuffer(chunk, dtype='<i2').reshape(-1, 2).astype(np.float64)
        frames += len(pcm)
        peak_sample = max(peak_sample, int(np.max(np.abs(pcm))))
        channel_error = max(channel_error, int(np.max(np.abs(pcm[:, 0] - pcm[:, 1]))))
        if len(pcm) < rate // 100:
            continue
        mono = pcm[:, 0]
        t = np.arange(len(mono)) / rate
        basis = np.column_stack((np.sin(2*np.pi*440*t), np.cos(2*np.pi*440*t),
                                 np.sin(2*np.pi*880*t), np.cos(2*np.pi*880*t), np.ones(len(t))))
        fit, *_ = np.linalg.lstsq(basis, mono, rcond=None)
        music.append(float(np.hypot(fit[0], fit[1])))
        speech.append(float(np.hypot(fit[2], fit[3])))
        power = np.abs(np.fft.rfft((mono-np.mean(mono))*np.hanning(len(mono))))**2
        frequency = np.fft.rfftfreq(len(mono), 1/rate)
        total = float(power.sum())
        energy.append(float(power[(frequency >= 380) & (frequency <= 500)].sum()/total) if total > 0 else 0)
        peaks.append(float(frequency[int(np.argmax(power))]))
        rms_values.append(float(np.sqrt(np.mean(mono**2))))
    require(frames >= rate*minimum_seconds and bool(music), 'Insufficient final-output PCM')
    require(channel_error <= 3, 'Final output has unexpected stereo channel differences')
    require(peak_sample < 32000, 'Final PCM clips or approaches clipping')
    return {'rate': rate, 'format': 'S16LE', 'channels': 2, 'observed_frames': frames,
            'analyzed_blocks': len(music), 'music_440_amplitude': float(np.median(music)),
            'speech_880_amplitude': float(np.median(speech)),
            'median_440_band_energy_fraction': float(np.median(energy)),
            'median_peak_hz': float(np.median(peaks)), 'median_rms': float(np.median(rms_values)),
            'max_abs_sample': peak_sample, 'max_channel_difference': channel_error}


def require_music(measured, floor):
    require(measured['music_440_amplitude'] > max(8, floor*8),
            'Final music does not rise measurably above the observed noise/quantization floor')
    require(415 <= measured['median_peak_hz'] <= 465 and measured['median_440_band_energy_fraction'] >= .65,
            'Actual final output lacks the required dominant 440Hz music probe')


def music_block(data, rate, floor):
    measured = spectrum([data], rate, minimum_seconds=.01)
    require(measured['music_440_amplitude'] > floor,
            'An individual final-output PCM block lost the continuous440 music probe')
    return measured


class SpectrumTransition:
    """Require consecutive actual PCM, rather than a health flag or fixed sleep."""
    def __init__(self, kind, trigger_at, baseline_amplitude, voice_floor, *, previous_voice=None, expected_ratio=None):
        require(kind in {'duck_voice', 'restore_no_voice', 'output_volume'}, 'Unknown final PCM transition')
        self.kind, self.trigger_at = kind, trigger_at
        self.baseline_amplitude, self.voice_floor = baseline_amplitude, voice_floor
        self.previous_voice = previous_voice
        self.expected_ratio = expected_ratio
        self.first_packet_at = self.first_match_at = self.stable_first_at = None
        self.last_at = None
        self.frames = self.blocks = self.examined = 0
        self.last_measured = None
        self.complete = False

    def push(self, data, at, rate):
        if at < self.trigger_at:
            return False
        require(self.last_at is None or at > self.last_at, 'Transition PCM callback times did not advance')
        if self.first_packet_at is None:
            self.first_packet_at = at
        self.last_at = at
        measured = spectrum([data], rate, minimum_seconds=.01)
        self.complete = False
        self.last_measured = measured
        self.examined += 1
        ratio = measured['music_440_amplitude']/self.baseline_amplitude
        if self.kind == 'duck_voice':
            matches = (.12 < ratio < .35
                       and measured['speech_880_amplitude'] > max(8., self.voice_floor*8))
        elif self.kind == 'restore_no_voice':
            require(self.previous_voice is not None, 'Restoration gate lacks observed speech reference')
            matches = (.85 < ratio < 1.15
                       and measured['speech_880_amplitude'] <= max(self.voice_floor*4, self.previous_voice*.15))
        else:
            require(self.expected_ratio is not None and self.expected_ratio > 0,
                    'Output-volume gate lacks the pinned gain-ratio reference')
            matches = (.85*self.expected_ratio < ratio < 1.15*self.expected_ratio
                       and measured['speech_880_amplitude'] <= max(8., self.voice_floor*4))
        if not matches:
            self.frames = self.blocks = 0
            self.stable_first_at = None
            return False
        if self.first_match_at is None:
            self.first_match_at = at
        if self.stable_first_at is None:
            self.stable_first_at = at
        self.frames += measured['observed_frames']
        self.blocks += 1
        self.complete = (self.blocks >= 3 and self.frames >= rate*.4
                         and at-self.stable_first_at >= .35)
        return self.complete

    def evidence(self, now):
        def latency(at):
            return round(at-self.trigger_at, 6) if at is not None else None
        return {'kind': self.kind, 'passed': self.complete,
                'first_packet_callback_seconds_after_trigger': latency(self.first_packet_at),
                'first_matching_callback_seconds_after_trigger': latency(self.first_match_at),
                'stable_window_first_callback_seconds_after_trigger': latency(self.stable_first_at),
                'gate_passed_seconds_after_trigger': latency(now) if self.complete else None,
                'consecutive_matching_frames': self.frames, 'consecutive_matching_blocks': self.blocks,
                'stable_callback_span_seconds': round(self.last_at-self.stable_first_at, 6)
                    if self.last_at is not None and self.stable_first_at is not None else None,
                'examined_blocks': self.examined, 'last_spectrum': self.last_measured,
                'latency_scope': 'Actual ALSA capture callback observations; includes API/transport/mixing/output buffering, not a physical acoustic deadline.'}


def private_source_tone(path, seconds=180):
    """Extend only this new private source before launch; keep the same probe."""
    mono = (8192*np.sin(2*np.pi*440*np.arange(base.RATE)/base.RATE)).astype('<i2')
    second = np.repeat(mono[:, None], 2, axis=1).astype('<i2').tobytes()
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(base.RATE)
        for _ in range(seconds):
            output.writeframesraw(second)
    path.chmod(0o600)


def output_hw_params():
    text = Path(f'/proc/asound/Loopback/pcm1p/sub{SLOT}/hw_params').read_text()
    fields = dict(line.split(':', 1) for line in text.splitlines() if ':' in line)
    require(fields.get('format', '').strip() == 'S16_LE' and fields.get('channels', '').strip() == '2',
            'OwnTone final output has not configured stereo S16LE hardware')
    rate = int(fields.get('rate', '').strip().split()[0])
    require(8000 <= rate <= 192000, 'OwnTone final output negotiated an invalid hardware rate')
    return {'format': 'S16_LE', 'channels': 2, 'rate': rate, 'playback': LOCAL_PLAYBACK}


def local_selection_evidence(observed, state, outputs):
    """Bounded diagnostic fields; never record control credentials or SDP."""
    runtime = observed.get('runtime', {})
    return {
        'saved_revision': observed.get('revision'),
        'saved_speaker_ids': [item['id'] for item in observed.get('speakers', [])],
        'api_runtime_status': runtime.get('status'),
        'api_runtime_error': str(runtime.get('error'))[:2000] if runtime.get('error') else None,
        'api_runtime_selected_ids': runtime.get('selected_ids'),
        'broker_status': state.status,
        'broker_error': str(state.error)[:2000] if state.error else None,
        'broker_selected_ids': list(state.selected_ids),
        'broker_desired_revision': state.desired.revision,
        'broker_desired_speaker_ids': [item.id for item in state.desired.speakers],
        'broker_applied_revision': state.applied.revision if state.applied else None,
        'broker_applied_speaker_ids': [item.id for item in state.applied.speakers] if state.applied else None,
        'actual_outputs': [
            {key: entry.get(key) for key in ('id', 'protocol', 'selected', 'assignable', 'volume', 'offset_ms')}
            | {'name': str(entry.get('name', ''))[:128]}
            for entry in outputs[:32]
        ],
        'actual_selected_ids': [entry['id'] for entry in outputs if entry.get('selected')],
        'actual_output_count': len(outputs),
    }


def exact_local_selected(observed, selected_ids, outputs):
    matches = [entry for entry in outputs if entry['selected']]
    require(all(entry['id'] == '0' and entry['protocol'] == 'alsa' for entry in matches),
            'A house/nonlocal output was selected')
    return (selected_ids == ['0'] and len(matches) == 1
            and bool(observed.get('speakers')) and observed['speakers'][0]['id'] == '0')


async def exercise(broker, state, source, source_client, target, queue_id, api, api_process,
                   capture, peer, tone, identity, placeholder_id, report):
    phase = 'awaiting_final_music'
    started = time.monotonic()
    initial_pids = base.owned_identities(broker, state, source)
    initial_api = api_process.evidence()
    initial_player = await state.client.request('GET', '/api/player')
    require(initial_player.get('state') == 'play' and type(initial_player.get('item_id')) is int,
            'Candidate OwnTone must already be playing before the continuity observation')
    output_item = initial_player['item_id']
    report['continuity_before'] = {'processes': initial_pids, 'api': initial_api,
                                   'candidate_player': initial_player}
    samples = []
    done = asyncio.Event()
    last_progress_at = started
    last_output_progress_at = started
    allowed_output_volumes = {TEST_OUTPUT_VOLUME}
    music_check_index = None
    music_presence_floor = None
    verified_music_blocks = 0
    minimum_music_amplitude = None

    def check_music_blocks():
        nonlocal music_check_index, verified_music_blocks, minimum_music_amplitude
        if music_check_index is None:
            return
        while music_check_index < len(capture.chunks):
            block = capture.chunks[music_check_index]
            measured = music_block(block, capture.rate, music_presence_floor)
            music_check_index += 1
            amplitude = measured['music_440_amplitude']
            verified_music_blocks += 1
            minimum_music_amplitude = amplitude if minimum_music_amplitude is None else min(minimum_music_amplitude, amplitude)
        report['per_block_music_continuity'] = {
            'verified_blocks': verified_music_blocks, 'minimum_440_amplitude': minimum_music_amplitude,
            'required_440_amplitude_floor': music_presence_floor,
            'scope': 'Every captured complete block after proven440 onset is fitted individually; no median can hide a silent block. Sub-block transients remain outside this observation.'}

    async def monitor():
        nonlocal last_progress_at, last_output_progress_at
        previous_source = previous_output = None
        while not done.is_set():
            capture.poll()
            check_music_blocks()
            player, source_outputs, candidate, outputs, health = await asyncio.gather(
                source_client.request('GET', '/api/player'), source_client.outputs(set()),
                state.client.request('GET', '/api/player'), state.client.outputs(set()),
                call_rpc(state.directory/'audio.sock', 'health', timeout=2))
            at = time.monotonic()
            require(base.owned_identities(broker, state, source) == initial_pids,
                    'An owned backend PID/birth changed during API speech')
            require(api_process.evidence() == initial_api, 'The rootless API process identity changed')
            require(player.get('state') == 'play' and player.get('item_id') == queue_id,
                    'AirPlay source stopped, paused or changed queue item')
            require(player.get('volume') == SOURCE_VOLUME, 'AirPlay source volume changed during observation')
            require({output['id'] for output in source_outputs if output['selected']} == {target['id']},
                    'AirPlay source selected an unintended destination')
            require(candidate.get('state') == 'play' and candidate.get('item_id') == output_item,
                    'Candidate OwnTone stopped, paused or replaced the room program')
            require(candidate.get('volume') in allowed_output_volumes,
                    'Candidate output volume differs from the controlled API test stage')
            require({output['id'] for output in outputs if output['selected']} == {'0'}
                    and state.selected_ids == ['0'], 'Candidate selected a nonlocal or unexpected output')
            require(health['ready'] and not health['error'] and health['music_active'] and health['audio_active'],
                    'Room worker lost healthy continuous music')
            require(health['speech_session_id'] in {None, identity['session_id']}, 'An unintended speech session appeared')
            progress, output_progress = player.get('item_progress_ms'), candidate.get('item_progress_ms')
            require(type(progress) is int and type(output_progress) is int, 'Backend omitted actual program progress')
            if previous_source is not None:
                require(progress >= previous_source, 'AirPlay source program moved backward or restarted')
                require(output_progress >= previous_output, 'Candidate output program moved backward or restarted')
                if progress > previous_source:
                    last_progress_at = at
                if output_progress > previous_output:
                    last_output_progress_at = at
                require(at-last_progress_at < 1.25 and at-last_output_progress_at < 1.25,
                        'A source or output program stopped advancing during speech')
            previous_source, previous_output = progress, output_progress
            evidence = capture.poll()
            samples.append({'at_seconds': round(at-started, 3), 'stage': phase,
                            'source_progress_ms': progress, 'output_progress_ms': output_progress,
                            'source_volume': player.get('volume'), 'output_volume': candidate.get('volume'),
                            'music_gain': health['music_gain'], 'speech_session_id': health['speech_session_id'],
                            'worker_dropped_bytes': health['dropped_bytes'],
                            'captured_bytes': evidence['observed_bytes']})
            await asyncio.sleep(.1)

    monitor_task = asyncio.create_task(monitor())

    async def healthy(*, allow_initial_empty=False):
        if monitor_task.done():
            await monitor_task
            raise base.RuntimeFailure('API speech continuity monitor ended unexpectedly')
        evidence = capture.poll()
        check_music_blocks()
        if not allow_initial_empty or capture.last_packet_at is not None:
            require(capture.last_packet_at is not None and time.monotonic()-capture.last_packet_at < .3,
                    'Final output has a capture gap above300ms')
        require(evidence['max_packet_gap_seconds'] < .3, 'Final-output capture has a packet gap above300ms')
        health = await call_rpc(state.directory/'audio.sock', 'health', timeout=2)
        require(health['ready'] and not health['error'], 'Room worker failed')
        return health

    async def gain_ready(value, session):
        health = await healthy()
        return health if abs(health['music_gain']-value) <= .025 and health['speech_session_id'] == session else None

    async def observe(label, duration=1.5):
        nonlocal phase
        phase = label
        first = len(capture.chunks)
        end = time.monotonic()+duration
        while time.monotonic() < end:
            await healthy()
            await asyncio.sleep(.02)
        measured = spectrum(capture.chunks[first:], capture.rate)
        measured['worker'] = await healthy()
        measured['capture'] = capture.poll()
        stage_samples = [sample for sample in samples if sample['stage'] == label]
        require(len(stage_samples) >= 2 and stage_samples[-1]['source_progress_ms'] > stage_samples[0]['source_progress_ms']+500,
                f'Actual AirPlay program did not advance during {label}')
        require(stage_samples[-1]['output_progress_ms'] > stage_samples[0]['output_progress_ms']+500,
                f'Actual final output program did not advance during {label}')
        report.setdefault('stages', {})[label] = measured
        return measured

    async def change_output_volume(value):
        nonlocal allowed_output_volumes, phase
        phase = f'volume_transition_to_{value}'
        allowed_output_volumes = allowed_output_volumes | {value}
        triggered_at = time.monotonic()
        await api.patch({'volume': value})
        stable_since = None
        async def settled():
            nonlocal stable_since
            await healthy()
            room = await api.room()
            player = await state.client.request('GET', '/api/player')
            stable = (room['volume'] == player.get('volume') == state.current_volume == value
                      and state.phone_volume_update is None and state.phone_volume_next is None)
            if not stable:
                stable_since = None
                return None
            if stable_since is None:
                stable_since = time.monotonic()
            return {'committed_room_volume': room['volume'], 'actual_output_volume': player['volume'],
                    'broker_volume': state.current_volume, 'room_revision': room['revision'],
                    'phone_receipts_settled': True} if time.monotonic()-stable_since >= .3 else None
        evidence = await base.eventually(settled, f'authenticated output volume{value} readback', timeout=5)
        allowed_output_volumes = {value}
        evidence['request_started_monotonic'] = triggered_at
        return evidence

    async def output_transition(label, kind, trigger_at, baseline_amplitude, voice_floor, *, previous_voice=None, expected_ratio=None):
        nonlocal phase
        phase = label
        gate = SpectrumTransition(kind, trigger_at, baseline_amplitude, voice_floor,
                                  previous_voice=previous_voice, expected_ratio=expected_ratio)
        index = 0
        # Include actual PCM that arrived while API/worker acknowledgments were
        # in flight, but never reuse buffers captured before the action trigger.
        while index < len(capture.chunks) and capture.captured_at[index] < trigger_at:
            index += 1
        async def run_gate():
            nonlocal index
            while True:
                await healthy()
                while index < len(capture.chunks):
                    data, at = capture.chunks[index], capture.captured_at[index]
                    index += 1
                    complete = gate.push(data, at, capture.rate)
                    report.setdefault('output_transitions', {})[label] = gate.evidence(time.monotonic())
                    if complete and time.monotonic()-at < .25:
                        return report['output_transitions'][label]
                await asyncio.sleep(.02)
        try:
            return await asyncio.wait_for(run_gate(), 8)
        except asyncio.TimeoutError as exc:
            raise base.RuntimeFailure(f'Timed out waiting for actual final PCM {label}; last spectrum retained privately') from exc
        finally:
            report.setdefault('output_transitions', {})[label] = gate.evidence(time.monotonic())

    try:
        phase = 'awaiting_first_final_pcm'
        await asyncio.sleep(0)  # Schedule control observation before ALSA starts.
        capture.start()
        async def first_pcm():
            while True:
                await healthy(allow_initial_empty=True)
                report['capture_startup_last_observed'] = capture.poll()
                if capture.rate and capture.total >= capture.rate//10*4:
                    return
                await asyncio.sleep(.02)
        try:
            await asyncio.wait_for(first_pcm(), 8)
        except asyncio.TimeoutError as exc:
            raise base.RuntimeFailure('Bounded first-final-PCM readiness did not receive100ms of real ALSA frames') from exc
        report['capture_startup'] = {
            'first_packet_callback_seconds_after_continuity_started': round(capture.captured_at[0]-started, 6),
            'readiness_seconds_after_continuity_started': round(time.monotonic()-started, 6),
            'note': 'Control monitor active before firstPCM; after first packet every observed callback gap remains below300ms.'}
        report['capture_negotiated'] = capture.poll()
        require(capture.rate == report['output_hw_params_before_capture']['rate'],
                'Capture rate does not match actual OwnTone output hardware rate')
        report['output_hw_params_with_capture'] = output_hw_params()
        phase = 'awaiting_final_music'
        # Evaluate real post-OwnTone output; do not borrow the upstream FIFO's
        # absolute amplitudes or treat a play flag as audible music.
        index = len(capture.chunks)
        quiet_music = []
        qualifying_frames = qualifying_blocks = 0
        first_audible = None
        end = time.monotonic()+20
        while time.monotonic() < end:
            await healthy()
            while index < len(capture.chunks):
                block, observed_at = capture.chunks[index], capture.captured_at[index]
                index += 1
                if len(block) < capture.rate//100*4:
                    continue
                measured = spectrum([block], capture.rate, minimum_seconds=.01)
                if measured['median_440_band_energy_fraction'] < .15:
                    quiet_music.append(measured['music_440_amplitude'])
                floor = max(1., float(np.median(quiet_music))) if quiet_music else 1.
                strong = (measured['music_440_amplitude'] > max(8., floor*8)
                          and 415 <= measured['median_peak_hz'] <= 465
                          and measured['median_440_band_energy_fraction'] >= .65
                          and time.monotonic()-observed_at < .25)
                if strong:
                    if qualifying_blocks == 0:
                        first_audible = observed_at
                    qualifying_blocks += 1
                    qualifying_frames += measured['observed_frames']
                else:
                    qualifying_blocks = qualifying_frames = 0
                    first_audible = None
                if qualifying_blocks >= 3 and qualifying_frames >= capture.rate*.25:
                    report['output_onset'] = {'seconds_after_continuity_started': round(first_audible-started, 6),
                        'gate_passed_seconds': round(time.monotonic()-started, 6), 'last_block': measured,
                        'qualifying_frames': qualifying_frames, 'qualifying_blocks': qualifying_blocks,
                        'noise_440_amplitude_floor': floor, 'quiet_blocks_observed': len(quiet_music),
                        'threshold_justification': 'Eight-times measured quiet440 fit or one S16 quantization count, minimum8 counts; strict dominant440 spectral gate retained. Output volume was configured before baseline only.'}
                    break
            if report.get('output_onset'):
                break
            await asyncio.sleep(.02)
        require(bool(report.get('output_onset')), 'Bounded actual final-output440 onset gate did not pass')
        music_check_index = index
        music_presence_floor = max(8., report['output_onset']['noise_440_amplitude_floor']*8)
        await base.eventually(lambda: gain_ready(1., None), 'baseline worker gain', timeout=3)
        full = await observe('volume_100_initial')
        require_music(full, report['output_onset']['noise_440_amplitude_floor'])
        expected_half = (.5)**SOFTWARE_VOLUME_EXPONENT
        pre_speech_voice_floor = max(1., full['speech_880_amplitude'])
        at_half = await change_output_volume(50)
        await output_transition('output_volume_50', 'output_volume', at_half['request_started_monotonic'],
                                full['music_440_amplitude'], pre_speech_voice_floor, expected_ratio=expected_half)
        half = await observe('volume_50_applied')
        require_music(half, report['output_onset']['noise_440_amplitude_floor'])
        half_ratio = half['music_440_amplitude']/full['music_440_amplitude']
        require(.85*expected_half < half_ratio < 1.15*expected_half,
                'Actual final PCM does not follow the pinned cubic local-output volume50 gain')
        restored_volume = await change_output_volume(TEST_OUTPUT_VOLUME)
        await output_transition('output_volume_100_restored', 'output_volume', restored_volume['request_started_monotonic'],
                                full['music_440_amplitude'], pre_speech_voice_floor, expected_ratio=1.)
        baseline = await observe('baseline')
        require_music(baseline, report['output_onset']['noise_440_amplitude_floor'])
        restored_ratio = baseline['music_440_amplitude']/full['music_440_amplitude']
        require(.85 < restored_ratio < 1.15, 'Final local-output volume100 did not restore unity amplitude')
        for measured in (full, half, baseline):
            require(abs(measured['worker']['music_gain']-1.) <= .025
                    and measured['worker']['speech_session_id'] is None,
                    'Output-volume evidence changed upstream room ducking or speech ownership')
        report['local_output_volume_control'] = {
            'api_volume_sequence': [100, 50, 100], 'gain_curve': '(percent/100)^3',
            'expected_50_amplitude_ratio': expected_half, 'measured_50_amplitude_ratio': half_ratio,
            'measured_restored_100_amplitude_ratio': restored_ratio,
            'volume_50_readback': at_half, 'volume_100_restored_readback': restored_volume,
            'source_volume_unchanged': SOURCE_VOLUME, 'worker_music_gain_unchanged': 1.,
            'scope': 'Authenticated API local-only volume changed final ALSA PCM; same source/program/processes, raw FIFO never read by this harness.'}
        voice_floor = max(1., baseline['speech_880_amplitude'])
        report['voice_threshold'] = {'baseline_880_amplitude_floor': voice_floor,
                                    'minimum_voice_amplitude': max(8., voice_floor*8),
                                    'justification': 'Decoded880 must exceed8x measured baseline880/quantization floor, remain unclipped, then disappear during same-session silence and close.'}
        phase = 'negotiating_api_speech'
        await asyncio.wait_for(peer.setLocalDescription(await peer.createOffer()), 8)
        offer = {**identity, 'action': 'offer', 'sdp': peer.localDescription.sdp, 'type': 'offer'}
        # A disabled reservation cannot be used as a speech destination.
        denied = await api.request('POST', f'/api/v1/rooms/{placeholder_id}/speech', json=offer, expected=409)
        require(denied.get('code') == 'conflict', 'Disabled destination was not rejected by domain admission')
        missing = await api.request('POST', '/api/v1/nobly/rooms/not-this-test-binding/speech', json=offer, expected=404)
        require(missing.get('code') == 'not_found', 'Unknown external binding was not rejected')
        speech_trigger = time.monotonic()
        answer = await api.request('POST', f'/api/v1/rooms/{ROOM_ID}/speech', json=offer)
        require(answer.get('admitted_room_id') == ROOM_ID, 'API did not acknowledge the exact admitted room UUID')
        codecs = SessionDescription.parse(answer['sdp']).media[0].rtp.codecs
        require(codecs and all(codec.mimeType.lower() == 'audio/opus' for codec in codecs), 'API speech did not negotiate forced Opus')
        await asyncio.wait_for(peer.setRemoteDescription(RTCSessionDescription(sdp=answer['sdp'], type=answer['type'])), 4)
        del offer, answer
        async def connected():
            health = await gain_ready(.2, identity['session_id'])
            return health if peer.connectionState == 'connected' else None
        await base.eventually(connected, 'API/broker Opus admission and real music ducking', timeout=12)
        report['speech_worker_duck_confirmation_seconds_after_offer_started'] = round(time.monotonic()-speech_trigger, 6)
        require(broker.sessions.get(identity['session_id']) == ROOM_ID, 'Broker session ownership differs from API admission')
        wrong = await api.request('POST', f'/api/v1/rooms/{placeholder_id}/speech',
                                 json={**identity, 'action': 'close', 'request_id': f'wrong-close-{uuid4()}'}, expected=409)
        require(wrong.get('code') == 'session_conflict', 'Wrong-room close was not rejected by broker ownership')
        require(broker.sessions.get(identity['session_id']) == ROOM_ID, 'Wrong-room close altered the admitted owner')
        report['destination_guards'] = {'disabled_offer_http': 409, 'unknown_binding_http': 404,
                                        'wrong_room_close_http': 409, 'wrong_room_close_code': 'session_conflict',
                                        'admitted_room_id': ROOM_ID}
        await output_transition('speech_duck_and_voice', 'duck_voice', speech_trigger,
                                baseline['music_440_amplitude'], voice_floor)
        mixed = await observe('api_speech_ducked')
        ratio = mixed['music_440_amplitude']/baseline['music_440_amplitude']
        mixed['music_ratio_to_baseline'] = ratio
        require(.12 < ratio < .35, 'Final OwnTone ALSA music did not follow requested0.2 duck gain')
        require(mixed['speech_880_amplitude'] > max(8., voice_floor*8), 'Decoded Opus voice did not rise above final-output baseline noise')
        phase = 'silence_tail'
        silence_trigger = time.monotonic()
        tone.silent = True
        await base.eventually(lambda: gain_ready(1., identity['session_id']), 'same-session silence restores worker music gain', timeout=4)
        report['silence_worker_restore_confirmation_seconds_after_trigger'] = round(time.monotonic()-silence_trigger, 6)
        await output_transition('same_session_silence_restore', 'restore_no_voice', silence_trigger,
                                baseline['music_440_amplitude'], voice_floor,
                                previous_voice=mixed['speech_880_amplitude'])
        silent = await observe('silence_connected')
        silent['music_ratio_to_baseline'] = silent['music_440_amplitude']/baseline['music_440_amplitude']
        require(.85 < silent['music_ratio_to_baseline'] < 1.15, 'Final output music did not restore during silence')
        require(silent['speech_880_amplitude'] <= max(voice_floor*4, mixed['speech_880_amplitude']*.15),
                'Final output retained audible speech after bounded silence tail')
        require(peer.connectionState == 'connected', 'Healthy speech session disconnected during silence')
        # Resume this exact healthy session so close is tested while audible;
        # closing an already-silent stream would not prove final speech drains.
        phase = 'same_session_voice_resumed'
        resume_trigger = time.monotonic()
        tone.silent = False
        await base.eventually(lambda: gain_ready(.2, identity['session_id']), 'same-session resumed voice ducks worker', timeout=4)
        await output_transition('same_session_voice_resumed', 'duck_voice', resume_trigger,
                                baseline['music_440_amplitude'], voice_floor)
        resumed = await observe('resumed_before_close')
        resumed['music_ratio_to_baseline'] = resumed['music_440_amplitude']/baseline['music_440_amplitude']
        require(.12 < resumed['music_ratio_to_baseline'] < .35
                and resumed['speech_880_amplitude'] > max(8., voice_floor*8),
                'Actual resumed speech was not audible and ducked before API close')
        stats = await peer.getStats()
        sent = sum(item.packetsSent for item in stats.values() if item.type == 'outbound-rtp')
        require(sent >= 50, 'Actual speech RTP evidence is too short')
        phase = 'api_speech_close'
        close_trigger = time.monotonic()
        await api.request('POST', f'/api/v1/rooms/{ROOM_ID}/speech',
                          json={**identity, 'action': 'close', 'request_id': f'close-{uuid4()}'})
        tone.stop()
        await asyncio.wait_for(peer.close(), 4)
        await base.eventually(lambda: gain_ready(1., None), 'API close releases worker', timeout=4)
        report['close_worker_restore_confirmation_seconds_after_trigger'] = round(time.monotonic()-close_trigger, 6)
        await output_transition('api_close_restore', 'restore_no_voice', close_trigger,
                                baseline['music_440_amplitude'], voice_floor,
                                previous_voice=resumed['speech_880_amplitude'])
        restored = await observe('closed_restored')
        restored['music_ratio_to_baseline'] = restored['music_440_amplitude']/baseline['music_440_amplitude']
        require(.85 < restored['music_ratio_to_baseline'] < 1.15, 'Final output music did not remain restored after close')
        require(restored['speech_880_amplitude'] <= max(voice_floor*4, mixed['speech_880_amplitude']*.15),
                'Final output retained closed speech media')
        require(peer.connectionState == 'closed' and identity['session_id'] not in broker.sessions, 'API/broker speech ownership survived healthy close')
        report['webrtc'] = {'codec': 'audio/opus', 'rtp_packets_sent': sent,
                            'peer_state_after_close': peer.connectionState, 'admitted_room_id': ROOM_ID}
        report['continuity_after'] = {'processes': base.owned_identities(broker, state, source),
                                      'api': api_process.evidence(), 'candidate_player': await state.client.request('GET', '/api/player')}
        report['capture'] = capture.poll()
        report['scope_note'] = 'Actual authenticated rootless HTTPAPI/broker/worker and selected OwnTone ALSA output; source is synthetic, output is kernel Loopback, no house speaker or native-phone/group proof. About100ms control polling cannot rule out shorter control transients.'
    finally:
        done.set()
        if not monitor_task.done():
            monitor_task.cancel()
        outcomes = await asyncio.gather(monitor_task, return_exceptions=True)
        report['program_progress'] = samples
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                raise outcome


async def check():
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'passed': False,
              'scope': 'Synthetic AirPlay2 input plus authenticated rootless API->broker->worker Opus speech->actual OwnTone ALSA reverseLoopback7 PCM',
              'stock_phone_verified': False, 'physical_speaker_output_verified': False,
              'native_multi_zone_grouping_verified': False, 'cast_input_verified': False,
              'least_privilege_daemons_verified': False, 'room_id': ROOM_ID, 'slot': SLOT,
              'cleanup': {'speech_session_closed': False, 'speech_peer_closed': False,
                          'source_stopped': False, 'api_stopped': False, 'capture_null': False,
                          'broker_closed': False, 'empty_manifest': False,
                          'slot_closed': False, 'host_preserved': False}}
    broker = source = source_client = api = api_process = capture = peer = tone = None
    api_directory = source_directory = state = None
    identity = None
    token = None
    baseline = None
    placeholders = []
    complete = False
    cleanup_errors = []
    try:
        require(sys.platform == 'linux' and os.geteuid() == 0, 'Run explicitly as Linux root')
        account, group = pwd.getpwnam('shiri'), grp.getgrnam('shiri')
        require(account.pw_uid > 0 and account.pw_gid == group.gr_gid, 'Existing nonroot shiri account/group is required')
        root_directory(STATE)
        root_directory(RUN, 0o750)
        manifest_path = STATE/'ownership.json'
        require(manifest_path.is_file(), 'Existing candidate installation manifest is required')
        manifest = json.loads(manifest_path.read_text())
        installation_id = manifest.get('installation_id', '')
        require(installation_id.startswith('b265'), 'Known candidate installation identity does not match')
        require(not manifest.get('networks') and not manifest.get('processes'), 'Candidate retains resources; do not run concurrent harnesses')
        base.closed_slot()
        baseline = await base.host_snapshot()
        report['installation_id'] = installation_id
        source_directory = Path(tempfile.mkdtemp(prefix='api-airplay-source-', dir=STATE))
        source_directory.chmod(0o700)
        api_directory = Path(tempfile.mkdtemp(prefix='shiri-v2-api-tts-', dir='/tmp'))
        os.chown(api_directory, 0, group.gr_gid)
        api_directory.chmod(0o750)
        report['artifacts'] = {'private_source_directory': str(source_directory), 'api_directory': str(api_directory)}
        config = Settings(state_dir=STATE/'unused-api', runtime_state_dir=STATE, runtime_dir=RUN,
                          runtime_socket=RUN/'runtime.sock', binary_dir=BINARIES)
        broker = Broker(config)
        health = await broker.start(serve=True)
        require(health['ready'], health.get('error') or 'Candidate broker did not become ready')
        require(broker.network.installation_id == installation_id, 'Installation identity changed')
        report['versions'] = health['versions']
        api_process, token, placeholders, api_log = await launch_api(api_directory, config, account, group)
        report['artifacts']['api_log'] = str(api_log)
        api = Api()
        async def api_ready():
            require(api_process.alive, 'Rootless API exited; inspect the private API log')
            try:
                response = await api.client.get('/api/v1/health/live')
            except httpx.HTTPError:
                return None
            return response.status_code == 200
        await base.eventually(api_ready, 'actual rootless API listener', timeout=20)
        report['api_process'] = api_process.evidence()
        denied = await api.request('GET', '/api/v1/state', expected=401)
        require(denied.get('code') == 'unauthorized', 'Anonymous API request was not denied')
        await api.request('POST', '/api/v1/session', json={'token': token})
        cookies = list(api.client.cookies.jar)
        require(cookies and any('httponly' in {key.lower() for key in cookie._rest} for cookie in cookies),
                'Authenticated API did not issue an HttpOnly session cookie')
        view = await api.request('GET', '/api/v1/state')
        require(len(view['rooms']) == 8 and all(not room['enabled'] for room in view['rooms']),
                'Disposable API seed differs from eight disabled room definitions')
        report['authentication'] = {'anonymous_state_http': 401, 'session_login_http': 200,
                                    'authenticated_state_http': 200, 'httponly_cookie': True}
        await api.patch({'enabled': True, 'duck_gain': .2})
        async def room_ready():
            observed = await api.room()
            status = observed['runtime']['status']
            require(status not in {'error', 'degraded'}, observed['runtime'].get('error') or 'Room startup failed')
            return observed if status == 'running' else None
        observed = await base.eventually(room_ready, 'API-enabled slot7 room startup', timeout=65)
        require(observed['slot'] == SLOT and observed['local_audio_device'] == LOCAL_PLAYBACK,
                'API room changed the exact virtual playback endpoint')
        state = broker.rooms[ROOM_ID]
        report['receiver'] = {key: state.receiver[key] for key in ('namespace', 'interface', 'mac', 'ip', 'alias')}
        discovered = await api.request('GET', f'/api/v1/rooms/{ROOM_ID}/speakers')
        local = [entry for entry in discovered['outputs'] if entry['id'] == '0']
        require(len(local) == 1 and local[0]['protocol'] == 'alsa' and local[0]['assignable'],
                'Configured final virtual ALSA output was not discovered as assignable local0')
        for _ in range(5):
            observed = await api.room()
            response = await asyncio.wait_for(api.client.put(f'/api/v1/rooms/{ROOM_ID}/speakers',
                json={'expected_revision': observed['revision'], 'speaker_ids': ['0']}), 30)
            if response.status_code == 409:
                await asyncio.sleep(.1)
                continue
            require(response.status_code == 200, f'Authenticated output assignment returned HTTP {response.status_code}')
            assigned = response.json()
            require(assigned.get('runtime_accepted') is True, 'API local assignment was not accepted by broker')
            report['api_assignment_ack'] = {
                'runtime_accepted': assigned['runtime_accepted'],
                'saved_room_revision': assigned['room']['revision'],
                'saved_speaker_ids': [item['id'] for item in assigned['room']['speakers']],
            }
            break
        else:
            raise base.RuntimeFailure('API assignment revision did not settle')
        async def local_selected():
            observed = await api.room()
            outputs = await state.client.outputs(set())
            evidence = local_selection_evidence(observed, state, outputs)
            evidence['tracked_broker_room_identity_matches'] = broker.rooms.get(ROOM_ID) is state
            report['local_selection_last_observed'] = evidence
            trace = report.setdefault('local_selection_changes', [])
            if (not trace or trace[-1] != evidence) and len(trace) < 24:
                trace.append(evidence)
            ready = exact_local_selected(observed, state.selected_ids, outputs)
            if not ready:
                player = await state.client.request('GET', '/api/player')
                report['local_selection_last_player'] = {
                    key: player.get(key) for key in ('state', 'volume', 'item_id', 'item_progress_ms')}
            return observed if ready else None
        report['api_assigned_room'] = await base.eventually(local_selected, 'exact API/broker local0 selection', timeout=10)

        source_config, wav = base.source_config(source_directory, broker.sender, broker._password)
        private_source_tone(wav)
        report['private_source_probe_duration_seconds'] = 180
        report['artifacts']['source_wav'] = str(wav)
        source = await broker._start_process(base.SOURCE_KEY, 'validation-source',
            isolated_command(STATE/'sender'/'isolation', broker.sender['namespace'], [
                broker.binary('owntone'), '-f', '-c', str(source_config),
                '--mdns-no-rsp', '--mdns-no-daap', '--mdns-no-web', '--mdns-no-cname']), source_directory)
        source_client = OwnToneClient(f"http://{broker.sender['api_ip']}:{base.SOURCE_PORT}", password=broker._password)
        async def source_ready():
            require(source.alive, 'Synthetic source exited; inspect its private log')
            return await source_client.request('GET', '/api/player')
        await base.eventually(source_ready, 'synthetic OwnTone source', timeout=35)
        await broker._remember_process(base.SOURCE_KEY, source)
        await source_client.select([], await source_client.outputs(set()))
        async def discover():
            outputs = await source_client.outputs(set())
            matches = [entry for entry in outputs if entry['name'] == NAME]
            require(len(matches) <= 1, 'AirPlay receiver name is ambiguous')
            if not matches:
                return None
            target = matches[0]
            require(target['protocol'] == 'airplay2', 'Synthetic source discovered RAOP fallback rather than AirPlay2')
            require(target['id'] == str(int(state.receiver['mac'].replace(':', ''), 16)),
                    'AirPlay receiver name matches but MAC-derived identity differs')
            require(not target['requires_auth'], 'Candidate receiver requires unavailable authorization')
            return target
        target = await base.eventually(discover, 'exact named/MAC AirPlay2 receiver', timeout=40)
        await source_client.select([SpeakerRef(id=target['id'], name=NAME, protocol='airplay2')],
                                   await source_client.outputs(set()))
        await source_client.volume(SOURCE_VOLUME)
        require((await source_client.request('GET', '/api/player')).get('volume') == SOURCE_VOLUME,
                'Source volume was not retained after selection')
        report['source_target'] = {key: target[key] for key in ('id', 'name', 'protocol')}
        async def library_ready():
            library = await source_client.request('GET', '/api/library')
            require(library.get('songs', 0) <= 1, 'Private source indexed unexpected media')
            return library if library.get('songs') == 1 and not library.get('updating') else None
        await base.eventually(library_ready, 'private source tone scan', timeout=35)
        queued = await source_client.request('POST', '/api/queue/items/add', params={
            'expression': 'media_kind is music', 'limit': '1', 'playback': 'start', 'clear': 'true'})
        require(queued.get('count') == 1 and len(queued.get('items', [])) == 1,
                'Synthetic source did not queue exactly one probe')
        require(queued['items'][0].get('path') == str(wav), 'Queued source differs from generated private probe')
        async def actual_players():
            source_player, candidate_player = await asyncio.gather(
                source_client.request('GET', '/api/player'), state.client.request('GET', '/api/player'))
            return (source_player, candidate_player) if source_player.get('state') == candidate_player.get('state') == 'play' else None
        players = await base.eventually(actual_players, 'actual source and final OwnTone play state', timeout=25)
        report['players_after_setup'] = {'source': players[0], 'candidate': players[1]}
        report['output_hw_params_before_capture'] = output_hw_params()
        # Before baseline only, override protocol volume20 with an authenticated
        # nominal0dB room volume. This affects only the verified Loopback local0.
        await api.patch({'volume': TEST_OUTPUT_VOLUME})
        stable_started = None
        async def volume_settled():
            nonlocal stable_started
            saved = await api.room()
            player = await state.client.request('GET', '/api/player')
            stable = (saved['volume'] == TEST_OUTPUT_VOLUME and player.get('volume') == TEST_OUTPUT_VOLUME
                      and state.current_volume == TEST_OUTPUT_VOLUME
                      and state.phone_volume_update is None and state.phone_volume_next is None)
            if not stable:
                stable_started = None
                return None
            if stable_started is None:
                stable_started = time.monotonic()
            return {'source_volume': SOURCE_VOLUME, 'committed_room_volume': saved['volume'],
                    'actual_output_volume': player['volume'], 'room_revision': saved['revision'],
                    'phone_receipts_settled': True,
                    'note': 'Authenticated room volume configured before baseline only; output is exclusively virtualLoopback slot7 localID0, not a house speaker.'} if time.monotonic()-stable_started >= .6 else None
        report['volume_baseline_configuration'] = await base.eventually(volume_settled, 'phone receipt and API output volume settling', timeout=12)
        require((await source_client.request('GET', '/api/player')).get('volume') == SOURCE_VOLUME,
                'Settling output volume changed the source volume')
        capture = OutputCapture(start=False)
        peer, tone = base.make_speech_peer()
        identity = {'session_id': f'api-tts-{uuid4()}', 'request_id': f'offer-{uuid4()}'}
        await exercise(broker, state, source, source_client, target, queued['items'][0]['id'],
                       api, api_process, capture, peer, tone, identity, placeholders[0], report)
        complete = True
    except BaseException as exc:
        report['failure'] = {'type': type(exc).__name__, 'message': redact_exception(exc, token, broker._password if broker else None)}
    finally:
        if identity and api:
            try:
                await api.request('POST', f'/api/v1/rooms/{ROOM_ID}/speech',
                                  json={**identity, 'action': 'close', 'request_id': f'cleanup-{uuid4()}'})
                report['cleanup']['speech_session_closed'] = True
            except Exception as exc:
                cleanup_errors.append(f'API speech close: {redact_exception(exc, token)}')
        if tone:
            tone.stop()
        if peer:
            try:
                await asyncio.wait_for(peer.close(), 4)
                require(peer.connectionState == 'closed', 'Owned speech peer survived close')
                report['cleanup']['speech_peer_closed'] = True
            except Exception as exc:
                cleanup_errors.append(f'speech peer: {type(exc).__name__}')
        if source_client:
            with suppress(Exception):
                await source_client.request('PUT', '/api/player/stop')
                await source_client.select([], await source_client.outputs(set()))
            try:
                await source_client.close()
            except Exception as exc:
                cleanup_errors.append(f'source control: {type(exc).__name__}')
        if capture:
            try:
                capture.poll()
                pcm = source_directory/'observed-final-alsa-s16le-stereo.pcm'
                with pcm.open('xb') as output:
                    for chunk in capture.chunks:
                        output.write(chunk)
                pcm.chmod(0o600)
                report['artifacts']['output_pcm'] = str(pcm)
                report['capture_pcm_sha256'] = hashlib.sha256(pcm.read_bytes()).hexdigest()
            except Exception as exc:
                cleanup_errors.append(f'output artifact: {redact_exception(exc, token)}')
            try:
                await asyncio.wait_for(asyncio.to_thread(capture.close), 3)
                require(capture.pipeline.get_state(0).state == capture.Gst.State.NULL, 'Final capture did not reachNULL')
                report['cleanup']['capture_null'] = True
            except Exception as exc:
                cleanup_errors.append(f'final capture: {redact_exception(exc, token)}')
        if source:
            try:
                await asyncio.wait_for(source.stop(), 10)
                require(not source.alive, 'Owned synthetic source survived stop')
                broker.network.forget_process(base.SOURCE_KEY)
                report['cleanup']['source_stopped'] = True
            except Exception as exc:
                cleanup_errors.append(f'source: {redact_exception(exc, token)}')
        if api and api_process and api_process.alive:
            try:
                await api.patch({'enabled': False})
                async def disabled():
                    room = await api.room()
                    return room if room['runtime']['status'] == 'stopped' else None
                await base.eventually(disabled, 'API-disabled room releases actual runtime', timeout=35)
                view = await api.request('GET', '/api/v1/state')
                for room in view['rooms']:
                    require(not room['enabled'], 'Disposable room unexpectedly remains enabled')
                    await api.request('DELETE', f"/api/v1/rooms/{room['id']}?expected_revision={room['revision']}")
                require(not (await api.request('GET', '/api/v1/state'))['rooms'], 'Disposable API room definitions remain')
                report['cleanup']['api_disposable_rooms_deleted'] = True
            except Exception as exc:
                cleanup_errors.append(f'API desired-state cleanup: {redact_exception(exc, token)}')
        if api_process:
            try:
                await api_process.stop()
                report['cleanup']['api_stopped'] = True
            except Exception as exc:
                cleanup_errors.append(f'rootless API: {redact_exception(exc, token)}')
        if api:
            await api.close()
        if broker:
            try:
                await asyncio.wait_for(broker.close(), 45)
                report['cleanup']['broker_closed'] = True
            except Exception as exc:
                cleanup_errors.append(f'broker: {redact_exception(exc, token, broker._password)}')
        if baseline is not None:
            try:
                current = json.loads((STATE/'ownership.json').read_text())
                require(current['installation_id'] == report['installation_id'], 'Installation identity changed during cleanup')
                require(not current['networks'] and not current['processes'], 'Owned resources remain after cleanup')
                report['cleanup']['empty_manifest'] = True
                base.closed_slot()
                report['cleanup']['slot_closed'] = True
                require(await base.host_snapshot() == baseline, 'Host link/address/namespace baseline changed')
                report['cleanup']['host_preserved'] = True
            except Exception as exc:
                cleanup_errors.append(f'cleanup verification: {redact_exception(exc, token)}')
        if cleanup_errors:
            report['cleanup_errors'] = cleanup_errors
        absent = {}
        for name, resource in (
            ('speech_session_closed', identity), ('speech_peer_closed', peer),
            ('source_stopped', source), ('api_stopped', api_process),
            ('capture_null', capture), ('broker_closed', broker),
        ):
            if resource is None:
                report['cleanup'][name] = True
                absent[name] = 'Not created; no resource requires release'
        if absent:
            report['cleanup_absent_resources'] = absent
        report['passed'] = complete and not cleanup_errors and all(report['cleanup'].values())
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        atomic_json(RESULT, report)
        print(json.dumps({'passed': report['passed'], 'result': str(RESULT),
                          'failure': report.get('failure'), 'cleanup_errors': report.get('cleanup_errors')}), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING, format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('aiortc').setLevel(logging.WARNING)
    logging.getLogger('aioice').setLevel(logging.WARNING)
    raise SystemExit(asyncio.run(check()))
