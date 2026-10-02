"""Bluetooth final-PCM profiles never reopen arbitrary local hardware routes."""
from pathlib import Path
import re
from uuid import uuid4

import pytest

from shiri.domain import Room, SpeakerRef
from shiri.runtime.configuration import backend_configs
from shiri.runtime.system import RuntimeFailure


def profile(room):
    return {"socket": Path('/run/shiri-worker/bridge/final-pcm.sock'), "peer_uid": 963,
            "room_id": room.id, "launch_generation": "0123456789abcdef0123456789abcdef",
            "rate": 48000, "channels": 2, "format_code": 0x8210}


def render(tmp_path, room, framed, **extra):
    options = dict(native_timing=True, audio_uid=962, own_username='shiri-output-3',
                   view_directory=Path('/run/shiri-worker'), framed_output=framed)
    options.update(extra)
    return backend_configs(room, tmp_path/'room', {'interface': 'receiver0'},
                           {'api_host_ip': '10.211.0.1', 'api_ip': '10.211.0.2'},
                           broker_socket=tmp_path/'broker.sock', all_receiver_names=[],
                           password='private-never-trusted-on-LAN', **options)


@pytest.fixture
def room():
    return Room(id=str(uuid4()), slot=3, name='Kitchen "speaker"', airplay_name='Kitchen', interface='eth0',
                local_audio_device='bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp')


@pytest.mark.parametrize('format_code', [0x8210, 0x8318, 0x8418, 0x8420])
def test_only_admitted_capabilities_and_peer_are_exposed_to_framed_output(tmp_path, room, format_code):
    framed = {**profile(room), 'format_code': format_code}
    _receiver, own = render(tmp_path, room, framed)
    text = own.read_text()
    audio = re.search(r'audio \{ (.*?) \}', text, re.S).group(1)
    assert 'type = "shiri-pcm"' in audio
    assert 'software_volume = true' in audio
    assert 'shiri_pcm_socket = "/run/shiri-worker/bridge/final-pcm.sock"' in audio
    assert f'shiri_pcm_room_id = "{room.id}"' in audio
    assert 'shiri_pcm_peer_uid = 963' in audio
    assert 'shiri_pcm_rate = 48000' in audio and 'shiri_pcm_channels = 2' in audio
    assert f'shiri_pcm_format = {format_code}' in audio
    assert 'shiri_pcm_launch_generation = "0123456789abcdef0123456789abcdef"' in audio
    assert not re.search(r'\b(card|mixer|mixer_device|pcm_identity_file)\s*=', audio)
    assert 'bluealsa:' not in text and 'hw:Loopback' not in text
    assert 'trusted_networks = {  }' in text and 'logfile = "/dev/null"' in text
    # No selected negative compensation needs an enlarged output buffer.
    assert 'start_buffer_ms = 40' in text


@pytest.mark.parametrize('key,value', [('socket', Path('/tmp/arbitrary.sock')), ('socket', '/run/shiri-worker/bridge/final-pcm.sock'),
                                     ('peer_uid', 0), ('peer_uid', True), ('peer_uid', 2**32), ('rate', True),
                                     ('rate', 7999), ('rate', 192001), ('channels', 3), ('format_code', 8),
                                     ('room_id', str(uuid4())), ('room_id', 'invalid'),
                                     ('launch_generation', '0'*32), ('launch_generation', 'A'*32)])
def test_invalid_handoff_cannot_write_partial_backend_configuration(tmp_path, room, key, value):
    with pytest.raises(RuntimeFailure):
        render(tmp_path, room, {**profile(room), key: value})
    assert not (tmp_path/'room').exists()


@pytest.mark.parametrize('wrong', ['extra', 'missing', 'root', 'untimed', 'hardware', 'pin'])
def test_framed_output_cannot_acquire_unadmitted_hardware_or_root_profile(tmp_path, room, wrong):
    framed, extra = profile(room), {}
    if wrong == 'extra':
        framed['pcm_identity_file'] = '/untrusted'
    elif wrong == 'missing':
        del framed['peer_uid']
    elif wrong == 'root':
        extra['own_username'] = 'root'
    elif wrong == 'untimed':
        extra['native_timing'] = False
    elif wrong == 'hardware':
        room = room.model_copy(update={'local_audio_device': 'hw:CARD=KitchenDAC,DEV=0'})
    else:
        extra['pcm_identity_file'] = Path('/run/shiri-worker/credentials/pcm-identity.json')
    with pytest.raises(RuntimeFailure):
        render(tmp_path, room, framed, **extra)
    assert not (tmp_path/'room').exists()


def test_unselected_profile_retains_local_route_and_uses_native_local_buffer(tmp_path, room):
    _receiver, own = render(tmp_path, room, None)
    assert 'type = "alsa"' in own.read_text()
    assert 'start_buffer_ms = 40' in own.read_text()
    assert 'shiri_pcm_' not in own.read_text()


@pytest.mark.parametrize('framed', [False, True])
def test_selected_negative_compensation_has_real_margin_in_both_output_profiles(tmp_path, room, framed):
    room = room.model_copy(update={'speakers': [
        SpeakerRef(id='0', name='Exact Bluetooth output', protocol='alsa', offset_ms=-2000)]})
    _receiver, own = render(tmp_path, room, profile(room) if framed else None)
    text = own.read_text()
    assert 'start_buffer_ms = 2040' in text
    assert ('type = "shiri-pcm"' in text) is framed
