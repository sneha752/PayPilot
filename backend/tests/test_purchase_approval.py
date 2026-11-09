"""Tests for the explicit-approval flow (POST /purchase/approve).

PayPal is mocked at the service boundary (shared recording fake), so no
real PayPal call is made. The catalog, Policy Engine, and currency service
run for real. Core invariant under test: explicit approval satisfies
REQUIRE_USER_APPROVAL but can NEVER turn a DENY into a purchase.
"""

import json

import pytest
from starlette.testclient import TestClient

from app.main import app
from app.services import purchase_execution_service
from app.services.product_service import get_product

client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def paypal_spy(monkeypatch):
    """Replace paypal.create_order at the service boundary; records calls."""
    calls: list[dict] = []

    def fake_create_order(amount, currency="USD", description=None, request_id=None):
        calls.append({"amount": amount, "currency": currency, "description": description})
        return {
            "order_id": "FAKE-ORDER-6C",
            "status": "CREATED",
            "approve_url": "https://www.sandbox.paypal.com/checkoutnow?token=FAKE-ORDER-6C",
        }

    monkeypatch.setattr(purchase_execution_service, "create_order", fake_create_order)
    return calls


def _approve(body: dict):
    return client.post("/purchase/approve", json=body)


def _execute(body: dict):
    return client.post("/purchase/execute", json=body)


# The canonical Phase 6 policy context: budget 80,000, autonomy 50,000.
AUTONOMOUS_50K = {
    "purchase_requested": True,
    "autonomous_purchase_allowed": True,
    "auto_approve_below_inr": 50000,
}
LAP002_REQUEST = {
    "product_id": "LAP002",  # ₹68,990
    "authorization": AUTONOMOUS_50K,
    "hard_constraints": {"max_price_inr": 80000},
}


# --- 1, 12, 13 & Task 6. The full recheck scenario ---------------------------


def test_execute_then_approve_full_scenario(paypal_spy):
    # Step 1: autonomous execution escalates — no PayPal order yet.
    resp = _execute(LAP002_REQUEST)
    assert resp.status_code == 200
    first = resp.json()
    assert first["status"] == "approval_required"
    assert first["policy_decision"]["reason_code"] == "EXCEEDS_AUTONOMOUS_LIMIT"
    assert first["paypal_order_id"] is None
    assert paypal_spy == []                       # nothing created yet

    # Step 2: the user explicitly approves -> policy re-runs, shopping
    # constraint still passes, approval satisfies the escalation.
    resp = _approve(LAP002_REQUEST)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "order_created"
    assert body["approval_used"] is True
    assert body["catalog_price_inr"] == 68990
    assert body["paypal_amount"] == "766.56"      # ₹68,990 / 90, half-up
    assert body["paypal_currency"] == "USD"
    assert body["demo_fx_rate"] == "90"
    assert body["paypal_order_id"] == "FAKE-ORDER-6C"

    # PayPal called exactly once across the whole sequence, with the
    # catalog-derived amount.
    assert len(paypal_spy) == 1
    assert paypal_spy[0]["amount"] == "766.56"
    assert paypal_spy[0]["currency"] == "USD"


# --- 2. DENY -> approval stays denied ------------------------------------------


def test_approval_cannot_override_no_purchase_requested(paypal_spy):
    resp = _approve({
        "product_id": "LAP002",
        "authorization": {"purchase_requested": False},
    })
    body = resp.json()
    assert body["status"] == "denied"
    assert body["approval_used"] is True
    assert body["policy_decision"]["reason_code"] == "NO_PURCHASE_REQUESTED"
    assert body["paypal_order_id"] is None
    assert paypal_spy == []


# --- 3. Shopping max cannot be bypassed by approval ------------------------------


def test_approval_cannot_bypass_shopping_max(paypal_spy):
    resp = _approve({
        "product_id": "LAP001",  # ₹124,990
        "authorization": AUTONOMOUS_50K,
        "hard_constraints": {"max_price_inr": 80000},
    })
    body = resp.json()
    assert body["status"] == "denied"
    assert body["policy_decision"]["reason_code"] == "EXCEEDS_SHOPPING_MAX"
    assert body["paypal_order_id"] is None
    assert paypal_spy == []


# --- 4. Unknown product -----------------------------------------------------------


def test_unknown_product_404_no_paypal(paypal_spy):
    resp = _approve({"product_id": "LAP999", "authorization": AUTONOMOUS_50K})
    assert resp.status_code == 404
    assert paypal_spy == []


