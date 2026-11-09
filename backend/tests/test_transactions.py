"""Tests for the SQLite transaction ledger and its query endpoints.

PayPal is mocked at the purchase-service boundary; the ledger, catalog,
Policy Engine, and currency service run for real against a per-test
temporary database (see conftest.py). Core invariants: a record exists only
after PayPal order creation succeeds, and transitions to CAPTURED only
after a successful PayPal capture response.
"""

import json

import pytest
from starlette.testclient import TestClient

from app import paypal
from app.main import app
from app.services import purchase_execution_service, transaction_service

client = TestClient(app, raise_server_exceptions=False)

AUTONOMOUS_50K = {
    "purchase_requested": True,
    "autonomous_purchase_allowed": True,
    "auto_approve_below_inr": 50000,
}


@pytest.fixture
def create_order_ok(monkeypatch):
    calls: list[dict] = []

    def fake_create_order(amount, currency="USD", description=None, request_id=None):
        calls.append({"amount": amount, "currency": currency})
        return {
            "order_id": "ORDER-7C",
            "status": "CREATED",
            "approve_url": "https://www.sandbox.paypal.com/checkoutnow?token=ORDER-7C",
        }

    monkeypatch.setattr(purchase_execution_service, "create_order", fake_create_order)
    return calls


@pytest.fixture
def capture_ok(monkeypatch):
    def fake_capture(order_id):
        return {
            "order_id": order_id,
            "status": "COMPLETED",
            "capture_id": "CAP-7C",
            "capture_status": "COMPLETED",
            "amount": "498.89",
            "currency": "USD",
        }

    monkeypatch.setattr(purchase_execution_service, "capture_order", fake_capture)


def _execute_tab003(**extra):
    return client.post(
        "/purchase/execute",
        json={"product_id": "TAB003", "authorization": AUTONOMOUS_50K, **extra},
    )


# --- 1. Order creation -> ORDER_CREATED record -----------------------------------


def test_order_creation_creates_transaction(create_order_ok):
    body = _execute_tab003().json()
    assert body["status"] == "order_created"
    txn_id = body["transaction_id"]
    assert txn_id and txn_id.startswith("txn_")

    txn = client.get(f"/transactions/{txn_id}").json()
    assert txn["status"] == "ORDER_CREATED"
    assert txn["product_id"] == "TAB003"
    assert txn["product_name"] == "Apple iPad (10th Gen)"
    assert txn["catalog_price_inr"] == 44900
    assert txn["paypal_amount"] == "498.89"
    assert txn["paypal_currency"] == "USD"
    assert txn["paypal_order_id"] == "ORDER-7C"
    assert txn["paypal_capture_id"] is None
    assert txn["created_at"] and txn["updated_at"]


# --- 2. PayPal failure -> no record -----------------------------------------------


def test_failed_order_creation_creates_no_transaction(monkeypatch):
    def failing_create_order(amount, currency="USD", description=None, request_id=None):
        raise paypal.PayPalOrderError("PayPal returned HTTP 500 for create-order")

    monkeypatch.setattr(
        purchase_execution_service, "create_order", failing_create_order
    )
    assert _execute_tab003().status_code == 502
    with pytest.raises(transaction_service.TransactionNotFoundError):
        transaction_service.get_transaction_by_order("ORDER-7C")


# --- 3. Escalation/denial -> no record ---------------------------------------------


def test_approval_required_creates_no_transaction(create_order_ok):
    body = client.post(
        "/purchase/execute",
        json={
            "product_id": "LAP002",
            "authorization": AUTONOMOUS_50K,
            "hard_constraints": {"max_price_inr": 80000},
        },
    ).json()
    assert body["status"] == "approval_required"
    assert body["transaction_id"] is None
    assert create_order_ok == []  # no PayPal order, hence no record either


# --- 4/5. Capture -> CAPTURED + capture id -------------------------------------------


