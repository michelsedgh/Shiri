import re
from uuid import uuid4

import pytest

from shiri.domain import Room
from shiri.runtime.configuration import backend_configs


@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('volume', [0, 5, 20, 50, 70, 99, 100])
def test_receiver_initial_volume_matches_saved_room_master_before_phone_command(tmp_path, native, volume):
    room = Room(id=str(uuid4()), slot=0, name='Saved master', airplay_name='Saved master zone',
                interface='eth0', volume=volume)
    receiver = {'interface': 'eth0'}
    sender = {'api_host_ip': '10.2.0.1', 'api_ip': '10.2.0.2'}
    shairport, _ = backend_configs(room, tmp_path / 'room', receiver, sender,
        broker_socket=tmp_path / 'broker.sock', all_receiver_names=[room.airplay_name],
        password='private-test-only', native_timing=native, audio_uid=61000 if native else None)
    value = float(re.search(r'default_airplay_volume = ([-.0-9]+);', shairport.read_text()).group(1))
    assert -30 <= value <= 0
    assert round((value + 30) * 100 / 30) == volume
