"""Catalog order-id persistence + recovery-safe finalization (Phase 11F-K.20).

WHAT THIS PHASE ADDED (on top of the K.19 reservation layer)
    1. The PayPal order id is committed to the CREATING attempt in its own
       write BEFORE the ledger row is created, so a crash after that point
       leaves a CREATING attempt that NAMES its order (correlatable).
    2. Finalization is idempotent and recovery-safe: it LOCATES the
       transaction by its unique order id (or creates it), verifies it matches
       the attempt, then marks the attempt FINALIZED. A re-run links the
       existing transaction instead of inserting a duplicate; a product /
       amount / currency / order-id / transaction-id conflict fails closed.

THE PROPERTIES DEFENDED HERE
    - Order id committed to the attempt BEFORE create_transaction.
    - create_transaction failure after the id commit -> recoverable CREATING
      attempt that retains the order id.
    - create_transaction succeeds but finalization fails -> recovery links the
      existing transaction, never a duplicate.
    - Conflicts (order id on two attempts, mismatched product/amount/currency,
      already-finalized to a different txn) fail closed.
    - Repeated processing issues no second create_order; ambiguous timeout
      stays unresolved and is not auto-retried.
    - The K.12 concurrent-confirmation one-winner guarantee still holds.

PayPal is mocked at the service boundary; no real call is ever made. Recovery
tests arm explicit traps against unintended PayPal calls.
"""

from __future__ import annotations

import sqlite3

import pytest
from starlette.testclient import TestClient

from app import paypal
from app.main import app
from app.schemas.catalog_attempt import CatalogAttemptStatus
from app.schemas.intent import Authorization
from app.schemas.session import PendingActionType
from app.services import (
    catalog_attempt_service as cas,
    purchase_execution_service as pes,
    session_service,
    transaction_service,
)

client = TestClient(app, raise_server_exceptions=False)

PRODUCT_ID = "HDP006"  # boAt Rockerz 450, 1499 INR


def _ok_order(order_id="ORDER-OK-1"):
    return {"order_id": order_id, "status": "CREATED", "approve_url": "https://x"}


def _arm_no_paypal(monkeypatch):
    """Trap every PayPal entry point used by catalog recovery/finalize."""

    def bomb(name):
        def _b(*a, **k):
            raise AssertionError(f"recovery must not call {name}")

        return _b

    monkeypatch.setattr(pes, "create_order", bomb("create_order"))
    monkeypatch.setattr(pes, "capture_order", bomb("capture_order"))
    monkeypatch.setattr(paypal, "create_order", bomb("paypal.create_order"))
    monkeypatch.setattr(paypal, "capture_order", bomb("paypal.capture_order"))


def _creating():
    return cas.list_by_status(CatalogAttemptStatus.CREATING, limit=100)


def _finalized():
    return cas.list_by_status(CatalogAttemptStatus.FINALIZED, limit=100)


# ===========================================================================
# 1. Order id committed BEFORE create_transaction
# ===========================================================================


def test_order_id_committed_before_create_transaction(monkeypatch):
    seen: dict = {}
    real_create_transaction = pes.create_transaction

    def spy_create_transaction(**kwargs):
        # When the ledger row is about to be written, the attempt must already
        # carry the order id (committed in its own prior transaction).
        creating = _creating()
        seen["attempt_order_ids"] = [a.paypal_order_id for a in creating]
        seen["order_id_arg"] = kwargs.get("paypal_order_id")
        return real_create_transaction(**kwargs)

    monkeypatch.setattr(pes, "create_order", lambda **k: _ok_order("ORDER-SEQ"))
    monkeypatch.setattr(pes, "create_transaction", spy_create_transaction)

    result = pes.approve_purchase(PRODUCT_ID, Authorization(purchase_requested=True))

    assert seen["attempt_order_ids"] == ["ORDER-SEQ"]  # persisted before ledger
    assert seen["order_id_arg"] == "ORDER-SEQ"
    assert result.paypal_order_id == "ORDER-SEQ"
    fin = _finalized()
    assert len(fin) == 1 and fin[0].paypal_order_id == "ORDER-SEQ"
    assert fin[0].transaction_id == result.transaction_id


