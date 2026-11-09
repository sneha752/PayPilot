"""Reliability regression tests for the five gaps from the Phase 11F-K.10 audit.

TESTS ONLY — no production code is changed here. Every PayPal interaction is
mocked at a service boundary (``purchase_execution_service.create_order`` /
``.capture_order`` and ``refund_service.refund_capture``), so no real PayPal
call, order, capture, or refund is ever made.

PURCHASE confirmation is now fixed (Phase 11F-K.12): an ATOMIC claim consumes
the pending purchase action before any PayPal call, so concurrent
confirmations cannot both create an order. The purchase tests here assert that
invariant (exactly one winner). The REFUND concurrency gap is intentionally
NOT fixed in this phase, so its test still pins the current (racy) behavior as
a regression target, clearly labeled.

The endpoint-level concurrency tests reproduce the race DETERMINISTICALLY by
re-entering the endpoint at the money-call boundary a concurrent request would
occupy; a separate test drives the atomic claim primitive with REAL threads
and a barrier to exercise database-level serialization directly. Both avoid
arbitrary sleeps and cannot hang (a bounded busy_timeout plus join timeouts).
"""

import sqlite3
import threading

import pytest
from starlette.testclient import TestClient

from app import paypal
from app.main import app
from app.schemas.intent import Authorization
from app.schemas.session import PendingActionType
from app.schemas.transaction import TransactionStatus
from app.services import (
    purchase_execution_service,
    refund_service,
    session_service,
    transaction_service,
)
from app.services.webhook_reconciliation_service import reconcile_event

client = TestClient(app, raise_server_exceptions=False)


# --- shared helpers (mirroring test_confirmation.py conventions) -------------


def _session_with_pending_purchase(product_id="HDP006"):
    session = session_service.create_session()
    session_service.set_pending_action(
        session.session_id,
        PendingActionType.PURCHASE,
        product_id=product_id,
        decision_snapshot="REQUIRE_USER_APPROVAL",
        ttl_seconds=None,
    )
    return session


def _confirm(session_id):
    return client.post(f"/sessions/{session_id}/confirm", json={"confirm": True})


@pytest.fixture
def creds(monkeypatch):
    """Inert PayPal env (never real credentials). PayPal is mocked at the
    service boundary, so these are only here for parity with the existing
    confirmation tests."""
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("PAYPAL_ENVIRONMENT", "sandbox")


def _mock_order_boundary(monkeypatch):
    """Replace purchase_execution_service.create_order with a counting fake
    that returns a valid order dict. Returns the call counter."""
    calls = {"n": 0}

    def fake_create_order(amount, currency="USD", description=None, request_id=None):
        calls["n"] += 1
        return {
            "order_id": f"ORDER-OK-{calls['n']}",
            "status": "CREATED",
            "approve_url": "https://sandbox.paypal.com/approve",
        }

    monkeypatch.setattr(purchase_execution_service, "create_order", fake_create_order)
    return calls


def _captured_txn(order_id="ORDER-RF", capture_id="CAP-RF"):
    """A CAPTURED transaction with a stored capture id — the refund precondition."""
    txn = transaction_service.create_transaction(
        product_id="HDP006",
        product_name="boAt Rockerz 450",
        catalog_price_inr=1499,
        paypal_amount="16.66",
        paypal_currency="USD",
        paypal_order_id=order_id,
    )
    return transaction_service.update_status(
        txn.transaction_id, TransactionStatus.CAPTURED, capture_id=capture_id
    )


def _mock_refund_boundary(monkeypatch):
    """Replace refund_service.refund_capture with a counting success fake."""
    calls = {"n": 0}

    def fake_refund_capture(capture_id):
        calls["n"] += 1
        return {
            "refund_id": f"REF-{calls['n']}",
            "refund_status": "COMPLETED",
            "refunded_amount": "16.66",
            "currency": "USD",
        }

    monkeypatch.setattr(refund_service, "refund_capture", fake_refund_capture)
    return calls


def _forbid_refund_boundary(monkeypatch):
    """Make any refund_capture call fail the test (asserts PayPal is untouched)."""

    def forbidden(capture_id):
        raise AssertionError("refund_capture must not be called")

    monkeypatch.setattr(refund_service, "refund_capture", forbidden)


# --- 1. Concurrent double-confirm is now prevented (Phase 11F-K.12) ----------


