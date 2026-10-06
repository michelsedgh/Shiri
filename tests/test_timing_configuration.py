from pathlib import Path
from uuid import uuid4

import pytest

from shiri.domain import Room, SpeakerRef
from shiri.runtime.configuration import backend_configs
from shiri.runtime.system import RuntimeFailure


def render(tmp_path, **kwargs):
    room = Room(id=str(uuid4()),slot=3,name='Kitchen',airplay_name='Kitchen input',interface='eth0')
    kwargs.setdefault('audio_uid', 1234)
    return backend_configs(room,tmp_path/'host-room', {'interface':'room-lan'},
                           all_receiver_names=['Kitchen input'],
                           password='private-password-not-trusted-on-LAN',**kwargs)


def test_native_config_private_mount_view_and_exact_uid_without_unfenced_shell_hooks(tmp_path):
    sha, own = render(tmp_path,view_directory=Path('/run/shiri-worker'),
                      output_state_directory=Path('/run/shiri-worker/state'),own_username='shiri-output-3',
                      audio_uid=61203)
    sha_text, own_text = sha.read_text(), own.read_text()
    assert 'output_backend = "shiri"' in sha_text
    assert 'socket = "/run/shiri-worker/input/music.sock"' in sha_text
    assert 'peer_uid = 61203' in sha_text
    assert 'output_rate = 48000' in sha_text and 'output_format = "S16_LE"' in sha_text
    assert 'sessioncontrol' not in sha_text and 'run_this_' not in sha_text
    assert 'hw:Loopback' not in sha_text
    assert 'pipe_framed = true' in own_text
    assert 'uid = "shiri-output-3"' in own_text
    assert 'db_path = "/run/shiri-worker/state/songs.db"' in own_text
    assert 'directories = { "/run/shiri-worker/pipes" }' in own_text
    assert 'trusted_networks = {  }' in own_text
    # OwnTone's foreground console goes to stderr; reopening the journald
    # stdout socket with fopen fails ENXIO. The bounded reader retains stderr.
    assert 'logfile = "/dev/null"' in own_text and '/dev/stdout' not in own_text
    assert str(tmp_path) not in own_text
    assert sha.parent == tmp_path/'host-room/config'
    assert own.parent == tmp_path/'host-room/config'


@pytest.mark.parametrize('uid', [None, True, -1, 0, 2**32])
def test_native_profile_requires_exact_unprivileged_peer_uid(tmp_path, uid):
    with pytest.raises(RuntimeFailure, match='exact private audio worker UID'):
        render(tmp_path, audio_uid=uid)
    assert not (tmp_path/'host-room').exists()




@pytest.mark.parametrize('device',['hw:CARD=KitchenDAC,DEV=0,SUBDEV=0','plughw:CARD=KitchenDAC,DEV=0,SUBDEV=0'])
def test_admitted_local_pcm_pin_is_explicit_and_preserves_exact_converter_uri(tmp_path,device):
    room=Room(id=str(uuid4()),slot=3,name='Kitchen',airplay_name='Kitchen input',interface='eth0',local_audio_device=device)
    _sha,own=backend_configs(room,tmp_path/'room',{'interface':'room-lan'},
                            all_receiver_names=[],password='private-password-value',
                            audio_uid=1234,pcm_identity_file=Path('/run/shiri-worker/credentials/pcm-identity.json'))
    text=own.read_text()
    assert f'card = "{device}"' in text
    assert 'pcm_identity_file = "/run/shiri-worker/credentials/pcm-identity.json"' in text
    assert 'software_volume = true' in text


def test_pcm_pin_never_implied_by_stock_configuration_or_missing_output(tmp_path):
    _sha,own=render(tmp_path)
    assert 'pcm_identity_file' not in own.read_text()
    with pytest.raises(RuntimeFailure,match='explicit local output'):
        render(tmp_path,pcm_identity_file=Path('/run/shiri-worker/credentials/pcm-identity.json'))


def test_late_speech_profile_is_bound_to_the_native_room_launch_and_has_no_music_commands(tmp_path):
    room = Room(id=str(uuid4()), slot=3, name='Kitchen', airplay_name='Kitchen', interface='eth0')
    launch = uuid4().hex
    _sha, own = backend_configs(
        room, tmp_path/'room', {'interface': 'room-lan'},
        all_receiver_names=[], password='test-private-password',
        audio_uid=1234, own_username='shiri-output-3',
        view_directory=Path('/run/shiri-worker'), output_buffer_ms=500,
        speech_output={'socket': Path('/run/shiri-worker/overlay/speech.sock'), 'peer_uid': 1234,
                       'room_id': room.id, 'launch_generation': launch},
    )
    text = own.read_text()
    assert 'start_buffer_ms = 500' in text
    assert 'shiri_speech_socket = "/run/shiri-worker/overlay/speech.sock"' in text
    assert f'shiri_speech_room_id = "{room.id}"' in text
    assert f'shiri_speech_launch_generation = "{launch}"' in text
    assert 'shiri_speech_peer_uid = 1234' in text
    assert 'pipe_framed = true' in text and 'pipe_autostart = true' in text


def test_small_output_buffer_cannot_silently_ignore_a_selected_negative_correction(tmp_path):
    room = Room(id=str(uuid4()), slot=3, name='Kitchen', airplay_name='Kitchen', interface='eth0',
                speakers=[SpeakerRef(id='123', name='Speaker', protocol='airplay2', offset_ms=-2000)])
    with pytest.raises(RuntimeFailure, match="required timing lead"):
        backend_configs(room, tmp_path/'room', {'interface': 'room-lan'},
                        all_receiver_names=[],
                        password='test-private-password', output_buffer_ms=500, audio_uid=1234)


def test_buffered_phone_alignment_uses_common_horizon_across_mixed_room_buffers(tmp_path):
    # A grouped local receiver and AirPlay receiver must advance equally;
    # choosing the advance from each room's B would split their final clock.
    for buffer in (40, 250, 500):
        sha, own = render(tmp_path/str(buffer), output_buffer_ms=buffer, relay_delay_ms=600)
        assert 'buffered_audio_advance_ms = 600;' in sha.read_text()
        assert 'audio_backend_latency_offset_in_seconds = 0.0;' in sha.read_text()
        assert 'audio_backend_buffer_desired_length_in_seconds = 0.15;' in sha.read_text()
        assert f'start_buffer_ms = {buffer}\n' in own.read_text()


@pytest.mark.parametrize('horizon,advance', [(None, 0), (140, 140), (350, 350), (599, 599),
                                           (600, 600), (601, 600), (2140, 600), (2600, 600)])
def test_buffered_advance_is_bounded_without_a_discontinuity_at_the_qualified_maximum(tmp_path, horizon, advance):
    sha, _own = render(tmp_path, relay_delay_ms=horizon)
    assert f'buffered_audio_advance_ms = {advance};' in sha.read_text()
    assert 'audio_backend_latency_offset_in_seconds = 0.0;' in sha.read_text()


@pytest.mark.parametrize('horizon', [True, 600.0, '600', 139, 2601])
def test_invalid_common_horizon_fails_before_backend_files_are_created(tmp_path, horizon):
    with pytest.raises(RuntimeFailure, match='bounded common relay horizon'):
        render(tmp_path, relay_delay_ms=horizon)
    assert not (tmp_path/'host-room').exists()


def test_shared_horizon_cannot_shortchange_the_frozen_output_buffer(tmp_path):
    with pytest.raises(RuntimeFailure, match='cannot precede'):
        render(tmp_path, output_buffer_ms=700, relay_delay_ms=600)
    assert not (tmp_path/'host-room').exists()
