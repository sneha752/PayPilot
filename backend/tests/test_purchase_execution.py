"""Tests for the policy-gated execution service and POST /purchase/execute.

PayPal is mocked at the service boundary (create_order is replaced with a
recording fake), so no real PayPal call is ever made. The catalog, the
Policy Engine, and the currency service all run for real — they are
deterministic. No AI provider is involved anywhere in this path.
"""

import json

import pytest
from starlette.testclient import TestClient

from app.main import app
from app.services import purchase_execution_service

client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def paypal_spy(monkeypatch):
    """Replace paypal.create_order at the service boundary; records calls."""
    calls: list[dict] = []

    def fake_create_order(amount, currency="USD", description=None, request_id=None):
        calls.append({"amount": amount, "currency": currency, "description": description})
        return {
            "order_id": "FAKE-ORDER-1",
            "status": "CREATED",
            "approve_url": "https://www.sandbox.paypal.com/checkoutnow?token=FAKE-ORDER-1",
        }

    monkeypatch.setattr(purchase_execution_service, "create_order", fake_create_order)
    return calls


def _post(body: dict):
    return client.post("/purchase/execute", json=body)


AUTONOMOUS = {
    "purchase_requested": True,
    "autonomous_purchase_allowed": True,
    "auto_approve_below_inr": 50000,
}


# --- 1 & 10. ALLOW_AUTONOMOUS_PURCHASE -> PayPal called once -------------------


def test_allow_creates_order_with_catalog_price(paypal_spy):
    # TAB003 costs ₹44,900 — genuinely below the ₹50,000 autonomy limit.
    resp = _post({"product_id": "TAB003", "authorization": AUTONOMOUS})
    assert resp.status_code == 200
    body = resp.json()

    assert body["status"] == "order_created"
    assert body["policy_decision"]["decision"] == "ALLOW_AUTONOMOUS_PURCHASE"
    assert body["catalog_price_inr"] == 44900          # authoritative price
    assert body["paypal_amount"] == "498.89"           # 44900 / 90, half-up
    assert body["paypal_currency"] == "USD"
    assert body["demo_fx_rate"] == "90"
    assert body["paypal_order_id"] == "FAKE-ORDER-1"
    assert body["approve_url"].startswith("https://www.sandbox.paypal.com")

    # PayPal called exactly once, with the catalog-derived USD amount.
    assert len(paypal_spy) == 1
    assert paypal_spy[0]["amount"] == "498.89"
    assert paypal_spy[0]["currency"] == "USD"
    assert "TAB003" in paypal_spy[0]["description"]


