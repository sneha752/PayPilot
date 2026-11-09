"""SQLite-backed conversation-session store (Phase 8E Step 1).

Pure state management for the future chat layer: sessions, their turns,
and ONE pending action per session awaiting explicit confirmation. Follows
the ledger's established conventions — stdlib sqlite3, parameterized SQL,
PAYPILOT_DB_PATH for test isolation — in its own tables; the existing
transactions table is never touched.

This module executes NOTHING: no PayPal call, no Policy Engine
evaluation, no AI call, and no business/payment logic. A pending action
is a reference-only proposal; the future confirmation step re-fetches the
product/transaction and re-runs every deterministic check, so nothing a
client could write into a session can influence money movement.

Pending-action expiry is deterministic: an action is active only while
now < expires_at. Expired actions are never returned as active, never
silently executed, and never revived.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from ..schemas.session import (
    ConversationTurn,
    PendingAction,
    PendingActionType,
    SessionRecord,
    TurnRole,
)

logger = logging.getLogger("paypilot.sessions")

# How long a pending action stays confirmable (seconds). One constant,
# overridable via the environment for deployment tuning — never hardcoded
# at call sites.
DEFAULT_PENDING_ACTION_TTL_SECONDS = 300


def _pending_action_ttl_seconds() -> int:
    env = os.getenv("PAYPILOT_PENDING_ACTION_TTL_SECONDS", "").strip()
    return int(env) if env else DEFAULT_PENDING_ACTION_TTL_SECONDS


# Same database file as the ledger (and the same PAYPILOT_DB_PATH test
# isolation mechanism), but strictly separate tables.
_DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "paypilot.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id              TEXT PRIMARY KEY,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    pending_action_type     TEXT,
    pending_product_id      TEXT,
    pending_transaction_id  TEXT,
    pending_decision        TEXT,
    pending_created_at      TEXT,
    pending_expires_at      TEXT
);
CREATE TABLE IF NOT EXISTS session_turns (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id     TEXT NOT NULL UNIQUE,
    session_id  TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


class SessionNotFoundError(Exception):
    """Raised when no session matches the given id."""


def _db_path() -> Path:
    env = os.getenv("PAYPILOT_DB_PATH", "").strip()
    return Path(env) if env else _DEFAULT_DB


@contextmanager
def _connect():
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _now_dt().isoformat()


def _require_session(conn, session_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM sessions WHERE session_id = ?", (session_id.strip(),)
    ).fetchone()
    if row is None:
        raise SessionNotFoundError(f"No session found with id {session_id}")
    return row


def _touch(conn, session_id: str) -> None:
    conn.execute(
        "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
        (_now(), session_id.strip()),
    )


# --- Sessions -----------------------------------------------------------------


def create_session() -> SessionRecord:
    """Create and return a new empty session."""
    now = _now()
    record = SessionRecord(
        session_id=f"sess_{uuid.uuid4().hex}", created_at=now, updated_at=now
    )
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, created_at, updated_at) VALUES (?, ?, ?)",
            (record.session_id, record.created_at, record.updated_at),
        )
    logger.info("Session created: %s", record.session_id)
    return record


def get_session(session_id: str) -> SessionRecord:
    """Fetch one session, or raise SessionNotFoundError."""
    with _connect() as conn:
        row = _require_session(conn, session_id)
    return SessionRecord(
        session_id=row["session_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# --- Turns ----------------------------------------------------------------------


def append_turn(session_id: str, role: TurnRole, content: str) -> ConversationTurn:
    """Append one conversation turn and refresh the session's updated_at."""
    turn = ConversationTurn(
        turn_id=f"turn_{uuid.uuid4().hex}",
        session_id=session_id.strip(),
        role=TurnRole(role),
        content=content,
        created_at=_now(),
    )
    with _connect() as conn:
        _require_session(conn, session_id)
        conn.execute(
            """INSERT INTO session_turns (turn_id, session_id, role, content, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                turn.turn_id,
                turn.session_id,
                turn.role.value,
                turn.content,
                turn.created_at,
            ),
        )
        _touch(conn, session_id)
    return turn


def get_turns(session_id: str) -> list[ConversationTurn]:
    """Return a session's turns in chronological order.

    Ordered by the monotonic insert sequence (seq), never by SQLite row
    order or timestamp string comparison — deterministic even for turns
    created within the same clock tick.
    """
    with _connect() as conn:
        _require_session(conn, session_id)
        rows = conn.execute(
            """SELECT turn_id, session_id, role, content, created_at
               FROM session_turns WHERE session_id = ? ORDER BY seq ASC""",
            (session_id.strip(),),
        ).fetchall()
    return [
        ConversationTurn(
            turn_id=row["turn_id"],
            session_id=row["session_id"],
            role=TurnRole(row["role"]),
            content=row["content"],
            created_at=row["created_at"],
        )
        for row in rows
    ]


# --- Pending action (one per session, replace semantics) ----------------------


def set_pending_action(
    session_id: str,
    action_type: PendingActionType,
    product_id: Optional[str] = None,
    transaction_id: Optional[str] = None,
    decision_snapshot: Optional[str] = None,
    ttl_seconds: Optional[int] = None,
) -> PendingAction:
    """Store (replacing any previous) the session's pending action.

    References only — the schema validator enforces product_id for
    PURCHASE and transaction_id for REFUND, and nothing price/currency/
    capture-shaped exists to store. ttl_seconds defaults to the configured
    DEFAULT_PENDING_ACTION_TTL_SECONDS.
    """
    ttl = ttl_seconds if ttl_seconds is not None else _pending_action_ttl_seconds()
    created = _now_dt()
    action = PendingAction(
        action_type=PendingActionType(action_type),
        product_id=product_id,
        transaction_id=transaction_id,
        decision_snapshot=decision_snapshot,
        created_at=created.isoformat(),
        expires_at=(created + timedelta(seconds=ttl)).isoformat(),
    )
    with _connect() as conn:
        _require_session(conn, session_id)
        conn.execute(
            """UPDATE sessions SET
                   pending_action_type = ?, pending_product_id = ?,
                   pending_transaction_id = ?, pending_decision = ?,
                   pending_created_at = ?, pending_expires_at = ?,
                   updated_at = ?
               WHERE session_id = ?""",
            (
                action.action_type.value,
                action.product_id,
                action.transaction_id,
                action.decision_snapshot,
                action.created_at,
                action.expires_at,
                _now(),
                session_id.strip(),
            ),
        )
    logger.info(
        "Pending action set: session=%s type=%s expires_at=%s",
        session_id,
        action.action_type.value,
        action.expires_at,
    )
    return action


def get_pending_action(session_id: str) -> Optional[PendingAction]:
    """Return the session's ACTIVE pending action, or None.

    Deterministic expiry: None is returned when there is no pending
    action OR when now >= expires_at. An expired action is never
    returned as active and never revived.
    """
    with _connect() as conn:
        row = _require_session(conn, session_id)

    if row["pending_action_type"] is None:
        return None

    action = PendingAction(
        action_type=PendingActionType(row["pending_action_type"]),
        product_id=row["pending_product_id"],
        transaction_id=row["pending_transaction_id"],
        decision_snapshot=row["pending_decision"],
        created_at=row["pending_created_at"],
        expires_at=row["pending_expires_at"],
    )
    if _now_dt() >= datetime.fromisoformat(action.expires_at):
        logger.info("Pending action expired: session=%s", session_id)
        return None
    return action


def clear_pending_action(session_id: str) -> None:
    """Remove the session's pending action (idempotent)."""
    with _connect() as conn:
        _require_session(conn, session_id)
        conn.execute(
            """UPDATE sessions SET
                   pending_action_type = NULL, pending_product_id = NULL,
                   pending_transaction_id = NULL, pending_decision = NULL,
                   pending_created_at = NULL, pending_expires_at = NULL,
                   updated_at = ?
               WHERE session_id = ?""",
            (_now(), session_id.strip()),
        )
    logger.info("Pending action cleared: session=%s", session_id)


def claim_pending_action(
    session_id: str, action_type: PendingActionType
) -> Optional[PendingAction]:
    """Atomically claim-and-consume the ACTIVE pending action of `action_type`.

    The execution concurrency gate (Phase 11F-K.12). Reading the action and
    removing it happen inside ONE serialized database transaction
    (``BEGIN IMMEDIATE`` + ``busy_timeout``), so two concurrent confirmations
    of the same pending action cannot both receive it: exactly one caller gets
    the action, every other caller gets None. This is a database-level atomic
    conditional mutation, not a Python check followed by a later write — the
    losing caller observes the already-consumed row and the caller maps that
    to a controlled conflict that never reaches PayPal.

    Returns None (no raise), exactly like get_pending_action, when there is no
    active matching action: none set, expired, or a different action_type.
    Raises SessionNotFoundError for an unknown session.

    NOTE: this guarantees AT MOST ONE concurrent claim. It is not, by itself,
    exactly-once external execution — recovery after an ambiguous upstream
    outcome is a separate concern handled by the caller (see
    restore_pending_action) and by webhook reconciliation.
    """
    sid = session_id.strip()
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Own connection with manual transaction control so BEGIN IMMEDIATE takes
    # the write lock up front and serializes concurrent claimants.
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        conn.execute("PRAGMA busy_timeout = 5000")  # losers wait briefly, not error
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (sid,)
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            raise SessionNotFoundError(f"No session found with id {session_id}")
        if row["pending_action_type"] is None:
            conn.execute("ROLLBACK")
            return None
        action = PendingAction(
            action_type=PendingActionType(row["pending_action_type"]),
            product_id=row["pending_product_id"],
            transaction_id=row["pending_transaction_id"],
            decision_snapshot=row["pending_decision"],
            created_at=row["pending_created_at"],
            expires_at=row["pending_expires_at"],
        )
        if action.action_type != PendingActionType(action_type):
            conn.execute("ROLLBACK")
            return None
        if _now_dt() >= datetime.fromisoformat(action.expires_at):
            conn.execute("ROLLBACK")
            logger.info("Pending action expired at claim: session=%s", sid)
            return None
        # Sole claimant (we hold the write lock): consume it in this same txn.
        conn.execute(
            """UPDATE sessions SET
                   pending_action_type = NULL, pending_product_id = NULL,
                   pending_transaction_id = NULL, pending_decision = NULL,
                   pending_created_at = NULL, pending_expires_at = NULL,
                   updated_at = ?
               WHERE session_id = ? AND pending_action_type IS NOT NULL""",
            (_now(), sid),
        )
        conn.execute("COMMIT")
        logger.info(
            "Pending action claimed: session=%s type=%s", sid, action.action_type.value
        )
        return action
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def restore_pending_action(session_id: str, action: PendingAction) -> None:
    """Re-store a previously claimed pending action with its ORIGINAL expiry.

    Used only to make a recoverable PRE-order failure safely retryable: the
    atomic claim consumed the action, but no PayPal order was created, so the
    proposal is written back unchanged. The TTL is NOT extended (the original
    created_at / expires_at are preserved), so if it has since expired,
    get_pending_action treats it as inactive exactly as before the claim.
    """
    with _connect() as conn:
        _require_session(conn, session_id)
        conn.execute(
            """UPDATE sessions SET
                   pending_action_type = ?, pending_product_id = ?,
                   pending_transaction_id = ?, pending_decision = ?,
                   pending_created_at = ?, pending_expires_at = ?,
                   updated_at = ?
               WHERE session_id = ?""",
            (
                action.action_type.value,
                action.product_id,
                action.transaction_id,
                action.decision_snapshot,
                action.created_at,
                action.expires_at,
                _now(),
                session_id.strip(),
            ),
        )
    logger.info("Pending action restored for retry: session=%s", session_id.strip())
