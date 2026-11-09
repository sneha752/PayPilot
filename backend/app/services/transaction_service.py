"""SQLite transaction ledger — the smallest clean persistence layer.

Stdlib sqlite3, one table, no ORM: suitable for the hackathon prototype.
The database lives at backend/data/paypilot.db by default; tests point the
PAYPILOT_DB_PATH env var at a temporary file so unit tests never pollute
the development ledger.

This ledger TRACKS purchases; it never authorizes or executes them, and it
is not the payment authority — PayPal is. A record becomes CAPTURED only
after a successful PayPal capture response is in hand.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..schemas.transaction import TransactionRecord, TransactionStatus

logger = logging.getLogger("paypilot.transactions")

# backend/data/paypilot.db (this file is backend/app/services/...).
_DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "paypilot.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    transaction_id    TEXT PRIMARY KEY,
    product_id        TEXT NOT NULL,
    product_name      TEXT NOT NULL,
    catalog_price_inr INTEGER NOT NULL,
    paypal_amount     TEXT NOT NULL,
    paypal_currency   TEXT NOT NULL,
    paypal_order_id   TEXT NOT NULL UNIQUE,
    paypal_capture_id TEXT,
    status            TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
)
"""


class TransactionNotFoundError(Exception):
    """Raised when no transaction matches the given id / PayPal order id."""


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
        conn.execute(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_record(row: sqlite3.Row) -> TransactionRecord:
    return TransactionRecord(**dict(row))


def create_transaction(
    product_id: str,
    product_name: str,
    catalog_price_inr: int,
    paypal_amount: str,
    paypal_currency: str,
    paypal_order_id: str,
) -> TransactionRecord:
    """Record a purchase whose PayPal order was JUST successfully created.

    Callers must only invoke this AFTER create_order() succeeded — a failed
    PayPal order creation must never produce an ORDER_CREATED record.
    """
    now = _now()
    record = TransactionRecord(
        transaction_id=f"txn_{uuid.uuid4().hex}",
        product_id=product_id,
        product_name=product_name,
        catalog_price_inr=catalog_price_inr,
        paypal_amount=paypal_amount,
        paypal_currency=paypal_currency,
        paypal_order_id=paypal_order_id,
        paypal_capture_id=None,
        status=TransactionStatus.ORDER_CREATED,
        created_at=now,
        updated_at=now,
    )
    with _connect() as conn:
        conn.execute(
            """INSERT INTO transactions (
                   transaction_id, product_id, product_name, catalog_price_inr,
                   paypal_amount, paypal_currency, paypal_order_id,
                   paypal_capture_id, status, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.transaction_id,
                record.product_id,
                record.product_name,
                record.catalog_price_inr,
                record.paypal_amount,
                record.paypal_currency,
                record.paypal_order_id,
                record.paypal_capture_id,
                record.status.value,
                record.created_at,
                record.updated_at,
            ),
        )
    logger.info(
        "Transaction created: %s product=%s order=%s status=%s",
        record.transaction_id,
        product_id,
        paypal_order_id,
        record.status.value,
    )
    return record


def get_transaction(transaction_id: str) -> TransactionRecord:
    """Fetch one transaction by its id, or raise TransactionNotFoundError."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM transactions WHERE transaction_id = ?",
            (transaction_id.strip(),),
        ).fetchone()
    if row is None:
        raise TransactionNotFoundError(
            f"No transaction found with id {transaction_id}"
        )
    return _to_record(row)


def get_transaction_by_order(paypal_order_id: str) -> TransactionRecord:
    """Fetch one transaction by PayPal order id, or raise not-found."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM transactions WHERE paypal_order_id = ?",
            (paypal_order_id.strip(),),
        ).fetchone()
    if row is None:
        raise TransactionNotFoundError(
            f"No transaction found for PayPal order {paypal_order_id}"
        )
    return _to_record(row)


def list_transactions(
    status: Optional[TransactionStatus] = None,
    limit: int = 20,
) -> list[TransactionRecord]:
    """Return transactions newest-first, optionally filtered by status.

    Read-only history query (Phase 8D). Ordering is deterministic: primary
    key created_at DESC (the authoritative creation timestamp), with
    transaction_id DESC as a tie-breaker — never SQLite insertion order.
    Only parameterized SQL; the status filter is a TransactionStatus enum
    member, so no client string ever reaches the query text.

    Args:
        status: Keep only transactions in this state, or None for all.
        limit: Maximum rows returned, 1..100 (default 20).

    Raises:
        ValueError: if limit is outside 1..100 (never silently clamped).
    """
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")

    query = "SELECT * FROM transactions"
    params: list = []
    if status is not None:
        query += " WHERE status = ?"
        params.append(status.value)
    query += " ORDER BY created_at DESC, transaction_id DESC LIMIT ?"
    params.append(limit)

    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_to_record(row) for row in rows]


def get_transaction_by_capture(paypal_capture_id: str) -> TransactionRecord:
    """Fetch one transaction by PayPal capture id, or raise not-found.

    Used by webhook reconciliation when an event (e.g. a refund) carries a
    capture id but no order id.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM transactions WHERE paypal_capture_id = ?",
            (paypal_capture_id.strip(),),
        ).fetchone()
    if row is None:
        raise TransactionNotFoundError(
            f"No transaction found for PayPal capture {paypal_capture_id}"
        )
    return _to_record(row)


