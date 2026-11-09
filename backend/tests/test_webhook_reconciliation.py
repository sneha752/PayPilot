"""Tests for webhook → ledger reconciliation (Phase 8B — HTTP fully mocked).

Covers how VERIFIED PayPal webhook events move transactions through the
lifecycle (ORDER_CREATED → ORDER_APPROVED → CAPTURED → REFUNDED, with
DENIED → FAILED), idempotent redelivery, out-of-order protection, lookup by
order id and by capture id, and that unverified events never touch the
ledger. Verification itself is mocked at the PayPal HTTP boundary exactly
as in test_webhooks.py; no real PayPal calls are made.
"""

import pytest
from starlette.testclient import TestClient

from app.main import app
from app.services import transaction_service, webhook_service

client = TestClient(app, raise_server_exceptions=False)

WEBHOOK_URL = "/webhooks/paypal"

GOOD_HEADERS = {
    "PAYPAL-TRANSMISSION-ID": "tid-123",
    "PAYPAL-TRANSMISSION-TIME": "2026-10-04T10:00:00Z",
    "PAYPAL-TRANSMISSION-SIG": "sig-abc",
    "PAYPAL-CERT-URL": "https://api.sandbox.paypal.com/cert.pem",
    "PAYPAL-AUTH-ALGO": "SHA256withRSA",
}


class FakeResponse:
    def __init__(self, status_code: int, payload=None):
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


def _mock_paypal(monkeypatch, verify_response):
    def fake_post(url, **kwargs):
        if url.endswith("/v1/oauth2/token"):
            return TOKEN_OK
        if url.endswith("/v1/notifications/verify-webhook-signature"):
            return verify_response
        raise AssertionError(f"Unexpected PayPal URL called: {url}")

    monkeypatch.setattr(webhook_service.httpx, "post", fake_post)


# --- Event builders (shapes mirror real PayPal webhook resources) -------------


def capture_event(event_type: str, order_id: str, capture_id: str = "CAP-1") -> dict:
    """PAYMENT.CAPTURE.COMPLETED / DENIED: resource is the capture."""
    return {
        "id": "WH-EVT-1",
        "event_type": event_type,
        "resource": {
            "id": capture_id,
            "status": "COMPLETED",
            "supplementary_data": {"related_ids": {"order_id": order_id}},
        },
    }


def refund_event(
    order_id: str | None = None,
    capture_id: str = "CAP-1",
    refund_id: str = "REF-9",
) -> dict:
    """PAYMENT.CAPTURE.REFUNDED: resource is the REFUND (id = refund id);
    the capture id only appears in the "up" link."""
    resource: dict = {
        "id": refund_id,
        "status": "COMPLETED",
        "links": [
            {
                "rel": "up",
                "href": f"https://api.sandbox.paypal.com/v2/payments/captures/{capture_id}",
            }
        ],
    }
    if order_id:
        resource["supplementary_data"] = {"related_ids": {"order_id": order_id}}
    return {"id": "WH-EVT-2", "event_type": "PAYMENT.CAPTURE.REFUNDED", "resource": resource}


def order_approved_event(order_id: str) -> dict:
    """CHECKOUT.ORDER.APPROVED: resource IS the order."""
    return {
        "id": "WH-EVT-3",
        "event_type": "CHECKOUT.ORDER.APPROVED",
        "resource": {"id": order_id, "status": "APPROVED"},
    }


def make_transaction(order_id: str = "ORDER-1"):
    return transaction_service.create_transaction(
        product_id="p1",
        product_name="Demo Product",
        catalog_price_inr=1000,
        paypal_amount="12.00",
        paypal_currency="USD",
        paypal_order_id=order_id,
    )


def post_webhook(event: dict):
    return client.post(WEBHOOK_URL, json=event, headers=GOOD_HEADERS)


def ledger_status(order_id: str) -> str:
    return transaction_service.get_transaction_by_order(order_id).status.value


# --- Capture completed -> CAPTURED ------------------------------------------