# ===========================================================================
# 2. Ledger insert failure after the id commit -> recoverable CREATING
# ===========================================================================


def test_ledger_insert_failure_leaves_creating_with_order_id(monkeypatch):
    monkeypatch.setattr(pes, "create_order", lambda **k: _ok_order("ORDER-ORPHAN"))

    def failing_insert(**k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(pes, "create_transaction", failing_insert)

    with pytest.raises(sqlite3.OperationalError):
        pes.approve_purchase(PRODUCT_ID, Authorization(purchase_requested=True))

    creating = _creating()
    assert len(creating) == 1
    assert creating[0].paypal_order_id == "ORDER-ORPHAN"  # correlatable orphan
    assert _finalized() == []
    assert transaction_service.list_transactions(limit=100) == []


# ===========================================================================
# 3. Transaction succeeds but finalization fails -> recovery links, no dupe
# ===========================================================================


def test_recovery_links_existing_transaction_without_duplicate(monkeypatch):
    # Drive the real flow but make mark_finalized fail AFTER create_transaction
    # committed, leaving a CREATING attempt with its order id and a real txn.
    monkeypatch.setattr(pes, "create_order", lambda **k: _ok_order("ORDER-FIN"))
    original_mark = cas.mark_finalized

    def boom_finalize(attempt_id, transaction_id):
        raise RuntimeError("mark_finalized crashed")

    monkeypatch.setattr(cas, "mark_finalized", boom_finalize)

    with pytest.raises(RuntimeError):
        pes.approve_purchase(PRODUCT_ID, Authorization(purchase_requested=True))

    # State after the crash: attempt CREATING + order id; one ledger row exists.
    creating = _creating()
    assert len(creating) == 1 and creating[0].paypal_order_id == "ORDER-FIN"
    attempt_id = creating[0].attempt_id
    txns = transaction_service.list_transactions(limit=100)
    assert len(txns) == 1 and txns[0].paypal_order_id == "ORDER-FIN"
    original_txn_id = txns[0].transaction_id

    # Recovery: restore a working mark_finalized, trap PayPal, re-run finalize.
    monkeypatch.setattr(cas, "mark_finalized", original_mark)
    _arm_no_paypal(monkeypatch)

    txn = pes.finalize_attempt(attempt_id)

    assert txn.transaction_id == original_txn_id  # linked the EXISTING row
    assert len(transaction_service.list_transactions(limit=100)) == 1  # no dupe
    refreshed = cas.get(attempt_id)
    assert refreshed.status is CatalogAttemptStatus.FINALIZED
    assert refreshed.transaction_id == original_txn_id
    # Idempotent: a second recovery is a no-op that returns the same row.
    assert pes.finalize_attempt(attempt_id).transaction_id == original_txn_id
    assert len(transaction_service.list_transactions(limit=100)) == 1


# ===========================================================================
# 4. Conflicts fail closed
# ===========================================================================


def test_duplicate_order_id_on_second_attempt_fails_closed(monkeypatch):
    _arm_no_paypal(monkeypatch)
    a = cas.reserve_attempt(PRODUCT_ID, 1499, "16.66", "USD")
    b = cas.reserve_attempt(PRODUCT_ID, 1499, "16.66", "USD")

    ok_a, _ = cas.claim_order_id(a.attempt_id, "DUP-ORDER")
    assert ok_a is True
    # The same order id can never be claimed by a second attempt.
    ok_b, rec_b = cas.claim_order_id(b.attempt_id, "DUP-ORDER")
    assert ok_b is False
    assert rec_b.paypal_order_id is None  # not stolen


def test_claim_does_not_overwrite_a_different_stored_id(monkeypatch):
    _arm_no_paypal(monkeypatch)
    a = cas.reserve_attempt(PRODUCT_ID, 1499, "16.66", "USD")
    assert cas.claim_order_id(a.attempt_id, "ORDER-A")[0] is True
    ok, rec = cas.claim_order_id(a.attempt_id, "ORDER-B")
    assert ok is False
    assert rec.paypal_order_id == "ORDER-A"  # unchanged


def test_finalize_fails_closed_on_mismatched_transaction(monkeypatch):
    _arm_no_paypal(monkeypatch)
    # An attempt whose order id points at a transaction recording a DIFFERENT
    # product/amount must never be linked.
    attempt = cas.reserve_attempt(PRODUCT_ID, 1499, "16.66", "USD")
    cas.claim_order_id(attempt.attempt_id, "ORDER-CONFLICT")
    transaction_service.create_transaction(
        product_id="SOME-OTHER-PRODUCT",
        product_name="Other",
        catalog_price_inr=9999,
        paypal_amount="99.99",
        paypal_currency="USD",
        paypal_order_id="ORDER-CONFLICT",
    )

    with pytest.raises(pes.CatalogAttemptFinalizeConflict):
        pes.finalize_attempt(attempt.attempt_id)

    assert cas.get(attempt.attempt_id).status is CatalogAttemptStatus.CREATING


def test_finalize_refuses_an_unresolved_attempt_without_order_id(monkeypatch):
    _arm_no_paypal(monkeypatch)
    attempt = cas.reserve_attempt(PRODUCT_ID, 1499, "16.66", "USD")  # no order id
    with pytest.raises(ValueError):
        pes.finalize_attempt(attempt.attempt_id)
    assert cas.get(attempt.attempt_id).status is CatalogAttemptStatus.CREATING


# ===========================================================================
# 5. No second order on repeated processing; ambiguous timeout unresolved
# ===========================================================================


def test_finalize_recovery_issues_no_second_order(monkeypatch):
    # Build a CREATING-with-id attempt + existing txn (finalize-failed state).
    monkeypatch.setattr(pes, "create_order", lambda **k: _ok_order("ORDER-NO2ND"))
    original_mark = cas.mark_finalized
    monkeypatch.setattr(
        cas, "mark_finalized", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    )
    with pytest.raises(RuntimeError):
        pes.approve_purchase(PRODUCT_ID, Authorization(purchase_requested=True))
    attempt_id = _creating()[0].attempt_id

    monkeypatch.setattr(cas, "mark_finalized", original_mark)
    _arm_no_paypal(monkeypatch)  # any create_order/capture call now fails loudly

    pes.finalize_attempt(attempt_id)  # must not contact PayPal
    assert cas.get(attempt_id).status is CatalogAttemptStatus.FINALIZED


def test_ambiguous_timeout_unresolved_and_no_id(monkeypatch):
    def timing_out(**k):
        raise paypal.PayPalOrderError("Could not reach PayPal to create an order")

    monkeypatch.setattr(pes, "create_order", timing_out)

    with pytest.raises(paypal.PayPalOrderError):
        pes.approve_purchase(PRODUCT_ID, Authorization(purchase_requested=True))

    creating = _creating()
    assert len(creating) == 1
    assert creating[0].paypal_order_id is None  # no id guessed
    assert _finalized() == []
    assert transaction_service.list_transactions(limit=100) == []


# ===========================================================================
# 6. K.12 concurrent-confirmation protection still holds
# ===========================================================================


def test_concurrent_confirmations_still_one_order_and_attempt(monkeypatch):
    session = session_service.create_session()
    session_service.set_pending_action(
        session.session_id,
        PendingActionType.PURCHASE,
        product_id=PRODUCT_ID,
        decision_snapshot="REQUIRE_USER_APPROVAL",
        ttl_seconds=None,
    )
    order_calls = {"n": 0}
    reentry = {"done": False, "second": None}

    def fake_create_order(amount, currency="USD", description=None, request_id=None):
        order_calls["n"] += 1
        if not reentry["done"]:
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
    assert reentry["second"].status_code == 409
    assert order_calls["n"] == 1
    assert cas.count() == 1
    fin = _finalized()
    assert len(fin) == 1 and fin[0].paypal_order_id == "ORDER-ONE-1"
    assert len(transaction_service.list_transactions(limit=100)) == 1
