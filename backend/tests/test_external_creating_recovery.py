"""Stale-CREATING visibility and read-first recovery (Phase 11F-K.17).

WHAT THIS PHASE ADDED
    Since Phase 11F-K.16 the PayPal order id is committed to the row the
    instant create_order returns one, so a crash during the final settlement
    can leave a CREATING reservation that STILL NAMES its order. This phase:

      A. surfaces STALE CREATING rows (older than a grace period) in the
         operator triage list, distinguishing those with a persisted order id
         (has_paypal_order_id) from those without; and
      B. lets the operator reconcile endpoint recover a CREATING row WITH an
         order id to ORDER_CREATED, read-first, on conclusive independent
         evidence -- and return manual_review_required when there is no id.

THE PROPERTIES DEFENDED HERE
    1-3. Stale CREATING (with or without an id) is visible; a recent CREATING
         is not; a no-id row reconciles to manual_review_required.
    4.   A consistent PayPal order read settles CREATING -> ORDER_CREATED only
         (never BUYER_APPROVED, never CAPTURED).
    5-6. An amount/currency/identity/status mismatch, or an unreadable order,
         leaves the record unchanged.
    7-8. Reconciliation is idempotent and a compare-and-set cannot overwrite a
         changed/terminal state.
    9.   Explicit traps prove NO create/capture/approve/authorize/refund call
         occurs on any recovery path.

PayPal HTTP is fully mocked. The orphan under test is built through the real
11E/11F-K.16 flow (create the order, then fail the final settlement), so the
bound revalidation snapshot that derives the expected amount really exists.
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

from test_external_purchase_order import (  # noqa: F401 - pytest fixtures
    adopted,
    approved_approval,
    clock,
    no_ai,
    no_money_moves,
    order_calls,
    paypal_http,
    require_approval,
    snapshot,
)

client = TestClient(app, raise_server_exceptions=False)

LIST_URL = "/discovery/external-orders/unresolved"
ORDER_ID = "EXTORDER123"       # ORDER_RESPONSE["id"] from the 11E fake
AMOUNT = "19.99"               # FRESH_OBSERVED from the 11E snapshot
CURRENCY = "USD"


def _boom(*args, **kwargs):
    raise RuntimeError("final settlement crashed")


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def seed_approval(adopted_product):
    require_approval()
    snapshot(adopted_product.adopted_product_id)
    return approved_approval(adopted_product.adopted_product_id)


def make_creating_orphan_with_id(adopted_product, monkeypatch) -> str:
    """A CREATING reservation that carries its persisted order id.

    Built by running the real create flow and failing the final settlement,
    which is exactly the K.16 crash window. Returns the approval id.
    """
    approval = seed_approval(adopted_product)
    with monkeypatch.context() as m:
        m.setattr(eps, "_settle", _boom)
        with pytest.raises(RuntimeError):
            eps.create_order_for_approval(approval.approval_id)
    row = eps.get_by_approval(approval.approval_id)
    assert row.status is ExternalOrderStatus.CREATING
    assert row.paypal_order_id == ORDER_ID
    return approval.approval_id


def insert_creating(created_at, *, order_id=None, approval_id=None) -> str:
    """Insert a raw CREATING row with a chosen created_at. Returns approval id."""
    approval_id = approval_id or f"xapr_{uuid.uuid4().hex}"
    execution_id = f"xord_{uuid.uuid4().hex}"
    stamp = created_at if isinstance(created_at, str) else created_at.isoformat()
    with sqlite3.connect(eps._db_path()) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(eps._SCHEMA)
        eps._migrate(conn)
        conn.execute(
            f"""INSERT INTO external_purchase_orders ({", ".join(eps._COLUMNS)})
                VALUES ({", ".join("?" * len(eps._COLUMNS))})""",
            (
                execution_id, approval_id, "adopt_x", "reval_x", order_id,
                None, None, None, None, None, None, None, None,
                ExternalOrderStatus.CREATING.value, stamp, stamp,
            ),
        )
        conn.commit()
    return approval_id


def fake_order(order_id=ORDER_ID, status="CREATED", value=AMOUNT, currency=CURRENCY):
    return {
        "order_id": order_id,
        "order_status": status,
        "intent": "CAPTURE",
        "purchase_units": [{"amount": {"currency_code": currency, "value": value}}],
        "captures": [],
        "approve_url": "https://www.sandbox.paypal.com/checkoutnow?token=EXTORDER123",
    }


def reconcile(approval_id):
    return client.post(f"/discovery/approvals/{approval_id}/reconcile")


def arm_recovery_traps(monkeypatch) -> list[str]:
    """Trap every money-moving / order-creating method. Returns a hit list."""
    hits: list[str] = []

    def bomb(name):
        def _b(*a, **k):
            hits.append(name)
            raise AssertionError(f"recovery must never call {name}")

        return _b

    monkeypatch.setattr(eps, "create_order", bomb("eps.create_order"))
    monkeypatch.setattr(eps, "capture_order", bomb("eps.capture_order"))
    monkeypatch.setattr(eps, "authorize_capture", bomb("eps.authorize_capture"))
    monkeypatch.setattr(paypal, "capture_order", bomb("paypal.capture_order"))
    monkeypatch.setattr(paypal, "refund_capture", bomb("paypal.refund_capture"))
    monkeypatch.setattr(refund_service, "refund_capture", bomb("refund_capture"))
    monkeypatch.setattr(
        transaction_service, "create_transaction", bomb("catalog ledger")
    )
    monkeypatch.setattr(transaction_service, "mark_captured", bomb("catalog ledger"))
    return hits


# ===========================================================================
# A. Stale-CREATING visibility
# ===========================================================================


def test_stale_creating_with_order_id_appears_in_triage():
    old = datetime.now(timezone.utc) - timedelta(seconds=3600)
    approval_id = insert_creating(old, order_id=ORDER_ID)

    resp = client.get(LIST_URL)
    assert resp.status_code == 200
    rows = resp.json()["executions"]
    mine = [e for e in rows if e["approval_id"] == approval_id]
    assert len(mine) == 1
    assert mine[0]["status"] == "CREATING"
    assert mine[0]["has_paypal_order_id"] is True
    # Triage never leaks the checkout URL.
    assert "approve_url" not in mine[0]


def test_recent_creating_is_not_flagged_as_stale():
    recent = datetime.now(timezone.utc) - timedelta(seconds=5)
    approval_id = insert_creating(recent, order_id=ORDER_ID)

    resp = client.get(LIST_URL)
    assert resp.status_code == 200
    assert all(e["approval_id"] != approval_id for e in resp.json()["executions"])


def test_stale_creating_without_order_id_is_visible_and_distinguished():
    old = datetime.now(timezone.utc) - timedelta(seconds=3600)
    approval_id = insert_creating(old, order_id=None)

    rows = client.get(LIST_URL).json()["executions"]
    mine = [e for e in rows if e["approval_id"] == approval_id]
    assert len(mine) == 1
    assert mine[0]["has_paypal_order_id"] is False
    assert mine[0]["paypal_order_id"] is None


def test_creating_filter_returns_only_stale_creating():
    old = datetime.now(timezone.utc) - timedelta(seconds=3600)
    stale = insert_creating(old, order_id=ORDER_ID)
    recent = insert_creating(
        datetime.now(timezone.utc) - timedelta(seconds=5), order_id="EXTORDER-RECENT"
    )

    rows = client.get(LIST_URL, params={"status": ["CREATING"]}).json()["executions"]
    returned = {e["approval_id"] for e in rows}
    assert stale in returned
    assert recent not in returned
    assert all(e["status"] == "CREATING" for e in rows)


def test_list_still_rejects_a_non_listable_status_filter():
    assert client.get(LIST_URL, params={"status": ["CAPTURED"]}).status_code == 422


# ===========================================================================
# C. Reconcile a CREATING record WITHOUT a persisted order id
# ===========================================================================


def test_no_id_creating_reconciles_to_manual_review_without_paypal(monkeypatch):
    approval_id = insert_creating(
        datetime.now(timezone.utc) - timedelta(seconds=3600), order_id=None
    )
    # Trap PayPal entirely: a no-id row must never trigger a lookup.
    monkeypatch.setattr(
        eps, "get_order", lambda *a, **k: pytest.fail("no-id must not read PayPal")
    )
    hits = arm_recovery_traps(monkeypatch)

    body = reconcile(approval_id).json()
    assert body["outcome"] == "manual_review_required"
    assert body["review_required"] is True
    assert body["reconciled"] is False
    assert body["status"] == "CREATING"
    assert eps.get_by_approval(approval_id).status is ExternalOrderStatus.CREATING
    assert hits == []


# ===========================================================================
# B. Reconcile a CREATING record WITH a persisted order id
# ===========================================================================


def test_consistent_order_recovers_to_order_created_only(
    adopted, paypal_http, monkeypatch
):
    approval_id = make_creating_orphan_with_id(adopted, monkeypatch)

    calls: list[str] = []

    def fake_get_order(order_id):
        calls.append(order_id)
        return fake_order(status="CREATED")

    monkeypatch.setattr(eps, "get_order", fake_get_order)
    hits = arm_recovery_traps(monkeypatch)

    body = reconcile(approval_id).json()

    assert body["outcome"] == "recovered_order_created"
    assert body["reconciled"] is False     # not a capture; no money asserted
    assert body["captured"] is False
    assert body["status"] == "ORDER_CREATED"

    stored = eps.get_by_approval(approval_id)
    assert stored.status is ExternalOrderStatus.ORDER_CREATED
    assert stored.paypal_order_id == ORDER_ID
    assert stored.paypal_amount == AMOUNT      # derived from the bound snapshot
    assert stored.paypal_currency == CURRENCY

    assert calls == [ORDER_ID]             # read once, by the stored id
    assert len(order_calls(paypal_http)) == 1  # the one real create only
    assert hits == []


def test_approved_order_recovers_to_order_created_not_buyer_approved(
    adopted, paypal_http, monkeypatch
):
    """An order the buyer already APPROVED still maps only to ORDER_CREATED:
    buyer-approval is never inferred here, and nothing is captured."""
    approval_id = make_creating_orphan_with_id(adopted, monkeypatch)
    monkeypatch.setattr(eps, "get_order", lambda oid: fake_order(status="APPROVED"))
    hits = arm_recovery_traps(monkeypatch)

    body = reconcile(approval_id).json()
    assert body["outcome"] == "recovered_order_created"
    assert body["status"] == "ORDER_CREATED"
    assert eps.get_by_approval(approval_id).status is ExternalOrderStatus.ORDER_CREATED
    assert hits == []


@pytest.mark.parametrize(
    "order,reason",
    [
        (fake_order(value="0.01"), "amount mismatch"),
        (fake_order(currency="EUR"), "currency mismatch"),
        (fake_order(status="COMPLETED"), "captured status"),
        (fake_order(status="VOIDED"), "voided status"),
        (fake_order(order_id="OTHER-ORDER"), "order id mismatch"),
    ],
)
def test_inconsistent_order_leaves_record_unchanged(
    adopted, paypal_http, monkeypatch, order, reason
):
    approval_id = make_creating_orphan_with_id(adopted, monkeypatch)
    monkeypatch.setattr(eps, "get_order", lambda oid: order)
    hits = arm_recovery_traps(monkeypatch)

    body = reconcile(approval_id).json()
    assert body["outcome"] == "review_required", reason
    assert body["reconciled"] is False
    assert body["captured"] is False
    # Unchanged: still CREATING, still holding only the persisted id.
    stored = eps.get_by_approval(approval_id)
    assert stored.status is ExternalOrderStatus.CREATING
    assert stored.paypal_order_id == ORDER_ID
    assert stored.paypal_amount is None
    assert hits == []


def test_paypal_read_failure_leaves_record_unchanged(
    adopted, paypal_http, monkeypatch
):
    approval_id = make_creating_orphan_with_id(adopted, monkeypatch)

    def boom_read(order_id):
        raise paypal.PayPalGetOrderError("HTTP 500 from PayPal")

    monkeypatch.setattr(eps, "get_order", boom_read)
    hits = arm_recovery_traps(monkeypatch)

    body = reconcile(approval_id).json()
    assert body["outcome"] == "inconclusive"
    assert body["retryable"] is True
    assert eps.get_by_approval(approval_id).status is ExternalOrderStatus.CREATING
    assert hits == []


# ===========================================================================
# D. Idempotency and compare-and-set safety
# ===========================================================================


def test_recovery_is_idempotent(adopted, paypal_http, monkeypatch):
    approval_id = make_creating_orphan_with_id(adopted, monkeypatch)
    calls: list[str] = []

    def fake_get_order(order_id):
        calls.append(order_id)
        return fake_order(status="CREATED")

    monkeypatch.setattr(eps, "get_order", fake_get_order)
    arm_recovery_traps(monkeypatch)

    first = reconcile(approval_id).json()
    second = reconcile(approval_id).json()

    assert first["outcome"] == "recovered_order_created"
    # The second call sees ORDER_CREATED (no longer CREATING): no state change,
    # and no further PayPal read (the recovered order needs no re-lookup).
    assert second["status"] == "ORDER_CREATED"
    assert second["reconciled"] is False
    assert eps.get_by_approval(approval_id).status is ExternalOrderStatus.ORDER_CREATED
    assert calls == [ORDER_ID]  # read exactly once, on the first reconcile


def test_concurrent_recovery_settles_exactly_once(adopted, paypal_http, monkeypatch):
    approval_id = make_creating_orphan_with_id(adopted, monkeypatch)
    monkeypatch.setattr(eps, "get_order", lambda oid: fake_order(status="CREATED"))
    arm_recovery_traps(monkeypatch)

    outcomes: list[str] = []
    lock = threading.Lock()

    def worker():
        o, _, _ = eps.reconcile_creating_order(eps.get_by_approval(approval_id))
        with lock:
            outcomes.append(o)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive()

    assert outcomes.count("recovered_order_created") == 1
    assert all(o in ("recovered_order_created", "already_applied") for o in outcomes)
    final = eps.get_by_approval(approval_id)
    assert final.status is ExternalOrderStatus.ORDER_CREATED
    assert final.paypal_order_id == ORDER_ID


def test_cas_cannot_overwrite_a_changed_terminal_state(
    adopted, paypal_http, monkeypatch
):
    """If the row moves OFF CREATING between the read and the write, the
    compare-and-set matches nothing and never forces it back."""
    approval_id = make_creating_orphan_with_id(adopted, monkeypatch)
    execution = eps.get_by_approval(approval_id)

    real_settle_cas = eps._settle_recovered_order_created

    def racing_get_order(order_id):
        # Simulate a concurrent writer moving the row to a terminal/other state
        # AFTER this reconcile read it but BEFORE its compare-and-set runs.
        with sqlite3.connect(eps._db_path()) as conn:
            conn.execute(
                "UPDATE external_purchase_orders SET status = ? "
                "WHERE approval_id = ?",
                (ExternalOrderStatus.FAILED.value, approval_id),
            )
            conn.commit()
        return fake_order(status="CREATED")

    monkeypatch.setattr(eps, "get_order", racing_get_order)
    hits = arm_recovery_traps(monkeypatch)

    outcome, updated, _ = eps.reconcile_creating_order(execution)

    assert outcome == "review_required"
    # The concurrently-written state is preserved; recovery did not force it.
    assert eps.get_by_approval(approval_id).status is ExternalOrderStatus.FAILED
    assert hits == []
    assert real_settle_cas is eps._settle_recovered_order_created  # untouched