def test_capture_completed_marks_transaction_captured(creds, monkeypatch):
    _mock_paypal(monkeypatch, VERIFY_SUCCESS)
    txn = make_transaction("ORDER-1")

    resp = post_webhook(capture_event("PAYMENT.CAPTURE.COMPLETED", "ORDER-1"))

    assert resp.status_code == 200
    rec = resp.json()["reconciliation"]
    assert rec["action"] == "updated"
    assert rec["old_status"] == "ORDER_CREATED"
    assert rec["status"] == "CAPTURED"
    assert rec["transaction_id"] == txn.transaction_id

    stored = transaction_service.get_transaction_by_order("ORDER-1")
    assert stored.status.value == "CAPTURED"
    assert stored.paypal_capture_id == "CAP-1"


def test_capture_completed_is_idempotent_on_redelivery(creds, monkeypatch):
    _mock_paypal(monkeypatch, VERIFY_SUCCESS)
    make_transaction("ORDER-1")
    event = capture_event("PAYMENT.CAPTURE.COMPLETED", "ORDER-1")

    assert post_webhook(event).json()["reconciliation"]["action"] == "updated"
    second = post_webhook(event)

    assert second.status_code == 200
    assert second.json()["reconciliation"]["action"] == "already_applied"
    assert ledger_status("ORDER-1") == "CAPTURED"


# --- Capture denied -> FAILED --------------------------------------------------


def test_capture_denied_marks_transaction_failed(creds, monkeypatch):
    _mock_paypal(monkeypatch, VERIFY_SUCCESS)
    make_transaction("ORDER-1")

    resp = post_webhook(capture_event("PAYMENT.CAPTURE.DENIED", "ORDER-1"))

    assert resp.json()["reconciliation"]["action"] == "updated"
    assert ledger_status("ORDER-1") == "FAILED"


def test_stale_denied_never_regresses_a_captured_transaction(creds, monkeypatch):
    _mock_paypal(monkeypatch, VERIFY_SUCCESS)
    make_transaction("ORDER-1")
    post_webhook(capture_event("PAYMENT.CAPTURE.COMPLETED", "ORDER-1"))

    resp = post_webhook(capture_event("PAYMENT.CAPTURE.DENIED", "ORDER-1"))

    assert resp.status_code == 200
    assert resp.json()["reconciliation"]["action"] == "out_of_order_ignored"
    assert ledger_status("ORDER-1") == "CAPTURED"


# --- Refunded -> REFUNDED ---------------------------------------------------------


def test_refund_after_capture_marks_refunded(creds, monkeypatch):
    _mock_paypal(monkeypatch, VERIFY_SUCCESS)
    make_transaction("ORDER-1")
    post_webhook(capture_event("PAYMENT.CAPTURE.COMPLETED", "ORDER-1"))

    resp = post_webhook(refund_event(order_id="ORDER-1"))

    assert resp.json()["reconciliation"]["action"] == "updated"
    stored = transaction_service.get_transaction_by_order("ORDER-1")
    assert stored.status.value == "REFUNDED"
    # The refund id must never be stored as the capture id.
    assert stored.paypal_capture_id == "CAP-1"


def test_refund_found_via_capture_id_when_order_id_absent(creds, monkeypatch):
    _mock_paypal(monkeypatch, VERIFY_SUCCESS)
    make_transaction("ORDER-1")
    post_webhook(capture_event("PAYMENT.CAPTURE.COMPLETED", "ORDER-1"))

    # Refund event carrying only the "up" link to capture CAP-1.
    resp = post_webhook(refund_event(order_id=None, capture_id="CAP-1"))

    assert resp.json()["reconciliation"]["action"] == "updated"
    assert ledger_status("ORDER-1") == "REFUNDED"


def test_refund_accepted_even_if_capture_webhook_was_missed(creds, monkeypatch):
    # Out-of-order delivery: REFUNDED arrives while we still say ORDER_CREATED.
    _mock_paypal(monkeypatch, VERIFY_SUCCESS)
    make_transaction("ORDER-1")

    resp = post_webhook(refund_event(order_id="ORDER-1"))

    assert resp.json()["reconciliation"]["action"] == "updated"
    assert ledger_status("ORDER-1") == "REFUNDED"


