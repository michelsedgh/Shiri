#!/usr/bin/env python3
"""Private finite PCM fixture for independent Loopback capture-clock measurement.

Invoked only by check_native_grouping.py inside a tracked, filtered output-UID
unit. It uses kernel MONOTONIC PCM status and queue delay, never desired render
timestamps, as the digital reference. No physical speaker or phone is involved.
Importing this module opens no library, socket, device or kernel directory.
"""
from __future__ import annotations

from array import array
import ctypes as C
import json
import math
import os
from pathlib import Path
import signal
import stat
import sys
import time
from uuid import UUID

RATE = 48000
FRAMES = 960
DURATION = 6
CHIP_FRAMES = RATE*120//1000


def require(value, message):
    if not value:
        raise ValueError(message)


def envelope(frame):
    chip = frame//CHIP_FRAMES
    value = (chip+1)*0x9E3779B1 & 0xFFFFFFFF
    value ^= value >> 16
    value = value*0x85EBCA6B & 0xFFFFFFFF
    value ^= value >> 13
    return 1. if value & 1 else .65


def pcm_frames(first, count):
    pcm = array('h')
    for frame in range(first, first+count):
        value = int(8192*envelope(frame)*math.sin(2*math.pi*440*frame/RATE))
        pcm.extend((value, value))
    if sys.byteorder != 'little':
        pcm.byteswap()
    return pcm.tobytes()


def queue_origin(timestamp_ns, submitted, delay, rate):
    """Queue delay is remaining frames AFTER the submitted-frame observation."""
    require(all(type(value) is int for value in (timestamp_ns, submitted, delay, rate))
            and timestamp_ns > 0 and rate > 0 and 0 <= delay <= submitted,
            'Invalid kernel PCM queue anchor')
    return timestamp_ns-(submitted-delay)*1_000_000_000//rate


def write_json(path, value):
    temporary = path.with_suffix('.writing')
    with temporary.open('w', encoding='utf-8') as stream:
        temporary.chmod(0o600)
        json.dump(value, stream, separators=(',', ':'))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class Timespec(C.Structure):
    _fields_ = [('seconds', C.c_long), ('nanoseconds', C.c_long)]

    def ns(self):
        require(self.seconds >= 0 and 0 <= self.nanoseconds < 1_000_000_000, 'Invalid kernel PCM timestamp')
        return self.seconds*1_000_000_000+self.nanoseconds


class Pollfd(C.Structure):
    _fields_ = [('fd', C.c_int), ('events', C.c_short), ('revents', C.c_short)]


