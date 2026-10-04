"""Saved compensation must be scheduled, with one common native final horizon."""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4

import pytest

from shiri.domain import Room, SpeakerRef
from shiri.runtime.configuration import backend_configs

spec = importlib.util.spec_from_file_location('common_buffer_native_check', Path(__file__).parent/'native/check_common_buffer.py')
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


def render(tmp_path, *, device, full_margin=False):
    room = Room(id=str(uuid4()), slot=3, name='Kitchen', airplay_name='Kitchen', interface='eth0', local_audio_device=device,
                speakers=[SpeakerRef(id='123', name='Speaker', protocol='airplay2', offset_ms=-2000)] if full_margin else [])
    _sha, own = backend_configs(room, tmp_path/'room', {'interface': 'room-lan'},
                               all_receiver_names=[], password='test-private-password',
                               audio_uid=960)
    return own.read_text()


@pytest.mark.parametrize('device', [None, 'hw:CARD=KitchenDAC,DEV=0', 'plughw:CARD=KitchenDAC,DEV=0'])
def test_every_generated_profile_uses_only_its_selected_compensation_margin(tmp_path, device):
    assert check.configured_buffer(render(tmp_path, device=device)) == 40
    assert check.configured_buffer(render(tmp_path, device=device, full_margin=True)) == 2500


def test_actual_input_and_output_schedule_every_accepted_offset_without_changing_common_horizon(tmp_path):
    compiler = shutil.which('clang') or shutil.which('cc')
    if not compiler:
        pytest.skip('Actual source scheduling regression requires a C compiler')
    rendered = render(tmp_path, device='hw:CARD=KitchenDAC,DEV=0', full_margin=True)
    result = check.run_tests(check.configured_buffer(rendered), compiler=compiler)
    assert result['ok'] and result['sanitized'] and result['input_buffer_subtraction_verified']
    assert result['offset_cases'] == 4001 and result['ignored_offsets'] == result['horizon_mismatches'] == 0
    # The exact prior generated profile admits the saved values but its actual
    # ALSA guard ignores -501..-2000. Keep the failing scheduling proof visible.
    preimage = check.run_tests(check.configured_buffer(rendered.replace('start_buffer_ms = 2500', 'start_buffer_ms = 500')),
                               compiler=compiler)
    assert not preimage['ok'] and preimage['ignored_offsets'] == preimage['horizon_mismatches'] == 1500


def test_compiler_failure_retains_the_actual_stderr(tmp_path):
    compiler = tmp_path/'failing-compiler'
    compiler.write_text(f'#!{sys.executable}\nimport sys\nsys.stderr.write("exact compiler diagnostic\\n")\nsys.exit(2)\n')
    compiler.chmod(0o700)
    with pytest.raises(RuntimeError, match='exact compiler diagnostic') as failure:
        check.run_tests(2500, compiler=str(compiler))
    assert isinstance(failure.value.__cause__, subprocess.CalledProcessError)
    assert failure.value.__cause__.returncode == 2


@pytest.mark.parametrize('text', ['', 'start_buffer_ms = 0', 'start_buffer_ms = 60001',
                                 'start_buffer_ms = 2250\nstart_buffer_ms = 500'])
def test_native_source_check_refuses_ambiguous_or_unbounded_buffer_configuration(text):
    with pytest.raises(ValueError):
        check.configured_buffer(text)
