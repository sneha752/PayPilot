"""Phase 11F-J end-to-end integration tests for the parts that authentication
and recovery most affect, plus the D-5 catalog-capture status fix.

Focus — genuine integration boundaries, not re-tests of unit behavior:
  * A catalog capture whose nested PayPal status is COMPLETED collects; any
    other nested status (e.g. PENDING) is NOT recorded as collected and the
    ledger is left unchanged (D-5 fix), on BOTH catalog capture routes.
  * A pending catalog capture is later reconciled to CAPTURED by a verified
    PAYMENT.CAPTURE.COMPLETED webhook — proving the fix leaves the order
    recoverable rather than stranded.
  * Catalog/external ownership separation holds at the capture route.

All PayPal HTTP is mocked; no live call or money movement. Auth is provided
by the conftest token auto-injection (the real gate still runs).
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient

from app.main import app
from app.services import (
    external_purchase_service as eps,
    purchase_execution_service as pes,
    transaction_service,
    webhook_service,
)

client = TestClient(app, raise_server_exceptions=False)

CATALOG_ROUTES = ["/purchase/capture", "/paypal/capture-order"]


def make_catalog_txn(order_id="CATO-1"):
    return transaction_service.create_transaction(
        product_id="p1",
        product_name="Demo Product",
        catalog_price_inr=1000,
        paypal_amount="12.00",
        paypal_currency="USD",
        paypal_order_id=order_id,
    )


def mock_capture(monkeypatch, capture_status, *, order_id="CATO-1"):
    def fake_capture_order(oid):
        return {
            "order_id": oid,
            "status": "COMPLETED" if capture_status == "COMPLETED" else "PENDING",
            "capture_id": "CAPX",
            "capture_status": capture_status,
            "amount": "12.00",
            "currency": "USD",
        }

    monkeypatch.setattr(pes, "capture_order", fake_capture_order)


def insert_external_execution(order_id, status="ORDER_CREATED"):
    approval_id = f"appr_{uuid.uuid4().hex}"
    execution_id = f"xord_{uuid.uuid4().hex}"
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(eps._db_path()) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(eps._SCHEMA)
        eps._migrate(conn)
        conn.execute(
            f"""INSERT INTO external_purchase_orders ({", ".join(eps._COLUMNS)})
                VALUES ({", ".join("?" * len(eps._COLUMNS))})""",
            (execution_id, approval_id, "adopt_x", "reval_x", order_id, "12.00",
             "USD", None, None, None, None, None, None, status, now, now),
        )
        conn.commit()
    return execution_id


def capture_via(route, order_id):
    return client.post(f"{route}/{order_id}")


def ledger_status(order_id):
    return transaction_service.get_transaction_by_order(order_id).status.value


# ---------------------------------------------------------------------------
# D-5: nested capture status gates whether the ledger is marked CAPTURED
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", CATALOG_ROUTES)
def test_completed_catalog_capture_is_collected(monkeypatch, route):
    make_catalog_txn("CATO-OK")
    mock_capture(monkeypatch, "COMPLETED")

    resp = capture_via(route, "CATO-OK")
    assert resp.status_code == 200, resp.text
    assert ledger_status("CATO-OK") == "CAPTURED"


@pytest.mark.parametrize("route", CATALOG_ROUTES)
def test_pending_catalog_capture_is_not_recorded_as_collected(monkeypatch, route):
    make_catalog_txn("CATO-PEND")
    mock_capture(monkeypatch, "PENDING")

    resp = capture_via(route, "CATO-PEND")
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["code"] == "CAPTURE_NOT_COMPLETED"
    assert detail["captured"] is False
    assert detail["capture_status"] == "PENDING"
    # The ledger must NOT have been marked CAPTURED.
    assert ledger_status("CATO-PEND") == "ORDER_CREATED"


@pytest.mark.parametrize("status", ["PENDING", "DECLINED", "", "FOO"])
def test_non_completed_statuses_never_collect(monkeypatch, status):
    make_catalog_txn("CATO-X")
    mock_capture(monkeypatch, status)
    resp = capture_via("/purchase/capture", "CATO-X")
    assert resp.status_code == 502
    assert ledger_status("CATO-X") == "ORDER_CREATED"


# ---------------------------------------------------------------------------
# Integration: a pending catalog capture is later reconciled by a verified
# COMPLETED webhook (the fix leaves the order recoverable, not stranded).
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._p = payload

    def json(self):
        return self._p


def test_pending_then_completed_webhook_reconciles_to_captured(monkeypatch):
    make_catalog_txn("CATO-FLOW")
    mock_capture(monkeypatch, "PENDING")
    # Pending capture: 502, ledger stays ORDER_CREATED.
    assert capture_via("/purchase/capture", "CATO-FLOW").status_code == 502
    assert ledger_status("CATO-FLOW") == "ORDER_CREATED"

    # Now a VERIFIED PayPal webhook reports the capture completed.
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "x")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "y")
    monkeypatch.setenv("PAYPAL_WEBHOOK_ID", "whid")

    def fake_post(url, **kwargs):
        if url.endswith("/v1/oauth2/token"):
            return _FakeResp(200, {"access_token": "t", "token_type": "Bearer"})
        if url.endswith("/v1/notifications/verify-webhook-signature"):
            return _FakeResp(200, {"verification_status": "SUCCESS"})
        raise AssertionError(url)

    monkeypatch.setattr(webhook_service.httpx, "post", fake_post)

    headers = {
        "PAYPAL-TRANSMISSION-ID": "t",
        "PAYPAL-TRANSMISSION-TIME": "2026-10-11T10:00:00Z",
        "PAYPAL-TRANSMISSION-SIG": "s",
        "PAYPAL-CERT-URL": "https://api.sandbox.paypal.com/c.pem",
        "PAYPAL-AUTH-ALGO": "SHA256withRSA",
    }
    event = {
        "id": "WH-FLOW",
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {
            "id": "CAP-FLOW",
            "status": "COMPLETED",
            "supplementary_data": {"related_ids": {"order_id": "CATO-FLOW"}},
        },
    }
    resp = client.post("/webhooks/paypal", json=event, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["reconciliation"]["action"] == "updated"
    assert ledger_status("CATO-FLOW") == "CAPTURED"


# ---------------------------------------------------------------------------
# Catalog/external separation at the capture route (ownership gate)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", CATALOG_ROUTES)
def test_external_order_cannot_capture_through_catalog_route(monkeypatch, route):
    insert_external_execution("EXT-ORDER-1", status="BUYER_APPROVED")
    # Trap the capture call: the gate must refuse BEFORE any PayPal contact.
    monkeypatch.setattr(
        pes, "capture_order",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call PayPal")),
    )
    resp = capture_via(route, "EXT-ORDER-1")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "EXTERNAL_ORDER_NOT_CAPTURABLE_HERE"
    # The external execution is untouched and no catalog row was created.
    assert eps.get_by_paypal_order("EXT-ORDER-1").status.value == "BUYER_APPROVED"


def test_unknown_order_cannot_capture(monkeypatch):
    monkeypatch.setattr(
        pes, "capture_order",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call PayPal")),
    )
    resp = capture_via("/purchase/capture", "NOPE-ORDER")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "UNKNOWN_ORDER"
