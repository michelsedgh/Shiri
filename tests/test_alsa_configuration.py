"""Typed private ALSA configuration; parsing never opens a PCM or device.

Linux also exercises libasound's actual parser/type API. These checks do not
substitute for a control-node-denied service opening the guarded kernel PCM.
"""
from copy import deepcopy
import ctypes
import ctypes.util
from types import SimpleNamespace

import pytest

from shiri.runtime.alsa_configuration import render_bridge_config, render_pcm_config
from shiri.runtime.alsa_identity import PCMIdentityError


class Pin:
    def __init__(self, *, card=5, device=1, subdevice=7):
        self.fingerprint = {"version": 1, "kind": "virtual", "binding": "loopback", "instance": 0,
                            "device": device, "subdevice": subdevice}
        self.manifest = {"card_index": card, "device": device, "subdevice": subdevice}
        # The renderer must not use this mutable descriptive selector as DSL.
        self.pcm = 'plughw:CARD=unsafe"\n@hooks [{ func load files ["/tmp/untrusted"] }]'
        self.validations = 0
        self.closed = False

    def validate(self):
        self.validations += 1
        if self.closed:
            raise PCMIdentityError("The selected kernel instance disappeared")
        return self


def test_exact_numeric_playback_and_conversion_are_not_named_or_global_aliases():
    pin = Pin()
    plain = render_pcm_config(pin, False)
    converted = render_pcm_config(pin, True)
    assert plain == "pcm.shiri {\n  type hw\n  card 5\n  device 1\n  subdevice 7\n}\n"
    assert converted == ("pcm.shiri {\n  type plug\n  slave.pcm {\n    type hw\n"
                         "    card 5\n    device 1\n    subdevice 7\n  }\n}\n")
    assert pin.validations == 2
    assert "unsafe" not in plain + converted and "@" not in plain + converted


def test_fresh_kernel_card_is_used_without_modifying_saved_physical_binding():
    pin = Pin(card=0)
    saved = deepcopy(pin.fingerprint)
    assert "card 0\n" in render_pcm_config(pin, True)
    pin.manifest["card_index"] = 9
    assert "card 9\n" in render_pcm_config(pin, True)
    assert pin.fingerprint == saved


@pytest.mark.parametrize("conversion", [None, 0, 1, "true", [], {}])
def test_conversion_is_not_coerced_and_invalid_requests_never_validate_a_pin(conversion):
    pin = Pin()
    with pytest.raises(PCMIdentityError, match="explicit boolean"):
        render_pcm_config(pin, conversion)
    assert pin.validations == 0


@pytest.mark.parametrize("field,value", [("card_index", True), ("card_index", -1), ("card_index", 256),
    ("card_index", '0\n@hooks [{ func load }]'), ("device", "1"), ("subdevice", 7.0)])
def test_manifest_dsl_injection_and_nonnumeric_fields_are_rejected(field, value):
    pin = Pin()
    pin.manifest[field] = value
    with pytest.raises(PCMIdentityError, match="validated current kernel"):
        render_pcm_config(pin, False)


def test_live_pin_and_physical_endpoint_guards_precede_rendering():
    pin = Pin()
    pin.manifest["subdevice"] = 6
    with pytest.raises(PCMIdentityError, match="physical binding"):
        render_pcm_config(pin, True)
    pin.closed = True
    with pytest.raises(PCMIdentityError, match="disappeared"):
        render_pcm_config(pin, True)


def test_bridge_has_fixed_reverse_pair_and_exact_bluetooth_plugin_fields():
    pin = Pin(card=4, subdevice=6)
    assert render_bridge_config(pin, "bluealsa:DEV=aa:bb:cc:dd:ee:ff,PROFILE=a2dp") == (
        "pcm.shiri_capture {\n  type hw\n  card 4\n  device 0\n  subdevice 6\n}\n"
        "pcm.shiri_target {\n  type bluealsa\n  device \"AA:BB:CC:DD:EE:FF\"\n  profile \"a2dp\"\n}\n")
    assert pin.validations == 1


@pytest.mark.parametrize("device", [None, "bluealsa", "bluealsa:DEV=00:00:00:00:00:00,PROFILE=a2dp",
    "bluealsa:DEV=FF:FF:FF:FF:FF:FF,PROFILE=a2dp", "bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=sco",
    "bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp,SRV=untrusted", "/tmp/asound.conf",
    'bluealsa:DEV=AA:BB:CC:DD:EE:FF"\n@hooks,PROFILE=a2dp'])
