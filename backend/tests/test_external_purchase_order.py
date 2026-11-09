"""APPROVED external product -> PayPal order creation tests (Phase 11E).

The properties these tests defend:

  1. THE CLIENT NEVER SUPPLIES MONEY. The endpoint takes only an approval id
     in the URL. Every test that posts a forged price, currency, merchant,
     URL, product id, revalidation id or PayPal order id asserts the exact
     bytes sent to PayPal, and they are always the server's own.

  2. THE AMOUNT IS THE APPROVED PROVIDER-OBSERVED PRICE. The adopted
     snapshot's price (USD 29.99), the normalized policy figure (INR
     1824.09) and the old discovery evidence are all deliberately DIFFERENT
     from the fresh observed price (USD 19.99), so any test that reads the
     wrong one fails loudly rather than coincidentally passing.

  3. EVERY PHASE 11D BINDING IS RE-VERIFIED before PayPal is contacted, and
     any failure is a deterministic 409 with no PayPal call at all.

  4. AT MOST ONE PAYPAL ORDER PER APPROVAL. A duplicate request replays the
     same order id; a concurrent request refuses rather than creating a
     second one; a failed execution is never retried.

  5. NOTHING IS CAPTURED AND NOTHING IS REFUNDED. capture_order and
     refund_capture are booby-trapped, the fake PayPal transport raises on
     any URL other than token + create-order, and the catalog transaction
     ledger is never written.

HTTP to PayPal is fully mocked: httpx.post inside the paypal module is
replaced by a fake that records the exact JSON body sent, which is what the
money assertions read. Nothing here contacts PayPal, Serper or Channel3.
Each test gets its own temporary SQLite database (conftest).

Several tests tamper with stored rows through raw SQL. That is deliberate:
the price, currency, identity and status re-checks are DEFENCE IN DEPTH
behind the revalidation_id binding, and the only way to exercise them is to
simulate a record that has been corrupted or forged.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app import paypal
from app.main import app
from app.schemas.adoption import AdoptedProductStatus
from app.schemas.discovery import NormalizedDiscoveredProduct
from app.schemas.external_approval import ExternalApprovalStatus
from app.schemas.external_purchase import (
    ExternalOrderConflict,
    ExternalOrderStatus,
    ExternalPurchaseOrder,
)
from app.schemas.policy import PolicyOutcome
from app.schemas.revalidation import (
    AvailabilityStatus,
    IdentityMatch,
    MerchantStatus,
    PriceStatus,
    RevalidationReason,
    RevalidationSnapshot,
    RevalidationStatus,
)
from app.schemas.transaction import TransactionRecord, TransactionStatus
from app.services import (
    adoption_service,
    external_approval_service,
    external_purchase_service,
    purchase_policy_settings,
    revalidation_service,
    transaction_service,
)
from app.services.discovery_evidence_store import record_products
from app.services.external_approval_service import (
    request_approval,
    resolve_approval,
)
from app.services.external_purchase_service import (
    ExternalOrderConflictError,
    create_order_for_approval,
)

client = TestClient(app)

NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
TITLE = "Sony WH-CH520 Wireless Headphones"
GOOGLE_LINK = "https://www.google.com/shopping/product/1"
MERCHANT = "Amazon"

#: What the provider is asking NOW, in ITS OWN currency. THE PayPal amount.
FRESH_OBSERVED = Decimal("19.99")
FRESH_OBSERVED_CURRENCY = "USD"
#: Phase 10F's comparison figure for that price (19.99 * 91.25 = 1824.0875).
#: The POLICY price. Never a PayPal amount.
FRESH_NORMALIZED = Decimal("1824.09")

#: The adopted snapshot's ORIGINAL figures. Decoys: deliberately different
#: from the fresh ones so reading the wrong row is always visible.
ADOPTED_OBSERVED = Decimal("29.99")
ADOPTED_NORMALIZED = Decimal("2736.59")

TTL = external_approval_service.DEFAULT_EXTERNAL_REVALIDATION_TTL_SECONDS

PRODUCTS_JSON = Path(__file__).resolve().parents[1] / "data" / "products.json"

TOKEN_URL = "/v1/oauth2/token"
ORDERS_URL = "/v2/checkout/orders"

ORDER_RESPONSE = {
    "id": "EXTORDER123",
    "status": "CREATED",
    "links": [
        {
            "rel": "approve",
            "href": "https://www.sandbox.paypal.com/checkoutnow?token=EXTORDER123",
        }
    ],
}

#: Money fields a malicious client might try to smuggle in. Every one of
#: these is posted as a request body by the injection tests below.
INJECTION_BODY = {
    "amount": "0.01",
    "price": "0.01",
    "value": "0.01",
    "paypal_amount": "0.01",
    "approved_normalized_price": "0.01",
    "currency": "EUR",
    "paypal_currency": "EUR",
    "approved_normalized_currency": "EUR",
    "merchant_name": "attacker-shop",
    "merchant_domain": "attacker.example",
    "product_url": "https://attacker.example/pay",
    "title": "Attacker Product",
    "description": "Attacker description",
    "product_id": "LAP001",
    "adopted_product_id": "adopt_forged",
    "revalidation_id": "reval_forged",
    "policy_decision": "ALLOW_AUTONOMOUS_PURCHASE",
    "paypal_order_id": "FORGEDORDER",
    "capture_id": "FORGEDCAPTURE",
    "return_url": "https://attacker.example/return",
    "cancel_url": "https://attacker.example/cancel",
}


# ---------------------------------------------------------------------------
# Fakes and fixtures
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


@pytest.fixture
def paypal_http(monkeypatch):
    """Mock PayPal's HTTP transport; record every request body sent.

    `state` lets a test turn the create-order call into any failure mode.
    Any URL other than the token and create-order endpoints raises — which
    is how "no capture, no refund" is enforced at the transport level.
    """
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("PAYPAL_ENVIRONMENT", "sandbox")

    calls: list[dict] = []
    state = SimpleNamespace(
        token_status=200,
        order_status=201,
        order_payload=ORDER_RESPONSE,
        order_raises=None,
    )

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        if url.endswith(TOKEN_URL):
            if state.token_status != 200:
                return FakeResponse(state.token_status, {"error": "invalid_client"})
            return FakeResponse(
                200,
                {
                    "access_token": "fake-token",
                    "token_type": "Bearer",
                    "expires_in": 32400,
                },
            )
        if url.endswith(ORDERS_URL):
            if state.order_raises is not None:
                raise state.order_raises
            return FakeResponse(state.order_status, state.order_payload)
        raise AssertionError(f"Phase 11E must not call PayPal at {url}")

    def no_get(url, **kwargs):  # pragma: no cover - must never run
        raise AssertionError(f"Phase 11E must not GET PayPal at {url}")

    monkeypatch.setattr(paypal.httpx, "post", fake_post)
    monkeypatch.setattr(paypal.httpx, "get", no_get)
    return SimpleNamespace(calls=calls, state=state)


@pytest.fixture
def no_money_moves(monkeypatch):
    """Any capture, refund, ledger write, session or webhook touch fails.

    PayPal order CREATION is deliberately NOT trapped: this phase is
    supposed to do exactly that, and the tests assert on the request it
    sends. Everything downstream of creation is a booby trap.
    """
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
            raise AssertionError(f"external order creation must not touch {name}")

        return _bomb

    monkeypatch.setattr(paypal, "capture_order", bomb("PayPal capture_order"))
    monkeypatch.setattr(paypal, "refund_capture", bomb("PayPal refund_capture"))
    monkeypatch.setattr(paypal, "get_order", bomb("PayPal get_order"))
    monkeypatch.setattr(
        purchase_execution_service, "execute_purchase", bomb("catalog execution")
    )
    monkeypatch.setattr(
        purchase_execution_service, "approve_purchase", bomb("catalog approval")
    )
    monkeypatch.setattr(
        purchase_execution_service, "capture_purchase", bomb("capture")
    )
    monkeypatch.setattr(ledger, "create_transaction", bomb("the catalog ledger"))
    monkeypatch.setattr(ledger, "update_status", bomb("the catalog ledger"))
    monkeypatch.setattr(ledger, "mark_captured", bomb("the catalog ledger"))
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
        raise AssertionError("external order creation must not invoke AI")

    monkeypatch.setattr(providers, "get_provider", bomb)
    monkeypatch.setattr(intent_agent, "get_provider", bomb)
    monkeypatch.setattr(comparison_agent, "rank", bomb)
    monkeypatch.setattr(recommendation_agent, "recommend", bomb)


@pytest.fixture
def clock(monkeypatch):
    """Pin BOTH services' clocks. Returns a setter for later instants."""
    holder = {"now": NOW}
    monkeypatch.setattr(
        external_approval_service, "_now_dt", lambda: holder["now"]
    )
    monkeypatch.setattr(
        external_purchase_service, "_now_dt", lambda: holder["now"]
    )
    return holder


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
        # The decoy prices. Any code path that reads the ADOPTED record for
        # money instead of the fresh revalidation charges 29.99 and fails.
        "observed_price": ADOPTED_OBSERVED,
        "observed_currency": "USD",
        "observed_at": NOW,
        "normalized_price": ADOPTED_NORMALIZED,
        "normalized_currency": "INR",
        "conversion_rate": Decimal("91.25"),
        "conversion_source": "configured_demo_rate",
        "rate_observed_at": None,
        "merchant_name": MERCHANT,
        "merchant_domain": None,
    }
    fields.update(overrides)
    return NormalizedDiscoveredProduct(**fields)


