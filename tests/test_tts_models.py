"""Public model capabilities and the boundary between requests and installation."""
from dataclasses import replace
import json
import os
import subprocess
import sys

import pytest

from shiri.tts.models import (
    DEFAULT_MODEL_ID, QWEN_MODEL_ID, GenerationRequest, get_model, get_models, load_registry, validate_request,
)


def test_model_catalogue_has_truthful_streaming_and_controls():
    models = {model.id: model for model in get_models()}
    qwen, kokoro, soprano = (models[key] for key in (QWEN_MODEL_ID, DEFAULT_MODEL_ID, "soprano-1.1-80m"))
    assert qwen.streaming == "incremental" and not qwen.supports_instruction and not qwen.supports_speed
    assert qwen.experimental and not kokoro.experimental and not soprano.experimental
    assert DEFAULT_MODEL_ID == "kokoro-82m" and kokoro.default_voice == "af_heart"
    assert kokoro.streaming == "phrase" and kokoro.supports_speed and len(kokoro.voices) == 54
    assert soprano.streaming == "phrase" and soprano.voices == ("default",) and soprano.languages == ("en",)
    assert json.loads(json.dumps(qwen.public_dict()))["voices"] == list(qwen.voices)
    assert qwen.to_dict() == qwen.public_dict()


