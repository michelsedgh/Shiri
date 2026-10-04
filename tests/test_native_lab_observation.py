"""Offline regressions for the native laboratory PCM measurement instruments."""
from collections import deque
import importlib.util
from pathlib import Path

import pytest

np = pytest.importorskip("numpy", reason="AirPlay observation checks require the audio extra")
pytest.importorskip("aiortc", reason="Manual harness imports the optional audio extra")
pytest.importorskip("av", reason="Manual harness imports the optional audio extra")

HARNESS = Path(__file__).parent / "linux" / "native_lab_observation.py"
spec = importlib.util.spec_from_file_location("airplay_observation_harness", HARNESS)
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


def pcm(rate=48000, *, music=8192, speech=0, blocks=30):
    size = rate // 50
    result = []
    for index in range(blocks):
        at = (index * size + np.arange(size)) / rate
        mono = (music * np.sin(2 * np.pi * 440 * at)
                + speech * np.sin(2 * np.pi * 880 * at)).astype("<i2")
        result.append(np.repeat(mono[:, None], 2, axis=1).astype("<i2").tobytes())
    return result


def metadata(rate=48000, first=0, frames=960, *, discont=False):
    return {"offset": first, "offset_end": first + frames,
            "pts": first * 1_000_000_000 // rate,
            "duration": frames * 1_000_000_000 // rate, "discont": discont}


def observer():
    capture = harness.OutputCapture.__new__(harness.OutputCapture)
    capture.device = "synthetic-observer"
    capture.error = capture.warning = None
    capture.capture_dropped = 0
    capture.needs_latency = False
    capture.pending = deque()
    capture.rate = capture.channels = capture.format = None
    capture.total = 0
    capture.chunks, capture.captured_at = [], []
    capture.discontinuities = 0
    capture.max_packet_gap = 0
    capture.last_packet_at = None
    capture.sequence = harness.PcmSequence()
    return capture


@pytest.mark.parametrize("rate", [44100, 48000])
def test_fit_detects_cubic_volume_and_independent_music_speech_gain(rate):
    baseline = harness.spectrum(pcm(rate), rate)
    mixed = harness.spectrum(pcm(rate, music=8192 * .2, speech=423), rate)
    half = harness.spectrum(pcm(rate, music=8192 * .125), rate)
    harness.require_music(baseline, 1)
    harness.require_music(half, 1)
    assert mixed["music_440_amplitude"] / baseline["music_440_amplitude"] == pytest.approx(.2, abs=.001)
    assert half["music_440_amplitude"] / baseline["music_440_amplitude"] == pytest.approx(.125, abs=.001)
    assert mixed["speech_880_amplitude"] == pytest.approx(423, abs=2)
    assert baseline["speech_880_amplitude"] < 2


def test_each_block_detects_600ms_silence_that_a_median_accepts():
    damaged = pcm(music=1000, blocks=75)
    damaged[20:50] = pcm(music=0, blocks=30)
    harness.require_music(harness.spectrum(damaged, 48000), 1)
    with pytest.raises(harness.base.RuntimeFailure, match="lost the continuous440"):
        for block in damaged:
            harness.music_block(block, 48000, 8)


@pytest.mark.parametrize("bad", [b"12", np.full((48000, 2), 32767, dtype="<i2").tobytes()],
                         ids=["incomplete", "clipped"])
def test_invalid_or_clipped_pcm_cannot_be_observed(bad):
    with pytest.raises(harness.base.RuntimeFailure):
        harness.spectrum([bad], 48000)


@pytest.mark.parametrize("first,discont", [(1921, False), (960, False), (1920, True)])
def test_sequence_rejects_missing_repeated_frames_and_late_discontinuity(first, discont):
    sequence = harness.PcmSequence()
    sequence.push(metadata(discont=True), 960, 48000)
    sequence.push(metadata(first=960), 960, 48000)
    with pytest.raises(harness.base.RuntimeFailure):
        sequence.push(metadata(first=first, discont=discont), 960, 48000)
    assert sequence.evidence()["verified_frames"] == 1920
    assert sequence.evidence()["initial_discontinuity"] is True


def test_timestamp_fallback_verifies_adjacent_pcm_instead_of_packet_cadence():
    def timed(pts):
        return {"offset": None, "offset_end": None, "pts": pts,
                "duration": 20_000_000, "discont": False}
    sequence = harness.PcmSequence()
    sequence.push(timed(0), 960, 48000)
    sequence.push(timed(20_000_000), 960, 48000)
    assert sequence.evidence()["mode"] == "pts_duration"
    with pytest.raises(harness.base.RuntimeFailure, match="lost or repeated"):
        sequence.push(timed(41_000_000), 960, 48000)


@pytest.mark.parametrize("fault", ["rate", "format", "frames", "empty", "offset", "discont"])
def test_observation_failure_stays_fatal_after_healthy_data_arrives(fault):
    capture = observer()
    clean = pcm(blocks=1)[0]
    capture.pending.append((1., clean, 48000, 2, "S16LE", metadata()))
    capture.poll()
    rate, audio_format, data, fields = 48000, "S16LE", clean, metadata(first=960)
    if fault == "rate":
        rate = 44100
    elif fault == "format":
        audio_format = "S32LE"
    elif fault == "frames":
        data += b"x"
    elif fault == "empty":
        data = b""
    elif fault == "offset":
        fields = metadata(first=1920)
    elif fault == "discont":
        fields["discont"] = True
    capture.pending.append((1.02, data, rate, 2, audio_format, fields))
    with pytest.raises(harness.base.RuntimeFailure):
        capture.poll()
    assert capture.error and len(capture.pending) == 1
    capture.pending.append((1.04, clean, 48000, 2, "S16LE", metadata(first=960)))
    with pytest.raises(harness.base.RuntimeFailure):
        capture.poll()
    assert len(capture.chunks) == 1


async def test_readiness_retry_cannot_consume_a_bad_buffer_and_then_succeed():
    capture = observer()
    clean = pcm(blocks=1)[0]
    capture.pending.append((1., clean, 48000, 2, "S16LE", metadata()))
    capture.poll()
    capture.pending.append((1.02, clean, 48000, 2, "S16LE", metadata(first=1920)))
    calls = 0
    async def ready():
        nonlocal calls
        calls += 1
        if calls > 1:
            capture.pending.append((1.04, clean, 48000, 2, "S16LE", metadata(first=960)))
        capture.poll()
        return True
    with pytest.raises(harness.base.RuntimeFailure, match="lost or repeated"):
        await harness.base.eventually(ready, "test PCM readiness", timeout=.3)
    assert calls >= 2 and len(capture.chunks) == 1 and capture.error


@pytest.mark.parametrize("rate", [44100, 48000])
def test_transition_waits_for_actual_buffered_audio_and_rejects_brief_wrong_or_stale_ducks(rate):
    baseline = harness.spectrum(pcm(rate), rate)["music_440_amplitude"]
    clean = pcm(rate, blocks=1)[0]
    duck = pcm(rate, music=8192 * .2, speech=423, blocks=1)[0]
    gate = harness.SpectrumTransition("duck_voice", 10., baseline, 1)
    assert not gate.push(duck, 9.99, rate) and gate.examined == 0
    for index in range(120):
        assert not gate.push(clean, 10. + (index + 1) * .02, rate)
    for index in range(30):
        gate.push(duck, 12.4 + (index + 1) * .02, rate)
    assert gate.complete
    assert gate.evidence(13.)["first_matching_callback_seconds_after_trigger"] >= 2.4
    wrong = pcm(rate, music=8192 * .7, speech=423, blocks=1)[0]
    assert not gate.push(wrong, 13.02, rate)
    assert not gate.complete and gate.frames == 0
    for index in range(3):
        assert not gate.push(duck, 13.04 + index * .02, rate)
    assert not gate.complete


@pytest.mark.parametrize("rate", [44100, 48000])
def test_restoration_waits_for_actual_music_and_rejects_lingering_voice(rate):
    baseline = harness.spectrum(pcm(rate), rate)["music_440_amplitude"]
    clean = pcm(rate, blocks=1)[0]
    duck = pcm(rate, music=8192 * .2, speech=423, blocks=1)[0]
    gate = harness.SpectrumTransition("restore_no_voice", 20., baseline, 1, previous_voice=423)
    for index in range(120):
        assert not gate.push(duck, 20. + (index + 1) * .02, rate)
    assert not gate.push(pcm(rate, speech=100, blocks=1)[0], 22.42, rate)
    for index in range(30):
        gate.push(clean, 22.44 + index * .02, rate)
    assert gate.complete
    assert gate.evidence(23.1)["first_matching_callback_seconds_after_trigger"] >= 2.4