class PCM:
    def __init__(self, manifest):
        self.library = C.CDLL('libasound.so.2')
        self.handle = C.c_void_p()
        self.hw = C.c_void_p()
        self.sw = C.c_void_p()
        self.status = C.c_void_p()
        self.info = C.c_void_p()
        self.held_card = -1
        self.manifest = manifest
        self.timestamp_started = False
        void, integer = C.c_void_p, C.c_int
        signatures = {
            'snd_strerror': ([integer], C.c_char_p),
            'snd_pcm_open': ([C.POINTER(void), C.c_char_p, integer, integer], integer),
            'snd_pcm_close': ([void], integer), 'snd_pcm_drop': ([void], integer),
            'snd_pcm_drain': ([void], integer),
            'snd_pcm_state': ([void], integer),
            'snd_pcm_writei': ([void, void, C.c_ulong], C.c_long),
            'snd_pcm_hw_params_any': ([void, void], integer),
            'snd_pcm_hw_params_set_access': ([void, void, integer], integer),
            'snd_pcm_hw_params_set_format': ([void, void, integer], integer),
            'snd_pcm_hw_params_set_channels': ([void, void, C.c_uint], integer),
            'snd_pcm_hw_params_set_rate_near': ([void, void, C.POINTER(C.c_uint), C.POINTER(integer)], integer),
            'snd_pcm_hw_params_set_period_size_near': ([void, void, C.POINTER(C.c_ulong), C.POINTER(integer)], integer),
            'snd_pcm_hw_params_set_buffer_size_near': ([void, void, C.POINTER(C.c_ulong)], integer),
            'snd_pcm_hw_params_get_period_size': ([void, C.POINTER(C.c_ulong), C.POINTER(integer)], integer),
            'snd_pcm_hw_params_get_buffer_size': ([void, C.POINTER(C.c_ulong)], integer),
            'snd_pcm_hw_params': ([void, void], integer),
            'snd_pcm_sw_params_current': ([void, void], integer),
            'snd_pcm_sw_params_set_start_threshold': ([void, void, C.c_ulong], integer),
            'snd_pcm_sw_params_set_avail_min': ([void, void, C.c_ulong], integer),
            'snd_pcm_sw_params_set_tstamp_mode': ([void, void, integer], integer),
            'snd_pcm_sw_params_set_tstamp_type': ([void, void, integer], integer),
            'snd_pcm_sw_params': ([void, void], integer),
            'snd_pcm_status': ([void, void], integer),
            'snd_pcm_status_get_state': ([void], integer),
            'snd_pcm_status_get_delay': ([void], C.c_long),
            'snd_pcm_status_get_htstamp': ([void, C.POINTER(Timespec)], None),
            'snd_pcm_status_get_trigger_htstamp': ([void, C.POINTER(Timespec)], None),
            'snd_pcm_info': ([void, void], integer),
            'snd_pcm_info_get_card': ([void], integer),
            'snd_pcm_info_get_device': ([void], C.c_uint),
            'snd_pcm_info_get_subdevice': ([void], C.c_uint),
            'snd_pcm_info_get_stream': ([void], integer),
            'snd_pcm_poll_descriptors_count': ([void], integer),
            'snd_pcm_poll_descriptors': ([void, C.POINTER(Pollfd), C.c_uint], integer),
        }
        for kind in ('hw_params', 'sw_params', 'status', 'info'):
            signatures[f'snd_pcm_{kind}_malloc'] = ([C.POINTER(void)], integer)
            signatures[f'snd_pcm_{kind}_free'] = ([void], None)
        for name, (arguments, result) in signatures.items():
            function = getattr(self.library, name)
            function.argtypes, function.restype = arguments, result
        try:
            self.validate_identity(before=True)
            self.call('snd_pcm_open', C.byref(self.handle), b'shiri', 0, 1)  # playback/nonblocking
            for kind, pointer in (('hw_params', self.hw), ('sw_params', self.sw), ('status', self.status), ('info', self.info)):
                self.call(f'snd_pcm_{kind}_malloc', C.byref(pointer))
            self.call('snd_pcm_info', self.handle, self.info)
            actual = [getattr(self.library, f'snd_pcm_info_get_{key}')(self.info)
                      for key in ('card', 'device', 'subdevice', 'stream')]
            require(actual == [manifest['card_index'], manifest['device'], manifest['subdevice'], 0],
                    'Opened PCM differs from the exact pinned playback identity')
            count = self.library.snd_pcm_poll_descriptors_count(self.handle)
            require(0 < count <= 16, 'Opened PCM has no bounded underlying poll descriptors')
            descriptors = (Pollfd*count)()
            self.call('snd_pcm_poll_descriptors', self.handle, descriptors, count)
            matched = False
            for item in descriptors:
                info = os.fstat(item.fd)
                if stat.S_ISCHR(info.st_mode):
                    require((info.st_dev, info.st_ino, info.st_rdev) == self.node_identity,
                            'Opened PCM exposes a different character node')
                    matched = True
            require(matched, 'Opened PCM does not expose the pinned playback node')
            self.validate_identity()
            self.call('snd_pcm_hw_params_any', self.handle, self.hw)
            self.call('snd_pcm_hw_params_set_access', self.handle, self.hw, 3)
            self.call('snd_pcm_hw_params_set_format', self.handle, self.hw, 2)
            self.call('snd_pcm_hw_params_set_channels', self.handle, self.hw, 2)
            rate, direction = C.c_uint(RATE), C.c_int(0)
            self.call('snd_pcm_hw_params_set_rate_near', self.handle, self.hw, C.byref(rate), C.byref(direction))
            require(rate.value == RATE, 'Independent fixture did not negotiate exact48kHz')
            period, buffer = C.c_ulong(FRAMES), C.c_ulong(FRAMES*6)
            self.call('snd_pcm_hw_params_set_period_size_near', self.handle, self.hw, C.byref(period), C.byref(direction))
            self.call('snd_pcm_hw_params_set_buffer_size_near', self.handle, self.hw, C.byref(buffer))
            self.call('snd_pcm_hw_params', self.handle, self.hw)
            self.call('snd_pcm_hw_params_get_period_size', self.hw, C.byref(period), C.byref(direction))
            self.call('snd_pcm_hw_params_get_buffer_size', self.hw, C.byref(buffer))
            require(0 < period.value <= FRAMES*2 and period.value < buffer.value <= RATE,
                    'Independent fixture negotiated an unbounded or unusable period/buffer')
            self.period, self.buffer = period.value, buffer.value
            self.call('snd_pcm_sw_params_current', self.handle, self.sw)
            self.call('snd_pcm_sw_params_set_start_threshold', self.handle, self.sw, 1)
            self.call('snd_pcm_sw_params_set_avail_min', self.handle, self.sw, self.period)
            self.call('snd_pcm_sw_params_set_tstamp_mode', self.handle, self.sw, 1)
            self.call('snd_pcm_sw_params_set_tstamp_type', self.handle, self.sw, 1)  # MONOTONIC, not realtime
            self.call('snd_pcm_sw_params', self.handle, self.sw)
        except BaseException:
            self.close()
            raise

    def call(self, name, *arguments):
        result = getattr(self.library, name)(*arguments)
        if result < 0:
            raise ValueError(f'{name} failed: {self.library.snd_strerror(result).decode()}')
        return result

    def validate_identity(self, *, before=False):
        manifest = self.manifest
        require(Path('/proc/sys/kernel/random/boot_id').read_text().strip() == manifest['boot_id'], 'PCM kernel boot changed')
        if before:
            self.held_card = os.open(manifest['sysfs_path'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        held = os.fstat(self.held_card)
        current = Path(manifest['sysfs_path']).stat(follow_symlinks=False)
        require(stat.S_ISDIR(current.st_mode) and (held.st_dev, held.st_ino) ==
                (current.st_dev, current.st_ino) == (manifest['sysfs_st_dev'], manifest['sysfs_st_ino']),
                'Selected kernel card directory changed')
        node = Path(manifest['node_path']).stat(follow_symlinks=False)
        self.node_identity = manifest['node_st_dev'], manifest['node_st_ino'], manifest['node_st_rdev']
        require(stat.S_ISCHR(node.st_mode) and (node.st_dev, node.st_ino, node.st_rdev) == self.node_identity,
                'Pinned playback character node changed')

    def anchor(self, submitted):
        timestamp, trigger = Timespec(), Timespec()
        before = time.monotonic_ns()
        self.call('snd_pcm_status', self.handle, self.status)
        after = time.monotonic_ns()
        state = self.library.snd_pcm_status_get_state(self.status)
        require(state == 3, 'Independent PCM fixture stopped or underrun; do not recover it silently')
        self.library.snd_pcm_status_get_htstamp(self.status, C.byref(timestamp))
        self.library.snd_pcm_status_get_trigger_htstamp(self.status, C.byref(trigger))
        delay = self.library.snd_pcm_status_get_delay(self.status)
        require(after-before <= 1_000_000 and 0 <= delay <= self.buffer,
                'Kernel queue observation has excessive query uncertainty or invalid delay')
        # snd-aloop publishes its first status timestamp on the first timer
        # update. Its initial zero is not an observation of presentation time.
        # Admit only that bounded startup interval and never use it as an anchor.
        period_ns = self.period*1_000_000_000//RATE
        if timestamp.ns() == 0 and not self.timestamp_started:
            require(0 < trigger.ns() <= before and after-trigger.ns() <= 2*period_ns,
                    'Kernel PCM timestamp did not initialize within two periods')
            return None
        self.timestamp_started = True
        require(abs(timestamp.ns()-before) <= (after-before)+self.period*1_000_000_000//RATE,
                f'Kernel queue timestamp is not the configured fresh monotonic clock: '
                f'timestamp={timestamp.ns()}, before={before}, after={after}, '
                f'trigger={trigger.ns()}, period={self.period}, delay={delay}')
        return {'before_ns': before, 'after_ns': after, 'timestamp_ns': timestamp.ns(),
                'trigger_ns': trigger.ns(), 'delay_frames': delay, 'submitted_frames': submitted,
                'origin_ns': queue_origin(timestamp.ns(), submitted, delay, RATE)}

    def close(self):
        if self.handle:
            self.library.snd_pcm_drop(self.handle)
            self.library.snd_pcm_close(self.handle)
            self.handle = C.c_void_p()
        for kind, pointer in (('hw_params', self.hw), ('sw_params', self.sw), ('status', self.status), ('info', self.info)):
            if pointer:
                getattr(self.library, f'snd_pcm_{kind}_free')(pointer)
                pointer.value = None
        if self.held_card >= 0:
            os.close(self.held_card)
            self.held_card = -1


def run(config_path):
    config = json.loads(Path(config_path).read_text())
    require(set(config) == {'uid', 'gid', 'manifest', 'command', 'status'}, 'Malformed independent fixture configuration')
    require(os.geteuid() == config['uid'] != 0 and os.getegid() == config['gid'], 'Independent fixture credential mismatch')
    manifest = config['manifest']
    require(manifest['version'] == 1 and str(UUID(manifest['boot_id'])) == manifest['boot_id']
            and manifest['device'] in (0, 1) and manifest['subdevice'] == 7,
            'Independent fixture is restricted to the admitted virtual sub7 endpoint')
    stopped = False
    def stop(_number, _frame):
        nonlocal stopped
        stopped = True
    for name in (signal.SIGTERM, signal.SIGINT):
        signal.signal(name, stop)
    status_path, command = Path(config['status']), Path(config['command'])
    pcm = None
    state = {'stage': 'opening', 'uid': os.geteuid(), 'gid': os.getegid(), 'pid': os.getpid(),
             'submitted_frames': 0, 'anchors': [], 'finished': False, 'format': 'S16LE', 'rate': RATE, 'channels': 2}
    write_json(status_path, state)
    deadline = time.monotonic()+25
    try:
        pcm = PCM(manifest)
        state.update(stage='ready', period_frames=pcm.period, buffer_frames=pcm.buffer)
        write_json(status_path, state)
        while json.loads(command.read_text()) != {'action': 'run'}:
            require(not stopped and time.monotonic() < deadline, 'Independent fixture did not receive a bounded start')
            time.sleep(.01)
        submitted = 0
        while submitted < DURATION*RATE:
            require(not stopped and time.monotonic() < deadline, 'Independent PCM delivery exceeded its bound')
            frames = min(FRAMES, DURATION*RATE-submitted)
            data = pcm_frames(submitted, frames)
            buffer = C.create_string_buffer(data)
            result = pcm.library.snd_pcm_writei(pcm.handle, buffer, frames)
            if result == -11:  # EAGAIN is pacing, never XRUN recovery.
                time.sleep(.002)
                continue
            require(0 < result <= frames, 'Independent PCM write failed or made no bounded progress')
            submitted += result
            anchor = pcm.anchor(submitted)
            if anchor is not None:
                state['anchors'].append(anchor)
            require(len(state['anchors']) <= 2000, 'Independent queue observations exceeded their bound')
            state.update(stage='playing', submitted_frames=submitted)
            write_json(status_path, state)
        drained = pcm.library.snd_pcm_drain(pcm.handle)
        require(drained in (0, -11), 'Independent PCM drain failed')
        while drained != 0:
            # The nonblocking drain ioctl returns EAGAIN even when the kernel
            # has subsequently stopped. Refresh actual status rather than
            # repeatedly initiating drain against a stale userspace snapshot.
            pcm.call('snd_pcm_status', pcm.handle, pcm.status)
            current = pcm.library.snd_pcm_status_get_state(pcm.status)
            state['drain_state'] = current
            state['drain_delay_frames'] = pcm.library.snd_pcm_status_get_delay(pcm.status)
            if current == 1:  # SETUP: kernel finished draining and stopped.
                break
            require(current == 5, 'Independent PCM left its exact draining state')
            require(not stopped and time.monotonic() < deadline, 'Independent PCM drain exceeded its bound')
            time.sleep(.005)
        pcm.validate_identity()
        state.update(stage='finished', finished=True)
    except BaseException as exc:
        state.update(stage='failed', error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        if pcm:
            pcm.close()
        state['closed'] = True
        write_json(status_path, state)


if __name__ == '__main__':
    require(len(sys.argv) == 2, 'One private fixture configuration is required')
    run(sys.argv[1])
