"""Tests for the transaction-aware refund flow (Phase 8C — HTTP fully mocked).

Covers POST /transactions/{transaction_id}/refund: deterministic
eligibility (only CAPTURED with a stored capture id), the ledger moving to
REFUNDED only after PayPal confirms, double-refund rejection without a
second PayPal call, the server-stored capture id being the only refund
identifier (client-supplied ids/amounts/currencies are ignored), error
mapping, and the absence of AI/Policy involvement and credential leakage.
No real PayPal calls, no real refunds.
"""

import logging

import pytest
from starlette.testclient import TestClient

from app import paypal
from app.main import app
from app.schemas.transaction import TransactionStatus
from app.services import refund_service, transaction_service

client = TestClient(app, raise_server_exceptions=False)


class FakeResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


TOKEN_OK = FakeResponse(200, {"access_token": "fake-token", "token_type": "Bearer"})
REFUND_OK = FakeResponse(
    201,
    {
        "id": "REF-123",
        "status": "COMPLETED",
        "amount": {"value": "12.00", "currency_code": "USD"},
    },
)


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("PAYPAL_ENVIRONMENT", "sandbox")


@pytest.fixture(autouse=True)
def _no_ai_or_policy(monkeypatch):
    """Tests 15/16: the refund path must never call Gemini/AI or the Policy
    Engine. Any attempt fails the test."""
    from app.policy import policy_engine
    from app.services.providers import gemini

    def _forbidden(*args, **kwargs):  # pragma: no cover - fails the test if hit
        raise AssertionError("Refund flow must not call AI or the Policy Engine")

    monkeypatch.setattr(policy_engine, "evaluate_policy", _forbidden)
    monkeypatch.setattr(gemini, "GeminiProvider", _forbidden)


def _mock_refund(monkeypatch, refund_response):
    """Route the token call to success and the refund call to the given
    response (or raise it, if it's an exception); returns recorded calls."""
    calls: list[dict] = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        if url.endswith("/v1/oauth2/token"):
            return TOKEN_OK
        if "/v2/payments/captures/" in url and url.endswith("/refund"):
            if isinstance(refund_response, Exception):
                raise refund_response
            return refund_response
        raise AssertionError(f"Unexpected PayPal URL called: {url}")

    monkeypatch.setattr(paypal.httpx, "post", fake_post)
    return calls


def make_transaction(
    status: TransactionStatus = TransactionStatus.CAPTURED,
    capture_id: str | None = "CAP-1",
    order_id: str = "ORDER-1",
):
    txn = transaction_service.create_transaction(
        product_id="p1",
        product_name="Demo Product",
        catalog_price_inr=1000,
        paypal_amount="12.00",
        paypal_currency="USD",
        paypal_order_id=order_id,
    )
    if status != TransactionStatus.ORDER_CREATED:
        txn = transaction_service.update_status(
            txn.transaction_id, status, capture_id=capture_id
        )
    return txn


def refund(transaction_id: str, **kwargs):
    return client.post(f"/transactions/{transaction_id}/refund", **kwargs)


# --- 1-3. Successful refund -----------------------------------------------------


def test_captured_transaction_refunds_successfully(creds, monkeypatch):
    calls = _mock_refund(monkeypatch, REFUND_OK)
    txn = make_transaction()

    resp = refund(txn.transaction_id)

    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "status": "refunded",
        "transaction_id": txn.transaction_id,
        "paypal_order_id": "ORDER-1",
        "paypal_capture_id": "CAP-1",
        "refund_id": "REF-123",
        "refund_status": "COMPLETED",
        "amount": "12.00",
        "currency": "USD",
    }
    # Full refund only: the PayPal request body is empty (no amount chosen).
    refund_call = calls[-1]
    assert refund_call["json"] == {}


def test_successful_refund_moves_ledger_to_refunded(creds, monkeypatch):
    _mock_refund(monkeypatch, REFUND_OK)
    txn = make_transaction()
    assert txn.status == TransactionStatus.CAPTURED

    refund(txn.transaction_id)

    stored = transaction_service.get_transaction(txn.transaction_id)
    assert stored.status == TransactionStatus.REFUNDED
    assert stored.paypal_capture_id == "CAP-1"  # untouched by the refund
    assert stored.updated_at >= txn.updated_at


