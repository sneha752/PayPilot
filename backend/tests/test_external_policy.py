"""External-product policy evaluation tests (Phase 11C).

The two properties these tests defend:

  1. NO SUCCESSFUL REVALIDATION -> NO PURCHASE. Only the LATEST revalidation
     being REVALIDATED admits a product to the Policy Engine. Every other
     status is an upstream eligibility failure with policy_decision=null,
     never a fabricated DENY.

  2. THE FRESH PRICE IS THE ONLY PRICE. The engine receives the revalidation
     snapshot's normalized price. The adopted snapshot's price is never a
     fallback, and a client cannot supply one — the endpoint has no body.

Plus: the existing Policy Engine remains the sole authority (its decisions
and reason codes are asserted identical to calling it directly), and no
PayPal, ledger, refund, session or AI path runs.

Providers are faked; nothing here calls Serper. Each test gets its own
temporary SQLite database (conftest).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.policy import evaluate_policy
from app.schemas.adoption import AdoptedProductStatus
from app.schemas.discovery import NormalizedDiscoveredProduct
from app.schemas.external_policy import EligibilityReason, EligibilityStatus
from app.schemas.intent import Authorization, HardConstraints
from app.schemas.policy import PolicyOutcome, PolicySelectedProduct, ReasonCode
from app.schemas.revalidation import (
    AvailabilityStatus,
    IdentityMatch,
    MerchantStatus,
    PriceStatus,
    RevalidationReason,
    RevalidationSnapshot,
    RevalidationStatus,
)
from app.services import (
    adoption_service,
    external_policy_service,
    purchase_policy_settings,
    revalidation_service,
)
from app.services.adoption_service import AdoptedProductNotFoundError
from app.services.discovery_evidence_store import record_products
from app.services.external_policy_service import evaluate_adopted_product

client = TestClient(app)

NOW = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)
TITLE = "Sony WH-CH520 Wireless Headphones"
GOOGLE_LINK = "https://www.google.com/shopping/product/1"


# ---------------------------------------------------------------------------
# Fixtures and builders
# ---------------------------------------------------------------------------


def evidence(**overrides) -> NormalizedDiscoveredProduct:
    fields = {
        "external_id": "serper:111111",
        "source": "serper",
        "title": TITLE,
        "description": None,
        "brand": "Sony",
        "category": "headphones",
        "image_url": None,
        "product_url": GOOGLE_LINK,
        "retailer_domain": "www.google.com",
        "availability": None,
        # Adopted at USD 19.99 -> INR 1824.09 (Phase 10F demo rate 91.25).
        "observed_price": Decimal("19.99"),
        "observed_currency": "USD",
        "observed_at": NOW,
        "normalized_price": Decimal("1824.09"),
        "normalized_currency": "INR",
        "conversion_rate": Decimal("91.25"),
        "conversion_source": "configured_demo_rate",
        "rate_observed_at": None,
        "merchant_name": "Amazon",
        "merchant_domain": None,
    }
    fields.update(overrides)
    return NormalizedDiscoveredProduct(**fields)


@pytest.fixture
def frozen_core(monkeypatch):
    """Any PayPal/purchase/ledger/refund/session/webhook touch fails the test.

    evaluate_policy is deliberately NOT booby-trapped: this phase is
    supposed to call it. Tests below assert it was called with the right
    input rather than that it was avoided.
    """
    from app import paypal
    from app.services import (
        confirmation_service,
        purchase_execution_service,
        refund_service,
        session_service,
        transaction_service,
        webhook_reconciliation_service,
    )

    def bomb(name):
        def _bomb(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError(f"policy evaluation must not touch {name}")

        return _bomb

    monkeypatch.setattr(paypal, "get_access_token", bomb("PayPal auth"))
    monkeypatch.setattr(paypal, "create_order", bomb("PayPal create_order"))
    monkeypatch.setattr(paypal, "capture_order", bomb("PayPal capture_order"))
    monkeypatch.setattr(paypal, "refund_capture", bomb("PayPal refund_capture"))
    monkeypatch.setattr(paypal, "get_order", bomb("PayPal get_order"))
    monkeypatch.setattr(
        purchase_execution_service, "execute_purchase", bomb("purchase execution")
    )
    monkeypatch.setattr(
        purchase_execution_service, "approve_purchase", bomb("purchase approval")
    )
    monkeypatch.setattr(
        purchase_execution_service, "capture_purchase", bomb("capture")
    )
    monkeypatch.setattr(
        transaction_service, "create_transaction", bomb("the ledger")
    )
    monkeypatch.setattr(transaction_service, "update_status", bomb("the ledger"))
    monkeypatch.setattr(transaction_service, "mark_captured", bomb("the ledger"))
    monkeypatch.setattr(refund_service, "refund_transaction", bomb("refunds"))
    monkeypatch.setattr(session_service, "create_session", bomb("sessions"))
    monkeypatch.setattr(
        confirmation_service, "confirm_pending_action", bomb("confirmation")
    )
    monkeypatch.setattr(
        webhook_reconciliation_service, "reconcile_event", bomb("webhooks")
    )


@pytest.fixture
def no_ai(monkeypatch):
    from app.agents import comparison_agent, intent_agent, recommendation_agent
    from app.services import providers

    def bomb(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("policy evaluation must not invoke AI")

    monkeypatch.setattr(providers, "get_provider", bomb)
    monkeypatch.setattr(intent_agent, "get_provider", bomb)
    monkeypatch.setattr(comparison_agent, "rank", bomb)
    monkeypatch.setattr(recommendation_agent, "recommend", bomb)


@pytest.fixture
def adopted(frozen_core, no_ai):
    """One adopted product, with no revalidation yet."""
    record_products([evidence()])
    record, _ = adoption_service.adopt("serper", "serper:111111")
    return record


def snapshot(
    adopted_product_id: str,
    *,
    status=RevalidationStatus.REVALIDATED,
    price=Decimal("1824.09"),
    currency="INR",
    price_status=PriceStatus.PRICE_UNCHANGED,
    checked_at=None,
    reval_id=None,
) -> RevalidationSnapshot:
    """Persist one revalidation snapshot directly.

    Written through the service's own insert, so the row is exactly what a
    real revalidation would have produced — no second schema in the tests.
    """
    confirmed = status is RevalidationStatus.REVALIDATED
    record = RevalidationSnapshot(
        revalidation_id=reval_id or f"reval_{status.value.lower()}_{price}",
        adopted_product_id=adopted_product_id,
        checked_at=(checked_at or NOW).isoformat(),
        discovery_source="serper",
        discovery_external_id="serper:111111",
        identity_kind="PROVIDER_PRODUCT_ID",
        product_identity_key="serper:serper:111111",
        revalidation_status=status,
        identity_match=(
            IdentityMatch.EXACT_EXTERNAL_ID if confirmed else IdentityMatch.NOT_FOUND
        ),
        reason_code=(
            RevalidationReason.IDENTITY_CONFIRMED
            if confirmed
            else RevalidationReason.NO_RESULT_MATCHED_IDENTITY
        ),
        price_status=price_status,
        availability=AvailabilityStatus.UNKNOWN,
        merchant_status=MerchantStatus.MERCHANT_UNCHANGED,
        current_title=TITLE if confirmed else None,
        current_merchant_name="Amazon" if confirmed else None,
        current_merchant_domain=None,
        current_product_url=GOOGLE_LINK if confirmed else None,
        current_observed_price=Decimal("19.99") if confirmed else None,
        current_observed_currency="USD" if confirmed else None,
        current_normalized_price=price if confirmed else None,
        current_normalized_currency=currency if confirmed else None,
        previous_observed_price=Decimal("19.99"),
        previous_observed_currency="USD",
    )
    revalidation_service._insert(record)
    return record


def allow_autonomous(limit=Decimal("5000")):
    """Settings that permit autonomous purchase below `limit`."""
    purchase_policy_settings.set_settings(
        purchase_requested=True,
        autonomous_purchase_allowed=True,
        auto_approve_below_inr=limit,
    )


def post_evaluate(adopted_product_id, **kwargs):
    return client.post(
        f"/discovery/adopted/{adopted_product_id}/policy-evaluate", **kwargs
    )


# ---------------------------------------------------------------------------
# 1-3. The FRESH price is what the engine evaluates
# ---------------------------------------------------------------------------


def test_price_unchanged_evaluates_the_fresh_price(adopted):
    allow_autonomous()
    snapshot(adopted.adopted_product_id, price=Decimal("1824.09"))

    body = post_evaluate(adopted.adopted_product_id).json()

    assert body["eligibility"] == "EVALUATED"
    assert body["eligibility_reason"] == "FRESH_EVIDENCE_CONFIRMED"
    assert body["current_normalized_price"] == "1824.09"
    assert body["current_normalized_currency"] == "INR"
    assert body["policy_decision"]["product_price_inr"] == 1824.09


def test_price_changed_evaluates_the_fresh_price_not_the_adopted_one(adopted):
    """The adopted record says 1824.09; the provider now says 2700.00."""
    allow_autonomous(limit=Decimal("5000"))
    snapshot(
        adopted.adopted_product_id,
        price=Decimal("2700.00"),
        price_status=PriceStatus.PRICE_CHANGED,
    )

    body = post_evaluate(adopted.adopted_product_id).json()

    assert body["price_status"] == "PRICE_CHANGED"
    assert body["current_normalized_price"] == "2700.00"
    # The FRESH figure reached the engine...
    assert body["policy_decision"]["product_price_inr"] == 2700.0
    # ...and a price change alone is not a denial.
    assert body["policy_decision"]["decision"] == "ALLOW_AUTONOMOUS_PURCHASE"


def test_the_adopted_price_never_reaches_the_engine(adopted):
    """A fresh price above the limit must not be rescued by the old one."""
    allow_autonomous(limit=Decimal("2000"))
    snapshot(
        adopted.adopted_product_id,
        price=Decimal("2700.00"),
        price_status=PriceStatus.PRICE_CHANGED,
    )

    body = post_evaluate(adopted.adopted_product_id).json()

    decision = body["policy_decision"]
    # The adopted 1824.09 WOULD have been within the 2000 limit. It was not
    # used, so the engine escalates on the fresh 2700.00 instead.
    assert decision["product_price_inr"] == 2700.0
    assert decision["product_price_inr"] != 1824.09
    assert decision["decision"] == "REQUIRE_USER_APPROVAL"
    assert decision["reason_code"] == "EXCEEDS_AUTONOMOUS_LIMIT"


def test_a_cheaper_fresh_price_is_also_used(adopted):
    """Freshness cuts both ways: a drop is used too, not just a rise."""
    allow_autonomous(limit=Decimal("1000"))
    snapshot(
        adopted.adopted_product_id,
        price=Decimal("900.00"),
        price_status=PriceStatus.PRICE_CHANGED,
    )

    body = post_evaluate(adopted.adopted_product_id).json()

    assert body["policy_decision"]["product_price_inr"] == 900.0
    assert body["policy_decision"]["decision"] == "ALLOW_AUTONOMOUS_PURCHASE"


def test_the_adapter_cannot_read_a_price_from_the_adopted_record():
    """Structural: _policy_input receives a price, so none can be substituted."""
    import inspect

    params = set(
        inspect.signature(external_policy_service._policy_input).parameters
    )

    assert params == {"adopted_product_id", "price", "currency"}


def test_no_response_field_carries_the_adopted_observed_price(adopted):
    allow_autonomous()
    snapshot(adopted.adopted_product_id, price=Decimal("2700.00"))

    body = post_evaluate(adopted.adopted_product_id).json()

    # There is deliberately no adopted/observed price field to confuse.
    for forbidden in (
        "observed_price",
        "observed_currency",
        "adopted_observed_price",
        "previous_observed_price",
        "purchase_price",
        "amount",
    ):
        assert forbidden not in body


# ---------------------------------------------------------------------------
# 4-9. The revalidation gate: no success -> no policy evaluation
# ---------------------------------------------------------------------------


def test_no_revalidation_on_record_is_an_upstream_failure(adopted):
    allow_autonomous()

    body = post_evaluate(adopted.adopted_product_id).json()

    assert body["eligibility"] == "NOT_ELIGIBLE"
    assert body["eligibility_reason"] == "NO_REVALIDATION_ON_RECORD"
    # The engine was never asked, so it issued no ruling.
    assert body["policy_decision"] is None
    assert body["revalidation_id"] is None
    # And no price is echoed, so nothing looks current.
    assert body["current_normalized_price"] is None


@pytest.mark.parametrize(
    ("status", "expected_reason"),
    [
        (
            RevalidationStatus.IDENTITY_NOT_FOUND,
            "LATEST_REVALIDATION_IDENTITY_NOT_FOUND",
        ),
        (
            RevalidationStatus.IDENTITY_UNVERIFIABLE,
            "LATEST_REVALIDATION_IDENTITY_UNVERIFIABLE",
        ),
        (
            RevalidationStatus.PROVIDER_UNAVAILABLE,
            "LATEST_REVALIDATION_PROVIDER_UNAVAILABLE",
        ),
        (
            RevalidationStatus.PROVIDER_ERROR,
            "LATEST_REVALIDATION_PROVIDER_ERROR",
        ),
        (
            RevalidationStatus.NOT_REVALIDATABLE,
            "LATEST_REVALIDATION_NOT_REVALIDATABLE",
        ),
    ],
)
def test_every_failed_revalidation_status_is_an_upstream_failure(
    adopted, status, expected_reason
):
    allow_autonomous()
    snapshot(adopted.adopted_product_id, status=status)

    body = post_evaluate(adopted.adopted_product_id).json()

    assert body["eligibility"] == "NOT_ELIGIBLE"
    assert body["eligibility_reason"] == expected_reason
    assert body["policy_decision"] is None
    assert body["current_normalized_price"] is None
    # The upstream status stays visible rather than being swallowed.
    assert body["revalidation_status"] == status.value


def test_an_upstream_failure_is_never_reported_as_a_policy_deny(adopted):
    """The engine must not be made to appear to have ruled."""
    allow_autonomous()
    snapshot(
        adopted.adopted_product_id,
        status=RevalidationStatus.IDENTITY_NOT_FOUND,
    )

    body = post_evaluate(adopted.adopted_product_id).json()

    assert body["policy_decision"] is None
    assert "DENY" not in str(body)
    for code in (c.value for c in ReasonCode):
        assert body["eligibility_reason"] != code


def test_identity_not_found_does_not_claim_the_product_was_withdrawn(adopted):
    """Phase 11B.1 semantics: it means "could not confirm", nothing more."""
    allow_autonomous()
    snapshot(
        adopted.adopted_product_id,
        status=RevalidationStatus.IDENTITY_NOT_FOUND,
    )

    body = post_evaluate(adopted.adopted_product_id).json()
    text = str(body).lower()

    assert body["eligibility_reason"] == "LATEST_REVALIDATION_IDENTITY_NOT_FOUND"
    for wrong in ("withdrawn", "discontinued", "removed", "deleted", "gone"):
        assert wrong not in text


def test_revalidated_without_a_fresh_price_is_an_upstream_failure(adopted):
    """Identity confirmed, but the provider supplied no price."""
    allow_autonomous()
    snapshot(adopted.adopted_product_id, price=None, price_status=PriceStatus.PRICE_UNAVAILABLE)

    body = post_evaluate(adopted.adopted_product_id).json()

    assert body["eligibility"] == "NOT_ELIGIBLE"
    assert body["eligibility_reason"] == "NO_FRESH_NORMALIZED_PRICE"
    assert body["policy_decision"] is None
    # Crucially: it did NOT fall back to the adopted 1824.09.
    assert body["current_normalized_price"] is None


def test_a_disabled_adopted_product_is_not_evaluated(adopted):
    """An operator decision outranks fresh evidence."""
    allow_autonomous()
    snapshot(adopted.adopted_product_id)
    adoption_service.set_status(
        adopted.adopted_product_id, AdoptedProductStatus.DISABLED
    )

    body = post_evaluate(adopted.adopted_product_id).json()

    assert body["eligibility"] == "NOT_ELIGIBLE"
    assert body["eligibility_reason"] == "ADOPTED_PRODUCT_DISABLED"
    assert body["policy_decision"] is None


def test_every_revalidation_status_is_mapped(adopted):
    """No status may silently fall through the gate to "eligible"."""
    mapped = set(external_policy_service._UPSTREAM_REASONS)
    all_statuses = set(RevalidationStatus)

    assert mapped == all_statuses - {RevalidationStatus.REVALIDATED}


def test_unknown_adopted_product_is_404(frozen_core, no_ai):
    assert post_evaluate("adopt_does_not_exist").status_code == 404


def test_unknown_adopted_product_raises_a_typed_error(frozen_core, no_ai):
    with pytest.raises(AdoptedProductNotFoundError):
        evaluate_adopted_product("adopt_does_not_exist")


# ---------------------------------------------------------------------------
# 10-12. Latest applicable revalidation selection
# ---------------------------------------------------------------------------


def test_the_latest_successful_revalidation_is_used(adopted):
    allow_autonomous()
    snapshot(
        adopted.adopted_product_id,
        price=Decimal("1000.00"),
        checked_at=NOW - timedelta(hours=2),
        reval_id="reval_old",
    )
    snapshot(
        adopted.adopted_product_id,
        price=Decimal("2000.00"),
        checked_at=NOW,
        reval_id="reval_new",
    )

    body = post_evaluate(adopted.adopted_product_id).json()

    assert body["revalidation_id"] == "reval_new"
    assert body["current_normalized_price"] == "2000.00"
    assert body["policy_decision"]["product_price_inr"] == 2000.0


def test_a_stale_success_is_not_preferred_over_a_newer_failure(adopted):
    """The decisive stale-data test.

    An older snapshot succeeded; the newest one could not confirm the
    product. Using the older success would evaluate on evidence we have
    since superseded.
    """
    allow_autonomous()
    snapshot(
        adopted.adopted_product_id,
        price=Decimal("1500.00"),
        checked_at=NOW - timedelta(hours=1),
        reval_id="reval_old_success",
    )
    snapshot(
        adopted.adopted_product_id,
        status=RevalidationStatus.IDENTITY_NOT_FOUND,
        checked_at=NOW,
        reval_id="reval_new_failure",
    )

    body = post_evaluate(adopted.adopted_product_id).json()

    assert body["eligibility"] == "NOT_ELIGIBLE"
    assert body["revalidation_id"] == "reval_new_failure"
    assert body["policy_decision"] is None
    assert body["current_normalized_price"] is None


def test_a_newer_success_supersedes_an_older_failure(adopted):
    """Recovery works in the other direction too."""
    allow_autonomous()
    snapshot(
        adopted.adopted_product_id,
        status=RevalidationStatus.PROVIDER_UNAVAILABLE,
        checked_at=NOW - timedelta(hours=1),
        reval_id="reval_old_failure",
    )
    snapshot(
        adopted.adopted_product_id,
        price=Decimal("1200.00"),
        checked_at=NOW,
        reval_id="reval_new_success",
    )

    body = post_evaluate(adopted.adopted_product_id).json()

    assert body["eligibility"] == "EVALUATED"
    assert body["revalidation_id"] == "reval_new_success"
    assert body["policy_decision"]["product_price_inr"] == 1200.0


def test_many_snapshots_resolve_to_the_single_newest(adopted):
    allow_autonomous()
    for hours, price in ((5, "100"), (4, "200"), (3, "300"), (2, "400")):
        snapshot(
            adopted.adopted_product_id,
            price=Decimal(price),
            checked_at=NOW - timedelta(hours=hours),
            reval_id=f"reval_{hours}",
        )
    snapshot(
        adopted.adopted_product_id,
        price=Decimal("500"),
        checked_at=NOW,
        reval_id="reval_newest",
    )

    body = post_evaluate(adopted.adopted_product_id).json()

    assert revalidation_service.count(adopted.adopted_product_id) == 5
    assert body["revalidation_id"] == "reval_newest"
    assert body["current_normalized_price"] == "500"


def test_another_products_revalidation_is_not_used(adopted, frozen_core, no_ai):
    allow_autonomous()
    record_products([evidence(external_id="serper:other")])
    other, _ = adoption_service.adopt("serper", "serper:other")
    snapshot(other.adopted_product_id, price=Decimal("999.00"))

    body = post_evaluate(adopted.adopted_product_id).json()

    assert body["eligibility_reason"] == "NO_REVALIDATION_ON_RECORD"
    assert body["revalidation_id"] is None


def test_latest_revalidation_helper_returns_the_newest(adopted):
    snapshot(
        adopted.adopted_product_id,
        checked_at=NOW - timedelta(days=1),
        reval_id="reval_a",
    )
    snapshot(adopted.adopted_product_id, checked_at=NOW, reval_id="reval_b")

    latest = external_policy_service.latest_revalidation(
        adopted.adopted_product_id
    )

    assert latest.revalidation_id == "reval_b"


def test_latest_revalidation_helper_is_none_when_there_is_none(adopted):
    assert (
        external_policy_service.latest_revalidation(adopted.adopted_product_id)
        is None
    )


# ---------------------------------------------------------------------------
# 13-21. The existing Policy Engine is the sole authority
# ---------------------------------------------------------------------------


def test_allow_autonomous_purchase_is_preserved_exactly(adopted):
    allow_autonomous(limit=Decimal("5000"))
    snapshot(adopted.adopted_product_id, price=Decimal("2000.00"))

    decision = post_evaluate(adopted.adopted_product_id).json()["policy_decision"]

    assert decision["decision"] == "ALLOW_AUTONOMOUS_PURCHASE"
    assert decision["reason_code"] == "WITHIN_AUTONOMOUS_LIMIT"


def test_require_user_approval_is_preserved_exactly(adopted):
    purchase_policy_settings.set_settings(
        purchase_requested=True, autonomous_purchase_allowed=False
    )
    snapshot(adopted.adopted_product_id, price=Decimal("2000.00"))

    decision = post_evaluate(adopted.adopted_product_id).json()["policy_decision"]

    assert decision["decision"] == "REQUIRE_USER_APPROVAL"
    assert decision["reason_code"] == "AUTONOMY_NOT_GRANTED"


def test_deny_is_preserved_exactly(adopted):
    """Unconfigured settings authorize nothing, and the engine says so."""
    snapshot(adopted.adopted_product_id, price=Decimal("2000.00"))

    body = post_evaluate(adopted.adopted_product_id).json()

    # This IS a real policy ruling, so eligibility is EVALUATED.
    assert body["eligibility"] == "EVALUATED"
    assert body["policy_decision"]["decision"] == "DENY"
    assert body["policy_decision"]["reason_code"] == "NO_PURCHASE_REQUESTED"


def test_shopping_maximum_rule_is_preserved(adopted):
    purchase_policy_settings.set_settings(
        purchase_requested=True,
        autonomous_purchase_allowed=True,
        auto_approve_below_inr=Decimal("10000"),
        max_price_inr=2000,
    )
    snapshot(adopted.adopted_product_id, price=Decimal("2500.00"))

    decision = post_evaluate(adopted.adopted_product_id).json()["policy_decision"]

    assert decision["decision"] == "DENY"
    assert decision["reason_code"] == "EXCEEDS_SHOPPING_MAX"
    assert decision["shopping_max_inr"] == 2000


def test_shopping_minimum_rule_is_preserved(adopted):
    purchase_policy_settings.set_settings(
        purchase_requested=True,
        autonomous_purchase_allowed=True,
        auto_approve_below_inr=Decimal("10000"),
        min_price_inr=3000,
    )
    snapshot(adopted.adopted_product_id, price=Decimal("2500.00"))

    decision = post_evaluate(adopted.adopted_product_id).json()["policy_decision"]

    assert decision["decision"] == "DENY"
    assert decision["reason_code"] == "BELOW_SHOPPING_MIN"


def test_autonomous_limit_rule_is_preserved(adopted):
    allow_autonomous(limit=Decimal("2000"))
    snapshot(adopted.adopted_product_id, price=Decimal("2500.00"))

    decision = post_evaluate(adopted.adopted_product_id).json()["policy_decision"]

    assert decision["decision"] == "REQUIRE_USER_APPROVAL"
    assert decision["reason_code"] == "EXCEEDS_AUTONOMOUS_LIMIT"
    assert decision["authorization_limit_inr"] == 2000.0


def test_the_autonomous_limit_boundary_is_inclusive(adopted):
    """R4 is <=, and that must not shift at the external-product boundary."""
    allow_autonomous(limit=Decimal("2000"))
    snapshot(adopted.adopted_product_id, price=Decimal("2000.00"))

    decision = post_evaluate(adopted.adopted_product_id).json()["policy_decision"]

    assert decision["decision"] == "ALLOW_AUTONOMOUS_PURCHASE"


def test_missing_autonomous_limit_semantics_are_preserved(adopted):
    """Autonomy without a ceiling is never unlimited authority (R6)."""
    purchase_policy_settings.set_settings(
        purchase_requested=True,
        autonomous_purchase_allowed=True,
        auto_approve_below_inr=None,
    )
    snapshot(adopted.adopted_product_id, price=Decimal("10.00"))

    decision = post_evaluate(adopted.adopted_product_id).json()["policy_decision"]

    assert decision["decision"] == "REQUIRE_USER_APPROVAL"
    assert decision["reason_code"] == "NO_AUTONOMOUS_LIMIT"


def test_a_non_inr_currency_is_denied_by_the_engines_own_rule(adopted):
    """The V2 rule is NOT duplicated in the adapter; it is passed through."""
    allow_autonomous()
    snapshot(
        adopted.adopted_product_id, price=Decimal("24.99"), currency="USD"
    )

    body = post_evaluate(adopted.adopted_product_id).json()

    # Eligible (fresh evidence existed), and the ENGINE issued the DENY.
    assert body["eligibility"] == "EVALUATED"
    assert body["policy_decision"]["decision"] == "DENY"
    assert body["policy_decision"]["reason_code"] == "UNSUPPORTED_CURRENCY"


def test_an_invalid_fresh_price_is_denied_by_the_engines_own_rule(adopted):
    """V1 likewise stays with the engine."""
    allow_autonomous()
    snapshot(adopted.adopted_product_id, price=Decimal("0"))

    body = post_evaluate(adopted.adopted_product_id).json()

    assert body["eligibility"] == "EVALUATED"
    assert body["policy_decision"]["decision"] == "DENY"
    assert body["policy_decision"]["reason_code"] == "INVALID_PRODUCT_PRICE"


@pytest.mark.parametrize(
    ("price", "limit", "expected_decision", "expected_code"),
    [
        ("1824.09", "5000", "ALLOW_AUTONOMOUS_PURCHASE", "WITHIN_AUTONOMOUS_LIMIT"),
        ("2700.00", "5000", "ALLOW_AUTONOMOUS_PURCHASE", "WITHIN_AUTONOMOUS_LIMIT"),
        ("2700.00", "2000", "REQUIRE_USER_APPROVAL", "EXCEEDS_AUTONOMOUS_LIMIT"),
        ("2000.00", "2000", "ALLOW_AUTONOMOUS_PURCHASE", "WITHIN_AUTONOMOUS_LIMIT"),
        ("2000.01", "2000", "REQUIRE_USER_APPROVAL", "EXCEEDS_AUTONOMOUS_LIMIT"),
    ],
)
def test_the_decision_matches_calling_the_engine_directly(
    adopted, price, limit, expected_decision, expected_code
):
    """The strongest form: identical to invoking evaluate_policy ourselves.

    If this phase had introduced a rule of its own, these two would diverge.
    """
    allow_autonomous(limit=Decimal(limit))
    snapshot(adopted.adopted_product_id, price=Decimal(price))

    via_endpoint = post_evaluate(adopted.adopted_product_id).json()[
        "policy_decision"
    ]
    direct = evaluate_policy(
        product=PolicySelectedProduct(
            product_id=adopted.adopted_product_id,
            price_inr=float(Decimal(price)),
            currency="INR",
        ),
        authorization=Authorization(
            purchase_requested=True,
            autonomous_purchase_allowed=True,
            auto_approve_below_inr=float(Decimal(limit)),
        ),
        hard_constraints=None,
    )

    assert via_endpoint["decision"] == expected_decision == direct.decision.value
    assert via_endpoint["reason_code"] == expected_code == direct.reason_code.value
    assert via_endpoint["reason"] == direct.reason


def test_decimal_precision_is_preserved_in_the_response(adopted):
    allow_autonomous(limit=Decimal("200000"))
    snapshot(adopted.adopted_product_id, price=Decimal("118620.44"))

    body = post_evaluate(adopted.adopted_product_id).json()

    # Exact decimal STRING in our own response...
    assert body["current_normalized_price"] == "118620.44"
    assert isinstance(body["current_normalized_price"], str)
    # ...and the engine echoes the float it was handed, which is the
    # documented Decimal -> float boundary, not a re-rounding.
    assert body["policy_decision"]["product_price_inr"] == 118620.44


def _code_without_text(source: str) -> str:
    """Strip comments and string literals, leaving executable code only.

    Needed because the module's docstring legitimately NAMES the rules it
    deliberately does not implement ("...so the engine's own V2 rule issues
    UNSUPPORTED_CURRENCY"). Prose about what is left to the engine is
    documentation, not a duplicated rule.
    """
    import io
    import tokenize

    kept = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        kept.append(token.string)
    return "\n".join(kept)


def test_the_adapter_does_not_reimplement_any_policy_rule():
    """Structural: no limit/budget comparison may live in this module."""
    import pathlib
    import re

    code = _code_without_text(
        pathlib.Path(external_policy_service.__file__).read_text(encoding="utf-8")
    )

    for forbidden in (
        "auto_approve_below_inr",
        "max_price_inr",
        "min_price_inr",
        "SUPPORTED_CURRENCY",
        "isfinite",
    ):
        assert forbidden not in code, f"policy rule leaked into adapter: {forbidden}"
    # No numeric threshold comparison against a price at all.
    assert not re.search(r"price\s*[<>]=?", code)


def test_the_engine_is_never_told_the_product_is_external(adopted):
    """The Policy Engine stays product-type agnostic."""
    allow_autonomous()
    snapshot(adopted.adopted_product_id)

    decision = post_evaluate(adopted.adopted_product_id).json()["policy_decision"]

    assert set(decision) == {
        "decision",
        "reason_code",
        "reason",
        "product_id",
        "product_price_inr",
        "currency",
        "shopping_max_inr",
        "authorization_limit_inr",
    }
    for invented in (
        "EXTERNAL_PRODUCT_ALLOW",
        "AI_ALLOW",
        "SAFE_TO_BUY",
        "EXTERNAL",
    ):
        assert invented not in str(decision)


def test_no_second_policy_outcome_vocabulary_exists():
    assert {o.value for o in PolicyOutcome} == {
        "ALLOW_AUTONOMOUS_PURCHASE",
        "REQUIRE_USER_APPROVAL",
        "DENY",
    }
    # Eligibility codes are a separate namespace and never overlap.
    eligibility = {r.value for r in EligibilityReason} | {
        s.value for s in EligibilityStatus
    }
    assert eligibility.isdisjoint({o.value for o in PolicyOutcome})
    assert eligibility.isdisjoint({c.value for c in ReasonCode})


# ---------------------------------------------------------------------------
# 22-27. Security: the client cannot influence the decision
# ---------------------------------------------------------------------------


def test_the_endpoint_takes_no_request_body():
    """Structural: the route has no body parameter at all."""
    import inspect

    from app.main import discovery_adopted_policy_evaluate

    assert set(
        inspect.signature(discovery_adopted_policy_evaluate).parameters
    ) == {"adopted_product_id"}


@pytest.mark.parametrize(
    "forged",
    [
        {"price_inr": 1.0},
        {"current_normalized_price": "1.00"},
        {"normalized_price": "1.00"},
        {"observed_price": "1.00"},
        {"currency": "USD"},
        {"current_normalized_currency": "USD"},
        {"policy_decision": {"decision": "ALLOW_AUTONOMOUS_PURCHASE"}},
        {"decision": "ALLOW_AUTONOMOUS_PURCHASE"},
        {"eligibility": "EVALUATED"},
        {"auto_approve_below_inr": 999999},
        {"authorization": {"purchase_requested": True,
                           "autonomous_purchase_allowed": True,
                           "auto_approve_below_inr": 999999}},
        {"max_price_inr": 999999},
        {"min_price_inr": 0},
        {"hard_constraints": {"max_price_inr": 999999}},
        {"merchant_name": "Fake Merchant"},
        {"merchant_domain": "evil.example"},
        {"product_url": "https://evil.example"},
        {"title": "Cheap Laptop"},
        {"source": "channel3"},
        {"external_id": "serper:999999"},
        {"product_identity_key": "serper:serper:attacker"},
        {"adopted_product_id": "adopt_someone_else"},
        {"revalidation_id": "reval_attacker"},
        {"paypal_amount": "0.01"},
        {"paypal_order_id": "fake"},
        {"paypal_capture_id": "fake"},
    ],
)
def test_a_forged_body_cannot_influence_the_decision(adopted, forged):
    """Baseline is a DENY; no payload may turn it into an ALLOW."""
    snapshot(adopted.adopted_product_id, price=Decimal("2000.00"))

    body = post_evaluate(adopted.adopted_product_id, json=forged).json()

    # Unconfigured settings -> the engine's own DENY, every time.
    assert body["eligibility"] == "EVALUATED"
    assert body["policy_decision"]["decision"] == "DENY"
    assert body["policy_decision"]["reason_code"] == "NO_PURCHASE_REQUESTED"
    # Server-side facts, not the payload's.
    assert body["adopted_product_id"] == adopted.adopted_product_id
    assert body["current_normalized_price"] == "2000.00"
    assert body["current_normalized_currency"] == "INR"


def test_a_forged_limit_cannot_raise_the_ceiling(adopted):
    """The decisive security case: an attacker-chosen spend limit."""
    allow_autonomous(limit=Decimal("2000"))
    snapshot(adopted.adopted_product_id, price=Decimal("50000.00"))

    body = post_evaluate(
        adopted.adopted_product_id,
        json={
            "auto_approve_below_inr": 999999,
            "authorization": {
                "purchase_requested": True,
                "autonomous_purchase_allowed": True,
                "auto_approve_below_inr": 999999,
            },
        },
    ).json()

    decision = body["policy_decision"]
    # The SERVER's 2000 limit applied, not the body's 999999.
    assert decision["authorization_limit_inr"] == 2000.0
    assert decision["decision"] == "REQUIRE_USER_APPROVAL"
    assert decision["reason_code"] == "EXCEEDS_AUTONOMOUS_LIMIT"


def test_a_forged_price_cannot_lower_the_evaluated_amount(adopted):
    allow_autonomous(limit=Decimal("2000"))
    snapshot(adopted.adopted_product_id, price=Decimal("50000.00"))

    body = post_evaluate(
        adopted.adopted_product_id,
        json={"price_inr": 1.0, "current_normalized_price": "1.00"},
    ).json()

    assert body["policy_decision"]["product_price_inr"] == 50000.0
    assert body["policy_decision"]["decision"] == "REQUIRE_USER_APPROVAL"


def test_the_evaluate_function_takes_only_an_id():
    import inspect

    assert set(inspect.signature(evaluate_adopted_product).parameters) == {
        "adopted_product_id"
    }


def test_settings_default_to_authorizing_nothing(frozen_core, no_ai):
    """An unconfigured system must not authorize spending."""
    authorization = purchase_policy_settings.get_authorization()

    assert authorization.purchase_requested is False
    assert authorization.autonomous_purchase_allowed is False
    assert authorization.auto_approve_below_inr is None
    assert purchase_policy_settings.get_hard_constraints() is None


def test_settings_round_trip_exactly(frozen_core, no_ai):
    purchase_policy_settings.set_settings(
        purchase_requested=True,
        autonomous_purchase_allowed=True,
        auto_approve_below_inr=Decimal("2500.05"),
        max_price_inr=9000,
        min_price_inr=100,
    )

    authorization = purchase_policy_settings.get_authorization()
    constraints = purchase_policy_settings.get_hard_constraints()

    assert authorization.purchase_requested is True
    assert authorization.auto_approve_below_inr == 2500.05
    assert constraints.max_price_inr == 9000
    assert constraints.min_price_inr == 100


def test_settings_are_a_singleton(frozen_core, no_ai):
    purchase_policy_settings.set_settings(auto_approve_below_inr=Decimal("1"))
    purchase_policy_settings.set_settings(auto_approve_below_inr=Decimal("2"))

    with sqlite3.connect(purchase_policy_settings._db_path()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM purchase_policy_settings"
        ).fetchone()["n"]

    assert rows == 1
    assert purchase_policy_settings.get_authorization().auto_approve_below_inr == 2.0


def test_clearing_settings_restores_the_safe_defaults(frozen_core, no_ai):
    allow_autonomous()
    purchase_policy_settings.clear_settings()

    assert purchase_policy_settings.get_authorization().purchase_requested is False


def test_an_invalid_stored_limit_is_rejected_before_being_written(
    frozen_core, no_ai
):
    with pytest.raises(Exception):
        purchase_policy_settings.set_settings(
            auto_approve_below_inr=Decimal("-1")
        )

    # Nothing was persisted, so the safe default still applies.
    assert purchase_policy_settings.get_authorization().auto_approve_below_inr is None


def test_there_is_no_http_route_that_writes_policy_settings():
    """Raising a spend ceiling over unauthenticated HTTP is not offered."""
    paths = [getattr(r, "path", "") for r in app.routes]

    for path in paths:
        assert "policy-settings" not in path
        assert "settings" not in path


# ---------------------------------------------------------------------------
# 28-32. No payment, no AI
# ---------------------------------------------------------------------------


def test_evaluation_makes_no_paypal_purchase_ledger_or_ai_call(adopted):
    """frozen_core and no_ai boobytraps would raise if any were touched."""
    allow_autonomous()
    snapshot(adopted.adopted_product_id)

    assert post_evaluate(adopted.adopted_product_id).status_code == 200


def test_an_allow_decision_creates_no_paypal_order(adopted):
    """ALLOW_AUTONOMOUS_PURCHASE is a statement, not an action."""
    allow_autonomous(limit=Decimal("5000"))
    snapshot(adopted.adopted_product_id, price=Decimal("2000.00"))

    body = post_evaluate(adopted.adopted_product_id).json()

    assert body["policy_decision"]["decision"] == "ALLOW_AUTONOMOUS_PURCHASE"

    # Nothing was created: no order, no transaction, nothing to capture.
    # (Checked by SQL before touching /transactions, because reading that
    # endpoint would itself initialise the ledger table.)
    with sqlite3.connect(purchase_policy_settings._db_path()) as conn:
        conn.row_factory = sqlite3.Row
        tables = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "transactions" not in tables, "evaluation must not create the ledger"

    # And the ledger endpoint confirms an empty history.
    assert client.get("/transactions").json() == {"transactions": [], "count": 0}


def test_an_allow_decision_does_not_make_the_product_purchasable(adopted):
    allow_autonomous(limit=Decimal("5000"))
    snapshot(adopted.adopted_product_id, price=Decimal("2000.00"))
    assert (
        post_evaluate(adopted.adopted_product_id).json()["policy_decision"][
            "decision"
        ]
        == "ALLOW_AUTONOMOUS_PURCHASE"
    )

    response = client.post(
        "/purchase/execute",
        json={
            "product_id": adopted.adopted_product_id,
            "authorization": {
                "purchase_requested": True,
                "autonomous_purchase_allowed": True,
                "auto_approve_below_inr": 1000000,
            },
        },
    )

    # Still not a catalog product: no execution path exists for it.
    assert response.status_code == 404


def test_evaluation_does_not_mutate_the_adopted_record(adopted):
    allow_autonomous()
    before = adoption_service.get(adopted.adopted_product_id).model_dump()
    snapshot(adopted.adopted_product_id, price=Decimal("2700.00"))

    post_evaluate(adopted.adopted_product_id)

    assert adoption_service.get(adopted.adopted_product_id).model_dump() == before


def test_evaluation_does_not_add_revalidation_history(adopted):
    allow_autonomous()
    snapshot(adopted.adopted_product_id)

    post_evaluate(adopted.adopted_product_id)
    post_evaluate(adopted.adopted_product_id)

    # Read-and-decide: it never performs a revalidation of its own.
    assert revalidation_service.count(adopted.adopted_product_id) == 1


def test_the_service_imports_no_payment_or_ai_module():
    """Structural guard. evaluate_policy IS expected; payment is not."""
    import ast
    import pathlib

    tree = ast.parse(
        pathlib.Path(external_policy_service.__file__).read_text(encoding="utf-8")
    )
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(f"{node.module or ''}.{a.name}" for a in node.names)
    leaves = {n.rsplit(".", 1)[-1] for n in names}

    for forbidden in (
        "paypal",
        "purchase_execution_service",
        "purchase_orchestrator",
        "transaction_service",
        "refund_service",
        "session_service",
        "confirmation_service",
        "webhook_service",
        "webhook_reconciliation_service",
        "providers",
        "comparison_agent",
        "intent_agent",
        "currency_service",
    ):
        assert forbidden not in leaves, f"must not import {forbidden}"

    # It DOES use the existing policy engine, by design.
    assert "evaluate_policy" in leaves


def test_the_response_exposes_no_paypal_field(adopted):
    allow_autonomous()
    snapshot(adopted.adopted_product_id)

    body = post_evaluate(adopted.adopted_product_id).json()
    text = str(body).lower()

    assert "paypal" not in text
    assert "capture" not in text
    assert "refund" not in text
    assert "order_id" not in text


# ---------------------------------------------------------------------------
# 33-37. Existing behaviour unchanged
# ---------------------------------------------------------------------------


def test_the_existing_policy_evaluate_endpoint_is_unchanged(frozen_core, no_ai):
    """The local-catalog policy route still takes its client-supplied input."""
    response = client.post(
        "/policy/evaluate",
        json={
            "product": {"product_id": "LAP002", "price_inr": 50000, "currency": "INR"},
            "authorization": {
                "purchase_requested": True,
                "autonomous_purchase_allowed": True,
                "auto_approve_below_inr": 60000,
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "ALLOW_AUTONOMOUS_PURCHASE"
    assert body["reason_code"] == "WITHIN_AUTONOMOUS_LIMIT"


def test_server_side_settings_do_not_leak_into_the_catalog_policy_route(
    frozen_core, no_ai
):
    """The two paths keep their own authorization sources."""
    purchase_policy_settings.set_settings(
        purchase_requested=True,
        autonomous_purchase_allowed=True,
        auto_approve_below_inr=Decimal("1"),
    )

    response = client.post(
        "/policy/evaluate",
        json={
            "product": {"product_id": "LAP002", "price_inr": 500, "currency": "INR"},
            "authorization": {
                "purchase_requested": True,
                "autonomous_purchase_allowed": True,
                "auto_approve_below_inr": 60000,
            },
        },
    )

    # The REQUEST's 60000 governed, not the stored 1.
    assert response.json()["decision"] == "ALLOW_AUTONOMOUS_PURCHASE"
    assert response.json()["authorization_limit_inr"] == 60000.0


def test_the_existing_catalog_purchase_flow_still_rejects_unknown_products(
    frozen_core, no_ai
):
    response = client.post(
        "/purchase/execute",
        json={
            "product_id": "NOT_A_PRODUCT",
            "authorization": {"purchase_requested": True},
        },
    )

    assert response.status_code == 404


def test_products_json_is_unchanged(adopted):
    import pathlib

    from app.services import product_service
    from app.services.product_service import list_products

    allow_autonomous()
    snapshot(adopted.adopted_product_id)
    before = [p.model_dump() for p in list_products()]
    path = pathlib.Path(product_service._DATA_FILE)
    before_bytes = path.read_bytes()
    before_mtime = path.stat().st_mtime

    post_evaluate(adopted.adopted_product_id)

    assert [p.model_dump() for p in list_products()] == before
    assert path.read_bytes() == before_bytes
    assert path.stat().st_mtime == before_mtime


def test_the_transaction_schema_is_unchanged(adopted):
    from app.services import transaction_service

    allow_autonomous()
    snapshot(adopted.adopted_product_id)
    with sqlite3.connect(purchase_policy_settings._db_path()) as conn:
        conn.executescript(transaction_service._SCHEMA)
        conn.commit()

    def columns():
        with sqlite3.connect(purchase_policy_settings._db_path()) as conn:
            conn.row_factory = sqlite3.Row
            return [
                r["name"] for r in conn.execute("PRAGMA table_info(transactions)")
            ]

    before = columns()

    post_evaluate(adopted.adopted_product_id)

    assert columns() == before


def test_the_adoption_schema_is_unchanged_by_this_phase(adopted):
    """11C adds no column to adopted_products."""
    with sqlite3.connect(purchase_policy_settings._db_path()) as conn:
        conn.row_factory = sqlite3.Row
        columns = {
            r["name"] for r in conn.execute("PRAGMA table_info(adopted_products)")
        }

    assert columns == {
        "adopted_product_id",
        "discovery_source",
        "discovery_external_id",
        "title",
        "description",
        "brand",
        "category",
        "merchant_name",
        "merchant_domain",
        "product_url",
        "observed_price",
        "observed_currency",
        "normalized_price",
        "normalized_currency",
        "observed_at",
        "adopted_at",
        "updated_at",
        "status",
        "product_identity_kind",
        "product_identity_key",
    }


def test_the_revalidation_schema_is_unchanged_by_this_phase(adopted):
    """11C adds no column to adopted_product_revalidations."""
    snapshot(adopted.adopted_product_id)

    with sqlite3.connect(revalidation_service._db_path()) as conn:
        conn.row_factory = sqlite3.Row
        columns = {
            r["name"]
            for r in conn.execute(
                "PRAGMA table_info(adopted_product_revalidations)"
            )
        }

    assert columns == set(revalidation_service._COLUMNS)


def test_the_policy_engine_source_is_untouched():
    """No rule was added, removed, or reordered for external products."""
    import pathlib

    from app.policy import policy_engine

    source = pathlib.Path(policy_engine.__file__).read_text(encoding="utf-8")

    for foreign in (
        "adopted",
        "external",
        "revalidat",
        "discovery",
        "merchant",
        "serper",
        "channel3",
    ):
        assert foreign.lower() not in source.lower(), (
            f"policy engine must stay product-type agnostic: {foreign}"
        )