# --- 5. Unsupported currency --------------------------------------------------------


def test_non_inr_catalog_currency_denies_approval(paypal_spy, monkeypatch):
    monkeypatch.setattr(purchase_execution_service, "CATALOG_CURRENCY", "EUR")
    body = _approve(LAP002_REQUEST).json()
    assert body["status"] == "denied"
    assert body["policy_decision"]["reason_code"] == "UNSUPPORTED_CURRENCY"
    assert paypal_spy == []


# --- 6/7/8/9. Client tampering is inert on the approval path -------------------------


def test_price_tampering_ignored_on_approval(paypal_spy):
    body = _approve({**LAP002_REQUEST, "price_inr": 1, "catalog_price_inr": 1}).json()
    assert body["catalog_price_inr"] == 68990
    assert paypal_spy[0]["amount"] == "766.56"    # derived from the catalog


def test_paypal_amount_tampering_ignored_on_approval(paypal_spy):
    body = _approve({
        **LAP002_REQUEST,
        "paypal_amount": "0.01",
        "amount": "0.01",
        "currency": "USD",
    }).json()
    assert body["paypal_amount"] == "766.56"
    assert paypal_spy[0]["amount"] == "766.56"


def test_fake_allow_decision_cannot_force_denied_purchase(paypal_spy):
    # A fake ALLOW decision on a request that policy DENIES must stay denied.
    body = _approve({
        "product_id": "LAP001",
        "decision": "ALLOW_AUTONOMOUS_PURCHASE",
        "policy_decision": {"decision": "ALLOW_AUTONOMOUS_PURCHASE"},
        "paypal_order_id": "INJECTED",
        "authorization": AUTONOMOUS_50K,
        "hard_constraints": {"max_price_inr": 80000},
    }).json()
    assert body["status"] == "denied"
    assert body["paypal_order_id"] is None        # the injected id never surfaces
    assert paypal_spy == []


# --- 10. Price changed between planning and approval ----------------------------------


def test_price_increase_after_planning_is_caught_on_approval(paypal_spy, monkeypatch):
    # At planning time LAP002 cost ₹68,990 (within the ₹80,000 budget). By
    # approval time the price rose to ₹85,000 — the re-fetch + policy re-run
    # must DENY and PayPal must not be called.
    repriced = get_product("LAP002").model_copy(update={"price_inr": 85000})
    monkeypatch.setattr(
        purchase_execution_service, "get_product", lambda pid: repriced
    )
    body = _approve(LAP002_REQUEST).json()
    assert body["status"] == "denied"
    assert body["policy_decision"]["reason_code"] == "EXCEEDS_SHOPPING_MAX"
    assert body["catalog_price_inr"] == 85000     # decided on the CURRENT price
    assert body["paypal_order_id"] is None
    assert paypal_spy == []


# --- 11. Autonomous path unchanged ------------------------------------------------------


def test_autonomous_execute_still_works_without_approval(paypal_spy):
    body = _execute({"product_id": "TAB003", "authorization": AUTONOMOUS_50K}).json()
    assert body["status"] == "order_created"
    assert body["approval_used"] is False         # no approval was needed
    assert body["paypal_amount"] == "498.89"
    assert len(paypal_spy) == 1


def test_approving_an_already_autonomous_purchase_also_works(paypal_spy):
    # Approval is a strictly stronger authorization; it must not break ALLOW.
    body = _approve({"product_id": "TAB003", "authorization": AUTONOMOUS_50K}).json()
    assert body["status"] == "order_created"
    assert body["approval_used"] is True
    assert len(paypal_spy) == 1


# --- 14/15. Hygiene ------------------------------------------------------------------------


def test_approval_response_contains_no_credentials(paypal_spy):
    text = json.dumps(_approve(LAP002_REQUEST).json()).lower()
    for secret in ("client_secret", "access_token", "bearer", "client_id"):
        assert secret not in text


def test_approval_needs_no_ai_provider(paypal_spy, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("AI_PROVIDER", raising=False)
    resp = _approve(LAP002_REQUEST)
    assert resp.status_code == 200
    assert resp.json()["status"] == "order_created"


def test_missing_authorization_rejected(paypal_spy):
    assert _approve({"product_id": "LAP002"}).status_code == 422
    assert paypal_spy == []
