"""Tests for the conversation-session store (Phase 8E Step 1).

Pure state-layer tests: sessions, turns in chronological order, session
isolation, pending-action lifecycle (set/replace/clear), deterministic
expiry, timestamp maintenance, and persistence across connections. An
autouse fixture proves no session operation ever calls PayPal/HTTP,
Gemini, or the Policy Engine. Runs against the isolated per-test SQLite
database provided by conftest.py (PAYPILOT_DB_PATH).
"""

import sqlite3

import pytest

from app.schemas.session import PendingActionType, TurnRole
from app.services import session_service


@pytest.fixture(autouse=True)
def _no_network_ai_or_policy(monkeypatch):
    """Session state management must be entirely local: any PayPal/HTTP,
    Gemini, or Policy Engine call fails the test."""
    import httpx

    from app.policy import policy_engine
    from app.services.providers import gemini

    def _forbidden(*args, **kwargs):  # pragma: no cover - fails the test if hit
        raise AssertionError(
            "Session operations must not call PayPal, AI, or the Policy Engine"
        )

    monkeypatch.setattr(httpx, "post", _forbidden)
    monkeypatch.setattr(httpx, "get", _forbidden)
    monkeypatch.setattr(policy_engine, "evaluate_policy", _forbidden)
    monkeypatch.setattr(gemini, "GeminiProvider", _forbidden)


# --- Sessions -----------------------------------------------------------------


def test_create_session_returns_identity_and_timestamps():
    session = session_service.create_session()
    assert session.session_id.startswith("sess_")
    assert session.created_at == session.updated_at
    assert session.created_at  # non-empty ISO string


def test_get_session_roundtrip():
    created = session_service.create_session()
    fetched = session_service.get_session(created.session_id)
    assert fetched == created


def test_unknown_session_raises():
    with pytest.raises(session_service.SessionNotFoundError):
        session_service.get_session("sess_does_not_exist")


def test_sessions_are_isolated_from_each_other():
    a = session_service.create_session()
    b = session_service.create_session()
    session_service.append_turn(a.session_id, TurnRole.USER, "hello from A")
    session_service.set_pending_action(
        b.session_id, PendingActionType.PURCHASE, product_id="HDP006"
    )

    assert session_service.get_turns(b.session_id) == []
    assert session_service.get_pending_action(a.session_id) is None
    assert len(session_service.get_turns(a.session_id)) == 1
    assert session_service.get_pending_action(b.session_id) is not None


# --- Turns ----------------------------------------------------------------------


def test_append_user_and_assistant_turns():
    session = session_service.create_session()
    user = session_service.append_turn(
        session.session_id, TurnRole.USER, "buy me headphones"
    )
    assistant = session_service.append_turn(
        session.session_id, TurnRole.ASSISTANT, "I recommend the boAt Rockerz 450."
    )

    assert user.role == TurnRole.USER
    assert assistant.role == TurnRole.ASSISTANT
    assert user.turn_id != assistant.turn_id
    assert user.session_id == assistant.session_id == session.session_id


def test_turns_returned_in_chronological_order(monkeypatch):
    # Same wall-clock timestamp for every turn: ordering must come from
    # the monotonic insert sequence, not from timestamps or row order.
    fixed = "2026-10-04T10:00:00+00:00"
    monkeypatch.setattr(session_service, "_now", lambda: fixed)
    session = session_service.create_session()
    contents = ["first", "second", "third", "fourth"]
    for i, content in enumerate(contents):
        role = TurnRole.USER if i % 2 == 0 else TurnRole.ASSISTANT
        session_service.append_turn(session.session_id, role, content)

    turns = session_service.get_turns(session.session_id)

    assert [t.content for t in turns] == contents


def test_turns_for_unknown_session_raise():
    with pytest.raises(session_service.SessionNotFoundError):
        session_service.append_turn("sess_nope", TurnRole.USER, "hi")
    with pytest.raises(session_service.SessionNotFoundError):
        session_service.get_turns("sess_nope")


