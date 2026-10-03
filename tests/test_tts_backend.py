"""True AV resampling, finite PCM admission, stream ownership and cancellation."""
from types import SimpleNamespace

import numpy as np
import pytest

from shiri.tts.backend import (
    BackendUnavailable, GenerationError, MLXBackend, _english_sentences, _resolve_model_path, load_backend,
)
from shiri.tts.models import QWEN_MODEL_ID, get_model


class Decoder:
    def __init__(self):
        self.resets = 0

    def reset_streaming_state(self):
        self.resets += 1


class Model:
    def __init__(self, results):
        self.results = results
        self.arguments = []
        self.closes = 0
        self.speech_tokenizer = SimpleNamespace(decoder=Decoder())

    def generate(self, **arguments):
        self.arguments.append(arguments)
        try:
            yield from self.results
        finally:
            self.closes += 1


def result(audio, rate=24000, tokens=1, final=False):
    return SimpleNamespace(audio=np.array(audio, dtype=np.float32), sample_rate=rate,
                           token_count=tokens, is_final_chunk=final)


def backend(tmp_path, results, *, model_id=QWEN_MODEL_ID):
    model = Model(results)
    return MLXBackend(get_model(model_id), model, tmp_path)


@pytest.mark.parametrize("rate", [8000, 24000, 32000, 44100, 48000])
def test_real_resampler_preserves_complete_duration_and_drains_tail_once(tmp_path, rate):
    native = np.full(rate // 100, 0.125, dtype=np.float32)
    engine = backend(tmp_path, [result(native, rate), result(native, rate)])
    pcm = b"".join(engine.generate({"text": "Hello"}))
    # Exactly 20 ms must remain 20 ms across chunk boundaries and noninteger ratios.
    assert len(pcm) == 960 * 2
    samples = np.frombuffer(pcm, dtype="<i2")
    assert np.max(np.abs(samples[40:-40].astype(int) - 4096)) <= 1
    assert engine.last_metrics["complete"] and engine.last_metrics["eos"] is True
    assert engine.last_metrics["resampler_tail_samples"] > 0 if rate != 48000 else engine.last_metrics["resampler_tail_samples"] == 0
    assert engine.model.closes == 1 and engine.model.speech_tokenizer.decoder.resets == 1
    assert engine.last_metrics["generated_audio_seconds"] == 0.02


def test_natural_eos_on_exact_chunk_boundary_does_not_require_final_flag(tmp_path):
    engine = backend(tmp_path, [result(np.full(1920, 0.1), tokens=1, final=False)])
    assert sum(len(chunk) for chunk in engine.generate({"text": "Hello"})) == 7680
    assert engine.last_metrics["tokens"] == 1 and engine.last_metrics["eos"] is True


def test_sampling_state_is_reset_before_each_request_and_explicit_seed_is_recorded(tmp_path):
    events = []

    class SeededModel(Model):
        def generate(self, **arguments):
            events.append("generate")
            yield from super().generate(**arguments)

    model = SeededModel([result(np.full(1920, 0.1))])
    engine = MLXBackend(get_model(QWEN_MODEL_ID), model, tmp_path,
                        seed_random=lambda seed: events.append(("seed", seed)))
    first = b"".join(engine.generate({"text": "Hello"}))
    second = b"".join(engine.generate({"text": "Hello"}))
    assert first == second
    assert events == [("seed", 42), "generate", ("seed", 42), "generate"]
    assert engine.last_metrics["seed"] == 42
    list(engine.generate({"text": "Hello", "seed": 0xFFFFFFFF}))
    assert events[-2:] == [("seed", 0xFFFFFFFF), "generate"]
    assert engine.last_metrics["seed"] == 0xFFFFFFFF


def test_invalid_or_busy_requests_do_not_change_active_sampling_state(tmp_path):
    seeds = []
    engine = backend(tmp_path, [result(np.full(1920, 0.1)), result(np.full(1920, 0.1))])
    engine.seed_random = seeds.append
    with pytest.raises(ValueError, match="unsigned 32-bit"):
        list(engine.generate({"text": "Hello", "seed": -1}))
    assert seeds == []
    active = engine.generate({"text": "Hello", "seed": 7})
    next(active)
    with pytest.raises(GenerationError, match="one utterance"):
        list(engine.generate({"text": "Another", "seed": 9}))
    assert seeds == [7]
    active.close()


def test_cooperative_cancellation_cannot_acknowledge_a_failed_decoder_reset(tmp_path):
    engine = backend(tmp_path, [result(np.full(1920, 0.1)), result(np.full(1920, 0.1))])
    active = engine.generate({"text": "Hello"})
    assert next(active)

    def broken_reset():
        raise RuntimeError("Decoder reset failed")

    engine.model.speech_tokenizer.decoder.reset_streaming_state = broken_reset
    with pytest.raises(GenerationError, match="could not be reset safely"):
        active.close()
    assert not engine.last_metrics["complete"] and not engine._busy
    assert engine.last_metrics["resampler_tail_samples"] == 0


def test_qwen_sampling_temperature_is_forwarded_and_receipted(tmp_path):
    engine = backend(tmp_path, [result(np.full(1920, 0.1))])
    list(engine.generate({"text": "Hello", "temperature": 0.3}))
    assert engine.model.arguments[0]["temperature"] == 0.3
    assert engine.last_metrics["temperature"] == 0.3


def test_token_cap_cannot_be_reported_as_success_or_drain_tail(tmp_path):
    engine = backend(tmp_path, [result(np.full(240, 0.1), tokens=32, final=True)])
    with pytest.raises(GenerationError, match="may be truncated"):
        list(engine.generate({"text": "Hello", "max_tokens": 32}))
    assert not engine.last_metrics["complete"] and engine.last_metrics["eos"] is None
    assert engine.last_metrics["resampler_tail_samples"] == 0
    assert engine.model.speech_tokenizer.decoder.resets == 1


def test_generator_cancellation_discards_old_tail_and_successor_starts_cleanly(tmp_path):
    results = [result(np.full(240, 0.125)), result(np.full(240, 0.125))]
    engine = backend(tmp_path, results)
    stream = engine.generate({"text": "Hello"})
    first = next(stream)
    assert len(first) < 480 * 2
    stream.close()
    assert not engine.last_metrics["complete"] and engine.last_metrics["resampler_tail_samples"] == 0
    assert engine.model.closes == 1 and engine.model.speech_tokenizer.decoder.resets == 1
    complete = b"".join(engine.generate({"text": "Hello"}))
    assert len(complete) == 960 * 2 and complete.startswith(first)
    assert engine.model.closes == 2 and engine.model.speech_tokenizer.decoder.resets == 2


def test_busy_worker_does_not_reset_or_replace_the_active_utterance(tmp_path):
    engine = backend(tmp_path, [result(np.full(1920, 0.125))])
    stream = engine.generate({"text": "Hello"})
    next(stream)
    with pytest.raises(GenerationError, match="one utterance"):
        list(engine.generate({"text": "Other"}))
    assert engine.model.speech_tokenizer.decoder.resets == 0
    stream.close()


@pytest.mark.parametrize("invalid", [
    result([float("nan")]), result([float("inf")]), result([[0.1, 0.2], [0.3, 0.4]]),
    result([0.1], rate=True), result([0.1], rate=0), result([0.1], tokens=-1),
    result([0.1], tokens=0),
])
def test_invalid_model_pcm_fails_without_success_or_converter_tail(tmp_path, invalid):
    engine = backend(tmp_path, [invalid])
    with pytest.raises(GenerationError):
        list(engine.generate({"text": "Hello"}))
    assert not engine.last_metrics["complete"] and engine.last_metrics["resampler_tail_samples"] == 0
    assert engine.model.speech_tokenizer.decoder.resets == 1


def test_changing_sample_rate_mid_utterance_is_explicit_failure(tmp_path):
    engine = backend(tmp_path, [result(np.full(240, 0.1), 24000), result(np.full(480, 0.1), 48000)])
    with pytest.raises(GenerationError, match="changed sample rate"):
        list(engine.generate({"text": "Hello"}))
    assert not engine.last_metrics["complete"]


def test_silent_output_is_not_successful_speech(tmp_path):
    engine = backend(tmp_path, [result(np.zeros(240))])
    with pytest.raises(GenerationError, match="detectable speech"):
        list(engine.generate({"text": "Hello"}))
    assert engine.last_metrics["leading_silence_ms"] is None and not engine.last_metrics["complete"]


def test_empty_model_output_is_explicit_failure(tmp_path):
    engine = backend(tmp_path, [])
    with pytest.raises(GenerationError, match="without generating PCM"):
        list(engine.generate({"text": "Hello"}))


def test_signal_latency_is_distinct_from_first_pcm_and_clipping_is_bounded(tmp_path):
    engine = backend(tmp_path, [result(np.zeros(240)), result(np.full(240, 2.0))])
    pcm = b"".join(engine.generate({"text": "Hello"}))
    assert np.frombuffer(pcm, dtype="<i2").max() == 32767
    metrics = engine.last_metrics
    assert metrics["first_nonquiet_pcm_ms"] > metrics["first_pcm_ms"]
    assert 9 <= metrics["leading_silence_ms"] <= 10
    assert metrics["clipped_samples"] == 240


def test_warmup_discards_pcm_and_uses_model_defaults_without_playback(tmp_path):
    engine = backend(tmp_path, [result(np.full(240, 0.1))])
    metrics = engine.warmup()
    assert metrics["complete"] and engine.model.arguments[0]["voice"] == "ryan"
    assert engine.model.arguments[0]["stream"] is True
    assert engine.model.arguments[0]["streaming_interval"] == 0.08


def test_kokoro_uses_registered_local_voice_and_never_a_request_path(tmp_path, monkeypatch):
    monkeypatch.setattr("shiri.tts.backend.importlib.util.find_spec", lambda _name: object())
    (tmp_path / "voices").mkdir()
    voice = tmp_path / "voices/af_heart.safetensors"
    voice.touch()
    engine = backend(tmp_path, [result(np.full(240, 0.1))], model_id="kokoro-82m")
    engine.english_sentences = lambda text: (text,)
    list(engine.generate({"text": "Hello", "speed": 0.9}))
    assert engine.model.arguments == [{"text": "Hello", "voice": str(voice), "lang_code": "a", "speed": 0.9}]


def sentence_backend(tmp_path, monkeypatch):
    monkeypatch.setattr("shiri.tts.backend.importlib.util.find_spec", lambda _name: object())
    (tmp_path / "voices").mkdir()
    (tmp_path / "voices/af_heart.safetensors").touch()
    phrases = ("Good evening.", 'Dr. Jones paid 3.14 dollars and said, "Ready!"', "Then we left.")

    class SentenceModel(Model):
        def generate(self, **arguments):
            self.arguments.append(arguments)
            try:
                value = 0.1 * (phrases.index(arguments["text"]) + 1)
                yield result(np.full(480, value))
            finally:
                self.closes += 1

    model = SentenceModel([])
    engine = MLXBackend(get_model("kokoro-82m"), model, tmp_path, english_sentences=lambda _text: phrases)
    return engine, phrases


def test_complete_sentences_share_one_resampler_and_preserve_audio_order_and_tail(tmp_path, monkeypatch):
    engine, phrases = sentence_backend(tmp_path, monkeypatch)
    samples = np.frombuffer(b"".join(engine.generate({"text": " ".join(phrases)})), dtype="<i2")
    assert len(samples) == 2880  # Three 20 ms phrases remain exactly 60 ms.
    assert [item["text"] for item in engine.model.arguments] == list(phrases)
    for index, expected in enumerate([3277, 6554, 9830]):
        assert np.max(np.abs(samples[index*960+40:(index+1)*960-40].astype(int) - expected)) <= 1
    assert engine.model.closes == 3
    assert engine.last_metrics["resampler_tail_samples"] == 32
    assert engine.last_metrics["complete"]


def test_sentence_cancellation_closes_current_native_source_and_does_not_start_later_sentences(tmp_path, monkeypatch):
    engine, phrases = sentence_backend(tmp_path, monkeypatch)
    active = engine.generate({"text": " ".join(phrases)})
    assert next(active)
    active.close()
    assert [item["text"] for item in engine.model.arguments] == [phrases[0]]
    assert engine.model.closes == 1
    assert engine.last_metrics["resampler_tail_samples"] == 0
    assert not engine.last_metrics["complete"]


def test_actual_english_sentence_boundaries_preserve_abbreviations_decimals_and_quoted_punctuation():
    pytest.importorskip("spacy")
    text = 'The temperature is 22.5 degrees. Dr. Jones said, "Ready!" Then we left.'
    assert _english_sentences(text) == (
        "The temperature is 22.5 degrees.", 'Dr. Jones said, "Ready!"', "Then we left.",
    )


@pytest.mark.parametrize("language,voice", [("f", "ff_siwis"), ("j", "jf_alpha"), ("z", "zf_xiaobei")])
def test_non_english_kokoro_text_keeps_its_native_segmentation(tmp_path, language, voice):
    (tmp_path / "voices").mkdir()
    (tmp_path / "voices" / f"{voice}.safetensors").touch()
    engine = backend(tmp_path, [result(np.full(240, 0.1))], model_id="kokoro-82m")

    def unexpected_split(_text):
        raise AssertionError("English boundaries must not be applied to another language")

    engine.english_sentences = unexpected_split
    text = "Première phrase. Deuxième phrase."
    list(engine.generate({"text": text, "language": language, "voice": voice}))
    assert engine.model.arguments[0]["text"] == text


def test_missing_kokoro_voice_is_setup_failure(tmp_path):
    engine = backend(tmp_path, [], model_id="kokoro-82m")
    with pytest.raises(BackendUnavailable, match="voice asset"):
        list(engine.generate({"text": "Hello"}))


def test_backend_requires_preinstalled_assets_and_explicit_local_paths(tmp_path):
    model = get_model(QWEN_MODEL_ID)
    with pytest.raises(BackendUnavailable, match="incomplete"):
        _resolve_model_path(model, None, tmp_path, False)
    with pytest.raises(ValueError, match="absolute Path"):
        _resolve_model_path(model, None, "https://example.test/model", False)


def test_native_backend_fails_clearly_on_an_unsupported_host(monkeypatch):
    monkeypatch.setattr("shiri.tts.backend.platform.system", lambda: "Linux")
    with pytest.raises(BackendUnavailable, match="native Apple Silicon"):
        load_backend(get_model(QWEN_MODEL_ID))
