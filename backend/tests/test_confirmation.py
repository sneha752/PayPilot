"""Tests for POST /sessions/{session_id}/confirm (Phase 8E Step 4).

PayPal is mocked at the HTTP boundary; Gemini is forbidden entirely.
Covers the general confirm flow, purchase confirmation through the
existing approval path (catalog re-fetch, policy re-run, DENY blocks
PayPal), refund confirmation through the existing refund service,
pending-action lifecycle (consume on success/terminal, retain on
transient upstream failure), double-confirmation safety, and
anti-tampering (the body can only say yes or no).
"""

import pytest
from starlette.testclient import TestClient

from app import paypal
from app.main import app
from app.schemas.session import PendingActionType
from app.schemas.transaction import TransactionStatus
from app.services import (
    purchase_execution_service,
    session_service,
    transaction_service,
)
from app.services.product_service import get_product

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
ORDER_OK = FakeResponse(
    201,
    {
        "id": "ORDER-NEW-1",
        "status": "CREATED",
        "links": [{"rel": "approve", "href": "https://sandbox.paypal.com/approve"}],
    },
)
REFUND_OK = FakeResponse(
    201,
    {
        "id": "REF-77",
        "status": "COMPLETED",
        "amount": {"value": "16.66", "currency_code": "USD"},
    },
)


@pytest.fixture(autouse=True)
def _no_gemini(monkeypatch):
    """Test 25: confirmation is fully deterministic — any AI/provider call
    fails the test."""
    from app.agents import router_agent
    from app.services import providers
    from app.services.providers import gemini

    def _forbidden(*args, **kwargs):  # pragma: no cover - fails the test if hit
        raise AssertionError("Confirmation must not call Gemini or any AI provider")

    monkeypatch.setattr(gemini, "GeminiProvider", _forbidden)
    monkeypatch.setattr(providers, "get_provider", _forbidden)
    monkeypatch.setattr(router_agent, "route_message", _forbidden)


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("PAYPAL_ENVIRONMENT", "sandbox")


def _mock_paypal(monkeypatch, order_response=ORDER_OK, refund_response=REFUND_OK):
    """Route token/create-order/refund calls; returns the recorded calls."""
    calls: list[dict] = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        if url.endswith("/v1/oauth2/token"):
            return TOKEN_OK
        if url.endswith("/v2/checkout/orders"):
            if isinstance(order_response, Exception):
                raise order_response
            return order_response
        if "/v2/payments/captures/" in url and url.endswith("/refund"):
            if isinstance(refund_response, Exception):
                raise refund_response
            return refund_response
        raise AssertionError(f"Unexpected PayPal URL called: {url}")

    monkeypatch.setattr(paypal.httpx, "post", fake_post)
    return calls


def _order_calls(calls):
    return [c for c in calls if c["url"].endswith("/v2/checkout/orders")]


def _refund_calls(calls):
    return [c for c in calls if c["url"].endswith("/refund")]


def session_with_pending_purchase(product_id="HDP006", ttl=None):
    session = session_service.create_session()
    session_service.set_pending_action(
        session.session_id,
        PendingActionType.PURCHASE,
        product_id=product_id,
        decision_snapshot="REQUIRE_USER_APPROVAL",
        ttl_seconds=ttl,
    )
    return session


def session_with_pending_refund(ttl=None):
    txn = transaction_service.create_transaction(
        product_id="HDP006",
        product_name="boAt Rockerz 450",
        catalog_price_inr=1499,
        paypal_amount="16.66",
        paypal_currency="USD",
        paypal_order_id="ORDER-OLD-1",
    )
    txn = transaction_service.update_status(
        txn.transaction_id, TransactionStatus.CAPTURED, capture_id="CAP-1"
    )
    session = session_service.create_session()
    session_service.set_pending_action(
        session.session_id,
        PendingActionType.REFUND,
        transaction_id=txn.transaction_id,
        decision_snapshot="CAPTURED",
        ttl_seconds=ttl,
    )
    return session, txn


def confirm(session_id, body=None):
    return client.post(f"/sessions/{session_id}/confirm", json=body or {"confirm": True})


