"""Opt-in real two-zone speech stress; import creates no media/kernel resources.

The grouped harness supplies its already owned isolated fixture. This helper
changes no production code and counts only sustained, observed final PCM.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from functools import lru_cache
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import numpy as np

from shiri.rpc import call_rpc
from shiri.runtime.system import RuntimeFailure

RATE = 48000
ROUNDS = 20
CAPTURE_BYTES = 128*1024*1024
PRODUCER_SECONDS = 420
STRESS_SECONDS = 360
INNER_SECONDS = 480
CONTROL_SAMPLES = 20000
ARTIFACT_METADATA_BYTES = 1024*1024
ARTIFACT_PCM_BYTES = 16*1024*1024

reference_spec = importlib.util.spec_from_file_location('shiri_manual_speech_reference',
    Path(__file__).with_name('native_speech_reference.py'))
reference = importlib.util.module_from_spec(reference_spec)
sys.modules[reference_spec.name] = reference
reference_spec.loader.exec_module(reference)
SOURCE_RECEIPTS = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in (Path(__file__), Path(reference.__file__))}


def require(value, message):
    if not value:
        raise RuntimeFailure(message)


def retain_bytes(directory, name, parts, limit):
    """Exclusive, bounded regular-file retention in one fresh private directory."""
    path, temporary = directory/name, directory/(name+'.tmp')
    digest, total = hashlib.sha256(), 0
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        for part in parts:
            require(type(part) is bytes or isinstance(part, memoryview) and part.readonly,
                    'Stress retained artifact is not immutable PCM')
            total += len(part)
            require(total <= limit, 'Stress retained artifact exceeded its fixed byte bound')
            stream.write(part)
            digest.update(part)
    require(not path.exists(), 'Stress artifact attempted to overwrite retained evidence')
    os.rename(temporary, path)
    path.chmod(0o400)
    return {'path': str(path), 'bytes': total, 'sha256': digest.hexdigest()}


def retain_json(directory, name, value):
    data = (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)+'\n').encode()
    require(len(data) <= ARTIFACT_METADATA_BYTES, 'Stress replay metadata exceeded its1MiB bound')
    return retain_bytes(directory, name, (data,), ARTIFACT_METADATA_BYTES)


def retain_session(root, number, session, capture, carrier, evidence, baseline):
    """Retain replay inputs before any analysis thread or decoder/reference clear."""
    root = Path(root).resolve(strict=True)
    require(root.is_dir() and root.stat().st_uid == os.getuid() and root.stat().st_mode & 0o022 == 0,
            'Stress replay directory is not owned and private')
    directory = root/f'speech-{number:02d}-{session.identity["session_id"]}'
    directory.mkdir(mode=0o700)
    evidence['replay_artifacts'] = {'directory': str(directory), 'retained_before_analysis': False}
    artifacts = evidence['replay_artifacts']
    require(type(session.blocks) is tuple and all(type(data) is bytes and len(data) == 3840 for data in session.blocks)
            and sum(map(len, session.blocks)) <= reference.MAX_SESSION_CAPTURE_BYTES,
            'Stress replay capture exceeded its immutable8MiB bound')
    require(session.snapshot.pcm.nbytes <= reference.REFERENCE_BYTES and not session.snapshot.pcm.flags.writeable,
            'Stress replay reference exceeded its immutable8MiB bound')
    artifacts['decoded_mono'] = retain_bytes(directory, 'decoded-mono-s16.pcm',
        (memoryview(session.snapshot.pcm).cast('B'),), reference.REFERENCE_BYTES)
    artifacts['final_stereo'] = retain_bytes(directory, 'final-stereo-s16.pcm', session.blocks,
        reference.MAX_SESSION_CAPTURE_BYTES)
    period = carrier.period.astype('<i2', copy=False).tobytes()
    artifacts['carrier_mono'] = retain_bytes(directory, 'carrier-period-s16.pcm', (period,), reference.PERIOD*2)
    require(sum(artifacts[key]['bytes'] for key in ('decoded_mono', 'final_stereo', 'carrier_mono')) <= ARTIFACT_PCM_BYTES,
            'Stress replay PCM exceeded its16MiB per-session bound')
    rows, frame, byte_at = [], session.first_frame, 0
    times = tuple(capture.captured_at[session.start_index:session.start_index+len(session.blocks)])
    require(len(times) == len(session.blocks), 'Stress replay omitted capture callback mapping')
    for at, data in zip(times, session.blocks, strict=True):
        metadata = capture.buffer_metadata[at]
        absolute = capture.absolute[at]
        require(type(absolute) is int and absolute >= 0
                and all(type(metadata[key]) is int and metadata[key] >= 0 for key in ('offset', 'offset_end', 'pts', 'duration'))
                and metadata['offset_end']-metadata['offset'] == len(data)//4
                and absolute == capture.expected_base+metadata['pts']-capture.clock_offset_ns,
                'Stress replay changed its admitted Gst/sample clock mapping')
        rows.append({'first_global_frame': frame, 'byte_offset': byte_at, 'frames': len(data)//4,
                     'callback_monotonic': at, 'absolute_pts_monotonic_ns': absolute, **metadata})
        frame += len(data)//4
        byte_at += len(data)
    mapping = {'session_id': session.identity['session_id'], 'first_frame': session.first_frame,
        'start_index': session.start_index, 'close_index': session.close_index, 'buffers': rows,
        'common_clock_base_ns': capture.expected_base, 'clock_offset_to_monotonic_ns': capture.clock_offset_ns,
        'carrier_origin': carrier.origin, 'carrier_period_frames': reference.PERIOD,
        'carrier_frozen_before_offer': True, 'baseline_music_amplitude': baseline,
        'frequency_hz': evidence['frequency_hz'], 'requested_end_mode': evidence['end_mode'],
        'emitted_reference': evidence['emitted_reference'], 'emission': evidence['emission'],
        'api_identity': session.identity, 'admitted_begin': session.admitted_begin,
        'terminal_observation': session.terminal_observation, 'guards': session.preservation,
        'source_sha256': dict(SOURCE_RECEIPTS), 'pcm': dict(artifacts),
        'scope': 'Exact immutable replay inputs retained before analysis; retention alone grants no audible count'}
    artifacts['mapping'] = retain_json(directory, 'mapping.json', mapping)
    artifacts['retained_before_analysis'] = True
    return directory


def capture_type(base):
    """Extend only the declared byte budget, retaining every metadata check."""
    class StressCapture(base):
        maximum_bytes = CAPTURE_BYTES

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
                    require(audio_format == 'S16LE' and channels == 2 and rate == RATE,
                            'Stress final output changed its declared PCM contract')
                    if self.rate is not None:
                        require((rate, channels, audio_format) == (self.rate, self.channels, self.format),
                                'Final ALSA output renegotiated during observation')
                    require(self.total+len(data) <= CAPTURE_BYTES, 'Stress capture exceeded its128MiB per-zone bound')
                    self.sequence.push(metadata, len(data)//4, rate)
                    self.rate, self.channels, self.format = rate, channels, audio_format
                    self.total += len(data)
                    self.chunks.append(data)
                    self.captured_at.append(at)
                    self.pending.popleft()
            except RuntimeFailure as exc:
                self.error = str(exc)[:2000]
                raise
            return {'capture': self.device, 'format': self.format, 'rate': self.rate, 'channels': self.channels,
                    'observed_bytes': self.total, 'maximum_bytes': CAPTURE_BYTES,
                    'capture_dropped_bytes': self.capture_dropped, 'discontinuities': self.discontinuities,
                    'max_packet_gap_seconds': self.max_packet_gap, 'warning': self.warning,
                    'frame_continuity': self.sequence.evidence(), 'common_clock_base_ns': self.expected_base,
                    'clock_offset_to_monotonic_ns': self.clock_offset_ns}
    return StressCapture


@lru_cache(maxsize=8)
def projection(frames):
    require(type(frames) is int and 480 <= frames <= 960, 'Stress spectrum requires10..20ms complete buffers')
    times = np.arange(frames)/RATE
    centered = np.linspace(-1, 1, frames)
    # A real duck/restore envelope creates spectral side lobes. Fit its smooth
    # local envelope independently for each declared carrier instead of
    # mislabeling a valid440Hz ramp as speech in the peer room. Exact steady
    # carriers and actual peer tones retain their separate fitted amplitudes.
    columns = [column*centered**power for frequency in (440, 880, 1320)
               for power in range(4)
               for column in (np.sin(2*np.pi*frequency*times), np.cos(2*np.pi*frequency*times))]
    return (np.linalg.pinv(np.column_stack((*columns, np.ones(frames)))),
            np.vstack([centered**power for power in range(4)]))


def spectrum(data):
    require(bool(data) and len(data) % 4 == 0, 'Malformed stress final stereo PCM')
    pcm = np.frombuffer(data, dtype='<i2').reshape(-1, 2)
    require(len(pcm) >= 480 and len(pcm) <= 960, 'Stress PCM exceeds declared capture period')
    require(int(np.max(np.abs(pcm.astype(np.int32)))) < 32000, 'Stress final PCM clips')
    require(int(np.max(np.abs(pcm[:, 0].astype(np.int32)-pcm[:, 1]))) <= 3,
            'Stress PCM stereo channels differ')
    matrix, powers = projection(len(pcm))
    fitted = matrix @ pcm[:, 0].astype(float)
    def voice(offset):
        # Include the entire fitted envelope. Its midpoint alone can be near
        # zero while a brief wrong-room tone remains at a buffer edge.
        envelopes = fitted[offset:offset+8].reshape(4, 2).T @ powers
        return float(np.sqrt(np.mean(np.sum(envelopes**2, axis=0))))
    return {'frames': len(pcm), 'music': float(np.hypot(fitted[0], fitted[1])),
            'voice': {880: voice(8), 1320: voice(16)}}


def observe_progress(anchors, room, progress, at):
    """Compare against the original NPT clock, not only small adjacent steps."""
    require(type(progress) is int and progress >= 0, 'Stress NPT lacks bounded advancing integer progress')
    if room in anchors:
        anchor, first_progress, previous = anchors[room]
        require(progress >= previous and abs(progress-first_progress-(at-anchor)*1000) < 1500,
                'Stress NPT moved backward, stalled or jumped from the unchanged program timeline')
        anchors[room] = (anchor, first_progress, progress)
    else:
        anchors[room] = (at, progress, progress)


class PcmGuard:
    """Inspect every buffer; transitions never reset continuity or failures."""
    def __init__(self, captures, references, frequencies, *, reference_mode=False):
        self.captures, self.frequencies = captures, frequencies
        self.references = references
        self.index = {room: len(capture.chunks) for room, capture in captures.items()}
        self.mode = {room: 'idle' for room in captures}
        self.checked = {room: 0 for room in captures}
        self.zero_run = {room: 0 for room in captures}
        self.error = self.failed = None
        self.reference_mode = reference_mode
        self.carriers = {}
        self.frame_at = {room: sum(len(data)//4 for data in capture.chunks) for room, capture in captures.items()}

    def set_mode(self, room, mode):
        require(mode in {'idle', 'starting', 'active', 'ending'}, 'Unknown stress observation phase')
        self.mode[room] = mode

    def voice_limit(self, room, frequency):
        # A contaminated baseline must never raise the wrong-room threshold.
        # Pure quantized carriers stay below this fixed predeclared floor.
        return 4.

    def check(self):
        require(self.error is None, self.error or 'Stress PCM failed permanently')
        try:
            for room, capture in self.captures.items():
                capture.poll()
                require(capture.max_packet_gap < .3 and (capture.last_packet_at is None
                        or time.monotonic()-capture.last_packet_at < .3), 'Stress final PCM callback gap exceeds300ms')
                while self.index[room] < len(capture.chunks):
                    index = self.index[room]
                    data = capture.chunks[index]
                    measured = spectrum(data)
                    ratio = measured['music']/self.references[room]['music']
                    require(.15 < ratio < 1.05, 'A stress PCM buffer lost or amplified advancing music')
                    # A ramp can legitimately change gain across a buffer, but
                    # it cannot insert digital silence. Carry this test across
                    # buffer boundaries; per-stage medians cannot hide a cut.
                    zero = np.all(np.frombuffer(data, dtype='<i2').reshape(-1, 2) == 0, axis=1)
                    for silent in zero:
                        self.zero_run[room] = self.zero_run[room]+1 if silent else 0
                        require(self.zero_run[room] < 4, 'Stress final PCM inserted four consecutive silent frames')
                    own = self.frequencies[room]
                    other = 1320 if own == 880 else 880
                    if not self.reference_mode:
                        require(measured['voice'][other] <= self.voice_limit(room, other),
                                'Peer-zone speech leaked into an individual final PCM buffer')
                    if self.mode[room] == 'idle':
                        require(.95 < ratio < 1.05, 'Closed stress speech changed music gain')
                        if self.reference_mode and room in self.carriers:
                            mono = np.frombuffer(data, dtype='<i2').reshape(-1, 2)[:, 0].astype(float)
                            frames = np.arange(self.frame_at[room], self.frame_at[room]+len(mono))
                            residual = mono-self.carriers[room].values(frames)
                            require(float(np.sqrt(np.mean(residual**2))) <= reference.RESIDUAL_RMS_LIMIT,
                                    'Closed stress speech retained PCM or changed the immutable music carrier')
                            noise = spectrum(np.repeat(residual[:, None], 2, axis=1).astype('<i2').tobytes())
                            require(all(value <= self.voice_limit(room, frequency) for frequency, value in noise['voice'].items()),
                                    'Closed stress speech retained a declared wrong-room marker')
                        else:
                            require(measured['voice'][own] <= self.voice_limit(room, own),
                                    'Closed stress speech retained voice or changed music gain')
                    elif self.mode[room] == 'active':
                        require(.17 < ratio < .23 and measured['voice'][own] > max(8., self.voice_limit(room, own)*2),
                                'Active peer speech or its ducked music was interrupted')
                    self.index[room] += 1
                    self.frame_at[room] += measured['frames']
                    self.checked[room] += 1
        except RuntimeFailure as exc:
            self.error = str(exc)
            self.failed = {'room_id': room, 'index': self.index[room], 'mode': self.mode[room], 'message': self.error}
            raise

    def evidence(self):
        return {'checked_blocks': dict(self.checked), 'failed_buffer': self.failed,
                'music_ratio_during_transitions': [.15, 1.05], 'idle_ratio': [.95, 1.05],
                'active_ratio': [.17, .23], 'maximum_consecutive_silent_frames': 3,
                'foreign_authority': 'Immutable whole-session reference before counting' if self.reference_mode else 'fixed spectrum floor',
                'frequencies_hz': dict(self.frequencies)}


class Gate:
    def __init__(self, room, kind, trigger, guard):
        require(kind in {'active', 'idle'}, 'Invalid stress transition')
        self.room, self.kind, self.trigger, self.guard = room, kind, trigger, guard
        self.index = len(guard.captures[room].chunks)
        self.frames = self.blocks = self.examined = 0
        self.first = self.last = None
        self.passed = False

    def check(self):
        capture, room = self.guard.captures[self.room], self.room
        while self.index < len(capture.chunks):
            at = capture.captured_at[self.index]
            data = capture.chunks[self.index]
            if at < self.trigger:
                self.index += 1
                continue
            measured = spectrum(data)
            require(self.last is None or at > self.last, 'Stress transition callback times did not advance')
            ratio = measured['music']/self.guard.references[room]['music']
            voice = measured['voice'][self.guard.frequencies[room]]
            matches = (.17 < ratio < .23 and voice > max(8., self.guard.voice_limit(room, self.guard.frequencies[room])*2)
                       if self.kind == 'active' else
                       .95 < ratio < 1.05 and voice <= self.guard.voice_limit(room, self.guard.frequencies[room]))
            self.examined += 1
            if matches:
                if self.first is None:
                    self.first = at
                self.frames += measured['frames']
                self.blocks += 1
            else:
                self.first = None
                self.frames = self.blocks = 0
                self.passed = False
            self.last = at
            self.index += 1
            self.passed = (self.first is not None and self.frames >= RATE*.4
                           and self.blocks >= 3 and self.last-self.first >= .35)
        # A qualifying prefix cannot accept a currently wrong buffered tail.
        return self.passed

    def evidence(self):
        return {'passed': self.passed, 'room_id': self.room, 'kind': self.kind,
                'consecutive_frames': self.frames, 'consecutive_blocks': self.blocks,
                'examined_blocks': self.examined,
                'elapsed_seconds': round(time.monotonic()-self.trigger, 6),
                'scope': 'Final digital PCM callbacks, no physical speaker/acoustic assertion'}


def make_peer(frequency, session_id=None):
    from aiortc import RTCConfiguration, RTCPeerConnection, RTCRtpSender

    peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    tone = reference.packet_track(frequency, session_id or str(uuid4()))
    transceiver = peer.addTransceiver(tone, direction='sendonly')
    opus = [codec for codec in RTCRtpSender.getCapabilities('audio').codecs if codec.mimeType.lower() == 'audio/opus']
    require(bool(opus), 'Stress sender lacks real Opus')
    transceiver.setCodecPreferences(opus)
    return peer, tone


def verify_captured_session(blocks, first_frame, carrier, decoded, close_index, baseline,
                            admission, own_frequency, canceled):
    """One bounded thread invocation; complete PCM is the counting authority."""
    require(type(blocks) is tuple and sum(map(len, blocks)) <= reference.MAX_SESSION_CAPTURE_BYTES,
            'Stress session observation exceeds its8MiB frozen capture bound')
    require(all(len(data) == 3840 for data in blocks), 'Stress reference requires exact native20ms capture blocks')
    began = time.monotonic()
    def budget():
        require(not canceled.is_set(), 'Stress reference verification canceled')
        require(time.monotonic()-began <= 3, 'Stress reference analysis exceeded its3s complete work bound')
    def changed_index(start, condition):
        for index in range(start, len(blocks)):
            budget()
            if condition(spectrum(blocks[index])['music']):
                return index
        return None
    # Sampling at20ms can put the first ramp in the previous captured block.
    # These bounded windows nominate integer-only laws; all blocks are then
    # independently checked against ONE admitted session/calendar mapping.
    changed = changed_index(0, lambda music: music < baseline*.9)
    require(changed is not None, 'Stress session never lowered its actual music gain')
    onset_index = max(0, changed-2)
    onset = blocks[onset_index:onset_index+10]
    require(len(onset) == 10, 'Stress session lacks its bounded complete onset reference window')
    onset_frame = first_frame+onset_index*960
    duck_last = first_frame+(changed+1)*960
    foreign = 1320 if own_frequency == 880 else 880
    alignment, gain, initial = reference.admit_onset(b''.join(onset), onset_frame, carrier, decoded,
        decoded.session_id, (onset_frame, duck_last), (onset_frame, min(onset_frame+8640, duck_last+1920)),
        foreign, spectrum, canceled=canceled)
    terminal, directory = admission
    retain_json(directory, 'onset.json', {'alignment_start_frame': alignment.start_frame,
        'session_id': alignment.session_id, 'duck_frame': gain.duck_frame, 'admission': initial})
    budget()
    require(type(close_index) is int and 0 <= close_index < len(blocks), 'Stress close observation boundary changed')
    restored = changed_index(close_index, lambda music: music > baseline*.3)
    require(restored is not None, 'Stress session never restored its actual music gain')
    end_index = max(0, restored-17)
    end = blocks[end_index:end_index+36]
    require(len(end) == 36, 'Stress session lacks its bounded complete EOF/restore/tail reference window')
    end_frame = first_frame+end_index*960
    ended, final_gain, tail = reference.admit_restore(b''.join(end), end_frame, carrier, decoded,
        alignment, gain, (end_frame, end_frame+34560-9600), foreign, spectrum, canceled=canceled,
        terminal=terminal, proposed=lambda proposal: retain_json(directory, 'restore-proposal.json', proposal))
    budget()
    result = reference.verify_session(blocks, first_frame, carrier, decoded, ended, final_gain,
                                      foreign, spectrum, canceled=canceled)
    budget()
    result.update(onset_admission=initial, restore_admission=tail,
                  total_work_seconds=time.monotonic()-began, total_work_limit_seconds=3,
                  scope='Exact produced-payload continuous decode matched to ALL actual final PCM and closed tail; no physical assertion')
    return result


def reference_work(finished, *arguments):
    """Expose actual thread retirement, independently of its asyncio future."""
    try:
        result = verify_captured_session(*arguments)
        retain_json(arguments[-3][1], 'verification.json', result)
        return result
    except BaseException as failure:
        # Preserve the primary rejection even if retaining its bounded type
        # fails. Input/onset/proposal artifacts were committed before analysis.
        try:
            retain_json(arguments[-3][1], 'failure.json', {'type': type(failure).__name__,
                'message': str(failure)[:2000], 'verified': False})
        except Exception as artifact_error:
            if hasattr(failure, 'add_note'):
                failure.add_note(f'Stress failure artifact: {type(artifact_error).__name__}')
        raise
    finally:
        finished.set()


async def emission_evidence(peer, decoded):
    """Bounded public sender receipt; waveform verification proves arrival."""
    stats = await asyncio.wait_for(peer.getStats(), 2)
    require(isinstance(stats, dict) and len(stats) <= 32, 'Stress sender statistics exceed their bounded contract')
    outbound = [value for value in stats.values()
                if value.type == 'outbound-rtp' and value.kind == 'audio']
    require(len(outbound) == 1 and type(outbound[0].packetsSent) is int
            and 0 < outbound[0].packetsSent <= decoded.packet_count,
            'Exact reference publisher has no bounded audio RTP transmission evidence')
    return {'outbound_audio_packets': outbound[0].packetsSent,
            'produced_reference_packets': decoded.packet_count,
            'scope': 'Public sender packet count; exact captured waveform independently proves its received prefix'}


async def canceled_close(api, broker, room, identity, check):
    """Cancel an actual client request only after exact broker admission."""
    session = identity['session_id']
    require(session not in broker.pending_sessions, 'Earlier session operation still pending before canceled close')
    task = asyncio.create_task(api.request('POST', f'/api/v1/rooms/{room}/speech',
        json={**identity, 'action': 'close', 'request_id': str(uuid4())}))
    try:
        deadline = time.monotonic()+2
        while session not in broker.pending_sessions:
            await check()
            require(not task.done() and time.monotonic() < deadline,
                    'Could not observe actual admitted in-flight close before client cancellation')
            await asyncio.sleep(0)
        require(broker.sessions.get(session) == room and not task.done(), 'Canceled close lost exact admitted operation')
        task.cancel()
        result = await asyncio.gather(task, return_exceptions=True)
        require(isinstance(result[0], asyncio.CancelledError), 'Client close cancellation was not exercised')
        return {'client_canceled_after_exact_broker_admission': True}
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def exercise(api, broker, states, captures, base_guards, check, report, *, phase_change):
    """Twenty audible incarnations per zone, one uninterrupted native program."""
    from aiortc import RTCSessionDescription

    rooms = tuple(states)
    require(len(rooms) == 2, 'Speech stress requires exactly two owned zones')
    frequencies = dict(zip(rooms, (880, 1320), strict=True))
    receipt = {'required_sessions_per_zone': ROUNDS, 'audible_sessions': dict.fromkeys(rooms, 0),
               'rounds': [], 'final_pcm_byte_limit_per_zone': CAPTURE_BYTES,
               'stress_deadline_seconds': STRESS_SECONDS, 'producer_duration_seconds': PRODUCER_SECONDS,
               'baseline_voice_limit': 4., 'rejected_controls': 0, 'passed': False,
               'source_sha256': dict(SOURCE_RECEIPTS)}
    report['speech_stress'] = receipt
    artifact_root = report.get('artifacts', {}).get('private_directory')
    require(type(artifact_root) is str, 'Stress lacks its owned private replay artifact directory')
    references = {room: spectrum(b''.join(captures[room].chunks[-1:])) for room in rooms}
    require(all(reference['music'] > 1000 for reference in references.values()), 'Stress baseline is not real audible music')
    require(all(voice <= 4. for reference in references.values() for voice in reference['voice'].values()),
            'Stress baseline already contains a declared own/peer speech marker')
    guard = PcmGuard(captures, references, frequencies, reference_mode=True)
    old_reference = {room: base_guards[room].reference for room in rooms}
    for value in base_guards.values():
        value.reference = None  # Own active/idle guards below cover BOTH zones.
    live, previous, all_sessions = {}, {}, []
    operation_tasks, analysis_tasks = set(), set()
    aborting = False
    done = asyncio.Event()
    watcher = None
    try:
        before_intent = {room: states[room].desired.model_dump(mode='json') for room in rooms}
        from shiri.domain import Room
        api_intent = {room: {key: value for key, value in (await api.room(room)).items() if key in Room.model_fields}
                      for room in rooms}
        before_owner = {room: deepcopy((await call_rpc(broker._worker_socket(states[room]), 'health', {}, timeout=2))['source']['owner'])
                        for room in rooms}
        before_players = {room: await states[room].client.request('GET', '/api/player') for room in rooms}
        progress_started = time.monotonic()
        async def parallel(*coroutines):
            """Retain every child; one failed operation retires its siblings."""
            nonlocal aborting
            tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
            operation_tasks.update(tasks)
            try:
                return await asyncio.gather(*tasks)
            except BaseException:
                aborting = True
                raise
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                completed, pending = await asyncio.wait(tasks, timeout=2)
                for task in completed:
                    if not task.cancelled():
                        task.exception()  # Retrieve all exact sibling errors.
                operation_tasks.difference_update(completed)
                if pending:
                    receipt['operation_cleanup_pending'] = len(pending)
                    receipt['passed'] = False
        async def checked():
            if watcher is not None and watcher.done():
                watcher.result()
                raise RuntimeFailure('Speech stress observation task ended unexpectedly')
            await check()
            guard.check()
        async def watch():
            while not done.is_set():
                await check()
                guard.check()
                await asyncio.sleep(.02)
        watcher = asyncio.create_task(watch())
        while any(sum(len(data)//4 for data in captures[room].chunks[-5:]) < reference.PERIOD*2 for room in rooms):
            await checked()
            await asyncio.sleep(.02)
        for room in rooms:
            captures[room].poll()
            pure = b''.join(captures[room].chunks[-5:])[-reference.PERIOD*2*4:]
            guard.carriers[room] = reference.Carrier(pure, next_frame=sum(len(data)//4 for data in captures[room].chunks))
        async def transition(room, kind, trigger, evidence, *, seconds=8):
            gate = Gate(room, kind, trigger, guard)
            evidence[kind] = gate.evidence()
            end = time.monotonic()+seconds
            while time.monotonic() < end:
                await checked()
                if gate.check():
                    guard.set_mode(room, kind)
                    evidence[kind] = gate.evidence()
                    return
                await asyncio.sleep(.02)
            evidence[kind] = gate.evidence()
            raise RuntimeFailure(f'Final PCM stress {kind} transition failed for exact room{room}')
        async def offer(room, number, evidence):
            identity = {'session_id': str(uuid4()), 'request_id': str(uuid4())}
            peer, tone = make_peer(frequencies[room], identity['session_id'])
            session = SimpleNamespace(room=room, peer=peer, tone=tone, identity=identity, offer=None,
                start_index=len(captures[room].chunks)-1, close_index=None, snapshot=None,
                analysis_cancel=threading.Event(), analysis_done=threading.Event(), analysis_started=False)
            live[room] = session
            all_sessions.append(session)
            await peer.setLocalDescription(await peer.createOffer())
            require(not aborting, 'Retired stress negotiation cannot admit a late session')
            body = {**identity, 'action': 'offer', 'sdp': peer.localDescription.sdp, 'type': 'offer'}
            session.offer = body
            evidence.update(session_id=identity['session_id'], frequency_hz=frequencies[room])
            path = (f'/api/v1/nobly/rooms/{before_intent[room]["nobly_room_id"]}/speech'
                    if number % 2 == 0 else f'/api/v1/rooms/{room}/speech')
            answer = await api.request('POST', path, json=body)
            require(not aborting, 'Retired stress negotiation received a late answer')
            require(answer['admitted_room_id'] == room, 'Stress offer admitted a different stable zone UUID')
            await peer.setRemoteDescription(RTCSessionDescription(sdp=answer['sdp'], type=answer['type']))
        async def idle(room, session):
            deadline = time.monotonic()+8
            while time.monotonic() < deadline:
                await checked()
                health = await call_rpc(broker._worker_socket(states[room]), 'health', {}, timeout=2)
                if (health['speech_session_id'] is None and health.get('speech_cleanup_pending') == 0
                        and health.get('speech_ready') is True and not health.get('speech_cleanup_error')
                        and session.identity['session_id'] not in broker.sessions):
                    return
                await asyncio.sleep(.02)
            raise RuntimeFailure('Closed stress session did not release exact bounded ownership/peer resources')
        async def rounds_loop():
            for number in range(ROUNDS):
                phase_change(f'speech_stress_round_{number+1}')
                round_receipt = {'number': number+1, 'zones': {room: {} for room in rooms}, 'passed': False}
                receipt['rounds'].append(round_receipt)
                trigger = time.monotonic()
                for room in rooms:
                    guard.set_mode(room, 'starting')
                await parallel(*(offer(room, number, round_receipt['zones'][room]) for room in rooms))
                await parallel(*(transition(room, 'active', trigger, round_receipt['zones'][room]) for room in rooms))
                # Count only an actual sustained final-output interval, not an
                # HTTP success, media callback, or merely negotiating peer.
                for room in rooms:
                    require(live[room].peer.connectionState == 'connected', 'Audible stress peer is not connected')
                    other = rooms[1] if room == rooms[0] else rooms[0]
                    await api.request('POST', f'/api/v1/rooms/{other}/speech', expected=409,
                        json={**live[room].identity, 'action': 'close', 'request_id': str(uuid4())})
                    await api.request('POST', f'/api/v1/rooms/{room}/speech', expected=409,
                        json={**live[room].offer, 'session_id': str(uuid4()), 'request_id': str(uuid4())})
                    receipt['rejected_controls'] += 2
                    if room in previous:
                        await api.request('POST', f'/api/v1/rooms/{room}/speech', expected=409,
                            json={**previous[room], 'action': 'close', 'request_id': str(uuid4())})
                        receipt['rejected_controls'] += 1
                    health = await call_rpc(broker._worker_socket(states[room]), 'health', {}, timeout=2)
                    require(health['speech_session_id'] == live[room].identity['session_id'], 'Rejected stress control changed current speech owner')
                    live[room].admitted_begin = deepcopy(health.get('speech_startup_authenticated_begin_ack'))
                for position, room in enumerate(rooms if number % 2 == 0 else rooms[::-1]):
                    session = live[room]
                    own_receipt = round_receipt['zones'][room]
                    session.close_index = len(captures[room].chunks)-session.start_index
                    guard.set_mode(room, 'ending')
                    trigger = time.monotonic()
                    mode = ('eof' if number % 5 == 2 else 'cancel' if number % 5 == 3 else 'close') if position == 0 else 'close'
                    own_receipt['end_mode'] = mode
                    if mode == 'eof':
                        session.tone.stop()
                        await asyncio.wait_for(session.peer.close(), 4)
                    elif mode == 'cancel':
                        own_receipt['cancellation'] = await canceled_close(api, broker, room, session.identity, checked)
                    else:
                        await api.request('POST', f'/api/v1/rooms/{room}/speech',
                            json={**session.identity, 'action': 'close', 'request_id': str(uuid4())})
                    await transition(room, 'idle', trigger, own_receipt, seconds=16 if mode == 'eof' else 8)
                    await idle(room, session)
                    session.tone.stop()
                    await asyncio.wait_for(session.peer.close(), 4)
                    require(session.peer.connectionState == 'closed', 'Closed stress peer remains live')
                    require(session.tone.readyState == 'ended', 'Exact reference publisher survived joined peer shutdown')
                    # The sender is joined before this ONE immutable snapshot.
                    # Record its source evidence before any subsequent clear.
                    session.snapshot = session.tone.reference.snapshot()
                    own_receipt['emitted_reference'] = session.tone.reference.evidence()
                    own_receipt['emission'] = await emission_evidence(session.peer, session.snapshot)
                    await checked()
                    capture = captures[room]
                    require(guard.index[room] == len(capture.chunks), 'Final reference snapshot skipped a pending PCM buffer')
                    session.blocks = tuple(capture.chunks[session.start_index:])
                    session.first_frame = sum(len(data)//4 for data in capture.chunks[:session.start_index])
                    health = await call_rpc(broker._worker_socket(states[room]), 'health', {}, timeout=2)
                    session.terminal_observation = {key: deepcopy(health.get(key)) for key in (
                        'speech_startup_authenticated_begin_ack', 'speech_startup_authenticated_retirement_ack',
                        'speech_startup_identity', 'speech_session_id', 'speech_cleanup_pending',
                        'speech_ready', 'speech_cleanup_error')}
                    session.terminal_observation['source'] = {'owner': deepcopy(health['source']['owner'])}
                    player = await states[room].client.request('GET', '/api/player')
                    session.preservation = {'source_before': deepcopy(before_owner[room]),
                        'source_after': deepcopy(health['source']['owner']),
                        'player_before': deepcopy(before_players[room]), 'player_after': deepcopy(player),
                        'room_intent_before': before_intent[room],
                        'room_intent_after': states[room].desired.model_dump(mode='json')}
                    # Freeze/commit exact replay bytes before starting ANY
                    # analysis thread. Even rejected identity/model results
                    # retain their inputs, and neither zone can count alone.
                    try:
                        session.artifact_directory = retain_session(artifact_root, number+1, session,
                            capture, guard.carriers[room], own_receipt, references[room]['music'])
                        session.terminal = reference.admit_terminal(session.terminal_observation, room,
                            session.identity, before_owner[room], session.admitted_begin)
                        own_receipt['authenticated_terminal'] = session.terminal.action
                    except Exception as artifact_error:
                        own_receipt['replay_retention_or_admission_error'] = type(artifact_error).__name__
                        raise
                    previous[room] = session.identity
                    live.pop(room)
                    own_receipt['closed'] = True
                    if position == 0:
                        other = next(identifier for identifier in rooms if identifier != room)
                        require(live[other].peer.connectionState == 'connected' and guard.mode[other] == 'active',
                                'Closing one zone disconnected the simultaneously speaking peer zone')
                        own_receipt['peer_continued_during_close'] = True
                    await checked()
                verified = {}
                try:
                    for room in rooms:
                        session = next(item for item in reversed(all_sessions) if item.room == room)
                        session.analysis_started = True
                        operation = asyncio.create_task(asyncio.to_thread(reference_work, session.analysis_done,
                            session.blocks, session.first_frame, guard.carriers[room], session.snapshot,
                            session.close_index, references[room]['music'], (session.terminal, session.artifact_directory),
                            frequencies[room], session.analysis_cancel))
                        operation_tasks.add(operation)
                        analysis_tasks.add(operation)
                        try:
                            verified[room] = await asyncio.wait_for(asyncio.shield(operation), 3.5)
                            require(session.analysis_done.is_set(), 'Reference future completed before its actual worker retired')
                            verified[room]['analysis_worker_joined'] = True
                        finally:
                            session.analysis_cancel.set()
                            if not operation.done():
                                completed, pending = await asyncio.wait({operation}, timeout=1)
                                if pending:
                                    receipt['reference_worker_pending'] = True
                                    receipt['passed'] = False
                                else:
                                    for task in completed:
                                        if not task.cancelled():
                                            task.exception()
                                    operation_tasks.difference_update(completed)
                            else:
                                operation_tasks.discard(operation)
                                if not operation.cancelled():
                                    operation.exception()
                        await checked()
                    for room in rooms:
                        require(verified[room]['verified_received_prefix_frames'] <=
                                round_receipt['zones'][room]['emission']['outbound_audio_packets']*reference.FRAMES,
                                'Verified reference prefix exceeds exact sender RTP packet count')
                    for room in rooms:
                        round_receipt['zones'][room]['reference_verification'] = verified[room]
                        receipt['audible_sessions'][room] += 1
                finally:
                    # A pair grants counts only after BOTH complete waveforms
                    # pass; failed-pair references are still fenced/released.
                    for room in rooms:
                        session = next(item for item in reversed(all_sessions) if item.room == room)
                        session.analysis_cancel.set()
                        session.tone.reference.clear()
                        session.snapshot = session.blocks = None
                        session.tone.encoder = None
                round_receipt['passed'] = True
        await asyncio.wait_for(rounds_loop(), STRESS_SECONDS)
        for room in rooms:
            require(receipt['audible_sessions'][room] == ROUNDS, 'Stress omitted an audibly verified session')
            after = await api.room(room)
            require({key: value for key, value in after.items() if key in Room.model_fields} == api_intent[room]
                    and states[room].desired.model_dump(mode='json') == before_intent[room],
                    'Speech stress altered saved room intent/revision/volume/speakers/binding')
            health = await call_rpc(broker._worker_socket(states[room]), 'health', {}, timeout=2)
            player = await states[room].client.request('GET', '/api/player')
            require(health['source']['owner'] == before_owner[room], 'Speech stress changed exact native producer/token')
            require(player['state'] == 'play' and player['item_id'] == before_players[room]['item_id']
                    and player['item_progress_ms'] > before_players[room]['item_progress_ms']+1000
                    and abs(player['item_progress_ms']-before_players[room]['item_progress_ms']
                            -(time.monotonic()-progress_started)*1000) < 2000,
                    'OwnTone program/NPT did not advance through repeated speech')
        await checked()
        receipt['passed'] = True
    finally:
        original_failure = sys.exc_info()[1]
        aborting = True
        done.set()
        if watcher is not None:
            watcher.cancel()
            results = await asyncio.gather(watcher, return_exceptions=True)
            if isinstance(results[0], Exception) and not isinstance(results[0], asyncio.CancelledError):
                receipt['observation_error'] = type(results[0]).__name__
        cleanup_errors = []
        for session in all_sessions:
            session.analysis_cancel.set()
            if session.room in live and live[session.room] is session:
                try:
                    await api.request('POST', f'/api/v1/rooms/{session.room}/speech',
                        json={**session.identity, 'action': 'close', 'request_id': str(uuid4())})
                except Exception as exc:
                    cleanup_errors.append(f'API close: {type(exc).__name__}')
            try:
                session.tone.stop()
            except Exception as exc:
                cleanup_errors.append(f'tone stop: {type(exc).__name__}')
            try:
                if session.peer.connectionState != 'closed':
                    await asyncio.wait_for(session.peer.close(), 4)
                require(session.peer.connectionState == 'closed', 'Stress peer survived cleanup')
            except Exception as exc:
                cleanup_errors.append(f'peer close: {type(exc).__name__}')
            try:
                if not session.tone.reference.closed:
                    receipt.setdefault('retired_references', []).append(session.tone.reference.evidence())
                    session.tone.reference.clear()
                session.snapshot = None
                session.tone.encoder = None
            except Exception as exc:
                cleanup_errors.append(f'reference clear: {type(exc).__name__}')
        for task in operation_tasks:
            # Canceling a to_thread future does not stop its actual thread.
            # Its exact cancel Event is already set; retain/join that future.
            if task not in analysis_tasks and not task.done():
                task.cancel()
        if operation_tasks:
            completed, pending = await asyncio.wait(operation_tasks, timeout=2)
            for task in completed:
                if not task.cancelled():
                    task.exception()
            receipt['operation_cleanup_pending'] = len(pending)
            if pending:
                cleanup_errors.append('Owned concurrent operation did not retire within2s')
        unretired = sum(session.analysis_started and not session.analysis_done.is_set() for session in all_sessions)
        receipt['reference_worker_cleanup_pending'] = unretired
        if unretired:
            cleanup_errors.append('Exact reference analysis thread survived its bounded retirement')
        receipt['peer_cleanup_errors'] = cleanup_errors
        receipt['pcm_guard'] = guard.evidence()
        for room in rooms:
            base_guards[room].reference = old_reference[room]
        receipt['passed'] = receipt['passed'] and not cleanup_errors and 'observation_error' not in receipt
        if original_failure is None:
            require(not cleanup_errors, 'Stress owned peer cleanup failed')
            require('observation_error' not in receipt, 'Stress observer failed before cleanup')