def test_concurrent_double_confirm_creates_exactly_one_order(monkeypatch):
    """INVARIANT (post-fix): one pending purchase -> exactly one PayPal
    order-creation call and exactly one ledger row, even when a second
    confirmation arrives concurrently.

    The atomic claim consumes the pending action BEFORE create_order runs, so
    a concurrent confirm re-entering at the create_order boundary finds the
    action already gone and loses with a controlled 409 — it never creates a
    second order. Deterministic (re-entrant), no threads, no sleeps.
    """
    session = _session_with_pending_purchase()
    order_calls = {"n": 0}
    reentry = {"done": False, "second_response": None}

    def fake_create_order(amount, currency="USD", description=None, request_id=None):
        order_calls["n"] += 1
        my_n = order_calls["n"]
        if not reentry["done"]:
            # Inside the FIRST confirm, AFTER it atomically claimed/consumed
            # the action. A concurrent second confirm arriving now must lose.
            reentry["done"] = True
            reentry["second_response"] = _confirm(session.session_id)
        return {
            "order_id": f"ORDER-ONE-{my_n}",
            "status": "CREATED",
            "approve_url": "https://sandbox.paypal.com/approve",
        }

    monkeypatch.setattr(
        purchase_execution_service, "create_order", fake_create_order
    )

    first = _confirm(session.session_id)

    # Exactly one winner.
    assert first.status_code == 200 and first.json()["executed"] is True
    # The concurrent loser got a controlled conflict and did NOT create an order.
    assert reentry["second_response"] is not None
    assert reentry["second_response"].status_code == 409
    assert "executed" not in reentry["second_response"].json()

    assert order_calls["n"] == 1  # exactly one PayPal order-creation call
    one_rows = [
        r
        for r in transaction_service.list_transactions(limit=100)
        if r.paypal_order_id.startswith("ORDER-ONE-")
    ]
    assert len(one_rows) == 1  # exactly one ledger row


def test_atomic_claim_only_one_of_two_threads_wins():
    """Database-level invariant: two threads calling claim_pending_action for
    the SAME pending action simultaneously — exactly one receives it, the
    other receives None, and the action ends up consumed.

    Uses a real barrier so both threads hit the claim at once, exercising the
    BEGIN IMMEDIATE + busy_timeout serialization directly. Bounded by a join
    timeout so it can never hang.
    """
    session = _session_with_pending_purchase()
    barrier = threading.Barrier(2)
    results: list[object] = []
    lock = threading.Lock()

    def worker():
        barrier.wait(timeout=5)
        claimed = session_service.claim_pending_action(
            session.session_id, PendingActionType.PURCHASE
        )
        with lock:
            results.append(claimed)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive()  # never hangs

    winners = [r for r in results if r is not None]
    losers = [r for r in results if r is None]
    assert len(winners) == 1  # exactly one claim succeeded
    assert len(losers) == 1
    assert winners[0].product_id == "HDP006"
    # The action is consumed afterwards.
    assert session_service.get_pending_action(session.session_id) is None


def test_second_confirmation_after_claim_returns_409(creds, monkeypatch):
    """A second confirmation after the action was claimed/consumed is a
    controlled 409 and issues no further PayPal order-creation call."""
    calls = _mock_order_boundary(monkeypatch)
    session = _session_with_pending_purchase()

    first = _confirm(session.session_id)
    assert first.status_code == 200 and first.json()["executed"] is True

    second = _confirm(session.session_id)
    assert second.status_code == 409
    assert calls["n"] == 1  # only the first confirmation created an order


def test_ordinary_successful_confirmation(creds, monkeypatch):
    """Ordinary confirmation still succeeds: one order, action consumed."""
    calls = _mock_order_boundary(monkeypatch)
    session = _session_with_pending_purchase()

    resp = _confirm(session.session_id)
    body = resp.json()
    assert resp.status_code == 200
    assert body["executed"] is True
    assert body["purchase_result"]["status"] == "order_created"
    assert calls["n"] == 1
    assert session_service.get_pending_action(session.session_id) is None


