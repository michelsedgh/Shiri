"""Final-PCM instruments and owned API handles for native Linux qualification.

GStreamer is used only as a laboratory capture instrument. It does not mix or
route production audio. Every capture caller provides its admitted device.
"""
import asyncio
from collections import deque
import importlib.util
from pathlib import Path
import time

import numpy as np

from shiri.runtime.system import process_birth

_spec = importlib.util.spec_from_file_location('native_lab_audio', Path(__file__).with_name('native_lab_audio.py'))
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)
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



    async def close(self):
        await self.client.aclose()


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
    """Bounded observation of one explicitly admitted laboratory ALSA device."""
    def __init__(self, device, *, start=True):
        self.device = device
        import gi
        gi.require_version('Gst', '1.0')
        gi.require_version('GstAudio', '1.0')
        from gi.repository import Gst, GstAudio
        Gst.init(None)
        self.Gst, self.GstAudio = Gst, GstAudio
        self.pipeline = Gst.Pipeline.new('native-final-output-capture')
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
            self.source = element('alsasrc', 'admitted-device-capture', device=device,
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
        return {'capture': self.device, 'format': self.format, 'rate': self.rate, 'channels': self.channels,
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
