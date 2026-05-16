#!/usr/bin/env python3
"""
audio_mixer.py - Shiri live audio + TTS mixer.

OwnTone reads one raw PCM pipe per zone. This process keeps the existing
AirPlay capture flowing into that pipe, and overlays queued TTS WAV files
without stopping the live stream.
"""

from __future__ import annotations

import argparse
import errno
import json
import logging
import os
import select
import signal
import subprocess
import time
import wave
from array import array
from dataclasses import dataclass
from glob import glob
from pathlib import Path
from typing import Iterable


RATE = 44100
CHANNELS = 2
SAMPLE_WIDTH = 2
CHUNK_FRAMES = 512
CHUNK_BYTES = CHUNK_FRAMES * CHANNELS * SAMPLE_WIDTH
CHUNK_SECONDS = CHUNK_FRAMES / RATE
DEFAULT_DUCK_GAIN = 0.28
DEFAULT_TTS_GAIN = 1.0
ATTACK_SECONDS = 0.04
RELEASE_SECONDS = 0.25


log = logging.getLogger("shiri.audio_mixer")


@dataclass
class TTSClip:
    request_id: str
    pcm: bytes
    duck_gain: float
    tts_gain: float
    position: int = 0

    @property
    def done(self) -> bool:
        return self.position >= len(self.pcm)

    def next_chunk(self, size: int) -> bytes:
        chunk = self.pcm[self.position:self.position + size]
        self.position += len(chunk)
        if len(chunk) < size:
            chunk += b"\x00" * (size - len(chunk))
        return chunk


