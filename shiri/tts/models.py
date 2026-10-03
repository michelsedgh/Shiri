"""Pinned, trusted model catalogue and model-specific request validation.

HTTP callers select a registered ID. Repository addresses, revisions and local
asset directories belong to installation configuration, never a speech request.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Literal

BackendType = Literal["qwen3_custom_voice", "kokoro", "soprano"]
BACKEND_TYPES = {"qwen3_custom_voice", "kokoro", "soprano"}
QWEN_MODEL_ID = "qwen3-0.6b-customvoice"
DEFAULT_MODEL_ID = "kokoro-82m"
MAX_TEXT_CHARS = 2000
MAX_GENERATED_SECONDS = 60.0
MAX_GENERATION_SECONDS = 180.0
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,79}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class ModelSpec:
    id: str
    name: str
    backend: BackendType
    repo_id: str
    revision: str
    voices: tuple[str, ...]
    languages: tuple[str, ...]
    default_voice: str
    default_language: str
    streaming: Literal["incremental", "phrase"]
    supports_speed: bool = False
    supports_instruction: bool = False
    default_streaming_interval: float = 0.16
    license: str = "Apache-2.0"
    experimental: bool = False

    def __post_init__(self):
        if not isinstance(self.id, str) or not _IDENTIFIER.fullmatch(self.id):
            raise ValueError("A model requires a stable catalogue ID")
        if not isinstance(self.backend, str) or self.backend not in BACKEND_TYPES:
            raise ValueError("Unsupported text-to-speech backend")
        if not isinstance(self.name, str) or not self.name.strip() or len(self.name) > 160:
            raise ValueError("A model requires a bounded display name")
        if not isinstance(self.repo_id, str) or not _REPOSITORY.fullmatch(self.repo_id):
            raise ValueError("A model requires a Hugging Face repository ID, not a URL or file path")
        if not isinstance(self.revision, str) or not _REVISION.fullmatch(self.revision):
            raise ValueError("A model requires an immutable 40-character revision")
        for values in (self.voices, self.languages):
            if (not isinstance(values, tuple) or not 1 <= len(values) <= 128
                    or any(not isinstance(v, str) or not v or len(v) > 80
                           or any(ord(c) < 32 for c in v) for v in values)
                    or len(set(values)) != len(values)):
                raise ValueError("Model voices and languages must be distinct, bounded catalogue values")
        if self.default_voice not in self.voices or self.default_language not in self.languages:
            raise ValueError("Model defaults must belong to its supported voices and languages")
        if (type(self.supports_speed) is not bool or type(self.supports_instruction) is not bool
                or type(self.experimental) is not bool):
            raise ValueError("Model capabilities must be explicit booleans")
        expected_streaming = "incremental" if self.backend == "qwen3_custom_voice" else "phrase"
        if self.streaming != expected_streaming:
            raise ValueError("Streaming capability must match the implemented backend")
        if self.supports_speed != (self.backend == "kokoro"):
            raise ValueError("Only the Kokoro adapter implements speaking-rate control")
        if self.supports_instruction and self.backend != "qwen3_custom_voice":
            raise ValueError("This backend does not implement instruction control")
        if self.supports_instruction and "0.6b" in self.repo_id.lower():
            raise ValueError("Instruction control is not qualified for Qwen3 0.6B")
        if not _number(self.default_streaming_interval, 0.08, 1.0):
            raise ValueError("Streaming interval must be between 80 and 1000 milliseconds")
        if not isinstance(self.license, str) or not self.license or len(self.license) > 160:
            raise ValueError("A model requires license provenance")

    def public_dict(self) -> dict:
        result = asdict(self)
        result["voices"] = list(self.voices)
        result["languages"] = list(self.languages)
        return result

    def to_dict(self) -> dict:
        return self.public_dict()


def _number(value, lower: float, upper: float) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and lower <= value <= upper


_KOKORO_VOICES = (
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore", "af_nicole", "af_nova",
    "af_river", "af_sarah", "af_sky", "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam",
    "am_michael", "am_onyx", "am_puck", "am_santa", "bf_alice", "bf_emma", "bf_isabella",
    "bf_lily", "bm_daniel", "bm_fable", "bm_george", "bm_lewis", "ef_dora", "em_alex", "em_santa",
    "ff_siwis", "hf_alpha", "hf_beta", "hm_omega", "hm_psi", "if_sara", "im_nicola", "jf_alpha",
    "jf_gongitsune", "jf_nezumi", "jf_tebukuro", "jm_kumo", "pf_dora", "pm_alex", "pm_santa",
    "zf_xiaobei", "zf_xiaoni", "zf_xiaoxiao", "zf_xiaoyi", "zm_yunjian", "zm_yunxi", "zm_yunxia", "zm_yunyang",
)
BUILTIN_MODELS = (
    ModelSpec(
        id=QWEN_MODEL_ID, name="Qwen3 0.6B · experimental streaming voices", backend="qwen3_custom_voice",
        repo_id="mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-4bit",
        revision="08c72cad5e2fd0f41730c8bd1f28149585e46361",
        voices=("ryan", "aiden", "serena", "vivian", "uncle_fu", "ono_anna", "sohee", "eric", "dylan"),
        languages=("English", "Chinese", "Japanese", "Korean", "German", "French", "Russian",
                   "Portuguese", "Spanish", "Italian"),
        default_voice="ryan", default_language="English", streaming="incremental",
        default_streaming_interval=0.08, experimental=True,
    ),
    ModelSpec(
        id="kokoro-82m", name="Kokoro 82M · voice presets", backend="kokoro",
        repo_id="mlx-community/Kokoro-82M-bf16", revision="a71e4d38b236d968966a2002c4c895dbd12b1c3c",
        voices=_KOKORO_VOICES, languages=("a", "b", "e", "f", "h", "i", "p", "j", "z"),
        default_voice="af_heart", default_language="a", streaming="phrase", supports_speed=True,
    ),
    ModelSpec(
        id="soprano-1.1-80m", name="Soprano 1.1 · compact English", backend="soprano",
        repo_id="mlx-community/Soprano-1.1-80M-bf16", revision="745350c27f356c3910eebcb49e29760dcf6a643c",
        voices=("default",), languages=("en",), default_voice="default", default_language="en", streaming="phrase",
    ),
)


def _spec_from_dict(value: dict) -> ModelSpec:
    if not isinstance(value, dict):
        raise ValueError("Each registry model must be an object")
    fields = set(ModelSpec.__dataclass_fields__)
    if set(value) - fields:
        raise ValueError("Unknown model registry fields")
    values = dict(value)
    for key in ("voices", "languages"):
        if not isinstance(values.get(key), list):
            raise ValueError("Registry voices and languages must be arrays")
        values[key] = tuple(values[key])
    try:
        return ModelSpec(**values)
    except TypeError as exc:
        raise ValueError("Incomplete model registry entry") from exc


def load_registry(registry_file: Path | None = None) -> dict[str, ModelSpec]:
    """Built-ins plus explicit additions from an administrator-owned JSON file.

    File schema: {"version": 1, "models": [ModelSpec.to_dict(), ...]}. Existing
    IDs cannot be overridden; a changed checkpoint gets a distinct stable ID.
    """
    registry = {model.id: model for model in BUILTIN_MODELS}
    if registry_file is None:
        return registry
    if not isinstance(registry_file, Path):
        raise ValueError("The model registry must be a trusted local Path")
    file_error = "The model registry must be a bounded, administrator-owned, non-writable regular file"
    try:
        # A FIFO must not stall worker startup, and a symlink must not redirect
        # a trusted registry path to an unreviewed configuration file.
        descriptor = os.open(registry_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise ValueError(file_error) from exc
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        try:
            named = registry_file.stat(follow_symlinks=False)
        except OSError as exc:
            raise ValueError(file_error) from exc
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid not in (0, os.getuid())
                or metadata.st_mode & 0o022 or metadata.st_size > 1024 * 1024
                or (metadata.st_dev, metadata.st_ino) != (named.st_dev, named.st_ino)):
            raise ValueError(file_error)
        data = stream.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError(file_error)
        try:
            document = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("Invalid model registry JSON") from exc
    if (not isinstance(document, dict) or set(document) != {"version", "models"}
            or type(document["version"]) is not int or document["version"] != 1
            or not isinstance(document["models"], list) or len(document["models"]) > 64):
        raise ValueError("Unsupported model registry format")
    for value in document["models"]:
        model = _spec_from_dict(value)
        if model.id in registry:
            raise ValueError("Model registry IDs must be unique and cannot replace built-ins")
        registry[model.id] = model
    return registry


def get_models(registry_file: Path | None = None) -> tuple[ModelSpec, ...]:
    return tuple(load_registry(registry_file).values())


def get_model(model_id: str, registry_file: Path | None = None) -> ModelSpec:
    try:
        return load_registry(registry_file)[model_id]
    except (KeyError, TypeError):
        raise ValueError("Unknown registered text-to-speech model") from None


@dataclass(frozen=True)
class GenerationRequest:
    text: str
    voice: str | None = None
    language: str | None = None
    speed: float = 1.0
    instruction: str | None = None
    streaming_interval: float | None = None
    max_tokens: int = 512
    seed: int = 42
    temperature: float | None = None

    def validated(self, spec: ModelSpec) -> GenerationRequest:
        return validate_request(spec, self)

    def to_dict(self) -> dict:
        return asdict(self)


def validate_request(spec: ModelSpec, payload: GenerationRequest | Mapping) -> GenerationRequest:
    if isinstance(payload, GenerationRequest):
        request = payload
    elif isinstance(payload, Mapping):
        if set(payload) - set(GenerationRequest.__dataclass_fields__):
            raise ValueError("Unknown text-to-speech generation options")
        try:
            request = GenerationRequest(**payload)
        except TypeError as exc:
            raise ValueError("Speech requires text and supported generation options") from exc
    else:
        raise ValueError("Speech generation requires a validated request object")
    if (not isinstance(request.text, str) or not request.text.strip()
            or len(request.text) > MAX_TEXT_CHARS
            or any(ord(c) < 32 and c not in "\n\t\r" for c in request.text)):
        raise ValueError(f"Speech text must contain between 1 and {MAX_TEXT_CHARS} characters")
    voice = spec.default_voice if request.voice is None else request.voice
    language = spec.default_language if request.language is None else request.language
    if voice not in spec.voices or language not in spec.languages:
        raise ValueError("Voice and language must belong to the selected model")
    if not _number(request.speed, 0.5, 2.0):
        raise ValueError("Speaking rate must be between 0.5 and 2.0")
    if not spec.supports_speed and request.speed != 1.0:
        raise ValueError("The selected model does not support speaking-rate control")
    if request.instruction is not None:
        if not spec.supports_instruction:
            raise ValueError("The selected model does not support instruction control")
        if (not isinstance(request.instruction, str) or not request.instruction.strip()
                or len(request.instruction) > 500 or any(ord(c) < 32 for c in request.instruction)):
            raise ValueError("Voice instruction must be a bounded, nonempty string")
    interval = spec.default_streaming_interval if request.streaming_interval is None else request.streaming_interval
    if not _number(interval, 0.08, 1.0):
        raise ValueError("Streaming interval must be between 80 and 1000 milliseconds")
    if spec.streaming != "incremental" and request.streaming_interval is not None:
        raise ValueError("The selected model generates complete phrases and has no streaming interval")
    if type(request.max_tokens) is not int or not 32 <= request.max_tokens <= 750:
        raise ValueError("Speech token limit must be an integer between 32 and 750")
    if type(request.seed) is not int or not 0 <= request.seed <= 0xFFFFFFFF:
        raise ValueError("Speech sampling seed must be an unsigned 32-bit integer")
    temperature = 0.9 if request.temperature is None and spec.backend == "qwen3_custom_voice" else request.temperature
    if spec.backend == "qwen3_custom_voice" and not _number(temperature, 0.0, 1.0):
        raise ValueError("Speech sampling temperature must be between zero and one")
    if spec.backend != "qwen3_custom_voice" and temperature is not None:
        raise ValueError("Only the Qwen adapter supports the configured sampling temperature")
    return GenerationRequest(request.text, voice, language, float(request.speed), request.instruction,
                             float(interval) if spec.streaming == "incremental" else None, request.max_tokens,
                             request.seed, float(temperature) if temperature is not None else None)
