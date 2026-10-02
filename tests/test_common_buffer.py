"""Saved compensation must be scheduled, with one common native final horizon."""
import importlib.util
from pathlib import Path
import shutil
from uuid import uuid4

import pytest

from shiri.domain import Room, SpeakerRef
from shiri.runtime.configuration import backend_configs

spec = importlib.util.spec_from_file_location('common_buffer_native_check', Path(__file__).parent/'native/check_common_buffer.py')
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


def render(tmp_path, *, native, device, full_margin=False):
    room = Room(id=str(uuid4()), slot=3, name='Kitchen', airplay_name='Kitchen', interface='eth0', local_audio_device=device,
                speakers=[SpeakerRef(id='123', name='Speaker', protocol='airplay2', offset_ms=-2000)] if full_margin else [])
    _sha, own = backend_configs(room, tmp_path/'room', {'interface': 'room-lan'},
                               {'api_host_ip': '10.211.0.1', 'api_ip': '10.211.0.2'},
                               broker_socket=tmp_path/'broker.sock', all_receiver_names=[], password='test-private-password',
                               native_timing=native, audio_uid=960)
    return own.read_text()


@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('device', [None, 'hw:CARD=KitchenDAC,DEV=0', 'plughw:CARD=KitchenDAC,DEV=0',
                                  'bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp'])
def test_every_generated_profile_uses_only_its_selected_compensation_margin(tmp_path, native, device):
    assert check.configured_buffer(render(tmp_path, native=native, device=device)) == (40 if native else 500)
    assert check.configured_buffer(render(tmp_path, native=native, device=device, full_margin=True)) == 2500


def test_actual_input_and_output_schedule_every_accepted_offset_without_changing_common_horizon(tmp_path):
    compiler = shutil.which('clang') or shutil.which('cc')
    if not compiler:
        pytest.skip('Actual source scheduling regression requires a C compiler')
    rendered = render(tmp_path, native=True, device='hw:CARD=KitchenDAC,DEV=0', full_margin=True)
    result = check.run_tests(check.configured_buffer(rendered), compiler=compiler)
    assert result['ok'] and result['sanitized'] and result['input_buffer_subtraction_verified']
    assert result['offset_cases'] == 4001 and result['ignored_offsets'] == result['horizon_mismatches'] == 0
    # The exact prior generated profile admits the saved values but its actual
    # ALSA guard ignores -501..-2000. Keep the failing scheduling proof visible.
    preimage = check.run_tests(check.configured_buffer(rendered.replace('start_buffer_ms = 2500', 'start_buffer_ms = 500')),
                               compiler=compiler)
    assert not preimage['ok'] and preimage['ignored_offsets'] == preimage['horizon_mismatches'] == 1500


@pytest.mark.parametrize('text', ['', 'start_buffer_ms = 0', 'start_buffer_ms = 60001',
                                 'start_buffer_ms = 2250\nstart_buffer_ms = 500'])
def test_native_source_check_refuses_ambiguous_or_unbounded_buffer_configuration(text):
    with pytest.raises(ValueError):
        check.configured_buffer(text)
