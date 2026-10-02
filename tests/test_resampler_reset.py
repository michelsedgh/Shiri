"""The sealed source command resets resamplers; replay/stale callbacks do not."""
import importlib.util
from pathlib import Path


def test_actual_source_transition_converter_reset_with_preimage_and_sanitizers():
    path = Path(__file__).parent / 'native/check_resampler_reset.py'
    spec = importlib.util.spec_from_file_location('resampler_reset_check', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.run_tests()
    assert result['ok'] and result['sanitized']
    assert len(result['results']) == 3