def test_policy_denial_blocks_paypal_and_consumes_action(creds, monkeypatch):
    """Policy DENY: no PayPal order is created, and because the claim consumed
    the action, a re-confirm is a 409 (the dead proposal cannot be revived)."""
    session = _session_with_pending_purchase()

    # Force a deterministic DENY (no AI, no autonomy) by making the confirm
    # path evaluate the purchase against an impossible shopping ceiling, which
    # the Policy Engine rejects with EXCEEDS_SHOPPING_MAX before any PayPal call.
    from app.schemas.intent import HardConstraints

    def deny_create_order(*args, **kwargs):
        raise AssertionError("create_order must not be called on a policy DENY")

    monkeypatch.setattr(purchase_execution_service, "create_order", deny_create_order)
    monkeypatch.setattr(
        "app.services.confirmation_service.approve_purchase",
        lambda product_id, authorization: purchase_execution_service.approve_purchase(
            product_id=product_id,
            authorization=authorization,
            hard_constraints=HardConstraints(max_price_inr=1),
        ),
    )

    resp = _confirm(session.session_id)
    body = resp.json()
    assert resp.status_code == 200  # the confirm call itself succeeds...
    assert body["confirmed"] is True
    assert body["executed"] is False  # ...but nothing was executed (DENY)
    assert body["purchase_result"]["status"] == "denied"
    # Action consumed by the claim: a re-confirm cannot revive the dead proposal.
    assert _confirm(session.session_id).status_code == 409


def test_definitive_auth_failure_restores_action_and_retry_succeeds_once(
    creds, monkeypatch
):
    """A DEFINITIVE pre-order failure (auth token fetch, which precedes the
    create-order POST, so no order exists) restores the claimed action so an
    explicit re-confirm is a safe retry that creates exactly one order."""
    session = _session_with_pending_purchase()

    def failing_create_order(amount, currency="USD", description=None, request_id=None):
        raise paypal.PayPalAuthError("could not obtain a token")

    monkeypatch.setattr(
        purchase_execution_service, "create_order", failing_create_order
    )
    first = _confirm(session.session_id)
    assert first.status_code == 502
    # Definitive no-order: the action was restored for a safe retry.
    assert session_service.get_pending_action(session.session_id) is not None

    retry_calls = _mock_order_boundary(monkeypatch)
    retry = _confirm(session.session_id)
    assert retry.status_code == 200 and retry.json()["executed"] is True
    assert retry_calls["n"] == 1
    assert session_service.get_pending_action(session.session_id) is None


def test_ambiguous_paypal_failure_consumes_action_and_blocks_reconfirm(
    creds, monkeypatch
):
    """Phase 11F-K.21: an AMBIGUOUS create-order failure (PayPalOrderError —
    e.g. an HTTP 500 or a timeout, where PayPal MAY have created an order) now
    CONSUMES the pending action instead of restoring it. A re-confirm is a
    controlled 409 that issues NO second create_order, so an order that may
    already exist can never be duplicated; the unresolved attempt is left for
    read-first reconciliation."""
    session = _session_with_pending_purchase()
    calls = {"n": 0}

    def failing_create_order(amount, currency="USD", description=None, request_id=None):
        calls["n"] += 1
        raise paypal.PayPalOrderError("PayPal returned HTTP 500 for create-order")

    monkeypatch.setattr(
        purchase_execution_service, "create_order", failing_create_order
    )
    first = _confirm(session.session_id)
    assert first.status_code == 502
    assert calls["n"] == 1
    # The action is CONSUMED (not restored): no safe retry of an ambiguous order.
    assert session_service.get_pending_action(session.session_id) is None

    # A re-confirm finds no pending action and never calls create_order again.
    second = _confirm(session.session_id)
    assert second.status_code == 409
    assert calls["n"] == 1  # still exactly one create-order attempt


# --- 2. Capture timeout, then webhook recovery -------------------------------


