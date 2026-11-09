"""Server-side purchase policy settings (Phase 11C).

WHY THIS EXISTS
    Every pre-11C path fed the Policy Engine an Authorization that came from
    the CLIENT: /policy/evaluate and /purchase/execute take it in the request
    body, and /ai/plan derives it from intent extraction on the user's own
    message. That is coherent for those flows — the user is stating their own
    intent in the same breath as the request.

    It does not work for external-product policy evaluation, which must be
    callable with NO request body at all, so that a client cannot move its
    own spend ceiling. The authorization therefore has to live server-side,
    and before this phase there was nowhere for it to live.

AN UNCONFIGURED SYSTEM AUTHORIZES NOTHING
    When no row exists, the defaults are the most conservative values the
    Authorization schema allows: purchase_requested=False,
    autonomous_purchase_allowed=False, auto_approve_below_inr=None. Fed to
    the existing engine those produce DENY / NO_PURCHASE_REQUESTED.

    That is deliberate. A fresh deployment must not authorize spending
    because someone forgot to configure a limit, and "no settings" must
    never be read as "no restrictions".

NO WRITE ENDPOINT IN THIS PHASE
    set_settings() is a service function, used by tests and operators. There
    is intentionally no HTTP route that writes it: exposing spend limits over
    HTTP needs authentication to be meaningful, and this project has none
    yet. Adding an unauthenticated endpoint that raises a spend ceiling would
    hand every client exactly the control this module exists to remove.

THIS MODULE DECIDES NOTHING
    It stores and returns the user's stated authorization. It applies no
    rule, compares no price, and has no opinion about any product. The
    existing Policy Engine remains the sole authority; this is only where its
    input is kept. Nothing here imports paypal, the ledger, refunds,
    sessions, or any AI provider.

FOLLOWS THE LEDGER'S CONVENTIONS
    stdlib sqlite3, no ORM, parameterized SQL, the same
    backend/data/paypilot.db and PAYPILOT_DB_PATH test isolation — in its own
    single-row table. The transactions and sessions tables are never touched.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from ..schemas.intent import Authorization, HardConstraints

logger = logging.getLogger("paypilot.purchase_policy_settings")

_DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "paypilot.db"

#: One row, always. A fixed primary key keeps it a singleton in SQL rather
#: than by convention, so a second row cannot be inserted by accident.
_SINGLETON_ID = "default"

#: auto_approve_below_inr is TEXT holding an exact decimal string, matching
#: the ledger's choice for paypal_amount. SQLite REAL would turn a stated
#: ceiling like 2500.05 into a binary float, and a spend limit that drifts
#: during a round-trip is not a limit anyone agreed to.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS purchase_policy_settings (
    id                          TEXT PRIMARY KEY,
    purchase_requested          INTEGER NOT NULL,
    autonomous_purchase_allowed INTEGER NOT NULL,
    auto_approve_below_inr      TEXT,
    max_price_inr               INTEGER,
    min_price_inr               INTEGER,
    updated_at                  TEXT NOT NULL
);
"""


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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_authorization() -> Authorization:
    """The authorization an UNCONFIGURED system grants: none.

    Returned when no settings row exists. Fed to the existing engine this
    yields DENY / NO_PURCHASE_REQUESTED.
    """
    return Authorization(
        purchase_requested=False,
        autonomous_purchase_allowed=False,
        auto_approve_below_inr=None,
    )


def get_authorization() -> Authorization:
    """The user's stored financial authorization, or the safe default.

    Returns the EXISTING Authorization schema unchanged, so the Policy
    Engine receives exactly the input shape it already understands.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM purchase_policy_settings WHERE id = ?",
            (_SINGLETON_ID,),
        ).fetchone()

    if row is None:
        return default_authorization()

    raw_limit = row["auto_approve_below_inr"]
    return Authorization(
        purchase_requested=bool(row["purchase_requested"]),
        autonomous_purchase_allowed=bool(row["autonomous_purchase_allowed"]),
        # The schema types this float; the stored string is the exact value
        # the operator stated, converted once here at the boundary.
        auto_approve_below_inr=(
            None if raw_limit is None else float(Decimal(raw_limit))
        ),
    )


def get_hard_constraints() -> Optional[HardConstraints]:
    """The user's stored shopping bounds, or None when none are set.

    Shopping bounds and the autonomous spend ceiling stay strictly separate,
    exactly as the intent schema requires: a budget is not a spend
    authorization, and the engine evaluates them under different rules.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT max_price_inr, min_price_inr FROM purchase_policy_settings "
            "WHERE id = ?",
            (_SINGLETON_ID,),
        ).fetchone()

    if row is None:
        return None
    if row["max_price_inr"] is None and row["min_price_inr"] is None:
        return None
    return HardConstraints(
        max_price_inr=row["max_price_inr"], min_price_inr=row["min_price_inr"]
    )


def set_settings(
    purchase_requested: bool = False,
    autonomous_purchase_allowed: bool = False,
    auto_approve_below_inr: Optional[Decimal] = None,
    max_price_inr: Optional[int] = None,
    min_price_inr: Optional[int] = None,
) -> Authorization:
    """Replace the stored settings. Operator/test entry point only.

    Deliberately not reachable over HTTP in this phase — see the module
    docstring. Validation is delegated to the existing Authorization and
    HardConstraints schemas, so the bounds they already enforce (a
    non-negative ceiling, non-negative bounds) apply here too rather than
    being re-implemented.

    Raises:
        ValueError: a value the existing schemas reject.
    """
    # Validate through the real schemas BEFORE writing, so an invalid
    # ceiling can never be persisted and read back as authority.
    authorization = Authorization(
        purchase_requested=purchase_requested,
        autonomous_purchase_allowed=autonomous_purchase_allowed,
        auto_approve_below_inr=(
            None if auto_approve_below_inr is None else float(auto_approve_below_inr)
        ),
    )
    if max_price_inr is not None or min_price_inr is not None:
        HardConstraints(max_price_inr=max_price_inr, min_price_inr=min_price_inr)

    with _connect() as conn:
        conn.execute(
            """INSERT INTO purchase_policy_settings (
                   id, purchase_requested, autonomous_purchase_allowed,
                   auto_approve_below_inr, max_price_inr, min_price_inr,
                   updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   purchase_requested          = excluded.purchase_requested,
                   autonomous_purchase_allowed =
                       excluded.autonomous_purchase_allowed,
                   auto_approve_below_inr      = excluded.auto_approve_below_inr,
                   max_price_inr               = excluded.max_price_inr,
                   min_price_inr               = excluded.min_price_inr,
                   updated_at                  = excluded.updated_at""",
            (
                _SINGLETON_ID,
                int(purchase_requested),
                int(autonomous_purchase_allowed),
                None if auto_approve_below_inr is None else str(auto_approve_below_inr),
                max_price_inr,
                min_price_inr,
                _now(),
            ),
        )

    logger.info(
        "Purchase policy settings updated: requested=%s autonomous=%s "
        "limit=%s shopping_max=%s shopping_min=%s",
        purchase_requested,
        autonomous_purchase_allowed,
        auto_approve_below_inr,
        max_price_inr,
        min_price_inr,
    )
    return authorization


def clear_settings() -> None:
    """Delete the stored settings, restoring the safe defaults."""
    with _connect() as conn:
        conn.execute(
            "DELETE FROM purchase_policy_settings WHERE id = ?", (_SINGLETON_ID,)
        )
    logger.info("Purchase policy settings cleared; safe defaults apply")
