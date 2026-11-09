"""Tests for the Intent Agent and POST /ai/intent (provider-agnostic).

No real AI call is ever made: a FakeProvider returns canned payloads (or
raises), so the agent's Pydantic validation and the endpoint's error mapping
are exercised deterministically and for free. These tests do not touch PayPal
or the product catalog, and require no API key.
"""

from starlette.testclient import TestClient

from app.main import app
from app.agents import intent_agent
from app.services.providers.base import (
    AIConfigError,
    AIProvider,
    AIServiceError,
    InvalidAIResponseError,
)

client = TestClient(app, raise_server_exceptions=False)


class FakeProvider(AIProvider):
    """Returns a canned payload, or raises a given exception."""

    name = "fake"

    def __init__(self, payload=None, exc=None):
        self._payload = payload
        self._exc = exc

    def extract_intent(self, message: str) -> dict:
        if self._exc is not None:
            raise self._exc
        return self._payload


def _use_provider(monkeypatch, *, payload=None, exc=None):
    monkeypatch.setattr(
        intent_agent, "get_provider", lambda: FakeProvider(payload=payload, exc=exc)
    )


def _post(message: str):
    return client.post("/ai/intent", json={"message": message})


# --- Extraction scenarios (fake provider output, real validation) -----------


def test_basic_laptop_request(monkeypatch):
    _use_provider(monkeypatch, payload={"category": "laptop", "keywords": ["laptop"]})
    resp = _post("Find me a laptop")
    assert resp.status_code == 200
    body = resp.json()
    assert body["category"] == "laptop"
    assert body["quantity"] == 1              # default applied
    # authorization defaults applied when the model omits the block.
    assert body["authorization"]["purchase_requested"] is False
    assert body["authorization"]["autonomous_purchase_allowed"] is False


def test_max_price_extraction(monkeypatch):
    _use_provider(monkeypatch, payload={
        "category": "laptop", "hard_constraints": {"max_price_inr": 80000},
    })
    resp = _post("Find me a laptop under 80000")
    assert resp.status_code == 200
    assert resp.json()["hard_constraints"]["max_price_inr"] == 80000


def test_min_ram_extraction(monkeypatch):
    _use_provider(monkeypatch, payload={
        "category": "laptop", "hard_constraints": {"ram_gb_min": 16},
    })
    resp = _post("a laptop with at least 16GB RAM")
    assert resp.status_code == 200
    assert resp.json()["hard_constraints"]["ram_gb_min"] == 16


def test_preference_vs_hard_constraint(monkeypatch):
    _use_provider(monkeypatch, payload={
        "category": "laptop",
        "hard_constraints": {"max_price_inr": 80000, "ram_gb_min": 16},
        "preferences": {"gpu": "RTX"},
    })
    resp = _post("laptop under 80000 with 16GB RAM, prefer an RTX GPU")
    assert resp.status_code == 200
    body = resp.json()
    assert body["hard_constraints"]["max_price_inr"] == 80000
    assert body["hard_constraints"]["ram_gb_min"] == 16
    assert body["preferences"]["gpu"] == "RTX"
    assert body["hard_constraints"].get("gpu") is None


# --- Authorization vs shopping-budget separation ----------------------------


def test_example_a_pure_search(monkeypatch):
    # "Find me a laptop under 80000."
    _use_provider(monkeypatch, payload={
        "category": "laptop",
        "hard_constraints": {"max_price_inr": 80000},
        "authorization": {
            "purchase_requested": False,
            "autonomous_purchase_allowed": False,
            "auto_approve_below_inr": None,
        },
    })
    body = _post("Find me a laptop under 80000").json()
    assert body["hard_constraints"]["max_price_inr"] == 80000
    assert body["authorization"]["purchase_requested"] is False
    assert body["authorization"]["autonomous_purchase_allowed"] is False
    assert body["authorization"]["auto_approve_below_inr"] is None


def test_example_b_find_and_buy(monkeypatch):
    # "Find and buy me a laptop under 80000."
    _use_provider(monkeypatch, payload={
        "category": "laptop",
        "hard_constraints": {"max_price_inr": 80000},
        "authorization": {
            "purchase_requested": True,
            "autonomous_purchase_allowed": False,
            "auto_approve_below_inr": None,
        },
    })
    body = _post("Find and buy me a laptop under 80000").json()
    assert body["authorization"]["purchase_requested"] is True
    assert body["authorization"]["autonomous_purchase_allowed"] is False
    assert body["authorization"]["auto_approve_below_inr"] is None