def test_capture_timeout_then_webhook_reconciles_to_captured(monkeypatch):
    """An ambiguous capture failure leaves the ledger ORDER_CREATED; a later
    verified PAYMENT.CAPTURE.COMPLETED event reconciles it to CAPTURED without
    issuing a second capture (reconciliation is bookkeeping only)."""
    txn = transaction_service.create_transaction(
        product_id="HDP006",
        product_name="boAt Rockerz 450",
        catalog_price_inr=1499,
        paypal_amount="16.66",
        paypal_currency="USD",
        paypal_order_id="ORDER-RECOVER",
    )
    assert txn.status == TransactionStatus.ORDER_CREATED

    # Ambiguous capture: the HTTP call fails (e.g. timeout) — ledger unchanged.
    def timing_out_capture(order_id):
        raise paypal.PayPalCaptureError("Could not reach PayPal to capture the order")

    monkeypatch.setattr(
        purchase_execution_service, "capture_order", timing_out_capture
    )
    resp = client.post("/purchase/capture/ORDER-RECOVER")
    assert resp.status_code == 502
    assert (
        transaction_service.get_transaction_by_order("ORDER-RECOVER").status
        == TransactionStatus.ORDER_CREATED
    )

    # Guard: reconciliation must NEVER call capture. If it does, fail loudly.
    capture_calls = {"n": 0}

    def forbidden_capture(*args, **kwargs):
        capture_calls["n"] += 1
        raise AssertionError("Webhook reconciliation must not capture")

    monkeypatch.setattr(paypal, "capture_order", forbidden_capture)

    event = {
        "id": "WH-EVT-RECOVER",
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {
            "id": "CAP-RECOVER",
            "status": "COMPLETED",
            "supplementary_data": {"related_ids": {"order_id": "ORDER-RECOVER"}},
        },
    }
    result = reconcile_event(event)

    assert result["action"] == "updated"
    assert result["old_status"] == "ORDER_CREATED"
    assert result["status"] == "CAPTURED"
    recovered = transaction_service.get_transaction_by_order("ORDER-RECOVER")
    assert recovered.status == TransactionStatus.CAPTURED
    assert recovered.paypal_capture_id == "CAP-RECOVER"
    assert capture_calls["n"] == 0  # no second capture was issued


# --- 3. Capture returns PENDING ---------------------------------------------


def test_capture_pending_is_not_marked_captured(monkeypatch):
    """A nested capture status other than COMPLETED (e.g. PENDING) is not a
    collection: the endpoint reports non-success and the ledger keeps its
    prior ORDER_CREATED state."""
    transaction_service.create_transaction(
        product_id="HDP006",
        product_name="boAt Rockerz 450",
        catalog_price_inr=1499,
        paypal_amount="16.66",
        paypal_currency="USD",
        paypal_order_id="ORDER-PENDING",
    )

    def pending_capture(order_id):
        return {
            "order_id": "ORDER-PENDING",
            "status": "COMPLETED",  # order-level; the NESTED capture is PENDING
            "capture_id": "CAP-PENDING",
            "capture_status": "PENDING",
            "amount": "16.66",
            "currency": "USD",
        }

    monkeypatch.setattr(purchase_execution_service, "capture_order", pending_capture)

    resp = client.post("/purchase/capture/ORDER-PENDING")
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["code"] == "CAPTURE_NOT_COMPLETED"
    assert detail["captured"] is False
    assert detail["capture_status"] == "PENDING"
    # The ledger must NOT have advanced to CAPTURED.
    assert (
        transaction_service.get_transaction_by_order("ORDER-PENDING").status
        == TransactionStatus.ORDER_CREATED
    )


# --- 4. Order created but ledger write fails (orphan risk) -------------------


