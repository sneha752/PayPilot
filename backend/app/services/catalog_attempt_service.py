"""Durable catalog purchase-attempt reservations (Phase 11F-K.18 Option B, phase 1).

The catalog purchase flow (purchase_execution_service._create_order_result)
calls PayPal create_order BEFORE it writes the local `transactions` row, so a
crash or a ledger-write failure in between could leave a real PayPal order with
no local record at all (the Phase K.18 audit's orphan). This service adds a
DURABLE RESERVATION that is committed BEFORE that PayPal call, so such an
outcome leaves an identifiable attempt row instead of nothing.

CONVENTIONS, UNCHANGED. Stdlib sqlite3, no ORM, parameterized SQL, the same
backend/data/paypilot.db and PAYPILOT_DB_PATH test isolation as the existing
ledger (transaction_service). A DEDICATED table — the `transactions` ledger is
neither read nor written here, and is not modified by this phase.

WHAT THIS SERVICE DOES NOT DO (this phase). It does not persist the PayPal
order id (Phase K.20), does not reconcile or expose operator recovery (Phase
K.21), and never calls PayPal, captures, refunds, or touches the catalog
ledger. It records intent and disposition only.
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

from ..paypal import validate_paypal_request_id
from ..schemas.catalog_attempt import CatalogAttemptStatus, CatalogPurchaseAttempt

logger = logging.getLogger("paypilot.catalog_attempts")

# backend/data/paypilot.db (this file is backend/app/services/...).
_DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "paypilot.db"

# paypal_order_id is nullable + UNIQUE: null while CREATING (the id is not
# persisted until Phase K.20), and UNIQUE so one order id maps to at most one
# attempt. claim_key is NOT NULL UNIQUE: the reservation INSERT is therefore an
# atomic single-row claim. A separate index supports the future stale-attempt
# triage query (Phase K.21).
_SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog_purchase_attempts (
    attempt_id        TEXT PRIMARY KEY,
    product_id        TEXT NOT NULL,
    catalog_price_inr INTEGER NOT NULL,
    paypal_amount     TEXT NOT NULL,
    paypal_currency   TEXT NOT NULL,
    paypal_order_id   TEXT UNIQUE,
    claim_key         TEXT NOT NULL UNIQUE,
    paypal_request_id TEXT,
    recovery_claimed_at TEXT,
    recovery_claim_token TEXT,
    status            TEXT NOT NULL,
    transaction_id    TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_catalog_attempts_status
    ON catalog_purchase_attempts (status, created_at);
"""

_COLUMNS = (
    "attempt_id",
    "product_id",
    "catalog_price_inr",
    "paypal_amount",
    "paypal_currency",
    "paypal_order_id",
    "claim_key",
    "status",
    "transaction_id",
    "created_at",
    "updated_at",
)

#: Columns added after the table shipped, as {name: SQL type}. CREATE TABLE IF
#: NOT EXISTS does nothing to a table that already exists, so a database made
#: in Phase K.19 (before the request-id column) needs it added explicitly.
_ADDED_COLUMNS = {
    "paypal_request_id": "TEXT",  # Phase 11F-K.22a
    "recovery_claimed_at": "TEXT",  # Phase 11F-K.22b
    "recovery_claim_token": "TEXT",  # Phase 11F-K.22b ownership guard
}

#: Columns the reservation INSERT writes IN ADDITION to _COLUMNS. _COLUMNS is
#: left unchanged (tests build value tuples from it); the request id is written
#: through this extended list instead.
_RESERVE_COLUMNS = _COLUMNS + ("paypal_request_id",)


def _db_path() -> Path:
    env = os.getenv("PAYPILOT_DB_PATH", "").strip()
    return Path(env) if env else _DEFAULT_DB


def _migrate(conn: sqlite3.Connection) -> None:
    """Add any column the table predates. Additive, idempotent, harmless to
    re-run (Phase 11F-K.22a). No column is ever dropped, renamed or retyped."""
    present = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(catalog_purchase_attempts)")
    }
    for column, sql_type in _ADDED_COLUMNS.items():
        if column not in present:
            conn.execute(
                f"ALTER TABLE catalog_purchase_attempts ADD COLUMN {column} {sql_type}"
            )
            logger.info("catalog_purchase_attempts: added missing column %s", column)