def test_capture_updates_transaction(create_order_ok, capture_ok):
    created = _execute_tab003().json()
    txn_id = created["transaction_id"]

    resp = client.post("/purchase/capture/ORDER-7C")
    assert resp.status_code == 200
    body = resp.json()
    assert body["transaction_id"] == txn_id
    assert body["transaction_status"] == "CAPTURED"

    txn = client.get(f"/transactions/{txn_id}").json()
    assert txn["status"] == "CAPTURED"
    assert txn["paypal_capture_id"] == "CAP-7C"
    assert txn["updated_at"] >= txn["created_at"]


# --- 6. Failed capture -> state retained ----------------------------------------------


def test_failed_capture_retains_order_created(create_order_ok, monkeypatch):
    txn_id = _execute_tab003().json()["transaction_id"]

    def failing_capture(order_id):
        raise paypal.PayPalOrderNotApprovedError("not approved yet")

    monkeypatch.setattr(purchase_execution_service, "capture_order", failing_capture)
    assert client.post("/purchase/capture/ORDER-7C").status_code == 409

    txn = client.get(f"/transactions/{txn_id}").json()
    assert txn["status"] == "ORDER_CREATED"      # NOT captured
    assert txn["paypal_capture_id"] is None


# --- 7/8. Retrieval ---------------------------------------------------------------------


def test_lookup_by_paypal_order_id(create_order_ok):
    txn_id = _execute_tab003().json()["transaction_id"]
    resp = client.get("/transactions/paypal/ORDER-7C")
    assert resp.status_code == 200
    assert resp.json()["transaction_id"] == txn_id


# --- 9/10. Unknown ids -> 404 --------------------------------------------------------------


def test_unknown_transaction_404():
    assert client.get("/transactions/txn_nope").status_code == 404


def test_unknown_paypal_order_404():
    assert client.get("/transactions/paypal/NOPE-123").status_code == 404


# --- 11/12/13. Stored values are authoritative ----------------------------------------------


def test_client_price_tampering_cannot_alter_stored_price(create_order_ok):
    body = _execute_tab003(price_inr=1, catalog_price_inr=1).json()
    txn = client.get(f"/transactions/{body['transaction_id']}").json()
    assert txn["catalog_price_inr"] == 44900     # catalog, not client


def test_stored_amount_is_what_paypal_received(create_order_ok):
    txn_id = _execute_tab003().json()["transaction_id"]
    sent_to_paypal = create_order_ok[0]["amount"]
    txn = client.get(f"/transactions/{txn_id}").json()
    assert txn["paypal_amount"] == sent_to_paypal == "498.89"


# --- 14. No credentials stored ----------------------------------------------------------------


def test_no_credentials_in_transaction_record(create_order_ok):
    txn_id = _execute_tab003().json()["transaction_id"]
    text = json.dumps(client.get(f"/transactions/{txn_id}").json()).lower()
    for secret in ("client_secret", "access_token", "bearer", "client_id", "secret"):
        assert secret not in text


# --- The ledger is the capture gate (Phase 11F-B0) -----------------------------------------------
#
# This replaces an earlier test that asserted the OPPOSITE: that an order
# unknown to the ledger captured fine and simply reported no transaction.
# That was the vulnerability Phase 11F-A found. The ledger is still not the
# payment authority — it never decides whether an owned order is approved or
# already captured, PayPal does — but it IS now the record of WHOSE order
# this is, and without one there is nothing authorizing a capture at all.


def test_capture_of_an_order_the_ledger_does_not_know_is_refused(monkeypatch):
    def explode(order_id):  # pragma: no cover - fails the test if reached
        raise AssertionError("PayPal must not be called for an unowned order")

    monkeypatch.setattr(purchase_execution_service, "capture_order", explode)

    resp = client.post("/purchase/capture/UNTRACKED-ORDER")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "UNKNOWN_ORDER"


def test_capture_of_a_ledger_owned_order_still_succeeds(create_order_ok, capture_ok):
    txn_id = _execute_tab003().json()["transaction_id"]

    resp = client.post("/purchase/capture/ORDER-7C")
    assert resp.status_code == 200
    body = resp.json()
    assert body["capture_status"] == "COMPLETED"
    assert body["transaction_id"] == txn_id
    assert body["transaction_status"] == "CAPTURED"
