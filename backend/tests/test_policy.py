"""Tests for the deterministic Policy Engine and POST /policy/evaluate.

The engine is a pure function, so these tests call it directly with
structured inputs and assert exact decisions and reason codes. No AI
provider, no PayPal, no network — nothing to mock.
"""

import math

import pytest
from starlette.testclient import TestClient

from app.main import app
from app.policy import evaluate_policy
from app.schemas.intent import Authorization, HardConstraints
from app.schemas.policy import PolicyOutcome, PolicySelectedProduct, ReasonCode

client = TestClient(app, raise_server_exceptions=False)


def _product(price=42000, currency="INR", product_id="LAP002") -> PolicySelectedProduct:
    return PolicySelectedProduct(
        product_id=product_id, price_inr=price, currency=currency
    )


def _auth(requested=True, autonomous=True, limit=50000) -> Authorization:
    return Authorization(
        purchase_requested=requested,
        autonomous_purchase_allowed=autonomous,
        auto_approve_below_inr=limit,
    )


# --- Rule 1: no purchase requested -> DENY -----------------------------------


def test_no_purchase_requested_denies():
    result = evaluate_policy(_product(), _auth(requested=False))
    assert result.decision == PolicyOutcome.DENY
    assert result.reason_code == ReasonCode.NO_PURCHASE_REQUESTED
    assert result.reason == "No purchase was requested."


def test_recommendation_defaults_deny():
    # A pure recommendation flow leaves Authorization at its safe defaults —
    # that must never be read as permission to purchase.
    result = evaluate_policy(_product(), Authorization())
    assert result.decision == PolicyOutcome.DENY
    assert result.reason_code == ReasonCode.NO_PURCHASE_REQUESTED


# --- Rule 2: shopping-constraint violations -> DENY ---------------------------


def test_price_above_shopping_max_denies():
    result = evaluate_policy(
        _product(price=85000),
        _auth(limit=100000),  # generous autonomy limit must NOT rescue it
        hard_constraints=HardConstraints(max_price_inr=80000),
    )
    assert result.decision == PolicyOutcome.DENY
    assert result.reason_code == ReasonCode.EXCEEDS_SHOPPING_MAX
    assert "₹85,000" in result.reason and "₹80,000" in result.reason


def test_price_at_shopping_max_is_not_a_violation():
    result = evaluate_policy(
        _product(price=80000),
        _auth(limit=100000),
        hard_constraints=HardConstraints(max_price_inr=80000),
    )
    assert result.decision == PolicyOutcome.ALLOW_AUTONOMOUS_PURCHASE


def test_price_below_shopping_min_denies():
    result = evaluate_policy(
        _product(price=30000),
        _auth(),
        hard_constraints=HardConstraints(min_price_inr=40000),
    )
    assert result.decision == PolicyOutcome.DENY
    assert result.reason_code == ReasonCode.BELOW_SHOPPING_MIN


def test_no_hard_constraints_skips_shopping_rules():
    result = evaluate_policy(_product(price=42000), _auth(), hard_constraints=None)
    assert result.decision == PolicyOutcome.ALLOW_AUTONOMOUS_PURCHASE


# --- Rule 3: purchase requested, autonomy not granted -> REQUIRE_USER_APPROVAL


def test_purchase_without_autonomy_requires_approval():
    result = evaluate_policy(_product(), _auth(autonomous=False, limit=None))
    assert result.decision == PolicyOutcome.REQUIRE_USER_APPROVAL
    assert result.reason_code == ReasonCode.AUTONOMY_NOT_GRANTED


def test_stray_limit_without_autonomy_still_requires_approval():
    # Even if a limit value is present, autonomy=false means no autonomy.
    result = evaluate_policy(_product(price=1000), _auth(autonomous=False, limit=50000))
    assert result.decision == PolicyOutcome.REQUIRE_USER_APPROVAL
    assert result.reason_code == ReasonCode.AUTONOMY_NOT_GRANTED


# --- Rule 4: autonomous and within limit -> ALLOW ------------------------------


def test_within_autonomous_limit_allows():
    result = evaluate_policy(_product(price=42000), _auth(limit=50000))
    assert result.decision == PolicyOutcome.ALLOW_AUTONOMOUS_PURCHASE
    assert result.reason_code == ReasonCode.WITHIN_AUTONOMOUS_LIMIT
    assert result.product_id == "LAP002"
    assert result.product_price_inr == 42000
    assert result.authorization_limit_inr == 50000


def test_exact_boundary_price_equals_limit_allows():
    # Inclusive boundary: price == auto_approve_below_inr -> ALLOW.
    result = evaluate_policy(_product(price=50000), _auth(limit=50000))
    assert result.decision == PolicyOutcome.ALLOW_AUTONOMOUS_PURCHASE
    assert result.reason_code == ReasonCode.WITHIN_AUTONOMOUS_LIMIT


# --- Rule 5: autonomous but above limit -> REQUIRE_USER_APPROVAL ----------------


def test_above_autonomous_limit_requires_approval():
    result = evaluate_policy(_product(price=72000), _auth(limit=50000))
    assert result.decision == PolicyOutcome.REQUIRE_USER_APPROVAL
    assert result.reason_code == ReasonCode.EXCEEDS_AUTONOMOUS_LIMIT
    assert "₹72,000" in result.reason and "₹50,000" in result.reason
    assert "satisfies the shopping requirements" in result.reason


def test_one_rupee_over_limit_requires_approval():
    result = evaluate_policy(_product(price=50001), _auth(limit=50000))
    assert result.decision == PolicyOutcome.REQUIRE_USER_APPROVAL
    assert result.reason_code == ReasonCode.EXCEEDS_AUTONOMOUS_LIMIT