@pytest.fixture
def adopted(no_money_moves, no_ai, clock):
    """One adopted external product, no revalidation yet, frozen clock."""
    record_products([evidence()])
    record, _ = adoption_service.adopt("serper", "serper:111111")
    return record


def snapshot(
    adopted_product_id: str,
    *,
    status=RevalidationStatus.REVALIDATED,
    observed=FRESH_OBSERVED,
    observed_currency=FRESH_OBSERVED_CURRENCY,
    normalized=FRESH_NORMALIZED,
    normalized_currency="INR",
    checked_at=None,
    reval_id="reval_fresh_1",
) -> RevalidationSnapshot:
    """Persist one revalidation snapshot through the service's own insert."""
    confirmed = status is RevalidationStatus.REVALIDATED
    if checked_at is None:
        stamp = NOW.isoformat()
    elif isinstance(checked_at, str):
        stamp = checked_at
    else:
        stamp = checked_at.isoformat()
    record = RevalidationSnapshot(
        revalidation_id=reval_id,
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
        price_status=(
            PriceStatus.PRICE_CHANGED if confirmed else PriceStatus.PRICE_UNAVAILABLE
        ),
        availability=AvailabilityStatus.UNKNOWN,
        merchant_status=MerchantStatus.MERCHANT_UNCHANGED,
        current_title=TITLE if confirmed else None,
        current_merchant_name=MERCHANT if confirmed else None,
        current_merchant_domain=None,
        current_product_url=GOOGLE_LINK if confirmed else None,
        current_observed_price=observed if confirmed else None,
        current_observed_currency=observed_currency if confirmed else None,
        current_normalized_price=normalized if confirmed else None,
        current_normalized_currency=normalized_currency if confirmed else None,
        previous_observed_price=ADOPTED_OBSERVED,
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


def pending_approval(adopted_product_id: str):
    """A PENDING approval for the fresh binding."""
    result = request_approval(adopted_product_id)
    assert result.approvable, result.blocked_reason
    return result.approval


def approved_approval(adopted_product_id: str):
    """A fully APPROVED approval: explicit user decision, bindings verified."""
    approval = pending_approval(adopted_product_id)
    settled = resolve_approval(approval.approval_id, True)
    assert settled.status is ExternalApprovalStatus.APPROVED
    return settled


def post_create_order(approval_id: str, **kwargs):
    return client.post(
        f"/discovery/approvals/{approval_id}/create-order", **kwargs
    )


def order_calls(ph) -> list[dict]:
    return [c for c in ph.calls if c["url"].endswith(ORDERS_URL)]


def order_body(ph) -> dict:
    calls = order_calls(ph)
    assert len(calls) == 1, f"expected exactly one create-order call, got {len(calls)}"
    return calls[0]["json"]


def sent_amount(ph) -> dict:
    return order_body(ph)["purchase_units"][0]["amount"]


def executions() -> list[ExternalPurchaseOrder]:
    with sqlite3.connect(external_purchase_service._db_path()) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(external_purchase_service._SCHEMA)
        return [
            ExternalPurchaseOrder(**dict(row))
            for row in conn.execute("SELECT * FROM external_purchase_orders")
        ]


def tamper_approval(approval_id: str, **columns) -> None:
    """Rewrite stored approval columns directly, simulating a forged record."""
    assignments = ", ".join(f"{name} = ?" for name in columns)
    with sqlite3.connect(external_approval_service._db_path()) as conn:
        conn.execute(
            f"UPDATE external_product_approvals SET {assignments} "
            "WHERE approval_id = ?",
            (*columns.values(), approval_id),
        )
        conn.commit()


def tamper_snapshot(revalidation_id: str, **columns) -> None:
    """Rewrite stored revalidation columns directly, simulating a forgery."""
    assignments = ", ".join(f"{name} = ?" for name in columns)
    with sqlite3.connect(revalidation_service._db_path()) as conn:
        conn.execute(
            f"UPDATE adopted_product_revalidations SET {assignments} "
            "WHERE revalidation_id = ?",
            (*columns.values(), revalidation_id),
        )
        conn.commit()


def expect_conflict(approval_id: str, code: ExternalOrderConflict):
    """Call the service, require this exact conflict, return the error."""
    with pytest.raises(ExternalOrderConflictError) as exc:
        create_order_for_approval(approval_id)
    assert exc.value.code is code, f"expected {code.value}, got {exc.value.code.value}"
    return exc.value


# ---------------------------------------------------------------------------
# 1. The happy path: an APPROVED external product creates ONE PayPal order
# ---------------------------------------------------------------------------


def test_approved_approval_creates_a_paypal_order(adopted, paypal_http):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)

    result = create_order_for_approval(approval.approval_id)

    assert result.created is True
    assert result.captured is False
    assert result.execution.status is ExternalOrderStatus.ORDER_CREATED
    assert result.execution.paypal_order_id == "EXTORDER123"
    assert result.approve_url.endswith("token=EXTORDER123")
    # The amount PayPal actually received.
    assert sent_amount(paypal_http) == {"currency_code": "USD", "value": "19.99"}


def test_endpoint_creates_the_order_with_only_an_approval_id_in_the_url(
    adopted, paypal_http
):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)

    response = post_create_order(approval.approval_id)

    assert response.status_code == 200
    body = response.json()
    assert body["created"] is True
    assert body["captured"] is False
    assert body["execution"]["paypal_order_id"] == "EXTORDER123"
    assert body["execution"]["paypal_amount"] == "19.99"
    assert body["execution"]["paypal_currency"] == "USD"
    assert body["approval"]["approval_id"] == approval.approval_id