def test_order_created_but_ledger_write_fails_surfaces_no_false_success(monkeypatch):
    """If PayPal order creation succeeds but the ledger INSERT then fails, the
    error is surfaced (no ExecutionResult is returned) and no transaction row
    is fabricated. This documents an ORPHAN-ORDER risk: a real PayPal order
    now exists with no local record, and the current implementation has no
    automatic recovery for it (manual reconciliation, or a later webhook that
    will find no matching transaction)."""
    created = {"n": 0}

    def fake_create_order(amount, currency="USD", description=None, request_id=None):
        created["n"] += 1
        return {
            "order_id": "ORDER-ORPHAN",
            "status": "CREATED",
            "approve_url": "https://sandbox.paypal.com/approve",
        }

    def failing_create_transaction(**kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(purchase_execution_service, "create_order", fake_create_order)
    monkeypatch.setattr(
        purchase_execution_service, "create_transaction", failing_create_transaction
    )

    # The DB failure propagates; no false "order_created" result is returned.
    with pytest.raises(sqlite3.OperationalError):
        purchase_execution_service.approve_purchase(
            product_id="HDP006",
            authorization=Authorization(purchase_requested=True),
        )

    assert created["n"] == 1  # the order WAS created at PayPal...
    # ...but no ledger row tracks it: the order is orphaned (documented risk).
    with pytest.raises(transaction_service.TransactionNotFoundError):
        transaction_service.get_transaction_by_order("ORDER-ORPHAN")


# --- 5. Atomic refund protection (Phase 11F-K.13) ----------------------------


def test_concurrent_refund_calls_paypal_at_most_once(monkeypatch):
    """INVARIANT (post-fix): one CAPTURED transaction -> at most one PayPal
    refund call under concurrent refunds, and the concurrent loser receives a
    controlled 409. The atomic claim (CAPTURED -> REFUNDING) commits before the
    PayPal call, so a concurrent request re-entering at the refund_capture
    boundary finds REFUNDING and loses. Deterministic (re-entrant)."""
    txn = _captured_txn()
    refund_calls = {"n": 0}
    reentry = {"done": False, "second": None}

    def fake_refund_capture(capture_id):
        refund_calls["n"] += 1
        if not reentry["done"]:
            # Inside the FIRST refund, AFTER the atomic claim committed. A
            # concurrent second refund arriving now must lose with a 409.
            reentry["done"] = True
            reentry["second"] = client.post(
                f"/transactions/{txn.transaction_id}/refund"
            )
        return {
            "refund_id": "REF-1",
            "refund_status": "COMPLETED",
            "refunded_amount": "16.66",
            "currency": "USD",
        }

    monkeypatch.setattr(refund_service, "refund_capture", fake_refund_capture)

    first = client.post(f"/transactions/{txn.transaction_id}/refund")

    assert first.status_code == 200 and first.json()["status"] == "refunded"
    # Concurrent loser: controlled conflict, no second PayPal refund.
    assert reentry["second"] is not None
    assert reentry["second"].status_code == 409
    assert refund_calls["n"] == 1  # PayPal refund called at most once
    assert (
        transaction_service.get_transaction(txn.transaction_id).status
        == TransactionStatus.REFUNDED
    )


def test_atomic_refund_claim_only_one_of_two_threads_wins():
    """Database-level invariant: two threads calling claim_for_refund for the
    same CAPTURED transaction simultaneously — exactly one wins, the other
    loses, and the row ends REFUNDING. Real barrier, bounded join, no hang."""
    txn = _captured_txn()
    barrier = threading.Barrier(2)
    results: list[bool] = []
    lock = threading.Lock()

    def worker():
        barrier.wait(timeout=5)
        won = transaction_service.claim_for_refund(txn.transaction_id)
        with lock:
            results.append(won)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive()

    assert results.count(True) == 1  # exactly one winner
    assert results.count(False) == 1
    assert (
        transaction_service.get_transaction(txn.transaction_id).status
        == TransactionStatus.REFUNDING
    )


def test_normal_successful_refund(monkeypatch):
    """Ordinary refund still works: one PayPal call, ledger REFUNDED."""
    txn = _captured_txn()
    calls = _mock_refund_boundary(monkeypatch)
    resp = client.post(f"/transactions/{txn.transaction_id}/refund")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "refunded"
    assert body["refund_status"] == "COMPLETED"
    assert calls["n"] == 1
    assert (
        transaction_service.get_transaction(txn.transaction_id).status
        == TransactionStatus.REFUNDED
    )


def test_refund_ineligible_not_captured(monkeypatch):
    """A non-CAPTURED transaction is not refundable and PayPal is not called."""
    _forbid_refund_boundary(monkeypatch)
    txn = transaction_service.create_transaction(  # stays ORDER_CREATED
        product_id="HDP006",
        product_name="boAt Rockerz 450",
        catalog_price_inr=1499,
        paypal_amount="16.66",
        paypal_currency="USD",
        paypal_order_id="ORDER-INELIGIBLE",
    )
    resp = client.post(f"/transactions/{txn.transaction_id}/refund")
    assert resp.status_code == 409
    assert (
        transaction_service.get_transaction(txn.transaction_id).status
        == TransactionStatus.ORDER_CREATED
    )


def test_refund_already_refunded(monkeypatch):
    """An already-REFUNDED transaction is a 409 with no PayPal call."""
    _forbid_refund_boundary(monkeypatch)
    txn = _captured_txn(order_id="ORDER-DONE", capture_id="CAP-DONE")
    transaction_service.update_status(txn.transaction_id, TransactionStatus.REFUNDED)
    resp = client.post(f"/transactions/{txn.transaction_id}/refund")
    assert resp.status_code == 409


def test_definitive_rejection_releases_claim_and_retry_succeeds_once(monkeypatch):
    """A DEFINITIVE PayPal rejection (no funds moved) releases the claim back
    to CAPTURED; an explicit retry then refunds exactly once."""
    txn = _captured_txn(order_id="ORDER-REJ", capture_id="CAP-REJ")

    def definitive_failure(capture_id):
        raise paypal.PayPalRefundError("PayPal returned HTTP 422 for refund")

    monkeypatch.setattr(refund_service, "refund_capture", definitive_failure)
    first = client.post(f"/transactions/{txn.transaction_id}/refund")
    assert first.status_code == 502
    # Claim released back to CAPTURED → retryable.
    assert (
        transaction_service.get_transaction(txn.transaction_id).status
        == TransactionStatus.CAPTURED
    )

    calls = _mock_refund_boundary(monkeypatch)
    retry = client.post(f"/transactions/{txn.transaction_id}/refund")
    assert retry.status_code == 200 and retry.json()["status"] == "refunded"
    assert calls["n"] == 1
    assert (
        transaction_service.get_transaction(txn.transaction_id).status
        == TransactionStatus.REFUNDED
    )


def test_ambiguous_timeout_leaves_refunding_blocks_retry_then_webhook_reconciles(
    monkeypatch,
):
    """An AMBIGUOUS outcome (transport timeout) leaves the row REFUNDING and is
    NOT auto-retried: a second refund is a 409 with no PayPal call. A verified
    PAYMENT.CAPTURE.REFUNDED webhook then reconciles REFUNDING -> REFUNDED."""
    txn = _captured_txn(order_id="ORDER-AMB", capture_id="CAP-AMB")

    def timeout(capture_id):
        raise paypal.PayPalRefundNetworkError(
            "Could not reach PayPal to refund the capture"
        )

    monkeypatch.setattr(refund_service, "refund_capture", timeout)
    first = client.post(f"/transactions/{txn.transaction_id}/refund")
    assert first.status_code == 502
    # Ambiguous: left REFUNDING, NOT restored to CAPTURED.
    assert (
        transaction_service.get_transaction(txn.transaction_id).status
        == TransactionStatus.REFUNDING
    )

    # A retry must NOT issue a second refund — it is a controlled conflict.
    _forbid_refund_boundary(monkeypatch)
    retry = client.post(f"/transactions/{txn.transaction_id}/refund")
    assert retry.status_code == 409

    # Reconciliation: a verified PAYMENT.CAPTURE.REFUNDED event resolves it.
    event = {
        "id": "WH-REFUND-AMB",
        "event_type": "PAYMENT.CAPTURE.REFUNDED",
        "resource": {
            "id": "REFUND-AMB",
            "supplementary_data": {
                "related_ids": {"order_id": "ORDER-AMB", "capture_id": "CAP-AMB"}
            },
        },
    }
    result = reconcile_event(event)
    assert result["action"] == "updated"
    assert result["status"] == "REFUNDED"
    assert (
        transaction_service.get_transaction(txn.transaction_id).status
        == TransactionStatus.REFUNDED
    )


def test_refund_success_then_ledger_write_failure_surfaces_desync(monkeypatch):
    """A successful PayPal refund followed by a failing ledger write surfaces a
    REFUND_LEDGER_DESYNC (never a false success); the row is left REFUNDING."""
    txn = _captured_txn(order_id="ORDER-DESYNC", capture_id="CAP-DESYNC")
    _mock_refund_boundary(monkeypatch)

    def failing_update(transaction_id, status, capture_id=None):
        raise transaction_service.TransactionNotFoundError("row vanished")

    monkeypatch.setattr(refund_service, "update_status", failing_update)

    resp = client.post(f"/transactions/{txn.transaction_id}/refund")
    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert detail["code"] == "REFUND_LEDGER_DESYNC"
    assert detail["paypal_refunded"] is True
    assert detail["rolled_back"] is False
    # The row is left REFUNDING for reconciliation (not a false REFUNDED).
    assert (
        transaction_service.get_transaction(txn.transaction_id).status
        == TransactionStatus.REFUNDING
    )