# --- 1-6. General -----------------------------------------------------------------


def test_confirm_true_executes_active_pending_purchase(creds, monkeypatch):
    calls = _mock_paypal(monkeypatch)
    session = session_with_pending_purchase()

    resp = confirm(session.session_id)

    assert resp.status_code == 200
    body = resp.json()
    assert body["confirmed"] is True
    assert body["executed"] is True
    assert body["action_type"] == "PURCHASE"
    assert body["purchase_result"]["status"] == "order_created"
    assert body["purchase_result"]["approval_used"] is True
    assert len(_order_calls(calls)) == 1


def test_unknown_session_returns_404(creds, monkeypatch):
    calls = _mock_paypal(monkeypatch)
    assert confirm("sess_does_not_exist").status_code == 404
    assert calls == []


def test_no_pending_action_returns_409(creds, monkeypatch):
    calls = _mock_paypal(monkeypatch)
    session = session_service.create_session()
    resp = confirm(session.session_id)
    assert resp.status_code == 409
    assert calls == []


def test_confirm_false_executes_nothing_and_keeps_action(creds, monkeypatch):
    calls = _mock_paypal(monkeypatch)
    session = session_with_pending_purchase()

    resp = confirm(session.session_id, {"confirm": False})

    assert resp.status_code == 200
    body = resp.json()
    assert body["confirmed"] is False
    assert body["executed"] is False
    assert body["pending_action"]["product_id"] == "HDP006"
    assert calls == []  # no PayPal interaction of any kind
    # The proposal survives and can still be confirmed later.
    assert session_service.get_pending_action(session.session_id) is not None


def test_missing_confirm_field_is_rejected(creds, monkeypatch):
    """The boolean is REQUIRED: merely calling the endpoint never executes."""
    calls = _mock_paypal(monkeypatch)
    session = session_with_pending_purchase()
    resp = client.post(f"/sessions/{session.session_id}/confirm", json={})
    assert resp.status_code == 422
    assert calls == []


def test_expired_pending_action_cannot_execute(creds, monkeypatch):
    calls = _mock_paypal(monkeypatch)
    session = session_with_pending_purchase(ttl=-1)  # expired at creation

    resp = confirm(session.session_id)

    assert resp.status_code == 409
    assert calls == []  # expired => PayPal untouched, nothing revived


def test_successful_confirmation_clears_pending_action(creds, monkeypatch):
    _mock_paypal(monkeypatch)
    session = session_with_pending_purchase()
    confirm(session.session_id)
    assert session_service.get_pending_action(session.session_id) is None


# --- 7-15. Purchase ------------------------------------------------------------------


def test_purchase_goes_through_existing_approval_service(creds, monkeypatch):
    _mock_paypal(monkeypatch)
    session = session_with_pending_purchase()

    body = confirm(session.session_id).json()
    result = body["purchase_result"]

    # Full existing ExecutionResult shape: catalog price, policy decision,
    # ledger transaction — exactly what /purchase/approve produces.
    assert result["product_id"] == "HDP006"
    assert result["catalog_price_inr"] == 1499
    assert result["paypal_amount"] == "16.66"
    assert result["paypal_order_id"] == "ORDER-NEW-1"
    assert result["policy_decision"]["decision"] == "REQUIRE_USER_APPROVAL"
    txn = transaction_service.get_transaction(result["transaction_id"])
    assert txn.status == TransactionStatus.ORDER_CREATED


def test_current_catalog_price_is_refetched(creds, monkeypatch):
    """The stored action references only product_id; the executed amount
    follows the CURRENT catalog price, not anything from proposal time."""
    calls = _mock_paypal(monkeypatch)
    session = session_with_pending_purchase()

    current = get_product("HDP006").model_copy(update={"price_inr": 2999})
    monkeypatch.setattr(
        purchase_execution_service, "get_product", lambda pid: current
    )

    body = confirm(session.session_id).json()

    assert body["purchase_result"]["catalog_price_inr"] == 2999
    assert body["purchase_result"]["paypal_amount"] == "33.32"  # 2999/90
    sent = _order_calls(calls)[0]["json"]
    assert sent["purchase_units"][0]["amount"]["value"] == "33.32"


