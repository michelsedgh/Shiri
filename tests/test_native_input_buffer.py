"""Exercise real OwnTone buffering instead of mocking input_write capacity."""
import importlib.util
from pathlib import Path


def test_real_native_pipeline_buffers_full_admitted_lead_but_preserves_raw_limits():
    source = Path(__file__).parent/'native/check_native_input_buffer.py'
    spec = importlib.util.spec_from_file_location('native_buffer_check', source)
    check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check)
    result = check.run_tests()
    assert result['ok'] and result['sanitized']
    assert 'reproduced' in result['results'][0]
    assert '6s admission threshold+one32768-byte packet' in result['results'][1]
    assert 'raw/file2s limits preserved' in result['results'][1]
