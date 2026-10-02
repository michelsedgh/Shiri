"""Actual pinned transport C callbacks tested without sockets or speakers."""
import importlib.util
from pathlib import Path


def test_transport_control_actual_c():
    path=Path(__file__).parent/'native/check_transport_control.py'
    spec=importlib.util.spec_from_file_location('native_transport_check',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    results=module.run_tests()
    assert len(results)==4
