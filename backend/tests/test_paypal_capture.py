"""Tests for PayPal order capture (HTTP fully mocked — no real PayPal calls).

Covers capture_order() parsing/validation, the distinct error types
(not found / not approved / already captured / upstream / auth / malformed),
the POST /paypal/capture-order/{order_id} endpoint mapping, and regression
locks proving get_order() and refund_capture() behavior is unchanged.

Phase 11F-B0: the ENDPOINT is ownership-gated (the paypal.capture_order
CLIENT is unchanged and is still exercised directly here). Endpoint tests
that expect to reach PayPal therefore seed the catalog ledger row owning the
order id via the `owned_order` fixture; the refusals for an unowned id live
in test_capture_refund_ownership.py.
"""

import json

import pytest
from starlette.testclient import TestClient

from app import paypal
from app.main import app
from app.services import transaction_service

client = TestClient(app, raise_server_exceptions=False)


class FakeResponse:
    def __init__(self, status_code: int, payload=None, raw_text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self._raw_text = raw_text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


TOKEN_OK = FakeResponse(200, {"access_token": "fake-token", "token_type": "Bearer"})

CAPTURE_OK = {
    "id": "ORDER123",
    "status": "COMPLETED",
    "purchase_units": [
        {
            "payments": {
                "captures": [
                    {
                        "id": "CAP123",
                        "status": "COMPLETED",
                        "amount": {"currency_code": "USD", "value": "16.66"},
                    }
                ]
            }
        }
    ],
}


@pytest.fixture
def owned_order():
    """A catalog ledger row owning ORDER123 — the capture gate's precondition."""
    return transaction_service.create_transaction(
        product_id="TAB003",
        product_name="Demo Tablet",
        catalog_price_inr=44900,
        paypal_amount="16.66",
        paypal_currency="USD",
        paypal_order_id="ORDER123",
    )


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("PAYPAL_ENVIRONMENT", "sandbox")


def _mock_capture(monkeypatch, capture_response: FakeResponse):
    """Route the token call to success and the capture call to the given
    response; returns the recorded calls."""
    calls: list[dict] = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        if url.endswith("/v1/oauth2/token"):
            return TOKEN_OK
        if "/v2/checkout/orders/" in url and url.endswith("/capture"):
            return capture_response
        raise AssertionError(f"Unexpected PayPal URL called: {url}")

    monkeypatch.setattr(paypal.httpx, "post", fake_post)
    return calls


# --- 1. Successful capture ------------------------------------------------------


def test_successful_capture_parses_response(creds, monkeypatch):
    calls = _mock_capture(monkeypatch, FakeResponse(201, CAPTURE_OK))
    result = paypal.capture_order("ORDER123")

    # Correct endpoint, method (httpx.post), order id, and auth header.
    capture_call = calls[-1]
    assert capture_call["url"].endswith("/v2/checkout/orders/ORDER123/capture")
    assert capture_call["headers"]["Authorization"] == "Bearer fake-token"

    assert result == {
        "order_id": "ORDER123",
        "status": "COMPLETED",
        "capture_id": "CAP123",
        "capture_status": "COMPLETED",
        "amount": "16.66",
        "currency": "USD",
    }


def test_capture_endpoint_success(creds, owned_order, monkeypatch):
    _mock_capture(monkeypatch, FakeResponse(201, CAPTURE_OK))
    resp = client.post("/paypal/capture-order/ORDER123")
    assert resp.status_code == 200
    body = resp.json()
    assert body["capture_id"] == "CAP123"
    assert body["capture_status"] == "COMPLETED"
    assert body["amount"] == "16.66"
    assert body["currency"] == "USD"


# --- 2. Order not found ------------------------------------------------------------


def test_order_not_found(creds, monkeypatch):
    _mock_capture(monkeypatch, FakeResponse(404, {"name": "RESOURCE_NOT_FOUND"}))
    with pytest.raises(paypal.PayPalOrderNotFoundError):
        paypal.capture_order("NOPE123")
    assert client.post("/paypal/capture-order/NOPE123").status_code == 404


# --- 3. Invalid state / already captured --------------------------------------------


def test_order_not_approved_yet(creds, owned_order, monkeypatch):
    _mock_capture(monkeypatch, FakeResponse(422, {
        "name": "UNPROCESSABLE_ENTITY",
        "message": "The requested action could not be performed.",
        "details": [{"issue": "ORDER_NOT_APPROVED"}],
    }))
    with pytest.raises(paypal.PayPalOrderNotApprovedError):
        paypal.capture_order("ORDER123")
    assert client.post("/paypal/capture-order/ORDER123").status_code == 409


def test_double_capture_is_not_a_success(creds, owned_order, monkeypatch):
    # A second capture of an already-captured order must surface PayPal's
    # error — never a fabricated local success.
    _mock_capture(monkeypatch, FakeResponse(422, {
        "name": "UNPROCESSABLE_ENTITY",
        "message": "The requested action could not be performed.",
        "details": [{"issue": "ORDER_ALREADY_CAPTURED"}],
    }))
    with pytest.raises(paypal.PayPalAlreadyCapturedError):
        paypal.capture_order("ORDER123")
    resp = client.post("/paypal/capture-order/ORDER123")
    assert resp.status_code == 409
    assert "capture_id" not in resp.json()


# --- 4. Upstream failure -------------------------------------------------------------


def test_upstream_500_surfaced(creds, owned_order, monkeypatch):
    _mock_capture(monkeypatch, FakeResponse(500, {"name": "INTERNAL_SERVER_ERROR"}))
    with pytest.raises(paypal.PayPalCaptureError):
        paypal.capture_order("ORDER123")
    assert client.post("/paypal/capture-order/ORDER123").status_code == 502


def test_network_error_surfaced(creds, monkeypatch):
    def fake_post(url, **kwargs):
        if url.endswith("/v1/oauth2/token"):
            return TOKEN_OK
        raise paypal.httpx.ConnectError("boom")

    monkeypatch.setattr(paypal.httpx, "post", fake_post)
    with pytest.raises(paypal.PayPalCaptureError):
        paypal.capture_order("ORDER123")


# --- 5. Authentication failure ----------------------------------------------------------


def test_auth_failure_surfaced(creds, owned_order, monkeypatch):
    monkeypatch.setattr(
        paypal.httpx, "post",
        lambda url, **kw: FakeResponse(401, {"error": "invalid_client"}),
    )
    with pytest.raises(paypal.PayPalAuthError):
        paypal.capture_order("ORDER123")
    assert client.post("/paypal/capture-order/ORDER123").status_code == 502


# --- 6. Malformed PayPal response ----------------------------------------------------------


def test_malformed_non_json_response(creds, monkeypatch):
    _mock_capture(monkeypatch, FakeResponse(201, payload=None, raw_text="<html>"))
    with pytest.raises(paypal.PayPalCaptureError):
        paypal.capture_order("ORDER123")


def test_malformed_empty_structures_degrade_safely(creds, monkeypatch):
    # Valid JSON but empty purchase_units: no crash, None capture fields.
    _mock_capture(monkeypatch, FakeResponse(
        201, {"id": "ORDER123", "status": "COMPLETED", "purchase_units": []}
    ))
    result = paypal.capture_order("ORDER123")
    assert result["status"] == "COMPLETED"
    assert result["capture_id"] is None
    assert result["amount"] is None


# --- 7. Empty/invalid order id ---------------------------------------------------------------


@pytest.mark.parametrize("bad_id", ["", "   "])
def test_blank_order_id_rejected_before_any_call(creds, monkeypatch, bad_id):
    def explode(*a, **kw):
        raise AssertionError("No HTTP call may happen for a blank order id")

    monkeypatch.setattr(paypal.httpx, "post", explode)
    with pytest.raises(ValueError):
        paypal.capture_order(bad_id)


def test_blank_order_id_endpoint_422(creds, monkeypatch):
    def explode(*a, **kw):
        raise AssertionError("No HTTP call may happen for a blank order id")

    monkeypatch.setattr(paypal.httpx, "post", explode)
    assert client.post("/paypal/capture-order/%20").status_code == 422


# --- 8. No credentials in API responses ------------------------------------------------------


def test_no_credentials_in_capture_response(creds, owned_order, monkeypatch):
    _mock_capture(monkeypatch, FakeResponse(201, CAPTURE_OK))
    text = json.dumps(client.post("/paypal/capture-order/ORDER123").json()).lower()
    for secret in ("client_secret", "access_token", "fake-token", "bearer", "client_id"):
        assert secret not in text


# --- 9/10/11. Existing behavior locked (create/get/refund unchanged) --------------------------


def test_get_order_behavior_unchanged(creds, monkeypatch):
    monkeypatch.setattr(
        paypal.httpx, "post", lambda url, **kw: TOKEN_OK  # token only
    )
    monkeypatch.setattr(
        paypal.httpx, "get",
        lambda url, **kw: FakeResponse(200, {
            "id": "ORDER123",
            "status": "COMPLETED",
            "intent": "CAPTURE",
            "purchase_units": [
                {"payments": {"captures": [
                    {"id": "CAP123", "status": "COMPLETED",
                     "amount": {"currency_code": "USD", "value": "16.66"}}
                ]}}
            ],
        }),
    )
    result = paypal.get_order("ORDER123")
    assert result["order_id"] == "ORDER123"
    assert result["order_status"] == "COMPLETED"
    assert result["captures"] == [
        {"id": "CAP123", "status": "COMPLETED",
         "amount": {"currency_code": "USD", "value": "16.66"}}
    ]


def test_refund_behavior_unchanged(creds, monkeypatch):
    def fake_post(url, **kwargs):
        if url.endswith("/v1/oauth2/token"):
            return TOKEN_OK
        assert url.endswith("/v2/payments/captures/CAP123/refund")
        return FakeResponse(201, {
            "id": "REF123", "status": "COMPLETED",
            "amount": {"currency_code": "USD", "value": "16.66"},
        })

    monkeypatch.setattr(paypal.httpx, "post", fake_post)
    result = paypal.refund_capture("CAP123")
    assert result == {
        "refund_id": "REF123",
        "refund_status": "COMPLETED",
        "refunded_amount": "16.66",
        "currency": "USD",
    }