def test_policy_is_reevaluated_and_deny_blocks_paypal(creds, monkeypatch):
    """A product whose CURRENT state fails policy (invalid price) is denied
    at confirmation time — the stored snapshot does not carry authority."""
    calls = _mock_paypal(monkeypatch)
    session = session_with_pending_purchase()

    broken = get_product("HDP006").model_copy(update={"price_inr": 0})
    monkeypatch.setattr(
        purchase_execution_service, "get_product", lambda pid: broken
    )

    resp = confirm(session.session_id)

    assert resp.status_code == 200
    body = resp.json()
    assert body["executed"] is False
    assert body["purchase_result"]["status"] == "denied"
    assert (
        body["purchase_result"]["policy_decision"]["reason_code"]
        == "INVALID_PRODUCT_PRICE"
    )
    assert _order_calls(calls) == []  # DENY => PayPal never called
    # The dead proposal is consumed.
    assert session_service.get_pending_action(session.session_id) is None


def test_client_cannot_override_action_through_body(creds, monkeypatch):
    """Tests 10-13 + 26-27: product/price/currency/PayPal ids in the body
    are dropped; the stored pending action is the sole action source."""
    calls = _mock_paypal(monkeypatch)
    session = session_with_pending_purchase(product_id="HDP006")

    resp = confirm(
        session.session_id,
        {
            "confirm": True,
            "product_id": "LAP001",
            "transaction_id": "txn_attacker",
            "price": 1,
            "amount": "0.01",
            "currency": "EUR",
            "paypal_order_id": "ORDER-ATTACKER",
            "capture_id": "CAP-ATTACKER",
            "policy_decision": "ALLOW_AUTONOMOUS_PURCHASE",
        },
    )

    assert resp.status_code == 200
    result = resp.json()["purchase_result"]
    assert result["product_id"] == "HDP006"  # stored reference won
    assert result["paypal_amount"] == "16.66"  # catalog-derived
    assert result["paypal_currency"] == "USD"
    sent = _order_calls(calls)[0]["json"]
    assert sent["purchase_units"][0]["amount"] == {
        "currency_code": "USD",
        "value": "16.66",
    }
    assert "ORDER-ATTACKER" not in str(resp.json())


def test_ambiguous_paypal_order_failure_consumes_action(creds, monkeypatch):
    """Phase 11F-K.21: an HTTP 500 from create-order is a PayPalOrderError —
    an AMBIGUOUS outcome where PayPal may have created an order. The action is
    now CONSUMED (not retained), so a re-confirm is a 409 that issues no second
    create_order; the unresolved attempt is left for read-first reconciliation
    rather than a blind retry that could duplicate an order."""
    calls = _mock_paypal(
        monkeypatch, order_response=FakeResponse(500, {"name": "INTERNAL"})
    )
    session = session_with_pending_purchase()

    resp = confirm(session.session_id)

    assert resp.status_code == 502
    assert len(_order_calls(calls)) == 1
    # Consumed, not retained: no safe retry of an ambiguous order.
    assert session_service.get_pending_action(session.session_id) is None
    # A re-confirm finds no action and never issues a second create-order.
    calls2 = _mock_paypal(monkeypatch)
    assert confirm(session.session_id).status_code == 409
    assert len(_order_calls(calls2)) == 0


# --- 16-21. Refund --------------------------------------------------------------------


def test_refund_executes_through_existing_service(creds, monkeypatch):
    calls = _mock_paypal(monkeypatch)
    session, txn = session_with_pending_refund()

    resp = confirm(session.session_id)

    assert resp.status_code == 200
    body = resp.json()
    assert body["executed"] is True
    assert body["action_type"] == "REFUND"
    refund = body["refund_result"]
    assert refund["transaction_id"] == txn.transaction_id
    assert refund["refund_id"] == "REF-77"
    assert refund["amount"] == "16.66"
    # Existing service semantics: server-stored capture id, ledger REFUNDED.
    assert _refund_calls(calls)[0]["url"].endswith("/captures/CAP-1/refund")
    assert (
        transaction_service.get_transaction(txn.transaction_id).status
        == TransactionStatus.REFUNDED
    )


