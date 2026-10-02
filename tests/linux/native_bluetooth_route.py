"""Private BlueALSA fixture and bounded real SBC/RTP observations.

Import creates no bus, codec, socket, process or device. Mock BlueZ supplies only
metadata and an AF_UNIX transport; the maintained daemon exports PCM and encodes
the actual broker/OwnTone stream. RTP arrival is not a speaker presentation PTS.
"""
from __future__ import annotations

# Manual private fixture files/proc observations are bounded between awaits.
# ruff: noqa: ASYNC240

import array
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import stat
import struct
import subprocess
import time

import numpy as np
from dbus_next import Variant
from dbus_next.aio import MessageBus

from shiri.runtime.bluealsa import BlueALSA, DescriptorBus, _bounded, _close_fds
from shiri.runtime.system import RuntimeFailure, atomic_json

HERE = Path(__file__).resolve()
_spec = importlib.util.spec_from_file_location('private_bluetooth_protocol', HERE.with_name('check_bluealsa_private_bus.py'))
private = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(private)
_diagnostic_spec = importlib.util.spec_from_file_location('private_bluetooth_startup_diagnostics',
                                                        HERE.with_name('bluetooth_startup_diagnostics.py'))
diagnostics = importlib.util.module_from_spec(_diagnostic_spec)
_diagnostic_spec.loader.exec_module(diagnostics)

RATE = 48000
CHANNELS = 2
MAX_RTP_BYTES = 32 * 1024 * 1024
MAX_PCM_BYTES = 64 * 1024 * 1024
MAX_PACKETS = 64000
MAX_CONTROLS = 8000
MAX_LOG_BYTES = 4 * 1024 * 1024
MAX_PACKET_BYTES = 1000  # Exact negotiated mock transport MTU.
PHASE_SECONDS = 180
PRODUCER_SECONDS = 180
INNER_SECONDS = 280
EXTERNAL_SECONDS = 400
DEVICE = f'bluealsa:DEV={private.MAC},PROFILE=a2dp'
CODEC_TONE_FLOOR = 8.  # Existing codec floor; never derive it from an impure signal.
# Encoder and decoder each have a10×8-sample subband filter. Add one
# maximum15×128-frame RTP packet for the indivisible receive observation.
# No extra output-buffer wait is counted after the first actual gain change.
CODEC_SETTLE_FRAMES = 2 * 10 * 8 + 15 * 128


def require(value, message):
    if not value:
        raise RuntimeFailure(message)


