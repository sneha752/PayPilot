"""Tests for GET /sessions/{session_id} (Phase 8E Step 5 — strictly read-only).

Covers correct retrieval of identity/timestamps/turns/pending action,
deterministic chronological turn ordering, 404 for unknown sessions
without auto-creation, expired pending actions appearing as null without
being revived or extended, byte-level proof that retrieval mutates
nothing in the database, and guards ensuring no Gemini/PayPal/Policy
Engine call. Also locks that /chat and /confirm behavior is unchanged.
"""

import sqlite3

import pytest
from starlette.testclient import TestClient

from app.main import app
from app.schemas.routing import ChatRoute, RouteResult
from app.schemas.session import PendingActionType, TurnRole
from app.services import chat_service, session_service

client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _strictly_read_only(monkeypatch):
    """Tests 13-15: retrieval must never reach AI, PayPal/HTTP, or policy."""
    import httpx

    from app.policy import policy_engine
    from app.services.providers import gemini

    def _forbidden(*args, **kwargs):  # pragma: no cover - fails the test if hit
        raise AssertionError("Session retrieval must be strictly read-only")

    monkeypatch.setattr(httpx, "post", _forbidden)
    monkeypatch.setattr(httpx, "get", _forbidden)
    monkeypatch.setattr(policy_engine, "evaluate_policy", _forbidden)
    monkeypatch.setattr(gemini, "GeminiProvider", _forbidden)


def _dump_db() -> list:
    """Raw snapshot of sessions + turns tables for mutation checks."""
    conn = sqlite3.connect(session_service._db_path())
    try:
        sessions = conn.execute(
            "SELECT * FROM sessions ORDER BY session_id"
        ).fetchall()
        turns = conn.execute("SELECT * FROM session_turns ORDER BY seq").fetchall()
    finally:
        conn.close()
    return [sessions, turns]


def get_session_detail(session_id: str):
    return client.get(f"/sessions/{session_id}")


# --- 1-5. Retrieval and ordering --------------------------------------------------


def test_existing_session_returns_full_detail():
    session = session_service.create_session()
    session_service.append_turn(session.session_id, TurnRole.USER, "hello")
    session_service.append_turn(session.session_id, TurnRole.ASSISTANT, "hi there")

    resp = get_session_detail(session.session_id)

    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == session.session_id
    assert body["created_at"] == session.created_at
    assert len(body["turns"]) == 2
    assert body["pending_action"] is None


def test_unknown_session_returns_404_and_is_not_created():
    resp = get_session_detail("sess_does_not_exist")
    assert resp.status_code == 404
    # Still unknown afterwards — nothing was auto-created.
    with pytest.raises(session_service.SessionNotFoundError):
        session_service.get_session("sess_does_not_exist")


def test_empty_session_returns_empty_turns():
    session = session_service.create_session()
    body = get_session_detail(session.session_id).json()
    assert body["turns"] == []
    assert body["pending_action"] is None


def test_turns_chronological_with_roles_preserved(monkeypatch):
    # Identical timestamps: ordering must come from the store's monotonic
    # sequence, exactly as locked in Step 1 — never SQLite row order.
    monkeypatch.setattr(
        session_service, "_now", lambda: "2026-10-04T10:00:00+00:00"
    )
    session = session_service.create_session()
    expected = [
        (TurnRole.USER, "first"),
        (TurnRole.ASSISTANT, "second"),
        (TurnRole.USER, "third"),
        (TurnRole.ASSISTANT, "fourth"),
    ]
    for role, content in expected:
        session_service.append_turn(session.session_id, role, content)

    body = get_session_detail(session.session_id).json()

    assert [(t["role"], t["content"]) for t in body["turns"]] == [
        (r.value, c) for r, c in expected
    ]


# --- 6-8. Pending action ------------------------------------------------------------


def test_active_pending_purchase_is_returned():
    session = session_service.create_session()
    session_service.set_pending_action(
        session.session_id,
        PendingActionType.PURCHASE,
        product_id="HDP006",
        decision_snapshot="REQUIRE_USER_APPROVAL",
    )

    pending = get_session_detail(session.session_id).json()["pending_action"]

    assert pending["action_type"] == "PURCHASE"
    assert pending["product_id"] == "HDP006"
    assert pending["transaction_id"] is None
    # Only the safe reference fields — nothing payment-shaped.
    assert set(pending.keys()) == {
        "action_type",
        "product_id",
        "transaction_id",
        "decision_snapshot",
        "created_at",
        "expires_at",
    }


