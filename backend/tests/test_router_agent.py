"""Tests for the Router Agent (Phase 8E Step 2 — provider fully mocked).

Covers classification into the four routes, structured-output validation,
rejection of unsupported/invented routes and malformed payloads, safe
failure on provider errors (never a guessed or defaulted route), and
proof that routing performs no PayPal call, no Policy Engine evaluation,
no refund, and no purchase. No real Gemini API calls.
"""

import pytest

from app.agents.router_agent import RouteValidationError, route_message
from app.schemas.routing import ChatRoute, RouteResult
from app.services.providers import (
    AIConfigError,
    AIProvider,
    AIServiceError,
    InvalidAIResponseError,
)


class FakeProvider(AIProvider):
    """Canned provider: returns a fixed payload or raises a fixed error."""

    name = "fake"

    def __init__(self, payload=None, error: Exception | None = None):
        self._payload = payload
        self._error = error
        self.calls: list[str] = []

    def extract_intent(self, message: str) -> dict:  # pragma: no cover
        raise AssertionError("Router must not use extract_intent")

    def classify_route(self, message: str) -> dict:
        self.calls.append(message)
        if self._error is not None:
            raise self._error
        return self._payload


@pytest.fixture(autouse=True)
def _routing_is_classification_only(monkeypatch):
    """Tests 9-12: routing must never reach PayPal, the Policy Engine,
    refund execution, or purchase execution."""
    import httpx

    from app.policy import policy_engine
    from app.services import purchase_execution_service, refund_service

    def _forbidden(*args, **kwargs):  # pragma: no cover - fails the test if hit
        raise AssertionError("Routing must not execute money/state actions")

    monkeypatch.setattr(httpx, "post", _forbidden)
    monkeypatch.setattr(httpx, "get", _forbidden)
    monkeypatch.setattr(policy_engine, "evaluate_policy", _forbidden)
    monkeypatch.setattr(refund_service, "refund_transaction", _forbidden)
    monkeypatch.setattr(purchase_execution_service, "execute_purchase", _forbidden)
    monkeypatch.setattr(purchase_execution_service, "approve_purchase", _forbidden)
    monkeypatch.setattr(purchase_execution_service, "capture_purchase", _forbidden)


# --- 1-4. The four routes ------------------------------------------------------


@pytest.mark.parametrize(
    "message,route",
    [
        ("Find me a good laptop under ₹80,000", "SHOP"),
        ("I want cheap headphones", "SHOP"),
        ("Buy me the best monitor for coding", "SHOP"),
        ("I want a refund", "REFUND"),
        ("The laptop arrived damaged, I want my money back", "REFUND"),
        ("Can I return that purchase?", "REFUND"),
        ("Show me my purchases", "HISTORY"),
        ("What's the status of my order?", "HISTORY"),
        ("Hi", "SMALLTALK"),
        ("What can you do?", "SMALLTALK"),
    ],
)
def test_messages_route_to_expected_flow(message, route):
    provider = FakeProvider(payload={"route": route, "summary": "test"})
    result = route_message(message, provider=provider)

    assert isinstance(result, RouteResult)
    assert result.route == ChatRoute(route)
    assert provider.calls == [message]


# --- 5. Structured output validation -----------------------------------------------


def test_result_is_validated_structured_output():
    provider = FakeProvider(
        payload={"route": "SHOP", "summary": "User wants headphones."}
    )
    result = route_message("I want cheap headphones", provider=provider)

    assert result.route is ChatRoute.SHOP
    assert result.summary == "User wants headphones."


def test_summary_is_optional():
    provider = FakeProvider(payload={"route": "HISTORY"})
    result = route_message("what have I bought?", provider=provider)
    assert result.route is ChatRoute.HISTORY
    assert result.summary is None


def test_unknown_payload_fields_never_surface():
    """Fields the schema doesn't define (price, ids, decisions) are dropped."""
    provider = FakeProvider(
        payload={
            "route": "SHOP",
            "amount": "999999",
            "currency": "USD",
            "capture_id": "CAP-ATTACK",
            "policy_decision": "ALLOW_AUTONOMOUS_PURCHASE",
        }
    )
    result = route_message("buy stuff", provider=provider)

    assert result.model_dump() == {"route": ChatRoute.SHOP, "summary": None}


# --- 6/7. Unsupported route / malformed output rejected -----------------------------


@pytest.mark.parametrize(
    "bad_payload",
    [
        {"route": "PURCHASE_NOW"},          # invented fifth route
        {"route": "shop"},                   # wrong case is not a valid enum
        {"route": ""},
        {"summary": "no route at all"},      # missing route
        {"route": None},
        {"route": 42},
        {"route": "SHOP", "summary": "x" * 500},  # over max_length
    ],
)
def test_invalid_model_output_rejected(bad_payload):
    provider = FakeProvider(payload=bad_payload)
    with pytest.raises(RouteValidationError):
        route_message("hello", provider=provider)


# --- 8. Provider failure handled safely (no guessed route) ---------------------------


@pytest.mark.parametrize(
    "error",
    [
        AIConfigError("GEMINI_API_KEY is not set in the environment"),
        AIServiceError("Gemini API request failed"),
        InvalidAIResponseError("Gemini returned non-JSON content"),
    ],
)
def test_provider_failures_propagate_without_fallback_route(error):
    provider = FakeProvider(error=error)
    with pytest.raises(type(error)):
        route_message("I want a refund", provider=provider)
    # Nothing was swallowed into a default SHOP/REFUND result — the call
    # raised; there is no result object at all.


# --- Gemini provider plumbing (no SDK, no key, no network) ---------------------------


def test_gemini_classify_route_uses_shared_structured_call(monkeypatch):
    from app.services.providers import gemini as gemini_module
    from app.services.providers.base import ROUTING_SYSTEM_PROMPT

    captured = {}

    def fake_generate(self, system_prompt, contents, response_schema, task):
        captured.update(
            prompt=system_prompt,
            contents=contents,
            schema=response_schema,
            task=task,
        )
        return {"route": "SMALLTALK"}

    monkeypatch.setattr(
        gemini_module.GeminiProvider, "_generate_structured", fake_generate
    )

    payload = gemini_module.GeminiProvider().classify_route("hello there")

    assert payload == {"route": "SMALLTALK"}
    assert captured["prompt"] == ROUTING_SYSTEM_PROMPT
    assert captured["contents"] == "hello there"
    assert captured["schema"] is RouteResult
    assert captured["task"] == "message routing"


def test_provider_without_routing_support_raises_not_implemented():
    class MinimalProvider(AIProvider):
        name = "minimal"

        def extract_intent(self, message: str) -> dict:  # pragma: no cover
            return {}

    with pytest.raises(NotImplementedError):
        route_message("hi", provider=MinimalProvider())