def test_an_allow_autonomous_approval_still_required_the_explicit_approval(
    adopted, paypal_http
):
    """ALLOW_AUTONOMOUS_PURCHASE never bypasses the human gate (11D), and
    11E executes only the record that human approved."""
    allow_autonomous()
    snapshot(adopted.adopted_product_id)
    pending = pending_approval(adopted.adopted_product_id)
    assert pending.policy_decision is PolicyOutcome.ALLOW_AUTONOMOUS_PURCHASE

    # Not approved yet -> no order, even though policy would allow it.
    expect_conflict(pending.approval_id, ExternalOrderConflict.APPROVAL_NOT_APPROVED)
    assert order_calls(paypal_http) == []

    resolve_approval(pending.approval_id, True)
    assert create_order_for_approval(pending.approval_id).created is True
    assert sent_amount(paypal_http)["value"] == "19.99"


def test_the_execution_record_binds_the_whole_trust_chain(adopted, paypal_http):
    require_approval()
    snap = snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)

    create_order_for_approval(approval.approval_id)

    stored = executions()
    assert len(stored) == 1
    record = stored[0]
    assert record.approval_id == approval.approval_id
    assert record.adopted_product_id == adopted.adopted_product_id
    assert record.revalidation_id == snap.revalidation_id
    assert record.paypal_order_id == "EXTORDER123"
    assert record.paypal_amount == "19.99"
    assert record.paypal_currency == "USD"
    assert record.status is ExternalOrderStatus.ORDER_CREATED


# ---------------------------------------------------------------------------
# 2-4. An approval that is not an explicit APPROVED decision buys nothing
# ---------------------------------------------------------------------------


def test_pending_approval_is_rejected(adopted, paypal_http):
    require_approval()
    snapshot(adopted.adopted_product_id)
    pending = pending_approval(adopted.adopted_product_id)

    expect_conflict(pending.approval_id, ExternalOrderConflict.APPROVAL_NOT_APPROVED)

    assert order_calls(paypal_http) == []
    assert executions() == []


def test_rejected_approval_is_rejected(adopted, paypal_http):
    require_approval()
    snapshot(adopted.adopted_product_id)
    pending = pending_approval(adopted.adopted_product_id)
    resolve_approval(pending.approval_id, False)

    expect_conflict(pending.approval_id, ExternalOrderConflict.APPROVAL_REJECTED)

    assert order_calls(paypal_http) == []
    assert executions() == []


def test_expired_pending_approval_is_rejected(adopted, paypal_http, clock):
    require_approval()
    snapshot(adopted.adopted_product_id)
    pending = pending_approval(adopted.adopted_product_id)

    clock["now"] = NOW + timedelta(seconds=TTL + 1)

    expect_conflict(pending.approval_id, ExternalOrderConflict.APPROVAL_EXPIRED)
    assert order_calls(paypal_http) == []


def test_an_approved_approval_past_its_deadline_is_rejected(
    adopted, paypal_http, clock
):
    """APPROVED is terminal, so the TTL must be enforced at order time too:
    approving fresh evidence does not freeze it."""
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)

    clock["now"] = NOW + timedelta(seconds=TTL + 1)

    expect_conflict(approval.approval_id, ExternalOrderConflict.APPROVAL_EXPIRED)
    assert order_calls(paypal_http) == []
    assert executions() == []


def test_unknown_approval_is_a_404_with_no_paypal_call(adopted, paypal_http):
    response = post_create_order("xapv_does_not_exist")

    assert response.status_code == 404
    assert order_calls(paypal_http) == []


# ---------------------------------------------------------------------------
# 5-9. Every Phase 11D binding is re-verified before any money path
# ---------------------------------------------------------------------------


def test_stale_revalidation_is_rejected(adopted, paypal_http, clock):
    """The evidence aged out even though the approval row claims otherwise.

    expires_at is pushed far into the future so the APPROVAL deadline does
    NOT fire first; the refusal must come from the snapshot's own age.
    """
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)
    tamper_approval(
        approval.approval_id,
        expires_at=(NOW + timedelta(days=30)).isoformat(),
    )

    clock["now"] = NOW + timedelta(seconds=TTL + 1)

    expect_conflict(approval.approval_id, ExternalOrderConflict.REVALIDATION_STALE)
    assert order_calls(paypal_http) == []


def test_a_newer_revalidation_supersedes_the_approved_one(adopted, paypal_http):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)

    # A later check supersedes the approved snapshot: the user approved
    # evidence we have since replaced.
    snapshot(
        adopted.adopted_product_id,
        reval_id="reval_fresh_2",
        checked_at=NOW + timedelta(seconds=1),
    )

    expect_conflict(
        approval.approval_id, ExternalOrderConflict.REVALIDATION_SUPERSEDED
    )
    assert order_calls(paypal_http) == []


def test_a_changed_bound_revalidation_id_is_rejected(adopted, paypal_http):
    """Re-pointing the approval at another snapshot does not buy that one."""
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)
    tamper_approval(approval.approval_id, revalidation_id="reval_forged")

    expect_conflict(approval.approval_id, ExternalOrderConflict.REVALIDATION_MISSING)
    assert order_calls(paypal_http) == []


def test_a_bound_revalidation_that_is_not_confirmed_is_rejected(
    adopted, paypal_http
):
    require_approval()
    snap = snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)
    tamper_snapshot(
        snap.revalidation_id,
        revalidation_status=RevalidationStatus.IDENTITY_NOT_FOUND.value,
    )

    expect_conflict(
        approval.approval_id, ExternalOrderConflict.REVALIDATION_NOT_CONFIRMED
    )
    assert order_calls(paypal_http) == []


def test_a_changed_price_is_rejected(adopted, paypal_http):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)
    tamper_approval(approval.approval_id, approved_normalized_price="1.00")

    expect_conflict(approval.approval_id, ExternalOrderConflict.PRICE_CHANGED)
    assert order_calls(paypal_http) == []


def test_a_changed_currency_is_rejected(adopted, paypal_http):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)
    tamper_approval(approval.approval_id, approved_normalized_currency="EUR")

    expect_conflict(approval.approval_id, ExternalOrderConflict.CURRENCY_CHANGED)
    assert order_calls(paypal_http) == []


def test_a_changed_policy_decision_is_rejected(adopted, paypal_http):
    """The engine's ruling moved after the user approved. Nothing executes."""
    allow_autonomous()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)
    assert approval.policy_decision is PolicyOutcome.ALLOW_AUTONOMOUS_PURCHASE

    require_approval()  # same product, different ruling

    expect_conflict(approval.approval_id, ExternalOrderConflict.POLICY_CHANGED)
    assert order_calls(paypal_http) == []


def test_a_missing_adopted_product_is_rejected(adopted, paypal_http):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)
    tamper_approval(approval.approval_id, adopted_product_id="adopt_gone")

    expect_conflict(
        approval.approval_id, ExternalOrderConflict.ADOPTED_PRODUCT_MISSING
    )
    assert order_calls(paypal_http) == []


def test_a_disabled_product_is_rejected(adopted, paypal_http):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)
    adoption_service.set_status(
        adopted.adopted_product_id, AdoptedProductStatus.DISABLED
    )

    expect_conflict(approval.approval_id, ExternalOrderConflict.NOT_POLICY_ELIGIBLE)
    assert order_calls(paypal_http) == []


def test_a_stale_adopted_record_is_not_purchasable(adopted, paypal_http, monkeypatch):
    """Lifecycle re-check: only an ACTIVE adopted record may be charged for."""
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)

    # STALE contradicts the confirmed evidence the approval is bound to, so
    # it can only be reached by forcing the lifecycle directly.
    adoption_service.set_status(
        adopted.adopted_product_id, AdoptedProductStatus.STALE
    )

    expect_conflict(
        approval.approval_id, ExternalOrderConflict.PRODUCT_NOT_PURCHASABLE
    )
    assert order_calls(paypal_http) == []


