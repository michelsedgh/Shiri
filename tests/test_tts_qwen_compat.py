"""Exact input layout and guarded instance ownership without native MLX."""
import hashlib
from types import SimpleNamespace

import numpy as np
import pytest

from shiri.tts import qwen_compat


class Tokenizer:
    def __init__(self):
        self.texts = []

    def encode(self, text):
        self.texts.append(text)
        # Three role tokens, three text tokens, five closing/template tokens.
        return [100, 101, 102, 3, 4, 5, 200, 201, 202, 203, 204]


class Model:
    def __init__(self):
        self.tokenizer = Tokenizer()
        self.arguments = []
        self.config = SimpleNamespace(tts_model_type="custom_voice", tts_eos_token_id=7,
                                      talker_config=SimpleNamespace(codec_pad_id=11, codec_bos_id=13))
        embedding = lambda ids: np.asarray(ids)[..., None]  # noqa: E731
        self.talker = SimpleNamespace(get_text_embeddings=lambda: embedding,
                                      get_input_embeddings=lambda: embedding,
                                      text_projection=lambda values: values * 2)

    def _prepare_generation_inputs(self, **arguments):
        self.arguments.append(arguments)
        prefix = np.array([1000, 1001, 1002, 1003, 9999])[None, :, None]
        return prefix, np.array([88])[None, :, None], np.array([50])[None, :, None]


def test_full_text_layout_retains_prefix_and_orders_text_eos_pad_and_bos():
    model = Model()
    adapted = qwen_compat.full_text_preparer(model, model._prepare_generation_inputs, arrays=np)
    inputs, trailing, pad = adapted("Hello", language="English", speaker="Ryan", instruct="Speak calmly")
    assert inputs[0, :, 0].tolist() == [1000, 1001, 1002, 1003, 17, 19, 21, 25, 63]
    assert trailing is pad and trailing.tolist() == [[[50]]]
    assert model.arguments == [{"text": "Hello", "language": "English", "speaker": "Ryan",
                                "ref_audio": None, "ref_text": None, "instruct": "Speak calmly"}]
    assert model.tokenizer.texts == ["<|im_start|>assistant\nHello<|im_end|>\n<|im_start|>assistant\n"]


def test_full_text_customvoice_adapter_rejects_reference_audio_before_inference():
    model = Model()
    adapted = qwen_compat.full_text_preparer(model, model._prepare_generation_inputs, arrays=np)
    with pytest.raises(qwen_compat.QwenCompatibilityError, match="reference audio"):
        adapted("Hello", ref_audio=np.zeros(16))
    assert model.arguments == []


def verified_source(tmp_path, monkeypatch):
    source = tmp_path / "qwen.py"
    source.write_bytes(b"verified test source")
    monkeypatch.setattr(qwen_compat.importlib.metadata, "version", lambda _name: "0.5.7")
    monkeypatch.setattr(qwen_compat.inspect, "getsourcefile", lambda _model: str(source))
    monkeypatch.setattr(qwen_compat, "QWEN_SOURCE_SHA256", hashlib.sha256(source.read_bytes()).hexdigest())
    return source


def test_installer_is_instance_scoped_idempotent_and_does_not_edit_source(tmp_path, monkeypatch):
    source = verified_source(tmp_path, monkeypatch)
    first, second = Model(), Model()
    original = source.read_bytes()
    qwen_compat.install_full_text_prefill(first, arrays=np)
    adapted = first._prepare_generation_inputs
    qwen_compat.install_full_text_prefill(first, arrays=np)
    assert first._prepare_generation_inputs is adapted and first._shiri_full_text_prefill
    assert not hasattr(second, "_shiri_full_text_prefill")
    assert second._prepare_generation_inputs.__func__ is Model._prepare_generation_inputs
    assert source.read_bytes() == original


@pytest.mark.parametrize("change", ["version", "source", "checkpoint"])
def test_incompatible_package_or_checkpoint_cannot_silently_use_adapter(tmp_path, monkeypatch, change):
    source = verified_source(tmp_path, monkeypatch)
    model = Model()
    original = model._prepare_generation_inputs
    if change == "version":
        monkeypatch.setattr(qwen_compat.importlib.metadata, "version", lambda _name: "0.5.8")
    elif change == "source":
        source.write_bytes(b"different inference implementation")
    else:
        model.config.tts_model_type = "base"
    with pytest.raises(qwen_compat.QwenCompatibilityError):
        qwen_compat.install_full_text_prefill(model, arrays=np)
    assert model._prepare_generation_inputs == original and not hasattr(model, "_shiri_full_text_prefill")