def test_allow_within_higher_limit(paypal_spy):
    # LAP006 (₹52,990) with a ₹60,000 limit: 52990 / 90 = 588.777... -> 588.78.
    resp = _post({
        "product_id": "LAP006",
        "authorization": {**AUTONOMOUS, "auto_approve_below_inr": 60000},
        "hard_constraints": {"max_price_inr": 80000},
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "order_created"
    assert paypal_spy[0]["amount"] == "588.78"


# --- 2 & 11 & security scenario. REQUIRE_USER_APPROVAL -> no PayPal ------------


def test_above_autonomy_limit_requires_approval_and_skips_paypal(paypal_spy):
    # THE security scenario: shopping max ₹80,000, autonomy limit ₹50,000,
    # LAP002 costs ₹68,990 -> approval required, PayPal untouched.
    resp = _post({
        "product_id": "LAP002",
        "authorization": AUTONOMOUS,
        "hard_constraints": {"max_price_inr": 80000},
    })
    assert resp.status_code == 200
    body = resp.json()

    assert body["status"] == "approval_required"
    assert body["policy_decision"]["decision"] == "REQUIRE_USER_APPROVAL"
    assert body["policy_decision"]["reason_code"] == "EXCEEDS_AUTONOMOUS_LIMIT"
    assert body["paypal_order_id"] is None
    assert body["approve_url"] is None
    assert body["paypal_amount"] is None
    assert paypal_spy == []                            # PayPal NEVER called


def test_autonomy_not_granted_requires_approval(paypal_spy):
    resp = _post({
        "product_id": "TAB003",
        "authorization": {"purchase_requested": True, "autonomous_purchase_allowed": False},
    })
    body = resp.json()
    assert body["status"] == "approval_required"
    assert body["policy_decision"]["reason_code"] == "AUTONOMY_NOT_GRANTED"
    assert body["paypal_order_id"] is None
    assert paypal_spy == []


# --- 3 & 9. DENY -> no PayPal ---------------------------------------------------


def test_no_purchase_requested_denies_and_skips_paypal(paypal_spy):
    resp = _post({
        "product_id": "TAB003",
        "authorization": {"purchase_requested": False},
    })
    body = resp.json()
    assert body["status"] == "denied"
    assert body["policy_decision"]["reason_code"] == "NO_PURCHASE_REQUESTED"
    assert body["paypal_order_id"] is None
    assert paypal_spy == []


def test_above_shopping_max_denies_and_skips_paypal(paypal_spy):
    # LAP001 costs ₹124,990, over the ₹80,000 shopping max.
    resp = _post({
        "product_id": "LAP001",
        "authorization": {**AUTONOMOUS, "auto_approve_below_inr": 200000},
        "hard_constraints": {"max_price_inr": 80000},
    })
    body = resp.json()
    assert body["status"] == "denied"
    assert body["policy_decision"]["reason_code"] == "EXCEEDS_SHOPPING_MAX"
    assert body["paypal_order_id"] is None
    assert paypal_spy == []


# --- 4/5/6. Client tampering is inert --------------------------------------------


def test_client_price_tampering_is_ignored(paypal_spy):
    # Client claims the product costs ₹1 — the catalog price must win.
    resp = _post({
        "product_id": "TAB003",
        "price_inr": 1,
        "catalog_price_inr": 1,
        "authorization": AUTONOMOUS,
    })
    body = resp.json()
    assert body["catalog_price_inr"] == 44900
    assert paypal_spy[0]["amount"] == "498.89"         # from ₹44,900, not ₹1


def test_client_decision_tampering_is_ignored(paypal_spy):
    # Client asserts ALLOW for a product above the autonomy limit; the server
    # re-evaluates and escalates instead.
    resp = _post({
        "product_id": "LAP002",
        "decision": "ALLOW_AUTONOMOUS_PURCHASE",
        "policy_decision": {"decision": "ALLOW_AUTONOMOUS_PURCHASE"},
        "authorization": AUTONOMOUS,
        "hard_constraints": {"max_price_inr": 80000},
    })
    body = resp.json()
    assert body["status"] == "approval_required"
    assert body["paypal_order_id"] is None
    assert paypal_spy == []


def test_client_paypal_amount_tampering_is_ignored(paypal_spy):
    resp = _post({
        "product_id": "TAB003",
        "paypal_amount": "0.01",
        "amount": "0.01",
        "currency": "USD",
        "authorization": AUTONOMOUS,
    })
    assert resp.json()["paypal_amount"] == "498.89"
    assert paypal_spy[0]["amount"] == "498.89"         # never "0.01"


# --- 7. Unknown product -----------------------------------------------------------


def test_unknown_product_404_no_paypal(paypal_spy):
    resp = _post({"product_id": "LAP999", "authorization": AUTONOMOUS})
    assert resp.status_code == 404
    assert paypal_spy == []


# --- 8. Unsupported catalog currency ------------------------------------------------


def test_non_inr_catalog_currency_denies(paypal_spy, monkeypatch):
    # Simulate a future non-INR catalog: the engine's currency rule must
    # block execution, since no conversion for it exists in this phase.
    monkeypatch.setattr(purchase_execution_service, "CATALOG_CURRENCY", "EUR")
    resp = _post({"product_id": "TAB003", "authorization": AUTONOMOUS})
    body = resp.json()
    assert body["status"] == "denied"
    assert body["policy_decision"]["reason_code"] == "UNSUPPORTED_CURRENCY"
    assert paypal_spy == []


# --- Determinism / isolation ----------------------------------------------------------


def test_execution_needs_no_ai_provider(paypal_spy, monkeypatch):
    # The execution path is fully deterministic: it works with no AI key at
    # all, proving no Gemini call hides in it.
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("AI_PROVIDER", raising=False)
    resp = _post({"product_id": "TAB003", "authorization": AUTONOMOUS})
    assert resp.status_code == 200
    assert resp.json()["status"] == "order_created"


def test_response_contains_no_credentials(paypal_spy):
    resp = _post({"product_id": "TAB003", "authorization": AUTONOMOUS})
    text = json.dumps(resp.json()).lower()
    for secret in ("client_secret", "access_token", "bearer", "client_id"):
        assert secret not in text


def test_missing_authorization_rejected(paypal_spy):
    assert _post({"product_id": "TAB003"}).status_code == 422
    assert paypal_spy == []
