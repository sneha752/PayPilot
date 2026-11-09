"""Operator recovery & unresolved-payment monitoring (Phase 11F-I).

Two authenticated, operator-only endpoints:
  GET  /discovery/external-orders/unresolved   (read-only triage list)
  POST /discovery/approvals/{approval_id}/reconcile  (read-first recovery)

Reconciliation delegates to the existing reconcile_capture primitive: it
re-reads PayPal by the SERVER-STORED order id and promotes
CAPTURING/CAPTURE_PENDING to CAPTURED only on conclusive, independently
verified evidence, via compare-and-set. It never calls capture, refund,
create-order or capture-authorization — a trap over all of them proves it.

Everything PayPal is mocked; no live call or money movement occurs. The
conftest token auto-injection authenticates ordinary requests; the 401 tests
opt out with `no_auto_auth` and drive the header themselves.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from app import paypal
from app.main import app
from app.schemas.external_purchase import ExternalOrderStatus
from app.services import (
    external_purchase_service as eps,
    refund_service,
    transaction_service,
)

client = TestClient(app, raise_server_exceptions=False)

LIST_URL = "/discovery/external-orders/unresolved"
ORDER_ID = "EXTORDER-REC-1"
CAPTURE_ID = "EXTCAP-REC-1"
AMOUNT = "19.99"
CURRENCY = "USD"


# ---------------------------------------------------------------------------
# Money-safety trap: NO recovery path may capture/refund/create/authorize.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def money_is_untouchable(monkeypatch):
    hits: list[str] = []

    def bomb(name):
        def _bomb(*a, **k):
            hits.append(name)
            raise AssertionError(f"recovery must never call {name}")

        return _bomb

    monkeypatch.setattr(eps, "capture_order", bomb("capture_order"))
    monkeypatch.setattr(eps, "create_order", bomb("create_order"))
    monkeypatch.setattr(paypal, "capture_order", bomb("paypal.capture_order"))
    monkeypatch.setattr(paypal, "refund_capture", bomb("paypal.refund_capture"))
    monkeypatch.setattr(refund_service, "refund_capture", bomb("refund_capture"))
    monkeypatch.setattr(transaction_service, "create_transaction", bomb("catalog ledger"))
    monkeypatch.setattr(transaction_service, "mark_captured", bomb("catalog ledger"))
    return hits


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
# Builders / seeding
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


def insert_execution(
    status,
    *,
    approval_id=None,
    order_id=ORDER_ID,
    amount=AMOUNT,
    currency=CURRENCY,
    capture_id=None,
    capture_status=None,
):
    approval_id = approval_id or f"appr_{uuid.uuid4().hex}"
    execution_id = f"xord_{uuid.uuid4().hex}"
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(eps._db_path()) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(eps._SCHEMA)
        eps._migrate(conn)
        conn.execute(
            f"""INSERT INTO external_purchase_orders ({", ".join(eps._COLUMNS)})
                VALUES ({", ".join("?" * len(eps._COLUMNS))})""",
            (
                execution_id, approval_id, "adopt_x", "reval_x", order_id,
                amount, currency, None, None, capture_id, capture_status,
                None, None,
                status.value if isinstance(status, ExternalOrderStatus) else status,
                now, now,
            ),
        )
        conn.commit()
    return eps.get(execution_id)


def reconcile(approval_id, **kwargs):
    return client.post(f"/discovery/approvals/{approval_id}/reconcile", **kwargs)


def status_of(execution_id):
    return eps.get(execution_id).status


# ===========================================================================
# 1 — authentication
# ===========================================================================


def test_list_requires_a_token(no_auto_auth):
    assert client.get(LIST_URL).status_code == 401


def test_reconcile_requires_a_token(no_auto_auth):
    assert reconcile("appr_x").status_code == 401


def test_reconcile_with_invalid_token_is_401_and_runs_nothing(no_auto_auth, reads):
    ex = insert_execution(ExternalOrderStatus.CAPTURING)
    resp = reconcile(ex.approval_id, headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401
    assert reads.get_calls == []  # never reached the service
    assert status_of(ex.execution_id) is ExternalOrderStatus.CAPTURING


# ===========================================================================
# 2 & 3 — listing and filter/limit validation
# ===========================================================================


def test_list_returns_unresolved_statuses(reads):
    cap = insert_execution(ExternalOrderStatus.CAPTURING, order_id="O-CAP")
    pend = insert_execution(ExternalOrderStatus.CAPTURE_PENDING, order_id="O-PEND")
    fail = insert_execution(ExternalOrderStatus.CAPTURE_FAILED, order_id="O-FAIL")
    # Not unresolved -> must be excluded:
    insert_execution(ExternalOrderStatus.CAPTURED, order_id="O-DONE")
    insert_execution(ExternalOrderStatus.ORDER_CREATED, order_id="O-NEW")

    resp = client.get(LIST_URL)
    assert resp.status_code == 200
    body = resp.json()
    returned = {e["execution_id"] for e in body["executions"]}
    assert returned == {cap.execution_id, pend.execution_id, fail.execution_id}
    assert body["count"] == 3
    # Triage projection must not leak the checkout URL.
    for e in body["executions"]:
        assert "approve_url" not in e


def test_list_filter_narrows_to_one_status(reads):
    cap = insert_execution(ExternalOrderStatus.CAPTURING)
    insert_execution(ExternalOrderStatus.CAPTURE_FAILED, order_id="O-F")

    resp = client.get(LIST_URL, params={"status": ["CAPTURING"]})
    assert resp.status_code == 200
    rows = resp.json()["executions"]
    assert [e["execution_id"] for e in rows] == [cap.execution_id]


def test_list_rejects_a_non_unresolved_status_filter(reads):
    resp = client.get(LIST_URL, params={"status": ["CAPTURED"]})
    assert resp.status_code == 422


def test_list_rejects_an_unknown_status_filter(reads):
    resp = client.get(LIST_URL, params={"status": ["NONSENSE"]})
    assert resp.status_code == 422


@pytest.mark.parametrize("limit", [0, 101, -5])
def test_list_rejects_out_of_range_limit(reads, limit):
    assert client.get(LIST_URL, params={"limit": limit}).status_code == 422


def test_list_respects_a_valid_limit(reads):
    for i in range(5):
        insert_execution(ExternalOrderStatus.CAPTURING, order_id=f"O-{i}")
    resp = client.get(LIST_URL, params={"limit": 2})
    assert resp.status_code == 200
    assert resp.json()["count"] == 2


def test_list_never_returns_catalog_rows(reads):
    _insert_catalog_transaction("CATALOG-ORDER-1")
    cap = insert_execution(ExternalOrderStatus.CAPTURING)
    resp = client.get(LIST_URL)
    rows = resp.json()["executions"]
    assert [e["execution_id"] for e in rows] == [cap.execution_id]
    assert all(e["execution_id"].startswith("xord_") for e in rows)


# ===========================================================================
# 4-8 — reconcile state behavior
# ===========================================================================


def test_capturing_with_matching_capture_is_reconciled_to_captured(reads):
    ex = insert_execution(ExternalOrderStatus.CAPTURING)
    reads.order = approved_order(captures=one_completed_capture())

    resp = reconcile(ex.approval_id)
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "reconciled_to_captured"
    assert body["reconciled"] is True
    assert body["captured"] is True
    assert body["status"] == "CAPTURED"
    assert status_of(ex.execution_id) is ExternalOrderStatus.CAPTURED


def test_capturing_with_no_capture_stays_unresolved(reads):
    ex = insert_execution(ExternalOrderStatus.CAPTURING)
    reads.order = approved_order(captures=[])

    body = reconcile(ex.approval_id).json()
    assert body["outcome"] == "unresolved_no_capture"
    assert body["reconciled"] is False
    assert body["retryable"] is True
    assert status_of(ex.execution_id) is ExternalOrderStatus.CAPTURING


def test_capture_pending_confirmed_completed_is_captured(reads):
    ex = insert_execution(
        ExternalOrderStatus.CAPTURE_PENDING, capture_id=CAPTURE_ID, capture_status="PENDING"
    )
    reads.order = approved_order(captures=one_completed_capture())

    body = reconcile(ex.approval_id).json()
    assert body["outcome"] == "reconciled_to_captured"
    assert status_of(ex.execution_id) is ExternalOrderStatus.CAPTURED


def test_capture_failed_is_manual_review_and_makes_no_paypal_call(reads):
    ex = insert_execution(ExternalOrderStatus.CAPTURE_FAILED, capture_status="UNCONFIRMED")

    body = reconcile(ex.approval_id).json()
    assert body["outcome"] == "manual_review_required"
    assert body["review_required"] is True
    assert body["reconciled"] is False
    assert status_of(ex.execution_id) is ExternalOrderStatus.CAPTURE_FAILED
    assert reads.get_calls == []  # never looked PayPal up


@pytest.mark.parametrize(
    "setup",
    [
        pytest.param(("raise", paypal.PayPalGetOrderError("5xx")), id="timeout-5xx"),
        pytest.param(("order", {"order_id": ORDER_ID, "order_status": "APPROVED"}), id="malformed-no-captures"),
        pytest.param(("order", None), id="multiple-captures"),  # replaced below
        pytest.param(("captures", one_completed_capture(value="0.01")), id="amount-mismatch"),
        pytest.param(("captures", one_completed_capture(currency="EUR")), id="currency-mismatch"),
        pytest.param(("captures", one_completed_capture(capture_id="DIFFERENT")), id="changed-capture-id"),
    ],
)
def test_inconclusive_or_discrepant_evidence_never_captures(reads, setup):
    kind, value = setup
    # CAPTURE_PENDING carries a stored capture id, so the changed-id case has
    # something to conflict with.
    ex = insert_execution(
        ExternalOrderStatus.CAPTURE_PENDING, capture_id=CAPTURE_ID, capture_status="PENDING"
    )
    if kind == "raise":
        reads.order_raises = value
    elif kind == "captures":
        reads.order = approved_order(captures=value)
    elif kind == "order":
        if value is None:
            reads.order = approved_order(
                captures=[
                    {"id": "C1", "status": "COMPLETED", "amount": {"currency_code": CURRENCY, "value": AMOUNT}},
                    {"id": "C2", "status": "COMPLETED", "amount": {"currency_code": CURRENCY, "value": AMOUNT}},
                ]
            )
        else:
            reads.order = value

    body = reconcile(ex.approval_id).json()
    # The invariant that matters: inconclusive/discrepant/not-yet-visible
    # evidence NEVER promotes to CAPTURED, whatever the specific outcome.
    assert body["outcome"] in ("inconclusive", "review_required", "unresolved_no_capture")
    assert body["reconciled"] is False
    assert body["captured"] is False
    assert status_of(ex.execution_id) is ExternalOrderStatus.CAPTURE_PENDING


# ===========================================================================
# 9 & 10 — unknown approval, idempotency
# ===========================================================================


def test_unknown_approval_is_404(reads):
    assert reconcile("appr_does_not_exist").status_code == 404
    assert reads.get_calls == []


def test_reconciliation_is_idempotent_on_a_captured_execution(reads):
    ex = insert_execution(
        ExternalOrderStatus.CAPTURED, capture_id=CAPTURE_ID, capture_status="COMPLETED"
    )
    body = reconcile(ex.approval_id).json()
    assert body["outcome"] == "already_captured"
    assert body["reconciled"] is False
    assert body["captured"] is True
    assert reads.get_calls == []  # CAPTURED is never re-read


def test_reconcile_twice_captures_once_then_replays(reads):
    ex = insert_execution(ExternalOrderStatus.CAPTURING)
    reads.order = approved_order(captures=one_completed_capture())

    first = reconcile(ex.approval_id).json()
    second = reconcile(ex.approval_id).json()
    assert first["outcome"] == "reconciled_to_captured"
    assert second["outcome"] == "already_captured"
    assert status_of(ex.execution_id) is ExternalOrderStatus.CAPTURED


# ===========================================================================
# 11 — concurrency (compare-and-set): exactly one promotion
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
# 12 — catalog executions are untouchable through this endpoint
# ===========================================================================


def _insert_catalog_transaction(order_id, approval_like_id=None):
    now = datetime.now(timezone.utc).isoformat()
    txn_id = approval_like_id or f"txn_{uuid.uuid4().hex}"
    with sqlite3.connect(transaction_service._db_path()) as conn:
        conn.execute(transaction_service._SCHEMA)
        conn.execute(
            """INSERT INTO transactions (
                   transaction_id, product_id, product_name, catalog_price_inr,
                   paypal_amount, paypal_currency, paypal_order_id,
                   paypal_capture_id, status, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (txn_id, "p1", "Demo", 1000, "12.00", "USD", order_id, None,
             "ORDER_CREATED", now, now),
        )
        conn.commit()
    return txn_id


def test_catalog_transaction_cannot_be_reconciled_here(reads):
    # A catalog transaction id is not an external approval id -> 404, and the
    # catalog ledger is untouched.
    txn_id = _insert_catalog_transaction("CATALOG-ORDER-9", approval_like_id="txn_catalog")
    resp = reconcile("txn_catalog")
    assert resp.status_code == 404
    assert reads.get_calls == []
    row = transaction_service.get_transaction(txn_id)
    assert row.status.value == "ORDER_CREATED"  # unchanged


# ===========================================================================
# 13 — money-safety trap proves nothing money-moving ran
# ===========================================================================


def test_full_reconcile_path_invokes_no_money_moving_method(money_is_untouchable, reads):
    ex = insert_execution(ExternalOrderStatus.CAPTURING)
    reads.order = approved_order(captures=one_completed_capture())
    assert reconcile(ex.approval_id).json()["outcome"] == "reconciled_to_captured"
    assert money_is_untouchable == []