# ---------------------------------------------------------------------------
# 10. Identity must still be durable and still exactly confirmed
# ---------------------------------------------------------------------------


def test_a_forged_identity_key_is_rejected(adopted, paypal_http):
    require_approval()
    snap = snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)
    tamper_snapshot(snap.revalidation_id, product_identity_key="serper:other")

    expect_conflict(
        approval.approval_id, ExternalOrderConflict.PRODUCT_IDENTITY_INVALID
    )
    assert order_calls(paypal_http) == []


def test_a_snapshot_that_did_not_match_by_external_id_is_rejected(
    adopted, paypal_http
):
    require_approval()
    snap = snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)
    tamper_snapshot(
        snap.revalidation_id, identity_match=IdentityMatch.UNVERIFIABLE.value
    )

    expect_conflict(
        approval.approval_id, ExternalOrderConflict.PRODUCT_IDENTITY_INVALID
    )
    assert order_calls(paypal_http) == []


def test_a_non_durable_provider_identity_is_rejected(adopted, paypal_http):
    """A provider id that only names an observation cannot be charged for."""
    require_approval()
    snap = snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)

    # Serper link-hash ids are observation-only (Phase 11B.1). Rewriting the
    # adopted record's identity to one simulates a provider downgrade.
    with sqlite3.connect(adoption_service._db_path()) as conn:
        conn.execute(
            "UPDATE adopted_products SET discovery_external_id = ?, "
            "product_identity_kind = ?, product_identity_key = ? "
            "WHERE adopted_product_id = ?",
            (
                "serper:link:abc123",
                "PROVIDER_OBSERVATION_ONLY",
                None,
                adopted.adopted_product_id,
            ),
        )
        conn.commit()
    tamper_snapshot(snap.revalidation_id, discovery_external_id="serper:link:abc123")

    expect_conflict(
        approval.approval_id, ExternalOrderConflict.PRODUCT_IDENTITY_INVALID
    )
    assert order_calls(paypal_http) == []


# ---------------------------------------------------------------------------
# 11-17. Client injection: there is nowhere to put a forged money field
# ---------------------------------------------------------------------------


def test_a_body_full_of_forged_money_fields_changes_nothing(adopted, paypal_http):
    """Price, currency, merchant, URL, title, product id, revalidation id,
    policy decision, PayPal order id, capture id and checkout URLs -- all
    posted at once, all irrelevant."""
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)

    response = post_create_order(approval.approval_id, json=INJECTION_BODY)

    assert response.status_code == 200
    body = order_body(paypal_http)
    assert body["purchase_units"][0]["amount"] == {
        "currency_code": "USD",
        "value": "19.99",
    }
    serialized = str(body)
    for forged in (
        "0.01",
        "EUR",
        "attacker",
        "Attacker",
        "FORGEDORDER",
        "FORGEDCAPTURE",
        "LAP001",
        "adopt_forged",
        "reval_forged",
    ):
        assert forged not in serialized, f"client value {forged!r} reached PayPal"

    stored = executions()[0]
    assert stored.paypal_amount == "19.99"
    assert stored.paypal_currency == "USD"
    assert stored.paypal_order_id == "EXTORDER123"
    assert stored.adopted_product_id == adopted.adopted_product_id


@pytest.mark.parametrize(
    "field,value",
    [
        ("amount", "0.01"),
        ("price", "0.01"),
        ("paypal_amount", "0.01"),
        ("currency", "EUR"),
        ("paypal_currency", "JPY"),
        ("merchant_name", "attacker-shop"),
        ("product_url", "https://attacker.example/pay"),
        ("product_id", "LAP001"),
        ("adopted_product_id", "adopt_forged"),
        ("revalidation_id", "reval_forged"),
        ("policy_decision", "ALLOW_AUTONOMOUS_PURCHASE"),
        ("paypal_order_id", "FORGEDORDER"),
        ("capture_id", "FORGEDCAPTURE"),
        ("return_url", "https://attacker.example/return"),
    ],
)
def test_each_injected_field_individually_cannot_influence_the_order(
    adopted, paypal_http, field, value
):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)

    response = post_create_order(approval.approval_id, json={field: value})

    assert response.status_code == 200
    assert sent_amount(paypal_http) == {"currency_code": "USD", "value": "19.99"}
    assert value not in str(order_body(paypal_http))


def test_the_create_order_path_has_no_money_parameters_at_all(adopted):
    """Structural, not behavioural: the signature cannot accept money.

    create_order_for_approval takes one argument, and the amount helper can
    see nothing but one revalidation snapshot.
    """
    import inspect

    signature = inspect.signature(create_order_for_approval)
    assert list(signature.parameters) == ["approval_id"]

    amount_signature = inspect.signature(
        external_purchase_service.paypal_amount_from_snapshot
    )
    assert list(amount_signature.parameters) == ["snapshot"]


# ---------------------------------------------------------------------------
# 18-21. The amount comes from the approved state. Nothing else can reach it
# ---------------------------------------------------------------------------


def test_the_amount_is_the_approved_revalidations_observed_price(
    adopted, paypal_http
):
    require_approval()
    snap = snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)

    result = create_order_for_approval(approval.approval_id)

    assert sent_amount(paypal_http)["value"] == str(snap.current_observed_price)
    assert result.observed_price_source.endswith(snap.revalidation_id)


def test_the_adopted_snapshot_price_cannot_influence_the_amount(
    adopted, paypal_http
):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)

    create_order_for_approval(approval.approval_id)

    value = sent_amount(paypal_http)["value"]
    assert value == "19.99"
    assert value != str(ADOPTED_OBSERVED)
    assert value != str(ADOPTED_NORMALIZED)


def test_rewriting_the_adopted_price_does_not_move_the_amount(
    adopted, paypal_http
):
    """Even a corrupted adopted row cannot reprice the order."""
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)

    with sqlite3.connect(adoption_service._db_path()) as conn:
        conn.execute(
            "UPDATE adopted_products SET observed_price = ?, "
            "normalized_price = ? WHERE adopted_product_id = ?",
            ("99999.00", "99999.00", adopted.adopted_product_id),
        )
        conn.commit()

    create_order_for_approval(approval.approval_id)

    assert sent_amount(paypal_http)["value"] == "19.99"


def test_old_discovery_evidence_cannot_influence_the_amount(adopted, paypal_http):
    """Re-recording the discovery evidence at a new price changes nothing:
    the amount comes from the bound revalidation, not the evidence store."""
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)

    record_products([evidence(observed_price=Decimal("0.01"))])

    create_order_for_approval(approval.approval_id)

    assert sent_amount(paypal_http)["value"] == "19.99"


def test_the_normalized_policy_price_is_never_the_paypal_amount(
    adopted, paypal_http
):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)

    result = create_order_for_approval(approval.approval_id)

    # The policy figure is reported, clearly separated, and NOT charged.
    assert result.approval.approved_normalized_price == FRESH_NORMALIZED
    assert result.approval.approved_normalized_currency == "INR"
    assert sent_amount(paypal_http) == {"currency_code": "USD", "value": "19.99"}


def test_ai_cannot_influence_the_amount(adopted, paypal_http, no_ai):
    """no_ai booby-traps every provider and agent; the order still happens."""
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)

    create_order_for_approval(approval.approval_id)

    assert sent_amount(paypal_http)["value"] == "19.99"


