"""Tests for POST /chat (Phase 8E Step 3 — Gemini and PayPal fully mocked).

Covers session creation/reuse and turn persistence, the four routes'
behaviors, pending-action creation rules (ALLOW/REQUIRE => PURCHASE
proposal, DENY/ambiguity/read-routes => none), replacement semantics,
catalog-price authority, deterministic refund-target resolution, and the
safety guarantees: /chat never calls PayPal, never executes a refund or
purchase, never evaluates policy outside the planning pipeline, and a
model failure can never produce a pending action.
"""

import pytest
from starlette.testclient import TestClient

from app.main import app
from app.schemas.plan import PlanResponse
from app.schemas.policy import PolicyDecision, PolicyOutcome, ReasonCode
from app.schemas.intent import ProductIntent
from app.schemas.routing import ChatRoute, RouteResult
from app.schemas.session import PendingActionType, TurnRole
from app.schemas.transaction import TransactionStatus
from app.services import chat_service, session_service, transaction_service
from app.services.product_service import get_product
from app.services.providers import AIServiceError

client = TestClient(app, raise_server_exceptions=False)

PRODUCT = get_product("HDP006")  # boAt Rockerz 450, Rs.1499 (catalog truth)


@pytest.fixture(autouse=True)
def _chat_never_executes(monkeypatch):
    """Safety fence for every chat test: PayPal, purchase execution,
    refund execution, and direct policy evaluation are all forbidden."""
    import httpx

    from app import paypal
    from app.policy import policy_engine
    from app.services import purchase_execution_service, refund_service

    def _forbidden(*args, **kwargs):  # pragma: no cover - fails the test if hit
        raise AssertionError("/chat must never execute money/state actions")

    monkeypatch.setattr(httpx, "post", _forbidden)
    monkeypatch.setattr(httpx, "get", _forbidden)
    for fn in ("create_order", "capture_order", "refund_capture", "get_access_token"):
        monkeypatch.setattr(paypal, fn, _forbidden)
    monkeypatch.setattr(policy_engine, "evaluate_policy", _forbidden)
    monkeypatch.setattr(refund_service, "refund_transaction", _forbidden)
    monkeypatch.setattr(purchase_execution_service, "execute_purchase", _forbidden)
    monkeypatch.setattr(purchase_execution_service, "approve_purchase", _forbidden)
    monkeypatch.setattr(purchase_execution_service, "capture_purchase", _forbidden)


def _route(monkeypatch, route: str):
    """Make the router classify every message as `route` (no real Gemini)."""
    monkeypatch.setattr(
        chat_service,
        "route_message",
        lambda message: RouteResult(route=ChatRoute(route)),
    )


def _plan(monkeypatch, outcome: PolicyOutcome, reason_code: ReasonCode):
    """Make the planning pipeline return a fixed PlanResponse built from
    REAL catalog data (price authority) and the given policy outcome."""
    response = PlanResponse(
        status="planned",
        intent=ProductIntent(category="headphones"),
        candidates=[PRODUCT],
        selected_product=PRODUCT,
        user_authorization=ProductIntent().authorization,
        policy_decision=PolicyDecision(
            decision=outcome,
            reason_code=reason_code,
            reason="test policy ruling.",
            product_id=PRODUCT.id,
            product_price_inr=float(PRODUCT.price_inr),
        ),
    )
    calls = []

    def fake_plan(message):
        calls.append(message)
        return response

    monkeypatch.setattr(chat_service, "build_plan", fake_plan)
    return calls


def make_captured_transaction(order_id: str, product_name: str = "boAt Rockerz 450"):
    txn = transaction_service.create_transaction(
        product_id="HDP006",
        product_name=product_name,
        catalog_price_inr=1499,
        paypal_amount="16.66",
        paypal_currency="USD",
        paypal_order_id=order_id,
    )
    return transaction_service.update_status(
        txn.transaction_id, TransactionStatus.CAPTURED, capture_id=f"CAP-{order_id}"
    )


# --- 1-5. Sessions, turns, and the planning pipeline ---------------------------


