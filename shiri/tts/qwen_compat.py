"""Guarded, instance-local CustomVoice input compatibility for MLX Audio 0.5.7.

Official CustomVoice defaults to full-text prefill (non_streaming_mode=True):
https://github.com/QwenLM/Qwen3-TTS/blob/022e286b98fbec7e1e916cb940cdf532cd9f488e/qwen_tts/core/models/modeling_qwen3_tts.py
MLX Audio's CustomVoice path instead feeds text alongside generated codec steps:
https://github.com/Blaizzy/mlx-audio/blob/94c7716212b2228f178d2f9c7619a591fd1b0b78/mlx_audio/tts/models/qwen3_tts/qwen3_tts.py

This adapter preserves its role, instruction, speaker and codec prefix, then
assembles the official full-text/PAD/BOS layout. Audio decoding stays incremental.
Only the loaded model instance is adapted; package files and global classes are
never changed. A different upstream version/source requires explicit review.
"""
from __future__ import annotations

from collections.abc import Callable
import hashlib
import importlib
import importlib.metadata
import inspect
from pathlib import Path

MLX_AUDIO_VERSION = "0.5.7"
QWEN_SOURCE_SHA256 = "0d9437e4f08680d7bf8cb7bf3b44c8e3de37ad9edf025f50dba37dface2e6902"


class QwenCompatibilityError(RuntimeError):
    """The installed inference source does not match the qualified adapter."""


def full_text_preparer(model, original: Callable, *, arrays):
    """Build an instance method; arrays is MLX in production, NumPy in tests."""
    def prepare(text: str, language: str = "auto", speaker: str | None = None,
                ref_audio=None, ref_text: str | None = None, instruct: str | None = None):
        if ref_audio is not None or ref_text is not None:
            raise QwenCompatibilityError("The CustomVoice full-text adapter does not accept reference audio")
        streaming, _trailing, text_pad = original(
            text=text, language=language, speaker=speaker,
            ref_audio=ref_audio, ref_text=ref_text, instruct=instruct,
        )
        chat = f"<|im_start|>assistant\n{text}<|im_end|>\n<|im_start|>assistant\n"
        ids = arrays.array(model.tokenizer.encode(chat))[None, :]
        text_embedding = model.talker.text_projection(model.talker.get_text_embeddings()(ids[:, 3:-5]))
        text_eos = model.talker.text_projection(model.talker.get_text_embeddings()(
            arrays.array([[model.config.tts_eos_token_id]])))
        codec_pad = model.talker.get_input_embeddings()(
            arrays.array([[model.config.talker_config.codec_pad_id]]))
        codec_bos = model.talker.get_input_embeddings()(
            arrays.array([[model.config.talker_config.codec_bos_id]]))
        full_text = arrays.concatenate([text_embedding, text_eos], axis=1)
        # Replace the streaming first-token/BOS overlay, retaining the complete
        # earlier prefix. All text plus its EOS is over codec PAD; then text PAD
        # is over codec BOS. Subsequent codec steps use text PAD only.
        input_embedding = arrays.concatenate([
            streaming[:, :-1, :], full_text + codec_pad, text_pad + codec_bos,
        ], axis=1)
        return input_embedding, text_pad, text_pad

    return prepare


def install_full_text_prefill(model, *, arrays=None) -> None:
    """Adapt one verified CustomVoice instance without editing MLX Audio."""
    if getattr(getattr(model, "config", None), "tts_model_type", None) != "custom_voice":
        raise QwenCompatibilityError("The full-text adapter requires a CustomVoice checkpoint")
    try:
        version = importlib.metadata.version("mlx-audio")
        source_file = inspect.getsourcefile(type(model))
        source_hash = hashlib.sha256(Path(source_file).read_bytes()).hexdigest() if source_file else None
    except (importlib.metadata.PackageNotFoundError, OSError, TypeError) as exc:
        raise QwenCompatibilityError("Could not verify the installed Qwen inference source") from exc
    if version != MLX_AUDIO_VERSION or source_hash != QWEN_SOURCE_SHA256:
        raise QwenCompatibilityError(
            "Qwen input compatibility requires mlx-audio 0.5.7 with its verified source; "
            "review the adapter before using a different installation"
        )
    if getattr(model, "_shiri_full_text_prefill", False):
        return
    arrays = importlib.import_module("mlx.core") if arrays is None else arrays
    model._prepare_generation_inputs = full_text_preparer(model, model._prepare_generation_inputs, arrays=arrays)
    model._shiri_full_text_prefill = True