def test_the_service_imports_no_ai_no_fx_and_no_capture(adopted):
    """Structural: the module's CODE never names a capture, a refund, an FX
    conversion, an AI provider or the catalog ledger.

    Parsed with ast, not grepped, so the module docstring explaining what it
    deliberately avoids does not satisfy (or break) the assertion.
    """
    import ast

    tree = ast.parse(
        Path(external_purchase_service.__file__).read_text(encoding="utf-8")
    )
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.name.rsplit(".", 1)[-1])
            if node.asname:
                names.add(node.asname)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.rsplit(".", 1)[-1])

    # capture_order left this list in Phase 11F-D, which implements the one
    # external capture path. Everything else stays forbidden — in
    # particular EVERY refund name and EVERY catalog-ledger name, which is
    # what keeps external capture from leaking into the catalog or
    # acquiring a refund it must not have.
    assert "capture_order" in names, (
        "Phase 11F-D captures external orders through paypal.capture_order; "
        "if this name is gone the capture path has been removed or renamed"
    )
    for forbidden in (
        "httpx",
        "capture_purchase",
        "refund_capture",
        "refund_transaction",
        "refund_service",
        "DEMO_INR_PER_USD",
        "convert_inr_to_usd",
        "currency_service",
        "convert_amount",
        "get_rate_provider",
        "discovery_currency_service",
        "normalize_product",
        "providers",
        "intent_agent",
        "recommendation_agent",
        "comparison_agent",
        "transaction_service",
        "create_transaction",
        "mark_captured",
        "session_service",
        "webhook_service",
    ):
        assert forbidden not in names, (
            f"external purchase service code references {forbidden}"
        )


# ---------------------------------------------------------------------------
# 22. Decimal money: exact two places, never a silent rounding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "observed,expected",
    [
        (Decimal("19.99"), "19.99"),
        (Decimal("19.9"), "19.90"),
        (Decimal("19"), "19.00"),
        (Decimal("1824"), "1824.00"),
        (Decimal("0.01"), "0.01"),
        (Decimal("19.990"), "19.99"),
    ],
)
def test_exact_prices_are_formatted_to_two_decimal_places(
    adopted, paypal_http, observed, expected
):
    require_approval()
    snapshot(adopted.adopted_product_id, observed=observed)
    approval = approved_approval(adopted.adopted_product_id)

    create_order_for_approval(approval.approval_id)

    amount = sent_amount(paypal_http)
    assert amount["value"] == expected
    assert isinstance(amount["value"], str)  # never a float


@pytest.mark.parametrize("observed", [Decimal("19.999"), Decimal("0.005")])
def test_a_price_needing_rounding_fails_closed(adopted, paypal_http, observed):
    """Rounding would charge an amount nobody approved."""
    require_approval()
    snapshot(adopted.adopted_product_id, observed=observed)
    approval = approved_approval(adopted.adopted_product_id)

    expect_conflict(
        approval.approval_id, ExternalOrderConflict.AMOUNT_NOT_REPRESENTABLE
    )
    assert order_calls(paypal_http) == []


@pytest.mark.parametrize("observed", [Decimal("0"), Decimal("-5.00")])
def test_a_non_positive_price_fails_closed(adopted, paypal_http, observed):
    require_approval()
    snap = snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)
    # The engine would reject a non-positive normalized price, so the only
    # way this row exists is corruption of the observed column.
    tamper_snapshot(snap.revalidation_id, current_observed_price=str(observed))

    expect_conflict(
        approval.approval_id, ExternalOrderConflict.AMOUNT_NOT_REPRESENTABLE
    )
    assert order_calls(paypal_http) == []


def test_a_snapshot_without_an_observed_price_fails_closed(adopted, paypal_http):
    """The normalized INR figure is NOT substituted for a missing price."""
    require_approval()
    snap = snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)
    tamper_snapshot(
        snap.revalidation_id,
        current_observed_price=None,
        current_observed_currency=None,
    )

    expect_conflict(
        approval.approval_id, ExternalOrderConflict.OBSERVED_PRICE_UNAVAILABLE
    )
    assert order_calls(paypal_http) == []


def test_the_amount_helper_uses_decimal_not_float(adopted):
    """A float-based implementation would produce 0.1+0.2 artefacts."""
    snap = snapshot(adopted.adopted_product_id, observed=Decimal("0.30"))
    amount, currency = external_purchase_service.paypal_amount_from_snapshot(snap)
    assert (amount, currency) == ("0.30", "USD")


# ---------------------------------------------------------------------------
# 23-24. Currency support is an explicit allowlist that fails closed
# ---------------------------------------------------------------------------


def test_usd_is_the_supported_currency(adopted, paypal_http):
    assert external_purchase_service.SUPPORTED_PAYPAL_CURRENCIES == frozenset(
        {"USD"}
    )

    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)

    create_order_for_approval(approval.approval_id)

    assert sent_amount(paypal_http)["currency_code"] == "USD"


@pytest.mark.parametrize("currency", ["EUR", "GBP", "JPY", "AUD"])
def test_an_unsupported_observed_currency_fails_closed(
    adopted, paypal_http, currency
):
    require_approval()
    snapshot(adopted.adopted_product_id, observed_currency=currency)
    approval = approved_approval(adopted.adopted_product_id)

    error = expect_conflict(
        approval.approval_id, ExternalOrderConflict.UNSUPPORTED_PAYPAL_CURRENCY
    )

    assert currency in error.message
    assert order_calls(paypal_http) == []
    assert executions() == []


def test_an_inr_priced_external_product_is_not_silently_converted(
    adopted, paypal_http
):
    """The Policy Engine evaluates normalized INR. That does NOT mean PayPal
    can charge INR, and no rate is invented to bridge the gap."""
    require_approval()
    snapshot(
        adopted.adopted_product_id,
        observed=Decimal("1824.09"),
        observed_currency="INR",
    )
    approval = approved_approval(adopted.adopted_product_id)

    expect_conflict(
        approval.approval_id, ExternalOrderConflict.UNSUPPORTED_PAYPAL_CURRENCY
    )
    assert order_calls(paypal_http) == []


def test_the_supported_currency_rule_has_no_environment_override(
    adopted, paypal_http, monkeypatch
):
    """Widening the allowlist must be a reviewed code change, not a setting."""
    for name in (
        "SUPPORTED_PAYPAL_CURRENCIES",
        "PAYPAL_SUPPORTED_CURRENCIES",
        "EXTERNAL_PAYPAL_CURRENCIES",
    ):
        monkeypatch.setenv(name, "EUR,INR,USD")

    require_approval()
    snapshot(adopted.adopted_product_id, observed_currency="EUR")
    approval = approved_approval(adopted.adopted_product_id)

    expect_conflict(
        approval.approval_id, ExternalOrderConflict.UNSUPPORTED_PAYPAL_CURRENCY
    )


# ---------------------------------------------------------------------------
# 25-27. Description and checkout URLs are server-controlled
# ---------------------------------------------------------------------------


def test_the_description_is_assembled_from_trusted_server_data(
    adopted, paypal_http
):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)

    create_order_for_approval(approval.approval_id)

    description = order_body(paypal_http)["purchase_units"][0]["description"]
    assert description == (
        f"{TITLE} from {MERCHANT} (PayPilot {adopted.adopted_product_id})"
    )
    assert len(description) <= external_purchase_service.MAX_DESCRIPTION_LENGTH


def test_a_client_description_never_reaches_paypal(adopted, paypal_http):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)

    post_create_order(
        approval.approval_id, json={"description": "Attacker description"}
    )

    description = order_body(paypal_http)["purchase_units"][0]["description"]
    assert "Attacker" not in description
    assert description.startswith(TITLE)


