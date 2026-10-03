"""Selectable text-to-speech generation; playback belongs to Shiri's room runtime.

Model metadata imports without MLX. The native model backend is loaded only in
the isolated generation worker on a supported host.
"""

from .models import GenerationRequest, ModelSpec, get_model, get_models, load_registry, validate_request

__all__ = ["GenerationRequest", "ModelSpec", "get_model", "get_models", "load_registry", "validate_request"]
