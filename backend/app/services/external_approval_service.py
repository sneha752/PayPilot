"""External-product explicit approval gate (Phase 11D).

    AdoptedProduct
      + latest revalidation snapshot      <- server-side state only
      + the EXISTING Policy Engine's ruling (via Phase 11C)
            |
            v
    [ freshness gate ]   is that evidence still young enough to buy against?
            |
    [ DENY gate ]        the engine said no -> no approval record exists
            |
            v
    PENDING approval, bound to (product, revalidation, price, currency,
                                decision, reason code)
            |
      explicit {"approve": true}  +  every binding re-verified NOW
            |
            v
         APPROVED        <- the last state this phase has. Executes nothing.

WHY A DEDICATED RECORD RATHER THAN THE EXISTING CONFIRMATION PATH
    The existing session confirmation path was inspected and deliberately
    NOT reused. confirmation_service.confirm_pending_action() calls
    purchase_execution_service.approve_purchase(), which re-fetches a LOCAL
    CATALOG product by id, creates a PayPal order and writes the transaction
    ledger. Its PendingAction carries a catalog product_id or a
    transaction_id and lives on a chat session.

    Every one of those assumptions is false here: an external product has no
    catalog id, no catalog price, and no execution path at all in this
    phase. Adapting that machinery would mean handing an external product to
    a function whose next act is to create a payment -- the precise
    boundary this phase exists to establish. So this module owns its own
    table, its own statuses, and no import of any payment code.

THERE IS NO SECOND POLICY ENGINE HERE
    Search this file for a price threshold, a spend limit, a budget
    comparison or a notion of "allowed" and there is none. The decision
    arrives from external_policy_service.evaluate_adopted_product(), which
    is itself only an adapter onto the existing engine. This module reads
    the decision, stores it, and later compares it for equality. It never
    computes one.

    The ONE rule this phase adds is not a policy rule, and it is stricter
    rather than looser: an external product requires explicit human approval
    even when the engine returns ALLOW_AUTONOMOUS_PURCHASE. A permissive
    ruling is treated as "the rules would permit this", never as "proceed".

FRESHNESS IS SERVER-SIDE AND EXPLICIT
    Phase 11C had no freshness bound at all: a revalidation from last week
    could be evaluated as current. The rule added here is

        checked_at + TTL >= now        (inclusive at the boundary)

    computed from the revalidation snapshot's own server-generated
    checked_at. No client timestamp exists in either request, so none can be
    consulted. A timestamp that will not parse, or that carries no timezone,
    is UNUSABLE rather than assumed-UTC: an unknown age must not read as
    fresh.

    The same TTL also sets the approval's expires_at
    (checked_at + TTL), so an approval can never outlive the evidence it
    commits to. One knob, one rule, no interaction to reason about.

LATEST REVALIDATION SEMANTICS ARE PHASE 11C's, UNCHANGED
    Eligibility is decided by evaluate_adopted_product(), which considers
    only the single most recent snapshot. If the latest snapshot is not
    REVALIDATED, an older successful one never rescues it. This module adds
    nothing to that rule and cannot weaken it, because it never queries
    revalidation history itself -- it only re-reads one snapshot BY ID to
    confirm the exact row it is bound to.

NO PAYMENT, NO AI, NO SESSION
    This module imports no PayPal client, no purchase execution, no capture,
    no refund, no transaction ledger, no session store, no confirmation
    service, no webhook code and no AI provider. It writes exactly one
    table. APPROVED creates nothing downstream; there is no downstream yet.

FOLLOWS THE LEDGER'S CONVENTIONS
    stdlib sqlite3, no ORM, parameterized SQL, the same
    backend/data/paypilot.db and PAYPILOT_DB_PATH test isolation -- in its
    own table. Prices are TEXT holding exact Decimal strings, because
    SQLite's REAL would turn an exact decimal into a binary float.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from ..schemas.external_approval import (
    ApprovalBlockedReason,
    ApprovalConflict,
    ExternalApproval,
    ExternalApprovalRequestResult,
    ExternalApprovalStatus,
)
from ..schemas.external_policy import EligibilityStatus, ExternalPolicyEvaluation
from ..schemas.policy import PolicyOutcome
from ..schemas.revalidation import RevalidationStatus
from . import adoption_service, external_policy_service, revalidation_service

logger = logging.getLogger("paypilot.external_approval")

#: How recently the provider must have confirmed the product for an approval
#: to be prepared or resolved (seconds). Conservative on purpose: this
#: governs the evidence a human is asked to commit money against, and a
#: retailer can change a price at any moment. Five minutes matches the
#: existing pending-action TTL, so the two human-decision windows in the
#: system behave alike.
#:
#: There is deliberately no "unlimited" setting and no code path that skips
#: the check.
DEFAULT_EXTERNAL_REVALIDATION_TTL_SECONDS = 300

#: Environment override, read per call so a deployment (or a test) can
#: change it without restarting or reaching into module state.
TTL_ENV_VAR = "EXTERNAL_REVALIDATION_TTL_SECONDS"

_DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "paypilot.db"

# Prices are TEXT holding the exact Decimal string, the same choice the
# ledger makes for paypal_amount. There is no paypal_order_id,
# paypal_capture_id, refund_id, transaction_id or session_id column: those
# things do not exist in this phase, so a forged value has nowhere to land.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS external_product_approvals (
    approval_id                 TEXT PRIMARY KEY,
    adopted_product_id          TEXT NOT NULL,
    revalidation_id             TEXT NOT NULL,
    revalidation_checked_at     TEXT NOT NULL,
    policy_decision             TEXT NOT NULL,
    policy_reason_code          TEXT NOT NULL,
    approved_normalized_price   TEXT NOT NULL,
    approved_normalized_currency TEXT NOT NULL,
    merchant_name               TEXT,
    merchant_domain             TEXT,
    status                      TEXT NOT NULL,
    created_at                  TEXT NOT NULL,
    expires_at                  TEXT NOT NULL,
    approved_at                 TEXT,
    rejected_at                 TEXT
);
CREATE INDEX IF NOT EXISTS idx_external_approvals_product
    ON external_product_approvals (adopted_product_id, status);
"""