def test_a_long_title_is_truncated_without_losing_the_identifier(adopted):
    """The PayPilot id survives truncation; the title gives way."""
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)

    record = adoption_service.get(adopted.adopted_product_id)
    record = record.model_copy(update={"title": "X" * 400})

    description = external_purchase_service.order_description(record, approval)

    assert len(description) <= external_purchase_service.MAX_DESCRIPTION_LENGTH
    assert description.endswith(f"(PayPilot {adopted.adopted_product_id})")


def test_the_checkout_urls_are_the_servers_own(adopted, paypal_http):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)

    post_create_order(
        approval.approval_id,
        json={
            "return_url": "https://attacker.example/return",
            "cancel_url": "https://attacker.example/cancel",
        },
    )

    context = order_body(paypal_http)["application_context"]
    assert context["return_url"] and context["cancel_url"]
    assert "attacker.example" not in context["return_url"]
    assert "attacker.example" not in context["cancel_url"]
    assert context["brand_name"] == "PayPilot"


def test_the_order_intent_is_capture_but_capture_is_never_called(
    adopted, paypal_http
):
    """intent=CAPTURE describes the order TYPE; capturing is a separate call
    this phase never makes."""
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)

    create_order_for_approval(approval.approval_id)

    assert order_body(paypal_http)["intent"] == "CAPTURE"
    assert not [c for c in paypal_http.calls if "/capture" in c["url"]]


# ---------------------------------------------------------------------------
# 28-29, 37-38. Nothing is captured, refunded, or written to the ledger
# ---------------------------------------------------------------------------


def test_no_capture_no_refund_no_ledger_row(adopted, paypal_http):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)

    result = create_order_for_approval(approval.approval_id)

    # The order is CREATED at PayPal and awaiting the buyer.
    assert result.execution.status is ExternalOrderStatus.ORDER_CREATED
    assert result.captured is False
    # No capture, no refund, no get-order: the transport raises on those.
    assert {c["url"].rsplit("/", 2)[-1] for c in paypal_http.calls} <= {
        "token",
        "orders",
    }
    # The catalog ledger is untouched (its writers are booby-trapped too).
    assert transaction_service.list_transactions() == []


def test_no_execution_record_can_reach_a_refunded_state(adopted, paypal_http):
    """What this lock guards, phase by phase.

    11F-C added BUYER_APPROVED (a fact about the buyer, not a payment).
    11F-D added the capture states — external capture now exists, on this
    table, through one dedicated path. What has NEVER existed and must not
    is a REFUND: no REFUNDED status, no refund field, and no catalog-ledger
    identifier anywhere on an external execution.
    """
    assert {s.value for s in ExternalOrderStatus} == {
        "CREATING",
        "ORDER_CREATED",
        "BUYER_APPROVED",
        "CAPTURING",
        "CAPTURED",
        "CAPTURE_PENDING",
        "CAPTURE_DECLINED",
        "CAPTURE_FAILED",
        "FAILED",
    }
    assert "REFUNDED" not in {s.value for s in ExternalOrderStatus}
    assert "REFUNDING" not in {s.value for s in ExternalOrderStatus}
    for forbidden in (
        "refund_id",
        "paypal_refund_id",
        "refunded_amount",
        "refund_status",
        "transaction_id",
    ):
        assert forbidden not in ExternalPurchaseOrder.model_fields
    # Creating an order still captures nothing: the capture states are
    # reachable only through the dedicated 11F-D path.
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)
    result = create_order_for_approval(approval.approval_id)
    assert result.execution.status is ExternalOrderStatus.ORDER_CREATED
    assert result.captured is False
    assert result.execution.paypal_capture_id is None
    assert result.execution.capture_authorized_at is None


def test_the_paypal_order_id_is_stored_server_side(adopted, paypal_http):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)

    create_order_for_approval(approval.approval_id)

    stored = external_purchase_service.get_by_approval(approval.approval_id)
    assert stored is not None
    assert stored.paypal_order_id == "EXTORDER123"
    # Read straight from SQLite, not from the in-memory result.
    with sqlite3.connect(external_purchase_service._db_path()) as conn:
        row = conn.execute(
            "SELECT paypal_order_id, paypal_amount, paypal_currency, status "
            "FROM external_purchase_orders WHERE approval_id = ?",
            (approval.approval_id,),
        ).fetchone()
    assert row == ("EXTORDER123", "19.99", "USD", "ORDER_CREATED")


# ---------------------------------------------------------------------------
# 30-31. One approval, one PayPal order
# ---------------------------------------------------------------------------


def test_a_duplicate_request_is_idempotent(adopted, paypal_http):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)

    first = create_order_for_approval(approval.approval_id)
    second = create_order_for_approval(approval.approval_id)

    assert first.created is True
    assert second.created is False
    assert second.execution.execution_id == first.execution.execution_id
    assert second.execution.paypal_order_id == first.execution.paypal_order_id
    # Exactly ONE create-order call reached PayPal.
    assert len(order_calls(paypal_http)) == 1
    assert len(executions()) == 1


def test_a_duplicate_endpoint_request_returns_the_same_order(
    adopted, paypal_http
):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)

    first = post_create_order(approval.approval_id).json()
    second = post_create_order(approval.approval_id).json()

    assert first["created"] is True
    assert second["created"] is False
    assert (
        second["execution"]["paypal_order_id"]
        == first["execution"]["paypal_order_id"]
    )
    assert len(order_calls(paypal_http)) == 1


def test_a_replay_still_works_after_the_approval_deadline_passes(
    adopted, paypal_http, clock
):
    """Handing back an existing order id creates nothing, so it must not
    start failing as the approved evidence ages."""
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)
    create_order_for_approval(approval.approval_id)

    clock["now"] = NOW + timedelta(seconds=TTL + 1)
    replay = create_order_for_approval(approval.approval_id)

    assert replay.created is False
    assert replay.execution.paypal_order_id == "EXTORDER123"
    assert len(order_calls(paypal_http)) == 1


def test_a_concurrent_request_during_the_paypal_call_creates_no_second_order(
    adopted, paypal_http
):
    """The dangerous race, made deterministic.

    The second request arrives while the first is still inside PayPal. The
    reservation row -- committed before the PayPal call -- is what makes the
    second one refuse instead of creating a second order.
    """
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)

    inside_paypal = threading.Event()
    release = threading.Event()
    original_post = paypal.httpx.post

    def blocking_post(url, **kwargs):
        if url.endswith(ORDERS_URL):
            inside_paypal.set()
            assert release.wait(timeout=10), "test deadlock"
        return original_post(url, **kwargs)

    paypal.httpx.post = blocking_post
    try:
        outcome: dict = {}

        def winner():
            try:
                outcome["result"] = create_order_for_approval(approval.approval_id)
            except BaseException as exc:  # pragma: no cover - diagnostics
                outcome["error"] = exc

        thread = threading.Thread(target=winner)
        thread.start()
        assert inside_paypal.wait(timeout=10), "first request never reached PayPal"

        # The first request holds the reservation and PayPal has not answered.
        error = expect_conflict(
            approval.approval_id, ExternalOrderConflict.EXECUTION_IN_PROGRESS
        )
        assert error.execution is not None
        assert error.execution.status is ExternalOrderStatus.CREATING

        release.set()
        thread.join(timeout=10)
    finally:
        paypal.httpx.post = original_post

    assert "error" not in outcome, outcome.get("error")
    assert outcome["result"].created is True
    assert len(order_calls(paypal_http)) == 1
    assert len(executions()) == 1


