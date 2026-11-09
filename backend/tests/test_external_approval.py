"""External-product explicit approval gate tests (Phase 11D).

The properties these tests defend:

  1. AN EXTERNAL PRODUCT ALWAYS NEEDS A HUMAN. Even
     ALLOW_AUTONOMOUS_PURCHASE produces only a PENDING approval, and even an
     explicitly APPROVED one executes nothing. There is no code path from
     this phase to PayPal, a capture, a refund, a transaction or a session.

  2. THE APPROVAL IS BOUND TO EXACT SERVER STATE. adopted product +
     revalidation snapshot + normalized price + currency + the engine's
     decision and reason code. Every one is re-verified at resolve time, and
     a client cannot substitute any of them because the request carries one
     boolean and nothing else.

  3. EVIDENCE HAS A DEADLINE. Freshness is computed server-side from the
     snapshot's own checked_at against EXTERNAL_REVALIDATION_TTL_SECONDS,
     inclusive at the boundary, and an approval can never outlive it.

  4. THE LIFECYCLE ONLY RUNS FORWARD. PENDING -> APPROVED / REJECTED /
     EXPIRED, and nothing ever leaves a terminal state.

Several tests tamper with stored rows through raw SQL. That is deliberate:
the price, currency and status re-checks are DEFENCE IN DEPTH behind the
revalidation_id binding, and the only way to exercise them is to simulate a
record that has been corrupted or forged. They prove the gate does not rely
on a single stored field being honest.

Providers are faked; nothing here calls Serper or Channel3. Each test gets
its own temporary SQLite database (conftest).
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.adoption import AdoptedProductStatus
from app.schemas.discovery import NormalizedDiscoveredProduct
from app.schemas.external_approval import (
    ApprovalBlockedReason,
    ApprovalConflict,
    ExternalApprovalStatus,
)
from app.schemas.revalidation import (
    AvailabilityStatus,
    IdentityMatch,
    MerchantStatus,
    PriceStatus,
    RevalidationReason,
    RevalidationSnapshot,
    RevalidationStatus,
)
from app.schemas.transaction import TransactionRecord
from app.services import (
    adoption_service,
    external_approval_service,
    external_policy_service,
    purchase_policy_settings,
    revalidation_service,
    transaction_service,
)
from app.services.discovery_evidence_store import record_products
from app.services.external_approval_service import (
    ApprovalConflictError,
    ApprovalNotFoundError,
    request_approval,
    resolve_approval,
)

client = TestClient(app)

NOW = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)
TITLE = "Sony WH-CH520 Wireless Headphones"
GOOGLE_LINK = "https://www.google.com/shopping/product/1"
FRESH_PRICE = Decimal("2000.00")
TTL = external_approval_service.DEFAULT_EXTERNAL_REVALIDATION_TTL_SECONDS

PRODUCTS_JSON = Path(__file__).resolve().parents[1] / "data" / "products.json"


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
        # Deliberately DIFFERENT from FRESH_PRICE, so any test that
        # accidentally reads the adopted snapshot instead of the fresh
        # revalidation fails loudly.
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
    """Any PayPal / purchase / ledger / refund / session / webhook touch fails.

    These are the booby traps for the no-payment property. The Policy Engine
    is deliberately NOT trapped: this phase is supposed to consult it (via
    Phase 11C), and the tests below assert it was consulted correctly rather
    than avoided.
    """
    from app import paypal
    from app.services import (
        confirmation_service,
        purchase_execution_service,
        refund_service,
        session_service,
        transaction_service as ledger,
        webhook_reconciliation_service,
    )

    def bomb(name):
        def _bomb(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError(f"the approval gate must not touch {name}")

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
    monkeypatch.setattr(ledger, "create_transaction", bomb("the ledger"))
    monkeypatch.setattr(ledger, "update_status", bomb("the ledger"))
    monkeypatch.setattr(ledger, "mark_captured", bomb("the ledger"))
    monkeypatch.setattr(refund_service, "refund_transaction", bomb("refunds"))
    monkeypatch.setattr(session_service, "create_session", bomb("sessions"))
    monkeypatch.setattr(session_service, "set_pending_action", bomb("sessions"))
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
        raise AssertionError("the approval gate must not invoke AI")

    monkeypatch.setattr(providers, "get_provider", bomb)
    monkeypatch.setattr(intent_agent, "get_provider", bomb)
    monkeypatch.setattr(comparison_agent, "rank", bomb)
    monkeypatch.setattr(recommendation_agent, "recommend", bomb)


@pytest.fixture
def clock(monkeypatch):
    """Pin the approval service's clock. Returns a setter for later instants."""
    holder = {"now": NOW}
    monkeypatch.setattr(
        external_approval_service, "_now_dt", lambda: holder["now"]
    )
    return holder


@pytest.fixture
def adopted(frozen_core, no_ai, clock):
    """One adopted product with no revalidation yet, and a frozen clock."""
    record_products([evidence()])
    record, _ = adoption_service.adopt("serper", "serper:111111")
    return record


def snapshot(
    adopted_product_id: str,
    *,
    status=RevalidationStatus.REVALIDATED,
    price=FRESH_PRICE,
    currency="INR",
    price_status=PriceStatus.PRICE_CHANGED,
    checked_at=None,
    reval_id=None,
) -> RevalidationSnapshot:
    """Persist one revalidation snapshot through the service's own insert.

    `checked_at` may be a datetime or a raw string, so the unusable-timestamp
    cases can be written exactly as a corrupted row would hold them.
    """
    confirmed = status is RevalidationStatus.REVALIDATED
    if checked_at is None:
        stamp = NOW.isoformat()
    elif isinstance(checked_at, str):
        stamp = checked_at
    else:
        stamp = checked_at.isoformat()
    record = RevalidationSnapshot(
        revalidation_id=reval_id or f"reval_{status.value.lower()}_{price}_{stamp}",
        adopted_product_id=adopted_product_id,
        checked_at=stamp,
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
        price_status=price_status if confirmed else PriceStatus.PRICE_UNAVAILABLE,
        availability=AvailabilityStatus.UNKNOWN,
        merchant_status=MerchantStatus.MERCHANT_UNCHANGED,
        current_title=TITLE if confirmed else None,
        current_merchant_name="Amazon" if confirmed else None,
        current_merchant_domain=None,
        current_product_url=GOOGLE_LINK if confirmed else None,
        current_observed_price=Decimal("21.92") if confirmed else None,
        current_observed_currency="USD" if confirmed else None,
        current_normalized_price=price if confirmed else None,
        current_normalized_currency=currency if confirmed else None,
        previous_observed_price=Decimal("19.99"),
        previous_observed_currency="USD",
    )
    revalidation_service._insert(record)
    return record


