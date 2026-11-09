"""Durable catalog purchase-attempt reservation (Phase 11F-K.19).

WHAT THIS PHASE ADDED
    The catalog flow (purchase_execution_service._create_order_result) called
    PayPal create_order BEFORE writing the local ledger row, so a crash /
    ledger-write failure in between could orphan a real PayPal order. Now a
    durable attempt reservation is COMMITTED BEFORE that PayPal call, and the
    attempt is resolved conservatively:

      - success           -> FINALIZED (after the unchanged create_transaction)
      - definitive failure -> FAILED   (no order was created)
      - ambiguous failure  -> left CREATING (order may exist; never auto-retried)

THE PROPERTIES DEFENDED HERE
    1. The reservation is committed and visible BEFORE create_order runs.
    2. It records the authoritative catalog price and the intended PayPal
       amount/currency.
    3. DENY / REQUIRE_USER_APPROVAL / invalid product create NO reservation and
       make NO PayPal call (the policy + authorization gates are untouched).
    4. A definitive pre-order failure (auth / checkout-config) -> FAILED.
    5. An ambiguous timeout / transport error -> a durable CREATING record.
    6. An unresolved (CREATING) attempt is never automatically retried.
    7. Concurrent confirmations keep the K.12 one-winner guarantee: exactly one
       order and one FINALIZED attempt.

PayPal is mocked at the service boundary (purchase_execution_service.create_order);
no real PayPal call is ever made. Local SQLite only.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import paypal
from app.main import app
from app.schemas.catalog_attempt import CatalogAttemptStatus
from app.schemas.intent import Authorization, HardConstraints
from app.schemas.session import PendingActionType
from app.services import (
    catalog_attempt_service as cas,
    purchase_execution_service as pes,
    session_service,
    transaction_service,
)
from app.services.currency_service import convert_inr_to_usd

client = TestClient(app, raise_server_exceptions=False)

PRODUCT_ID = "HDP006"  # exists in the demo catalog (boAt Rockerz 450, 1499 INR)


def _ok_order(order_id="ORDER-OK-1"):
    return {
        "order_id": order_id,
        "status": "CREATED",
        "approve_url": "https://sandbox.paypal.com/approve",
    }


def _forbid_create_order(monkeypatch):
    def bomb(*a, **k):
        raise AssertionError("create_order must not be called")

    monkeypatch.setattr(pes, "create_order", bomb)


def _creating():
    return cas.list_by_status(CatalogAttemptStatus.CREATING, limit=100)


def _finalized():
    return cas.list_by_status(CatalogAttemptStatus.FINALIZED, limit=100)


def _failed():
    return cas.list_by_status(CatalogAttemptStatus.FAILED, limit=100)


# ===========================================================================
# 1. Reservation committed BEFORE create_order
# ===========================================================================


def test_reservation_is_committed_before_create_order(monkeypatch):
    seen: dict = {}

    def fake_create_order(amount, currency="USD", description=None, request_id=None):
        # When PayPal is contacted, the reservation must already be durably
        # committed (visible on a fresh connection) and still CREATING.
        rows = _creating()
        seen["creating_at_call"] = [r.attempt_id for r in rows]
        return _ok_order()

    monkeypatch.setattr(pes, "create_order", fake_create_order)

    result = pes.approve_purchase(
        PRODUCT_ID, Authorization(purchase_requested=True)
    )

    assert result.status == "order_created"
    assert len(seen["creating_at_call"]) == 1  # committed & visible before call
    # Resolved to FINALIZED, linked to the ledger row.
    fin = _finalized()
    assert len(fin) == 1
    assert fin[0].transaction_id == result.transaction_id
    assert _creating() == []


# ===========================================================================
# 2. Reservation records authoritative price + intended amount/currency
# ===========================================================================


def test_reservation_records_authoritative_price_and_amount(monkeypatch):
    monkeypatch.setattr(pes, "create_order", lambda **k: _ok_order())

    result = pes.approve_purchase(
        PRODUCT_ID, Authorization(purchase_requested=True)
    )

    fin = _finalized()
    assert len(fin) == 1
    attempt = fin[0]
    assert attempt.product_id == PRODUCT_ID
    assert attempt.catalog_price_inr == 1499
    assert attempt.paypal_currency == "USD"
    # Exactly what the existing conversion produces and what the ledger stored.
    assert attempt.paypal_amount == convert_inr_to_usd(1499)
    assert attempt.paypal_amount == result.paypal_amount
    # Phase K.20: a finalized attempt now carries the persisted PayPal order id.
    assert attempt.paypal_order_id == result.paypal_order_id


# ===========================================================================
# 3. The policy / authorization gates still block before any reservation
# ===========================================================================


def test_deny_creates_no_reservation_and_no_paypal_call(monkeypatch):
    _forbid_create_order(monkeypatch)
    # max_price_inr=1 forces a Policy Engine DENY (R2) before any PayPal path.
    result = pes.approve_purchase(
        PRODUCT_ID,
        Authorization(purchase_requested=True),
        hard_constraints=HardConstraints(max_price_inr=1),
    )
    assert result.status == "denied"
    assert cas.count() == 0


def test_require_user_approval_unapproved_creates_no_reservation(monkeypatch):
    _forbid_create_order(monkeypatch)
    # Autonomy not granted -> REQUIRE_USER_APPROVAL (R3); execute_purchase does
    # NOT proceed to the money path, so no reservation and no PayPal call.
    result = pes.execute_purchase(
        PRODUCT_ID,
        Authorization(purchase_requested=True, autonomous_purchase_allowed=False),
    )
    assert result.status == "approval_required"
    assert cas.count() == 0


def test_invalid_product_creates_no_reservation(monkeypatch):
    _forbid_create_order(monkeypatch)
    with pytest.raises(Exception):
        pes.approve_purchase(
            "NO-SUCH-PRODUCT", Authorization(purchase_requested=True)
        )
    assert cas.count() == 0


# ===========================================================================
# 4. Definitive pre-order failure -> FAILED
# ===========================================================================


@pytest.mark.parametrize(
    "exc",
    [
        paypal.PayPalAuthError("no token"),
        paypal.PayPalCheckoutConfigError("bad return url"),
    ],
)
def test_definitive_pre_order_failure_marks_attempt_failed(monkeypatch, exc):
    def failing(**k):
        raise exc

    monkeypatch.setattr(pes, "create_order", failing)

    with pytest.raises(type(exc)):
        pes.approve_purchase(PRODUCT_ID, Authorization(purchase_requested=True))

    failed = _failed()
    assert len(failed) == 1  # terminal
    assert _creating() == []
    assert _finalized() == []
    # No ledger row was written for a definitively failed order.
    assert transaction_service.list_transactions(limit=100) == []


# ===========================================================================
# 5. Ambiguous failure -> durable CREATING (unresolved), never retried
# ===========================================================================


def test_ambiguous_timeout_leaves_durable_creating_attempt(monkeypatch):
    def timing_out(**k):
        # PayPalOrderError is what create_order raises for a transport error /
        # timeout — the ambiguous case (an order may or may not exist).
        raise paypal.PayPalOrderError("Could not reach PayPal to create an order")

    monkeypatch.setattr(pes, "create_order", timing_out)

    with pytest.raises(paypal.PayPalOrderError):
        pes.approve_purchase(PRODUCT_ID, Authorization(purchase_requested=True))

    creating = _creating()
    assert len(creating) == 1  # durable, unresolved
    assert creating[0].status is CatalogAttemptStatus.CREATING
    assert creating[0].paypal_order_id is None
    assert _failed() == []  # NOT claimed as a definitive no-order
    assert transaction_service.list_transactions(limit=100) == []


def test_ledger_write_failure_after_order_leaves_creating(monkeypatch):
    """The orphan window itself: create_order succeeds, create_transaction
    fails. The attempt stays CREATING (durable), no false success is returned."""
    import sqlite3

    monkeypatch.setattr(pes, "create_order", lambda **k: _ok_order("ORDER-ORPHAN"))

    def failing_create_transaction(**k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(pes, "create_transaction", failing_create_transaction)

    with pytest.raises(sqlite3.OperationalError):
        pes.approve_purchase(PRODUCT_ID, Authorization(purchase_requested=True))

    creating = _creating()
    assert len(creating) == 1  # the order exists at PayPal; this is the record
    assert _finalized() == []


# ===========================================================================
# 6. An unresolved attempt is never automatically retried
# ===========================================================================


def test_unresolved_attempt_is_not_auto_retried(monkeypatch):
    calls = {"n": 0}

    def timing_out(**k):
        calls["n"] += 1
        raise paypal.PayPalOrderError("timeout")

    monkeypatch.setattr(pes, "create_order", timing_out)

    with pytest.raises(paypal.PayPalOrderError):
        pes.approve_purchase(PRODUCT_ID, Authorization(purchase_requested=True))

    first = _creating()
    assert len(first) == 1 and calls["n"] == 1

    # The service exposes NO method that resumes a CREATING attempt by calling
    # create_order again — reconciliation/recovery is a later phase.
    for forbidden in ("retry_attempt", "resume_attempt", "reconcile_attempt"):
        assert not hasattr(cas, forbidden), forbidden

    # A brand-new explicit request creates a SEPARATE attempt; it never
    # resurrects or re-fires the earlier unresolved one.
    monkeypatch.setattr(pes, "create_order", lambda **k: _ok_order("ORDER-OK-2"))
    pes.approve_purchase(PRODUCT_ID, Authorization(purchase_requested=True))

    assert calls["n"] == 1  # the earlier CREATING attempt was never retried
    assert len(_creating()) == 1  # the original orphan is untouched
    assert len(_finalized()) == 1  # the new request is its own attempt
    assert cas.count() == 2


# ===========================================================================
# 7. Concurrent confirmations: K.12 one-winner, exactly one order + attempt
# ===========================================================================


def _session_with_pending_purchase():
    session = session_service.create_session()
    session_service.set_pending_action(
        session.session_id,
        PendingActionType.PURCHASE,
        product_id=PRODUCT_ID,
        decision_snapshot="REQUIRE_USER_APPROVAL",
        ttl_seconds=None,
    )
    return session


def test_concurrent_confirmations_create_exactly_one_attempt(monkeypatch):
    session = _session_with_pending_purchase()
    order_calls = {"n": 0}
    reentry = {"done": False, "second": None}

    def fake_create_order(amount, currency="USD", description=None, request_id=None):
        order_calls["n"] += 1
        if not reentry["done"]:
            # Inside the first confirm, AFTER the K.12 atomic claim consumed the
            # action. A concurrent second confirm arriving now must lose.
            reentry["done"] = True
            reentry["second"] = client.post(
                f"/sessions/{session.session_id}/confirm", json={"confirm": True}
            )
        return _ok_order(f"ORDER-ONE-{order_calls['n']}")

    monkeypatch.setattr(pes, "create_order", fake_create_order)

    first = client.post(
        f"/sessions/{session.session_id}/confirm", json={"confirm": True}
    )

    assert first.status_code == 200 and first.json()["executed"] is True
    assert reentry["second"] is not None
    assert reentry["second"].status_code == 409  # concurrent loser
    assert order_calls["n"] == 1  # exactly one PayPal order-creation call
    # Exactly one attempt, FINALIZED; no stray CREATING left behind.
    assert cas.count() == 1
    assert len(_finalized()) == 1
    assert _creating() == []