def test_two_parallel_requests_produce_exactly_one_execution_record(
    adopted, paypal_http
):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)

    results: list = []
    errors: list = []
    barrier = threading.Barrier(2)

    def attempt():
        barrier.wait(timeout=10)
        try:
            results.append(create_order_for_approval(approval.approval_id))
        except ExternalOrderConflictError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert len(results) + len(errors) == 2
    # Never two PayPal orders and never two rows, whichever way it raced.
    assert len(order_calls(paypal_http)) == 1
    assert len(executions()) == 1
    created = [r for r in results if r.created]
    assert len(created) == 1
    order_ids = {r.execution.paypal_order_id for r in results}
    assert order_ids == {"EXTORDER123"}
    for error in errors:
        assert error.code is ExternalOrderConflict.EXECUTION_IN_PROGRESS


# ---------------------------------------------------------------------------
# 32-36. PayPal failures never become a false success
# ---------------------------------------------------------------------------


def test_a_paypal_auth_failure_is_a_502_and_a_failed_execution(
    adopted, paypal_http
):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)
    paypal_http.state.token_status = 401

    response = post_create_order(approval.approval_id)

    assert response.status_code == 502
    assert order_calls(paypal_http) == []  # never got as far as the order
    stored = executions()
    assert len(stored) == 1
    assert stored[0].status is ExternalOrderStatus.FAILED
    assert stored[0].paypal_order_id is None


@pytest.mark.parametrize("status_code", [400, 403, 422, 429, 500, 502, 503])
def test_a_paypal_error_status_is_a_502_and_never_a_success(
    adopted, paypal_http, status_code
):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)
    paypal_http.state.order_status = status_code
    paypal_http.state.order_payload = {
        "name": "UNPROCESSABLE_ENTITY",
        "message": "upstream said no",
    }

    response = post_create_order(approval.approval_id)

    assert response.status_code == 502
    stored = executions()
    assert len(stored) == 1
    assert stored[0].status is ExternalOrderStatus.FAILED
    assert stored[0].paypal_order_id is None
    assert stored[0].paypal_amount is None


def test_a_paypal_timeout_is_a_502_and_never_a_success(adopted, paypal_http):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)
    paypal_http.state.order_raises = httpx.ConnectTimeout("timed out")

    response = post_create_order(approval.approval_id)

    assert response.status_code == 502
    assert executions()[0].status is ExternalOrderStatus.FAILED


def test_a_network_error_is_a_502_and_never_a_success(adopted, paypal_http):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)
    paypal_http.state.order_raises = httpx.ConnectError("connection refused")

    response = post_create_order(approval.approval_id)

    assert response.status_code == 502
    assert executions()[0].status is ExternalOrderStatus.FAILED


def test_a_malformed_paypal_body_is_never_a_success(adopted, paypal_http):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)
    paypal_http.state.order_payload = {"status": "CREATED"}  # no id

    response = post_create_order(approval.approval_id)

    assert response.status_code == 502
    assert executions()[0].status is ExternalOrderStatus.FAILED


def test_a_failed_execution_is_never_retried(adopted, paypal_http):
    """At-most-once ATTEMPT: a retry could duplicate an order PayPal may
    have created without telling us."""
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)
    paypal_http.state.order_status = 500

    assert post_create_order(approval.approval_id).status_code == 502

    paypal_http.state.order_status = 201  # PayPal is healthy again
    response = post_create_order(approval.approval_id)

    assert response.status_code == 409
    assert (
        response.json()["detail"]["code"]
        == ExternalOrderConflict.EXECUTION_ALREADY_FAILED.value
    )
    assert len(order_calls(paypal_http)) == 1
    assert len(executions()) == 1


def test_a_failure_does_not_mutate_the_approval(adopted, paypal_http):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)
    paypal_http.state.order_status = 500

    post_create_order(approval.approval_id)

    after = external_approval_service.get(approval.approval_id)
    assert after.status is ExternalApprovalStatus.APPROVED
    assert after.approved_at == approval.approved_at
    assert after.approved_normalized_price == approval.approved_normalized_price


def test_a_conflict_does_not_mutate_the_approval(adopted, paypal_http):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)
    tamper_approval(approval.approval_id, approved_normalized_price="1.00")

    expect_conflict(approval.approval_id, ExternalOrderConflict.PRICE_CHANGED)

    after = external_approval_service.get(approval.approval_id)
    assert after.status is ExternalApprovalStatus.APPROVED


def test_a_conflict_leaves_no_execution_row_behind(adopted, paypal_http):
    """A refusal must not block a later legitimate attempt."""
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)

    # Policy moves, then moves back: the refusal left nothing behind.
    allow_autonomous()
    expect_conflict(approval.approval_id, ExternalOrderConflict.POLICY_CHANGED)
    assert executions() == []

    require_approval()
    assert create_order_for_approval(approval.approval_id).created is True


# ---------------------------------------------------------------------------
# 40-44. Nothing that already existed was changed
# ---------------------------------------------------------------------------


def test_the_local_catalog_purchase_flow_is_unchanged(paypal_http, clock):
    """The existing catalog path still prices from products.json, still
    converts INR -> USD at the demo rate, and still writes the ledger."""
    response = client.post(
        "/purchase/approve",
        json={
            "product_id": "LAP002",
            "authorization": {
                "purchase_requested": True,
                "autonomous_purchase_allowed": False,
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "order_created"
    assert body["catalog_price_inr"] == 68990
    assert body["paypal_amount"] == "766.56"  # 68990 / 90, ROUND_HALF_UP
    assert body["paypal_currency"] == "USD"
    assert body["demo_fx_rate"] == "90"
    assert body["transaction_id"]

    ledger = transaction_service.get_transaction(body["transaction_id"])
    assert ledger.status is TransactionStatus.ORDER_CREATED
    assert ledger.paypal_amount == "766.56"
    # And the external table stayed empty: the two paths never cross.
    assert executions() == []


def test_the_external_flow_writes_no_catalog_ledger_row(adopted, paypal_http):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)

    create_order_for_approval(approval.approval_id)

    assert transaction_service.list_transactions() == []
    with pytest.raises(transaction_service.TransactionNotFoundError):
        transaction_service.get_transaction_by_order("EXTORDER123")


def test_the_policy_flow_is_unchanged(adopted, paypal_http):
    """The engine's ruling is carried verbatim, never re-derived here."""
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)

    result = create_order_for_approval(approval.approval_id)

    from app.services.external_policy_service import evaluate_adopted_product

    evaluation = evaluate_adopted_product(adopted.adopted_product_id)
    assert evaluation.policy_decision is not None
    assert result.approval.policy_decision is evaluation.policy_decision.decision
    assert (
        result.approval.policy_reason_code is evaluation.policy_decision.reason_code
    )


def test_the_adoption_and_revalidation_history_are_untouched(
    adopted, paypal_http
):
    require_approval()
    snap = snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)
    before = adoption_service.get(adopted.adopted_product_id)
    reval_count = revalidation_service.count(adopted.adopted_product_id)

    create_order_for_approval(approval.approval_id)

    assert adoption_service.get(adopted.adopted_product_id) == before
    assert revalidation_service.count(adopted.adopted_product_id) == reval_count
    assert revalidation_service.get_revalidation(snap.revalidation_id) == snap


def test_products_json_is_unchanged_by_the_order_flow(adopted, paypal_http):
    before = hashlib.sha256(PRODUCTS_JSON.read_bytes()).hexdigest()

    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)
    create_order_for_approval(approval.approval_id)

    assert hashlib.sha256(PRODUCTS_JSON.read_bytes()).hexdigest() == before


def test_the_transaction_schema_is_unchanged(adopted):
    """No external, approval or revalidation column leaked into the ledger."""
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
    for forbidden in ("approval_id", "adopted_product_id", "revalidation_id"):
        assert forbidden not in TransactionRecord.model_fields


