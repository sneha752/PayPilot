"""Provider-agnostic AI layer for PayPilot.

The Intent Agent depends on the AIProvider interface, not on any single vendor
SDK. A factory selects the concrete provider from the AI_PROVIDER env var
(default: gemini), so Gemini and Claude are interchangeable.
"""

from __future__ import annotations

import os

from .base import (
    AIConfigError,
    AIProvider,
    AIProviderError,
    AIServiceError,
    InvalidAIResponseError,
)
from .claude import ClaudeProvider
from .gemini import GeminiProvider
from .groq import GroqProvider

# Registry of available providers by name.
_PROVIDERS: dict[str, type[AIProvider]] = {
    "gemini": GeminiProvider,
    "claude": ClaudeProvider,
    "groq": GroqProvider,
}

_DEFAULT_PROVIDER = "gemini"


def get_provider(name: str | None = None) -> AIProvider:
    """Return a provider instance.

    Args:
        name: Explicit provider name; defaults to the AI_PROVIDER env var,
            then to "gemini".

    Raises:
        AIConfigError: if the requested provider name is unknown.
    """
    key = (name or os.getenv("AI_PROVIDER", _DEFAULT_PROVIDER)).strip().lower()
    provider_cls = _PROVIDERS.get(key)
    if provider_cls is None:
        raise AIConfigError(
            f"Unknown AI provider '{key}'. Valid options: {sorted(_PROVIDERS)}"
        )
    return provider_cls()


__all__ = [
    "AIProvider",
    "AIProviderError",
    "AIConfigError",
    "AIServiceError",
    "InvalidAIResponseError",
    "GeminiProvider",
    "ClaudeProvider",
    "GroqProvider",
    "get_provider",
]
