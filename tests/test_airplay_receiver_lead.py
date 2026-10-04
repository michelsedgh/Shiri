"""Actual pinned AP2 consumer arithmetic distinguishes saved lead from applied time."""
import importlib.util
from pathlib import Path
import shutil
from uuid import uuid4

import pytest

from shiri.domain import Room, SpeakerRef
from shiri.runtime.configuration import backend_configs
from shiri.runtime.latency import room_buffer_ms
from shiri.runtime.system import RuntimeFailure

spec = importlib.util.spec_from_file_location("actual_receiver_lead", Path(__file__).parent / "native/check_airplay_receiver_lead.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


def room(protocol, offset):
    return Room(id=str(uuid4()), slot=3, name="Exact room", airplay_name="Exact room", interface="eth0",
                local_audio_device="hw:CARD=KitchenDAC,DEV=0" if protocol in {"alsa", "pulseaudio"} else None,
                speakers=[SpeakerRef(id="0" if protocol in {"alsa", "pulseaudio"} else "1",
                                     name="Exact endpoint", protocol=protocol, offset_ms=offset)])


@pytest.mark.parametrize("protocol", ["airplay1", "airplay2"])
def test_actual_sender_and_ap2_consumer_all_offsets_reject_preimage_and_preserve_shared_calendar(protocol):
    compiler = shutil.which("clang") or shutil.which("cc")
    if not compiler:
        pytest.skip("Actual source seam requires a C compiler")
    # AirPlay1 shares the conservative candidate lead; this exact consumer
    # is AP2, not a claim that RAOP1 suffers this particular receiver branch.
    corrected = check.run_tests([room_buffer_ms(room(protocol, offset)) for offset in range(-2000, 2001)], compiler=compiler)
    assert corrected["ok"] and corrected["sanitized"] and corrected["offset_cases"] == 4001
    assert corrected["minimum_window_samples"] == 4410
    assert corrected["nonpositive_windows"] == corrected["below_known_100ms_windows"] == corrected["presentation_mismatches"] == 0
    preimage = check.run_tests([max(500, 250 - min(0, offset)) for offset in range(-2000, 2001)], compiler=compiler)
    assert not preimage["ok"] and preimage["nonpositive_windows"] == 1901
    assert preimage["below_known_100ms_windows"] == 2000 and preimage["minimum_window_samples"] == -6615
    assert preimage["presentation_mismatches"] == 0


@pytest.mark.parametrize("protocol,offset,refused,accepted", [
    ("airplay1", -100, 500, 600), ("airplay2", -250, 500, 750),
    ("airplay2", -2000, 2250, 2500), ("alsa", -2000, 2039, 2040),
    ("pulseaudio", -2000, 2249, 2250), ("chromecast", -2000, 2249, 2250),
])
def test_explicit_buffer_renderer_retains_selected_protocol_lead_before_writing(tmp_path, protocol, offset, refused, accepted):
    definition = room(protocol, offset)
    args = dict(all_receiver_names=[], password="private-test-only", audio_uid=1234)
    with pytest.raises(RuntimeFailure, match="required timing lead"):
        backend_configs(definition, tmp_path / "refused", {"interface": "receiver0"},
                        output_buffer_ms=refused, **args)
    assert not (tmp_path / "refused").exists()
    _, own = backend_configs(definition, tmp_path / "accepted", {"interface": "receiver0"},
                             output_buffer_ms=accepted, **args)
    assert f"start_buffer_ms = {accepted}" in own.read_text()
    assert room_buffer_ms(definition) == (2040 if protocol == "alsa" else accepted)


@pytest.mark.parametrize("buffers", [None, [], [500] * 4000, [True] * 4001, [499] * 4001, [4251] * 4001])
def test_actual_c_check_has_bounded_typed_candidate_inputs(buffers):
    with pytest.raises(ValueError):
        check.run_tests(buffers)
