"""Tests for the PayPal webhook receiver (Phase 8A — HTTP fully mocked).

Covers POST /webhooks/paypal: successful verification, FAILURE verdicts,
each missing PAYPAL-* header, malformed JSON, verification-API failures,
event-type identification, the no-ledger-mutation guarantee, and that no
credential ever appears in responses or logs. No real PayPal calls.
"""

import json
import logging

import pytest
from starlette.testclient import TestClient

from app import paypal
from app.main import app
from app.services import (
    transaction_service,
    webhook_reconciliation_service,
    webhook_service,
)

client = TestClient(app, raise_server_exceptions=False)

WEBHOOK_URL = "/webhooks/paypal"


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

GOOD_HEADERS = {
    "PAYPAL-TRANSMISSION-ID": "tid-123",
    "PAYPAL-TRANSMISSION-TIME": "2026-10-04T10:00:00Z",
    "PAYPAL-TRANSMISSION-SIG": "sig-abc",
    "PAYPAL-CERT-URL": "https://api.sandbox.paypal.com/cert.pem",
    "PAYPAL-AUTH-ALGO": "SHA256withRSA",
}


def _event(event_type: str = "PAYMENT.CAPTURE.COMPLETED") -> dict:
    return {
        "id": "WH-EVT-1",
        "event_type": event_type,
        "resource": {"id": "CAP123", "status": "COMPLETED"},
    }


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("PAYPAL_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("PAYPAL_WEBHOOK_ID", "test-webhook-id")


@pytest.fixture(autouse=True)
def _ledger_must_not_mutate(monkeypatch):
    """No test in THIS module matches a ledger transaction, so none may
    write to it: not the Phase 8A verification path, and not Phase 8B
    reconciliation (which must not write for unmatched/unknown events).
    Matched-event writes are exercised in test_webhook_reconciliation.py."""

    def _forbidden(*args, **kwargs):  # pragma: no cover - fails the test if hit
        raise AssertionError("Webhook handling must not mutate the transaction ledger")

    monkeypatch.setattr(transaction_service, "create_transaction", _forbidden)
    monkeypatch.setattr(transaction_service, "mark_captured", _forbidden)
    monkeypatch.setattr(transaction_service, "update_status", _forbidden)
    # The reconciliation module binds the writer at import time, so guard
    # its reference too.
    monkeypatch.setattr(webhook_reconciliation_service, "update_status", _forbidden)


def _mock_paypal(monkeypatch, verify_response):
    """Route the OAuth token call to success and the verify call to the given
    response (or raise it, if it's an exception); returns recorded calls.

    paypal.py and webhook_service.py share the same httpx module object, so
    one patch covers both.
    """
    calls: list[dict] = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        if url.endswith("/v1/oauth2/token"):
            return TOKEN_OK
        if url.endswith("/v1/notifications/verify-webhook-signature"):
            if isinstance(verify_response, Exception):
                raise verify_response
            return verify_response
        raise AssertionError(f"Unexpected PayPal URL called: {url}")

    monkeypatch.setattr(webhook_service.httpx, "post", fake_post)
    return calls


# --- 1. Valid verification -------------------------------------------------


def test_verified_webhook_returns_verified(creds, monkeypatch):
    calls = _mock_paypal(monkeypatch, VERIFY_SUCCESS)
    resp = client.post(WEBHOOK_URL, json=_event(), headers=GOOD_HEADERS)

    assert resp.status_code == 200
    body_out = resp.json()
    assert body_out["status"] == "verified"
    assert body_out["event_type"] == "PAYMENT.CAPTURE.COMPLETED"
    # No matching ledger transaction exists, so reconciliation changed nothing.
    assert body_out["reconciliation"]["action"] == "transaction_not_found"

    # The verification request carried the official fields, taken from the
    # transmission headers and server config — never from the payload.
    verify_call = calls[-1]
    assert verify_call["url"].endswith("/v1/notifications/verify-webhook-signature")
    assert verify_call["headers"]["Authorization"] == "Bearer fake-token"
    body = verify_call["json"]
    assert body["transmission_id"] == "tid-123"
    assert body["transmission_time"] == "2026-10-04T10:00:00Z"
    assert body["transmission_sig"] == "sig-abc"
    assert body["cert_url"] == "https://api.sandbox.paypal.com/cert.pem"
    assert body["auth_algo"] == "SHA256withRSA"
    assert body["webhook_id"] == "test-webhook-id"
    assert body["webhook_event"] == _event()


# --- 2. Invalid verification -----------------------------------------------


def test_failed_verification_returns_4xx_and_is_not_trusted(creds, monkeypatch):
    _mock_paypal(monkeypatch, VERIFY_FAILURE)
    resp = client.post(WEBHOOK_URL, json=_event(), headers=GOOD_HEADERS)

    assert resp.status_code == 400
    assert resp.json().get("status") != "verified"


def test_client_supplied_verified_field_is_ignored(creds, monkeypatch):
    """A payload claiming {"verified": true} must not bypass verification."""
    _mock_paypal(monkeypatch, VERIFY_FAILURE)
    forged = {**_event(), "verified": True, "verification_status": "SUCCESS"}
    resp = client.post(WEBHOOK_URL, json=forged, headers=GOOD_HEADERS)

    assert resp.status_code == 400
    assert resp.json().get("status") != "verified"


# --- 3-7. Missing required headers ------------------------------------------


@pytest.mark.parametrize(
    "missing_header",
    [
        "PAYPAL-TRANSMISSION-ID",
        "PAYPAL-TRANSMISSION-TIME",
        "PAYPAL-TRANSMISSION-SIG",
        "PAYPAL-CERT-URL",
        "PAYPAL-AUTH-ALGO",
    ],
)
def test_missing_required_header_is_rejected(creds, monkeypatch, missing_header):
    calls = _mock_paypal(monkeypatch, VERIFY_SUCCESS)
    headers = {k: v for k, v in GOOD_HEADERS.items() if k != missing_header}
    resp = client.post(WEBHOOK_URL, json=_event(), headers=headers)

    assert resp.status_code == 400
    assert missing_header in resp.json()["detail"]
    # Rejected before any PayPal call was made.
    assert calls == []


# --- 8. Malformed JSON -------------------------------------------------------


def test_malformed_json_is_rejected(creds, monkeypatch):
    calls = _mock_paypal(monkeypatch, VERIFY_SUCCESS)
    resp = client.post(
        WEBHOOK_URL,
        content=b"{not valid json",
        headers={**GOOD_HEADERS, "Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert calls == []


def test_non_object_json_is_rejected(creds, monkeypatch):
    _mock_paypal(monkeypatch, VERIFY_SUCCESS)
    resp = client.post(WEBHOOK_URL, json=["not", "an", "object"], headers=GOOD_HEADERS)
    assert resp.status_code == 400


# --- 9. PayPal verification API failure --------------------------------------


def test_verification_api_http_error_returns_502(creds, monkeypatch):
    _mock_paypal(monkeypatch, FakeResponse(500, {"name": "INTERNAL_SERVER_ERROR"}))
    resp = client.post(WEBHOOK_URL, json=_event(), headers=GOOD_HEADERS)
    assert resp.status_code == 502
    assert resp.json().get("status") != "verified"


def test_verification_api_network_error_returns_502(creds, monkeypatch):
    _mock_paypal(monkeypatch, paypal.httpx.ConnectError("boom"))
    resp = client.post(WEBHOOK_URL, json=_event(), headers=GOOD_HEADERS)
    assert resp.status_code == 502


def test_verification_response_without_status_returns_502(creds, monkeypatch):
    # An answer that is 200 but carries no verdict must never pass.
    _mock_paypal(monkeypatch, FakeResponse(200, {"something": "else"}))
    resp = client.post(WEBHOOK_URL, json=_event(), headers=GOOD_HEADERS)
    assert resp.status_code == 502


def test_missing_webhook_id_config_returns_503(creds, monkeypatch):
    monkeypatch.delenv("PAYPAL_WEBHOOK_ID", raising=False)
    calls = _mock_paypal(monkeypatch, VERIFY_SUCCESS)
    resp = client.post(WEBHOOK_URL, json=_event(), headers=GOOD_HEADERS)
    assert resp.status_code == 503
    assert calls == []


# --- 10. Unknown event type ---------------------------------------------------


def test_unknown_event_type_verified_without_side_effects(creds, monkeypatch):
    _mock_paypal(monkeypatch, VERIFY_SUCCESS)
    resp = client.post(
        WEBHOOK_URL, json=_event("BILLING.PLAN.CREATED"), headers=GOOD_HEADERS
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "verified"
    assert body["event_type"] == "BILLING.PLAN.CREATED"
    assert body["reconciliation"]["action"] == "ignored_event_type"


def test_service_flags_known_vs_unknown_event_types(creds, monkeypatch):
    _mock_paypal(monkeypatch, VERIFY_SUCCESS)
    known = webhook_service.verify_webhook(
        event=_event("PAYMENT.CAPTURE.DENIED"), headers=GOOD_HEADERS
    )
    unknown = webhook_service.verify_webhook(
        event=_event("BILLING.PLAN.CREATED"), headers=GOOD_HEADERS
    )
    assert known["verified"] and known["known_event_type"] is True
    assert unknown["verified"] and unknown["known_event_type"] is False


# --- 11-13. Known event types verify but DO NOT touch the ledger ---------------
# (the autouse _ledger_must_not_mutate fixture fails any test that writes)


@pytest.mark.parametrize(
    "event_type",
    [
        "PAYMENT.CAPTURE.COMPLETED",
        "PAYMENT.CAPTURE.REFUNDED",
        "CHECKOUT.ORDER.APPROVED",
        "PAYMENT.CAPTURE.DENIED",
    ],
)
def test_known_events_verified_with_no_transaction_update(
    creds, monkeypatch, event_type
):
    _mock_paypal(monkeypatch, VERIFY_SUCCESS)
    resp = client.post(WEBHOOK_URL, json=_event(event_type), headers=GOOD_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "verified"
    assert body["event_type"] == event_type
    # No matching transaction in the isolated test ledger — nothing written
    # (the autouse fixture turns any write attempt into a test failure).
    assert body["reconciliation"]["action"] in (
        "transaction_not_found",
        "unidentifiable",
    )


# --- 14. Credentials never leak into responses or logs --------------------------


def test_no_credentials_in_response_or_logs(creds, monkeypatch, caplog):
    _mock_paypal(monkeypatch, VERIFY_SUCCESS)
    with caplog.at_level(logging.DEBUG):
        ok = client.post(WEBHOOK_URL, json=_event(), headers=GOOD_HEADERS)
        _mock_paypal(monkeypatch, VERIFY_FAILURE)
        bad = client.post(WEBHOOK_URL, json=_event(), headers=GOOD_HEADERS)

    for text in (ok.text, bad.text, caplog.text):
        assert "test-client-secret" not in text
        assert "test-client-id" not in text
        assert "fake-token" not in text
        assert "test-webhook-id" not in text
    # The transmission signature is also kept out of our logs.
    assert "sig-abc" not in caplog.text