# --- Rule 6: autonomy granted but no limit -> REQUIRE_USER_APPROVAL -------------


def test_missing_limit_is_never_unlimited():
    result = evaluate_policy(_product(price=100), _auth(limit=None))
    assert result.decision == PolicyOutcome.REQUIRE_USER_APPROVAL
    assert result.reason_code == ReasonCode.NO_AUTONOMOUS_LIMIT


# --- Rule 7: invalid price -> DENY ----------------------------------------------


@pytest.mark.parametrize(
    "bad_price",
    [None, 0, -1, -42000, math.nan, math.inf, -math.inf],
    ids=["missing", "zero", "negative", "very-negative", "nan", "inf", "-inf"],
)
def test_invalid_price_denies(bad_price):
    # Even full autonomy cannot approve a corrupt price.
    result = evaluate_policy(_product(price=bad_price), _auth(limit=10**9))
    assert result.decision == PolicyOutcome.DENY
    assert result.reason_code == ReasonCode.INVALID_PRODUCT_PRICE


# --- Rule 8: currency -----------------------------------------------------------


def test_non_inr_currency_denies():
    result = evaluate_policy(_product(currency="USD"), _auth())
    assert result.decision == PolicyOutcome.DENY
    assert result.reason_code == ReasonCode.UNSUPPORTED_CURRENCY
    assert "USD" in result.reason


def test_inr_is_case_insensitive():
    result = evaluate_policy(_product(currency="inr"), _auth())
    assert result.decision == PolicyOutcome.ALLOW_AUTONOMOUS_PURCHASE


# --- Shopping budget vs autonomy limit stay independent -------------------------


SHOPPING_80K = HardConstraints(max_price_inr=80000)


def test_limits_independent_product_within_both():
    result = evaluate_policy(
        _product(price=48000), _auth(limit=50000), hard_constraints=SHOPPING_80K
    )
    assert result.decision == PolicyOutcome.ALLOW_AUTONOMOUS_PURCHASE


def test_limits_independent_between_autonomy_and_budget():
    # 50000 < 72000 <= 80000: passes the budget, needs manual approval.
    result = evaluate_policy(
        _product(price=72000), _auth(limit=50000), hard_constraints=SHOPPING_80K
    )
    assert result.decision == PolicyOutcome.REQUIRE_USER_APPROVAL
    assert result.reason_code == ReasonCode.EXCEEDS_AUTONOMOUS_LIMIT
    # Both facts are reported, unmixed.
    assert result.shopping_max_inr == 80000
    assert result.authorization_limit_inr == 50000


def test_limits_independent_above_budget():
    # 85000 > 80000: the budget denies outright, autonomy never consulted.
    result = evaluate_policy(
        _product(price=85000), _auth(limit=50000), hard_constraints=SHOPPING_80K
    )
    assert result.decision == PolicyOutcome.DENY
    assert result.reason_code == ReasonCode.EXCEEDS_SHOPPING_MAX


def test_autonomy_limit_never_acts_as_shopping_cap():
    # No shopping constraints at all: a price above the autonomy limit is NOT
    # denied (the limit is not a budget) — it escalates to the user instead.
    result = evaluate_policy(_product(price=79000), _auth(limit=50000))
    assert result.decision == PolicyOutcome.REQUIRE_USER_APPROVAL
    assert result.decision != PolicyOutcome.DENY


# --- Determinism -----------------------------------------------------------------


def test_same_input_same_output():
    args = (_product(price=72000), _auth(limit=50000), SHOPPING_80K)
    assert evaluate_policy(*args) == evaluate_policy(*args)


# --- POST /policy/evaluate --------------------------------------------------------


def _post(body: dict):
    return client.post("/policy/evaluate", json=body)


def test_endpoint_allow():
    resp = _post({
        "product": {"product_id": "LAP002", "price_inr": 42000},
        "authorization": {
            "purchase_requested": True,
            "autonomous_purchase_allowed": True,
            "auto_approve_below_inr": 50000,
        },
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "ALLOW_AUTONOMOUS_PURCHASE"
    assert body["reason_code"] == "WITHIN_AUTONOMOUS_LIMIT"
    assert body["product_id"] == "LAP002"
    assert body["product_price_inr"] == 42000
    assert body["authorization_limit_inr"] == 50000
    assert body["reason"]


def test_endpoint_require_approval_with_constraints():
    resp = _post({
        "product": {"product_id": "LAP002", "price_inr": 72000},
        "authorization": {
            "purchase_requested": True,
            "autonomous_purchase_allowed": True,
            "auto_approve_below_inr": 50000,
        },
        "hard_constraints": {"max_price_inr": 80000},
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "REQUIRE_USER_APPROVAL"
    assert body["reason_code"] == "EXCEEDS_AUTONOMOUS_LIMIT"


def test_endpoint_deny_no_purchase_requested():
    resp = _post({
        "product": {"product_id": "LAP002", "price_inr": 42000},
        "authorization": {"purchase_requested": False},
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "DENY"
    assert body["reason_code"] == "NO_PURCHASE_REQUESTED"


def test_endpoint_evaluates_only_no_payment_artifacts():
    # The response is a decision, never an executed payment.
    resp = _post({
        "product": {"product_id": "LAP002", "price_inr": 42000},
        "authorization": {
            "purchase_requested": True,
            "autonomous_purchase_allowed": True,
            "auto_approve_below_inr": 50000,
        },
    })
    body = resp.json()
    for forbidden in ("order_id", "capture_id", "approve_url", "links", "status"):
        assert forbidden not in body


def test_endpoint_missing_product_rejected():
    assert _post({"authorization": {"purchase_requested": True}}).status_code == 422