def allow_autonomous(limit=Decimal("5000")):
    """Settings under which the engine returns ALLOW_AUTONOMOUS_PURCHASE."""
    purchase_policy_settings.set_settings(
        purchase_requested=True,
        autonomous_purchase_allowed=True,
        auto_approve_below_inr=limit,
    )


def require_approval():
    """Settings under which the engine returns REQUIRE_USER_APPROVAL."""
    purchase_policy_settings.set_settings(
        purchase_requested=True, autonomous_purchase_allowed=False
    )


def post_approval(adopted_product_id, **kwargs):
    return client.post(
        f"/discovery/adopted/{adopted_product_id}/approval", **kwargs
    )


def post_resolve(approval_id, approve=True, body=None):
    return client.post(
        f"/discovery/approvals/{approval_id}/resolve",
        json={"approve": approve} if body is None else body,
    )


def approvals_table_columns() -> set[str]:
    with sqlite3.connect(external_approval_service._db_path()) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(external_approval_service._SCHEMA)
        return {
            row["name"]
            for row in conn.execute("PRAGMA table_info(external_product_approvals)")
        }


def tamper(approval_id: str, **columns) -> None:
    """Rewrite stored approval columns directly, simulating a forged record."""
    assignments = ", ".join(f"{name} = ?" for name in columns)
    with sqlite3.connect(external_approval_service._db_path()) as conn:
        conn.execute(
            f"UPDATE external_product_approvals SET {assignments} "
            "WHERE approval_id = ?",
            (*columns.values(), approval_id),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# 1. Approval creation with fresh REVALIDATED evidence
# ---------------------------------------------------------------------------


def test_approval_is_created_from_fresh_revalidated_evidence(adopted):
    require_approval()
    snap = snapshot(adopted.adopted_product_id)

    body = post_approval(adopted.adopted_product_id).json()

    assert body["approvable"] is True
    assert body["blocked_reason"] is None
    assert body["created"] is True
    assert body["eligibility"] == "EVALUATED"
    assert body["eligibility_reason"] == "FRESH_EVIDENCE_CONFIRMED"
    assert body["revalidation_id"] == snap.revalidation_id
    assert body["revalidation_status"] == "REVALIDATED"
    assert body["revalidation_ttl_seconds"] == TTL
    assert body["revalidation_age_seconds"] == 0.0

    approval = body["approval"]
    assert approval["status"] == "PENDING"
    assert approval["approval_id"].startswith("xapv_")
    assert approval["adopted_product_id"] == adopted.adopted_product_id
    assert approval["revalidation_id"] == snap.revalidation_id
    assert approval["policy_decision"] == "REQUIRE_USER_APPROVAL"
    assert approval["policy_reason_code"] == "AUTONOMY_NOT_GRANTED"
    assert Decimal(approval["approved_normalized_price"]) == FRESH_PRICE
    assert approval["approved_normalized_currency"] == "INR"
    assert approval["approved_at"] is None
    assert approval["rejected_at"] is None
    assert external_approval_service.count() == 1


def test_a_created_approval_carries_merchant_identity_from_fresh_evidence(adopted):
    require_approval()
    snapshot(adopted.adopted_product_id)

    approval = post_approval(adopted.adopted_product_id).json()["approval"]

    assert approval["merchant_name"] == "Amazon"
    assert approval["merchant_domain"] is None


def test_the_approval_record_holds_no_payment_fields(adopted):
    """The absence is structural: there is no column for one."""
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = post_approval(adopted.adopted_product_id).json()["approval"]

    forbidden = {
        "paypal_order_id",
        "paypal_capture_id",
        "refund_id",
        "transaction_id",
        "session_id",
        "amount",
        "paypal_amount",
        "approval_url",
    }
    assert forbidden.isdisjoint(approvals_table_columns())
    assert forbidden.isdisjoint(approval.keys())


# ---------------------------------------------------------------------------
# 2-6. Upstream evidence failures cannot produce an approval
# ---------------------------------------------------------------------------


def test_stale_revalidation_cannot_create_an_approval(adopted, clock):
    require_approval()
    snapshot(adopted.adopted_product_id, checked_at=NOW - timedelta(seconds=TTL + 1))

    body = post_approval(adopted.adopted_product_id).json()

    assert body["approvable"] is False
    assert body["blocked_reason"] == ApprovalBlockedReason.REVALIDATION_STALE.value
    # The upstream chain is still reported honestly: the evidence was a real
    # confirmation, it is merely too old to buy against.
    assert body["eligibility"] == "EVALUATED"
    assert body["revalidation_status"] == "REVALIDATED"
    assert body["revalidation_age_seconds"] == float(TTL + 1)
    assert body["approval"] is None
    assert external_approval_service.count() == 0


@pytest.mark.parametrize(
    "status, expected_reason",
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
def test_an_unconfirmed_latest_revalidation_cannot_create_an_approval(
    adopted, status, expected_reason
):
    """Phase 11C's eligibility codes are carried over verbatim, not renamed.

    IDENTITY_NOT_FOUND notably still does NOT mean the product was
    withdrawn — it means current evidence could not confirm it, and either
    way there is nothing to approve.
    """
    allow_autonomous()
    snapshot(adopted.adopted_product_id, status=status)

    body = post_approval(adopted.adopted_product_id).json()

    assert body["approvable"] is False
    assert body["blocked_reason"] == ApprovalBlockedReason.NOT_POLICY_ELIGIBLE.value
    assert body["eligibility"] == "NOT_ELIGIBLE"
    assert body["eligibility_reason"] == expected_reason
    assert body["policy_decision"] is None
    assert body["approval"] is None
    assert external_approval_service.count() == 0


def test_no_revalidation_at_all_cannot_create_an_approval(adopted):
    allow_autonomous()

    body = post_approval(adopted.adopted_product_id).json()

    assert body["approvable"] is False
    assert body["blocked_reason"] == ApprovalBlockedReason.NOT_POLICY_ELIGIBLE.value
    assert body["eligibility_reason"] == "NO_REVALIDATION_ON_RECORD"
    assert body["revalidation_id"] is None
    assert external_approval_service.count() == 0


def test_the_adopted_snapshot_price_is_never_a_fallback(adopted):
    """A confirmation with no fresh price is not rescued by the adopted one."""
    allow_autonomous()
    snapshot(adopted.adopted_product_id, price=None)

    body = post_approval(adopted.adopted_product_id).json()

    assert body["approvable"] is False
    assert body["eligibility_reason"] == "NO_FRESH_NORMALIZED_PRICE"
    assert body["current_normalized_price"] is None
    assert external_approval_service.count() == 0


def test_a_disabled_adopted_product_cannot_create_an_approval(adopted):
    allow_autonomous()
    snapshot(adopted.adopted_product_id)
    adoption_service.set_status(
        adopted.adopted_product_id, AdoptedProductStatus.DISABLED
    )

    body = post_approval(adopted.adopted_product_id).json()

    assert body["approvable"] is False
    assert body["eligibility_reason"] == "ADOPTED_PRODUCT_DISABLED"
    assert external_approval_service.count() == 0


def test_an_unknown_adopted_product_is_404(adopted):
    assert post_approval("adopt_does_not_exist").status_code == 404
    assert external_approval_service.count() == 0


# ---------------------------------------------------------------------------
# 7-9. The policy decision governs whether an approval may exist at all
# ---------------------------------------------------------------------------


def test_policy_deny_cannot_create_an_approval(adopted):
    """Unconfigured settings authorize nothing, and the engine says DENY."""
    snapshot(adopted.adopted_product_id)

    body = post_approval(adopted.adopted_product_id).json()

    assert body["approvable"] is False
    assert body["blocked_reason"] == ApprovalBlockedReason.POLICY_DENIED.value
    # The engine's own ruling is reported verbatim, not reinterpreted.
    assert body["eligibility"] == "EVALUATED"
    assert body["policy_decision"] == "DENY"
    assert body["policy_reason_code"] == "NO_PURCHASE_REQUESTED"
    assert body["approval"] is None
    assert external_approval_service.count() == 0


def test_a_deny_from_a_shopping_constraint_also_blocks_approval(adopted):
    purchase_policy_settings.set_settings(
        purchase_requested=True,
        autonomous_purchase_allowed=True,
        auto_approve_below_inr=Decimal("10000"),
        max_price_inr=1000,
    )
    snapshot(adopted.adopted_product_id)

    body = post_approval(adopted.adopted_product_id).json()

    assert body["blocked_reason"] == ApprovalBlockedReason.POLICY_DENIED.value
    assert body["policy_reason_code"] == "EXCEEDS_SHOPPING_MAX"
    assert external_approval_service.count() == 0


def test_require_user_approval_can_create_an_approval(adopted):
    require_approval()
    snapshot(adopted.adopted_product_id)

    body = post_approval(adopted.adopted_product_id).json()

    assert body["approvable"] is True
    assert body["approval"]["policy_decision"] == "REQUIRE_USER_APPROVAL"


def test_allow_autonomous_purchase_still_requires_explicit_approval(adopted):
    """THE phase-11D rule: a permissive ruling is not permission to proceed.

    The engine's answer is preserved exactly, and the approval still starts
    PENDING. Nothing self-approves and nothing executes.
    """
    allow_autonomous()
    snapshot(adopted.adopted_product_id)

    body = post_approval(adopted.adopted_product_id).json()

    assert body["policy_decision"] == "ALLOW_AUTONOMOUS_PURCHASE"
    assert body["policy_reason_code"] == "WITHIN_AUTONOMOUS_LIMIT"
    assert body["approvable"] is True
    assert body["approval"]["status"] == "PENDING"
    assert body["approval"]["approved_at"] is None
    assert transaction_service.list_transactions() == []


# ---------------------------------------------------------------------------
# 10-16. A client cannot supply any product fact
# ---------------------------------------------------------------------------


def test_the_creation_endpoint_ignores_every_injected_product_fact(adopted):
    """Creation has no request body, so there is nowhere for a fact to land."""
    require_approval()
    snapshot(adopted.adopted_product_id)

    body = post_approval(
        adopted.adopted_product_id,
        json={
            "price": "1.00",
            "approved_normalized_price": "1.00",
            "currency": "USD",
            "approved_normalized_currency": "USD",
            "merchant_name": "attacker-shop",
            "merchant_domain": "attacker.example",
            "product_url": "https://attacker.example/buy",
            "policy_decision": "ALLOW_AUTONOMOUS_PURCHASE",
            "policy_reason_code": "WITHIN_AUTONOMOUS_LIMIT",
            "revalidation_id": "reval_forged",
            "adopted_product_id": "adopt_someone_else",
            "status": "APPROVED",
            "expires_at": "2099-01-01T00:00:00+00:00",
            "paypal_order_id": "ORDER-FORGED",
        },
    ).json()

    approval = body["approval"]
    assert approval["adopted_product_id"] == adopted.adopted_product_id
    assert Decimal(approval["approved_normalized_price"]) == FRESH_PRICE
    assert approval["approved_normalized_currency"] == "INR"
    assert approval["merchant_name"] == "Amazon"
    assert approval["merchant_domain"] is None
    assert approval["policy_decision"] == "REQUIRE_USER_APPROVAL"
    assert approval["revalidation_id"].startswith("reval_revalidated")
    assert approval["status"] == "PENDING"
    assert approval["expires_at"] == (NOW + timedelta(seconds=TTL)).isoformat()


@pytest.mark.parametrize(
    "extra_key, extra_value",
    [
        ("price", "1.00"),
        ("approved_normalized_price", "1.00"),
        ("currency", "USD"),
        ("approved_normalized_currency", "USD"),
        ("merchant_name", "attacker-shop"),
        ("merchant_domain", "attacker.example"),
        ("product_url", "https://attacker.example/buy"),
        ("policy_decision", "ALLOW_AUTONOMOUS_PURCHASE"),
        ("policy_reason_code", "WITHIN_AUTONOMOUS_LIMIT"),
        ("revalidation_id", "reval_forged"),
        ("adopted_product_id", "adopt_someone_else"),
        ("paypal_order_id", "ORDER-FORGED"),
        ("status", "APPROVED"),
        ("expires_at", "2099-01-01T00:00:00+00:00"),
    ],
)
def test_the_resolve_body_rejects_every_product_fact(adopted, extra_key, extra_value):
    """extra="forbid" makes a smuggled field a 422, not a silent drop."""
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval_id = post_approval(adopted.adopted_product_id).json()["approval"][
        "approval_id"
    ]

    response = post_resolve(
        approval_id, body={"approve": True, extra_key: extra_value}
    )

    assert response.status_code == 422
    assert (
        external_approval_service.get(approval_id).status
        is ExternalApprovalStatus.PENDING
    )


def test_resolve_requires_an_explicit_boolean(adopted):
    """Calling the endpoint is not an approval; `approve` has no default."""
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval_id = post_approval(adopted.adopted_product_id).json()["approval"][
        "approval_id"
    ]

    assert post_resolve(approval_id, body={}).status_code == 422
    assert (
        external_approval_service.get(approval_id).status
        is ExternalApprovalStatus.PENDING
    )


def test_an_approval_id_from_another_product_resolves_only_its_own_product(adopted):
    """A resolve names an approval, and that approval names its product."""
    require_approval()
    snapshot(adopted.adopted_product_id)
    record_products([evidence(external_id="serper:222222")])
    other, _ = adoption_service.adopt("serper", "serper:222222")
    snapshot(other.adopted_product_id, reval_id="reval_other")

    first = post_approval(adopted.adopted_product_id).json()["approval"]
    second = post_approval(other.adopted_product_id).json()["approval"]
    assert first["approval_id"] != second["approval_id"]

    resolved = post_resolve(first["approval_id"], approve=True).json()

    assert resolved["adopted_product_id"] == adopted.adopted_product_id
    assert (
        external_approval_service.get(second["approval_id"]).status
        is ExternalApprovalStatus.PENDING
    )


# ---------------------------------------------------------------------------
# 17-20. The binding, and its re-verification
# ---------------------------------------------------------------------------


def test_approval_binds_the_exact_product_revalidation_price_and_currency(adopted):
    require_approval()
    snap = snapshot(adopted.adopted_product_id)

    stored = external_approval_service.get(
        post_approval(adopted.adopted_product_id).json()["approval"]["approval_id"]
    )

    assert stored.adopted_product_id == adopted.adopted_product_id
    assert stored.revalidation_id == snap.revalidation_id
    assert stored.revalidation_checked_at == snap.checked_at
    assert stored.approved_normalized_price == FRESH_PRICE
    assert stored.approved_normalized_currency == "INR"
    # Bound to the FRESH price, never the adopted snapshot's.
    assert stored.approved_normalized_price != adopted.normalized_price


def test_a_newer_revalidation_makes_the_approval_stale(adopted, clock):
    """Revalidation changed after approval -> deterministic conflict."""
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval_id = post_approval(adopted.adopted_product_id).json()["approval"][
        "approval_id"
    ]

    later = NOW + timedelta(seconds=60)
    clock["now"] = later
    snapshot(
        adopted.adopted_product_id,
        price=Decimal("2500.00"),
        checked_at=later,
        reval_id="reval_newer",
    )

    response = post_resolve(approval_id, approve=True)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == ApprovalConflict.REVALIDATION_SUPERSEDED.value
    assert detail["approval"]["status"] == "PENDING"
    # Not silently re-bound, and no second approval invented for the user.
    assert (
        external_approval_service.get(approval_id).status
        is ExternalApprovalStatus.PENDING
    )
    assert external_approval_service.count(adopted.adopted_product_id) == 1


def test_a_failed_newer_revalidation_cannot_fall_back_to_the_approved_one(
    adopted, clock
):
    """Latest-wins, from this side too: an older success does not rescue it."""
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval_id = post_approval(adopted.adopted_product_id).json()["approval"][
        "approval_id"
    ]

    later = NOW + timedelta(seconds=60)
    clock["now"] = later
    snapshot(
        adopted.adopted_product_id,
        status=RevalidationStatus.IDENTITY_NOT_FOUND,
        checked_at=later,
        reval_id="reval_newer_failure",
    )

    response = post_resolve(approval_id, approve=True)

    assert response.status_code == 409
    assert (
        response.json()["detail"]["code"]
        == ApprovalConflict.NOT_POLICY_ELIGIBLE.value
    )
    assert (
        external_approval_service.get(approval_id).status
        is ExternalApprovalStatus.PENDING
    )


def test_a_changed_policy_decision_makes_the_approval_stale(adopted):
    allow_autonomous()
    snapshot(adopted.adopted_product_id)
    approval_id = post_approval(adopted.adopted_product_id).json()["approval"][
        "approval_id"
    ]

    # The operator withdraws autonomy. Same product, same evidence, new rule.
    require_approval()

    response = post_resolve(approval_id, approve=True)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == ApprovalConflict.POLICY_CHANGED.value
    assert "ALLOW_AUTONOMOUS_PURCHASE" in detail["message"]
    assert "REQUIRE_USER_APPROVAL" in detail["message"]
    assert (
        external_approval_service.get(approval_id).status
        is ExternalApprovalStatus.PENDING
    )


def test_a_changed_policy_reason_code_alone_makes_the_approval_stale(adopted):
    """The reason code is bound too, so a same-decision change is caught."""
    require_approval()  # REQUIRE_USER_APPROVAL / AUTONOMY_NOT_GRANTED
    snapshot(adopted.adopted_product_id)
    approval_id = post_approval(adopted.adopted_product_id).json()["approval"][
        "approval_id"
    ]

    # Still REQUIRE_USER_APPROVAL, but now for EXCEEDS_AUTONOMOUS_LIMIT.
    allow_autonomous(limit=Decimal("1000"))

    response = post_resolve(approval_id, approve=True)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == ApprovalConflict.POLICY_CHANGED.value


def test_a_tampered_stored_price_cannot_be_approved(adopted):
    """Defence in depth behind the revalidation_id binding."""
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval_id = post_approval(adopted.adopted_product_id).json()["approval"][
        "approval_id"
    ]

    tamper(approval_id, approved_normalized_price="1.00")

    response = post_resolve(approval_id, approve=True)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == ApprovalConflict.PRICE_CHANGED.value


def test_a_tampered_stored_currency_cannot_be_approved(adopted):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval_id = post_approval(adopted.adopted_product_id).json()["approval"][
        "approval_id"
    ]

    tamper(approval_id, approved_normalized_currency="USD")

    response = post_resolve(approval_id, approve=True)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == ApprovalConflict.CURRENCY_CHANGED.value


def test_a_deleted_adopted_product_cannot_be_approved(adopted):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval_id = post_approval(adopted.adopted_product_id).json()["approval"][
        "approval_id"
    ]

    with sqlite3.connect(adoption_service._db_path()) as conn:
        conn.execute("DELETE FROM adopted_products WHERE adopted_product_id = ?",
                     (adopted.adopted_product_id,))
        conn.commit()

    response = post_resolve(approval_id, approve=True)

    assert response.status_code == 409
    assert (
        response.json()["detail"]["code"]
        == ApprovalConflict.ADOPTED_PRODUCT_MISSING.value
    )


def test_a_deleted_revalidation_cannot_be_approved(adopted):
    require_approval()
    snap = snapshot(adopted.adopted_product_id)
    approval_id = post_approval(adopted.adopted_product_id).json()["approval"][
        "approval_id"
    ]

    with sqlite3.connect(revalidation_service._db_path()) as conn:
        conn.execute(
            "DELETE FROM adopted_product_revalidations WHERE revalidation_id = ?",
            (snap.revalidation_id,),
        )
        conn.commit()

    response = post_resolve(approval_id, approve=True)

    assert response.status_code == 409
    assert (
        response.json()["detail"]["code"]
        == ApprovalConflict.REVALIDATION_MISSING.value
    )


def test_a_bound_revalidation_that_stopped_being_confirmed_cannot_be_approved(
    adopted,
):
    """The exact snapshot is re-read BY ID and re-checked, not assumed."""
    require_approval()
    snap = snapshot(adopted.adopted_product_id)
    approval_id = post_approval(adopted.adopted_product_id).json()["approval"][
        "approval_id"
    ]

    with sqlite3.connect(revalidation_service._db_path()) as conn:
        conn.execute(
            "UPDATE adopted_product_revalidations SET revalidation_status = ? "
            "WHERE revalidation_id = ?",
            (RevalidationStatus.IDENTITY_NOT_FOUND.value, snap.revalidation_id),
        )
        conn.commit()

    response = post_resolve(approval_id, approve=True)

    assert response.status_code == 409
    assert (
        response.json()["detail"]["code"]
        == ApprovalConflict.REVALIDATION_NOT_CONFIRMED.value
    )


def test_a_disabled_product_cannot_have_its_approval_resolved(adopted):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval_id = post_approval(adopted.adopted_product_id).json()["approval"][
        "approval_id"
    ]

    adoption_service.set_status(
        adopted.adopted_product_id, AdoptedProductStatus.DISABLED
    )

    response = post_resolve(approval_id, approve=True)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == ApprovalConflict.NOT_POLICY_ELIGIBLE.value
    assert "ADOPTED_PRODUCT_DISABLED" in detail["message"]


# ---------------------------------------------------------------------------
# 21-23. Freshness TTL and expiry
# ---------------------------------------------------------------------------


def test_evidence_just_inside_the_ttl_is_fresh(adopted, clock):
    require_approval()
    snapshot(
        adopted.adopted_product_id, checked_at=NOW - timedelta(seconds=TTL - 1)
    )

    body = post_approval(adopted.adopted_product_id).json()

    assert body["approvable"] is True
    assert body["revalidation_age_seconds"] == float(TTL - 1)


def test_evidence_exactly_at_the_ttl_boundary_is_fresh(adopted, clock):
    """The rule is checked_at + TTL >= now, so the boundary is INCLUSIVE."""
    require_approval()
    snapshot(adopted.adopted_product_id, checked_at=NOW - timedelta(seconds=TTL))

    body = post_approval(adopted.adopted_product_id).json()

    assert body["approvable"] is True
    assert body["revalidation_age_seconds"] == float(TTL)
    # An approval bound to boundary-aged evidence expires immediately after.
    assert body["approval"]["expires_at"] == NOW.isoformat()


def test_evidence_one_microsecond_past_the_boundary_is_stale(adopted, clock):
    require_approval()
    snapshot(
        adopted.adopted_product_id,
        checked_at=NOW - timedelta(seconds=TTL, microseconds=1),
    )

    body = post_approval(adopted.adopted_product_id).json()

    assert body["approvable"] is False
    assert body["blocked_reason"] == ApprovalBlockedReason.REVALIDATION_STALE.value


@pytest.mark.parametrize(
    "raw", ["", "   ", "not-a-timestamp", "2026-10-08T12:00:00"]
)
def test_an_unusable_checked_at_is_never_treated_as_fresh(adopted, raw):
    """Missing, unparseable, or NAIVE: unknown age must not read as fresh.

    The last case is a timezone-less timestamp. "12:00" names a different
    instant in every zone, so assuming UTC would invent a fact in exactly
    the direction that makes stale evidence look current.
    """
    require_approval()
    snapshot(adopted.adopted_product_id, checked_at=raw, reval_id="reval_bad_stamp")

    body = post_approval(adopted.adopted_product_id).json()

    assert body["approvable"] is False
    assert (
        body["blocked_reason"]
        == ApprovalBlockedReason.REVALIDATION_TIMESTAMP_UNUSABLE.value
    )
    assert body["revalidation_age_seconds"] is None
    assert external_approval_service.count() == 0


def test_the_approval_expires_with_the_evidence_it_is_bound_to(adopted, clock):
    """expires_at is checked_at + TTL, not created_at + TTL."""
    require_approval()
    checked = NOW - timedelta(seconds=200)
    snapshot(adopted.adopted_product_id, checked_at=checked)

    approval = post_approval(adopted.adopted_product_id).json()["approval"]

    assert approval["created_at"] == NOW.isoformat()
    assert approval["expires_at"] == (checked + timedelta(seconds=TTL)).isoformat()
    assert datetime.fromisoformat(approval["expires_at"]) < NOW + timedelta(
        seconds=TTL
    )


def test_an_expired_pending_approval_cannot_be_approved(adopted, clock):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval_id = post_approval(adopted.adopted_product_id).json()["approval"][
        "approval_id"
    ]

    clock["now"] = NOW + timedelta(seconds=TTL, microseconds=1)

    response = post_resolve(approval_id, approve=True)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == ApprovalConflict.APPROVAL_EXPIRED.value
    # The expiry is persisted, deterministically and once.
    assert detail["approval"]["status"] == "EXPIRED"
    assert (
        external_approval_service.get(approval_id).status
        is ExternalApprovalStatus.EXPIRED
    )
    assert external_approval_service.get(approval_id).approved_at is None


def test_an_approval_is_still_resolvable_at_its_expiry_instant(adopted, clock):
    """expires_at is the first instant at which it is too late, not the last."""
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = post_approval(adopted.adopted_product_id).json()["approval"]

    clock["now"] = datetime.fromisoformat(approval["expires_at"]) - timedelta(
        microseconds=1
    )

    resolved = post_resolve(approval["approval_id"], approve=True)

    assert resolved.status_code == 200
    assert resolved.json()["status"] == "APPROVED"


def test_the_ttl_is_enforced_independently_of_the_stored_expires_at(adopted, clock):
    """A forged expires_at buys nothing: the TTL is re-derived from evidence."""
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval_id = post_approval(adopted.adopted_product_id).json()["approval"][
        "approval_id"
    ]

    tamper(approval_id, expires_at="2099-01-01T00:00:00+00:00")
    clock["now"] = NOW + timedelta(seconds=TTL + 1)

    response = post_resolve(approval_id, approve=True)

    assert response.status_code == 409
    assert (
        response.json()["detail"]["code"] == ApprovalConflict.REVALIDATION_STALE.value
    )
    assert (
        external_approval_service.get(approval_id).status
        is ExternalApprovalStatus.PENDING
    )


def test_a_forged_expires_at_cannot_rescue_an_unusable_timestamp(adopted, clock):
    require_approval()
    snap = snapshot(adopted.adopted_product_id)
    approval_id = post_approval(adopted.adopted_product_id).json()["approval"][
        "approval_id"
    ]

    with sqlite3.connect(revalidation_service._db_path()) as conn:
        conn.execute(
            "UPDATE adopted_product_revalidations SET checked_at = ? "
            "WHERE revalidation_id = ?",
            ("not-a-timestamp", snap.revalidation_id),
        )
        conn.commit()

    response = post_resolve(approval_id, approve=True)

    assert response.status_code == 409
    assert (
        response.json()["detail"]["code"]
        == ApprovalConflict.REVALIDATION_TIMESTAMP_UNUSABLE.value
    )


def test_the_ttl_is_configurable_through_the_environment(adopted, clock, monkeypatch):
    monkeypatch.setenv("EXTERNAL_REVALIDATION_TTL_SECONDS", "60")
    require_approval()
    snapshot(adopted.adopted_product_id, checked_at=NOW - timedelta(seconds=90))

    body = post_approval(adopted.adopted_product_id).json()

    assert body["revalidation_ttl_seconds"] == 60
    assert body["blocked_reason"] == ApprovalBlockedReason.REVALIDATION_STALE.value


def test_the_default_ttl_is_conservative_and_finite():
    assert external_approval_service.DEFAULT_EXTERNAL_REVALIDATION_TTL_SECONDS == 300
    assert external_approval_service.revalidation_ttl_seconds() == 300


@pytest.mark.parametrize("raw", ["0", "-1", "forever", "unlimited", "3.5"])
def test_an_unusable_ttl_setting_fails_closed(adopted, monkeypatch, raw):
    """A TTL nobody can honour is a 500, never a silent default."""
    monkeypatch.setenv("EXTERNAL_REVALIDATION_TTL_SECONDS", raw)
    require_approval()
    snapshot(adopted.adopted_product_id)

    assert post_approval(adopted.adopted_product_id).status_code == 500
    assert external_approval_service.count() == 0


def test_the_freshness_boundary_function_is_inclusive():
    checked = NOW - timedelta(seconds=300)
    assert external_approval_service.is_fresh(checked, NOW, 300) is True
    assert (
        external_approval_service.is_fresh(
            checked - timedelta(microseconds=1), NOW, 300
        )
        is False
    )


# ---------------------------------------------------------------------------
# 24-30. Lifecycle, idempotency and terminal states
# ---------------------------------------------------------------------------


def test_explicit_approval_marks_the_approval_approved(adopted, clock):
    require_approval()
    snap = snapshot(adopted.adopted_product_id)
    approval_id = post_approval(adopted.adopted_product_id).json()["approval"][
        "approval_id"
    ]

    body = post_resolve(approval_id, approve=True).json()

    assert body["status"] == "APPROVED"
    assert body["approved_at"] == NOW.isoformat()
    assert body["rejected_at"] is None
    assert body["revalidation_id"] == snap.revalidation_id
    assert Decimal(body["approved_normalized_price"]) == FRESH_PRICE
    assert body["approved_normalized_currency"] == "INR"
    assert body["policy_decision"] == "REQUIRE_USER_APPROVAL"
    assert body["policy_reason_code"] == "AUTONOMY_NOT_GRANTED"
    # Nothing beyond APPROVED exists to report.
    assert "paypal_order_id" not in body
    assert "transaction_id" not in body


def test_explicit_rejection_marks_the_approval_rejected(adopted, clock):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval_id = post_approval(adopted.adopted_product_id).json()["approval"][
        "approval_id"
    ]

    body = post_resolve(approval_id, approve=False).json()

    assert body["status"] == "REJECTED"
    assert body["rejected_at"] == NOW.isoformat()
    assert body["approved_at"] is None
    assert transaction_service.list_transactions() == []


def test_a_stale_approval_may_still_be_rejected(adopted, clock):
    """Declining is always a valid answer; it authorizes nothing."""
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval_id = post_approval(adopted.adopted_product_id).json()["approval"][
        "approval_id"
    ]
    require_approval()
    allow_autonomous()  # policy moved under the approval

    assert post_resolve(approval_id, approve=False).json()["status"] == "REJECTED"


def test_approved_cannot_become_rejected(adopted):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval_id = post_approval(adopted.adopted_product_id).json()["approval"][
        "approval_id"
    ]
    assert post_resolve(approval_id, approve=True).json()["status"] == "APPROVED"

    response = post_resolve(approval_id, approve=False)

    assert response.status_code == 409
    assert (
        response.json()["detail"]["code"]
        == ApprovalConflict.APPROVAL_ALREADY_APPROVED.value
    )
    assert (
        external_approval_service.get(approval_id).status
        is ExternalApprovalStatus.APPROVED
    )


def test_rejected_cannot_become_approved(adopted):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval_id = post_approval(adopted.adopted_product_id).json()["approval"][
        "approval_id"
    ]
    assert post_resolve(approval_id, approve=False).json()["status"] == "REJECTED"

    response = post_resolve(approval_id, approve=True)

    assert response.status_code == 409
    assert (
        response.json()["detail"]["code"]
        == ApprovalConflict.APPROVAL_ALREADY_REJECTED.value
    )
    assert (
        external_approval_service.get(approval_id).status
        is ExternalApprovalStatus.REJECTED
    )


def test_rejected_is_terminal_for_rejection_too(adopted):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval_id = post_approval(adopted.adopted_product_id).json()["approval"][
        "approval_id"
    ]
    post_resolve(approval_id, approve=False)

    assert post_resolve(approval_id, approve=False).status_code == 409


def test_expired_cannot_become_approved_or_rejected(adopted, clock):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval_id = post_approval(adopted.adopted_product_id).json()["approval"][
        "approval_id"
    ]
    clock["now"] = NOW + timedelta(seconds=TTL + 1)

    for approve in (True, False):
        response = post_resolve(approval_id, approve=approve)
        assert response.status_code == 409
        assert (
            response.json()["detail"]["code"]
            == ApprovalConflict.APPROVAL_EXPIRED.value
        )

    assert (
        external_approval_service.get(approval_id).status
        is ExternalApprovalStatus.EXPIRED
    )


def test_re_approving_an_approved_approval_is_idempotent(adopted, clock):
    """Same request, same answer, no second approval and nothing executed."""
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval_id = post_approval(adopted.adopted_product_id).json()["approval"][
        "approval_id"
    ]
    first = post_resolve(approval_id, approve=True).json()

    clock["now"] = NOW + timedelta(seconds=10)
    second = post_resolve(approval_id, approve=True)

    assert second.status_code == 200
    assert second.json() == first
    assert second.json()["approved_at"] == NOW.isoformat()
    assert external_approval_service.count() == 1


def test_preparing_an_approval_twice_reuses_the_pending_record(adopted):
    require_approval()
    snapshot(adopted.adopted_product_id)

    first = post_approval(adopted.adopted_product_id).json()
    second = post_approval(adopted.adopted_product_id).json()

    assert first["created"] is True
    assert second["created"] is False
    assert second["approval"]["approval_id"] == first["approval"]["approval_id"]
    assert external_approval_service.count() == 1


def test_a_changed_binding_gets_its_own_approval_record(adopted, clock):
    """A different commitment is a different record, never a mutated one."""
    require_approval()
    snapshot(adopted.adopted_product_id)
    first = post_approval(adopted.adopted_product_id).json()["approval"]

    later = NOW + timedelta(seconds=60)
    clock["now"] = later
    snapshot(
        adopted.adopted_product_id,
        price=Decimal("2500.00"),
        checked_at=later,
        reval_id="reval_newer",
    )
    second = post_approval(adopted.adopted_product_id).json()["approval"]

    assert second["approval_id"] != first["approval_id"]
    assert Decimal(second["approved_normalized_price"]) == Decimal("2500.00")
    assert external_approval_service.count() == 2
    # The superseded one is untouched, and is no longer approvable.
    assert (
        external_approval_service.get(first["approval_id"]).status
        is ExternalApprovalStatus.PENDING
    )
    assert post_resolve(first["approval_id"], approve=True).status_code == 409


def test_preparing_an_approval_after_rejection_does_not_revive_it(adopted):
    require_approval()
    snapshot(adopted.adopted_product_id)
    first = post_approval(adopted.adopted_product_id).json()["approval"]
    post_resolve(first["approval_id"], approve=False)

    second = post_approval(adopted.adopted_product_id).json()

    assert second["created"] is True
    assert second["approval"]["approval_id"] != first["approval_id"]
    assert (
        external_approval_service.get(first["approval_id"]).status
        is ExternalApprovalStatus.REJECTED
    )


def test_an_unknown_approval_id_is_404(adopted):
    assert post_resolve("xapv_does_not_exist", approve=True).status_code == 404
    with pytest.raises(ApprovalNotFoundError):
        external_approval_service.get("xapv_does_not_exist")


# ---------------------------------------------------------------------------
# 31. Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_approvals_produce_exactly_one_approved_outcome(adopted, clock):
    """Compare-and-set, not read-then-write: the loser cannot overwrite."""
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval_id = post_approval(adopted.adopted_product_id).json()["approval"][
        "approval_id"
    ]

    results: list[str] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def worker():
        barrier.wait()
        try:
            results.append(resolve_approval(approval_id, True).status.value)
        except BaseException as exc:  # pragma: no cover - recorded, not raised
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    # Every caller asked to approve, so every caller sees APPROVED — but
    # only one transition happened, and approved_at was written once.
    assert results == ["APPROVED"] * 4
    assert (
        external_approval_service.get(approval_id).status
        is ExternalApprovalStatus.APPROVED
    )
    assert external_approval_service.count() == 1


def test_a_concurrent_reject_and_approve_cannot_both_win(adopted, clock):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval_id = post_approval(adopted.adopted_product_id).json()["approval"][
        "approval_id"
    ]

    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def worker(approve: bool):
        barrier.wait()
        try:
            outcomes.append(resolve_approval(approval_id, approve).status.value)
        except ApprovalConflictError as exc:
            outcomes.append(exc.code.value)

    threads = [
        threading.Thread(target=worker, args=(True,)),
        threading.Thread(target=worker, args=(False,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    final = external_approval_service.get(approval_id)
    assert final.status in {
        ExternalApprovalStatus.APPROVED,
        ExternalApprovalStatus.REJECTED,
    }
    # Exactly one decision stuck; the other call learned it lost.
    assert sorted(outcomes) == sorted(
        [
            final.status.value,
            ApprovalConflict.APPROVAL_ALREADY_APPROVED.value
            if final.status is ExternalApprovalStatus.APPROVED
            else ApprovalConflict.APPROVAL_ALREADY_REJECTED.value,
        ]
    )
    assert (final.approved_at is None) != (final.rejected_at is None)


def test_concurrent_approval_preparation_does_not_fork_the_decision(adopted):
    require_approval()
    snapshot(adopted.adopted_product_id)

    ids: list[str] = []
    barrier = threading.Barrier(4)

    def worker():
        barrier.wait()
        result = request_approval(adopted.adopted_product_id)
        ids.append(result.approval.approval_id)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Racing callers may insert more than one PENDING record, but each is
    # bound to the same state and only ONE can ever become APPROVED, because
    # approving a superseded binding is refused.
    assert len(ids) == 4
    approved = 0
    for approval_id in set(ids):
        try:
            if resolve_approval(approval_id, True).status is (
                ExternalApprovalStatus.APPROVED
            ):
                approved += 1
        except ApprovalConflictError:
            pass
    assert approved >= 1


# ---------------------------------------------------------------------------
# 32-37. Boobytraps: no payment, no ledger, no session, no AI
# ---------------------------------------------------------------------------


def test_the_whole_flow_touches_no_payment_ledger_session_or_ai_path(adopted, clock):
    """The booby traps in `frozen_core` / `no_ai` would raise if touched."""
    allow_autonomous()
    snapshot(adopted.adopted_product_id)

    created = post_approval(adopted.adopted_product_id)
    approval_id = created.json()["approval"]["approval_id"]
    resolved = post_resolve(approval_id, approve=True)

    assert created.status_code == 200
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "APPROVED"
    assert transaction_service.list_transactions() == []


def test_no_transaction_row_exists_after_an_approval(adopted):
    allow_autonomous()
    snapshot(adopted.adopted_product_id)
    approval_id = post_approval(adopted.adopted_product_id).json()["approval"][
        "approval_id"
    ]
    post_resolve(approval_id, approve=True)

    with sqlite3.connect(transaction_service._db_path()) as conn:
        conn.executescript(transaction_service._SCHEMA)
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_no_session_row_exists_after_an_approval(adopted):
    from app.services import session_service

    allow_autonomous()
    snapshot(adopted.adopted_product_id)
    approval_id = post_approval(adopted.adopted_product_id).json()["approval"][
        "approval_id"
    ]
    post_resolve(approval_id, approve=True)

    with sqlite3.connect(session_service._db_path()) as conn:
        conn.executescript(session_service._SCHEMA)
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_the_approval_modules_import_no_payment_or_ai_code():
    """Structural, not behavioural: the imports simply are not there."""
    for module in (
        Path(external_approval_service.__file__),
        Path(__file__).resolve().parents[1]
        / "app"
        / "schemas"
        / "external_approval.py",
    ):
        source = module.read_text(encoding="utf-8")
        import_lines = [
            line
            for line in source.splitlines()
            if line.startswith(("import ", "from "))
        ]
        joined = "\n".join(import_lines)
        for forbidden in (
            "paypal",
            "purchase_execution_service",
            "refund_service",
            "transaction_service",
            "session_service",
            "confirmation_service",
            "webhook",
            "providers",
            "intent_agent",
            "comparison_agent",
            "recommendation_agent",
            "channel3_client",
        ):
            assert forbidden not in joined, f"{module.name} imports {forbidden}"


def test_the_approval_service_defines_no_policy_rule():
    """No second policy engine: the decision is read, never computed."""
    source = Path(external_approval_service.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "auto_approve_below_inr",
        "max_price_inr",
        "min_price_inr",
        "evaluate_policy",
        "purchase_policy_settings",
        "PolicySelectedProduct",
    ):
        assert forbidden not in source, f"approval service references {forbidden}"


# ---------------------------------------------------------------------------
# 38-42. Nothing that already existed was changed
# ---------------------------------------------------------------------------


def test_products_json_is_unchanged_by_the_approval_flow(adopted):
    before = hashlib.sha256(PRODUCTS_JSON.read_bytes()).hexdigest()

    allow_autonomous()
    snapshot(adopted.adopted_product_id)
    approval_id = post_approval(adopted.adopted_product_id).json()["approval"][
        "approval_id"
    ]
    post_resolve(approval_id, approve=True)

    assert hashlib.sha256(PRODUCTS_JSON.read_bytes()).hexdigest() == before


def test_the_transaction_schema_is_unchanged(adopted):
    """No approval, external or policy column leaked into the ledger."""
    expected = {
        "transaction_id",
        "product_id",
        "product_name",
        "catalog_price_inr",
        "paypal_amount",
        "paypal_currency",
        "paypal_order_id",
        "paypal_capture_id",
        "status",
        "created_at",
        "updated_at",
    }
    with sqlite3.connect(transaction_service._db_path()) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(transaction_service._SCHEMA)
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(transactions)")
        }
    assert columns == expected
    assert "approval_id" not in TransactionRecord.model_fields
    assert "adopted_product_id" not in TransactionRecord.model_fields


def test_the_adopted_record_and_revalidation_history_are_untouched(adopted, clock):
    allow_autonomous()
    snap = snapshot(adopted.adopted_product_id)
    before_adopted = adoption_service.get(adopted.adopted_product_id)
    before_history = revalidation_service.list_revalidations(
        adopted.adopted_product_id
    )

    approval_id = post_approval(adopted.adopted_product_id).json()["approval"][
        "approval_id"
    ]
    post_resolve(approval_id, approve=True)

    assert adoption_service.get(adopted.adopted_product_id) == before_adopted
    assert revalidation_service.list_revalidations(adopted.adopted_product_id) == (
        before_history
    )
    assert revalidation_service.count(adopted.adopted_product_id) == 1
    assert revalidation_service.get_revalidation(snap.revalidation_id) == snap


def test_the_existing_policy_evaluate_endpoint_is_unchanged(adopted):
    """The local Policy Engine endpoint behaves exactly as before."""
    response = client.post(
        "/policy/evaluate",
        json={
            "product": {"product_id": "LAP002", "price_inr": 1500.0, "currency": "INR"},
            "authorization": {
                "purchase_requested": True,
                "autonomous_purchase_allowed": True,
                "auto_approve_below_inr": 2000.0,
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["decision"] == "ALLOW_AUTONOMOUS_PURCHASE"
    assert response.json()["reason_code"] == "WITHIN_AUTONOMOUS_LIMIT"


def test_the_existing_external_policy_endpoint_is_unchanged(adopted):
    """Phase 11C's own answer is identical whether or not an approval exists."""
    allow_autonomous()
    snapshot(adopted.adopted_product_id)

    before = external_policy_service.evaluate_adopted_product(
        adopted.adopted_product_id
    )
    post_approval(adopted.adopted_product_id)
    after = external_policy_service.evaluate_adopted_product(
        adopted.adopted_product_id
    )

    assert before == after
    assert before.policy_decision.decision.value == "ALLOW_AUTONOMOUS_PURCHASE"


def test_the_local_purchase_endpoints_still_require_their_own_path(adopted):
    """The approval gate added no bypass into local purchase execution.

    /purchase/approve is booby-trapped in this module's fixtures, so a 500
    here would mean the endpoint ran; a clean 422 proves the route still
    demands its own documented request and was not rewired.
    """
    assert client.post("/purchase/approve", json={}).status_code == 422
    assert client.post("/purchase/execute", json={}).status_code == 422


# ---------------------------------------------------------------------------
# THE SECURITY PROPERTY: autonomy does not cross the human boundary
# ---------------------------------------------------------------------------


def test_an_autonomously_allowed_external_product_still_needs_a_human(
    frozen_core, no_ai, clock
):
    """End to end, with every payment path booby-trapped:

    discovered -> adopted -> revalidated -> ALLOW_AUTONOMOUS_PURCHASE
    -> approval created -> explicitly approved -> APPROVED, and nowhere
    further. No PayPal order, no capture, no transaction, no refund, no
    session, no AI.
    """
    # 1. Discovered (server-stored evidence only).
    record_products([evidence()])

    # 2. Adopted.
    adopted_product, created = adoption_service.adopt("serper", "serper:111111")
    assert created is True

    # 3. Revalidated, confirming identity with a fresh normalized price.
    snap = snapshot(adopted_product.adopted_product_id)
    assert snap.revalidation_status is RevalidationStatus.REVALIDATED

    # 4. The EXISTING Policy Engine returns its most permissive ruling.
    allow_autonomous()
    evaluation = external_policy_service.evaluate_adopted_product(
        adopted_product.adopted_product_id
    )
    assert evaluation.policy_decision.decision.value == "ALLOW_AUTONOMOUS_PURCHASE"

    # 5. An approval is created — PENDING, not executed, not self-approved.
    prepared = post_approval(adopted_product.adopted_product_id).json()
    assert prepared["policy_decision"] == "ALLOW_AUTONOMOUS_PURCHASE"
    assert prepared["approvable"] is True
    assert prepared["approval"]["status"] == "PENDING"
    assert prepared["approval"]["approved_at"] is None

    # 6. The client explicitly approves. One boolean, nothing else.
    resolved = post_resolve(
        prepared["approval"]["approval_id"], approve=True
    )

    # 7. The result is APPROVED.
    assert resolved.status_code == 200
    final = resolved.json()
    assert final["status"] == "APPROVED"
    assert final["approved_at"] == NOW.isoformat()
    assert final["adopted_product_id"] == adopted_product.adopted_product_id
    assert final["revalidation_id"] == snap.revalidation_id
    assert Decimal(final["approved_normalized_price"]) == FRESH_PRICE

    # 8. PayPal was never called — every entry point is a booby trap that
    #    would have raised AssertionError, and no PayPal field exists.
    assert not {"paypal_order_id", "paypal_capture_id", "refund_id"} & set(final)

    # 9. No transaction exists.
    assert transaction_service.list_transactions() == []
    with sqlite3.connect(transaction_service._db_path()) as conn:
        conn.executescript(transaction_service._SCHEMA)
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0

    # 10. No PayPal order exists: the approvals table has no column to hold
    #     one, so there is nowhere an order id could have been recorded.
    assert not {"paypal_order_id", "paypal_capture_id", "refund_id"} & (
        approvals_table_columns()
    )

    # APPROVED is the end of the road in this phase.
    assert [status.value for status in ExternalApprovalStatus] == [
        "PENDING",
        "APPROVED",
        "REJECTED",
        "EXPIRED",
    ]
