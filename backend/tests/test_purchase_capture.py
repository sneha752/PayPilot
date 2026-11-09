"""Tests for the purchase-level capture layer (POST /purchase/capture/{id}).

The PayPal layer is mocked at the purchase-service boundary, so no real
PayPal call is made. Also re-verifies that the surrounding purchase flow
(/purchase/execute, /purchase/approve, /paypal/capture-order) is unchanged.

Phase 11F-B0: capture is ownership-gated, so every test that expects to
reach PayPal first seeds the catalog ledger row that OWNS the order id (the
`owned_order` fixture). Refusal behavior for unowned ids lives in
test_capture_refund_ownership.py.
"""

import json

import pytest
from starlette.testclient import TestClient

from app import paypal
from app.main import app
from app.services import purchase_execution_service, transaction_service

client = TestClient(app, raise_server_exceptions=False)

CAPTURE_OK = {
    "order_id": "ORDER7B",
    "status": "COMPLETED",
    "capture_id": "CAP7B",
    "capture_status": "COMPLETED",
    "amount": "66.66",
    "currency": "USD",
}


@pytest.fixture
def owned_order():
    """A catalog ledger row owning ORDER7B — the capture gate's precondition.

    Written directly through the ledger service rather than by running
    /purchase/execute, so these tests keep exercising the capture layer
    alone.
    """
    return transaction_service.create_transaction(
        product_id="TAB003",
        product_name="Demo Tablet",
        catalog_price_inr=44900,
        paypal_amount="66.66",
        paypal_currency="USD",
        paypal_order_id="ORDER7B",
    )


@pytest.fixture
def capture_spy(monkeypatch):
    """Replace paypal.capture_order at the purchase-service boundary."""
    calls: list[str] = []

    def fake_capture(order_id):
        calls.append(order_id)
        return dict(CAPTURE_OK)

    monkeypatch.setattr(purchase_execution_service, "capture_order", fake_capture)
    return calls


def _mock_capture_error(monkeypatch, exc: Exception):
    def fake_capture(order_id):
        raise exc

    monkeypatch.setattr(purchase_execution_service, "capture_order", fake_capture)


# --- 1. Valid approved order -> capture succeeds --------------------------------


def test_capture_success(owned_order, capture_spy):
    resp = client.post("/purchase/capture/ORDER7B")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "status": "captured",
        "order_id": "ORDER7B",
        "order_status": "COMPLETED",
        "capture_id": "CAP7B",
        "capture_status": "COMPLETED",
        "amount": "66.66",
        "currency": "USD",
        # The owning ledger row moved to CAPTURED only after PayPal's
        # successful capture response was in hand.
        "transaction_id": owned_order.transaction_id,
        "transaction_status": "CAPTURED",
    }
    assert capture_spy == ["ORDER7B"]  # exactly one PayPal capture call


# --- 2. Not approved -> 409, no false success ------------------------------------


def test_not_approved_returns_409(owned_order, monkeypatch):
    _mock_capture_error(
        monkeypatch, paypal.PayPalOrderNotApprovedError("not approved yet")
    )
    resp = client.post("/purchase/capture/ORDER7B")
    assert resp.status_code == 409
    assert "captured" not in json.dumps(resp.json()).lower().replace(
        "not approved", ""
    )


# --- 3. Already captured -> conflict, no false success -----------------------------


def test_double_capture_returns_409(owned_order, monkeypatch):
    _mock_capture_error(
        monkeypatch, paypal.PayPalAlreadyCapturedError("already captured")
    )
    resp = client.post("/purchase/capture/ORDER7B")
    assert resp.status_code == 409
    assert "capture_id" not in resp.json()


# --- 4. Order not found -> 404 -------------------------------------------------------


def test_unknown_order_returns_404(monkeypatch):
    _mock_capture_error(monkeypatch, paypal.PayPalOrderNotFoundError("no such order"))
    assert client.post("/purchase/capture/NOPE").status_code == 404


# --- 5. Upstream / auth failure -> 502 --------------------------------------------------


def test_upstream_failure_returns_502(owned_order, monkeypatch):
    _mock_capture_error(monkeypatch, paypal.PayPalCaptureError("HTTP 500"))
    assert client.post("/purchase/capture/ORDER7B").status_code == 502


def test_auth_failure_returns_502(owned_order, monkeypatch):
    _mock_capture_error(monkeypatch, paypal.PayPalAuthError("no token"))
    assert client.post("/purchase/capture/ORDER7B").status_code == 502


