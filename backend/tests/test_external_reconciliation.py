"""Webhook-triggered external-order reconciliation (Phase 11F-F).

A VERIFIED PayPal webhook is only a TRIGGER. Reconciliation resolves the
event to one of this server's own external_purchase_orders rows by a
SERVER-STORED order/capture id, performs an INDEPENDENT read-only get_order,
and promotes the record ONLY on conclusive, independently verified evidence,
using a database compare-and-set. It moves no money.

Everything PayPal is mocked. No live order, approval, capture or refund
happens anywhere in this file. The catalog capture/refund surfaces are
booby-trapped so a reconciliation that ever reached them would fail loudly.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from app import paypal
from app.main import app
from app.schemas.external_purchase import ExternalOrderStatus
from app.services import (
    external_purchase_service as eps,
    external_reconciliation_service as recon,
    refund_service,
    transaction_service,
    webhook_service,
)

client = TestClient(app, raise_server_exceptions=False)

#: The genuine catalog writer, captured at import time — before the autouse
#: money trap replaces the module attribute. Restored in the one test that
#: legitimately exercises catalog reconciliation.
REAL_MARK_CAPTURED = transaction_service.mark_captured

WEBHOOK_URL = "/webhooks/paypal"
ORDER_ID = "EXTORDER-RECON-1"
CAPTURE_ID = "EXTCAP-RECON-1"
AMOUNT = "19.99"
CURRENCY = "USD"

GOOD_HEADERS = {
    "PAYPAL-TRANSMISSION-ID": "tid-123",
    "PAYPAL-TRANSMISSION-TIME": "2026-10-11T10:00:00Z",
    "PAYPAL-TRANSMISSION-SIG": "sig-abc",
    "PAYPAL-CERT-URL": "https://api.sandbox.paypal.com/cert.pem",
    "PAYPAL-AUTH-ALGO": "SHA256withRSA",
}


# ---------------------------------------------------------------------------
# PayPal HTTP (signature verification) mock — mirrors test_webhooks.py
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


TOKEN_OK = FakeResponse(200, {"access_token": "fake-token", "token_type": "Bearer"})
VERIFY_SUCCESS = FakeResponse(200, {"verification_status": "SUCCESS"})
VERIFY_FAILURE = FakeResponse(200, {"verification_status": "FAILURE"})


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("PAYPAL_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("PAYPAL_WEBHOOK_ID", "test-webhook-id")


def mock_signature(monkeypatch, verify_response=VERIFY_SUCCESS):
    def fake_post(url, **kwargs):
        if url.endswith("/v1/oauth2/token"):
            return TOKEN_OK
        if url.endswith("/v1/notifications/verify-webhook-signature"):
            return verify_response
        raise AssertionError(f"Unexpected PayPal URL called: {url}")

    monkeypatch.setattr(webhook_service.httpx, "post", fake_post)


# ---------------------------------------------------------------------------
# Money-safety trap — NO reconciliation path may capture or refund.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def money_is_untouchable(monkeypatch):
    hits: list[str] = []

    def bomb(name):
        def _bomb(*args, **kwargs):
            hits.append(name)
            raise AssertionError(f"reconciliation must never call {name}")

        return _bomb

    # The live money-moving calls, trapped wherever they are reachable.
    monkeypatch.setattr(eps, "capture_order", bomb("capture_order"))
    monkeypatch.setattr(paypal, "capture_order", bomb("paypal.capture_order"))
    monkeypatch.setattr(paypal, "refund_capture", bomb("paypal.refund_capture"))
    monkeypatch.setattr(refund_service, "refund_capture", bomb("refund_capture"))
    # The catalog ledger must never be written by external reconciliation.
    monkeypatch.setattr(transaction_service, "create_transaction", bomb("catalog ledger"))
    monkeypatch.setattr(transaction_service, "mark_captured", bomb("catalog ledger"))
    return hits


# ---------------------------------------------------------------------------
# Independent read (get_order) mock, at the service boundary.
# ---------------------------------------------------------------------------


@pytest.fixture
def reads(monkeypatch):
    state = SimpleNamespace(order=None, order_raises=None, get_calls=[])

    def fake_get_order(order_id):
        state.get_calls.append(order_id)
        if state.order_raises is not None:
            raise state.order_raises
        return state.order if state.order is not None else approved_order()

    monkeypatch.setattr(eps, "get_order", fake_get_order)
    return state


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def approved_order(order_id=ORDER_ID, captures=None, status="APPROVED"):
    return {
        "order_id": order_id,
        "order_status": status,
        "intent": "CAPTURE",
        "purchase_units": [{"amount": {"currency_code": CURRENCY, "value": AMOUNT}}],
        "captures": captures if captures is not None else [],
        "links": [],
        "approve_url": None,
    }


def one_completed_capture(capture_id=CAPTURE_ID, value=AMOUNT, currency=CURRENCY):
    return [{"id": capture_id, "status": "COMPLETED",
             "amount": {"currency_code": currency, "value": value}}]


def capture_event(event_type, order_id=ORDER_ID, capture_id=CAPTURE_ID):
    return {
        "id": f"WH-{uuid.uuid4().hex[:8]}",
        "event_type": event_type,
        "resource": {
            "id": capture_id,
            "status": "COMPLETED",
            "supplementary_data": {"related_ids": {"order_id": order_id}},
        },
    }


def order_approved_event(order_id=ORDER_ID):
    return {
        "id": f"WH-{uuid.uuid4().hex[:8]}",
        "event_type": "CHECKOUT.ORDER.APPROVED",
        "resource": {"id": order_id, "status": "APPROVED"},
    }


def insert_execution(
    status,
    *,
    approval_id=None,
    order_id=ORDER_ID,
    amount=AMOUNT,
    currency=CURRENCY,
    capture_id=None,
    capture_status=None,
    captured_amount=None,
    captured_currency=None,
):
    """Seed one external_purchase_orders row directly, deterministically."""
    approval_id = approval_id or f"appr_{uuid.uuid4().hex}"
    execution_id = f"xord_{uuid.uuid4().hex}"
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(eps._db_path()) as conn:
        conn.row_factory = sqlite3.Row  # _migrate reads PRAGMA rows by name
        conn.executescript(eps._SCHEMA)
        eps._migrate(conn)
        conn.execute(
            f"""INSERT INTO external_purchase_orders ({", ".join(eps._COLUMNS)})
                VALUES ({", ".join("?" * len(eps._COLUMNS))})""",
            (
                execution_id,
                approval_id,
                "adopt_x",
                "reval_x",
                order_id,
                amount,
                currency,
                None,            # paypal_approve_url
                None,            # capture_authorized_at
                capture_id,
                capture_status,
                captured_amount,
                captured_currency,
                status.value if isinstance(status, ExternalOrderStatus) else status,
                now,
                now,
            ),
        )
        conn.commit()
    record = eps.get(execution_id)
    assert record is not None
    return record


def status_of(execution_id):
    return eps.get(execution_id).status


# ===========================================================================
# Scope B.1 — ORDER_CREATED -> BUYER_APPROVED on an independent APPROVED read
# ===========================================================================


def test_order_created_becomes_buyer_approved_when_paypal_confirms(reads):
    ex = insert_execution(ExternalOrderStatus.ORDER_CREATED)
    reads.order = approved_order()

    outcome, updated, detail = eps.reconcile_buyer_approval(ex)

    assert outcome == "approved"
    assert updated.status is ExternalOrderStatus.BUYER_APPROVED
    assert reads.get_calls == [ORDER_ID]  # it really read PayPal independently


def test_order_not_approved_is_not_promoted(reads):
    ex = insert_execution(ExternalOrderStatus.ORDER_CREATED)
    reads.order = approved_order(status="CREATED")

    outcome, updated, detail = eps.reconcile_buyer_approval(ex)

    assert outcome == "order_not_approved"
    assert updated.status is ExternalOrderStatus.ORDER_CREATED


@pytest.mark.parametrize("seed", [ExternalOrderStatus.CREATING, ExternalOrderStatus.FAILED])
def test_approval_not_applicable_for_creating_or_failed(reads, seed):
    ex = insert_execution(seed, order_id=None if seed is ExternalOrderStatus.CREATING else ORDER_ID)
    outcome, updated, _ = eps.reconcile_buyer_approval(ex)
    assert outcome == "not_applicable"
    assert updated.status is seed
    assert reads.get_calls == []  # no PayPal read for a non-reconcilable state


def test_approval_replay_is_idempotent_without_a_paypal_call(reads):
    ex = insert_execution(ExternalOrderStatus.BUYER_APPROVED)
    outcome, updated, _ = eps.reconcile_buyer_approval(ex)
    assert outcome == "already_applied"
    assert updated.status is ExternalOrderStatus.BUYER_APPROVED
    assert reads.get_calls == []


# ===========================================================================
# Scope B.2/B.3 — CAPTURING/CAPTURE_PENDING -> CAPTURED, strictly verified
# ===========================================================================


def test_capturing_becomes_captured_with_one_matching_capture(reads):
    ex = insert_execution(ExternalOrderStatus.CAPTURING)
    reads.order = approved_order(captures=one_completed_capture())

    outcome, updated, observed = eps.reconcile_capture(ex)

    assert outcome == "captured"
    assert updated.status is ExternalOrderStatus.CAPTURED
    assert updated.paypal_capture_id == CAPTURE_ID
    assert updated.captured_amount == AMOUNT
    assert updated.captured_currency == CURRENCY


def test_capture_pending_becomes_captured_when_verified_completed(reads):
    ex = insert_execution(
        ExternalOrderStatus.CAPTURE_PENDING,
        capture_id=CAPTURE_ID,
        capture_status="PENDING",
    )
    reads.order = approved_order(captures=one_completed_capture())

    outcome, updated, _ = eps.reconcile_capture(ex)

    assert outcome == "captured"
    assert updated.status is ExternalOrderStatus.CAPTURED
    assert updated.paypal_capture_id == CAPTURE_ID


@pytest.mark.parametrize(
    "captures",
    [
        pytest.param(one_completed_capture(value="0.01"), id="amount-mismatch"),
        pytest.param(one_completed_capture(currency="EUR"), id="currency-mismatch"),
        pytest.param(
            [{"id": "C1", "status": "COMPLETED", "amount": {"currency_code": CURRENCY, "value": AMOUNT}},
             {"id": "C2", "status": "COMPLETED", "amount": {"currency_code": CURRENCY, "value": AMOUNT}}],
            id="multiple-captures",
        ),
        pytest.param([{"id": None, "status": "COMPLETED", "amount": {"currency_code": CURRENCY, "value": AMOUNT}}], id="no-capture-id"),
        pytest.param([{"id": CAPTURE_ID, "status": "PENDING", "amount": {"currency_code": CURRENCY, "value": AMOUNT}}], id="not-completed"),
    ],
)
def test_a_discrepant_capture_is_never_captured(reads, captures):
    ex = insert_execution(ExternalOrderStatus.CAPTURING)
    reads.order = approved_order(captures=captures)

    outcome, updated, detail = eps.reconcile_capture(ex)

    assert outcome == "review_required"
    assert updated.status is ExternalOrderStatus.CAPTURING  # never promoted
    assert detail["retryable"] is False


def test_an_order_id_mismatch_is_never_captured(reads):
    ex = insert_execution(ExternalOrderStatus.CAPTURING)
    reads.order = approved_order(order_id="SOMETHING-ELSE", captures=one_completed_capture())

    outcome, updated, detail = eps.reconcile_capture(ex)

    assert outcome == "review_required"
    assert detail["reason"] == "ORDER_ID_MISMATCH"
    assert updated.status is ExternalOrderStatus.CAPTURING


def test_a_capture_id_is_never_rewritten(reads):
    """A CAPTURE_PENDING row already holds a capture id; a DIFFERENT completed
    capture id must not overwrite it."""
    ex = insert_execution(
        ExternalOrderStatus.CAPTURE_PENDING,
        capture_id="ORIGINAL-CAP",
        capture_status="PENDING",
    )
    reads.order = approved_order(captures=one_completed_capture(capture_id="DIFFERENT-CAP"))

    outcome, updated, detail = eps.reconcile_capture(ex)

    assert outcome == "review_required"
    assert detail["reason"] == "CAPTURE_ID_CHANGED"
    assert updated.status is ExternalOrderStatus.CAPTURE_PENDING
    assert updated.paypal_capture_id == "ORIGINAL-CAP"


# ===========================================================================
# Scope B.5 — CAPTURING with no capture stays unresolved (no retry, no release)
# ===========================================================================


def test_capturing_with_no_capture_stays_unresolved(reads):
    ex = insert_execution(ExternalOrderStatus.CAPTURING)
    reads.order = approved_order(captures=[])  # APPROVED, but no capture visible

    outcome, updated, detail = eps.reconcile_capture(ex)

    assert outcome == "unresolved_no_capture"
    assert updated.status is ExternalOrderStatus.CAPTURING  # reservation kept
    assert detail["retryable"] is True


# ===========================================================================
# Scope B.6 — inconclusive evidence preserves state
# ===========================================================================


@pytest.mark.parametrize(
    "raiser",
    [
        paypal.PayPalGetOrderError("5xx"),
        paypal.PayPalAuthError("auth"),
        paypal.PayPalOrderNotFoundError("gone"),
    ],
)
def test_an_unreadable_order_preserves_state(reads, raiser):
    ex = insert_execution(ExternalOrderStatus.CAPTURING)
    reads.order_raises = raiser

    outcome, updated, detail = eps.reconcile_capture(ex)

    assert outcome == "inconclusive"
    assert updated.status is ExternalOrderStatus.CAPTURING
    assert detail["retryable"] is True


def test_a_malformed_order_is_not_a_capture(reads):
    ex = insert_execution(ExternalOrderStatus.CAPTURING)
    reads.order = {"order_id": ORDER_ID, "order_status": "APPROVED"}  # no captures key

    outcome, updated, _ = eps.reconcile_capture(ex)

    assert outcome == "unresolved_no_capture"  # no capture visible, stay put
    assert updated.status is ExternalOrderStatus.CAPTURING


# ===========================================================================
# Scope B.4/B.7 — CAPTURE_FAILED is NOT auto-promoted; CAPTURED never regresses
# ===========================================================================


def test_capture_failed_is_left_for_manual_review(reads):
    ex = insert_execution(ExternalOrderStatus.CAPTURE_FAILED, capture_status="UNCONFIRMED")
    outcome, updated, _ = eps.reconcile_capture(ex)
    assert outcome == "not_applicable"
    assert updated.status is ExternalOrderStatus.CAPTURE_FAILED
    assert reads.get_calls == []  # it never even read PayPal


def test_a_captured_execution_is_an_idempotent_no_op(reads):
    ex = insert_execution(
        ExternalOrderStatus.CAPTURED,
        capture_id=CAPTURE_ID,
        capture_status="COMPLETED",
        captured_amount=AMOUNT,
        captured_currency=CURRENCY,
    )
    outcome, updated, _ = eps.reconcile_capture(ex)
    assert outcome == "already_applied"
    assert updated.status is ExternalOrderStatus.CAPTURED
    assert updated.paypal_capture_id == CAPTURE_ID
    assert reads.get_calls == []  # CAPTURED is never re-read or regressed


# ===========================================================================
# Scope B — concurrency: compare-and-set yields exactly one transition
# ===========================================================================


def test_concurrent_reconciliation_captures_exactly_once(reads):
    ex = insert_execution(ExternalOrderStatus.CAPTURING)
    reads.order = approved_order(captures=one_completed_capture())

    outcomes: list[str] = []
    lock = threading.Lock()

    def worker():
        o, _, _ = eps.reconcile_capture(eps.get(ex.execution_id))
        with lock:
            outcomes.append(o)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("captured") == 1
    assert all(o in ("captured", "already_applied") for o in outcomes)
    final = eps.get(ex.execution_id)
    assert final.status is ExternalOrderStatus.CAPTURED
    assert final.paypal_capture_id == CAPTURE_ID


# ===========================================================================
# Scope C — webhook wiring (verified trigger only)
# ===========================================================================


def post_webhook(event):
    return client.post(WEBHOOK_URL, json=event, headers=GOOD_HEADERS)


def test_verified_completed_webhook_reconciles_external_to_captured(creds, monkeypatch, reads):
    mock_signature(monkeypatch)
    ex = insert_execution(ExternalOrderStatus.CAPTURING)
    reads.order = approved_order(captures=one_completed_capture())

    resp = post_webhook(capture_event("PAYMENT.CAPTURE.COMPLETED"))

    assert resp.status_code == 200
    body = resp.json()
    assert body["reconciliation"]["action"] == "transaction_not_found"
    assert body["external_reconciliation"]["action"] == "captured"
    assert status_of(ex.execution_id) is ExternalOrderStatus.CAPTURED


def test_verified_approved_webhook_reconciles_external_to_buyer_approved(creds, monkeypatch, reads):
    mock_signature(monkeypatch)
    ex = insert_execution(ExternalOrderStatus.ORDER_CREATED)
    reads.order = approved_order()

    resp = post_webhook(order_approved_event())

    assert resp.status_code == 200
    assert resp.json()["external_reconciliation"]["action"] == "approved"
    assert status_of(ex.execution_id) is ExternalOrderStatus.BUYER_APPROVED


def test_duplicate_completed_webhook_is_idempotent(creds, monkeypatch, reads):
    mock_signature(monkeypatch)
    ex = insert_execution(ExternalOrderStatus.CAPTURING)
    reads.order = approved_order(captures=one_completed_capture())
    event = capture_event("PAYMENT.CAPTURE.COMPLETED")

    first = post_webhook(event)
    second = post_webhook(event)

    assert first.json()["external_reconciliation"]["action"] == "captured"
    assert second.json()["external_reconciliation"]["action"] == "already_applied"
    assert status_of(ex.execution_id) is ExternalOrderStatus.CAPTURED


def test_out_of_order_completed_on_an_order_created_row_does_not_capture(creds, monkeypatch, reads):
    """A capture-completed webhook arriving while our record is only
    ORDER_CREATED must not promote straight to CAPTURED: that state is not
    reconcilable to CAPTURED, so nothing moves."""
    mock_signature(monkeypatch)
    ex = insert_execution(ExternalOrderStatus.ORDER_CREATED)
    reads.order = approved_order(captures=one_completed_capture())

    resp = post_webhook(capture_event("PAYMENT.CAPTURE.COMPLETED"))

    assert resp.json()["external_reconciliation"]["action"] == "not_applicable"
    assert status_of(ex.execution_id) is ExternalOrderStatus.ORDER_CREATED


def test_denied_webhook_never_promotes_and_reads_independently(creds, monkeypatch, reads):
    """A DENIED webhook is a trigger to re-check; with no completed capture
    at PayPal it leaves a CAPTURING row unresolved, never DECLINED/CAPTURED."""
    mock_signature(monkeypatch)
    ex = insert_execution(ExternalOrderStatus.CAPTURING)
    reads.order = approved_order(captures=[])

    resp = post_webhook(capture_event("PAYMENT.CAPTURE.DENIED"))

    assert resp.json()["external_reconciliation"]["action"] == "unresolved_no_capture"
    assert status_of(ex.execution_id) is ExternalOrderStatus.CAPTURING


def test_unknown_external_id_mutates_nothing(creds, monkeypatch, reads):
    mock_signature(monkeypatch)
    ex = insert_execution(ExternalOrderStatus.CAPTURING, order_id=ORDER_ID)

    resp = post_webhook(capture_event("PAYMENT.CAPTURE.COMPLETED", order_id="NOT-OURS", capture_id="NOPE"))

    assert resp.status_code == 200
    assert resp.json()["external_reconciliation"]["action"] == "external_execution_not_found"
    assert status_of(ex.execution_id) is ExternalOrderStatus.CAPTURING
    assert reads.get_calls == []  # never read PayPal for an id we do not own


def test_invalid_signature_never_invokes_external_reconciliation(creds, monkeypatch):
    mock_signature(monkeypatch, VERIFY_FAILURE)
    ex = insert_execution(ExternalOrderStatus.CAPTURING)

    called = {"n": 0}
    real = recon.reconcile_external_event

    def spy(event):
        called["n"] += 1
        return real(event)

    monkeypatch.setattr("app.main.reconcile_external_event", spy)

    resp = post_webhook(capture_event("PAYMENT.CAPTURE.COMPLETED"))

    assert resp.status_code == 400
    assert called["n"] == 0
    assert status_of(ex.execution_id) is ExternalOrderStatus.CAPTURING


def test_unsupported_event_type_changes_no_money_state(creds, monkeypatch, reads):
    mock_signature(monkeypatch)
    ex = insert_execution(ExternalOrderStatus.CAPTURING)

    event = {
        "id": "WH-UNSUP",
        "event_type": "PAYMENT.CAPTURE.REFUNDED",
        "resource": {"id": "REF-1", "supplementary_data": {"related_ids": {"order_id": ORDER_ID, "capture_id": CAPTURE_ID}}},
    }
    resp = post_webhook(event)

    assert resp.status_code == 200
    # Catalog does track REFUNDED but has no row -> transaction_not_found;
    # external does NOT act on REFUNDED -> ignored_event_type. Nothing moves.
    assert resp.json()["external_reconciliation"]["action"] == "ignored_event_type"
    assert status_of(ex.execution_id) is ExternalOrderStatus.CAPTURING


def _insert_catalog_transaction(order_id):
    """Seed a catalog `transactions` row directly (bypassing the trapped
    create_transaction), so a catalog-owned event can be exercised."""
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(transaction_service._db_path()) as conn:
        conn.execute(transaction_service._SCHEMA)
        conn.execute(
            """INSERT INTO transactions (
                   transaction_id, product_id, product_name, catalog_price_inr,
                   paypal_amount, paypal_currency, paypal_order_id,
                   paypal_capture_id, status, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (f"txn_{uuid.uuid4().hex}", "p1", "Demo", 1000, "12.00", "USD",
             order_id, None, "ORDER_CREATED", now, now),
        )
        conn.commit()


