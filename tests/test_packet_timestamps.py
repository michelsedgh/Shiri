"""Actual packet arithmetic with libavcodec/libavutil, preserving missing PTS."""
import importlib.util
from pathlib import Path
import shutil
import subprocess

import pytest


def test_actual_packet_timestamp_missing_and_boundary_cases_with_real_ffmpeg():
    if not shutil.which('pkg-config') or subprocess.run(
        ['pkg-config', '--exists', 'libavcodec', 'libavformat', 'libavfilter', 'libavutil'], check=False,
    ).returncode:
        pytest.skip('Real FFmpeg development packages required for actual timestamp arithmetic')
    path = Path(__file__).parent / 'native/check_packet_timestamps.py'
    spec = importlib.util.spec_from_file_location('packet_timestamp_check', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.run_tests()
    assert result['ok'] and result['sanitized'] and result['real_ffmpeg']