def test_example_c_conditional_autonomous(monkeypatch):
    # "...under 80000. You may buy it automatically if it costs below 50000."
    _use_provider(monkeypatch, payload={
        "category": "laptop",
        "hard_constraints": {"max_price_inr": 80000},
        "authorization": {
            "purchase_requested": True,
            "autonomous_purchase_allowed": True,
            "auto_approve_below_inr": 50000,
        },
    })
    body = _post("laptop under 80000, buy automatically if below 50000").json()
    # The two numbers stay in their own fields.
    assert body["hard_constraints"]["max_price_inr"] == 80000
    assert body["authorization"]["auto_approve_below_inr"] == 50000
    assert body["authorization"]["purchase_requested"] is True
    assert body["authorization"]["autonomous_purchase_allowed"] is True


def test_example_d_buy_but_ask_above_threshold(monkeypatch):
    # "Buy it, but ask me before spending more than 50000."
    _use_provider(monkeypatch, payload={
        "category": "laptop",
        "authorization": {
            "purchase_requested": True,
            "autonomous_purchase_allowed": True,
            "auto_approve_below_inr": 50000,
        },
    })
    body = _post("Buy it, but ask me before spending more than 50000").json()
    assert body["authorization"]["purchase_requested"] is True
    assert body["authorization"]["autonomous_purchase_allowed"] is True
    assert body["authorization"]["auto_approve_below_inr"] == 50000


def test_threshold_never_overwrites_max_price(monkeypatch):
    # The original ML-laptop case: 80000 is the budget, 50000 is authorization.
    _use_provider(monkeypatch, payload={
        "category": "laptop",
        "use_case": "machine learning",
        "hard_constraints": {"max_price_inr": 80000, "ram_gb_min": 16},
        "preferences": {"gpu": "RTX"},
        "authorization": {
            "purchase_requested": True,
            "autonomous_purchase_allowed": True,
            "auto_approve_below_inr": 50000,
        },
    })
    body = _post(
        "laptop under 80000 with 16GB RAM, prefer RTX, auto-buy below 50000"
    ).json()
    assert body["hard_constraints"]["max_price_inr"] == 80000  # NOT 50000
    assert body["authorization"]["auto_approve_below_inr"] == 50000
    # Belt-and-suspenders: the threshold must not have leaked into the budget.
    assert body["hard_constraints"]["max_price_inr"] != 50000


def test_simple_search_creates_no_authorization(monkeypatch):
    # No buy language at all -> authorization stays at safe defaults.
    _use_provider(monkeypatch, payload={"category": "laptop"})
    body = _post("show me some laptops").json()
    assert body["authorization"]["purchase_requested"] is False
    assert body["authorization"]["autonomous_purchase_allowed"] is False
    assert body["authorization"]["auto_approve_below_inr"] is None


# --- Malformed / empty input -------------------------------------------------


def test_empty_message_is_rejected():
    assert _post("").status_code == 422


def test_missing_message_field_is_rejected():
    assert client.post("/ai/intent", json={}).status_code == 422


# --- Invalid AI output -> 502 ------------------------------------------------


def test_invalid_ai_output_returns_502(monkeypatch):
    # quantity=0 violates ge=1 -> validation fails.
    _use_provider(monkeypatch, payload={"category": "laptop", "quantity": 0})
    assert _post("Find me a laptop").status_code == 502


def test_no_structured_output_returns_502(monkeypatch):
    _use_provider(monkeypatch, exc=InvalidAIResponseError("no content"))
    assert _post("Find me a laptop").status_code == 502


# --- Provider/API failure -> 502 --------------------------------------------


def test_provider_api_failure_returns_502(monkeypatch):
    _use_provider(monkeypatch, exc=AIServiceError("provider API request failed"))
    assert _post("Find me a laptop").status_code == 502


# --- Missing API key -> 503 --------------------------------------------------


def test_missing_key_returns_503_via_default_provider(monkeypatch):
    # No patching of get_provider: use the real default (gemini) with no key.
    monkeypatch.delenv("AI_PROVIDER", raising=False)   # default -> gemini
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert _post("Find me a laptop").status_code == 503


def test_config_error_maps_to_503(monkeypatch):
    _use_provider(monkeypatch, exc=AIConfigError("GEMINI_API_KEY is not set"))
    assert _post("Find me a laptop").status_code == 503