# --- Order approved -> ORDER_APPROVED ---------------------------------------------


def test_order_approved_then_captured_follows_lifecycle(creds, monkeypatch):
    _mock_paypal(monkeypatch, VERIFY_SUCCESS)
    make_transaction("ORDER-1")

    approved = post_webhook(order_approved_event("ORDER-1"))
    assert approved.json()["reconciliation"]["action"] == "updated"
    assert ledger_status("ORDER-1") == "ORDER_APPROVED"

    captured = post_webhook(capture_event("PAYMENT.CAPTURE.COMPLETED", "ORDER-1"))
    assert captured.json()["reconciliation"]["action"] == "updated"
    assert ledger_status("ORDER-1") == "CAPTURED"


def test_stale_order_approved_after_capture_is_ignored(creds, monkeypatch):
    _mock_paypal(monkeypatch, VERIFY_SUCCESS)
    make_transaction("ORDER-1")
    post_webhook(capture_event("PAYMENT.CAPTURE.COMPLETED", "ORDER-1"))

    resp = post_webhook(order_approved_event("ORDER-1"))

    assert resp.json()["reconciliation"]["action"] == "out_of_order_ignored"
    assert ledger_status("ORDER-1") == "CAPTURED"


# --- Events that must not write ------------------------------------------------


def test_unknown_order_is_acknowledged_without_ledger_change(creds, monkeypatch):
    _mock_paypal(monkeypatch, VERIFY_SUCCESS)
    make_transaction("ORDER-1")

    resp = post_webhook(
        capture_event("PAYMENT.CAPTURE.COMPLETED", "SOMEONE-ELSES-ORDER", "CAP-X")
    )

    # 200 so PayPal does not redeliver forever; our ledger is untouched.
    assert resp.status_code == 200
    assert resp.json()["reconciliation"]["action"] == "transaction_not_found"
    assert ledger_status("ORDER-1") == "ORDER_CREATED"


def test_unknown_event_type_changes_nothing(creds, monkeypatch):
    _mock_paypal(monkeypatch, VERIFY_SUCCESS)
    make_transaction("ORDER-1")

    event = {
        "id": "WH-EVT-9",
        "event_type": "BILLING.PLAN.CREATED",
        "resource": {"id": "ORDER-1"},
    }
    resp = post_webhook(event)

    assert resp.status_code == 200
    assert resp.json()["reconciliation"]["action"] == "ignored_event_type"
    assert ledger_status("ORDER-1") == "ORDER_CREATED"


def test_event_without_identifiers_changes_nothing(creds, monkeypatch):
    _mock_paypal(monkeypatch, VERIFY_SUCCESS)
    make_transaction("ORDER-1")

    event = {
        "id": "WH-EVT-10",
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {"status": "COMPLETED"},  # no ids at all
    }
    resp = post_webhook(event)

    assert resp.status_code == 200
    assert resp.json()["reconciliation"]["action"] == "unidentifiable"
    assert ledger_status("ORDER-1") == "ORDER_CREATED"


def test_unverified_event_never_touches_the_ledger(creds, monkeypatch):
    """The 8A gate still holds: FAILURE verdict → 400 and zero reconciliation."""
    _mock_paypal(monkeypatch, VERIFY_FAILURE)
    make_transaction("ORDER-1")

    resp = post_webhook(capture_event("PAYMENT.CAPTURE.COMPLETED", "ORDER-1"))

    assert resp.status_code == 400
    assert ledger_status("ORDER-1") == "ORDER_CREATED"


# --- Read-back through the existing API ----------------------------------------


def test_reconciled_status_visible_via_transactions_endpoint(creds, monkeypatch):
    _mock_paypal(monkeypatch, VERIFY_SUCCESS)
    make_transaction("ORDER-1")
    post_webhook(capture_event("PAYMENT.CAPTURE.COMPLETED", "ORDER-1"))

    resp = client.get("/transactions/paypal/ORDER-1")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "CAPTURED"
    assert body["paypal_capture_id"] == "CAP-1"