@contextmanager
def _connect():
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_record(row: sqlite3.Row) -> CatalogPurchaseAttempt:
    return CatalogPurchaseAttempt(**dict(row))


# ---------------------------------------------------------------------------
# Writes. Each is its own SHORT transaction that commits and closes before the
# caller does anything else — no write transaction is ever held open across a
# network request.
# ---------------------------------------------------------------------------


def reserve_attempt(
    product_id: str,
    catalog_price_inr: int,
    paypal_amount: str,
    paypal_currency: str,
    *,
    claim_key: Optional[str] = None,
) -> CatalogPurchaseAttempt:
    """Commit a CREATING reservation BEFORE PayPal is contacted.

    Returns the committed record. The INSERT is its own transaction, so the
    reservation is durable the instant this returns; the caller then makes its
    PayPal call with no database write transaction held open.

    claim_key defaults to a fresh server-generated token (one logical
    create-order invocation). It is UNIQUE, so the INSERT is an atomic
    single-row claim; a duplicate key raises sqlite3.IntegrityError, which is
    allowed to propagate (fail closed) rather than silently reusing a row.
    """
    key = claim_key or f"cpa_claim_{uuid.uuid4().hex}"
    now = _now()
    attempt_id = f"cpa_{uuid.uuid4().hex}"
    # Phase 11F-K.22a: the stable PayPal-Request-Id for THIS attempt's single
    # create_order call, derived from its own unique attempt id (never from a
    # product id, amount, or any value shared across purchases) and validated
    # against the provider's header constraints. Persisted here, before PayPal
    # is contacted, and never overwritten afterwards.
    request_id = validate_paypal_request_id(attempt_id)
    record = CatalogPurchaseAttempt(
        attempt_id=attempt_id,
        product_id=product_id,
        catalog_price_inr=catalog_price_inr,
        paypal_amount=paypal_amount,
        paypal_currency=paypal_currency,
        paypal_order_id=None,
        claim_key=key,
        paypal_request_id=request_id,
        status=CatalogAttemptStatus.CREATING,
        transaction_id=None,
        created_at=now,
        updated_at=now,
    )
    with _connect() as conn:
        conn.execute(
            f"""INSERT INTO catalog_purchase_attempts ({", ".join(_RESERVE_COLUMNS)})
                VALUES ({", ".join("?" * len(_RESERVE_COLUMNS))})""",
            (
                record.attempt_id,
                record.product_id,
                record.catalog_price_inr,
                record.paypal_amount,
                record.paypal_currency,
                None,
                record.claim_key,
                record.status.value,
                None,
                record.created_at,
                record.updated_at,
                record.paypal_request_id,
            ),
        )
    logger.info(
        "Catalog purchase attempt reserved: %s product=%s amount=%s %s "
        "status=CREATING (BEFORE PayPal)",
        record.attempt_id,
        product_id,
        paypal_amount,
        paypal_currency,
    )
    return record


class CatalogAttemptPersistenceError(Exception):
    """The PayPal order id could not be recorded on its CREATING attempt (K.20).

    Raised ONLY after create_order already returned a real, non-empty order id,
    when the guarded claim neither updated exactly one CREATING/NULL-order-id
    row nor found that row already carrying this exact id. Failing closed here
    is deliberate: a real PayPal order EXISTS, so the attempt is left CREATING
    (never FINALIZED on an inconsistent row, never a different id overwritten),
    and no second order or transaction is created. The order id is carried so
    the condition is actionable by later recovery.
    """

    def __init__(
        self,
        attempt_id: str,
        paypal_order_id: str,
        record: Optional[CatalogPurchaseAttempt] = None,
    ) -> None:
        self.attempt_id = attempt_id
        self.paypal_order_id = paypal_order_id
        self.record = record
        super().__init__(
            f"Attempt {attempt_id} could not record its PayPal order id on the "
            f"CREATING reservation; it is left for recovery and no second order "
            f"or transaction will be created."
        )


