"""Synchronous generation adapter for the isolated, persistent native worker.

Each utterance has one resampler. Natural completion drains it exactly once;
cancellation and invalid generation discard its pending tail. Nothing here
opens an audio device, plays a sample, or changes a room's music state.
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from fractions import Fraction
from functools import lru_cache
import importlib
import math
from pathlib import Path
import platform
import sys
import time

from .models import (
    GenerationRequest, MAX_GENERATED_SECONDS, MAX_GENERATION_SECONDS, ModelSpec, validate_request,
)

RATE = 48000
SIGNAL_THRESHOLD = 33  # approximately -60 dBFS for signed 16-bit PCM


class BackendUnavailable(RuntimeError):
    """The configured model or native generation dependencies are unavailable."""


class GenerationError(RuntimeError):
    """Generation failed or its output cannot be accepted as complete speech."""


@lru_cache(maxsize=1)
def _english_sentencizer():
    """A local tokenizer and boundary rules; no model download or NLP weights."""
    try:
        spacy = importlib.import_module("spacy")
    except ImportError as exc:
        raise BackendUnavailable("Install Kokoro's English text-processing dependencies first") from exc
    pipeline = spacy.blank("en")
    pipeline.add_pipe("sentencizer")
    return pipeline


def _english_sentences(text: str) -> tuple[str, ...]:
    return tuple(sentence.text.strip() for sentence in _english_sentencizer()(text).sents
                 if sentence.text.strip())


def _resolve_model_path(spec: ModelSpec, cache_dir: Path | None, local_model_path: Path | None,
                        allow_download: bool) -> Path:
    if local_model_path is not None:
        if not isinstance(local_model_path, Path) or not local_model_path.is_absolute():
            raise ValueError("A model asset directory must be an installation-owned absolute Path")
        path = local_model_path
    else:
        if cache_dir is not None and (not isinstance(cache_dir, Path) or not cache_dir.is_absolute()):
            raise ValueError("A model cache must be an installation-owned absolute Path")
        try:
            from huggingface_hub import snapshot_download
            path = Path(snapshot_download(
                spec.repo_id, revision=spec.revision, cache_dir=str(cache_dir) if cache_dir else None,
                local_files_only=not allow_download,
                allow_patterns=["*.json", "*.txt", "*.safetensors", "*.md"],
            ))
        except Exception as exc:
            raise BackendUnavailable("Pinned model assets are unavailable; install them before starting speech") from exc
    if not path.is_dir() or not (path / "config.json").is_file():
        raise BackendUnavailable("The registered model asset directory is incomplete")
    return path


def load_backend(spec: ModelSpec, cache_dir: Path | None = None, *,
                 local_model_path: Path | None = None, allow_download: bool = False) -> MLXBackend:
    """Load only a registered model, with immutable cached assets by default.

    allow_download is an installation/setup choice, not a request option. A
    supplied local_model_path is trusted deployment configuration. Never pass a
    client's repository or file path through to this function.
    """
    if not isinstance(spec, ModelSpec) or type(allow_download) is not bool:
        raise ValueError("The native worker requires a registered model and explicit asset policy")
    if platform.system() != "Darwin" or platform.machine().lower() not in ("arm64", "aarch64"):
        raise BackendUnavailable("MLX speech generation requires a native Apple Silicon host")
    try:
        mx = importlib.import_module("mlx.core")
        utils = importlib.import_module("mlx_audio.tts.utils")
        importlib.import_module("av")
        importlib.import_module("numpy")
    except ImportError as exc:
        raise BackendUnavailable("Install Shiri's native speech generation dependencies first") from exc
    path = _resolve_model_path(spec, cache_dir, local_model_path, allow_download)
    # These are process-local limits. The worker runs separately from room audio.
    mx.set_memory_limit(3 * 1024**3)
    mx.set_cache_limit(512 * 1024**2)
    mx.set_wired_limit(2 * 1024**3)
    architecture = {"qwen3_custom_voice": "qwen3_tts", "kokoro": "kokoro", "soprano": "soprano"}[spec.backend]
    try:
        # Explicit architecture also handles Kokoro's config lacking model_type:
        # an immutable snapshot basename is a commit hash, not an architecture.
        model = utils.load_model(path, model_type=architecture, strict=True)
    except Exception as exc:
        raise BackendUnavailable("The pinned model could not be loaded by its registered backend") from exc
    if spec.backend == "qwen3_custom_voice":
        config = getattr(model, "config", None)
        if getattr(config, "tts_model_type", None) != "custom_voice":
            raise BackendUnavailable("The Qwen CustomVoice adapter requires a CustomVoice checkpoint")
        voices = {str(voice).lower() for voice in model.supported_speakers}
        languages = {str(language).lower() for language in model.supported_languages}
        if (not set(spec.voices) <= voices
                or not {language.lower() for language in spec.languages} <= languages):
            raise BackendUnavailable("The loaded checkpoint does not match its registered voices or languages")
        from .qwen_compat import QwenCompatibilityError, install_full_text_prefill
        try:
            install_full_text_prefill(model, arrays=mx)
        except QwenCompatibilityError as exc:
            raise BackendUnavailable(str(exc)) from exc
    return MLXBackend(spec, model, path, evaluate=mx.eval, peak_memory=mx.get_peak_memory,
                      clear_cache=mx.clear_cache, seed_random=mx.random.seed)


class MLXBackend:
    """One loaded model, used serially by one worker process.

    generate accepts GenerationRequest or a strict generation-options mapping
    and yields bytes of mono 48 kHz s16le PCM. Chunks are not necessarily 20 ms;
    the delivery coordinator owns bounded framing/pacing. last_metrics reports
    generated PCM timing, not microphone-measured audible timing.
    """

    def __init__(self, spec: ModelSpec, model, model_path: Path, *, now=time.monotonic,
                 evaluate=None, peak_memory=None, clear_cache=None, seed_random=None, english_sentences=None):
        self.spec, self.model, self.model_path = spec, model, model_path
        self.now = now
        self.evaluate = evaluate or (lambda _value: None)
        self.peak_memory = peak_memory or (lambda: 0)
        self.clear_cache = clear_cache or (lambda: None)
        self.seed_random = seed_random or (lambda _seed: None)
        self.english_sentences = english_sentences or _english_sentences
        self.last_metrics: dict = {}
        self._busy = False

    def _source(self, request: GenerationRequest):
        if self.spec.backend == "qwen3_custom_voice":
            return self.model.generate(
                text=request.text, voice=request.voice, lang_code=request.language,
                instruct=request.instruction, stream=True, streaming_interval=request.streaming_interval,
                max_tokens=request.max_tokens, temperature=request.temperature, verbose=False,
            )
        if self.spec.backend == "kokoro":
            voice_path = self.model_path / "voices" / f"{request.voice}.safetensors"
            if not voice_path.is_file():
                raise BackendUnavailable("The selected voice asset is not installed")
            if request.language in ("a", "b") and importlib.util.find_spec("en_core_web_sm") is None:
                raise BackendUnavailable("Install Kokoro's English text-processing asset before starting speech")
            phrases = (self.english_sentences(request.text) if request.language in ("a", "b")
                       else (request.text,))

            def sentence_source():
                # Complete natural sentences can arrive before the paragraph is
                # synthesized. The outer utterance still owns one resampler and
                # one final tail. Closing it stops the current native iterator
                # before any later sentence begins.
                for phrase in phrases:
                    native = iter(self.model.generate(text=phrase, voice=str(voice_path),
                                                      lang_code=request.language, speed=request.speed))
                    try:
                        for generated in native:  # noqa: UP028 - this owner closes the native iterator exactly once.
                            yield generated
                    finally:
                        close = getattr(native, "close", None)
                        if close is not None:
                            close()

            return sentence_source()
        return self.model.generate(text=request.text, max_tokens=request.max_tokens, verbose=False)

    def _reset_decoder(self):
        tokenizer = getattr(self.model, "speech_tokenizer", None)
        decoder = getattr(tokenizer, "decoder", None)
        reset = getattr(decoder, "reset_streaming_state", None)
        if reset is not None:
            reset()
        self.clear_cache()

    def warmup(self) -> dict:
        """Generate and discard a short sample; never open a playback device."""
        for _ in self.generate(GenerationRequest(text="Shiri is ready.", max_tokens=128)):
            pass
        return dict(self.last_metrics)

    prewarm = warmup

    def generate(self, payload: GenerationRequest | Mapping) -> Iterator[bytes]:
        request = validate_request(self.spec, payload)
        if self._busy:
            raise GenerationError("A model worker can synthesize only one utterance at a time")
        from av import AudioFrame, AudioResampler
        import numpy as np

        before = self.now()
        metrics = {
            "model_id": self.spec.id, "sample_rate": RATE, "channels": 1, "format": "s16le",
            "voice": request.voice, "language": request.language, "speed": request.speed,
            "streaming_interval": request.streaming_interval, "max_tokens": request.max_tokens,
            "seed": request.seed,
            "temperature": request.temperature if self.spec.backend == "qwen3_custom_voice" else None,
            "text_input_mode": "full" if getattr(self.model, "_shiri_full_text_prefill", False) else None,
            "streaming": self.spec.streaming, "first_pcm_ms": None, "first_native_pcm_ms": None,
            "first_nonquiet_pcm_ms": None, "leading_silence_ms": None, "generated_audio_seconds": 0.0,
            "generation_ms": 0.0, "rtf": None, "tokens": 0, "eos": None, "complete": False,
            "source_chunks": 0, "output_chunks": 0, "clipped_samples": 0, "resampler_tail_samples": 0,
            "peak_ml_bytes": 0,
        }
        self.last_metrics = metrics
        converter = AudioResampler(format="s16", layout="mono", rate=RATE)
        self._busy = True
        source_rate = None
        source_samples = 0
        output_samples = 0
        iterator = None
        cap_reached = False

        def normalized(frames, *, tail=False):
            nonlocal output_samples
            for frame in frames:
                pcm = bytes(frame.planes[0])[:frame.samples * 2]
                if len(pcm) != frame.samples * 2:
                    raise GenerationError("The speech resampler returned an incomplete frame")
                if sys.byteorder != "little":
                    pcm = np.frombuffer(pcm, dtype=np.int16).astype("<i2").tobytes()
                if not pcm:
                    continue
                elapsed_ms = (self.now() - before) * 1000
                if metrics["first_pcm_ms"] is None:
                    metrics["first_pcm_ms"] = elapsed_ms
                values = np.frombuffer(pcm, dtype="<i2").astype(np.int32)
                signal = np.flatnonzero(np.abs(values) >= SIGNAL_THRESHOLD)
                if len(signal) and metrics["first_nonquiet_pcm_ms"] is None:
                    metrics["first_nonquiet_pcm_ms"] = elapsed_ms
                    metrics["leading_silence_ms"] = (output_samples + int(signal[0])) / RATE * 1000
                output_samples += frame.samples
                if output_samples > RATE * MAX_GENERATED_SECONDS:
                    raise GenerationError("Generated speech exceeds the sixty-second audio limit")
                metrics["generated_audio_seconds"] = output_samples / RATE
                metrics["output_chunks"] += 1
                if tail:
                    metrics["resampler_tail_samples"] += frame.samples
                yield pcm

        try:
            # MLX's process RNG otherwise advances across unrelated utterances.
            # Reset per admitted request so persistent-worker sampling matches
            # repeated benchmarks and does not inherit the previous utterance.
            self.seed_random(request.seed)
            iterator = iter(self._source(request))
            for result in iterator:
                if self.now() - before > MAX_GENERATION_SECONDS:
                    raise GenerationError("Speech generation exceeded its time limit")
                rate = getattr(result, "sample_rate", None)
                if type(rate) is not int or not 8000 <= rate <= 192000:
                    raise GenerationError("The model emitted an invalid sample rate")
                if source_rate is not None and source_rate != rate:
                    raise GenerationError("The model changed sample rate during an utterance")
                source_rate = rate
                self.evaluate(result.audio)
                audio = np.asarray(result.audio, dtype=np.float32)
                if audio.ndim == 2 and audio.shape[0] == 1:
                    audio = audio[0]
                if audio.ndim != 1 or len(audio) > rate * MAX_GENERATED_SECONDS or not np.isfinite(audio).all():
                    raise GenerationError("The model must emit bounded, finite mono PCM")
                tokens = getattr(result, "token_count", 0)
                if type(tokens) is not int or tokens < 0:
                    raise GenerationError("The model emitted invalid speech token metadata")
                if self.spec.backend == "qwen3_custom_voice" and len(audio) and not tokens:
                    raise GenerationError("Incremental Qwen PCM requires speech token metadata")
                metrics["tokens"] += tokens
                if self.spec.backend == "qwen3_custom_voice":
                    cap_reached = metrics["tokens"] >= request.max_tokens
                elif self.spec.backend == "soprano":
                    cap_reached |= tokens >= request.max_tokens
                if not len(audio):
                    continue
                if metrics["first_native_pcm_ms"] is None:
                    metrics["first_native_pcm_ms"] = (self.now() - before) * 1000
                metrics["source_chunks"] += 1
                metrics["clipped_samples"] += int(np.count_nonzero(np.abs(audio) > 1.0))
                frame = AudioFrame.from_ndarray(np.clip(audio, -1.0, 1.0).reshape(1, -1),
                                               format="flt", layout="mono")
                frame.sample_rate = rate
                frame.time_base = Fraction(1, rate)
                frame.pts = source_samples
                source_samples += len(audio)
                yield from normalized(converter.resample(frame))
            if self.now() - before > MAX_GENERATION_SECONDS:
                raise GenerationError("Speech generation exceeded its time limit")
            if cap_reached:
                raise GenerationError("The model reached its speech token limit; the utterance may be truncated")
            if not source_samples:
                raise GenerationError("The model completed without generating PCM")
            yield from normalized(converter.resample(None), tail=True)
            if output_samples == 0:
                raise GenerationError("The model completed without deliverable PCM")
            if metrics["first_nonquiet_pcm_ms"] is None:
                raise GenerationError("The model completed without detectable speech")
            metrics["complete"] = True
            metrics["eos"] = True if self.spec.backend == "qwen3_custom_voice" else None
        finally:
            # Cancellation deliberately discards the converter tail. A later
            # utterance receives a new converter and cannot inherit old audio.
            # GeneratorExit is cooperative cancellation, not proof that cleanup
            # succeeded. close() must surface a failed reset so the worker cannot
            # acknowledge cancellation and reuse an uncertain decoder.
            primary_error = sys.exc_info()[0] not in (None, GeneratorExit)
            try:
                if iterator is not None:
                    close = getattr(iterator, "close", None)
                    if close is not None:
                        close()
            finally:
                self._busy = False
                try:
                    self._reset_decoder()
                except Exception as exc:
                    metrics["complete"] = False
                    if not primary_error:
                        raise GenerationError("The speech decoder could not be reset safely") from exc
                elapsed = max(0.0, self.now() - before)
                metrics["generation_ms"] = elapsed * 1000
                metrics["rtf"] = elapsed / metrics["generated_audio_seconds"] if output_samples else None
                peak = self.peak_memory()
                metrics["peak_ml_bytes"] = int(peak) if math.isfinite(peak) and peak >= 0 else 0