def test_refund_target_comes_only_from_stored_action(creds, monkeypatch):
    """Tests 18-19: transaction_id/capture_id in the body are ignored."""
    calls = _mock_paypal(monkeypatch)
    session, txn = session_with_pending_refund()
    other = transaction_service.create_transaction(
        product_id="p2",
        product_name="Other Product",
        catalog_price_inr=5000,
        paypal_amount="55.56",
        paypal_currency="USD",
        paypal_order_id="ORDER-OTHER",
    )
    transaction_service.update_status(
        other.transaction_id, TransactionStatus.CAPTURED, capture_id="CAP-OTHER"
    )

    resp = confirm(
        session.session_id,
        {
            "confirm": True,
            "transaction_id": other.transaction_id,
            "capture_id": "CAP-OTHER",
        },
    )

    assert resp.json()["refund_result"]["transaction_id"] == txn.transaction_id
    assert _refund_calls(calls)[0]["url"].endswith("/captures/CAP-1/refund")
    # The other transaction is untouched.
    assert (
        transaction_service.get_transaction(other.transaction_id).status
        == TransactionStatus.CAPTURED
    )


def test_already_refunded_returns_409_without_paypal(creds, monkeypatch):
    """Test 20: current ledger state wins over the stored snapshot."""
    calls = _mock_paypal(monkeypatch)
    session, txn = session_with_pending_refund()
    # The transaction gets refunded through some other path meanwhile.
    transaction_service.update_status(txn.transaction_id, TransactionStatus.REFUNDED)

    resp = confirm(session.session_id)

    assert resp.status_code == 409
    assert "already" in resp.json()["detail"].lower()
    assert calls == []  # no token, no refund — PayPal never contacted
    # Terminal: the action is consumed, a re-confirm is a clean 409.
    assert session_service.get_pending_action(session.session_id) is None


def test_refund_failure_does_not_fabricate_success(creds, monkeypatch):
    calls = _mock_paypal(
        monkeypatch, refund_response=FakeResponse(500, {"name": "INTERNAL"})
    )
    session, txn = session_with_pending_refund()

    resp = confirm(session.session_id)

    assert resp.status_code == 502
    assert (
        transaction_service.get_transaction(txn.transaction_id).status
        == TransactionStatus.CAPTURED
    )
    # Transient upstream failure: action retained for one safe explicit retry.
    assert session_service.get_pending_action(session.session_id) is not None
    assert len(_refund_calls(calls)) == 1


# --- 22-24. Duplication / retry ---------------------------------------------------------


def test_repeated_confirmation_cannot_create_second_purchase(creds, monkeypatch):
    calls = _mock_paypal(monkeypatch)
    session = session_with_pending_purchase()

    first = confirm(session.session_id)
    assert first.json()["executed"] is True

    # The response could have been lost; the user confirms again.
    second = confirm(session.session_id)

    assert second.status_code == 409  # action consumed — nothing to confirm
    assert len(_order_calls(calls)) == 1  # exactly ONE PayPal order ever


def test_repeated_confirmation_cannot_create_second_refund(creds, monkeypatch):
    calls = _mock_paypal(monkeypatch)
    session, txn = session_with_pending_refund()

    assert confirm(session.session_id).json()["executed"] is True
    second = confirm(session.session_id)

    assert second.status_code == 409
    assert len(_refund_calls(calls)) == 1
    assert (
        transaction_service.get_transaction(txn.transaction_id).status
        == TransactionStatus.REFUNDED
    )


# --- Session history -----------------------------------------------------------------


def test_result_recorded_as_assistant_turn_without_fabricated_user_turn(
    creds, monkeypatch
):
    _mock_paypal(monkeypatch)
    session = session_with_pending_purchase()

    message = confirm(session.session_id).json()["message"]

    turns = session_service.get_turns(session.session_id)
    assert len(turns) == 1  # only the assistant result — no fake user turn
    assert turns[0].role.value == "assistant"
    assert turns[0].content == message


# Test 25 (no Gemini) is enforced on EVERY test by the autouse fixture.