def claim_order_id(
    attempt_id: str, order_id: str
) -> tuple[bool, Optional[CatalogPurchaseAttempt]]:
    """Persist the PayPal order id on the still-CREATING attempt (Phase K.20).

    The committed write that closes the catalog orphan's definite window: once
    create_order returns a real id, it is written to the attempt BEFORE the
    ledger row, so a crash after this point leaves a CREATING attempt that
    names the order it created.

    Guarded so it can ONLY fill a NULL order id on a CREATING row, and never
    changes status. The UNIQUE(paypal_order_id) constraint means one order id
    maps to at most one attempt; an id already recorded against ANOTHER attempt
    raises IntegrityError, caught and reported as not-claimed rather than
    stealing or corrupting a row.

    Returns (claimed, record): claimed is True iff exactly one row moved.
    """
    with _connect() as conn:
        try:
            cursor = conn.execute(
                """UPDATE catalog_purchase_attempts
                   SET paypal_order_id = ?, updated_at = ?
                   WHERE attempt_id = ?
                     AND status = ?
                     AND paypal_order_id IS NULL""",
                (
                    order_id,
                    _now(),
                    attempt_id,
                    CatalogAttemptStatus.CREATING.value,
                ),
            )
            claimed = cursor.rowcount == 1
        except sqlite3.IntegrityError:
            logger.error(
                "Order-id claim for attempt %s hit a UNIQUE conflict; the id "
                "already belongs to another attempt. Failing closed.",
                attempt_id,
            )
            claimed = False
    return claimed, get(attempt_id)


def mark_failed(attempt_id: str) -> Optional[CatalogPurchaseAttempt]:
    """Move a CREATING reservation to FAILED (definitive no-order).

    Guarded on status = CREATING, so it can never overwrite a FINALIZED or an
    already-FAILED row. Returns the refreshed record (or None if it vanished).
    """
    with _connect() as conn:
        conn.execute(
            """UPDATE catalog_purchase_attempts
               SET status = ?, updated_at = ?
               WHERE attempt_id = ? AND status = ?""",
            (
                CatalogAttemptStatus.FAILED.value,
                _now(),
                attempt_id,
                CatalogAttemptStatus.CREATING.value,
            ),
        )
    logger.info("Catalog purchase attempt marked FAILED: %s", attempt_id)
    return get(attempt_id)


def mark_finalized(
    attempt_id: str, transaction_id: str
) -> Optional[CatalogPurchaseAttempt]:
    """Move a CREATING reservation to FINALIZED and link the ledger row.

    Guarded on status = CREATING, so it is applied exactly once and can never
    disturb a FAILED row. Called only AFTER the existing create_transaction
    committed, so a create_transaction failure leaves the attempt CREATING.
    """
    with _connect() as conn:
        conn.execute(
            """UPDATE catalog_purchase_attempts
               SET status = ?, transaction_id = ?, updated_at = ?
               WHERE attempt_id = ? AND status = ?""",
            (
                CatalogAttemptStatus.FINALIZED.value,
                transaction_id,
                _now(),
                attempt_id,
                CatalogAttemptStatus.CREATING.value,
            ),
        )
    logger.info(
        "Catalog purchase attempt FINALIZED: %s txn=%s", attempt_id, transaction_id
    )
    return get(attempt_id)


# ---------------------------------------------------------------------------
# Durable recovery claim (Phase 11F-K.22b).
#
# Operator-gated create-order REPLAY recovery must let at most ONE worker
# replay a given ambiguous attempt at a time. The claim is a DURABLE
# compare-and-set on a nullable column (never an in-memory lock): a guarded
# UPDATE that moves exactly one still-ambiguous row. It never touches the
# order id, the request id or the status, and it is cleared when the replay
# completes so a later operator retry (still inside the window) can re-claim.
# A claim left behind by a crashed worker becomes reclaimable after a TTL,
# but that NEVER widens eligibility: the recovery window is measured from the
# immutable created_at, checked independently before any claim.
# ---------------------------------------------------------------------------