_COLUMNS = (
    "approval_id",
    "adopted_product_id",
    "revalidation_id",
    "revalidation_checked_at",
    "policy_decision",
    "policy_reason_code",
    "approved_normalized_price",
    "approved_normalized_currency",
    "merchant_name",
    "merchant_domain",
    "status",
    "created_at",
    "expires_at",
    "approved_at",
    "rejected_at",
)


class ApprovalConfigError(Exception):
    """Raised when EXTERNAL_REVALIDATION_TTL_SECONDS is unusable.

    A deployment fault, not a client error: the endpoint maps it to 500. It
    is deliberately NOT swallowed into a default, because a silently
    defaulted TTL is a security setting nobody is told they lost.
    """


class ApprovalNotFoundError(Exception):
    """Raised when no approval record matches the given id."""


class ApprovalConflictError(Exception):
    """Raised when an approval cannot be resolved as requested.

    Carries the machine-readable code and the approval's CURRENT state, so
    the endpoint can return a deterministic 409 that shows the user what
    moved. Nothing was executed and nothing was repaired.
    """

    def __init__(
        self, code: ApprovalConflict, message: str, approval: ExternalApproval
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.approval = approval


# ---------------------------------------------------------------------------
# Configuration, clock and storage plumbing
# ---------------------------------------------------------------------------


def revalidation_ttl_seconds() -> int:
    """The configured freshness TTL, in seconds.

    A non-numeric or non-positive setting raises ApprovalConfigError rather
    than silently falling back: a TTL of 0 or -1 would make every external
    approval impossible, and a deployment deserves to learn that from an
    error instead of from a product that can never be bought.
    """
    raw = os.getenv(TTL_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_EXTERNAL_REVALIDATION_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError as exc:
        raise ApprovalConfigError(
            f"{TTL_ENV_VAR} must be an integer number of seconds, got {raw!r}"
        ) from exc
    if value <= 0:
        raise ApprovalConfigError(
            f"{TTL_ENV_VAR} must be a positive integer number of seconds, "
            f"got {value}"
        )
    return value


def _now_dt() -> datetime:
    """The server's own UTC clock. The only clock this module consults."""
    return datetime.now(timezone.utc)


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


def _to_record(row: sqlite3.Row) -> ExternalApproval:
    data = dict(row)
    data["approved_normalized_price"] = Decimal(data["approved_normalized_price"])
    return ExternalApproval(**data)


# ---------------------------------------------------------------------------
# Freshness: the one rule Phase 11D adds
# ---------------------------------------------------------------------------


def parse_checked_at(raw: Optional[str]) -> Optional[datetime]:
    """Read a revalidation's server-side checked_at as an aware UTC instant.

    Returns None when the value is missing, unparseable, or NAIVE. A naive
    timestamp names no instant -- "12:00" is a different moment in every
    zone -- so assuming UTC would be inventing a fact in exactly the
    direction that makes stale evidence look fresh. Unknown age is reported
    as unusable instead.
    """
    if not raw or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def is_fresh(checked_at: datetime, now: datetime, ttl_seconds: int) -> bool:
    """The freshness boundary, stated once:

        checked_at + ttl >= now

    INCLUSIVE at the boundary: evidence exactly ttl seconds old is still
    fresh, and one microsecond later it is not. One comparison, no tolerance
    band, no rounding, both sides timezone-aware.
    """
    return checked_at + timedelta(seconds=ttl_seconds) >= now


def _expires_at(checked_at: datetime, ttl_seconds: int) -> datetime:
    """When an approval bound to this evidence stops being resolvable.

    Deliberately derived from the EVIDENCE, not from the approval's creation
    time, so an approval prepared against already-ageing evidence inherits
    its remaining life rather than restarting the clock.
    """
    return checked_at + timedelta(seconds=ttl_seconds)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get(approval_id: str) -> ExternalApproval:
    """One approval by id, with any due expiry already applied.

    Reading can transition PENDING -> EXPIRED, which is the only mutation a
    read performs. It is safe and idempotent: expiry is a pure function of
    expires_at and the clock, so computing it lazily and persisting it give
    the same answer. Nothing else about the record changes.
    """
    identifier = (approval_id or "").strip()
    if not identifier:
        raise ValueError("approval_id must be a non-empty string")

    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM external_product_approvals WHERE approval_id = ?",
            (identifier,),
        ).fetchone()
        if row is None:
            raise ApprovalNotFoundError(f"No approval found with id {identifier}")

        record = _to_record(row)
        if record.status is not ExternalApprovalStatus.PENDING:
            return record
        if _now_dt() < datetime.fromisoformat(record.expires_at):
            return record

        # Due for expiry. Guarded on status so a concurrent resolve that
        # already won cannot be overwritten by this read.
        conn.execute(
            """UPDATE external_product_approvals
               SET status = ?
               WHERE approval_id = ? AND status = ?""",
            (
                ExternalApprovalStatus.EXPIRED.value,
                identifier,
                ExternalApprovalStatus.PENDING.value,
            ),
        )
        fresh_row = conn.execute(
            "SELECT * FROM external_product_approvals WHERE approval_id = ?",
            (identifier,),
        ).fetchone()

    logger.info("External approval %s expired (expires_at=%s)", identifier, record.expires_at)
    return _to_record(fresh_row)


def list_for_product(
    adopted_product_id: str, limit: int = 20
) -> list[ExternalApproval]:
    """Approvals for one adopted product, newest first. Read-only.

    Ordering is deterministic -- created_at DESC with approval_id DESC as a
    tie-breaker -- never SQLite insertion order. No expiry is applied here;
    use get() for a single authoritative record.
    """
    identifier = (adopted_product_id or "").strip()
    if not identifier:
        raise ValueError("adopted_product_id must be a non-empty string")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")

    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM external_product_approvals
               WHERE adopted_product_id = ?
               ORDER BY created_at DESC, approval_id DESC
               LIMIT ?""",
            (identifier, limit),
        ).fetchall()
    return [_to_record(row) for row in rows]


def count(adopted_product_id: Optional[str] = None) -> int:
    """How many approvals exist, overall or for one product. Tests/diagnostics."""
    with _connect() as conn:
        if adopted_product_id is None:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM external_product_approvals"
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM external_product_approvals "
                "WHERE adopted_product_id = ?",
                ((adopted_product_id or "").strip(),),
            ).fetchone()
    return int(row["n"])


# ---------------------------------------------------------------------------
# Preparing an approval
# ---------------------------------------------------------------------------


def _blocked(
    adopted_product_id: str,
    reason: ApprovalBlockedReason,
    evaluation: ExternalPolicyEvaluation,
    ttl: int,
    age_seconds: Optional[float],
) -> ExternalApprovalRequestResult:
    """Report that no approval was created. No record is inserted.

    The Phase 11C verdict is carried over verbatim so the caller can see the
    whole chain, but `approval` stays null -- there is nothing to resolve.
    """
    logger.info(
        "External approval blocked: %s for %s (eligibility=%s reason=%s "
        "revalidation=%s age=%s ttl=%s)",
        reason.value,
        adopted_product_id,
        evaluation.eligibility.value,
        evaluation.eligibility_reason.value,
        evaluation.revalidation_id,
        age_seconds,
        ttl,
    )
    decision = evaluation.policy_decision
    return ExternalApprovalRequestResult(
        adopted_product_id=adopted_product_id,
        approvable=False,
        blocked_reason=reason,
        created=False,
        eligibility=evaluation.eligibility,
        eligibility_reason=evaluation.eligibility_reason,
        revalidation_id=evaluation.revalidation_id,
        revalidation_checked_at=evaluation.revalidation_checked_at,
        revalidation_status=evaluation.revalidation_status,
        revalidation_ttl_seconds=ttl,
        revalidation_age_seconds=age_seconds,
        policy_decision=decision.decision if decision else None,
        policy_reason_code=decision.reason_code if decision else None,
        current_normalized_price=evaluation.current_normalized_price,
        current_normalized_currency=evaluation.current_normalized_currency,
        approval=None,
    )


def _existing_pending_for_binding(
    conn: sqlite3.Connection,
    *,
    adopted_product_id: str,
    revalidation_id: str,
    policy_decision: str,
    policy_reason_code: str,
    price: str,
    currency: str,
    now: datetime,
) -> Optional[ExternalApproval]:
    """A still-resolvable PENDING approval for this EXACT binding, if any.

    Matched on every bound field, not just the product: a second request
    after the price or the policy moved must get its own record, because it
    is a different commitment. Expired rows are skipped rather than revived.
    """
    rows = conn.execute(
        """SELECT * FROM external_product_approvals
           WHERE adopted_product_id = ?
             AND revalidation_id = ?
             AND policy_decision = ?
             AND policy_reason_code = ?
             AND approved_normalized_price = ?
             AND approved_normalized_currency = ?
             AND status = ?
           ORDER BY created_at DESC, approval_id DESC""",
        (
            adopted_product_id,
            revalidation_id,
            policy_decision,
            policy_reason_code,
            price,
            currency,
            ExternalApprovalStatus.PENDING.value,
        ),
    ).fetchall()
    for row in rows:
        record = _to_record(row)
        if now < datetime.fromisoformat(record.expires_at):
            return record
    return None


def request_approval(adopted_product_id: str) -> ExternalApprovalRequestResult:
    """Prepare a PENDING approval for one adopted external product.

    The ONLY input is the adopted product's id. The policy decision, the
    revalidation, the price and the currency all come from server-side
    state, so no caller can influence what gets approved.

    This function makes no PayPal call, creates no transaction, creates no
    session, executes no purchase and invokes no AI. Its single side effect
    is one row in external_product_approvals.

    Gates, in order:
      1. the adopted product must exist
      2. Phase 11C must have found it ELIGIBLE (which requires the LATEST
         revalidation to be REVALIDATED and to carry a fresh price)
      3. that revalidation's checked_at must be usable, and fresh per TTL
      4. the Policy Engine's decision must not be DENY

    Raises:
        ValueError: blank id.
        ApprovalConfigError: the TTL environment variable is unusable.
        AdoptedProductNotFoundError: no such adopted product.
    """
    identifier = (adopted_product_id or "").strip()
    if not identifier:
        raise ValueError("adopted_product_id must be a non-empty string")

    ttl = revalidation_ttl_seconds()

    # Raises AdoptedProductNotFoundError for an unknown id, before anything
    # else happens.
    adopted = adoption_service.get(identifier)

    # The EXISTING policy path, unchanged. It owns latest-revalidation
    # semantics, the no-stale-price rule, and the engine call itself.
    evaluation = external_policy_service.evaluate_adopted_product(
        adopted.adopted_product_id
    )

    now = _now_dt()

    # Gate 2 -- no fresh confirmed evidence means there is nothing to
    # approve. The reason code says which upstream requirement failed.
    if evaluation.eligibility is not EligibilityStatus.EVALUATED:
        return _blocked(
            adopted.adopted_product_id,
            ApprovalBlockedReason.NOT_POLICY_ELIGIBLE,
            evaluation,
            ttl,
            None,
        )

    # Gate 3 -- the freshness rule this phase adds. Computed from the
    # snapshot's own server-generated timestamp.
    checked_at = parse_checked_at(evaluation.revalidation_checked_at)
    if checked_at is None:
        return _blocked(
            adopted.adopted_product_id,
            ApprovalBlockedReason.REVALIDATION_TIMESTAMP_UNUSABLE,
            evaluation,
            ttl,
            None,
        )

    age_seconds = (now - checked_at).total_seconds()
    if not is_fresh(checked_at, now, ttl):
        return _blocked(
            adopted.adopted_product_id,
            ApprovalBlockedReason.REVALIDATION_STALE,
            evaluation,
            ttl,
            age_seconds,
        )

    decision = evaluation.policy_decision
    assert decision is not None  # EVALUATED always carries the engine's ruling

    # Gate 4 -- DENY means no approval record exists to be resolved. The
    # user is never offered a button that could overrule the engine.
    if decision.decision is PolicyOutcome.DENY:
        return _blocked(
            adopted.adopted_product_id,
            ApprovalBlockedReason.POLICY_DENIED,
            evaluation,
            ttl,
            age_seconds,
        )

    # NOTE: ALLOW_AUTONOMOUS_PURCHASE deliberately falls through to here.
    # For an external product it does NOT execute and does NOT self-approve;
    # it produces a PENDING record exactly like REQUIRE_USER_APPROVAL does.
    price_text = str(evaluation.current_normalized_price)
    currency = evaluation.current_normalized_currency or ""
    snapshot = revalidation_service.get_revalidation(evaluation.revalidation_id or "")

    with _connect() as conn:
        existing = _existing_pending_for_binding(
            conn,
            adopted_product_id=adopted.adopted_product_id,
            revalidation_id=evaluation.revalidation_id or "",
            policy_decision=decision.decision.value,
            policy_reason_code=decision.reason_code.value,
            price=price_text,
            currency=currency,
            now=now,
        )
        if existing is not None:
            logger.info(
                "External approval request reused PENDING %s for %s",
                existing.approval_id,
                adopted.adopted_product_id,
            )
            record = existing
            created = False
        else:
            record = ExternalApproval(
                approval_id=f"xapv_{uuid.uuid4().hex}",
                adopted_product_id=adopted.adopted_product_id,
                revalidation_id=evaluation.revalidation_id or "",
                revalidation_checked_at=evaluation.revalidation_checked_at or "",
                policy_decision=decision.decision,
                policy_reason_code=decision.reason_code,
                approved_normalized_price=evaluation.current_normalized_price,
                approved_normalized_currency=currency,
                merchant_name=snapshot.current_merchant_name if snapshot else None,
                merchant_domain=snapshot.current_merchant_domain if snapshot else None,
                status=ExternalApprovalStatus.PENDING,
                created_at=now.isoformat(),
                expires_at=_expires_at(checked_at, ttl).isoformat(),
                approved_at=None,
                rejected_at=None,
            )
            conn.execute(
                f"""INSERT INTO external_product_approvals ({", ".join(_COLUMNS)})
                    VALUES ({", ".join("?" * len(_COLUMNS))})""",
                (
                    record.approval_id,
                    record.adopted_product_id,
                    record.revalidation_id,
                    record.revalidation_checked_at,
                    record.policy_decision.value,
                    record.policy_reason_code.value,
                    price_text,
                    record.approved_normalized_currency,
                    record.merchant_name,
                    record.merchant_domain,
                    record.status.value,
                    record.created_at,
                    record.expires_at,
                    None,
                    None,
                ),
            )
            created = True

    logger.info(
        "External approval %s %s for %s (policy=%s/%s price=%s %s "
        "revalidation=%s age=%.3fs ttl=%ss expires_at=%s) -- NOTHING EXECUTED",
        record.approval_id,
        "created" if created else "reused",
        adopted.adopted_product_id,
        record.policy_decision.value,
        record.policy_reason_code.value,
        record.approved_normalized_currency,
        record.approved_normalized_price,
        record.revalidation_id,
        age_seconds,
        ttl,
        record.expires_at,
    )

    return ExternalApprovalRequestResult(
        adopted_product_id=adopted.adopted_product_id,
        approvable=True,
        blocked_reason=None,
        created=created,
        eligibility=evaluation.eligibility,
        eligibility_reason=evaluation.eligibility_reason,
        revalidation_id=evaluation.revalidation_id,
        revalidation_checked_at=evaluation.revalidation_checked_at,
        revalidation_status=evaluation.revalidation_status,
        revalidation_ttl_seconds=ttl,
        revalidation_age_seconds=age_seconds,
        policy_decision=decision.decision,
        policy_reason_code=decision.reason_code,
        current_normalized_price=evaluation.current_normalized_price,
        current_normalized_currency=evaluation.current_normalized_currency,
        approval=record,
    )


# ---------------------------------------------------------------------------
# Resolving an approval
# ---------------------------------------------------------------------------


def _verify_bindings(approval: ExternalApproval, now: datetime, ttl: int) -> None:
    """Re-verify, from scratch, everything the approval committed to.

    Called immediately before PENDING -> APPROVED. Nothing stored on the
    approval is trusted as still true: the product, the snapshot, the price,
    the currency and the engine's ruling are all re-read and compared. A
    mismatch raises ApprovalConflictError naming exactly what moved; it is
    never papered over by re-binding to the new state, because the user
    approved the OLD state and has not seen the new one.

    Raises:
        ApprovalConflictError: any binding condition fails.
    """

    def conflict(code: ApprovalConflict, message: str) -> ApprovalConflictError:
        logger.info(
            "External approval %s refused: %s (%s)",
            approval.approval_id,
            code.value,
            message,
        )
        return ApprovalConflictError(code, message, approval)

    # 5. Re-read the referenced adopted product.
    try:
        adoption_service.get(approval.adopted_product_id)
    except adoption_service.AdoptedProductNotFoundError as exc:
        raise conflict(
            ApprovalConflict.ADOPTED_PRODUCT_MISSING,
            f"Adopted product {approval.adopted_product_id} no longer exists.",
        ) from exc

    # 6/7. Re-read the referenced revalidation BY ID -- the exact row, not
    # whatever is newest.
    snapshot = revalidation_service.get_revalidation(approval.revalidation_id)
    if snapshot is None:
        raise conflict(
            ApprovalConflict.REVALIDATION_MISSING,
            f"Revalidation {approval.revalidation_id} no longer exists.",
        )

    # 8. That exact snapshot must still be a confirmation.
    if snapshot.revalidation_status is not RevalidationStatus.REVALIDATED:
        raise conflict(
            ApprovalConflict.REVALIDATION_NOT_CONFIRMED,
            f"Revalidation {approval.revalidation_id} is "
            f"{snapshot.revalidation_status.value}, not REVALIDATED.",
        )

    # 9. Still within the freshness TTL, by the server's own timestamp.
    checked_at = parse_checked_at(snapshot.checked_at)
    if checked_at is None:
        raise conflict(
            ApprovalConflict.REVALIDATION_TIMESTAMP_UNUSABLE,
            f"Revalidation {approval.revalidation_id} has an unusable "
            f"checked_at, so its age cannot be established.",
        )
    if not is_fresh(checked_at, now, ttl):
        raise conflict(
            ApprovalConflict.REVALIDATION_STALE,
            f"Revalidation {approval.revalidation_id} is "
            f"{(now - checked_at).total_seconds():.0f}s old, beyond the "
            f"{ttl}s freshness limit.",
        )

    # 10. The CURRENT policy state must still be the one the approval bound
    # to -- re-derived through the existing engine, never recomputed here.
    evaluation = external_policy_service.evaluate_adopted_product(
        approval.adopted_product_id
    )
    if evaluation.eligibility is not EligibilityStatus.EVALUATED:
        raise conflict(
            ApprovalConflict.NOT_POLICY_ELIGIBLE,
            f"This product is no longer eligible for evaluation "
            f"({evaluation.eligibility_reason.value}).",
        )

    # The snapshot the approval names must still be the LATEST one. An older
    # snapshot staying valid is not enough: a newer check supersedes it, and
    # Phase 11C's latest-wins rule must not be bypassed from this side.
    if evaluation.revalidation_id != approval.revalidation_id:
        raise conflict(
            ApprovalConflict.REVALIDATION_SUPERSEDED,
            f"A newer revalidation ({evaluation.revalidation_id}) has "
            f"superseded the approved one ({approval.revalidation_id}).",
        )

    if evaluation.current_normalized_currency != approval.approved_normalized_currency:
        raise conflict(
            ApprovalConflict.CURRENCY_CHANGED,
            f"Currency changed from {approval.approved_normalized_currency} "
            f"to {evaluation.current_normalized_currency}.",
        )

    if evaluation.current_normalized_price != approval.approved_normalized_price:
        raise conflict(
            ApprovalConflict.PRICE_CHANGED,
            f"Price changed from {approval.approved_normalized_price} to "
            f"{evaluation.current_normalized_price}.",
        )

    decision = evaluation.policy_decision
    assert decision is not None  # EVALUATED always carries the engine's ruling
    if (
        decision.decision is not approval.policy_decision
        or decision.reason_code is not approval.policy_reason_code
    ):
        raise conflict(
            ApprovalConflict.POLICY_CHANGED,
            f"Policy state changed from "
            f"{approval.policy_decision.value}/{approval.policy_reason_code.value} "
            f"to {decision.decision.value}/{decision.reason_code.value}.",
        )


def verify_bindings(approval: ExternalApproval, now: datetime, ttl: int) -> None:
    """Public entry point to the binding re-verification above (Phase 11E).

    A thin re-export, added so the external PayPal order-creation path can
    re-run EXACTLY these checks rather than writing its own weaker copy.
    The rules themselves live in _verify_bindings and are unchanged: one
    implementation, one place to audit, no second approval gate.

    Raises:
        ApprovalConflictError: any binding condition fails.
    """
    _verify_bindings(approval, now, ttl)


def _terminal_conflict(
    approval: ExternalApproval, approve: bool
) -> Optional[ApprovalConflictError]:
    """The refusal owed to a non-PENDING approval, or None when it is PENDING.

    The lifecycle only runs forward, so every terminal state refuses -- with
    exactly one idempotent exception: approve=true against an ALREADY
    APPROVED record is the same request answered the same way, so it returns
    the existing state rather than erroring, and creates nothing.
    """
    if approval.status is ExternalApprovalStatus.PENDING:
        return None
    if approval.status is ExternalApprovalStatus.APPROVED:
        if approve:
            return None  # idempotent replay; the caller returns it unchanged
        return ApprovalConflictError(
            ApprovalConflict.APPROVAL_ALREADY_APPROVED,
            f"Approval {approval.approval_id} is already APPROVED and cannot "
            f"be reversed. Approval state never moves backward.",
            approval,
        )
    if approval.status is ExternalApprovalStatus.REJECTED:
        return ApprovalConflictError(
            ApprovalConflict.APPROVAL_ALREADY_REJECTED,
            f"Approval {approval.approval_id} was already REJECTED. Request a "
            f"new approval to approve this product.",
            approval,
        )
    return ApprovalConflictError(
        ApprovalConflict.APPROVAL_EXPIRED,
        f"Approval {approval.approval_id} expired at {approval.expires_at}. "
        f"Request a new approval against current evidence.",
        approval,
    )


def _settle(
    approval_id: str, status: ExternalApprovalStatus, timestamp_column: str, now: str
) -> bool:
    """Compare-and-set PENDING -> status. True when THIS caller won.

    The WHERE clause carries the expected status, so the transition is one
    atomic SQL statement rather than a read-then-write. Two concurrent
    resolves therefore cannot both succeed: the loser updates zero rows,
    learns it lost, and reports the winner's outcome instead of overwriting
    it. This is also what stops an expiry performed by a concurrent read
    from being silently undone.
    """
    with _connect() as conn:
        cursor = conn.execute(
            f"""UPDATE external_product_approvals
                SET status = ?, {timestamp_column} = ?
                WHERE approval_id = ? AND status = ?""",
            (
                status.value,
                now,
                approval_id,
                ExternalApprovalStatus.PENDING.value,
            ),
        )
        return cursor.rowcount == 1


def resolve_approval(approval_id: str, approve: bool) -> ExternalApproval:
    """Record the user's explicit decision on one pending approval.

    `approve` is the ONLY user-controlled input. Nothing else about the
    approval can be influenced, because nothing else is accepted.

    approve=True marks the approval APPROVED -- and APPROVED is where this
    phase stops. No PayPal order is created, no payment is captured, no
    refund is issued, no transaction is written, no session is touched and
    no AI is called. That is true even when the bound policy decision is
    ALLOW_AUTONOMOUS_PURCHASE.

    approve=False marks it REJECTED, with equally no side effects.

    Raises:
        ValueError: blank id.
        ApprovalConfigError: the TTL environment variable is unusable.
        ApprovalNotFoundError: no such approval.
        ApprovalConflictError: a terminal status, an expiry, or a binding
            that no longer holds. Nothing is executed and nothing changes.
    """
    identifier = (approval_id or "").strip()
    if not identifier:
        raise ValueError("approval_id must be a non-empty string")

    ttl = revalidation_ttl_seconds()

    # 1-4. Load it, and let get() apply any due expiry first, so an expired
    # PENDING record can never be approved.
    approval = get(identifier)

    terminal = _terminal_conflict(approval, approve)
    if terminal is not None:
        raise terminal
    if approval.status is ExternalApprovalStatus.APPROVED:
        # Idempotent replay of approve=true. No second approval is created.
        logger.info(
            "External approval %s already APPROVED; returning existing state",
            approval.approval_id,
        )
        return approval

    now = _now_dt()

    if not approve:
        # Rejection needs no binding re-verification: declining a stale
        # offer is still a valid decline, and it authorizes nothing.
        if not _settle(
            approval.approval_id,
            ExternalApprovalStatus.REJECTED,
            "rejected_at",
            now.isoformat(),
        ):
            return _lost_race(approval.approval_id, approve)
        logger.info(
            "External approval %s REJECTED by explicit user decision", approval.approval_id
        )
        return get(approval.approval_id)

    # 5-10. Every binding re-verified against current server state.
    _verify_bindings(approval, now, ttl)

    # 11. Only now.
    if not _settle(
        approval.approval_id,
        ExternalApprovalStatus.APPROVED,
        "approved_at",
        now.isoformat(),
    ):
        return _lost_race(approval.approval_id, approve)

    settled = get(approval.approval_id)
    logger.info(
        "External approval %s APPROVED by explicit user decision "
        "(product=%s revalidation=%s policy=%s price=%s %s). "
        "NO PayPal order, NO capture, NO transaction, NO refund -- "
        "Phase 11D ends here.",
        settled.approval_id,
        settled.adopted_product_id,
        settled.revalidation_id,
        settled.policy_decision.value,
        settled.approved_normalized_currency,
        settled.approved_normalized_price,
    )
    return settled


def _lost_race(approval_id: str, approve: bool) -> ExternalApproval:
    """Report the winner's outcome after losing a compare-and-set.

    Re-reads the record and applies the same terminal rules, so a concurrent
    resolve produces exactly one of: the winner's APPROVED state (when both
    callers asked to approve) or a deterministic conflict. Never two
    approvals, and never a reversal.
    """
    current = get(approval_id)
    logger.info(
        "External approval %s was resolved concurrently; status is %s",
        approval_id,
        current.status.value,
    )
    terminal = _terminal_conflict(current, approve)
    if terminal is not None:
        raise terminal
    return current