def test_model_metadata_import_does_not_load_native_audio_dependencies():
    result = subprocess.run([
        sys.executable, "-c", "import sys; import shiri.tts.models; "
        "assert not {'mlx', 'mlx.core', 'mlx_audio', 'av', 'numpy'} & sys.modules.keys()",
    ], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def registry_file(tmp_path, models):
    path = tmp_path / "models.json"
    path.write_text(json.dumps({"version": 1, "models": models}))
    path.chmod(0o600)
    return path


def test_trusted_registry_can_add_a_distinct_pinned_backend(tmp_path):
    model = replace(get_model(QWEN_MODEL_ID), id="qwen3-1.7b-style",
                    repo_id="mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-4bit",
                    revision="a" * 40, supports_instruction=True)
    loaded = load_registry(registry_file(tmp_path, [model.to_dict()]))
    assert loaded[model.id] == model and loaded[QWEN_MODEL_ID] == get_model(QWEN_MODEL_ID)
    request = validate_request(model, {"text": "Hello", "instruction": "Speak warmly."})
    assert request.instruction == "Speak warmly."


@pytest.mark.parametrize("change", [
    {"revision": "main"}, {"repo_id": "https://example.test/model"}, {"repo_id": "/tmp/model"},
    {"backend": "arbitrary_remote_code"}, {"streaming": "phrase"}, {"supports_speed": True},
    {"supports_instruction": True}, {"arbitrary_loader": "os.system"}, {"default_voice": "unknown"},
    {"experimental": "true"},
])
def test_registry_rejects_unpinned_or_incompatible_models(tmp_path, change):
    values = get_model(QWEN_MODEL_ID).to_dict()
    values.update(id="custom", **change)
    with pytest.raises(ValueError):
        load_registry(registry_file(tmp_path, [values]))


def test_registry_cannot_replace_existing_model_or_be_world_writable(tmp_path):
    model = get_model(QWEN_MODEL_ID).to_dict()
    path = registry_file(tmp_path, [model])
    with pytest.raises(ValueError, match="replace built-ins"):
        load_registry(path)
    path = registry_file(tmp_path, [])
    path.chmod(0o666)
    with pytest.raises(ValueError, match="administrator-owned"):
        load_registry(path)


def test_registry_rejects_a_symlink_to_an_otherwise_trusted_file(tmp_path):
    target = registry_file(tmp_path, [])
    link = tmp_path / "linked-models.json"
    link.symlink_to(target)
    assert len(load_registry(target)) == len(get_models())
    with pytest.raises(ValueError, match="regular file"):
        load_registry(link)


def test_registry_rejects_a_fifo_without_waiting_for_a_writer(tmp_path):
    path = tmp_path / "models.fifo"
    os.mkfifo(path, 0o600)
    # Keep the timeout outside the reader so a regression fails rather than
    # hanging the test runner on the FIFO's blocking open.
    result = subprocess.run([
        sys.executable, "-c", """
import sys
from pathlib import Path
from shiri.tts.models import load_registry
try:
    load_registry(Path(sys.argv[1]))
except ValueError as exc:
    assert 'regular file' in str(exc), str(exc)
else:
    raise AssertionError('FIFO registry was accepted')
""", str(path),
    ], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr


def test_registry_rejects_an_oversized_regular_file(tmp_path):
    path = registry_file(tmp_path, [])
    with path.open("ab") as stream:
        stream.write(b" " * (1024 * 1024))
    with pytest.raises(ValueError, match="bounded"):
        load_registry(path)


def test_request_resolves_registered_defaults_and_explicit_controls():
    qwen = get_model(QWEN_MODEL_ID)
    request = GenerationRequest(text="Hello").validated(qwen)
    assert request.voice == "ryan" and request.language == "English" and request.streaming_interval == 0.08
    assert request.seed == 42
    assert request.temperature == 0.9
    assert validate_request(qwen, request.to_dict()) == request
    kokoro = validate_request(get_model("kokoro-82m"), {"text": "Hello", "voice": "af_bella", "speed": 0.85})
    assert kokoro.speed == 0.85 and kokoro.streaming_interval is None
    assert kokoro.temperature is None


@pytest.mark.parametrize("change", [
    {"text": ""}, {"text": " \n "}, {"text": "a" * 2001}, {"text": "Hello\x00"},
    {"voice": "/tmp/voice"}, {"language": "unlisted"}, {"model": "https://example.test/model"},
    {"local_path": "/tmp/weights"}, {"speed": 1.1}, {"speed": True}, {"speed": float("nan")},
    {"instruction": "Very happy"}, {"streaming_interval": 0.01}, {"streaming_interval": True},
    {"max_tokens": True}, {"max_tokens": 1}, {"max_tokens": 751},
    {"seed": True}, {"seed": -1}, {"seed": 2**32}, {"seed": 42.0}, {"seed": "42"},
    {"temperature": -0.1}, {"temperature": 1.01}, {"temperature": float("nan")},
    {"temperature": True}, {"temperature": "0.3"},
])
def test_http_generation_options_cannot_bypass_registered_capabilities(change):
    payload = {"text": "Hello", **change}
    with pytest.raises(ValueError):
        validate_request(get_model(QWEN_MODEL_ID), payload)


def test_phrase_model_does_not_silently_accept_a_streaming_interval():
    with pytest.raises(ValueError, match="complete phrases"):
        validate_request(get_model("kokoro-82m"), {"text": "Hello", "streaming_interval": 0.08})


def test_unknown_model_id_is_rejected():
    with pytest.raises(ValueError, match="Unknown registered"):
        get_model("other")


@pytest.mark.parametrize("seed", [0, 42, 2**32 - 1])
def test_sampling_seed_is_validated_and_retained(seed):
    request = validate_request(get_model(QWEN_MODEL_ID), {"text": "Hello", "seed": seed})
    assert request.seed == seed and request.to_dict()["seed"] == seed


@pytest.mark.parametrize("temperature", [0.0, 0.3, 0.7, 1.0])
def test_bounded_qwen_temperature_is_retained(temperature):
    request = validate_request(get_model(QWEN_MODEL_ID), {"text": "Hello", "temperature": temperature})
    assert request.temperature == temperature


def test_phrase_model_cannot_silently_ignore_sampling_temperature():
    with pytest.raises(ValueError, match="Only the Qwen"):
        validate_request(get_model("kokoro-82m"), {"text": "Hello", "temperature": 0.3})
