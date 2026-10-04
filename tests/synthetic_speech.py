"""Synthetic waveforms for testing current WebRTC capture measurement instruments.

These fixtures model the default WebRTC 48 kHz, 40 ms attack / 250 ms release
arithmetic independently of the reference verifier. They exercise its ability
to detect corrupted, missing, or wrong-room samples. They are not the native
output engine and cannot qualify production mixing, timing, or audibility.
"""

from array import array
from collections import deque
import sys


class SyntheticSpeechWaveform:
    def __init__(self, gain=1.0):
        self.gain = gain
        self.voice = deque()

    def push(self, pcm):
        samples = array("h", pcm)
        if sys.byteorder != "little":
            samples.byteswap()
        self.voice.extend(samples)

    def render(self, pcm, frames, *, target_gain):
        assert len(pcm) == frames * 4
        samples = array("h", pcm)
        if sys.byteorder != "little":
            samples.byteswap()
        for index in range(frames):
            step = 1 / (48000 * (0.04 if target_gain < self.gain else 0.25))
            self.gain = (min(target_gain, self.gain + step) if target_gain > self.gain
                         else max(target_gain, self.gain - step))
            voice = self.voice.popleft() if self.voice else 0
            for channel in (index * 2, index * 2 + 1):
                samples[channel] = max(-32768, min(32767, round(samples[channel] * self.gain) + voice))
        if sys.byteorder != "little":
            samples.byteswap()
        return samples.tobytes()