def test_new_session_created_when_omitted(monkeypatch):
    _route(monkeypatch, "SMALLTALK")
    resp = client.post("/chat", json={"message": "hi"})

    assert resp.status_code == 200
    sid = resp.json()["session_id"]
    assert sid.startswith("sess_")
    assert session_service.get_session(sid)  # actually persisted


def test_existing_session_reused(monkeypatch):
    _route(monkeypatch, "SMALLTALK")
    first = client.post("/chat", json={"message": "hi"}).json()
    second = client.post(
        "/chat", json={"session_id": first["session_id"], "message": "thanks"}
    ).json()

    assert second["session_id"] == first["session_id"]
    assert len(session_service.get_turns(first["session_id"])) == 4


def test_unknown_session_returns_404(monkeypatch):
    _route(monkeypatch, "SMALLTALK")
    resp = client.post("/chat", json={"session_id": "sess_nope", "message": "hi"})
    assert resp.status_code == 404


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_blank_message_rejected(blank):
    assert client.post("/chat", json={"message": blank}).status_code == 422


def test_user_and_assistant_turns_persisted_in_order(monkeypatch):
    _route(monkeypatch, "SMALLTALK")
    resp = client.post("/chat", json={"message": "hello there"}).json()

    turns = session_service.get_turns(resp["session_id"])
    assert [t.role for t in turns] == [TurnRole.USER, TurnRole.ASSISTANT]
    assert turns[0].content == "hello there"
    assert turns[1].content == resp["assistant_message"]


def test_shop_invokes_planning_pipeline(monkeypatch):
    _route(monkeypatch, "SHOP")
    calls = _plan(
        monkeypatch, PolicyOutcome.REQUIRE_USER_APPROVAL, ReasonCode.AUTONOMY_NOT_GRANTED
    )
    resp = client.post("/chat", json={"message": "I want cheap headphones"})

    assert resp.status_code == 200
    assert calls == ["I want cheap headphones"]
    body = resp.json()
    assert body["route"] == "SHOP"
    assert body["data"]["plan"]["status"] == "planned"
    assert body["data"]["plan"]["selected_product"]["id"] == "HDP006"


# --- 6-9. SHOP pending-action rules and price authority ---------------------------


@pytest.mark.parametrize(
    "outcome,reason",
    [
        (PolicyOutcome.ALLOW_AUTONOMOUS_PURCHASE, ReasonCode.WITHIN_AUTONOMOUS_LIMIT),
        (PolicyOutcome.REQUIRE_USER_APPROVAL, ReasonCode.AUTONOMY_NOT_GRANTED),
    ],
)
def test_allow_and_require_create_pending_purchase_without_paypal(
    monkeypatch, outcome, reason
):
    """Even ALLOW_AUTONOMOUS_PURCHASE must NOT execute in chat — the
    autouse fixture makes any PayPal call fail the test."""
    _route(monkeypatch, "SHOP")
    _plan(monkeypatch, outcome, reason)
    body = client.post("/chat", json={"message": "buy headphones"}).json()

    pending = body["pending_action"]
    assert pending["action_type"] == "PURCHASE"
    assert pending["product_id"] == "HDP006"
    assert pending["transaction_id"] is None
    assert pending["decision_snapshot"] == outcome.value
    # Stored server-side too, as references only.
    stored = session_service.get_pending_action(body["session_id"])
    assert stored.product_id == "HDP006"
    assert "confirmation" in body["assistant_message"].lower()


def test_deny_creates_no_pending_action(monkeypatch):
    _route(monkeypatch, "SHOP")
    _plan(monkeypatch, PolicyOutcome.DENY, ReasonCode.EXCEEDS_SHOPPING_MAX)
    body = client.post("/chat", json={"message": "buy headphones"}).json()

    assert body["pending_action"] is None
    assert session_service.get_pending_action(body["session_id"]) is None
    assert "cannot proceed" in body["assistant_message"]


