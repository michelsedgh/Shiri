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