#: A held claim older than this is treated as abandoned (a crashed worker) and
#: may be reclaimed. Comfortably longer than the 15s create_order timeout, so a
#: genuinely in-flight replay is never stolen from under a live worker.
DEFAULT_RECOVERY_CLAIM_TTL_SECONDS = 300


def claim_recovery(
    attempt_id: str,
    *,
    now: Optional[datetime] = None,
    claim_ttl_seconds: Optional[int] = None,
) -> tuple[bool, Optional[CatalogPurchaseAttempt], Optional[str]]:
    """Atomically claim a still-ambiguous CREATING attempt for a replay.

    The claim is granted ONLY to a row that is still CREATING with NO persisted
    order id and whose claim slot is free (null) or stale (older than the TTL).
    The WHERE clause is the guard, so the database arbitrates: exactly one
    concurrent caller sees rowcount == 1 and may contact PayPal; the rest see 0
    and must not. It writes neither the order id nor the status.

    A fresh UUID token is generated for every successful claim (including a
    stale-claim takeover) and stored alongside the timestamp. The caller must
    present this token to release_recovery_claim; a worker whose claim was
    reclaimed by a later worker will hold an outdated token, so its release
    becomes a harmless no-op.

    Returns (claimed, record, token): claimed is True iff this call won the
    claim; token is the ownership token when claimed, None otherwise.
    """
    current = now if now is not None else datetime.now(timezone.utc)
    ttl = (
        claim_ttl_seconds
        if claim_ttl_seconds is not None
        else DEFAULT_RECOVERY_CLAIM_TTL_SECONDS
    )
    stamp = current.isoformat()
    stale_cutoff = (current - timedelta(seconds=ttl)).isoformat()
    token = uuid.uuid4().hex
    with _connect() as conn:
        cursor = conn.execute(
            """UPDATE catalog_purchase_attempts
               SET recovery_claimed_at = ?,
                   recovery_claim_token = ?,
                   updated_at = ?
               WHERE attempt_id = ?
                 AND status = ?
                 AND paypal_order_id IS NULL
                 AND (recovery_claimed_at IS NULL OR recovery_claimed_at <= ?)""",
            (
                stamp,
                token,
                stamp,
                attempt_id,
                CatalogAttemptStatus.CREATING.value,
                stale_cutoff,
            ),
        )
        claimed = cursor.rowcount == 1
    if not claimed:
        token = None
    return claimed, get(attempt_id), token


def release_recovery_claim(
    attempt_id: str, claim_token: str
) -> Optional[CatalogPurchaseAttempt]:
    """Clear the recovery claim on a still-CREATING attempt (owner-guarded).

    Guarded on status = CREATING AND recovery_claim_token = claim_token, so it
    releases only the caller's OWN claim. If another worker reclaimed this
    attempt (writing a new token), this call's token no longer matches and the
    UPDATE is a no-op — the new owner's claim is never disturbed. Clearing the
    claim never makes the attempt eligible on its own — eligibility is
    re-derived from created_at and the window on every recovery call.
    """
    with _connect() as conn:
        conn.execute(
            """UPDATE catalog_purchase_attempts
               SET recovery_claimed_at = NULL,
                   recovery_claim_token = NULL,
                   updated_at = ?
               WHERE attempt_id = ?
                 AND status = ?
                 AND recovery_claim_token = ?""",
            (
                _now(),
                attempt_id,
                CatalogAttemptStatus.CREATING.value,
                claim_token,
            ),
        )
    return get(attempt_id)


# ---------------------------------------------------------------------------
# Reads (diagnostics and tests). All read-only; none calls PayPal.
# ---------------------------------------------------------------------------


def get(attempt_id: str) -> Optional[CatalogPurchaseAttempt]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM catalog_purchase_attempts WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
    return None if row is None else _to_record(row)


def get_by_claim_key(claim_key: str) -> Optional[CatalogPurchaseAttempt]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM catalog_purchase_attempts WHERE claim_key = ?",
            (claim_key,),
        ).fetchone()
    return None if row is None else _to_record(row)