def test_bridge_rejects_wildcards_arbitrary_aliases_profiles_and_dsl(device):
    pin = Pin()
    with pytest.raises(PCMIdentityError, match="exact A2DP"):
        render_bridge_config(pin, device)
    assert pin.validations == 0


@pytest.mark.parametrize("kind", ["wrong-device", "physical-usb"])
def test_bridge_requires_a_real_loopback_playback_pair(kind):
    pin = Pin(device=0 if kind == "wrong-device" else 1)
    if kind == "physical-usb":
        pin.fingerprint = {"version": 1, "kind": "usb", "binding": "serial", "vid": "1234", "pid": "5678",
                           "serial": "USB", "interface": "00", "configuration": 1, "device": 1, "subdevice": 7}
    with pytest.raises(PCMIdentityError, match="snd_aloop DEV1"):
        render_bridge_config(pin, "bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp")


@pytest.fixture
def asound():
    path = ctypes.util.find_library("asound")
    if path is None:
        pytest.skip("Actual ALSA parser is Linux-only; no libasound installed on this host")
    lib = ctypes.CDLL(path)
    pointer = ctypes.c_void_p
    signatures = {
        "snd_config_top": ([ctypes.POINTER(pointer)], ctypes.c_int),
        "snd_input_buffer_open": ([ctypes.POINTER(pointer), ctypes.c_char_p, ctypes.c_ssize_t], ctypes.c_int),
        "snd_config_load": ([pointer, pointer], ctypes.c_int),
        "snd_input_close": ([pointer], ctypes.c_int),
        "snd_config_delete": ([pointer], ctypes.c_int),
        "snd_config_search": ([pointer, ctypes.c_char_p, ctypes.POINTER(pointer)], ctypes.c_int),
        "snd_config_get_integer": ([pointer, ctypes.POINTER(ctypes.c_long)], ctypes.c_int),
        "snd_config_get_string": ([pointer, ctypes.POINTER(ctypes.c_char_p)], ctypes.c_int),
    }
    for name, (args, result) in signatures.items():
        getattr(lib, name).argtypes, getattr(lib, name).restype = args, result
    return SimpleNamespace(lib=lib, pointer=pointer)


@pytest.mark.parametrize("mode", ["plain", "plug", "bridge"])
def test_actual_alsa_parser_accepts_configuration_and_proves_integer_card_without_global_import(asound, mode):
    lib, pointer = asound.lib, asound.pointer
    pin = Pin()
    text = (render_bridge_config(pin, "bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp") if mode == "bridge"
            else render_pcm_config(pin, mode == "plug"))
    root, stream = pointer(), pointer()
    assert lib.snd_config_top(ctypes.byref(root)) == 0
    data = text.encode()
    try:
        assert lib.snd_input_buffer_open(ctypes.byref(stream), data, len(data)) == 0
        assert lib.snd_config_load(root, stream) == 0
        prefix = "pcm.shiri_capture" if mode == "bridge" else "pcm.shiri.slave.pcm" if mode == "plug" else "pcm.shiri"
        for key, expected in [("card", 5), ("device", 0 if mode == "bridge" else 1), ("subdevice", 7)]:
            node, number, string = pointer(), ctypes.c_long(), ctypes.c_char_p()
            assert lib.snd_config_search(root, f"{prefix}.{key}".encode(), ctypes.byref(node)) == 0
            assert lib.snd_config_get_integer(node, ctypes.byref(number)) == 0 and number.value == expected
            assert lib.snd_config_get_string(node, ctypes.byref(string)) < 0
        for forbidden in ["ctl", "pcm.default", "pcm.hw", "defaults", "@hooks"]:
            unused = pointer()
            assert lib.snd_config_search(root, forbidden.encode(), ctypes.byref(unused)) < 0
        if mode == "bridge":
            for key, expected in [("type", "bluealsa"), ("device", "AA:BB:CC:DD:EE:FF"), ("profile", "a2dp")]:
                node, string = pointer(), ctypes.c_char_p()
                assert lib.snd_config_search(root, f"pcm.shiri_target.{key}".encode(), ctypes.byref(node)) == 0
                assert lib.snd_config_get_string(node, ctypes.byref(string)) == 0 and string.value.decode() == expected
    finally:
        if stream:
            lib.snd_input_close(stream)
        lib.snd_config_delete(root)
