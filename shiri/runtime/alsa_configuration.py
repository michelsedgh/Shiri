"""Private ALSA configurations for a currently pinned kernel playback endpoint.

Stock hw/plughw aliases interpret CARD as a string and scan card control nodes
to resolve it. These configurations use integer card/device/subdevice fields
to avoid that scan. libasound still opens the selected card's control node for
subdevice selection; its permission boundary must be enforced separately.
The broker owns file creation, read-only service mounts and ALSA_CONFIG_PATH;
these functions neither write files nor replace the opened-PCM identity guard.
No current card index is saved as durable hardware identity.
"""
from __future__ import annotations

import re

from .alsa_identity import PCMIdentityError, validate_fingerprint


def _endpoint(pin):
    pin.validate()
    manifest = pin.manifest
    fields = ("card_index", "device", "subdevice")
    if (not isinstance(manifest, dict)
            or any(type(manifest.get(key)) is not int or not 0 <= manifest[key] <= 255 for key in fields)):
        raise PCMIdentityError("Private ALSA configuration needs a validated current kernel endpoint")
    fingerprint = validate_fingerprint(pin.fingerprint)
    if any(fingerprint[key] != manifest[key] for key in ("device", "subdevice")):
        raise PCMIdentityError("The pinned playback endpoint no longer matches its physical binding")
    return tuple(manifest[key] for key in fields), fingerprint


def _hardware(card, device, subdevice, indent):
    # Every interpolated value has already been bounded as an actual integer.
    return (f"{indent}type hw\n{indent}card {card}\n"
            f"{indent}device {device}\n{indent}subdevice {subdevice}\n")


def render_pcm_config(pin, conversion: bool) -> str:
    """Render the fixed 'shiri' playback alias from a still-owned live pin.

    The explicit conversion setting comes from the root-owned binding registry.
    The pin must be retained through the exact output unit's complete stop.
    """
    if type(conversion) is not bool:
        raise PCMIdentityError("PCM conversion must be an explicit boolean")
    (card, device, subdevice), _fingerprint = _endpoint(pin)
    if conversion:
        return ("pcm.shiri {\n  type plug\n  slave.pcm {\n"
                + _hardware(card, device, subdevice, "    ") + "  }\n}\n")
    return "pcm.shiri {\n" + _hardware(card, device, subdevice, "  ") + "}\n"


def render_bridge_config(pin, device: str) -> str:
    """Render one typed Loopback capture and one exact BlueALSA A2DP target.

    OwnTone writes the pinned snd_aloop DEV1/SUBDEV pair; the host bridge reads
    the opposite DEV0/SUBDEV pair on that same current card. The bridge keeps
    using the host system bus. Bluetooth pairing and plugin availability need
    separate runtime validation; this renderer does not claim either.
    """
    match = re.fullmatch(r"bluealsa:DEV=([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}),PROFILE=a2dp", device) if isinstance(device, str) else None
    if match is None or match[1].upper() in {"00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"}:
        raise PCMIdentityError("Bluetooth output needs one exact A2DP device address without aliases or wildcard targets")
    (card, playback_device, subdevice), fingerprint = _endpoint(pin)
    if (fingerprint["kind"] != "virtual" or fingerprint["binding"] != "loopback"
            or playback_device != 1 or not 0 <= subdevice <= 7):
        raise PCMIdentityError("Bluetooth bridge needs the exact pinned snd_aloop DEV1 playback pair")
    return ("pcm.shiri_capture {\n" + _hardware(card, 0, subdevice, "  ") + "}\n"
            "pcm.shiri_target {\n  type bluealsa\n"
            f'  device "{match[1].upper()}"\n  profile "a2dp"\n}}\n')
