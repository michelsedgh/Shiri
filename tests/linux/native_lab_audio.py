"""Shared audio/control probes for the current native Linux qualification tools.

This module is not an executable legacy AirPlay/Loopback routing check.
"""
import asyncio
from fractions import Fraction
import importlib.util
from pathlib import Path
import time

import numpy as np
from aiortc import AudioStreamTrack, RTCConfiguration, RTCPeerConnection, RTCRtpSender
from av import AudioFrame

from shiri.runtime.system import Runner, RuntimeFailure

SLOT, RATE = 7, 48000
_lab_spec = importlib.util.spec_from_file_location('native_lab_admission', Path(__file__).with_name('native_lab.py'))
_lab_module = importlib.util.module_from_spec(_lab_spec)
_lab_spec.loader.exec_module(_lab_module)
NATIVE_LAB = _lab_module.from_environment()


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