def test_empty_turn_content_rejected():
    session = session_service.create_session()
    with pytest.raises(Exception):  # pydantic ValidationError
        session_service.append_turn(session.session_id, TurnRole.USER, "")


# --- Pending action lifecycle ---------------------------------------------------


def test_set_and_get_pending_purchase_action():
    session = session_service.create_session()
    created = session_service.set_pending_action(
        session.session_id,
        PendingActionType.PURCHASE,
        product_id="HDP006",
        decision_snapshot="REQUIRE_USER_APPROVAL",
    )

    active = session_service.get_pending_action(session.session_id)

    assert active == created
    assert active.action_type == PendingActionType.PURCHASE
    assert active.product_id == "HDP006"
    assert active.transaction_id is None
    assert active.decision_snapshot == "REQUIRE_USER_APPROVAL"
    assert active.expires_at > active.created_at


def test_set_and_get_pending_refund_action():
    session = session_service.create_session()
    session_service.set_pending_action(
        session.session_id,
        PendingActionType.REFUND,
        transaction_id="txn_abc123",
        decision_snapshot="CAPTURED",
    )

    active = session_service.get_pending_action(session.session_id)

    assert active.action_type == PendingActionType.REFUND
    assert active.transaction_id == "txn_abc123"
    assert active.product_id is None


def test_purchase_action_requires_product_id():
    session = session_service.create_session()
    with pytest.raises(Exception):  # pydantic ValidationError
        session_service.set_pending_action(
            session.session_id, PendingActionType.PURCHASE
        )


def test_refund_action_requires_transaction_id():
    session = session_service.create_session()
    with pytest.raises(Exception):  # pydantic ValidationError
        session_service.set_pending_action(
            session.session_id, PendingActionType.REFUND
        )


def test_replace_pending_action():
    session = session_service.create_session()
    session_service.set_pending_action(
        session.session_id, PendingActionType.PURCHASE, product_id="HDP006"
    )
    session_service.set_pending_action(
        session.session_id, PendingActionType.REFUND, transaction_id="txn_xyz"
    )

    active = session_service.get_pending_action(session.session_id)

    assert active.action_type == PendingActionType.REFUND
    assert active.transaction_id == "txn_xyz"
    assert active.product_id is None  # nothing left over from the replaced one


def test_clear_pending_action():
    session = session_service.create_session()
    session_service.set_pending_action(
        session.session_id, PendingActionType.PURCHASE, product_id="HDP006"
    )

    session_service.clear_pending_action(session.session_id)

    assert session_service.get_pending_action(session.session_id) is None
    # Idempotent: clearing again is fine.
    session_service.clear_pending_action(session.session_id)


def test_no_pending_action_returns_none():
    session = session_service.create_session()
    assert session_service.get_pending_action(session.session_id) is None


# --- Expiry ------------------------------------------------------------------------


def test_active_pending_action_remains_active():
    session = session_service.create_session()
    session_service.set_pending_action(
        session.session_id,
        PendingActionType.PURCHASE,
        product_id="HDP006",
        ttl_seconds=3600,
    )
    assert session_service.get_pending_action(session.session_id) is not None


def test_expired_pending_action_is_not_returned_as_active():
    session = session_service.create_session()
    session_service.set_pending_action(
        session.session_id,
        PendingActionType.PURCHASE,
        product_id="HDP006",
        ttl_seconds=-1,  # already expired at creation
    )
    assert session_service.get_pending_action(session.session_id) is None


def test_expired_action_stays_expired_and_is_never_revived():
    session = session_service.create_session()
    session_service.set_pending_action(
        session.session_id,
        PendingActionType.PURCHASE,
        product_id="HDP006",
        ttl_seconds=-1,
    )
    # Repeated reads and unrelated session activity never bring it back.
    for _ in range(3):
        assert session_service.get_pending_action(session.session_id) is None
    session_service.append_turn(session.session_id, TurnRole.USER, "still there?")
    assert session_service.get_pending_action(session.session_id) is None