def update_status(
    transaction_id: str,
    status: TransactionStatus,
    capture_id: Optional[str] = None,
) -> TransactionRecord:
    """Set a transaction's status (webhook reconciliation writer, Phase 8B).

    A plain guarded write: the caller (webhook reconciliation) is responsible
    for deciding WHETHER the transition is allowed — this function only
    records it. It is only ever invoked for events PayPal has already
    verified and executed on its side; it never initiates payment activity.
    capture_id, when provided, fills paypal_capture_id without erasing an
    existing value with None.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT transaction_id FROM transactions WHERE transaction_id = ?",
            (transaction_id.strip(),),
        ).fetchone()
        if row is None:
            raise TransactionNotFoundError(
                f"No transaction found with id {transaction_id}"
            )
        if capture_id:
            conn.execute(
                """UPDATE transactions
                   SET status = ?, paypal_capture_id = ?, updated_at = ?
                   WHERE transaction_id = ?""",
                (status.value, capture_id, _now(), transaction_id.strip()),
            )
        else:
            conn.execute(
                """UPDATE transactions
                   SET status = ?, updated_at = ?
                   WHERE transaction_id = ?""",
                (status.value, _now(), transaction_id.strip()),
            )
    logger.info(
        "Transaction status updated: %s -> %s", transaction_id, status.value
    )
    return get_transaction(transaction_id)


def claim_for_refund(transaction_id: str) -> bool:
    """Atomically transition CAPTURED -> REFUNDING for exactly ONE caller.

    The refund concurrency gate (Phase 11F-K.13). A single conditional UPDATE
    guarded by `status = 'CAPTURED'`: SQLite serializes concurrent writers
    (each connection waits up to the default busy timeout for the write lock),
    so only the first caller flips the row and sees rowcount 1. Every other
    concurrent caller then matches 0 rows and loses. This is a database-level
    atomic conditional mutation — not a Python read-then-write — and it is a
    SHORT transaction that commits immediately: the PayPal refund call happens
    AFTER it, never with this write transaction held open.

    Returns True iff this caller won the claim (the row was CAPTURED and is now
    REFUNDING); False if it was already REFUNDING/REFUNDED or not CAPTURED.
    """
    tid = transaction_id.strip()
    with _connect() as conn:
        cur = conn.execute(
            """UPDATE transactions
                   SET status = ?, updated_at = ?
                 WHERE transaction_id = ? AND status = ?""",
            (
                TransactionStatus.REFUNDING.value,
                _now(),
                tid,
                TransactionStatus.CAPTURED.value,
            ),
        )
        won = cur.rowcount == 1
    logger.info(
        "Refund claim %s: %s", "won" if won else "lost", transaction_id
    )
    return won


def release_refund_claim(transaction_id: str) -> None:
    """Return a REFUNDING transaction to CAPTURED (retryable).

    Used only after a DEFINITIVE refund failure in which NO funds moved, so an
    explicit retry is safe. Conditional on the current status being REFUNDING,
    so it can never clobber a REFUNDED (or any other) state if something else
    advanced the row first.
    """
    tid = transaction_id.strip()
    with _connect() as conn:
        conn.execute(
            """UPDATE transactions
                   SET status = ?, updated_at = ?
                 WHERE transaction_id = ? AND status = ?""",
            (
                TransactionStatus.CAPTURED.value,
                _now(),
                tid,
                TransactionStatus.REFUNDING.value,
            ),
        )
    logger.info("Refund claim released back to CAPTURED: %s", transaction_id)


def mark_captured(paypal_order_id: str, capture_id: Optional[str]) -> TransactionRecord:
    """Transition a transaction to CAPTURED after a successful PayPal capture.

    Only called with a successful PayPal capture response in hand — a failed
    capture never reaches this function, so the record retains its previous
    state in that case.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT transaction_id FROM transactions WHERE paypal_order_id = ?",
            (paypal_order_id.strip(),),
        ).fetchone()
        if row is None:
            raise TransactionNotFoundError(
                f"No transaction found for PayPal order {paypal_order_id}"
            )
        conn.execute(
            """UPDATE transactions
               SET paypal_capture_id = ?, status = ?, updated_at = ?
               WHERE paypal_order_id = ?""",
            (
                capture_id,
                TransactionStatus.CAPTURED.value,
                _now(),
                paypal_order_id.strip(),
            ),
        )
    logger.info(
        "Transaction captured: order=%s capture=%s", paypal_order_id, capture_id
    )
    return get_transaction_by_order(paypal_order_id)