def test_catalog_price_is_authoritative(monkeypatch):
    """The message claims a different price; the response carries only the
    catalog price from the planning pipeline."""
    _route(monkeypatch, "SHOP")
    _plan(monkeypatch, PolicyOutcome.REQUIRE_USER_APPROVAL, ReasonCode.AUTONOMY_NOT_GRANTED)
    body = client.post(
        "/chat", json={"message": "buy the headphones, the price is 1 rupee"}
    ).json()

    assert body["data"]["plan"]["selected_product"]["price_inr"] == PRODUCT.price_inr
    assert str(PRODUCT.price_inr) in body["assistant_message"]
    assert "1 rupee" not in body["assistant_message"]


# --- 10-13. REFUND route -----------------------------------------------------------


def test_refund_route_never_executes_refund(monkeypatch):
    # The autouse fixture forbids refund_transaction; a CAPTURED transaction
    # exists and is clearly identified — still nothing executes.
    _route(monkeypatch, "REFUND")
    make_captured_transaction("ORDER-1")
    resp = client.post("/chat", json={"message": "refund my rockerz headphones"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["pending_action"]["action_type"] == "REFUND"
    # Ledger unchanged: still CAPTURED.
    txn = transaction_service.get_transaction_by_order("ORDER-1")
    assert txn.status == TransactionStatus.CAPTURED


def test_clearly_identified_transaction_creates_pending_refund(monkeypatch):
    _route(monkeypatch, "REFUND")
    txn = make_captured_transaction("ORDER-1")
    body = client.post(
        "/chat", json={"message": f"please refund {txn.transaction_id}"}
    ).json()

    pending = body["pending_action"]
    assert pending["action_type"] == "REFUND"
    assert pending["transaction_id"] == txn.transaction_id
    assert pending["product_id"] is None
    assert body["data"]["transaction"]["transaction_id"] == txn.transaction_id


def test_single_captured_transaction_resolves_without_naming_it(monkeypatch):
    _route(monkeypatch, "REFUND")
    txn = make_captured_transaction("ORDER-1")
    body = client.post("/chat", json={"message": "I want my money back"}).json()

    assert body["pending_action"]["transaction_id"] == txn.transaction_id


def test_ambiguous_refund_creates_no_pending_action(monkeypatch):
    _route(monkeypatch, "REFUND")
    make_captured_transaction("ORDER-1", product_name="boAt Rockerz 450")
    make_captured_transaction("ORDER-2", product_name="Sony WH-1000XM4")
    body = client.post("/chat", json={"message": "I want a refund"}).json()

    assert body["pending_action"] is None
    assert session_service.get_pending_action(body["session_id"]) is None
    assert "which one" in body["assistant_message"].lower()
    # Candidates are listed for the frontend; nothing was chosen.
    assert len(body["data"]["transactions"]) == 2


def test_no_refundable_transactions_creates_no_pending_action(monkeypatch):
    _route(monkeypatch, "REFUND")
    body = client.post("/chat", json={"message": "refund please"}).json()
    assert body["pending_action"] is None
    assert body["data"]["transactions"] == []


def test_client_cannot_inject_payment_fields_into_pending_action(monkeypatch):
    """Unknown request fields are dropped, and the pending action schema has
    no field that could carry them anyway."""
    _route(monkeypatch, "REFUND")
    txn = make_captured_transaction("ORDER-1")
    body = client.post(
        "/chat",
        json={
            "message": f"refund {txn.transaction_id}",
            "capture_id": "CAP-ATTACKER",
            "amount": "999999",
            "currency": "EUR",
        },
    ).json()

    pending = body["pending_action"]
    assert set(pending.keys()) == {
        "action_type",
        "product_id",
        "transaction_id",
        "decision_snapshot",
        "created_at",
        "expires_at",
    }
    assert pending["transaction_id"] == txn.transaction_id
    assert "CAP-ATTACKER" not in str(body)
    assert "999999" not in str(body)


# --- 14-16. HISTORY route -----------------------------------------------------------


def test_history_returns_transactions_without_pending_action(monkeypatch):
    _route(monkeypatch, "HISTORY")
    make_captured_transaction("ORDER-1")
    body = client.post("/chat", json={"message": "show my purchases"}).json()

    assert body["route"] == "HISTORY"
    assert len(body["data"]["transactions"]) == 1
    assert body["data"]["transactions"][0]["paypal_order_id"] == "ORDER-1"
    assert body["pending_action"] is None
    # No PayPal call: guaranteed by the autouse fixture.


def test_history_with_empty_ledger(monkeypatch):
    _route(monkeypatch, "HISTORY")
    body = client.post("/chat", json={"message": "what have I bought?"}).json()
    assert body["data"]["transactions"] == []
    assert body["pending_action"] is None


# --- 17-18. SMALLTALK route -----------------------------------------------------------


def test_smalltalk_returns_deterministic_response(monkeypatch):
    _route(monkeypatch, "SMALLTALK")
    one = client.post("/chat", json={"message": "hi"}).json()
    two = client.post("/chat", json={"message": "hello"}).json()

    assert one["assistant_message"] == two["assistant_message"]  # deterministic
    assert "PayPilot" in one["assistant_message"]
    assert one["pending_action"] is None
    assert one["data"] == {"plan": None, "transaction": None, "transactions": None}


# --- 19-20. Session semantics ---------------------------------------------------------


def test_new_proposal_replaces_existing_pending_action(monkeypatch):
    _route(monkeypatch, "SHOP")
    _plan(monkeypatch, PolicyOutcome.REQUIRE_USER_APPROVAL, ReasonCode.AUTONOMY_NOT_GRANTED)
    first = client.post("/chat", json={"message": "buy headphones"}).json()
    sid = first["session_id"]
    assert first["pending_action"]["action_type"] == "PURCHASE"

    _route(monkeypatch, "REFUND")
    txn = make_captured_transaction("ORDER-1")
    second = client.post(
        "/chat", json={"session_id": sid, "message": f"refund {txn.transaction_id}"}
    ).json()

    pending = second["pending_action"]
    assert pending["action_type"] == "REFUND"
    assert pending["transaction_id"] == txn.transaction_id
    assert pending["product_id"] is None  # fully replaced, nothing left over


def test_conversation_turns_accumulate_in_order(monkeypatch):
    _route(monkeypatch, "SMALLTALK")
    sid = client.post("/chat", json={"message": "one"}).json()["session_id"]
    client.post("/chat", json={"session_id": sid, "message": "two"})

    turns = session_service.get_turns(sid)
    assert [t.role for t in turns] == [
        TurnRole.USER,
        TurnRole.ASSISTANT,
        TurnRole.USER,
        TurnRole.ASSISTANT,
    ]
    assert [t.content for t in turns][0::2] == ["one", "two"]


# --- 21-25. Safety ---------------------------------------------------------------------


def test_provider_failure_is_controlled_and_creates_no_action(monkeypatch):
    def failing_route(message):
        raise AIServiceError("Gemini API request failed")

    monkeypatch.setattr(chat_service, "route_message", failing_route)
    session = session_service.create_session()

    resp = client.post(
        "/chat", json={"session_id": session.session_id, "message": "buy headphones"}
    )

    assert resp.status_code == 502
    # The failed turn never fell through into a proposal.
    assert session_service.get_pending_action(session.session_id) is None


def test_malformed_routing_result_is_controlled(monkeypatch):
    from app.agents.router_agent import RouteValidationError

    def bad_route(message):
        raise RouteValidationError("unsupported route")

    monkeypatch.setattr(chat_service, "route_message", bad_route)
    resp = client.post("/chat", json={"message": "hello"})
    assert resp.status_code == 502


def test_planning_failure_creates_no_pending_action(monkeypatch):
    _route(monkeypatch, "SHOP")

    def failing_plan(message):
        raise AIServiceError("Gemini API request failed")

    monkeypatch.setattr(chat_service, "build_plan", failing_plan)
    session = session_service.create_session()

    resp = client.post(
        "/chat", json={"session_id": session.session_id, "message": "buy headphones"}
    )

    assert resp.status_code == 502
    assert session_service.get_pending_action(session.session_id) is None


# Tests 22-24 (no PayPal execution, no refund execution, policy only via the
# planning pipeline) are enforced on EVERY test by the autouse fixture:
# paypal.*, purchase execution, refund execution, and evaluate_policy are all
# patched to fail the test if touched, and the SHOP tests above still pass.