def test_a_catalog_event_never_enters_external_reconciliation(creds, monkeypatch, reads):
    """When a catalog transaction owns the order, external reconciliation is
    not even invoked (external_reconciliation is null) and its get_order is
    never called — separation holds."""
    mock_signature(monkeypatch)
    # Let the real catalog writer run for THIS catalog row only.
    monkeypatch.setattr(transaction_service, "mark_captured", REAL_MARK_CAPTURED)
    _insert_catalog_transaction("CATALOG-ORDER-1")

    resp = post_webhook(capture_event("PAYMENT.CAPTURE.COMPLETED", order_id="CATALOG-ORDER-1"))

    assert resp.status_code == 200
    body = resp.json()
    assert body["reconciliation"]["action"] == "updated"
    assert body["external_reconciliation"] is None
    assert reads.get_calls == []  # external get_order never touched


# ===========================================================================
# Scope money-safety — the traps prove no capture/refund path was touched
# ===========================================================================


def test_no_reconciliation_path_calls_capture_or_refund(money_is_untouchable, reads):
    """Run every reconcilable transition; the autouse trap would raise if any
    of them reached capture_order or refund_capture."""
    # CAPTURED promotion
    ex1 = insert_execution(ExternalOrderStatus.CAPTURING)
    reads.order = approved_order(captures=one_completed_capture())
    assert eps.reconcile_capture(ex1)[0] == "captured"
    # BUYER_APPROVED promotion
    ex2 = insert_execution(ExternalOrderStatus.ORDER_CREATED, order_id="O2")
    reads.order = approved_order(order_id="O2")
    assert eps.reconcile_buyer_approval(ex2)[0] == "approved"

    assert money_is_untouchable == []  # no trapped call fired
