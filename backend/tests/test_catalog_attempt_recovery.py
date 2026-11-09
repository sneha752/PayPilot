"""Catalog orphan visibility & read-first recovery (Phase 11F-K.21).

Covers the three recovery surfaces added for stuck catalog purchase attempts:
  A. GET  /purchase/attempts/unresolved       - stale-CREATING triage list
  B. POST /purchase/attempts/{id}/reconcile   - operator read-first recovery
  D. reconcile_catalog_attempt_event(...)     - webhook fallback

Recovery settles a CREATING attempt to FINALIZED ONLY on conclusive,
independently verified evidence (an independent get_order), reusing the
idempotent K.20 finalize linking. It never creates, approves, authorizes,
captures, or refunds — explicit traps prove it. PayPal is mocked; local SQLite
only.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from app import paypal
from app.main import app
from app.schemas.catalog_attempt import CatalogAttemptStatus
from app.services import (
    catalog_attempt_service as cas,
    purchase_execution_service as pes,
    refund_service,
    transaction_service,
)
from app.services.catalog_reconciliation_service import reconcile_catalog_attempt_event
from app.services.currency_service import convert_inr_to_usd

client = TestClient(app, raise_server_exceptions=False)

PRODUCT_ID = "HDP006"  # boAt Rockerz 450, 1499 INR
PRICE_INR = 1499
AMOUNT = convert_inr_to_usd(PRICE_INR)  # "16.66"
CURRENCY = "USD"
ORDER_ID = "CATORDER-1"

LIST_URL = "/purchase/attempts/unresolved"


# ---------------------------------------------------------------------------
# Builders / fixtures
# ---------------------------------------------------------------------------


def seed_creating_with_id(order_id=ORDER_ID, product_id=PRODUCT_ID, amount=AMOUNT):
    """A real CREATING attempt carrying a persisted order id (via the service)."""
    attempt = cas.reserve_attempt(product_id, PRICE_INR, amount, CURRENCY)
    claimed, rec = cas.claim_order_id(attempt.attempt_id, order_id)
    assert claimed and rec.paypal_order_id == order_id
    return rec


def insert_attempt(created_at, *, status=CatalogAttemptStatus.CREATING, order_id=None):
    """Raw-insert an attempt with a chosen created_at (for staleness tests)."""
    attempt_id = f"cpa_{uuid.uuid4().hex}"
    stamp = created_at if isinstance(created_at, str) else created_at.isoformat()
    with sqlite3.connect(cas._db_path()) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(cas._SCHEMA)
        conn.execute(
            f"""INSERT INTO catalog_purchase_attempts ({", ".join(cas._COLUMNS)})
                VALUES ({", ".join("?" * len(cas._COLUMNS))})""",
            (
                attempt_id, PRODUCT_ID, PRICE_INR, AMOUNT, CURRENCY, order_id,
                f"cpa_claim_{uuid.uuid4().hex}",
                status.value if isinstance(status, CatalogAttemptStatus) else status,
                None, stamp, stamp,
            ),
        )
        conn.commit()
    return attempt_id


def fake_order(order_id=ORDER_ID, status="CREATED", value=AMOUNT, currency=CURRENCY):
    return {
        "order_id": order_id,
        "order_status": status,
        "intent": "CAPTURE",
        "purchase_units": [{"amount": {"currency_code": currency, "value": value}}],
        "approve_url": None,
    }


@pytest.fixture
def no_money(monkeypatch):
    """Trap every create / capture / authorize / refund path. Returns hits."""
    hits: list[str] = []

    def bomb(name):
        def _b(*a, **k):
            hits.append(name)
            raise AssertionError(f"recovery must not call {name}")

        return _b

    monkeypatch.setattr(pes, "create_order", bomb("pes.create_order"))
    monkeypatch.setattr(pes, "capture_order", bomb("pes.capture_order"))
    monkeypatch.setattr(paypal, "create_order", bomb("paypal.create_order"))
    monkeypatch.setattr(paypal, "capture_order", bomb("paypal.capture_order"))
    monkeypatch.setattr(paypal, "refund_capture", bomb("paypal.refund_capture"))
    monkeypatch.setattr(refund_service, "refund_capture", bomb("refund_capture"))
    return hits


def reconcile(attempt_id):
    return client.post(f"/purchase/attempts/{attempt_id}/reconcile")


# ===========================================================================
# A. Stale-CREATING triage
# ===========================================================================


def test_stale_creating_with_id_appears(no_money):
    old = datetime.now(timezone.utc) - timedelta(seconds=3600)
    aid = insert_attempt(old, order_id=ORDER_ID)

    resp = client.get(LIST_URL)
    assert resp.status_code == 200
    rows = resp.json()["attempts"]
    mine = [a for a in rows if a["attempt_id"] == aid]
    assert len(mine) == 1
    assert mine[0]["status"] == "CREATING"
    assert mine[0]["has_paypal_order_id"] is True
    assert "approve_url" not in mine[0]  # no checkout URL in triage


def test_stale_creating_without_id_appears_distinguished(no_money):
    old = datetime.now(timezone.utc) - timedelta(seconds=3600)
    aid = insert_attempt(old, order_id=None)
    rows = client.get(LIST_URL).json()["attempts"]
    mine = [a for a in rows if a["attempt_id"] == aid]
    assert len(mine) == 1 and mine[0]["has_paypal_order_id"] is False
    assert mine[0]["paypal_order_id"] is None


def test_recent_creating_excluded(no_money):
    recent = datetime.now(timezone.utc) - timedelta(seconds=5)
    aid = insert_attempt(recent, order_id=ORDER_ID)
    rows = client.get(LIST_URL).json()["attempts"]
    assert all(a["attempt_id"] != aid for a in rows)


def test_failed_and_finalized_excluded_from_triage(no_money):
    old = datetime.now(timezone.utc) - timedelta(seconds=3600)
    failed = insert_attempt(old, status=CatalogAttemptStatus.FAILED, order_id="O-F")
    final = insert_attempt(old, status=CatalogAttemptStatus.FINALIZED, order_id="O-D")
    rows = client.get(LIST_URL).json()["attempts"]
    ids = {a["attempt_id"] for a in rows}
    assert failed not in ids and final not in ids


def test_list_rejects_out_of_range_limit(no_money):
    assert client.get(LIST_URL, params={"limit": 0}).status_code == 422
    assert client.get(LIST_URL, params={"limit": 101}).status_code == 422


# ===========================================================================
# B. Operator read-first recovery
# ===========================================================================


def test_matching_order_recovers_to_finalized(monkeypatch, no_money):
    attempt = seed_creating_with_id()
    monkeypatch.setattr(pes, "get_order", lambda oid: fake_order(status="CREATED"))

    body = reconcile(attempt.attempt_id).json()

    assert body["outcome"] == "recovered_finalized"
    assert body["reconciled"] is True
    assert body["status"] == "FINALIZED"
    assert body["transaction_id"]
    refreshed = cas.get(attempt.attempt_id)
    assert refreshed.status is CatalogAttemptStatus.FINALIZED
    txn = transaction_service.get_transaction_by_order(ORDER_ID)
    assert txn.transaction_id == body["transaction_id"]
    assert txn.status.value == "ORDER_CREATED"  # created in the normal state
    assert no_money == []


def test_approved_order_also_recovers_to_finalized(monkeypatch, no_money):
    attempt = seed_creating_with_id()
    monkeypatch.setattr(pes, "get_order", lambda oid: fake_order(status="APPROVED"))
    body = reconcile(attempt.attempt_id).json()
    assert body["outcome"] == "recovered_finalized"
    assert cas.get(attempt.attempt_id).status is CatalogAttemptStatus.FINALIZED
    assert no_money == []


def test_existing_transaction_reused_without_duplicate(monkeypatch, no_money):
    attempt = seed_creating_with_id()
    # A transaction already exists for this order id (finalize-crash aftermath).
    existing = transaction_service.create_transaction(
        product_id=PRODUCT_ID, product_name="boAt Rockerz 450",
        catalog_price_inr=PRICE_INR, paypal_amount=AMOUNT,
        paypal_currency=CURRENCY, paypal_order_id=ORDER_ID,
    )
    monkeypatch.setattr(pes, "get_order", lambda oid: fake_order(status="CREATED"))

    body = reconcile(attempt.attempt_id).json()

    assert body["outcome"] == "recovered_finalized"
    assert body["transaction_id"] == existing.transaction_id  # linked, not new
    assert len(transaction_service.list_transactions(limit=100)) == 1  # no dupe
    assert no_money == []


@pytest.mark.parametrize(
    "order,reason",
    [
        (fake_order(status="COMPLETED"), "captured"),
        (fake_order(status="VOIDED"), "voided"),
        (fake_order(value="0.01"), "amount"),
        (fake_order(currency="EUR"), "currency"),
        (fake_order(order_id="OTHER"), "order-id"),
    ],
)
def test_discrepant_order_leaves_attempt_unchanged(monkeypatch, no_money, order, reason):
    attempt = seed_creating_with_id()
    monkeypatch.setattr(pes, "get_order", lambda oid: order)

    body = reconcile(attempt.attempt_id).json()

    assert body["outcome"] == "review_required", reason
    assert body["reconciled"] is False
    assert cas.get(attempt.attempt_id).status is CatalogAttemptStatus.CREATING
    # No ledger row fabricated for a discrepant order.
    with pytest.raises(transaction_service.TransactionNotFoundError):
        transaction_service.get_transaction_by_order(ORDER_ID)
    assert no_money == []


def test_paypal_read_failure_is_inconclusive(monkeypatch, no_money):
    attempt = seed_creating_with_id()

    def boom(oid):
        raise paypal.PayPalGetOrderError("HTTP 500")

    monkeypatch.setattr(pes, "get_order", boom)
    body = reconcile(attempt.attempt_id).json()
    assert body["outcome"] == "inconclusive"
    assert body["retryable"] is True
    assert cas.get(attempt.attempt_id).status is CatalogAttemptStatus.CREATING
    assert no_money == []


def test_missing_product_during_recovery_is_review_required(monkeypatch, no_money):
    # The attempt's product is not in the catalog and no txn exists yet.
    attempt = seed_creating_with_id(product_id="GHOST-PRODUCT")
    monkeypatch.setattr(pes, "get_order", lambda oid: fake_order(status="CREATED"))

    body = reconcile(attempt.attempt_id).json()

    assert body["outcome"] == "review_required"
    assert body["detail"]["reason"] == "PRODUCT_UNAVAILABLE"
    assert cas.get(attempt.attempt_id).status is CatalogAttemptStatus.CREATING
    assert transaction_service.list_transactions(limit=100) == []
    assert no_money == []


def test_no_id_attempt_is_manual_review(monkeypatch, no_money):
    attempt = cas.reserve_attempt(PRODUCT_ID, PRICE_INR, AMOUNT, CURRENCY)  # no id
    monkeypatch.setattr(
        pes, "get_order", lambda oid: pytest.fail("no-id must not read PayPal")
    )
    body = reconcile(attempt.attempt_id).json()
    assert body["outcome"] == "manual_review_required"
    assert body["review_required"] is True
    assert cas.get(attempt.attempt_id).status is CatalogAttemptStatus.CREATING
    assert no_money == []


def test_reconcile_is_idempotent(monkeypatch, no_money):
    attempt = seed_creating_with_id()
    monkeypatch.setattr(pes, "get_order", lambda oid: fake_order(status="CREATED"))

    first = reconcile(attempt.attempt_id).json()
    # After FINALIZED, get_order must not be called again.
    monkeypatch.setattr(
        pes, "get_order", lambda oid: pytest.fail("finalized must not re-read")
    )
    second = reconcile(attempt.attempt_id).json()

    assert first["outcome"] == "recovered_finalized"
    assert second["outcome"] == "already_finalized"
    assert len(transaction_service.list_transactions(limit=100)) == 1


def test_failed_attempt_is_not_applicable(monkeypatch, no_money):
    aid = insert_attempt(
        datetime.now(timezone.utc), status=CatalogAttemptStatus.FAILED, order_id="O-X"
    )
    monkeypatch.setattr(
        pes, "get_order", lambda oid: pytest.fail("FAILED must not read PayPal")
    )
    body = reconcile(aid).json()
    assert body["outcome"] == "not_applicable"
    assert no_money == []


def test_unknown_attempt_is_404(no_money):
    assert reconcile("cpa_does_not_exist").status_code == 404


# ===========================================================================
# C. Concurrency: exactly one finalization, one transaction
# ===========================================================================


def test_concurrent_reconciliation_finalizes_once(monkeypatch, no_money):
    attempt = seed_creating_with_id()
    monkeypatch.setattr(pes, "get_order", lambda oid: fake_order(status="CREATED"))

    outcomes: list[str] = []
    lock = threading.Lock()

    def worker():
        o, _, _ = pes.reconcile_creating_attempt(cas.get(attempt.attempt_id))
        with lock:
            outcomes.append(o)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive()

    assert all(o in ("recovered_finalized", "already_finalized") for o in outcomes)
    assert cas.get(attempt.attempt_id).status is CatalogAttemptStatus.FINALIZED
    assert len(transaction_service.list_transactions(limit=100)) == 1  # one txn
    assert no_money == []


# ===========================================================================
# D. Webhook fallback reconciliation
# ===========================================================================


def _event(event_type, order_id):
    return {
        "id": f"WH-{uuid.uuid4().hex}",
        "event_type": event_type,
        "resource": {"id": order_id},
    }


def test_webhook_resolves_creating_attempt_by_stored_id(monkeypatch, no_money):
    attempt = seed_creating_with_id()
    monkeypatch.setattr(pes, "get_order", lambda oid: fake_order(status="APPROVED"))

    result = reconcile_catalog_attempt_event(
        _event("CHECKOUT.ORDER.APPROVED", ORDER_ID)
    )

    assert result["action"] == "recovered_finalized"
    assert result["attempt_id"] == attempt.attempt_id
    assert cas.get(attempt.attempt_id).status is CatalogAttemptStatus.FINALIZED
    assert no_money == []


def test_webhook_payload_only_id_resolves_to_nothing(monkeypatch, no_money):
    seed_creating_with_id(order_id=ORDER_ID)
    monkeypatch.setattr(
        pes, "get_order", lambda oid: pytest.fail("must not read for a foreign id")
    )
    result = reconcile_catalog_attempt_event(
        _event("CHECKOUT.ORDER.APPROVED", "ATTACKER-ORDER")
    )
    assert result["action"] == "catalog_attempt_not_found"
    assert no_money == []


def test_webhook_duplicate_event_is_idempotent(monkeypatch, no_money):
    attempt = seed_creating_with_id()
    monkeypatch.setattr(pes, "get_order", lambda oid: fake_order(status="CREATED"))
    event = _event("CHECKOUT.ORDER.APPROVED", ORDER_ID)

    first = reconcile_catalog_attempt_event(event)
    assert first["action"] == "recovered_finalized"
    # Redelivery: the attempt is now FINALIZED, so it is no longer a CREATING
    # orphan this fallback owns — resolved to not-found (handled by the primary
    # transaction reconciler), never regressing it.
    monkeypatch.setattr(
        pes, "get_order", lambda oid: pytest.fail("finalized must not re-read")
    )
    second = reconcile_catalog_attempt_event(event)
    assert second["action"] == "catalog_attempt_not_found"
    assert cas.get(attempt.attempt_id).status is CatalogAttemptStatus.FINALIZED
    assert no_money == []


def test_webhook_captured_order_is_review_required(monkeypatch, no_money):
    attempt = seed_creating_with_id()
    monkeypatch.setattr(pes, "get_order", lambda oid: fake_order(status="COMPLETED"))

    # A capture event carries the order id under supplementary_data.related_ids
    # (resource.id is the capture id), matching how _identify extracts it.
    capture_event = {
        "id": f"WH-{uuid.uuid4().hex}",
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {
            "id": "CAP-1",
            "status": "COMPLETED",
            "supplementary_data": {"related_ids": {"order_id": ORDER_ID}},
        },
    }
    result = reconcile_catalog_attempt_event(capture_event)

    assert result["action"] == "review_required"
    assert cas.get(attempt.attempt_id).status is CatalogAttemptStatus.CREATING
    assert no_money == []


def test_webhook_unknown_event_type_ignored(no_money):
    result = reconcile_catalog_attempt_event(_event("SOME.OTHER.EVENT", ORDER_ID))
    assert result["action"] == "ignored_event_type"