def test_refund_id_comes_from_paypal_response(creds, monkeypatch):
    _mock_refund(monkeypatch, REFUND_OK)
    txn = make_transaction()
    assert refund(txn.transaction_id).json()["refund_id"] == "REF-123"


# --- 4-7. Deterministic eligibility ------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        TransactionStatus.ORDER_CREATED,
        TransactionStatus.ORDER_APPROVED,
        TransactionStatus.FAILED,
    ],
)
def test_non_captured_states_are_not_refundable(creds, monkeypatch, status):
    calls = _mock_refund(monkeypatch, REFUND_OK)
    txn = make_transaction(status=status, capture_id=None)

    resp = refund(txn.transaction_id)

    assert resp.status_code == 409
    assert status.value in resp.json()["detail"]
    # PayPal was never contacted.
    assert calls == []
    assert (
        transaction_service.get_transaction(txn.transaction_id).status == status
    )


def test_already_refunded_rejected_without_second_paypal_call(creds, monkeypatch):
    calls = _mock_refund(monkeypatch, REFUND_OK)
    txn = make_transaction()

    first = refund(txn.transaction_id)
    assert first.status_code == 200
    paypal_calls_after_first = len(calls)

    second = refund(txn.transaction_id)

    assert second.status_code == 409
    assert "already" in second.json()["detail"].lower()
    # No new PayPal call of any kind happened for the second attempt.
    assert len(calls) == paypal_calls_after_first
    assert (
        transaction_service.get_transaction(txn.transaction_id).status
        == TransactionStatus.REFUNDED
    )


# --- 8. CAPTURED without capture id --------------------------------------------


def test_captured_without_capture_id_never_calls_paypal(creds, monkeypatch):
    calls = _mock_refund(monkeypatch, REFUND_OK)
    txn = make_transaction(capture_id=None)  # CAPTURED but no capture id

    resp = refund(txn.transaction_id)

    assert resp.status_code == 500
    assert calls == []
    assert (
        transaction_service.get_transaction(txn.transaction_id).status
        == TransactionStatus.CAPTURED
    )


# --- 9. Unknown transaction ---------------------------------------------------------


def test_unknown_transaction_returns_404(creds, monkeypatch):
    calls = _mock_refund(monkeypatch, REFUND_OK)
    resp = refund("txn_does_not_exist")
    assert resp.status_code == 404
    assert calls == []


# --- 10. PayPal refund failure leaves CAPTURED ------------------------------------


def test_paypal_refund_failure_leaves_transaction_captured(creds, monkeypatch):
    _mock_refund(
        monkeypatch, FakeResponse(500, {"name": "INTERNAL_SERVICE_ERROR"})
    )
    txn = make_transaction()

    resp = refund(txn.transaction_id)

    assert resp.status_code == 502
    assert (
        transaction_service.get_transaction(txn.transaction_id).status
        == TransactionStatus.CAPTURED
    )


def test_paypal_network_failure_leaves_transaction_refunding(creds, monkeypatch):
    # Phase 11F-K.13: a transport-level failure is AMBIGUOUS (the refund may
    # have been applied), so the atomic claim leaves the row REFUNDING rather
    # than CAPTURED — a retry is then refused (409) instead of silently issuing
    # a possible second refund. Reconciliation (operator or a verified
    # PAYMENT.CAPTURE.REFUNDED webhook) resolves REFUNDING -> REFUNDED.
    _mock_refund(monkeypatch, paypal.httpx.ConnectError("boom"))
    txn = make_transaction()

    resp = refund(txn.transaction_id)

    assert resp.status_code == 502
    assert (
        transaction_service.get_transaction(txn.transaction_id).status
        == TransactionStatus.REFUNDING
    )


