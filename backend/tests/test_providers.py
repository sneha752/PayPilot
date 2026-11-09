"""Tests for the provider abstraction and the Gemini provider's configuration
and response handling. No real Gemini API call is made — the client is faked.
"""

import pytest

from app.services.providers import (
    ClaudeProvider,
    GeminiProvider,
    get_provider,
)
from app.services.providers.base import (
    AIConfigError,
    AIServiceError,
    InvalidAIResponseError,
)
from app.services.providers import gemini as gemini_mod


# --- Factory / abstraction ---------------------------------------------------


def test_factory_defaults_to_gemini(monkeypatch):
    monkeypatch.delenv("AI_PROVIDER", raising=False)
    provider = get_provider()
    assert isinstance(provider, GeminiProvider)
    assert provider.name == "gemini"


def test_factory_selects_claude():
    provider = get_provider("claude")
    assert isinstance(provider, ClaudeProvider)
    assert provider.name == "claude"


def test_factory_respects_env(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "claude")
    assert isinstance(get_provider(), ClaudeProvider)


def test_factory_unknown_provider_raises():
    with pytest.raises(AIConfigError):
        get_provider("no-such-provider")


# --- Gemini configuration (no network) --------------------------------------


def test_gemini_missing_key_raises_config_error(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(AIConfigError):
        GeminiProvider().extract_intent("find me a laptop")


# --- Gemini response handling (faked client, no network) --------------------


class _FakeResponse:
    def __init__(self, text):
        self.text = text


class _FakeModels:
    def __init__(self, text=None, exc=None):
        self._text = text
        self._exc = exc

    def generate_content(self, **kwargs):
        if self._exc is not None:
            raise self._exc
        return _FakeResponse(self._text)


class _FakeGeminiClient:
    def __init__(self, text=None, exc=None):
        self.models = _FakeModels(text=text, exc=exc)


def _fake_client(monkeypatch, *, text=None, exc=None):
    monkeypatch.setattr(
        GeminiProvider, "_get_client", lambda self: _FakeGeminiClient(text=text, exc=exc)
    )


def test_gemini_parses_valid_json(monkeypatch):
    _fake_client(monkeypatch, text='{"category": "laptop", "quantity": 2}')
    data = GeminiProvider().extract_intent("two laptops")
    assert data == {"category": "laptop", "quantity": 2}


def test_gemini_malformed_json_raises_invalid(monkeypatch):
    _fake_client(monkeypatch, text="not valid json {")
    with pytest.raises(InvalidAIResponseError):
        GeminiProvider().extract_intent("find me a laptop")


def test_gemini_empty_response_raises_invalid(monkeypatch):
    _fake_client(monkeypatch, text=None)
    with pytest.raises(InvalidAIResponseError):
        GeminiProvider().extract_intent("find me a laptop")


def test_gemini_non_object_json_raises_invalid(monkeypatch):
    _fake_client(monkeypatch, text="[1, 2, 3]")
    with pytest.raises(InvalidAIResponseError):
        GeminiProvider().extract_intent("find me a laptop")


def test_gemini_api_failure_raises_service_error(monkeypatch):
    _fake_client(monkeypatch, exc=RuntimeError("boom"))
    with pytest.raises(AIServiceError):
        GeminiProvider().extract_intent("find me a laptop")
