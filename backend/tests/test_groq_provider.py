"""Tests for the Groq provider (Phase 8F): configuration, structured-output
request shape, and response handling. No real Groq API call is made — the
single HTTP seam (GroqProvider._post) is faked.
"""

import httpx
import pytest

from app.services.providers import GroqProvider, get_provider
from app.services.providers.base import (
    AIConfigError,
    AIServiceError,
    InvalidAIResponseError,
)
from app.services.providers.groq import _DEFAULT_MODEL, _strict_schema


# --- Factory / registry ------------------------------------------------------


def test_factory_selects_groq():
    provider = get_provider("groq")
    assert isinstance(provider, GroqProvider)
    assert provider.name == "groq"


def test_factory_respects_env(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "groq")
    assert isinstance(get_provider(), GroqProvider)


# --- Configuration (no network) ----------------------------------------------


def test_groq_missing_key_raises_config_error(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(AIConfigError):
        GroqProvider().extract_intent("find me a laptop")


# --- Faked HTTP seam ----------------------------------------------------------


class _FakeHttpResponse:
    def __init__(self, status_code=200, body=None, invalid_json=False):
        self.status_code = status_code
        self._body = body
        self._invalid_json = invalid_json

    def json(self):
        if self._invalid_json:
            raise ValueError("not json")
        return self._body


def _completion_body(content):
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def _fake_post(monkeypatch, *, response=None, exc=None, captured=None):
    """Replace GroqProvider._post; optionally capture the request payload."""
    monkeypatch.setenv("GROQ_API_KEY", "test-key")

    def fake(self, payload, api_key):
        if captured is not None:
            captured.append(payload)
        if exc is not None:
            raise exc
        return response

    monkeypatch.setattr(GroqProvider, "_post", fake)


# --- Successful calls ----------------------------------------------------------


def test_groq_route_classification_parses_valid_json(monkeypatch):
    _fake_post(
        monkeypatch,
        response=_FakeHttpResponse(
            body=_completion_body('{"route": "SHOP", "summary": "wants laptop"}')
        ),
    )
    data = GroqProvider().classify_route("find me a laptop")
    assert data == {"route": "SHOP", "summary": "wants laptop"}


def test_groq_intent_extraction_parses_valid_json(monkeypatch):
    _fake_post(
        monkeypatch,
        response=_FakeHttpResponse(
            body=_completion_body('{"category": "laptop", "quantity": 2}')
        ),
    )
    data = GroqProvider().extract_intent("two laptops")
    assert data == {"category": "laptop", "quantity": 2}


def test_groq_recommendation_parses_valid_json(monkeypatch):
    captured = []
    _fake_post(
        monkeypatch,
        response=_FakeHttpResponse(
            body=_completion_body(
                '{"recommended_product_id": "LAP001", "reasoning": "best fit"}'
            )
        ),
        captured=captured,
    )
    intent = {"category": "laptop"}
    candidates = [{"id": "LAP001", "name": "Dell XPS 13"}]
    data = GroqProvider().recommend_product(intent, candidates)
    assert data["recommended_product_id"] == "LAP001"
    # Intent and candidates are serialized verbatim into the user content.
    user_content = captured[0]["messages"][1]["content"]
    assert '"LAP001"' in user_content
    assert '"category": "laptop"' in user_content


# --- Request shape -------------------------------------------------------------


def test_groq_uses_default_model_and_strict_schema(monkeypatch):
    captured = []
    _fake_post(
        monkeypatch,
        response=_FakeHttpResponse(body=_completion_body('{"route": "SMALLTALK"}')),
        captured=captured,
    )
    monkeypatch.delenv("GROQ_MODEL", raising=False)
    GroqProvider().classify_route("hi")

    payload = captured[0]
    assert payload["model"] == _DEFAULT_MODEL
    rf = payload["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "RouteResult"
    assert rf["json_schema"]["strict"] is True
    schema = rf["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"].keys())


def test_groq_model_env_override(monkeypatch):
    captured = []
    _fake_post(
        monkeypatch,
        response=_FakeHttpResponse(body=_completion_body('{"route": "SHOP"}')),
        captured=captured,
    )
    monkeypatch.setenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    GroqProvider().classify_route("find me a laptop")
    assert captured[0]["model"] == "llama-3.3-70b-versatile"


def test_strict_schema_closes_nested_objects_and_strips_metadata():
    schema = _strict_schema(
        {
            "type": "object",
            "title": "Outer",
            "properties": {
                # A property NAMED "title" must survive the keyword strip.
                "title": {"type": "string", "default": "x", "maxLength": 5},
                "inner": {
                    "type": "object",
                    "properties": {"n": {"type": "integer", "minimum": 1}},
                },
            },
        }
    )
    assert "title" in schema["properties"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"title", "inner"}
    assert "default" not in schema["properties"]["title"]
    assert "maxLength" not in schema["properties"]["title"]
    inner = schema["properties"]["inner"]
    assert inner["additionalProperties"] is False
    assert inner["required"] == ["n"]
    assert "minimum" not in inner["properties"]["n"]


# --- Failure handling ------------------------------------------------------------


def test_groq_network_failure_raises_service_error(monkeypatch):
    _fake_post(monkeypatch, exc=httpx.ConnectError("boom"))
    with pytest.raises(AIServiceError):
        GroqProvider().classify_route("hi")


def test_groq_http_error_status_raises_service_error(monkeypatch):
    _fake_post(monkeypatch, response=_FakeHttpResponse(status_code=429, body={}))
    with pytest.raises(AIServiceError):
        GroqProvider().extract_intent("find me a laptop")


def test_groq_unexpected_shape_raises_invalid(monkeypatch):
    _fake_post(monkeypatch, response=_FakeHttpResponse(body={"unexpected": True}))
    with pytest.raises(InvalidAIResponseError):
        GroqProvider().classify_route("hi")


def test_groq_non_json_body_raises_invalid(monkeypatch):
    _fake_post(monkeypatch, response=_FakeHttpResponse(invalid_json=True))
    with pytest.raises(InvalidAIResponseError):
        GroqProvider().classify_route("hi")


def test_groq_empty_content_raises_invalid(monkeypatch):
    _fake_post(monkeypatch, response=_FakeHttpResponse(body=_completion_body("")))
    with pytest.raises(InvalidAIResponseError):
        GroqProvider().classify_route("hi")


def test_groq_malformed_content_json_raises_invalid(monkeypatch):
    _fake_post(
        monkeypatch, response=_FakeHttpResponse(body=_completion_body("not json {"))
    )
    with pytest.raises(InvalidAIResponseError):
        GroqProvider().extract_intent("find me a laptop")


def test_groq_non_object_content_raises_invalid(monkeypatch):
    _fake_post(monkeypatch, response=_FakeHttpResponse(body=_completion_body("[1, 2]")))
    with pytest.raises(InvalidAIResponseError):
        GroqProvider().extract_intent("find me a laptop")