def get_by_order(paypal_order_id: str) -> Optional[CatalogPurchaseAttempt]:
    """By PayPal order id. Always None in Phase K.19 (the id is never stored
    yet); present so later phases and reconciliation can resolve an attempt."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM catalog_purchase_attempts WHERE paypal_order_id = ?",
            (paypal_order_id,),
        ).fetchone()
    return None if row is None else _to_record(row)


def list_by_status(
    status: CatalogAttemptStatus, limit: int = 50
) -> list[CatalogPurchaseAttempt]:
    """Attempts in one status, oldest-first, bounded. Read-only triage query."""
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM catalog_purchase_attempts
               WHERE status = ?
               ORDER BY created_at ASC, attempt_id ASC
               LIMIT ?""",
            (status.value, limit),
        ).fetchall()
    return [_to_record(row) for row in rows]


def count() -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM catalog_purchase_attempts"
        ).fetchone()
    return int(row["n"])


#: How old a CREATING attempt must be before operator triage treats it as STALE
#: (Phase 11F-K.21). CREATING means a create-order call was in flight or its
#: outcome was never learned; an attempt younger than this is very likely still
#: in flight (or mid-finalization), not a stuck orphan, so it is NOT surfaced.
#: 900s (15 min) is far longer than the 15s create_order timeout plus any
#: realistic finalization, so anything older is genuinely stuck.
DEFAULT_CATALOG_ATTEMPT_STALE_SECONDS = 900

CATALOG_ATTEMPT_STALE_ENV_VAR = "CATALOG_ATTEMPT_STALE_SECONDS"


def catalog_attempt_stale_seconds() -> int:
    """The configured CREATING staleness threshold, in seconds.

    Read from CATALOG_ATTEMPT_STALE_SECONDS when set to a positive integer;
    otherwise the clearly-defined default. A misconfigured value does NOT break
    the read-only triage list — it logs a warning and falls back to the
    default, because an operator's visibility tool failing closed to a 500
    would hide exactly the stuck attempts it exists to show.
    """
    raw = os.getenv(CATALOG_ATTEMPT_STALE_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_CATALOG_ATTEMPT_STALE_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "%s is not an integer (%r); using default %ss",
            CATALOG_ATTEMPT_STALE_ENV_VAR,
            raw,
            DEFAULT_CATALOG_ATTEMPT_STALE_SECONDS,
        )
        return DEFAULT_CATALOG_ATTEMPT_STALE_SECONDS
    if value <= 0:
        logger.warning(
            "%s must be positive (got %s); using default %ss",
            CATALOG_ATTEMPT_STALE_ENV_VAR,
            value,
            DEFAULT_CATALOG_ATTEMPT_STALE_SECONDS,
        )
        return DEFAULT_CATALOG_ATTEMPT_STALE_SECONDS
    return value


def list_stale_creating(
    limit: int = 50,
    *,
    now: Optional[datetime] = None,
    grace_seconds: Optional[int] = None,
) -> list[CatalogPurchaseAttempt]:
    """Read-only triage query: CREATING attempts older than the grace period.

    A CREATING attempt is "stale" when its created_at is at or before
    now - grace_seconds. Newer attempts are excluded so an in-flight / mid-
    finalization attempt is never mistaken for a stuck orphan. Oldest-first and
    bounded, like list_by_status. It never writes and never calls PayPal.

    Args:
        limit: maximum rows, 1..100 (default 50); never silently clamped.
        now: override clock (tests); defaults to the server's UTC clock.
        grace_seconds: override threshold (tests); defaults to
            catalog_attempt_stale_seconds().

    Raises:
        ValueError: limit is outside 1..100.
    """
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    current = now if now is not None else datetime.now(timezone.utc)
    grace = (
        grace_seconds if grace_seconds is not None else catalog_attempt_stale_seconds()
    )
    cutoff = (current - timedelta(seconds=grace)).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM catalog_purchase_attempts
               WHERE status = ? AND created_at <= ?
               ORDER BY created_at ASC, attempt_id ASC
               LIMIT ?""",
            (CatalogAttemptStatus.CREATING.value, cutoff, limit),
        ).fetchall()
    return [_to_record(row) for row in rows]