def test_active_pending_refund_is_returned():
    session = session_service.create_session()
    session_service.set_pending_action(
        session.session_id,
        PendingActionType.REFUND,
        transaction_id="txn_abc123",
        decision_snapshot="CAPTURED",
    )

    pending = get_session_detail(session.session_id).json()["pending_action"]

    assert pending["action_type"] == "REFUND"
    assert pending["transaction_id"] == "txn_abc123"
    assert pending["product_id"] is None


def test_expired_pending_action_is_null():
    session = session_service.create_session()
    session_service.set_pending_action(
        session.session_id,
        PendingActionType.PURCHASE,
        product_id="HDP006",
        ttl_seconds=-1,
    )
    assert get_session_detail(session.session_id).json()["pending_action"] is None


# --- 9-12, 16. Strictly no side effects -----------------------------------------------


def test_reading_does_not_extend_or_revive_pending_expiry():
    session = session_service.create_session()
    active = session_service.set_pending_action(
        session.session_id, PendingActionType.PURCHASE, product_id="HDP006"
    )

    for _ in range(3):
        body = get_session_detail(session.session_id).json()
    # expires_at is byte-identical after repeated reads — never refreshed.
    assert body["pending_action"]["expires_at"] == active.expires_at

    # And an expired one stays expired through repeated reads.
    session_service.set_pending_action(
        session.session_id, PendingActionType.PURCHASE, product_id="HDP006",
        ttl_seconds=-1,
    )
    for _ in range(3):
        assert get_session_detail(session.session_id).json()["pending_action"] is None


def test_repeated_gets_return_equivalent_data():
    session = session_service.create_session()
    session_service.append_turn(session.session_id, TurnRole.USER, "hello")

    first = get_session_detail(session.session_id).json()
    second = get_session_detail(session.session_id).json()
    third = get_session_detail(session.session_id).json()

    assert first == second == third


def test_get_mutates_nothing_in_the_database():
    """Byte-level check: sessions and turns tables are identical before
    and after retrieval (timestamps included)."""
    session = session_service.create_session()
    session_service.append_turn(session.session_id, TurnRole.USER, "hello")
    session_service.set_pending_action(
        session.session_id, PendingActionType.PURCHASE, product_id="HDP006"
    )

    before = _dump_db()
    for _ in range(3):
        assert get_session_detail(session.session_id).status_code == 200
    after = _dump_db()

    assert after == before  # no row, column, or timestamp changed


def test_get_does_not_touch_session_timestamps():
    session = session_service.create_session()
    get_session_detail(session.session_id)
    fetched = session_service.get_session(session.session_id)
    assert fetched.updated_at == session.updated_at
    assert fetched.created_at == session.created_at


# --- 17-18. Existing endpoints unchanged ------------------------------------------------


def test_chat_behavior_unchanged_and_readable_via_get(monkeypatch):
    monkeypatch.setattr(
        chat_service,
        "route_message",
        lambda message: RouteResult(route=ChatRoute.SMALLTALK),
    )
    chat_body = client.post("/chat", json={"message": "hi"}).json()

    body = get_session_detail(chat_body["session_id"]).json()

    assert [t["role"] for t in body["turns"]] == ["user", "assistant"]
    assert body["turns"][0]["content"] == "hi"
    assert body["turns"][1]["content"] == chat_body["assistant_message"]


def test_confirm_behavior_unchanged(monkeypatch):
    """confirm=false still works identically with the GET route present
    (same path prefix), and GET reflects the retained pending action."""
    session = session_service.create_session()
    session_service.set_pending_action(
        session.session_id, PendingActionType.PURCHASE, product_id="HDP006"
    )

    resp = client.post(
        f"/sessions/{session.session_id}/confirm", json={"confirm": False}
    )

    assert resp.status_code == 200
    assert resp.json()["executed"] is False
    assert (
        get_session_detail(session.session_id).json()["pending_action"]["product_id"]
        == "HDP006"
    )