class AudioMixer:
    def __init__(self, *, capture_dev: str, grp_dir: Path) -> None:
        self.capture_dev = capture_dev
        self.grp_dir = grp_dir
        self.pipe_path = grp_dir / "pipes" / "audio.pipe"
        self.queue_dir = grp_dir / "tts_queue"
        self.arecord_pid_path = grp_dir / "state" / "arecord.pid"
        self._stop = False
        self._arecord: subprocess.Popen[bytes] | None = None
        self._pipe_fd: int | None = None
        self._active_clip: TTSClip | None = None
        self._duck_level = 1.0

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_stop)
        signal.signal(signal.SIGINT, self._handle_stop)
        self.queue_dir.mkdir(parents=True, exist_ok=True)

        log.info("Starting mixer capture_dev=%s grp_dir=%s", self.capture_dev, self.grp_dir)
        while not self._stop:
            try:
                self._ensure_pipe()
                self._ensure_arecord()
                self._mix_once()
            except BrokenPipeError:
                log.warning("OwnTone pipe closed; reopening")
                self._close_pipe()
                time.sleep(0.2)
            except Exception:
                log.exception("Mixer loop error")
                time.sleep(0.2)

        self._cleanup()
        log.info("Mixer stopped")

    def _handle_stop(self, _signum: int, _frame: object) -> None:
        self._stop = True

    def _ensure_pipe(self) -> None:
        if self._pipe_fd is not None:
            return
        log.info("Opening OwnTone pipe for mixed audio: %s", self.pipe_path)
        try:
            self._pipe_fd = os.open(self.pipe_path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            if exc.errno == errno.ENXIO:
                log.warning("OwnTone is not reading the audio pipe yet")
            raise

    def _close_pipe(self) -> None:
        if self._pipe_fd is None:
            return
        try:
            os.close(self._pipe_fd)
        except OSError:
            pass
        self._pipe_fd = None

    def _ensure_arecord(self) -> None:
        if self._arecord is not None and self._arecord.poll() is None:
            return
        if self._arecord is not None:
            log.warning("arecord exited with rc=%s; restarting", self._arecord.poll())
        self._clear_stale_loopback()
        cmd = [
            "arecord",
            "-D",
            self.capture_dev,
            "-f",
            "cd",
            "-c",
            "2",
            "-t",
            "raw",
            "--buffer-size=2048",
            "--period-size=512",
        ]
        self._arecord = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.arecord_pid_path.write_text(str(self._arecord.pid))
        log.info("Started arecord pid=%d", self._arecord.pid)

    def _clear_stale_loopback(self) -> None:
        try:
            subprocess.run(
                [
                    "timeout",
                    "0.1",
                    "arecord",
                    "-D",
                    self.capture_dev,
                    "-f",
                    "cd",
                    "-c",
                    "2",
                    "-t",
                    "raw",
                    "-d",
                    "1",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError as exc:
            log.warning("Could not clear stale loopback data: %s", exc)

    def _mix_once(self) -> None:
        started_at = time.monotonic()
        self._load_clip_if_needed()
        live = self._read_live_chunk(timeout=0.006 if self._active_clip else 0.08)

        if live is None and self._active_clip is None:
            return

        generated_silence = live is None
        if live is None:
            live = b"\x00" * CHUNK_BYTES
        elif len(live) < CHUNK_BYTES:
            live += b"\x00" * (CHUNK_BYTES - len(live))

        clip = self._active_clip
        clip_position = clip.position if clip is not None else None
        if clip is not None:
            tts = clip.next_chunk(CHUNK_BYTES)
            target_duck = clip.duck_gain
            tts_gain = clip.tts_gain
        else:
            tts = b"\x00" * CHUNK_BYTES
            target_duck = 1.0
            tts_gain = 0.0

        self._duck_level = self._next_duck_level(target_duck)
        mixed = mix_pcm16(live, tts, music_gain=self._duck_level, tts_gain=tts_gain)
        if not self._write_pipe(mixed):
            if clip is not None and clip_position is not None:
                clip.position = clip_position
            time.sleep(CHUNK_SECONDS)
            return
        if generated_silence:
            time.sleep(max(0.0, CHUNK_SECONDS - (time.monotonic() - started_at)))

        if clip is not None and clip.done:
            log.info("Finished TTS request %s", clip.request_id)
            self._active_clip = None

    def _read_live_chunk(self, *, timeout: float) -> bytes | None:
        proc = self._arecord
        if proc is None or proc.stdout is None:
            return None
        if proc.poll() is not None:
            return None
        ready, _, _ = select.select([proc.stdout], [], [], timeout)
        if not ready:
            return None
        data = os.read(proc.stdout.fileno(), CHUNK_BYTES)
        if not data:
            return None
        return data

    def _write_pipe(self, data: bytes) -> bool:
        try:
            os.write(self._pipe_fd, data)
            return True
        except BlockingIOError:
            return False

    def _load_clip_if_needed(self) -> None:
        if self._active_clip is not None:
            return
        self._active_clip = self._load_next_clip()
        if self._active_clip is not None:
            log.info(
                "Starting TTS request %s duration=%.2fs duck_gain=%.2f tts_gain=%.2f",
                self._active_clip.request_id,
                len(self._active_clip.pcm) / (RATE * CHANNELS * SAMPLE_WIDTH),
                self._active_clip.duck_gain,
                self._active_clip.tts_gain,
            )

    def _load_next_clip(self) -> TTSClip | None:
        for meta_path in sorted(glob(str(self.queue_dir / "*.json"))):
            path = Path(meta_path)
            try:
                meta = json.loads(path.read_text())
                audio_path = Path(meta["audio_path"])
                if not audio_path.exists():
                    log.warning("Dropping TTS queue item with missing audio: %s", audio_path)
                    safe_unlink(path)
                    continue
                pcm = load_wav_as_pipe_pcm(audio_path)
                clip = TTSClip(
                    request_id=str(meta.get("request_id") or path.stem),
                    pcm=pcm,
                    duck_gain=clamp_float(meta.get("duck_gain"), 0.0, 1.0, DEFAULT_DUCK_GAIN),
                    tts_gain=clamp_float(meta.get("tts_gain"), 0.05, 2.0, DEFAULT_TTS_GAIN),
                )
                safe_unlink(audio_path)
                safe_unlink(path)
                return clip
            except Exception:
                log.exception("Failed to load TTS queue item %s", path)
                safe_unlink(path)
        return None

    def _next_duck_level(self, target: float) -> float:
        if target < self._duck_level:
            seconds = ATTACK_SECONDS
        else:
            seconds = RELEASE_SECONDS
        step = CHUNK_FRAMES / RATE / max(seconds, 0.001)
        if target < self._duck_level:
            return max(target, self._duck_level - step)
        return min(target, self._duck_level + step)

    def _cleanup(self) -> None:
        if self._arecord is not None:
            terminate_process(self._arecord)
            self._arecord = None
        self._close_pipe()
        safe_unlink(self.arecord_pid_path)


def load_wav_as_pipe_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())

    samples = decode_pcm_samples(frames, sample_width)
    stereo = to_stereo(samples, channels)
    if rate != RATE:
        stereo = resample_stereo(stereo, rate, RATE)
    return samples_to_bytes(stereo)


def decode_pcm_samples(data: bytes, sample_width: int) -> list[int]:
    if sample_width == 1:
        return [(value - 128) << 8 for value in data]

    if sample_width == 2:
        samples = array("h")
        samples.frombytes(data)
        return samples.tolist()

    if sample_width == 3:
        out: list[int] = []
        for i in range(0, len(data) - 2, 3):
            raw = int.from_bytes(data[i:i + 3], "little", signed=False)
            if raw & 0x800000:
                raw -= 0x1000000
            out.append(raw >> 8)
        return out

    if sample_width == 4:
        out = []
        for i in range(0, len(data) - 3, 4):
            out.append(int.from_bytes(data[i:i + 4], "little", signed=True) >> 16)
        return out

    raise ValueError(f"Unsupported WAV sample width: {sample_width}")


def to_stereo(samples: list[int], channels: int) -> list[int]:
    if channels == 2:
        return samples
    if channels == 1:
        out: list[int] = []
        for sample in samples:
            out.extend([sample, sample])
        return out
    if channels <= 0:
        raise ValueError(f"Invalid channel count: {channels}")

    out = []
    for i in range(0, len(samples) - channels + 1, channels):
        frame = samples[i:i + channels]
        mono = int(sum(frame) / channels)
        out.extend([mono, mono])
    return out


def resample_stereo(samples: list[int], src_rate: int, dst_rate: int) -> list[int]:
    if src_rate <= 0:
        raise ValueError(f"Invalid source sample rate: {src_rate}")
    if src_rate == dst_rate:
        return samples
    src_frames = len(samples) // 2
    if src_frames == 0:
        return []
    dst_frames = max(1, int(round(src_frames * dst_rate / src_rate)))
    out: list[int] = []
    for dst_index in range(dst_frames):
        src_pos = dst_index * src_rate / dst_rate
        src_index = int(src_pos)
        frac = src_pos - src_index
        next_index = min(src_index + 1, src_frames - 1)
        left = lerp(samples[src_index * 2], samples[next_index * 2], frac)
        right = lerp(samples[src_index * 2 + 1], samples[next_index * 2 + 1], frac)
        out.extend([left, right])
    return out


def samples_to_bytes(samples: Iterable[int]) -> bytes:
    out = array("h")
    out.extend(clip_int(sample) for sample in samples)
    return out.tobytes()


def mix_pcm16(live: bytes, tts: bytes, *, music_gain: float, tts_gain: float) -> bytes:
    live_samples = array("h")
    live_samples.frombytes(live)
    tts_samples = array("h")
    tts_samples.frombytes(tts)

    count = min(len(live_samples), len(tts_samples))
    out = array("h")
    append = out.append
    for index in range(count):
        mixed = int(live_samples[index] * music_gain + tts_samples[index] * tts_gain)
        append(clip_int(mixed))
    return out.tobytes()


def lerp(a: int, b: int, frac: float) -> int:
    return int(a + (b - a) * frac)


def clip_int(value: int) -> int:
    if value > 32767:
        return 32767
    if value < -32768:
        return -32768
    return value


def clamp_float(value: object, minimum: float, maximum: float, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, minimum), maximum)


def safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("Could not remove %s: %s", path, exc)


def terminate_process(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture-dev", required=True)
    parser.add_argument("--grp-dir", required=True, type=Path)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    AudioMixer(capture_dev=args.capture_dev, grp_dir=args.grp_dir).run()


if __name__ == "__main__":
    main()