def sbc_frames(payload, count):
    """Parse complete negotiated SBC frames, never infer count from decoding."""
    require(type(payload) is bytes and type(count) is int and 1 <= count <= 15,
            'Invalid bounded SBC frame envelope')
    offset, result = 0, []
    for _ in range(count):
        require(len(payload)-offset >= 4 and payload[offset] == 0x9c,
                'SBC transport contains an incomplete or wrong-sync frame')
        header, bitpool = payload[offset+1], payload[offset+2]
        rate = (16000, 32000, 44100, 48000)[header >> 6]
        blocks, mode = 4*((header >> 4 & 3)+1), header >> 2 & 3
        channels, bands = (1 if mode == 0 else 2), (8 if header & 1 else 4)
        require(rate == RATE and channels == CHANNELS and 2 <= bitpool <= 53,
                'SBC transport changed its admitted48k stereo/bitpool profile')
        require(bitpool <= bands*(16 if mode in (0, 1) else 32), 'SBC bitpool exceeds its channel-mode bound')
        audio_bits = blocks*channels*bitpool if mode in (0, 1) else blocks*bitpool+(bands if mode == 3 else 0)
        size = 4+(4*bands*channels)//8+(audio_bits+7)//8
        require(offset+size <= len(payload), 'SBC frame payload is truncated')
        frame = payload[offset:offset+size]
        # SBC's CRC8 covers codec header/bitpool, joint flags and scale factors,
        # not the encoded audio bits. Validate before any C decoder receives it.
        crc = 0x0f
        protected = 4*bands*channels+(bands if mode == 3 else 0)
        for index in range(16+protected):
            bit = ((frame[1+index//8] >> (7-index % 8)) & 1 if index < 16
                   else (frame[4+(index-16)//8] >> (7-(index-16) % 8)) & 1)
            crc = ((crc << 1) ^ (0x1d if (crc >> 7) ^ bit else 0)) & 0xff
        require(crc == frame[3], 'SBC codec header/scale-factor CRC is invalid')
        result.append((frame, blocks*bands, (header, bitpool)))
        offset += size
    require(offset == len(payload), 'SBC transport contains extra or undeclared frames')
    return result


def fit_tones(data):
    """Phase-independent stereo fits; lossy decoded amplitudes are observations."""
    require(type(data) is bytes and len(data) >= 128*4 and len(data) % 4 == 0,
            'Decoded SBC observation lacks complete stereo frames')
    values = np.frombuffer(data, dtype='<i2').reshape(-1, 2).astype(float)
    positions = np.arange(len(values))/RATE
    basis = np.column_stack(tuple(component for frequency in (440, 660, 880, 1320)
                                 for component in (np.sin(2*np.pi*frequency*positions),
                                                   np.cos(2*np.pi*frequency*positions)))+(np.ones(len(values)),))
    coefficients, *_ = np.linalg.lstsq(basis, values, rcond=None)
    result = {f'amplitude_{frequency}': float(np.max(np.hypot(coefficients[2*n], coefficients[2*n+1])))
              for n, frequency in enumerate((440, 660, 880, 1320))}
    result.update({f'minimum_channel_amplitude_{frequency}': float(np.min(np.hypot(coefficients[2*n], coefficients[2*n+1])))
                   for n, frequency in enumerate((440, 660, 880, 1320))})
    result.update(frames=len(values), peak=float(np.max(np.abs(values))),
                  rms=float(np.sqrt(np.mean(values*values))),
                  dc=float(np.max(np.abs(coefficients[-1]))))
    return result


def pure_baseline(chunks):
    """Validate every baseline block against the known single440Hz stimulus.

    The same8-count SBC projection floor is exercised by the independent real
    encoder/decoder at4096,8192 and16383. A measured880 component cannot declare
    itself codec noise and then increase later voice-admission thresholds.
    """
    require(chunks and sum(len(data)//4 for data in chunks) >= RATE*.4,
            'Pure codec baseline lacks a complete independently audible window')
    for data in chunks:
        measured = fit_tones(data)
        require(measured['peak'] < 32760 and measured['minimum_channel_amplitude_440'] > 1000,
                'Pure codec baseline clipped or lost a channel')
        require(max(measured[f'amplitude_{frequency}'] for frequency in (660, 880, 1320)) <= CODEC_TONE_FLOOR
                and measured['dc'] <= CODEC_TONE_FLOOR,
                'Codec baseline contains an undeclared source or voice instead of pure music')
    return fit_tones(b''.join(chunks))


class RtpCapture:
    """One real AV decoder, exact RTP/SBC framing and bounded retained bytes."""
    def __init__(self):
        import av
        self.decoder = av.CodecContext.create('sbc', 'r')
        # Explicit admitted codec caps avoid an uninitialized channel layout
        # in libav's standalone SBC decoder. Every encoded header and actual
        # decoded frame still independently has to match these exact caps.
        self.decoder.sample_rate, self.decoder.layout = RATE, 'stereo'
        self.chunks, self.at, self.records = [], [], []
        self.raw = bytearray()
        self.total_pcm, self.total_frames = 0, 0
        self.sequence = self.timestamp = self.previous_frames = self.ssrc = self.profile = None
        self.error, self.last_at, self.max_gap = None, None, 0.
        self.max_continuous_gap, self.barrier, self.barriers = 0., None, []

    def begin_barrier(self, kind):
        require(kind in {'takeover', 'end'} and self.barrier is None, 'Invalid explicit source-only observation barrier')
        self.check()
        self.barrier = {'kind': kind, 'requested_monotonic_ns': time.monotonic_ns(),
                        'previous_packet': len(self.records)-1, 'previous_arrival': self.last_at}
        self.barriers.append(self.barrier)

    def complete_barrier(self):
        require(self.barrier is not None and self.last_at is not None
                and round(self.last_at*1e9) > self.barrier['requested_monotonic_ns'],
                'Source barrier lacks actually observed successor PCM')
        self.barrier.update(completed_monotonic_ns=time.monotonic_ns(), observed_packet=len(self.records)-1,
                            observed_arrival=self.last_at)
        self.barrier = None

    def feed(self, packet, at):
        require(self.error is None, self.error or 'SBC observer is permanently failed')
        try:
            require(type(packet) is bytes and 14 <= len(packet) <= MAX_PACKET_BYTES,
                    'Actual RTP transport changed its complete negotiated MTU')
            require(type(at) in (int, float) and np.isfinite(at) and at > 0
                    and (self.last_at is None or at > self.last_at), 'RTP arrival clock did not advance')
            require(packet[0] == 0x80 and packet[1] == 96 and packet[12] & 0xf0 == 0,
                    'RTP transport contains unsupported extension/fragment/payload headers')
            sequence, timestamp, ssrc = struct.unpack_from('!HII', packet, 2)
            if self.sequence is not None:
                require(sequence == (self.sequence+1) & 0xffff, 'Actual SBC RTP packet was lost, repeated or reordered')
                require(timestamp == (self.timestamp+self.previous_frames) & 0xffffffff,
                        'Actual SBC RTP sample-count timestamp has a gap or repeat')
                require(ssrc == self.ssrc, 'Actual SBC RTP stream identity changed')
            frames = sbc_frames(packet[13:], packet[12] & 15)
            pieces = []
            import av
            for encoded, samples, profile in frames:
                if self.profile is not None:
                    require(profile == self.profile, 'SBC encoder profile changed within the admitted transport')
                decoded = self.decoder.decode(av.Packet(encoded))
                require(len(decoded) == 1 and decoded[0].samples == samples
                        and decoded[0].sample_rate == RATE and len(decoded[0].layout.channels) == CHANNELS
                        and decoded[0].format.name == 's16p', 'Actual AV SBC decoder changed its PCM contract')
                values = decoded[0].to_ndarray()
                require(values.shape == (CHANNELS, samples) and values.dtype == np.int16,
                        'Actual SBC decoder returned incomplete channels/samples')
                pieces.append(values.T.astype('<i2', copy=False).tobytes())
                self.profile = profile
            data, sample_count = b''.join(pieces), sum(frame[1] for frame in frames)
            require(len(self.records) < MAX_PACKETS and len(self.raw)+4+len(packet) <= MAX_RTP_BYTES
                    and self.total_pcm+len(data) <= MAX_PCM_BYTES, 'Bluetooth observer exceeded its admitted retention bound')
            require(len(data) == sample_count*4, 'SBC decoded frame count differs from actual encoded frame count')
            if self.last_at is not None:
                self.max_gap = max(self.max_gap, at-self.last_at)
                if self.barrier is None:
                    self.max_continuous_gap = max(self.max_continuous_gap, at-self.last_at)
            self.records.append({'index': len(self.records), 'sequence': sequence, 'timestamp': timestamp,
                                 'first_decoded_frame': self.total_frames, 'frames': sample_count,
                                 'arrival_monotonic_ns': round(at*1e9), 'bytes': len(packet)})
            self.raw.extend(struct.pack('!I', len(packet))+packet)
            self.chunks.append(data)
            self.at.append(at)
            self.total_pcm += len(data)
            self.total_frames += sample_count
            self.sequence, self.timestamp, self.previous_frames, self.ssrc = sequence, timestamp, sample_count, ssrc
            self.last_at = at
        except BaseException as exc:
            self.error = type(exc).__name__+': '+str(exc)[:200]
            raise

    def check(self, *, live=True):
        require(self.error is None, self.error or 'SBC observer failed')
        require(self.max_continuous_gap < .3, 'Actual SBC transport callback gap exceeds300ms outside explicit source barriers')
        if live and self.barrier is None and self.last_at is not None:
            require(time.monotonic()-self.last_at < .3, 'Actual SBC transport stopped delivering data')

    def evidence(self):
        return {'decoded_rate': RATE, 'decoded_channels': CHANNELS, 'decoded_format': 'S16LE',
                'decoder': 'actual libav SBC,s16p→interleaved without resampling',
                'packets': len(self.records), 'decoded_frames': self.total_frames,
                'decoded_bytes': self.total_pcm, 'raw_bytes': len(self.raw),
                'max_arrival_gap_seconds': self.max_gap, 'error': self.error,
                'max_continuous_arrival_gap_seconds': self.max_continuous_gap, 'source_barriers': self.barriers,
                'raw_byte_limit': MAX_RTP_BYTES, 'decoded_byte_limit': MAX_PCM_BYTES,
                'packet_limit': MAX_PACKETS,
                'timing_scope': 'RTP sample-count clock and receive callbacks; no speaker presentation/acoustic timestamps'}

    def retain(self, directory):
        directory = Path(directory)
        files = {'rtp': directory/'actual-sbc-rtp.length-prefixed',
                 'pcm': directory/'actual-sbc-decoded-s16le-stereo.pcm',
                 'timestamps': directory/'actual-sbc-transport-timestamps.json'}
        for kind, path in files.items():
            with path.open('xb') as output:
                path.chmod(0o600)
                if kind == 'rtp':
                    output.write(self.raw)
                elif kind == 'pcm':
                    for data in self.chunks:
                        output.write(data)
                else:
                    output.write((json.dumps(self.records, separators=(',', ':'))+'\n').encode())
        receipts = {}
        for kind, path in files.items():
            digest = hashlib.sha256()
            with path.open('rb') as retained:
                while data := retained.read(65536):
                    digest.update(data)
            receipts[kind] = {'path': str(path), 'sha256': digest.hexdigest(), 'bytes': path.stat().st_size}
        return receipts


class DecodedGuard:
    """Retain every decoded block; plateau checks handle SBC quantization."""
    def __init__(self, capture):
        self.capture, self.index, self.error = capture, 0, None
        self.carrier, self.baseline, self.checked, self.failed_block = None, None, 0, None
        self.retired_rms_limit = None
        self.steady_phase, self.phases = None, []

    def arm(self, frequency, baseline):
        require(frequency in (440, 660) and np.isfinite(baseline) and baseline > 1000,
                'Decoded carrier guard lacks an independently audible baseline')
        self.carrier, self.baseline = frequency, baseline

    def begin_transition(self, *, clear_carrier=False):
        self.check()
        boundary = {'first_block': len(self.capture.chunks), 'trigger_monotonic_ns': time.monotonic_ns(),
                    'previous_ratio': self.steady_phase['ratio'] if self.steady_phase else 1.,
                    'previous_voice': self.steady_phase['voice'] if self.steady_phase else False}
        self.steady_phase = None
        if clear_carrier:
            self.carrier = None
        return boundary

    def qualify(self, frequency, baseline, ratio, voice, first_block, label):
        require(frequency in (440, 660) and type(first_block) is int
                and 0 <= first_block < len(self.capture.chunks) and ratio in (.125, .2, 1.)
                and type(voice) is bool and np.isfinite(baseline) and baseline > 1000,
                'Invalid exact codec phase qualification')
        self.carrier, self.baseline = frequency, baseline
        self.steady_phase = {'label': label, 'frequency': frequency, 'ratio': ratio,
                             'voice': voice, 'first_block': first_block, 'checked_blocks': 0,
                             'last_checked_block': first_block-1}
        self.phases.append(self.steady_phase)
        # Replay the complete qualifying window plus anything already queued
        # after it. Prior permissive transition checks cannot erase that tail.
        for index in range(first_block, len(self.capture.chunks)):
            self._steady(fit_tones(self.capture.chunks[index]), index)

    def _steady(self, measured, index):
        phase = self.steady_phase
        if phase is None or index <= phase['last_checked_block']:
            return
        try:
            ratio = measured[f'amplitude_{phase["frequency"]}']/self.baseline
            minimum = measured[f'minimum_channel_amplitude_{phase["frequency"]}']/self.baseline
            require(.95*phase['ratio'] < minimum <= ratio < 1.05*phase['ratio'],
                    'An individual qualified SBC block changed its exact music gain')
            allowed = {phase['frequency']} | ({880} if phase['voice'] else set())
            require(max(measured[f'amplitude_{frequency}'] for frequency in (440, 660, 880, 1320)
                        if frequency not in allowed) <= CODEC_TONE_FLOOR
                    and measured['dc'] <= CODEC_TONE_FLOOR,
                    'An individual qualified SBC block replayed a forbidden source or voice')
            if phase['voice']:
                require(measured['minimum_channel_amplitude_880'] > CODEC_TONE_FLOOR,
                        'An individual qualified SBC block lost its own speech')
            phase['checked_blocks'] += 1
            phase['last_checked_block'] = index
        except RuntimeFailure as exc:
            self.error, self.failed_block = str(exc), index
            raise

    def _retired(self, measured, index):
        try:
            require(measured['rms'] < self.retired_rms_limit,
                    'An individual SBC block replayed audible content after exact source retirement')
            require(max(measured[f'amplitude_{frequency}'] for frequency in (440, 660, 880, 1320))
                    <= CODEC_TONE_FLOOR and measured['dc'] <= CODEC_TONE_FLOOR,
                    'An individual SBC block replayed a carrier or DC after exact source retirement')
        except RuntimeFailure as exc:
            self.error, self.failed_block = str(exc), index
            raise

    def retire(self, maximum_rms, *, first_block=None):
        require(self.retired_rms_limit is None and np.isfinite(maximum_rms) and maximum_rms > 0,
                'Invalid codec-aware retired-stream evidence')
        if first_block is not None:
            require(type(first_block) is int and 0 <= first_block <= len(self.capture.chunks),
                    'Invalid exact retirement observation boundary')
        self.check(live=False)
        self.carrier, self.steady_phase, self.retired_rms_limit = None, None, maximum_rms
        if first_block is not None:
            for index in range(first_block, len(self.capture.chunks)):
                self._retired(fit_tones(self.capture.chunks[index]), index)

    def check(self, *, live=True):
        require(self.error is None, self.error or 'Decoded content observer failed')
        try:
            self.capture.check(live=live)
            while self.index < len(self.capture.chunks):
                measured = fit_tones(self.capture.chunks[self.index])
                require(measured['peak'] < 32760, 'Decoded SBC output clipped')
                if self.carrier is not None:
                    # The minimum legitimate tested level is cubic volume50
                    # (0.125), lower than the declared0.2 speech duck. This
                    # presence fence does not replace strict plateau ratios.
                    require(measured[f'minimum_channel_amplitude_{self.carrier}'] > self.baseline*.05,
                            'An individual decoded SBC block lost its native music carrier')
                if self.retired_rms_limit is not None:
                    self._retired(measured, self.index)
                self._steady(measured, self.index)
                self.index += 1
                self.checked += 1
        except RuntimeFailure as exc:
            self.error = str(exc)
            self.failed_block = self.index
            raise

    def evidence(self):
        return {'checked_blocks': self.checked, 'failed_block': self.failed_block,
                'carrier': self.carrier, 'error': self.error, 'retired_rms_limit': self.retired_rms_limit,
                'qualified_phases': self.phases,
                'scope': 'Every qualified SBC block: exact music gain and forbidden-carrier/DC floors; no raw PCM residual or acoustic fidelity'}


class Plateau:
    """Require0.4s consecutive real decoded audio, never a stage median."""
    def __init__(self, baseline, ratio, *, voice, voice_floor, previous_voice=0,
                 first_block=0, previous_ratio=1., previous_voice_active=False):
        require(np.isfinite(baseline) and baseline > 1000 and ratio in (.125, .2, 1.), 'Invalid codec plateau reference')
        require(type(voice) is bool and np.isfinite(voice_floor) and 0 <= voice_floor <= CODEC_TONE_FLOOR,
                'Codec voice reference is not a verified pure baseline')
        require(type(first_block) is int and first_block >= 0 and previous_ratio in (.125, .2, 1.)
                and type(previous_voice_active) is bool, 'Invalid declared transition boundary')
        self.baseline, self.ratio, self.voice, self.voice_floor = baseline, ratio, voice, voice_floor
        self.previous_voice = previous_voice
        self.frames, self.blocks, self.first_at, self.last, self.passed = 0, 0, None, None, False
        self.last_at = None
        self.first_block, self.qualifying_first, self.observed_frames = first_block, None, 0
        self.previous_ratio, self.previous_voice_active = previous_ratio, previous_voice_active
        self.changed_frame, self.settled, self.failure, self.observations = None, False, None, []
        law_frames = RATE*40//1000 if voice else RATE*500//1000 if previous_voice_active else 0
        self.settle_frames = law_frames + CODEC_SETTLE_FRAMES

    def push(self, data, at):
        require(self.failure is None, self.failure or 'Codec transition already failed')
        require(type(at) in (int, float) and np.isfinite(at) and at > 0
                and (self.last_at is None or at > self.last_at), 'Plateau transport arrivals did not advance')
        self.last_at = at
        measured = fit_tones(data)
        ratio = measured['amplitude_440']/self.baseline
        minimum_ratio = measured['minimum_channel_amplitude_440']/self.baseline
        voice = measured['amplitude_880']
        if self.voice:
            match = (.95*self.ratio < minimum_ratio <= ratio < 1.05*self.ratio
                     and measured['minimum_channel_amplitude_880'] > max(CODEC_TONE_FLOOR, self.voice_floor*8))
        else:
            # The fixed codec floor is admitted only against independently
            # verified pure music; active voice never increases the floor.
            match = (.95*self.ratio < minimum_ratio <= ratio < 1.05*self.ratio
                     and voice <= CODEC_TONE_FLOOR)
        # Select only a window that already meets the unchanged qualified
        # guard. A legitimate restoration can enter the5% gain range while
        # its amplitude slope still projects onto a forbidden frequency.
        allowed = {440} | ({880} if self.voice else set())
        match = (match and max(measured[f'amplitude_{frequency}'] for frequency in (440, 660, 880, 1320)
                              if frequency not in allowed) <= CODEC_TONE_FLOOR
                 and measured['dc'] <= CODEC_TONE_FLOOR)
        block = self.first_block+len(self.observations)
        require(len(self.observations) < 4096, 'Codec transition exceeded its bounded block receipts')
        self.observations.append({'block': block, 'arrival_monotonic_ns': round(at*1e9),
                                  'music_ratio': ratio, 'minimum_music_ratio': minimum_ratio,
                                  'voice': voice, 'matched': match, 'frames': measured['frames']})
        previous_match = (.95*self.previous_ratio < minimum_ratio <= ratio < 1.05*self.previous_ratio
                          and (voice > CODEC_TONE_FLOOR) == self.previous_voice_active)
        if not previous_match and self.changed_frame is None:
            self.changed_frame = self.observed_frames
        if self.settled and not match:
            self.failure = 'Decoded SBC changed again after reaching the requested plateau'
        elif (not self.settled and self.changed_frame is not None
              and self.observed_frames-self.changed_frame > self.settle_frames):
            self.failure = 'Decoded SBC transition exceeded its declared gain/lease/codec settling law'
        self.observed_frames += measured['frames']
        if self.failure is not None:
            raise RuntimeFailure(self.failure)
        if match:
            self.settled = True
        self.last, self.passed = measured, False
        if not match:
            self.frames, self.blocks, self.first_at = 0, 0, None
            self.qualifying_first = None
            return False
        if self.first_at is None:
            self.first_at = at
            self.qualifying_first = block
        self.frames += measured['frames']
        self.blocks += 1
        self.passed = self.frames >= RATE*.4 and self.blocks >= 3 and at-self.first_at >= .35
        return self.passed

    def evidence(self):
        return {'passed': self.passed, 'expected_music_ratio': self.ratio, 'voice_expected': self.voice,
                'consecutive_frames': self.frames, 'consecutive_blocks': self.blocks, 'last_spectrum': self.last,
                'observed_prefix_blocks': self.observations, 'qualifying_first_block': self.qualifying_first,
                'settling_frame_bound': self.settle_frames, 'failure': self.failure,
                'scope': 'Decoded SBC plateau; preserved5% ratio bounds, independent codec noise baseline'}


class RouteBlueZ(private.MockBlueZ):
    def __init__(self, bus, capture):
        super().__init__(bus)
        self.capture = capture
        self.drain_eof = None
        self.drain_stop_boundary = None

    def handle(self, message):
        require(len(self.calls) < MAX_CONTROLS, 'Private BlueZ exceeded its bounded call audit')
        return super().handle(message)

    async def drain(self):
        try:
            while True:
                try:
                    data, controls, flags, _address = self.rtp.recvmsg(MAX_PACKET_BYTES+1, socket.CMSG_SPACE(16*4),
                        getattr(socket, 'MSG_CMSG_CLOEXEC', 0))
                    descriptors = []
                    for level, kind, payload in controls:
                        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                            values = array.array('i')
                            values.frombytes(payload[:len(payload)-len(payload) % values.itemsize])
                            descriptors.extend(values)
                    try:
                        require(not controls and not flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC),
                                'SBC transport was truncated or carried descriptors')
                    finally:
                        _close_fds(descriptors)
                    if not data:
                        self.drain_eof = {'observed_monotonic_ns': time.monotonic_ns(),
                                         'packets': len(self.capture.records), 'decoded_frames': self.capture.total_frames}
                        return
                    self.capture.feed(data, time.monotonic())
                    self.rtp_packets += 1
                    self.rtp_bytes += len(data)
                except BlockingIOError:
                    from shiri.runtime.bluealsa import _socket_ready
                    try:
                        await _socket_ready(self.rtp, timeout=1)
                    except asyncio.TimeoutError:
                        # Keep the same decoder/socket during legitimate idle.
                        continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.capture.error = self.capture.error or type(exc).__name__+': '+str(exc)[:200]
            raise

    async def close(self):
        """Retire the local producer descriptor, then consume its exact tail.

        PrivateDaemon calls this only after proving its direct encoder child
        stopped. The mock's original sender descriptor is the other producer;
        closing it permits real EOF after all queued transport packets. A
        cancelled observer or swallowed drain exception cannot prove that tail.
        """
        self.drain_stop_boundary = {'requested_monotonic_ns': time.monotonic_ns()}
        try:
            if self.active_handle:
                self.active_handle.cancel()
                self.active_handle = None
            if self.transport_fd:
                self.transport_fd.close()
                self.transport_fd = None
            self.drain_stop_boundary['producer_descriptor_closed_monotonic_ns'] = time.monotonic_ns()
            if self.drain_task is None:
                require(self.rtp is None, 'Acquired transport has no owned drain task')
                self.drain_stop_boundary['no_acquired_transport'] = True
            else:
                require(not self.drain_task.cancelled(), 'Cancelled SBC drain cannot prove transport retirement')
                try:
                    await asyncio.wait_for(asyncio.shield(self.drain_task), 2)
                except asyncio.TimeoutError:
                    self.drain_task.cancel()
                    await asyncio.wait_for(asyncio.gather(self.drain_task, return_exceptions=True), 1)
                    raise RuntimeFailure('SBC drain did not reach real EOF after exact producers stopped') from None
                require(self.drain_eof is not None and self.drain_eof['packets'] == len(self.capture.records),
                        'SBC drain stopped without consuming the exact transport tail to EOF')
                self.drain_stop_boundary['eof'] = dict(self.drain_eof)
            self.drain_stop_boundary['retired_monotonic_ns'] = time.monotonic_ns()
        finally:
            # Failed media validation still closes its exact owned resources.
            # Do not convert a failed or cancelled drain into successful EOF.
            if self.drain_task is not None and not self.drain_task.done():
                self.drain_task.cancel()
                await asyncio.wait_for(asyncio.gather(self.drain_task, return_exceptions=True), 1)
            if self.rtp:
                self.rtp.close()
                self.rtp = None
            self.drain_stop_boundary['receiver_descriptor_closed_monotonic_ns'] = time.monotonic_ns()


class PrivateDaemon:
    """Actual maintained BlueALSA, private bus and exact mock transport only."""
    def __init__(self, directory, binary, digest):
        self.directory, self.binary, self.digest = Path(directory), Path(binary), digest
        self.bus_process = self.daemon = self.bus = self.mock = None
        self.capture, self.manager = None, None
        self.directory_fd = None
        self.directory_identity = None
        self.log_path = self.directory/'bluealsa.log'
        self.startup_diagnostics = {'errors': [], 'omitted_errors': 0}
        self.receipt = {'started': False, 'host_bus_used': False, 'physical_adapter_used': False, 'cleanup': {}}

    def create_directory(self, group):
        """Pin only a freshly created parent; a collision is never adopted."""
        require(self.directory_identity is None, 'Private directory was already created')
        self.directory.mkdir(mode=0o710)
        descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        self.directory_fd = descriptor
        info = os.fstat(descriptor)
        self.directory_identity = (info.st_dev, info.st_ino, info.st_uid)
        os.chown(self.directory, -1, group)

    def remove_directory(self):
        """Remove the exact created parent only after all owned resources stop."""
        if self.directory_identity is None:
            require(not self.directory.exists() and not self.directory.is_symlink(),
                    'Refusing to remove a private directory not created by this fixture')
            return
        require(self.directory_fd is not None and self.directory_identity == (
            os.fstat(self.directory_fd).st_dev, os.fstat(self.directory_fd).st_ino,
            os.fstat(self.directory_fd).st_uid), 'Held private directory changed identity')
        info = self.directory.lstat()
        require(stat.S_ISDIR(info.st_mode) and not info.st_mode & 0o022
                and self.directory_identity == (info.st_dev, info.st_ino, info.st_uid),
                'Private directory pathname no longer names the exact owned parent')
        shutil.rmtree(self.directory)
        os.close(self.directory_fd)
        self.directory_fd = None

    async def start(self):
        require(not self.receipt['started'] and not Path('/sys/class/bluetooth/hci15').exists(),
                'Synthetic adapter must be absent; refuse physical adapter access')
        self.binary, self.digest = private.validated_binary(self.binary, self.digest)
        account = pwd.getpwnam('nobody')
        require(account.pw_uid > 0, 'Actual private daemon needs an existing unprivileged nobody account')
        self.create_directory(account.pw_gid)
        state = self.directory/'state'
        state.mkdir(mode=0o700)
        os.chown(state, account.pw_uid, account.pw_gid)
        cc = shutil.which('cc')
        require(cc is not None, 'The private daemon guard requires a Linux C compiler')
        guard = self.directory/'unix-only'
        built = subprocess.run([cc, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',  # noqa: ASYNC221
                                str(HERE.parents[1]/'native/private_bluealsa_exec.c'), '-o', str(guard)],
                               capture_output=True, timeout=30)
        require(built.returncode == 0, 'Private AF_UNIX guard did not compile')
        guard.chmod(0o755)
        path, config = self.directory/'bus.sock', self.directory/'bus.conf'
        config.write_text('<busconfig><type>system</type><listen>unix:path='+str(path)
            +'</listen><auth>EXTERNAL</auth><policy context="default"><allow user="*"/>'
             '<allow own="*"/><allow send_destination="*"/><allow receive_sender="*"/>'
             '</policy></busconfig>')
        config.chmod(0o600)
        address = 'unix:path='+str(path)
        self.bus_process = await asyncio.create_subprocess_exec('dbus-daemon', '--nofork', '--nopidfile',
            '--config-file='+str(config), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        async def socket_ready():
            while not path.exists():
                require(self.bus_process.returncode is None, 'Private bus exited during startup')
                await asyncio.sleep(.01)
        await _bounded(socket_ready(), 3)
        self.bus = MessageBus(bus_address=address, negotiate_unix_fd=True)
        await _bounded(self.bus.connect(), 3)
        await _bounded(self.bus.request_name('org.bluez'), 3)
        self.capture = RtpCapture()
        self.mock = RouteBlueZ(self.bus, self.capture)
        self.bus.add_message_handler(self.mock.handle)
        environment = {'PATH': '/usr/bin:/bin', 'DBUS_SYSTEM_BUS_ADDRESS': address,
                       'DBUS_SESSION_BUS_ADDRESS': address, 'STATE_DIRECTORY': str(state), 'TMPDIR': str(state)}
        require(Path('/usr/bin/prlimit').is_file(), 'Actual private daemon needs the kernel file-size limit launcher')
        with self.log_path.open('xb') as log:
            self.log_path.chmod(0o600)
            self.daemon = await asyncio.create_subprocess_exec('/usr/bin/prlimit',
                f'--fsize={MAX_LOG_BYTES}:{MAX_LOG_BYTES}', '--', str(guard), str(self.binary), '--device=hci15',
                '--profile=a2dp-source', '--codec=SBC', '--initial-volume=100', '--keep-alive=0',
                '--disable-realtek-usb-fix', '--loglevel=debug', env=environment, user=account.pw_uid, group=account.pw_gid,
                extra_groups=[], stdout=log, stderr=log)
        owner, app = await _bounded(self.mock.registered, 5)
        status = dict(line.split(':', 1) for line in Path(f'/proc/{self.daemon.pid}/status').read_text().splitlines()
                      if ':' in line)
        require(int(status['Uid'].split()[1]) == account.pw_uid and not int(status['CapEff'].strip(), 16)
                and status['NoNewPrivs'].strip() == '1' and status['Seccomp'].strip() == '2'
                and Path(f'/proc/{self.daemon.pid}/exe').resolve(strict=True) == self.binary,
                'Actual private daemon lost its unprivileged immutable guarded identity')
        objects = (await private.request(self.bus, owner, app, private.OBJECTS, 'GetManagedObjects')).body[0]
        endpoints = [path for path, interfaces in objects.items() if 'org.bluez.MediaEndpoint1' in interfaces
                     and interfaces['org.bluez.MediaEndpoint1'].get('UUID', Variant('s', '')).value == private.SOURCE_UUID
                     and interfaces['org.bluez.MediaEndpoint1'].get('Codec', Variant('y', 255)).value == 0]
        require(bool(endpoints), 'Actual maintained daemon did not export an SBC source')
        endpoint = sorted(endpoints)[0]
        selected = await private.request(self.bus, owner, endpoint, 'org.bluez.MediaEndpoint1',
                                         'SelectConfiguration', 'ay', [b'\xff\xff\x02\x35'])
        properties = self.mock.transport_properties() | {'Configuration': Variant('ay', selected.body[0])}
        await private.request(self.bus, owner, endpoint, 'org.bluez.MediaEndpoint1', 'SetConfiguration',
                              'oa{sv}', [private.TRANSPORT, properties])
        async def factory():
            connection = MessageBus(bus_address=address, negotiate_unix_fd=True)
            scoped = diagnostics.DiagnosticBus(connection, self.startup_diagnostics)
            try:
                await _bounded(connection.connect(), 3)
                return scoped
            except BaseException:
                await scoped.close()
                raise
        self.manager = BlueALSA(transport_factory=factory, service_uid=account.pw_uid)
        self.receipt.update(started=True, binary_sha256=self.digest, daemon_uid=account.pw_uid,
            guard={name: status[name].strip() for name in ('Uid', 'Gid', 'CapEff', 'NoNewPrivs', 'Seccomp')},
            selected_sbc_configuration=bytes(selected.body[0]).hex())
        return self.manager

    def healthy(self):
        require(self.daemon is not None and self.daemon.returncode is None
                and self.bus_process.returncode is None, 'Actual private daemon or private bus exited')
        require(self.mock is not None and (self.mock.drain_task is None or not self.mock.drain_task.done()),
                'Actual private RTP observer terminated')
        require(self.log_path.stat().st_size <= MAX_LOG_BYTES,
                'Actual private daemon exceeded its bounded diagnostic log')
        if self.capture is not None:
            self.capture.check()

    async def close(self, *, leases_released):
        require(leases_released is True and (self.manager is None or not self.manager.leases),
                'Stop exact broker workers and release their leases before private daemon teardown')
        errors = []
        async def cleanup(label, operation):
            try:
                await _bounded(operation, 5)
                self.receipt['cleanup'][label] = True
            except BaseException as exc:
                self.receipt['cleanup'][label] = False
                errors.append(label+': '+type(exc).__name__)
        async def stop_encoder():
            requested = time.monotonic_ns()
            await private.stop(self.daemon)
            require(self.daemon is None or self.daemon.returncode is not None,
                    'Actual encoder child still lives; transport tail cannot retire')
            self.receipt['encoder_stop_boundary'] = {
                'requested_monotonic_ns': requested, 'verified_monotonic_ns': time.monotonic_ns(),
                'pid': self.daemon.pid if self.daemon is not None else None,
                'returncode': self.daemon.returncode if self.daemon is not None else None,
            }
        await cleanup('actual_daemon_stopped', stop_encoder())
        if self.mock is not None:
            await cleanup('mock_transport_closed', self.mock.close())
            self.receipt['transport_stop_boundary'] = getattr(self.mock, 'drain_stop_boundary', None)
        if self.bus is not None:
            await cleanup('private_bus_client_closed', DescriptorBus(self.bus).close())
        await cleanup('private_bus_stopped', private.stop(self.bus_process))
        self.receipt['cleanup_errors'] = errors
        require(not errors, 'Private Bluetooth fixture cleanup failed')


def retain_receipt(directory, report):
    """A bounded private receipt survives disposable room cleanup."""
    atomic_json(Path(directory)/'bluetooth-route-evidence.json', report)