# --- 6. Blank order id -> 422, no PayPal HTTP call ----------------------------------------


def test_blank_order_id_rejected(monkeypatch):
    # Real validation path (paypal.capture_order), with HTTP exploded to
    # prove nothing is sent.
    def explode(*a, **kw):
        raise AssertionError("No HTTP call may happen for a blank order id")

    monkeypatch.setattr(paypal.httpx, "post", explode)
    assert client.post("/purchase/capture/%20").status_code == 422


# --- 7. Client cannot influence the capture ------------------------------------------------


def test_request_body_is_ignored(owned_order, capture_spy):
    # Amount/currency/product/decision in a body must change nothing:
    # the endpoint takes only the order id from the path.
    resp = client.post(
        "/purchase/capture/ORDER7B",
        json={
            "amount": "0.01",
            "currency": "EUR",
            "product_id": "LAP001",
            "price_inr": 1,
            "decision": "ALLOW_AUTONOMOUS_PURCHASE",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["amount"] == "66.66"      # PayPal's values, untouched
    assert body["currency"] == "USD"
    assert capture_spy == ["ORDER7B"]


# --- 8/9. Existing purchase flow unchanged ---------------------------------------------------


AUTONOMOUS_50K = {
    "purchase_requested": True,
    "autonomous_purchase_allowed": True,
    "auto_approve_below_inr": 50000,
}


@pytest.fixture
def create_order_spy(monkeypatch):
    calls: list[dict] = []

    def fake_create_order(amount, currency="USD", description=None, request_id=None):
        calls.append({"amount": amount, "currency": currency})
        return {
            "order_id": "FAKE-ORDER-7B",
            "status": "CREATED",
            "approve_url": "https://www.sandbox.paypal.com/checkoutnow?token=FAKE-ORDER-7B",
        }

    monkeypatch.setattr(purchase_execution_service, "create_order", fake_create_order)
    return calls


def test_execute_still_works_and_does_not_capture(create_order_spy, capture_spy):
    resp = client.post(
        "/purchase/execute",
        json={"product_id": "TAB003", "authorization": AUTONOMOUS_50K},
    )
    body = resp.json()
    assert body["status"] == "order_created"
    assert body["paypal_order_id"] == "FAKE-ORDER-7B"
    assert body["approve_url"]
    assert body["paypal_amount"] == "498.89"
    assert body["demo_fx_rate"] == "90"
    # The order stays CREATED: creating never auto-captures.
    assert capture_spy == []


def test_approve_still_works_and_does_not_capture(create_order_spy, capture_spy):
    resp = client.post(
        "/purchase/approve",
        json={
            "product_id": "LAP002",
            "authorization": AUTONOMOUS_50K,
            "hard_constraints": {"max_price_inr": 80000},
        },
    )
    body = resp.json()
    assert body["status"] == "order_created"
    assert body["approval_used"] is True
    assert body["paypal_amount"] == "766.56"
    assert capture_spy == []              # approval creates, never captures


def test_paypal_level_capture_endpoint_still_works(owned_order, monkeypatch):
    # The PayPal-level endpoint must keep working alongside the new one.
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "test-client-secret")

    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    def fake_post(url, **kwargs):
        if url.endswith("/v1/oauth2/token"):
            return FakeResponse(200, {"access_token": "fake-token"})
        return FakeResponse(201, {
            "id": "ORDER7B", "status": "COMPLETED",
            "purchase_units": [{"payments": {"captures": [
                {"id": "CAP7B", "status": "COMPLETED",
                 "amount": {"currency_code": "USD", "value": "66.66"}}
            ]}}],
        })

    monkeypatch.setattr(paypal.httpx, "post", fake_post)
    resp = client.post("/paypal/capture-order/ORDER7B")
    assert resp.status_code == 200
    assert resp.json()["capture_id"] == "CAP7B"


# --- 10/11/12. Hygiene ----------------------------------------------------------------------


def test_capture_needs_no_ai_provider(owned_order, capture_spy, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("AI_PROVIDER", raising=False)
    assert client.post("/purchase/capture/ORDER7B").status_code == 200


def test_no_credentials_in_capture_response(owned_order, capture_spy):
    text = json.dumps(client.post("/purchase/capture/ORDER7B").json()).lower()
    for secret in ("client_secret", "access_token", "bearer", "client_id"):
        assert secret not in text