def test_the_external_execution_schema_is_exactly_as_documented(adopted):
    # paypal_approve_url was added by Phase 11F-B to store PayPal's OWN
    # hosted-checkout href, so a replay can return a usable link instead of
    # a bare order id. It is the only column this lock has ever gained; the
    # forbidden-column assertion below is what the lock exists for.
    # Phase 11F-D added the five capture columns. They live HERE, on the
    # external table, and the forbidden list below is what the lock is for:
    # no catalog-ledger field, and no refund field in any phase.
    expected = {
        "execution_id",
        "approval_id",
        "adopted_product_id",
        "revalidation_id",
        "paypal_order_id",
        "paypal_amount",
        "paypal_currency",
        "paypal_approve_url",
        "capture_authorized_at",
        "paypal_capture_id",
        "paypal_capture_status",
        "captured_amount",
        "captured_currency",
        # Phase 11F-K.22a: the stable PayPal-Request-Id for this execution's
        # create_order call. An idempotency key, never a refund/ledger field.
        "paypal_request_id",
        # Phase 11F-K.22b: the durable single-flight claim for operator-gated
        # create-order replay recovery. An audit timestamp, never a
        # refund/ledger field.
        "recovery_claimed_at",
        # Phase 11F-K.22b ownership guard: a unique token per claim so
        # release_recovery_claim only clears the caller's own claim.
        "recovery_claim_token",
        "status",
        "created_at",
        "updated_at",
    }
    with sqlite3.connect(external_purchase_service._db_path()) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(external_purchase_service._SCHEMA)
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(external_purchase_orders)")
        }
        unique = [
            row["name"]
            for row in conn.execute(
                "PRAGMA index_list(external_purchase_orders)"
            )
            if row["unique"]
        ]
    assert columns == expected
    assert set(ExternalPurchaseOrder.model_fields) == expected
    # The point of the lock: no REFUND field and no CATALOG-LEDGER field has
    # anywhere to land in this table, in the schema or in the model. An
    # external capture id exists here since 11F-D; a refund id must not.
    for forbidden in (
        "refund_id",
        "paypal_refund_id",
        "refunded_amount",
        "refund_status",
        "transaction_id",
        "catalog_price_inr",
        "product_id",
    ):
        assert forbidden not in columns
        assert forbidden not in ExternalPurchaseOrder.model_fields
    # approval_id UNIQUE is the single-execution guarantee.
    indexed = set()
    with sqlite3.connect(external_purchase_service._db_path()) as conn:
        conn.row_factory = sqlite3.Row
        for name in unique:
            for row in conn.execute(f"PRAGMA index_info('{name}')"):
                indexed.add(row["name"])
    assert "approval_id" in indexed


def test_the_approval_schema_is_unchanged_by_this_phase(adopted):
    """11E stores its PayPal ids in its OWN table; the approval record still
    has nowhere to hold a payment field."""
    from app.schemas.external_approval import ExternalApproval

    for forbidden in (
        "paypal_order_id",
        "paypal_amount",
        "paypal_currency",
        "paypal_capture_id",
        "execution_id",
        "transaction_id",
    ):
        assert forbidden not in ExternalApproval.model_fields


# ---------------------------------------------------------------------------
# THE CRITICAL END-TO-END TEST
# ---------------------------------------------------------------------------


def test_end_to_end_external_product_to_uncaptured_paypal_order(
    adopted, paypal_http, no_ai, clock
):
    """Adopt -> revalidate -> policy -> approve -> create-order, over HTTP,
    proving every Phase 11E guarantee in one pass.

    1. an external product is adopted          (fixture, from stored evidence)
    2. it is freshly revalidated
    3. policy allows it
    4. the user explicitly approves it
    5. create-order is called with ONLY approval_id
    6. PayPal receives the server-derived amount/currency
    7. a PayPal order is created
    8. the PayPal order id is stored
    9. a client-supplied amount is impossible
   10. capture is NOT called
   11. no transaction CAPTURED state exists
   12. no refund exists
    """
    # 1. Adopted from server-side discovery evidence only.
    assert adopted.observed_price == ADOPTED_OBSERVED  # the decoy price
    assert adopted.product_identity_key == "serper:serper:111111"

    # 2. Fresh revalidation: provider asks USD 19.99 now.
    snap = snapshot(adopted.adopted_product_id)
    assert snap.revalidation_status is RevalidationStatus.REVALIDATED
    assert snap.current_observed_price == FRESH_OBSERVED
    assert snap.current_normalized_price == FRESH_NORMALIZED

    # 3. Policy, via the existing engine on the FRESH normalized price.
    allow_autonomous()
    evaluation = client.post(
        f"/discovery/adopted/{adopted.adopted_product_id}/policy-evaluate"
    ).json()
    assert evaluation["eligibility"] == "EVALUATED"
    assert evaluation["policy_decision"]["decision"] == "ALLOW_AUTONOMOUS_PURCHASE"
    assert evaluation["current_normalized_price"] == "1824.09"

    # 4. Explicit human approval. ALLOW_AUTONOMOUS_PURCHASE did not skip it.
    prepared = client.post(
        f"/discovery/adopted/{adopted.adopted_product_id}/approval"
    ).json()
    assert prepared["approvable"] is True
    approval_id = prepared["approval"]["approval_id"]
    assert prepared["approval"]["status"] == "PENDING"

    resolved = client.post(
        f"/discovery/approvals/{approval_id}/resolve", json={"approve": True}
    ).json()
    assert resolved["status"] == "APPROVED"
    assert resolved["revalidation_id"] == snap.revalidation_id

    # 5. + 9. ONLY the approval id matters; a forged amount changes nothing.
    response = post_create_order(approval_id, json=INJECTION_BODY)
    assert response.status_code == 200
    body = response.json()

    # 6. PayPal received the server-derived observed price, in USD.
    sent = order_body(paypal_http)
    assert sent["purchase_units"][0]["amount"] == {
        "currency_code": "USD",
        "value": "19.99",
    }
    assert sent["purchase_units"][0]["description"] == (
        f"{TITLE} from {MERCHANT} (PayPilot {adopted.adopted_product_id})"
    )
    assert "attacker.example" not in str(sent)
    assert "0.01" not in str(sent)

    # 7. + 8. The order exists and its id is stored server-side.
    assert body["created"] is True
    assert body["execution"]["status"] == "ORDER_CREATED"
    assert body["execution"]["paypal_order_id"] == "EXTORDER123"
    stored = external_purchase_service.get_by_approval(approval_id)
    assert stored.paypal_order_id == "EXTORDER123"
    assert stored.paypal_amount == "19.99"
    assert stored.paypal_currency == "USD"
    assert stored.revalidation_id == snap.revalidation_id
    assert stored.approval_id == approval_id

    # 10. Capture was never called -- no capture URL, and the trap never fired.
    assert body["captured"] is False
    assert not [c for c in paypal_http.calls if "/capture" in c["url"]]
    assert len(order_calls(paypal_http)) == 1

    # 11. No CAPTURED transaction anywhere, and this execution is NOT in a
    # capture state — creating an order captures nothing, even though Phase
    # 11F-D made external capture reachable through its own dedicated path.
    assert transaction_service.list_transactions() == []
    assert (
        transaction_service.list_transactions(status=TransactionStatus.CAPTURED) == []
    )
    assert executions()[0].status is ExternalOrderStatus.ORDER_CREATED
    assert executions()[0].paypal_capture_id is None
    assert executions()[0].capture_authorized_at is None

    # 12. No refund: no refund URL was called and no refund field exists.
    assert not [c for c in paypal_http.calls if "refund" in c["url"]]
    assert "refund_id" not in ExternalPurchaseOrder.model_fields