def test_paypal_says_already_refunded_maps_to_409_and_marks_refunded(
    creds, monkeypatch
):
    _mock_refund(
        monkeypatch,
        FakeResponse(
            422,
            {
                "name": "UNPROCESSABLE_ENTITY",
                "message": "The requested action could not be performed.",
                "details": [{"issue": "CAPTURE_FULLY_REFUNDED"}],
            },
        ),
    )
    txn = make_transaction()

    resp = refund(txn.transaction_id)

    # Conflict surfaced from PayPal; no local success is fabricated. Phase
    # 11F-K.13: because PayPal reports the capture is already fully refunded,
    # the funds are returned, so the ledger is reconciled to the truthful
    # REFUNDED state (it is no longer left CAPTURED).
    assert resp.status_code == 409
    assert (
        transaction_service.get_transaction(txn.transaction_id).status
        == TransactionStatus.REFUNDED
    )


# --- 11-14. Client cannot influence the refund ----------------------------------


def test_client_supplied_refund_fields_are_ignored(creds, monkeypatch):
    """A body with a fake capture id, amount, and currency changes nothing:
    the refund still targets the STORED capture id with an empty body."""
    calls = _mock_refund(monkeypatch, REFUND_OK)
    txn = make_transaction()

    resp = refund(
        txn.transaction_id,
        json={
            "capture_id": "CAP-ATTACKER",
            "paypal_capture_id": "CAP-ATTACKER",
            "order_id": "ORDER-ATTACKER",
            "amount": "0.01",
            "currency": "EUR",
        },
    )

    assert resp.status_code == 200
    refund_call = calls[-1]
    assert refund_call["url"].endswith("/v2/payments/captures/CAP-1/refund")
    assert refund_call["json"] == {}  # no amount/currency forwarded
    body = resp.json()
    assert body["paypal_capture_id"] == "CAP-1"
    assert body["amount"] == "12.00"  # from PayPal's response, not the client
    assert body["currency"] == "USD"


def test_refund_uses_stored_capture_id(creds, monkeypatch):
    calls = _mock_refund(monkeypatch, REFUND_OK)
    txn = make_transaction(capture_id="CAP-STORED-77")

    resp = refund(txn.transaction_id)

    assert resp.status_code == 200
    assert calls[-1]["url"].endswith("/v2/payments/captures/CAP-STORED-77/refund")


# --- 15/16 are enforced by the autouse _no_ai_or_policy fixture on every test ---


# --- 17. No credentials in responses/logs ------------------------------------------


def test_no_credentials_in_refund_response_or_logs(creds, monkeypatch, caplog):
    _mock_refund(monkeypatch, REFUND_OK)
    txn = make_transaction()

    with caplog.at_level(logging.DEBUG):
        resp = refund(txn.transaction_id)

    for text in (resp.text, caplog.text):
        assert "test-client-secret" not in text
        assert "test-client-id" not in text
        assert "fake-token" not in text


# --- Regression: the capture-level refund endpoint still refunds a real capture ----
#
# Phase 11F-B0 made this route resolve the capture id to its owning
# transaction and apply the SAME deterministic eligibility rules as the
# transaction-level route. For the one case that was ever legitimate — a
# capture this server stored on a CAPTURED transaction — it still refunds.
# Its refusals for unowned/ineligible captures are in
# test_capture_refund_ownership.py.


def test_paypal_level_refund_endpoint_still_works(creds, monkeypatch):
    calls = _mock_refund(monkeypatch, REFUND_OK)
    txn = make_transaction(capture_id="CAP-1")

    resp = client.post("/paypal/refund/CAP-1")

    assert resp.status_code == 200
    body = resp.json()
    assert body["refund_id"] == "REF-123"
    assert body["transaction_id"] == txn.transaction_id
    # The STORED capture id is what reached PayPal.
    assert calls[-1]["url"].endswith("/v2/payments/captures/CAP-1/refund")
    # And the ledger moved, exactly as through the transaction-level route.
    assert (
        transaction_service.get_transaction(txn.transaction_id).status
        == TransactionStatus.REFUNDED
    )