def test_default_ttl_constant_is_used(monkeypatch):
    monkeypatch.delenv("PAYPILOT_PENDING_ACTION_TTL_SECONDS", raising=False)
    session = session_service.create_session()
    action = session_service.set_pending_action(
        session.session_id, PendingActionType.PURCHASE, product_id="HDP006"
    )

    from datetime import datetime

    delta = datetime.fromisoformat(action.expires_at) - datetime.fromisoformat(
        action.created_at
    )
    assert delta.total_seconds() == session_service.DEFAULT_PENDING_ACTION_TTL_SECONDS


def test_ttl_configurable_via_environment(monkeypatch):
    monkeypatch.setenv("PAYPILOT_PENDING_ACTION_TTL_SECONDS", "7")
    session = session_service.create_session()
    action = session_service.set_pending_action(
        session.session_id, PendingActionType.PURCHASE, product_id="HDP006"
    )

    from datetime import datetime

    delta = datetime.fromisoformat(action.expires_at) - datetime.fromisoformat(
        action.created_at
    )
    assert delta.total_seconds() == 7


# --- Timestamps ------------------------------------------------------------------


def test_updated_at_advances_on_turns_and_pending_actions(monkeypatch):
    times = iter(
        [
            "2026-10-04T10:00:00+00:00",  # create_session (created_at+updated_at)
            "2026-10-04T10:01:00+00:00",  # append_turn: turn created_at
            "2026-10-04T10:01:00+00:00",  # append_turn: session touch
            "2026-10-04T10:02:00+00:00",  # set_pending_action: updated_at
        ]
    )
    monkeypatch.setattr(session_service, "_now", lambda: next(times))
    session = session_service.create_session()
    assert session.updated_at == "2026-10-04T10:00:00+00:00"

    session_service.append_turn(session.session_id, TurnRole.USER, "hi")
    after_turn = session_service.get_session(session.session_id)
    assert after_turn.updated_at == "2026-10-04T10:01:00+00:00"
    assert after_turn.created_at == session.created_at  # creation time immutable

    session_service.set_pending_action(
        session.session_id, PendingActionType.PURCHASE, product_id="HDP006"
    )
    after_action = session_service.get_session(session.session_id)
    assert after_action.updated_at == "2026-10-04T10:02:00+00:00"


# --- Persistence / isolation -----------------------------------------------------


def test_state_persists_across_separate_connections(tmp_path):
    """Every service call opens its own connection; the data must come
    back from disk, and be visible to a raw sqlite3 connection too."""
    session = session_service.create_session()
    session_service.append_turn(session.session_id, TurnRole.USER, "persist me")
    session_service.set_pending_action(
        session.session_id, PendingActionType.PURCHASE, product_id="HDP006"
    )

    # Fresh raw connection to the same test database file.
    conn = sqlite3.connect(session_service._db_path())
    try:
        s_count = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        t_count = conn.execute(
            "SELECT COUNT(*) FROM session_turns WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert s_count == 1
    assert t_count == 1

    # And the service reads it back on its own new connections.
    assert session_service.get_turns(session.session_id)[0].content == "persist me"
    assert session_service.get_pending_action(session.session_id) is not None


def test_uses_isolated_test_database(tmp_path, monkeypatch):
    """The PAYPILOT_DB_PATH mechanism isolates session data per test."""
    other_db = tmp_path / "other.db"
    monkeypatch.setenv("PAYPILOT_DB_PATH", str(other_db))
    session = session_service.create_session()
    assert other_db.exists()
    assert session_service.get_session(session.session_id)


def test_transactions_table_is_untouched_by_session_schema():
    """The session store must not alter the ledger's table definition."""
    session_service.create_session()  # bootstraps session tables
    conn = sqlite3.connect(session_service._db_path())
    try:
        ledger_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'transactions'"
        ).fetchone()
    finally:
        conn.close()
    # The ledger table either doesn't exist yet in this fresh test DB, or —
    # if some test data created it — it retains its own schema unchanged.
    if ledger_sql is not None:
        assert "paypal_order_id" in ledger_sql[0]
        assert "pending_action_type" not in ledger_sql[0]
