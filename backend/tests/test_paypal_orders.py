"""Tests for the PayPal create-order request payload (HTTP fully mocked).

httpx.post is monkeypatched so NO real PayPal call is ever made: the fake
routes the OAuth token request and the create-order request to canned
responses while recording the exact JSON body sent, which is what these
tests assert on.
"""

import pytest

from app import paypal


class FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


ORDER_RESPONSE = {
    "id": "ORDER123",
    "status": "CREATED",
    "links": [
        {"rel": "self", "href": "https://api-m.sandbox.paypal.com/v2/checkout/orders/ORDER123"},
        {"rel": "approve", "href": "https://www.sandbox.paypal.com/checkoutnow?token=ORDER123"},
    ],
}


@pytest.fixture
def paypal_http(monkeypatch):
    """Mock httpx.post inside the paypal module; returns the recorded calls."""
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("PAYPAL_ENVIRONMENT", "sandbox")

    calls: list[dict] = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        if url.endswith("/v1/oauth2/token"):
            return FakeResponse(200, {"access_token": "fake-token", "token_type": "Bearer", "expires_in": 32400})
        if url.endswith("/v2/checkout/orders"):
            return FakeResponse(201, ORDER_RESPONSE)
        raise AssertionError(f"Unexpected PayPal URL called: {url}")

    monkeypatch.setattr(paypal.httpx, "post", fake_post)
    return calls


def _order_call(calls) -> dict:
    return next(c for c in calls if c["url"].endswith("/v2/checkout/orders"))


def test_create_order_10_usd_payload(paypal_http):
    result = paypal.create_order(amount="10.00", currency="USD")

    body = _order_call(paypal_http)["json"]
    assert body["intent"] == "CAPTURE"
    assert body["purchase_units"] == [
        {"amount": {"currency_code": "USD", "value": "10.00"}}
    ]
    assert result["order_id"] == "ORDER123"
    assert result["status"] == "CREATED"
    assert "checkoutnow" in result["approve_url"]


def test_create_order_766_56_usd_payload(paypal_http):
    # The converted catalog price from Phase 6A: ₹68,990 -> $766.56.
    paypal.create_order(amount="766.56", currency="USD")

    body = _order_call(paypal_http)["json"]
    assert body["intent"] == "CAPTURE"
    amount = body["purchase_units"][0]["amount"]
    assert amount == {"currency_code": "USD", "value": "766.56"}


def test_create_order_with_description(paypal_http):
    paypal.create_order(
        amount="766.56", currency="USD", description="HP Pavilion 14 (LAP002)"
    )
    unit = _order_call(paypal_http)["json"]["purchase_units"][0]
    assert unit["description"] == "HP Pavilion 14 (LAP002)"
    assert unit["amount"] == {"currency_code": "USD", "value": "766.56"}


def test_create_order_without_description_omits_field(paypal_http):
    paypal.create_order(amount="10.00", currency="USD")
    unit = _order_call(paypal_http)["json"]["purchase_units"][0]
    assert "description" not in unit


def test_description_truncated_to_127_chars(paypal_http):
    paypal.create_order(amount="10.00", currency="USD", description="x" * 500)
    unit = _order_call(paypal_http)["json"]["purchase_units"][0]
    assert len(unit["description"]) == 127


def test_create_order_keeps_checkout_context(paypal_http):
    # The existing sandbox behavior must be preserved.
    paypal.create_order(amount="10.00", currency="USD")
    body = _order_call(paypal_http)["json"]
    ctx = body["application_context"]
    assert ctx["brand_name"] == "PayPilot"
    assert ctx["shipping_preference"] == "NO_SHIPPING"
    assert ctx["user_action"] == "PAY_NOW"
    assert ctx["return_url"] and ctx["cancel_url"]


def test_create_order_uses_oauth_token(paypal_http):
    paypal.create_order(amount="10.00", currency="USD")
    # First the token call, then the order call carrying the Bearer token.
    assert paypal_http[0]["url"].endswith("/v1/oauth2/token")
    order_headers = _order_call(paypal_http)["headers"]
    assert order_headers["Authorization"] == "Bearer fake-token"


def test_create_order_error_status_raises(paypal_http, monkeypatch):
    def fake_post(url, **kwargs):
        if url.endswith("/v1/oauth2/token"):
            return FakeResponse(200, {"access_token": "fake-token"})
        return FakeResponse(422, {"name": "UNPROCESSABLE_ENTITY", "message": "bad amount"})

    monkeypatch.setattr(paypal.httpx, "post", fake_post)
    with pytest.raises(paypal.PayPalOrderError):
        paypal.create_order(amount="-1.00", currency="USD")
