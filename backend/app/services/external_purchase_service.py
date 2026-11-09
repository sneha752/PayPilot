"""APPROVED external product -> server-side PayPal order creation (11E).

    APPROVED external approval
          |
          v
    [ re-read ]   the approval, by id, from the server's own store
    [ re-read ]   the exact adopted product it names
    [ re-read ]   the exact revalidation snapshot it names
    [ re-verify ] every Phase 11D binding, from scratch
    [ re-verify ] product identity is still durable and still confirmed
    [ derive ]    the amount from that snapshot's OBSERVED price alone
    [ reserve ]   a UNIQUE row on approval_id, committed first
          |
          v
    paypal.create_order(...)        <- the EXISTING client, unmodified
          |
          v
    ORDER_CREATED, awaiting the BUYER's approval at PayPal

    And order CREATION stops there: the order awaits the buyer, and
    create_order_for_approval never captures anything.

    Phase 11F-C then VERIFIES the buyer's approval against PayPal's own
    answer (BUYER_APPROVED), and Phase 11F-D adds the only path in this
    system that captures an external order:

    BUYER_APPROVED
          |
          v
    [ authorize ] an EXPLICIT PayPilot capture authorization, with a
                  deadline of its own -- see authorize_capture
          |
          v
    [ re-read ]   the order, still APPROVED, still that id, still exactly
                  the recorded amount and currency
          |
          v
    [ reserve ]   CAPTURING, committed BEFORE PayPal is contacted
          |
          v
    paypal.capture_order(...)      <- the EXISTING client, unmodified
          |
          v
    CAPTURED / CAPTURE_PENDING / CAPTURE_DECLINED / CAPTURE_FAILED

    capture_order is imported and called ONLY from that path, once per
    execution, under a compare-and-set. refund_capture is still NOT
    imported and no refund path exists: external refunds do not exist in
    any phase so far, and the catalog refund route refuses an external
    capture id.

THE CLIENT NEVER SUPPLIES MONEY. NEITHER DOES AN LLM.
    The only input to this module is an approval_id. There is no parameter,
    no keyword argument and no request model anywhere in this path for a
    price, an amount, a currency, a merchant, a product URL, a title, a
    product id, a revalidation id, a policy decision, a PayPal order id or
    a capture id. Those values are not filtered out; there is nowhere to
    put them. No AI provider is imported.

WHERE THE AMOUNT COMES FROM, AND WHY NOT FROM THE NORMALIZED PRICE
    An approval binds a revalidation_id, and revalidation history is
    append-only, so that one row still holds exactly what the provider said
    at the moment the user approved -- including its OWN observed price and
    currency (e.g. USD 19.99) next to Phase 10F's normalized comparison
    figure (e.g. INR 1824.09).

    The PayPal amount is the OBSERVED pair. The reasons, in order:

      1. It is the provider's actual asking price, in the provider's own
         currency. The normalized figure is arithmetic PayPilot performed on
         it for budget comparison, and discovery_currency_service states
         outright that such derived prices "may never become a PayPal,
         capture, or refund amount".
      2. Reversing the 10F conversion (INR -> USD) would need a rate, and
         the two rate sources in this system do not agree: discovery
         normalizes at the configured discovery provider's rate (91.25
         INR/USD in the demo configuration) while currency_service's
         catalog path uses DEMO_INR_PER_USD (90). Round-tripping through
         either would charge a number that is not the approved one, and
         picking one would create a second FX mechanism. So no conversion
         happens here at all -- this module imports neither currency
         service and defines no rate.

    The approved normalized price is still the policy figure and still
    fully enforced: Phase 11D's re-verification requires it to be unchanged
    before this module proceeds. The two are reported side by side in the
    response so they can never be mistaken for one another.

CURRENCY SUPPORT IS AN EXPLICIT ALLOWLIST, AND IT FAILS CLOSED
    The Policy Engine evaluating a normalized INR price says nothing about
    what PayPal can charge. SUPPORTED_PAYPAL_CURRENCIES names the
    currencies the existing integration demonstrably charges; anything else
    is a deterministic UNSUPPORTED_PAYPAL_CURRENCY refusal, never a silent
    conversion at an invented rate.

AT MOST ONE PAYPAL CALL PER APPROVAL -- AND AN HONEST LIMIT
    approval_id is UNIQUE in external_purchase_orders, and the row is
    INSERTed and COMMITTED as a CREATING reservation BEFORE PayPal is
    contacted. A concurrent second request therefore loses the insert and
    refuses; it never reaches create_order. A later request finds
    ORDER_CREATED and returns that same order without calling PayPal again.

    Exactly-once execution is NOT claimed, and cannot be: PayPal is an
    external system, and a create-order call that times out may still have
    created an order upstream. What this module guarantees is at-most-once
    ATTEMPT -- a FAILED execution is terminal and never retried, precisely
    so an upstream success we never heard about cannot be duplicated. The
    cost is that a transient PayPal failure permanently ends that
    approval's execution; the user requests a new approval, against fresh
    evidence, which is the safe direction. See the module-level note on
    PayPal-Request-Id in the closeout documentation.

FOLLOWS THE LEDGER'S CONVENTIONS, IN ITS OWN TABLE
    stdlib sqlite3, no ORM, parameterized SQL, the same
    backend/data/paypilot.db and PAYPILOT_DB_PATH test isolation. A
    DEDICATED table, not the transactions ledger: that schema is typed to
    the local catalog (NOT NULL catalog product id and INTEGER
    catalog_price_inr) and representing an external purchase there would
    erase the trust distinction between a catalog price and a re-verified
    provider observation. The existing ledger is neither read nor written
    by this module.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

from ..paypal import (
    PayPalAlreadyCapturedError,
    PayPalAuthError,
    PayPalCaptureError,
    PayPalCheckoutConfigError,
    PayPalGetOrderError,
    PayPalOrderError,
    PayPalOrderNetworkError,
    PayPalOrderNotApprovedError,
    PayPalOrderNotFoundError,
    capture_order,
    checkout_urls,
    classify_recovery_window,
    create_order,
    get_order,
    validate_paypal_request_id,
)
from ..schemas.adoption import AdoptedProduct, AdoptedProductStatus
from ..schemas.external_approval import ExternalApproval, ExternalApprovalStatus
from ..schemas.external_purchase import (
    ExternalApprovalVerification,
    ExternalApprovalVerificationFailure,
    ExternalCaptureAuthorization,
    ExternalCaptureFailure,
    ExternalCaptureResult,
    ExternalOrderConflict,
    ExternalOrderResult,
    ExternalOrderStatus,
    ExternalPurchaseOrder,
)
from ..schemas.revalidation import IdentityMatch, RevalidationSnapshot
from . import (
    adoption_service,
    external_approval_service,
    product_identity,
    revalidation_service,
)

logger = logging.getLogger("paypilot.external_purchase")

#: The currencies this system may ask PayPal to charge for an external
#: product. USD and USD only: it is the single currency the existing PayPal
#: integration has ever charged (every catalog purchase is converted to USD
#: before create_order, and the Sandbox checkout runs in USD), so it is the
#: only one we have evidence works end to end.
#:
#: Deliberately a hard-coded constant with NO environment override. An
#: override would be a way to authorize charging a currency the integration
#: cannot actually settle, which is exactly the failure this allowlist
#: exists to prevent. Widening it is a code change, reviewed as such.
SUPPORTED_PAYPAL_CURRENCIES = frozenset({"USD"})

#: PayPal amount values carry exactly two decimal places. Same convention as
#: currency_service (quantize to 0.01, ROUND_HALF_UP) -- reused as a FORMAT
#: here, never as a rounding licence: see _paypal_amount, which refuses any
#: price that is not already exact at two places rather than charging a
#: rounded number nobody approved.
_TWO_PLACES = Decimal("0.01")

#: PayPal caps a purchase-unit description at 127 characters. We assemble
#: within the cap ourselves so the value sent is deterministic rather than
#: whatever survives the client's truncation.
MAX_DESCRIPTION_LENGTH = 127

_DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "paypilot.db"

# approval_id is UNIQUE: that constraint is the single-execution guarantee.
# paypal_amount is TEXT holding the exact string sent to PayPal -- SQLite's
# REAL would turn an exact decimal into a binary float. There is no
# paypal_capture_id, refund_id or transaction_id column: those things do not
# exist in this phase, so a forged value has nowhere to land.
#
# paypal_approve_url (Phase 11F-B) stores PayPal's OWN hosted-checkout href
# for the created order, so a replay can hand back a usable link instead of
# a bare order id. It is written once, from the create-order response, and
# is never assembled from the order id.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS external_purchase_orders (
    execution_id        TEXT PRIMARY KEY,
    approval_id         TEXT NOT NULL UNIQUE,
    adopted_product_id  TEXT NOT NULL,
    revalidation_id     TEXT NOT NULL,
    paypal_order_id     TEXT UNIQUE,
    paypal_amount       TEXT,
    paypal_currency     TEXT,
    paypal_approve_url  TEXT,
    capture_authorized_at TEXT,
    paypal_capture_id     TEXT UNIQUE,
    paypal_capture_status TEXT,
    captured_amount       TEXT,
    captured_currency     TEXT,
    paypal_request_id     TEXT,
    recovery_claimed_at   TEXT,
    recovery_claim_token  TEXT,
    status              TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_external_orders_product
    ON external_purchase_orders (adopted_product_id, status);
"""

#: Created separately from _SCHEMA so a database that predates Phase 11F-D
#: gains it after _migrate has added the column. One capture id can belong
#: to at most one execution.
_CAPTURE_ID_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_external_orders_capture
    ON external_purchase_orders (paypal_capture_id)
    WHERE paypal_capture_id IS NOT NULL
"""

_COLUMNS = (
    "execution_id",
    "approval_id",
    "adopted_product_id",
    "revalidation_id",
    "paypal_order_id",
    "paypal_amount",
    "paypal_currency",
    "paypal_approve_url",
    "capture_authorized_at",
    "paypal_capture_id",
    "paypal_capture_status",
    "captured_amount",
    "captured_currency",
    "status",
    "created_at",
    "updated_at",
)

#: Columns added after the table shipped, as {name: SQL type}. CREATE TABLE
#: IF NOT EXISTS does nothing to a table that already exists, so a database
#: created before Phase 11F-B needs the new column added explicitly.
_ADDED_COLUMNS = {
    "paypal_approve_url": "TEXT",
    # Phase 11F-D. Deliberately added without UNIQUE on paypal_capture_id:
    # SQLite cannot add a UNIQUE column with ALTER TABLE, so the constraint
    # is created separately as a unique INDEX, which an existing database
    # can gain. New databases get both.
    "capture_authorized_at": "TEXT",
    "paypal_capture_id": "TEXT",
    "paypal_capture_status": "TEXT",
    "captured_amount": "TEXT",
    "captured_currency": "TEXT",
    # Phase 11F-K.22a. The stable PayPal-Request-Id sent on create_order for
    # this reservation, persisted at reservation time and never overwritten.
    "paypal_request_id": "TEXT",
    # Phase 11F-K.22b. The durable single-flight claim for operator-gated
    # create-order replay recovery. Nullable; never part of a reservation
    # INSERT.
    "recovery_claimed_at": "TEXT",
    # Phase 11F-K.22b ownership guard. A unique token per successful claim,
    # so release_recovery_claim only clears the caller's own claim.
    "recovery_claim_token": "TEXT",
}

#: Columns the reservation INSERT writes IN ADDITION to _COLUMNS. _COLUMNS is
#: deliberately left unchanged (several tests build value tuples from it); the
#: request id is written through this extended list instead.
_RESERVE_COLUMNS = _COLUMNS + ("paypal_request_id",)


class ExternalOrderConflictError(Exception):
    """Raised when no PayPal order may be created for this approval.

    Carries the machine-readable code and, when one exists, the execution
    record that explains the refusal, so the endpoint can answer with a
    deterministic 409 naming exactly what blocked it. Nothing was created
    and nothing was repaired.
    """

    def __init__(
        self,
        code: ExternalOrderConflict,
        message: str,
        execution: Optional[ExternalPurchaseOrder] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.execution = execution


# ---------------------------------------------------------------------------
# Clock and storage plumbing
# ---------------------------------------------------------------------------


def _now_dt() -> datetime:
    """The server's own UTC clock. The only clock this module consults."""
    return datetime.now(timezone.utc)


def _db_path() -> Path:
    env = os.getenv("PAYPILOT_DB_PATH", "").strip()
    return Path(env) if env else _DEFAULT_DB


def _migrate(conn: sqlite3.Connection) -> None:
    """Add any column the table predates. Additive and idempotent.

    An existing database is not recreated by CREATE TABLE IF NOT EXISTS, so
    a row written before Phase 11F-B lives in a table with no
    paypal_approve_url column. Adding it leaves every existing row intact
    with a NULL link, which is exactly the state the recovery path is built
    to handle. No column is ever dropped, renamed or retyped here.
    """
    present = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(external_purchase_orders)")
    }
    for column, sql_type in _ADDED_COLUMNS.items():
        if column not in present:
            conn.execute(
                f"ALTER TABLE external_purchase_orders ADD COLUMN {column} {sql_type}"
            )
            logger.info(
                "external_purchase_orders: added missing column %s", column
            )
    # After any missing column exists, the capture-id uniqueness index can
    # be created on an old database as well as a new one.
    conn.execute(_CAPTURE_ID_INDEX)


@contextmanager
def _connect():
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _to_record(row: sqlite3.Row) -> ExternalPurchaseOrder:
    return ExternalPurchaseOrder(**dict(row))


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get_by_approval(approval_id: str) -> Optional[ExternalPurchaseOrder]:
    """The execution record for one approval, or None. Read-only."""
    identifier = (approval_id or "").strip()
    if not identifier:
        raise ValueError("approval_id must be a non-empty string")
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM external_purchase_orders WHERE approval_id = ?",
            (identifier,),
        ).fetchone()
    return None if row is None else _to_record(row)


def get(execution_id: str) -> Optional[ExternalPurchaseOrder]:
    """One execution record by its own id, or None. Read-only."""
    identifier = (execution_id or "").strip()
    if not identifier:
        raise ValueError("execution_id must be a non-empty string")
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM external_purchase_orders WHERE execution_id = ?",
            (identifier,),
        ).fetchone()
    return None if row is None else _to_record(row)


def get_by_paypal_order(paypal_order_id: str) -> Optional[ExternalPurchaseOrder]:
    """One execution record by the PayPal order id it created, or None.

    Read-only, and added in Phase 11F-B0 for exactly one purpose: letting
    order_ownership_service recognise an EXTERNAL order id so the
    catalog-only capture routes can REFUSE it instead of capturing it
    through a path typed to the local catalog. Nothing in this module
    consumes it, and it creates no capture or refund capability.

    A CREATING reservation has no PayPal order id yet (the column is NULL
    until PayPal answers), so it cannot be matched here; such an id
    therefore resolves to UNKNOWN upstream, which is also a refusal.
    """
    identifier = (paypal_order_id or "").strip()
    if not identifier:
        raise ValueError("paypal_order_id must be a non-empty string")
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM external_purchase_orders WHERE paypal_order_id = ?",
            (identifier,),
        ).fetchone()
    return None if row is None else _to_record(row)


def count() -> int:
    """How many execution records exist. Tests and diagnostics."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM external_purchase_orders"
        ).fetchone()
    return int(row["n"])


#: External states that need operator attention (Phase 11F-I): a capture is
#: in flight (CAPTURING), accepted-but-unsettled (CAPTURE_PENDING), or failed
#: with an outcome that could not be cleanly classified (CAPTURE_FAILED).
#: Catalog transactions live in a separate ledger and are never returned by
#: the external recovery query.
UNRESOLVED_STATUSES = (
    ExternalOrderStatus.CAPTURING,
    ExternalOrderStatus.CAPTURE_PENDING,
    ExternalOrderStatus.CAPTURE_FAILED,
)


def list_by_statuses(
    statuses, limit: int = 50
) -> list[ExternalPurchaseOrder]:
    """Read-only triage query: executions in the given statuses, bounded.

    Parameterized `status IN (...)` with a stable, deterministic ordering
    (oldest-first, so the most stale unresolved execution surfaces first).
    Returns whole records; the route projects only triage-safe fields. It
    never writes and never calls PayPal.

    Args:
        statuses: a non-empty iterable of ExternalOrderStatus. An empty
            iterable returns [] (there is nothing to match).
        limit: maximum rows, 1..100 (default 50); never silently clamped.

    Raises:
        ValueError: limit is outside 1..100.
    """
    status_values = [s.value for s in statuses]
    if not status_values:
        return []
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    placeholders = ", ".join("?" * len(status_values))
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT * FROM external_purchase_orders
                WHERE status IN ({placeholders})
                ORDER BY created_at ASC, execution_id ASC
                LIMIT ?""",
            (*status_values, limit),
        ).fetchall()
    return [_to_record(row) for row in rows]


#: How old a CREATING reservation must be before operator triage treats it as
#: STALE (Phase 11F-K.17). CREATING means "a create-order call is in flight or
#: its outcome was never learned"; a reservation younger than this is very
#: likely an in-flight call, not a stuck orphan, so it is NOT surfaced -- that
#: keeps the triage list free of healthy, momentarily-creating rows. 900s (15
#: min) is far longer than the 15s create_order timeout plus any realistic
#: retry/settlement, so anything older is genuinely stuck.
DEFAULT_CREATING_STALE_SECONDS = 900

CREATING_STALE_ENV_VAR = "EXTERNAL_CREATING_STALE_SECONDS"


def creating_stale_seconds() -> int:
    """The configured CREATING staleness threshold, in seconds.

    Read from EXTERNAL_CREATING_STALE_SECONDS when set to a positive integer;
    otherwise the clearly-defined default. A misconfigured value does NOT
    break the read-only triage list -- it logs a warning and falls back to the
    default, because an operator's visibility tool failing closed to a 500
    would hide exactly the stuck records it exists to show.
    """
    raw = os.getenv(CREATING_STALE_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_CREATING_STALE_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "%s is not an integer (%r); using default %ss",
            CREATING_STALE_ENV_VAR,
            raw,
            DEFAULT_CREATING_STALE_SECONDS,
        )
        return DEFAULT_CREATING_STALE_SECONDS
    if value <= 0:
        logger.warning(
            "%s must be positive (got %s); using default %ss",
            CREATING_STALE_ENV_VAR,
            value,
            DEFAULT_CREATING_STALE_SECONDS,
        )
        return DEFAULT_CREATING_STALE_SECONDS
    return value


def list_stale_creating(
    limit: int = 50,
    *,
    now: Optional[datetime] = None,
    grace_seconds: Optional[int] = None,
) -> list[ExternalPurchaseOrder]:
    """Read-only triage query: CREATING reservations older than the grace period.

    A CREATING row is "stale" when its created_at is at or before
    now - grace_seconds. Newer reservations are excluded so an in-flight
    create-order call is never mistaken for a stuck orphan. Ordered oldest-
    first and bounded, like list_by_statuses. It never writes and never calls
    PayPal.

    Args:
        limit: maximum rows, 1..100 (default 50); never silently clamped.
        now: override clock (tests); defaults to the server's UTC clock.
        grace_seconds: override threshold (tests); defaults to
            creating_stale_seconds().

    Raises:
        ValueError: limit is outside 1..100.
    """
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    current = now if now is not None else _now_dt()
    grace = grace_seconds if grace_seconds is not None else creating_stale_seconds()
    cutoff = (current - timedelta(seconds=grace)).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM external_purchase_orders
               WHERE status = ? AND created_at <= ?
               ORDER BY created_at ASC, execution_id ASC
               LIMIT ?""",
            (ExternalOrderStatus.CREATING.value, cutoff, limit),
        ).fetchall()
    return [_to_record(row) for row in rows]


# ---------------------------------------------------------------------------
# The amount. The most security-sensitive function in this phase.
# ---------------------------------------------------------------------------


def paypal_amount_from_snapshot(
    snapshot: RevalidationSnapshot,
) -> tuple[str, str]:
    """Derive (amount, currency) for PayPal from ONE revalidation snapshot.

    Reads exactly two fields: current_observed_price and
    current_observed_currency -- the provider's own asking price in the
    provider's own currency, from the append-only row the approval is bound
    to. Note what this signature CANNOT reach: it takes no adopted product,
    no discovery evidence, no approval, no client value and no AI output, so
    none of them can influence the amount.

    The normalized price is NOT consulted and NOT converted. See the module
    docstring for why reversing Phase 10F would invent a rate.

    Raises:
        ExternalOrderConflictError:
            OBSERVED_PRICE_UNAVAILABLE  - the snapshot carries no observed
                price/currency. Nothing is substituted for it.
            UNSUPPORTED_PAYPAL_CURRENCY - the observed currency is not in
                SUPPORTED_PAYPAL_CURRENCIES. No rate is invented.
            AMOUNT_NOT_REPRESENTABLE    - the price is not finite, not
                positive, or carries more precision than a PayPal amount
                can state exactly.
    """
    price = snapshot.current_observed_price
    currency = (snapshot.current_observed_currency or "").strip().upper()

    if price is None or not currency:
        raise ExternalOrderConflictError(
            ExternalOrderConflict.OBSERVED_PRICE_UNAVAILABLE,
            f"Revalidation {snapshot.revalidation_id} carries no "
            f"provider-observed price and currency, so there is no amount "
            f"to charge. The normalized comparison price is not a payment "
            f"amount and is not substituted.",
        )

    if currency not in SUPPORTED_PAYPAL_CURRENCIES:
        raise ExternalOrderConflictError(
            ExternalOrderConflict.UNSUPPORTED_PAYPAL_CURRENCY,
            f"The approved product is priced in {currency}, which this "
            f"PayPal integration does not charge (supported: "
            f"{', '.join(sorted(SUPPORTED_PAYPAL_CURRENCIES))}). No "
            f"exchange rate is applied: converting here would charge an "
            f"amount nobody approved.",
        )

    # Exact Decimal throughout; no float ever touches this value.
    if not price.is_finite() or price <= 0:
        raise ExternalOrderConflictError(
            ExternalOrderConflict.AMOUNT_NOT_REPRESENTABLE,
            f"Observed price {price} is not a finite amount greater than "
            f"zero, so it cannot be charged.",
        )

    quantized = price.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    if quantized != price:
        # e.g. 19.999 -> 20.00 would charge a cent nobody approved. The
        # approved state must be payable exactly, or not at all.
        raise ExternalOrderConflictError(
            ExternalOrderConflict.AMOUNT_NOT_REPRESENTABLE,
            f"Observed price {price} cannot be stated exactly as a "
            f"{currency} PayPal amount (two decimal places); it would have "
            f"to be rounded to {quantized}, which is not the approved "
            f"amount.",
        )

    return f"{quantized:.2f}", currency


def order_description(adopted: AdoptedProduct, approval: ExternalApproval) -> str:
    """Build PayPal's purchase-unit description from trusted server data.

    Deterministic and never AI-written: the adopted record's own title, the
    merchant name the approval bound from fresh provider evidence, and
    PayPilot's adopted_product_id for reconciliation. No client text and no
    model output can reach it.

    Assembled to fit MAX_DESCRIPTION_LENGTH here, truncating the TITLE
    rather than the identifier, so the value sent is predictable instead of
    whatever survives the PayPal client's own 127-character cut.
    """
    suffix = f" (PayPilot {adopted.adopted_product_id})"

    head = adopted.title.strip()
    merchant = (approval.merchant_name or "").strip()
    if merchant:
        head = f"{head} from {merchant}"

    room = MAX_DESCRIPTION_LENGTH - len(suffix)
    if room <= 0:  # pragma: no cover - the id is a fixed 38-char handle
        return suffix.strip()[:MAX_DESCRIPTION_LENGTH]
    return f"{head[:room].rstrip()}{suffix}"


# ---------------------------------------------------------------------------
# Validation: everything the approval committed to, re-read and re-verified
# ---------------------------------------------------------------------------


def _approval_status_conflict(
    approval: ExternalApproval,
) -> Optional[ExternalOrderConflictError]:
    """The refusal owed to a non-APPROVED approval, or None when APPROVED."""
    if approval.status is ExternalApprovalStatus.APPROVED:
        return None
    if approval.status is ExternalApprovalStatus.PENDING:
        return ExternalOrderConflictError(
            ExternalOrderConflict.APPROVAL_NOT_APPROVED,
            f"Approval {approval.approval_id} is still PENDING. An order "
            f"may only be created for an approval a user explicitly "
            f"approved.",
        )
    if approval.status is ExternalApprovalStatus.REJECTED:
        return ExternalOrderConflictError(
            ExternalOrderConflict.APPROVAL_REJECTED,
            f"Approval {approval.approval_id} was explicitly REJECTED by "
            f"the user.",
        )
    return ExternalOrderConflictError(
        ExternalOrderConflict.APPROVAL_EXPIRED,
        f"Approval {approval.approval_id} expired at {approval.expires_at}.",
    )


def _verify_identity(
    adopted: AdoptedProduct, snapshot: RevalidationSnapshot
) -> None:
    """Re-verify that this is still a product we can re-identify at all.

    Two independent facts, both re-derived rather than trusted:

      1. The provider's id is still classified as DURABLE identity by
         product_identity. A provider downgraded to observation-only must
         not be charged for, because nothing could confirm what was bought.
      2. The bound snapshot confirmed the product by EXACT external id, and
         the durable key it matched against is still the adopted record's
         current key.

    Raises:
        ExternalOrderConflictError: PRODUCT_IDENTITY_INVALID.
    """
    identity = product_identity.classify(
        adopted.discovery_source, adopted.discovery_external_id
    )

    def invalid(detail: str) -> ExternalOrderConflictError:
        return ExternalOrderConflictError(
            ExternalOrderConflict.PRODUCT_IDENTITY_INVALID,
            f"Product identity for {adopted.adopted_product_id} is not "
            f"valid for purchase: {detail}",
        )

    if not identity.is_durable:
        raise invalid(
            f"{adopted.discovery_source} provides only "
            f"{identity.kind.value} for this product, which cannot be "
            f"re-identified later."
        )
    if snapshot.identity_match is not IdentityMatch.EXACT_EXTERNAL_ID:
        raise invalid(
            f"the approved revalidation matched by "
            f"{snapshot.identity_match.value}, not an exact external id."
        )
    if snapshot.product_identity_key != identity.product_identity_key:
        raise invalid(
            "the approved revalidation was matched against a different "
            "durable identity key than the adopted record now carries."
        )


def _verify_purchasable(adopted: AdoptedProduct) -> None:
    """The adopted record's lifecycle must still admit a purchase.

    DISABLED is an operator decision and STALE means the provider stopped
    serving the product; a successful revalidation sets ACTIVE, so anything
    else here contradicts the evidence the approval is bound to. Defence in
    depth behind Phase 11C's own DISABLED gate.

    Raises:
        ExternalOrderConflictError: PRODUCT_NOT_PURCHASABLE.
    """
    if adopted.status is not AdoptedProductStatus.ACTIVE:
        raise ExternalOrderConflictError(
            ExternalOrderConflict.PRODUCT_NOT_PURCHASABLE,
            f"Adopted product {adopted.adopted_product_id} is "
            f"{adopted.status.value}, not ACTIVE, so it is not eligible "
            f"for purchase.",
        )


def _verify_approved_state(
    approval_id: str,
) -> tuple[ExternalApproval, AdoptedProduct, RevalidationSnapshot]:
    """Re-read and re-verify EVERYTHING before any money path is touched.

    Reads the approval, the exact adopted product and the exact revalidation
    snapshot it names, and re-runs Phase 11D's own binding verification --
    the same function the resolve endpoint uses, not a weaker copy. Nothing
    stored on the approval is trusted as still true.

    Checks, in order:
      1-2. the approval exists and its status is APPROVED
      3.   it has not outlived its TTL (enforced for APPROVED too)
      4-12,15. Phase 11D's verify_bindings: the adopted product still
           exists; the bound revalidation still exists BY ID, is still
           REVALIDATED, is still fresh, and is still the LATEST one (not
           superseded); the product is still policy-eligible; and the
           current price, currency, policy decision and reason code still
           equal the approval's bindings
      13.  product identity is still durable and was confirmed exactly
      14.  the adopted record is still ACTIVE

    Raises:
        ValueError: blank id.
        ApprovalNotFoundError: no such approval.
        ApprovalConfigError: the TTL environment variable is unusable.
        ExternalOrderConflictError: any condition above fails. No PayPal
            call is made, and no execution record is written.
    """
    identifier = (approval_id or "").strip()
    if not identifier:
        raise ValueError("approval_id must be a non-empty string")

    ttl = external_approval_service.revalidation_ttl_seconds()

    # 1-2. The approval, re-read from the server's own store. get() applies
    # any due PENDING -> EXPIRED transition first.
    approval = external_approval_service.get(identifier)
    status_conflict = _approval_status_conflict(approval)
    if status_conflict is not None:
        raise status_conflict

    now = _now_dt()

    # 3. An APPROVED record never self-expires -- its status is terminal --
    # so the deadline is enforced here explicitly. Approving fresh evidence
    # does not freeze it: expires_at is revalidation_checked_at + TTL, and
    # an order may only be created while that still holds.
    if now >= datetime.fromisoformat(approval.expires_at):
        raise ExternalOrderConflictError(
            ExternalOrderConflict.APPROVAL_EXPIRED,
            f"Approval {approval.approval_id} was approved against evidence "
            f"that expired at {approval.expires_at}. Request a new approval "
            f"against current evidence.",
        )

    # 4-12, 15. Phase 11D's own re-verification, reused verbatim.
    try:
        external_approval_service.verify_bindings(approval, now, ttl)
    except external_approval_service.ApprovalConflictError as exc:
        try:
            code = ExternalOrderConflict(exc.code.value)
        except ValueError:  # pragma: no cover - every reachable code maps
            code = ExternalOrderConflict.APPROVAL_BINDING_CHANGED
        raise ExternalOrderConflictError(code, exc.message) from exc

    # The two rows the verification just vouched for, re-read for the
    # amount and the description. Both are exact-id reads, not "latest".
    adopted = adoption_service.get(approval.adopted_product_id)
    snapshot = revalidation_service.get_revalidation(approval.revalidation_id)
    if snapshot is None:  # pragma: no cover - verify_bindings proved it exists
        raise ExternalOrderConflictError(
            ExternalOrderConflict.REVALIDATION_MISSING,
            f"Revalidation {approval.revalidation_id} no longer exists.",
        )

    # 13-14. The checks this phase adds on top of Phase 11D.
    _verify_identity(adopted, snapshot)
    _verify_purchasable(adopted)

    return approval, adopted, snapshot


# ---------------------------------------------------------------------------
# The reservation: how two concurrent requests cannot both call PayPal
# ---------------------------------------------------------------------------


def _reserve(
    approval: ExternalApproval, now: str
) -> tuple[Optional[ExternalPurchaseOrder], Optional[ExternalPurchaseOrder]]:
    """Claim this approval's single execution slot.

    Returns (reservation, existing): exactly one is non-None.

    The INSERT is committed BEFORE PayPal is contacted, and approval_id is
    UNIQUE, so this is a compare-and-set on "has anyone started executing
    this approval". The loser of the race gets `existing` and must not call
    PayPal; the database constraint does the arbitration, so no read-then-
    write window exists for both callers to pass through.
    """
    execution_id = f"xord_{uuid.uuid4().hex}"
    # Phase 11F-K.22a: the stable PayPal-Request-Id for THIS execution's single
    # create_order call, derived from its own unique execution id (never from a
    # product id, amount, or any value shared across purchases) and validated
    # against the provider's header constraints. Persisted at reservation time,
    # before PayPal is contacted, and never overwritten afterwards.
    request_id = validate_paypal_request_id(execution_id)
    record = ExternalPurchaseOrder(
        execution_id=execution_id,
        approval_id=approval.approval_id,
        adopted_product_id=approval.adopted_product_id,
        revalidation_id=approval.revalidation_id,
        paypal_order_id=None,
        paypal_amount=None,
        paypal_currency=None,
        paypal_approve_url=None,
        capture_authorized_at=None,
        paypal_capture_id=None,
        paypal_capture_status=None,
        captured_amount=None,
        captured_currency=None,
        paypal_request_id=request_id,
        status=ExternalOrderStatus.CREATING,
        created_at=now,
        updated_at=now,
    )
    with _connect() as conn:
        try:
            conn.execute(
                f"""INSERT INTO external_purchase_orders
                    ({", ".join(_RESERVE_COLUMNS)})
                    VALUES ({", ".join("?" * len(_RESERVE_COLUMNS))})""",
                (
                    record.execution_id,
                    record.approval_id,
                    record.adopted_product_id,
                    record.revalidation_id,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    record.status.value,
                    record.created_at,
                    record.updated_at,
                    record.paypal_request_id,
                ),
            )
        except sqlite3.IntegrityError:
            # Someone already holds this approval's slot. Re-read THEIR row;
            # never insert a second one and never contact PayPal.
            row = conn.execute(
                "SELECT * FROM external_purchase_orders WHERE approval_id = ?",
                (approval.approval_id,),
            ).fetchone()
            if row is None:  # pragma: no cover - UNIQUE was the only cause
                raise
            return None, _to_record(row)
    return record, None


def _settle(
    execution_id: str,
    status: ExternalOrderStatus,
    now: str,
    *,
    paypal_order_id: Optional[str] = None,
    paypal_amount: Optional[str] = None,
    paypal_currency: Optional[str] = None,
    paypal_approve_url: Optional[str] = None,
) -> ExternalPurchaseOrder:
    """Move a CREATING reservation to its terminal state.

    Guarded on status = CREATING so a settle can never overwrite a record
    that already reached ORDER_CREATED or FAILED.
    """
    with _connect() as conn:
        conn.execute(
            """UPDATE external_purchase_orders
               SET status = ?, paypal_order_id = ?, paypal_amount = ?,
                   paypal_currency = ?, paypal_approve_url = ?,
                   updated_at = ?
               WHERE execution_id = ? AND status = ?""",
            (
                status.value,
                paypal_order_id,
                paypal_amount,
                paypal_currency,
                paypal_approve_url,
                now,
                execution_id,
                ExternalOrderStatus.CREATING.value,
            ),
        )
    settled = get(execution_id)
    assert settled is not None  # we just wrote it
    return settled


class ExternalOrderPersistenceError(Exception):
    """The PayPal order id could not be recorded on its CREATING reservation.

    Phase 11F-K.16. Raised ONLY after create_order already returned a real,
    non-empty order id, when the guarded claim neither updated exactly one
    CREATING/NULL-order-id row nor found that row already carrying this exact
    id. Failing closed here is deliberate and conservative: a real PayPal
    order EXISTS, so the reservation is left CREATING (never marked FAILED,
    which would hide the order, and never settled on an inconsistent row), and
    no second order is ever created. The order id is carried so the condition
    is actionable by recovery/operators.
    """

    def __init__(
        self,
        execution_id: str,
        paypal_order_id: str,
        record: Optional[ExternalPurchaseOrder] = None,
    ) -> None:
        self.execution_id = execution_id
        self.paypal_order_id = paypal_order_id
        self.record = record
        super().__init__(
            f"Execution {execution_id} could not record its PayPal order id on "
            f"the CREATING reservation; the reservation is left for recovery "
            f"and no second order will be created."
        )


def _claim_order_id(
    execution_id: str, order_id: str, now: str
) -> tuple[bool, Optional[ExternalPurchaseOrder]]:
    """Persist the PayPal order id on the still-CREATING reservation.

    Phase 11F-K.16. This is the committed write that closes the definite
    orphan window: once create_order returns a real id, that id is written to
    the row BEFORE the final settlement, so a crash after this point leaves a
    CREATING record that still names the order it created.

    The update is guarded so it can ONLY fill a NULL order id on a row that is
    still CREATING, and it never changes status. The UNIQUE constraint on
    paypal_order_id means one order id maps to at most one execution; an id
    already recorded against ANOTHER execution raises IntegrityError, which is
    caught and reported as "not claimed" rather than stealing or corrupting a
    row.

    Returns (claimed, record): claimed is True iff exactly one row moved.
    """
    with _connect() as conn:
        try:
            cursor = conn.execute(
                """UPDATE external_purchase_orders
                   SET paypal_order_id = ?, updated_at = ?
                   WHERE execution_id = ?
                     AND status = ?
                     AND paypal_order_id IS NULL""",
                (
                    order_id,
                    now,
                    execution_id,
                    ExternalOrderStatus.CREATING.value,
                ),
            )
            claimed = cursor.rowcount == 1
        except sqlite3.IntegrityError:
            # The unique index rejected this id: it already belongs to another
            # execution. Never steal it; report not-claimed and fail closed.
            logger.error(
                "Order-id claim for execution %s hit a UNIQUE conflict; the "
                "id already belongs to another execution. Failing closed.",
                execution_id,
            )
            claimed = False
    return claimed, get(execution_id)


def _settle_recovered_order_created(
    execution_id: str,
    order_id: str,
    now: str,
    *,
    paypal_amount: Optional[str],
    paypal_currency: Optional[str],
    paypal_approve_url: Optional[str],
) -> tuple[bool, Optional[ExternalPurchaseOrder]]:
    """Compare-and-set CREATING -> ORDER_CREATED for a recovered orphan (K.17).

    The guard is the WHERE clause, so the database arbitrates and rechecks
    BOTH invariants at write time: the row must still be CREATING AND must
    still carry the exact order id we verified against PayPal. That makes the
    recovery safe to repeat and impossible to apply to a row that moved on
    (e.g. a concurrent settle, or a different order id) -- it never regresses a
    terminal state and never rewrites the order id (the id is not in SET).

    Returns (transitioned, record): transitioned is True iff exactly one row
    moved.
    """
    with _connect() as conn:
        cursor = conn.execute(
            """UPDATE external_purchase_orders
               SET status = ?, paypal_amount = ?, paypal_currency = ?,
                   paypal_approve_url = ?, updated_at = ?
               WHERE execution_id = ?
                 AND status = ?
                 AND paypal_order_id = ?""",
            (
                ExternalOrderStatus.ORDER_CREATED.value,
                paypal_amount,
                paypal_currency,
                paypal_approve_url,
                now,
                execution_id,
                ExternalOrderStatus.CREATING.value,
                order_id,
            ),
        )
        transitioned = cursor.rowcount == 1
    return transitioned, get(execution_id)


# ---------------------------------------------------------------------------
# Durable recovery claim (Phase 11F-K.22b).
#
# Operator-gated create-order REPLAY recovery must let at most ONE worker
# replay a given ambiguous reservation at a time. The claim is a DURABLE
# compare-and-set on a nullable column (never an in-memory lock): a guarded
# UPDATE that moves exactly one still-ambiguous row. It never touches the
# order id, the request id or the status, and is cleared when the replay
# completes so a later operator retry (still inside the window) can re-claim.
# A claim left behind by a crashed worker becomes reclaimable after a TTL,
# but that NEVER widens eligibility: the recovery window is measured from the
# immutable created_at, checked independently before any claim.
# ---------------------------------------------------------------------------

#: A held claim older than this is treated as abandoned and may be reclaimed.
#: Comfortably longer than the 15s create_order timeout.
DEFAULT_RECOVERY_CLAIM_TTL_SECONDS = 300


def claim_recovery(
    execution_id: str,
    *,
    now: Optional[datetime] = None,
    claim_ttl_seconds: Optional[int] = None,
) -> tuple[bool, Optional[ExternalPurchaseOrder], Optional[str]]:
    """Atomically claim a still-ambiguous CREATING reservation for a replay.

    Granted ONLY to a row still CREATING with NO persisted order id whose claim
    slot is free (null) or stale (older than the TTL). The WHERE clause is the
    guard, so the database arbitrates: exactly one concurrent caller sees
    rowcount == 1 and may contact PayPal. It writes neither the order id nor
    the status.

    A fresh UUID token is generated for every successful claim (including a
    stale-claim takeover) and stored alongside the timestamp. The caller must
    present this token to release_recovery_claim; a worker whose claim was
    reclaimed by a later worker will hold an outdated token, so its release
    becomes a harmless no-op.

    Returns (claimed, record, token): claimed is True iff this call won the
    claim; token is the ownership token when claimed, None otherwise.
    """
    current = now if now is not None else _now_dt()
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
            """UPDATE external_purchase_orders
               SET recovery_claimed_at = ?,
                   recovery_claim_token = ?,
                   updated_at = ?
               WHERE execution_id = ?
                 AND status = ?
                 AND paypal_order_id IS NULL
                 AND (recovery_claimed_at IS NULL OR recovery_claimed_at <= ?)""",
            (
                stamp,
                token,
                stamp,
                execution_id,
                ExternalOrderStatus.CREATING.value,
                stale_cutoff,
            ),
        )
        claimed = cursor.rowcount == 1
    if not claimed:
        token = None
    return claimed, get(execution_id), token


def release_recovery_claim(
    execution_id: str, claim_token: str
) -> Optional[ExternalPurchaseOrder]:
    """Clear the recovery claim on a still-CREATING reservation (owner-guarded).

    Guarded on status = CREATING AND recovery_claim_token = claim_token, so it
    releases only the caller's OWN claim. If another worker reclaimed this
    reservation (writing a new token), this call's token no longer matches and
    the UPDATE is a no-op — the new owner's claim is never disturbed. Clearing
    the claim never makes the reservation eligible on its own — eligibility is
    re-derived from created_at and the window on every recovery call.
    """
    with _connect() as conn:
        conn.execute(
            """UPDATE external_purchase_orders
               SET recovery_claimed_at = NULL,
                   recovery_claim_token = NULL,
                   updated_at = ?
               WHERE execution_id = ?
                 AND status = ?
                 AND recovery_claim_token = ?""",
            (
                _now_dt().isoformat(),
                execution_id,
                ExternalOrderStatus.CREATING.value,
                claim_token,
            ),
        )
    return get(execution_id)


#: Statuses in which a PayPal order demonstrably EXISTS for the approval.
#: A create-order request that finds one of these replays it instead of
#: creating a second order. BUYER_APPROVED belongs here for the same reason
#: ORDER_CREATED does -- the order exists -- and leaving it out would make
#: _existing_conflict answer EXECUTION_ALREADY_FAILED about a perfectly
#: healthy, buyer-approved order, which would be untrue.
_ORDER_EXISTS_STATUSES = tuple(
    status
    for status in ExternalOrderStatus
    if status
    not in (ExternalOrderStatus.CREATING, ExternalOrderStatus.FAILED)
)

#: Statuses in which the buyer's approval has been VERIFIED server-side.
#: Every capture state implies it, because capture is only reachable from
#: BUYER_APPROVED. verify_buyer_approval replays from storage for all of
#: them rather than claiming a captured order was never approved.
_BUYER_APPROVED_STATUSES = tuple(
    status
    for status in ExternalOrderStatus
    if status
    not in (
        ExternalOrderStatus.CREATING,
        ExternalOrderStatus.ORDER_CREATED,
        ExternalOrderStatus.FAILED,
    )
)

#: A capture attempt has run and reached a terminal non-captured outcome.
#: None of these is ever retried: a second capture could double-charge one
#: that actually succeeded upstream without this server learning of it.
_CAPTURE_ATTEMPTED_STATUSES = (
    ExternalOrderStatus.CAPTURE_PENDING,
    ExternalOrderStatus.CAPTURE_DECLINED,
    ExternalOrderStatus.CAPTURE_FAILED,
)


def _existing_conflict(
    existing: ExternalPurchaseOrder,
) -> ExternalOrderConflictError:
    """The refusal owed to an execution record that cannot be replayed.

    Only ever called for a status NOT in _ORDER_EXISTS_STATUSES, i.e.
    CREATING or FAILED. The assertion keeps it that way: a future status
    added without revisiting this function should fail loudly rather than
    be silently reported as a failed creation.
    """
    assert existing.status not in _ORDER_EXISTS_STATUSES, existing.status
    if existing.status is ExternalOrderStatus.CREATING:
        return ExternalOrderConflictError(
            ExternalOrderConflict.EXECUTION_IN_PROGRESS,
            f"An order creation for approval {existing.approval_id} is "
            f"already in progress (execution {existing.execution_id}) and "
            f"its PayPal outcome is not yet known. Refusing, so a second "
            f"PayPal order cannot be created.",
            existing,
        )
    return ExternalOrderConflictError(
        ExternalOrderConflict.EXECUTION_ALREADY_FAILED,
        f"Order creation for approval {existing.approval_id} already failed "
        f"(execution {existing.execution_id}). It is not retried, because a "
        f"retry could duplicate an order PayPal may have created. Request a "
        f"new approval against current evidence.",
        existing,
    )


# ---------------------------------------------------------------------------
# Buyer-approval verification (Phase 11F-C).
#
# ONE QUESTION, ASKED OF PAYPAL: did the buyer approve THIS order, at THIS
# amount? The answer is PayPal's, read back through the existing read-only
# get_order. Nothing a browser carries -- a `token` query parameter, a
# `PayerID`, a redirect having happened at all -- is evidence of anything,
# and none of it is an input here: the only parameter is an approval id, and
# the order id asked about is the one this server stored when it created the
# order.
#
# AND IT MOVES NO MONEY. verify_buyer_approval reads an order and writes one
# status column. capture_order is not imported by this module and is not
# called; refund_capture is not imported and is not called; the catalog
# transaction ledger is neither read nor written. A BUYER_APPROVED external
# order is still refused by the Phase 11F-B0 ownership gate on every capture
# route, exactly as an ORDER_CREATED one is -- verification unlocks nothing.
#
# IT FAILS CLOSED, ALWAYS. Every branch below that is not a complete,
# matching, APPROVED answer raises. There is no partial credit, no "probably
# approved", and no path on which a failed check still writes the state.
# ---------------------------------------------------------------------------


class ExternalExecutionNotFoundError(Exception):
    """No execution record exists for the approval. Nothing was created."""


class ExternalApprovalVerificationError(Exception):
    """Buyer approval was NOT verified. Carries the machine-readable code.

    Raising this means: the execution did not become BUYER_APPROVED, no
    money moved, and nothing was created or repaired. `observed` carries
    what PayPal actually said, when anything was observed at all, so the
    refusal can be truthful about the real state without claiming approval.
    """

    def __init__(
        self,
        code: ExternalApprovalVerificationFailure,
        message: str,
        execution: Optional[ExternalPurchaseOrder] = None,
        observed: Optional[dict] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.execution = execution
        self.observed = observed or {}


#: The ONLY PayPal order status that means the buyer approved. Compared
#: case-insensitively against PayPal's own string. COMPLETED is deliberately
#: absent: this system never captures an external order, so a COMPLETED one
#: is an unexplained state to report, not an approval to act on.
_APPROVED_ORDER_STATUS = "APPROVED"


def _fail(
    code: ExternalApprovalVerificationFailure,
    message: str,
    execution: ExternalPurchaseOrder,
    **observed,
) -> ExternalApprovalVerificationError:
    logger.warning(
        "Buyer approval NOT verified (%s) for execution %s: %s",
        code.value,
        execution.execution_id,
        message,
    )
    return ExternalApprovalVerificationError(code, message, execution, observed)


def _observed_amount(
    order: dict, execution: ExternalPurchaseOrder
) -> tuple[str, str]:
    """The single purchase unit's (amount, currency), or raise.

    Parses a REMOTE payload, so every access is defensive: purchase_units
    that is absent or not a list, an entry that is not a dict, an amount
    that is not a dict, a value or currency_code that is missing or not a
    string -- each is a deterministic refusal, never an exception escaping
    from a KeyError or an attribute lookup.

    Exactly one purchase unit is required. Zero gives nothing to compare;
    more than one makes "the" amount ambiguous, and picking one is how an
    approval for a different sum gets waved through.
    """
    units = order.get("purchase_units")
    if not isinstance(units, list) or not units:
        raise _fail(
            ExternalApprovalVerificationFailure.PURCHASE_UNIT_MISSING,
            f"PayPal returned no purchase unit for order "
            f"{execution.paypal_order_id}, so there is no amount to check "
            f"the recorded one against.",
            execution,
        )
    if len(units) > 1:
        raise _fail(
            ExternalApprovalVerificationFailure.PURCHASE_UNIT_AMBIGUOUS,
            f"PayPal returned {len(units)} purchase units for order "
            f"{execution.paypal_order_id}. This server records exactly one "
            f"amount per external order, so which unit the buyer approved "
            f"is ambiguous and is not guessed.",
            execution,
            purchase_unit_count=len(units),
        )

    unit = units[0]
    amount = unit.get("amount") if isinstance(unit, dict) else None
    if not isinstance(amount, dict):
        raise _fail(
            ExternalApprovalVerificationFailure.PURCHASE_UNIT_MISSING,
            f"PayPal's purchase unit for order {execution.paypal_order_id} "
            f"carries no amount object.",
            execution,
        )

    value = amount.get("value")
    currency = amount.get("currency_code")
    if not isinstance(value, str) or not value.strip():
        raise _fail(
            ExternalApprovalVerificationFailure.PURCHASE_UNIT_MISSING,
            f"PayPal's amount for order {execution.paypal_order_id} has no "
            f"usable value.",
            execution,
        )
    if not isinstance(currency, str) or not currency.strip():
        raise _fail(
            ExternalApprovalVerificationFailure.PURCHASE_UNIT_MISSING,
            f"PayPal's amount for order {execution.paypal_order_id} has no "
            f"usable currency_code.",
            execution,
        )
    return value.strip(), currency.strip()


def _check_amount_matches(
    observed_value: str,
    observed_currency: str,
    execution: ExternalPurchaseOrder,
) -> None:
    """Refuse unless PayPal's amount IS the amount this server recorded.

    Currency is compared case-insensitively as a code. The amount is
    compared as an exact Decimal quantity, not as text: "19.990" and "19.99"
    are the same money and must both pass, while 19.9 against 19.99 must
    not. Both sides must be finite -- Decimal happily parses "NaN" and
    "Infinity", and neither is a sum anyone approved.
    """
    recorded_value = (execution.paypal_amount or "").strip()
    recorded_currency = (execution.paypal_currency or "").strip()

    if observed_currency.upper() != recorded_currency.upper():
        raise _fail(
            ExternalApprovalVerificationFailure.CURRENCY_MISMATCH,
            f"PayPal reports order {execution.paypal_order_id} in "
            f"{observed_currency}, but this server recorded sending "
            f"{recorded_currency}. The buyer did not approve the currency "
            f"this server charged.",
            execution,
            observed_amount=observed_value,
            observed_currency=observed_currency,
        )

    try:
        observed = Decimal(observed_value)
        recorded = Decimal(recorded_value)
    except (InvalidOperation, ValueError, ArithmeticError):
        raise _fail(
            ExternalApprovalVerificationFailure.AMOUNT_MALFORMED,
            f"PayPal's amount for order {execution.paypal_order_id} is not "
            f"a decimal number, so it cannot be compared with the recorded "
            f"amount.",
            execution,
            observed_amount=observed_value,
            observed_currency=observed_currency,
        ) from None

    if not observed.is_finite() or not recorded.is_finite():
        raise _fail(
            ExternalApprovalVerificationFailure.AMOUNT_MALFORMED,
            f"PayPal's amount for order {execution.paypal_order_id} is not "
            f"a finite number.",
            execution,
            observed_amount=observed_value,
            observed_currency=observed_currency,
        )

    if observed != recorded:
        raise _fail(
            ExternalApprovalVerificationFailure.AMOUNT_MISMATCH,
            f"PayPal reports order {execution.paypal_order_id} at "
            f"{observed_value} {observed_currency}, but this server recorded "
            f"sending {recorded_value} {recorded_currency}. The buyer "
            f"approved a different sum than the one that was authorized.",
            execution,
            observed_amount=observed_value,
            observed_currency=observed_currency,
        )


def _reread_and_match_order(
    execution: ExternalPurchaseOrder, order_id: str
) -> tuple[str, str, str]:
    """Re-read the order from PayPal and require it to match the record.

    THE SHARED CHECK. Phase 11F-C's approval verification and Phase 11F-D's
    capture both need exactly these guarantees before they act, so they run
    exactly this code. Duplicating them would let the two copies drift, and
    the copy that drifts is the one that lets a wrong amount through.

    Requires, in order: PayPal is readable; it answers about THIS order id;
    its status is exactly APPROVED; it carries exactly one purchase unit;
    that unit's currency and amount equal the recorded ones.

    Returns (observed_status_text, observed_amount, observed_currency).

    Raises:
        ExternalApprovalVerificationError: any check failed. The caller
            translates the code if it reports under a different vocabulary;
            the code NAMES are deliberately shared between the two phases.
    """
    try:
        order = get_order(order_id)
    except (
        PayPalAuthError,
        PayPalOrderNotFoundError,
        PayPalGetOrderError,
    ) as exc:
        raise _fail(
            ExternalApprovalVerificationFailure.ORDER_UNREADABLE,
            f"PayPal order {order_id} could not be read, so its current "
            f"state is unknown. Unknown is never treated as approved: the "
            f"execution keeps its current status.",
            execution,
        ) from exc

    returned_id = order.get("order_id")
    if not isinstance(returned_id, str) or returned_id.strip() != order_id:
        raise _fail(
            ExternalApprovalVerificationFailure.ORDER_ID_MISMATCH,
            f"PayPal answered about order {returned_id!r} when asked about "
            f"{order_id}. The stored order id is the only authority and is "
            f"never overridden by the response.",
            execution,
            observed_order_id=returned_id if isinstance(returned_id, str) else None,
        )

    status = order.get("order_status")
    status_text = status.strip() if isinstance(status, str) else ""
    if status_text.upper() != _APPROVED_ORDER_STATUS:
        raise _fail(
            ExternalApprovalVerificationFailure.ORDER_NOT_APPROVED,
            f"PayPal reports order {order_id} as "
            f"{status_text or 'an unreported status'}, not APPROVED. The "
            f"buyer has not approved it, and the observed status is reported "
            f"without being treated as approval.",
            execution,
            observed_order_status=status_text or None,
        )

    observed_value, observed_currency = _observed_amount(order, execution)
    _check_amount_matches(observed_value, observed_currency, execution)
    return status_text, observed_value, observed_currency


def _mark_buyer_approved(
    execution_id: str, now: str
) -> tuple[bool, Optional[ExternalPurchaseOrder]]:
    """Compare-and-set ORDER_CREATED -> BUYER_APPROVED.

    The guard is in the WHERE clause, so the database arbitrates: exactly
    one concurrent request can match status = ORDER_CREATED and update it.
    rowcount tells the caller whether it was the one that won.

    Returns (transitioned, record). A rowcount of 0 means someone else moved
    the row first (or it was never ORDER_CREATED); the caller re-reads and
    decides. The clause can never regress BUYER_APPROVED or resurrect
    FAILED, because neither matches the guard.
    """
    with _connect() as conn:
        cursor = conn.execute(
            """UPDATE external_purchase_orders
               SET status = ?, updated_at = ?
               WHERE execution_id = ? AND status = ?""",
            (
                ExternalOrderStatus.BUYER_APPROVED.value,
                now,
                execution_id,
                ExternalOrderStatus.ORDER_CREATED.value,
            ),
        )
        transitioned = cursor.rowcount == 1
    return transitioned, get(execution_id)


def verify_buyer_approval(approval_id: str) -> ExternalApprovalVerification:
    """Ask PayPal whether the buyer approved this approval's order.

    The ONLY input is the approval's id. There is no parameter for a PayPal
    order id, a token, a PayerID, an amount, a currency, a product, a
    merchant, a URL, a policy decision or a capture instruction -- not
    filtered out, simply absent. The order asked about is the one stored in
    external_purchase_orders when this server created it.

    Verified in order, every step failing closed:
      1. an execution exists for the approval;
      2. it is ORDER_CREATED (BUYER_APPROVED replays from storage with NO
         PayPal call; CREATING and FAILED are not verifiable);
      3. the row records an order id, an amount and a currency to check
         against;
      4. PayPal's answer is about THAT order id;
      5. PayPal's status is exactly APPROVED;
      6. PayPal reports exactly one purchase unit;
      7. its currency and amount equal the recorded ones.
    Only then is the compare-and-set performed.

    NO MONEY MOVES. One read of PayPal, one status column written. No
    capture, no refund, no catalog ledger write, no PayPal order created or
    replayed, and no change to any Phase 11D/11E binding or amount rule.

    Raises:
        ValueError: blank approval id.
        ExternalExecutionNotFoundError: no execution for this approval.
        ExternalApprovalVerificationError: any check above failed. The
            execution keeps its current status.
    """
    identifier = (approval_id or "").strip()
    if not identifier:
        raise ValueError("approval_id must be a non-empty string")

    execution = get_by_approval(identifier)
    if execution is None:
        raise ExternalExecutionNotFoundError(
            f"No external purchase execution exists for approval {identifier}"
        )

    # Already verified: answer from storage. PayPal is NOT contacted and no
    # second transition happens, so a replay cannot move state or money.
    if execution.status in _BUYER_APPROVED_STATUSES:
        # Every capture state is downstream of BUYER_APPROVED, so a captured
        # or capture-failed execution WAS verified; saying otherwise would
        # be false. Answered from storage: no PayPal call, no state change.
        logger.info(
            "Buyer approval replay for execution %s (status %s): approval "
            "already verified, no PayPal call and no state change",
            execution.execution_id,
            execution.status.value,
        )
        return ExternalApprovalVerification(
            execution=execution,
            verified=True,
            transitioned=False,
            verification_source="stored",
            captured=False,
        )

    if execution.status is not ExternalOrderStatus.ORDER_CREATED:
        because = (
            "its create-order outcome was never learned"
            if execution.status is ExternalOrderStatus.CREATING
            else "order creation failed terminally; request a new approval"
        )
        raise _fail(
            ExternalApprovalVerificationFailure.EXECUTION_NOT_VERIFIABLE,
            f"Execution {execution.execution_id} is {execution.status.value}, "
            f"which carries no confirmed PayPal order to verify: {because}.",
            execution,
            execution_status=execution.status.value,
        )

    order_id = (execution.paypal_order_id or "").strip()
    if not order_id or not (execution.paypal_amount or "").strip() or not (
        execution.paypal_currency or ""
    ).strip():
        raise _fail(
            ExternalApprovalVerificationFailure.RECORDED_ORDER_INCOMPLETE,
            f"Execution {execution.execution_id} does not record a complete "
            f"PayPal order id, amount and currency, so PayPal's answer could "
            f"not be checked against anything authoritative.",
            execution,
        )

    logger.info(
        "Verifying buyer approval for execution %s via READ-ONLY get_order"
        "(%s). No capture, no refund, no order creation.",
        execution.execution_id,
        order_id,
    )

    status_text, observed_value, observed_currency = _reread_and_match_order(
        execution, order_id
    )

    # Every check passed. The compare-and-set is the only write.
    transitioned, updated = _mark_buyer_approved(
        execution.execution_id, _now_dt().isoformat()
    )
    if updated is None:  # pragma: no cover - the row was just read
        raise _fail(
            ExternalApprovalVerificationFailure.RECORDED_ORDER_INCOMPLETE,
            f"Execution {execution.execution_id} disappeared while its "
            f"buyer approval was being recorded.",
            execution,
        )

    if updated.status is not ExternalOrderStatus.BUYER_APPROVED:
        # We lost the compare-and-set AND the row is not approved, so
        # something moved it elsewhere. Refuse rather than assert approval.
        raise _fail(
            ExternalApprovalVerificationFailure.EXECUTION_NOT_VERIFIABLE,
            f"Execution {execution.execution_id} changed to "
            f"{updated.status.value} while its buyer approval was being "
            f"recorded; no approval state was written.",
            updated,
            execution_status=updated.status.value,
        )

    logger.info(
        "Buyer approval VERIFIED for execution %s: PayPal reports order %s "
        "APPROVED at %s %s, matching the recorded amount. %s. NOTHING WAS "
        "CAPTURED and nothing was refunded.",
        updated.execution_id,
        order_id,
        observed_currency,
        observed_value,
        "This request performed the transition"
        if transitioned
        else "A concurrent request had already recorded it",
    )
    return ExternalApprovalVerification(
        execution=updated,
        verified=True,
        transitioned=transitioned,
        verification_source="paypal",
        observed_order_status=status_text,
        observed_amount=observed_value,
        observed_currency=observed_currency,
        captured=False,
    )


# ---------------------------------------------------------------------------
# External capture (Phase 11F-D). THE FIRST PHASE THAT MOVES MONEY FOR AN
# EXTERNAL PRODUCT -- and the only one. Everything here exists to make that
# single call deliberate, checked, and impossible to make twice.
#
# THREE SEPARATE ACTS ARE REQUIRED, AND NONE IMPLIES THE NEXT:
#   1. the user APPROVED the product            (Phase 11D/11E)
#   2. the BUYER approved the order at PayPal, verified server-side against
#      PayPal's own answer                      (Phase 11F-C)
#   3. a PayPilot operator EXPLICITLY AUTHORIZED capture, within a deadline
#      (this phase, authorize_capture)
# A verified buyer approval is NOT authorization to collect money: it is a
# fact about the buyer, not a decision by this system. Capture refuses with
# CAPTURE_NOT_AUTHORIZED until act 3 has happened.
#
# THE AUTHORIZATION IS NOT AUTHENTICATION. This API has no caller identity,
# so capture_authorized_at records that the step was TAKEN, never WHO took
# it. It is a workflow safeguard -- a second deliberate act with a deadline
# -- and it is documented as nothing more. An LLM response, a query
# parameter, a PayPal return URL and a PayerID are none of them this act:
# the authorize endpoint declares no body and no query parameter either.
#
# AND THE ORDER IS RE-READ ANYWAY. Even with all three acts in hand, capture
# re-reads the order from PayPal and requires it to still be APPROVED, still
# be that order id, and still carry exactly the recorded amount and currency
# -- through _reread_and_match_order, the same code Phase 11F-C uses.
#
# NOTHING HERE TOUCHES THE CATALOG. The `transactions` ledger is not
# imported, read or written; the catalog capture service is not called; the
# Phase 11F-B0 ownership gate still refuses this order on every catalog
# route whatever its status becomes. No refund capability is added: this
# module imports no refund function, and the catalog refund path refuses an
# external capture id.
# ---------------------------------------------------------------------------


#: How long an explicit capture authorization stays usable, in seconds.
#: 15 minutes: long enough for a human to finish a checkout, short enough
#: that an authorization left lying around does not become a standing
#: licence to charge.
#:
#: DELIBERATELY NOT the discovery revalidation TTL. That TTL answers "is the
#: provider evidence we showed the user still fresh?" -- a question about
#: discovery data, already answered when the order was created. This answers
#: "is the operator's decision to collect money still current?". Reusing one
#: for the other would silently tie the capture deadline to an unrelated
#: setting, and changing that setting would move this deadline without
#: anyone intending it.
DEFAULT_EXTERNAL_CAPTURE_WINDOW_SECONDS = 900

CAPTURE_WINDOW_ENV_VAR = "EXTERNAL_CAPTURE_WINDOW_SECONDS"

#: The ONLY nested capture status that means money was collected.
_COMPLETED_CAPTURE_STATUS = "COMPLETED"
_PENDING_CAPTURE_STATUS = "PENDING"
_DECLINED_CAPTURE_STATUS = "DECLINED"


class ExternalCaptureConfigError(Exception):
    """EXTERNAL_CAPTURE_WINDOW_SECONDS is unusable. A deployment fault.

    Not swallowed into a default: a silently defaulted capture deadline is a
    security setting nobody is told they lost.
    """


class ExternalCaptureRefusedError(Exception):
    """An external capture did not complete. Carries the machine code.

    `paypal_called` is the field that matters most to a reader: False means
    no capture request was ever sent and money cannot have moved. True means
    one was sent and its outcome was not an acceptable, verified COMPLETED
    -- whatever PayPal did report is persisted on the execution, and nothing
    is rolled back or retried.
    """

    def __init__(
        self,
        code: ExternalCaptureFailure,
        message: str,
        execution: Optional[ExternalPurchaseOrder] = None,
        *,
        paypal_called: bool = False,
        observed: Optional[dict] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.execution = execution
        self.paypal_called = paypal_called
        self.observed = observed or {}


def capture_window_seconds() -> int:
    """The configured capture deadline, in seconds. Validated on read."""
    raw = os.getenv(CAPTURE_WINDOW_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_EXTERNAL_CAPTURE_WINDOW_SECONDS
    try:
        value = int(raw)
    except ValueError as exc:
        raise ExternalCaptureConfigError(
            f"{CAPTURE_WINDOW_ENV_VAR} must be an integer number of "
            f"seconds, got {raw!r}"
        ) from exc
    if value <= 0:
        raise ExternalCaptureConfigError(
            f"{CAPTURE_WINDOW_ENV_VAR} must be a positive integer number of "
            f"seconds, got {value}"
        )
    return value


def _capture_fail(
    code: ExternalCaptureFailure,
    message: str,
    execution: Optional[ExternalPurchaseOrder],
    *,
    paypal_called: bool = False,
    **observed,
) -> ExternalCaptureRefusedError:
    logger.warning(
        "External capture refused (%s) for execution %s [paypal_called=%s]: %s",
        code.value,
        execution.execution_id if execution else "<none>",
        paypal_called,
        message,
    )
    return ExternalCaptureRefusedError(
        code,
        message,
        execution,
        paypal_called=paypal_called,
        observed=observed,
    )


def get_by_paypal_capture(
    paypal_capture_id: str,
) -> Optional[ExternalPurchaseOrder]:
    """One execution record by the PayPal capture id it holds, or None.

    Read-only, and added so order_ownership_service can RECOGNISE an
    external capture id and refuse it explicitly on the catalog refund
    route. It creates no refund capability: nothing in this module refunds
    anything, and this function only reads.
    """
    identifier = (paypal_capture_id or "").strip()
    if not identifier:
        raise ValueError("paypal_capture_id must be a non-empty string")
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM external_purchase_orders WHERE paypal_capture_id = ?",
            (identifier,),
        ).fetchone()
    return None if row is None else _to_record(row)


def _authorize_capture_cas(
    execution_id: str, now: str
) -> tuple[bool, Optional[ExternalPurchaseOrder]]:
    """Record an explicit capture authorization. Guarded on BUYER_APPROVED.

    Writes only capture_authorized_at. The status is deliberately NOT
    changed: authorization is a fact ABOUT a buyer-approved execution, and
    representing it as a separate status would add a state with no distinct
    behaviour. The guard means an execution already CAPTURING, CAPTURED or
    terminally failed can never be re-authorized into another attempt.
    """
    with _connect() as conn:
        cursor = conn.execute(
            """UPDATE external_purchase_orders
               SET capture_authorized_at = ?, updated_at = ?
               WHERE execution_id = ? AND status = ?""",
            (
                now,
                now,
                execution_id,
                ExternalOrderStatus.BUYER_APPROVED.value,
            ),
        )
        authorized = cursor.rowcount == 1
    return authorized, get(execution_id)


def _reserve_capture(
    execution_id: str, now: str
) -> tuple[bool, Optional[ExternalPurchaseOrder]]:
    """Compare-and-set BUYER_APPROVED -> CAPTURING, before PayPal is called.

    The single-capture guarantee, and it is the database's to enforce: the
    guard is in the WHERE clause, so exactly one concurrent request can
    match BUYER_APPROVED and claim the slot. The loser sees rowcount 0 and
    refuses; it never reaches capture_order.

    Committed BEFORE the capture call, so a crash between here and PayPal
    leaves the row visibly stuck in CAPTURING rather than silently
    recapturable. That is the intended failure mode: an explicit later
    reconciliation phase resolves it by ASKING PayPal what happened, which
    is the only safe way to find out.
    """
    with _connect() as conn:
        cursor = conn.execute(
            """UPDATE external_purchase_orders
               SET status = ?, updated_at = ?
               WHERE execution_id = ? AND status = ?""",
            (
                ExternalOrderStatus.CAPTURING.value,
                now,
                execution_id,
                ExternalOrderStatus.BUYER_APPROVED.value,
            ),
        )
        reserved = cursor.rowcount == 1
    return reserved, get(execution_id)


def _settle_capture(
    execution_id: str,
    status: ExternalOrderStatus,
    now: str,
    *,
    capture_id: Optional[str] = None,
    capture_status: Optional[str] = None,
    amount: Optional[str] = None,
    currency: Optional[str] = None,
) -> ExternalPurchaseOrder:
    """Move a CAPTURING reservation to its outcome, recording what PayPal said.

    Guarded on status = CAPTURING so a settle can never overwrite a record
    that already reached an outcome.

    The observed capture fields are written for EVERY outcome, not just the
    successful one. A DECLINED, PENDING or rejected capture that PayPal
    nonetheless reported details for must stay visible: pretending a call
    left no trace is how a real capture becomes invisible to reconciliation.

    A capture id PayPal reports for a second execution would violate the
    unique index; that is caught and the row is settled without it rather
    than losing the outcome entirely.
    """
    with _connect() as conn:
        try:
            conn.execute(
                """UPDATE external_purchase_orders
                   SET status = ?, paypal_capture_id = ?,
                       paypal_capture_status = ?, captured_amount = ?,
                       captured_currency = ?, updated_at = ?
                   WHERE execution_id = ? AND status = ?""",
                (
                    status.value,
                    capture_id,
                    capture_status,
                    amount,
                    currency,
                    now,
                    execution_id,
                    ExternalOrderStatus.CAPTURING.value,
                ),
            )
        except sqlite3.IntegrityError:
            logger.error(
                "Capture id reported for execution %s is already recorded "
                "against another execution; settling as %s without it",
                execution_id,
                status.value,
            )
            conn.execute(
                """UPDATE external_purchase_orders
                   SET status = ?, paypal_capture_status = ?,
                       captured_amount = ?, captured_currency = ?,
                       updated_at = ?
                   WHERE execution_id = ? AND status = ?""",
                (
                    ExternalOrderStatus.CAPTURE_FAILED.value,
                    capture_status,
                    amount,
                    currency,
                    now,
                    execution_id,
                    ExternalOrderStatus.CAPTURING.value,
                ),
            )
    settled = get(execution_id)
    assert settled is not None  # we just wrote it
    return settled


def authorize_capture(approval_id: str) -> ExternalCaptureAuthorization:
    """Record the explicit PayPilot authorization to capture this execution.

    The second deliberate act. It performs NO PayPal call of any kind and
    captures nothing: it writes one timestamp, which both records the
    decision and starts the capture deadline.

    Only a BUYER_APPROVED execution may be authorized -- authorizing an
    order whose buyer approval has not been verified would be authorizing a
    charge nobody has agreed to. Calling it again on a still-BUYER_APPROVED
    execution refreshes the window, because each call IS a fresh explicit
    decision; calling it on one already CAPTURING, CAPTURED or terminally
    failed is refused by the guard.

    NOT AUTHENTICATION. See the section header: this records that the step
    was taken, never who took it.

    Raises:
        ValueError: blank approval id.
        ExternalExecutionNotFoundError: no execution for this approval.
        ExternalCaptureConfigError: the capture window is misconfigured.
        ExternalCaptureRefusedError: CAPTURE_NOT_PERMITTED_IN_STATE.
    """
    identifier = (approval_id or "").strip()
    if not identifier:
        raise ValueError("approval_id must be a non-empty string")

    execution = get_by_approval(identifier)
    if execution is None:
        raise ExternalExecutionNotFoundError(
            f"No external purchase execution exists for approval {identifier}"
        )

    window = capture_window_seconds()  # validated before anything is written

    if execution.status is not ExternalOrderStatus.BUYER_APPROVED:
        raise _capture_fail(
            ExternalCaptureFailure.CAPTURE_NOT_PERMITTED_IN_STATE,
            f"Execution {execution.execution_id} is "
            f"{execution.status.value}. Capture may only be authorized for "
            f"an execution whose buyer approval has been verified "
            f"server-side (BUYER_APPROVED), and not once a capture attempt "
            f"has started.",
            execution,
            execution_status=execution.status.value,
        )

    now_dt = _now_dt()
    authorized, updated = _authorize_capture_cas(
        execution.execution_id, now_dt.isoformat()
    )
    if not authorized or updated is None:
        # The row moved between the read and the write.
        current = updated or execution
        raise _capture_fail(
            ExternalCaptureFailure.CAPTURE_NOT_PERMITTED_IN_STATE,
            f"Execution {execution.execution_id} changed to "
            f"{current.status.value} while capture was being authorized; "
            f"no authorization was recorded.",
            current,
            execution_status=current.status.value,
        )

    expires = now_dt + timedelta(seconds=window)
    logger.info(
        "Capture AUTHORIZED for execution %s until %s (window %ss). No "
        "PayPal call was made and nothing was captured.",
        updated.execution_id,
        expires.isoformat(),
        window,
    )
    return ExternalCaptureAuthorization(
        execution=updated,
        authorized=True,
        capture_authorized_at=updated.capture_authorized_at,
        capture_expires_at=expires.isoformat(),
        capture_window_seconds=window,
        captured=False,
    )


def _capture_replay(execution: ExternalPurchaseOrder) -> ExternalCaptureResult:
    """The stored result of an execution that is already CAPTURED."""
    logger.info(
        "External capture replay for execution %s: already CAPTURED, no "
        "PayPal call and no state change",
        execution.execution_id,
    )
    return ExternalCaptureResult(
        execution=execution,
        outcome="CAPTURED",
        captured=True,
        replay=True,
        paypal_capture_id=execution.paypal_capture_id,
        paypal_capture_status=execution.paypal_capture_status,
        captured_amount=execution.captured_amount,
        captured_currency=execution.captured_currency,
        refundable=False,
    )


def _check_capture_authorization(execution: ExternalPurchaseOrder) -> None:
    """Require an explicit, unexpired capture authorization."""
    authorized_at = (execution.capture_authorized_at or "").strip()
    if not authorized_at:
        raise _capture_fail(
            ExternalCaptureFailure.CAPTURE_NOT_AUTHORIZED,
            f"Execution {execution.execution_id} has a verified buyer "
            f"approval but no explicit PayPilot capture authorization. A "
            f"verified buyer approval is a fact about the buyer, not a "
            f"decision to collect money. Authorize capture first.",
            execution,
        )

    try:
        authorized_dt = datetime.fromisoformat(authorized_at)
    except ValueError:
        raise _capture_fail(
            ExternalCaptureFailure.CAPTURE_AUTHORIZATION_EXPIRED,
            f"Execution {execution.execution_id} records an unreadable "
            f"capture authorization timestamp, so its deadline cannot be "
            f"checked. Treated as expired: authorize capture again.",
            execution,
        ) from None
    if authorized_dt.tzinfo is None:
        authorized_dt = authorized_dt.replace(tzinfo=timezone.utc)

    window = capture_window_seconds()
    expires = authorized_dt + timedelta(seconds=window)
    now = _now_dt()
    if now > expires:
        raise _capture_fail(
            ExternalCaptureFailure.CAPTURE_AUTHORIZATION_EXPIRED,
            f"The capture authorization for execution "
            f"{execution.execution_id} expired at {expires.isoformat()} "
            f"(window {window}s). It is not extended silently; authorize "
            f"capture again to open a fresh window.",
            execution,
            capture_authorized_at=authorized_at,
            capture_expires_at=expires.isoformat(),
            capture_window_seconds=window,
        )


#: Verification codes and capture codes deliberately share their names for
#: the checks the two phases have in common, so translating is a lookup by
#: name rather than a second, drifting opinion about what each means.
def _as_capture_failure(
    exc: ExternalApprovalVerificationError,
) -> ExternalCaptureFailure:
    try:
        return ExternalCaptureFailure(exc.code.value)
    except ValueError:  # pragma: no cover - the names are kept in step
        return ExternalCaptureFailure.ORDER_UNREADABLE


def capture_external_order(approval_id: str) -> ExternalCaptureResult:
    """Capture ONE verified, explicitly authorized external PayPal order.

    The ONLY input is the approval's id. There is no parameter for a PayPal
    order id, a capture id, an amount, a currency, a product id, a merchant,
    a URL, a token, a PayerID or an instruction of any kind -- the order
    captured is the one stored in external_purchase_orders, and the amount
    is whatever PayPal reports for it, checked against what this server
    recorded sending.

    Refused before any capture call unless ALL of:
      1. an execution exists and is BUYER_APPROVED;
      2. an explicit PayPilot capture authorization exists;
      3. that authorization is within the capture window;
      4. the row records a complete order id, amount and currency;
      5. a fresh read-only get_order shows that exact order, still APPROVED,
         with exactly one purchase unit at exactly the recorded amount and
         currency.
    Then, and only then, a compare-and-set claims the capture slot and
    PayPal's capture endpoint is called ONCE.

    The outcome is read strictly: CAPTURED requires a nested capture status
    of exactly COMPLETED with a capture id and matching amount and currency.
    PENDING is a pending outcome, never success. DECLINED is terminal.
    Anything unknown or unexpected is CAPTURE_FAILED, recorded with whatever
    PayPal did report, and never retried automatically.

    Raises:
        ValueError: blank approval id.
        ExternalExecutionNotFoundError: no execution for this approval.
        ExternalCaptureConfigError: the capture window is misconfigured.
        ExternalCaptureRefusedError: every other refusal; `paypal_called`
            says whether a capture request was actually sent.
    """
    identifier = (approval_id or "").strip()
    if not identifier:
        raise ValueError("approval_id must be a non-empty string")

    execution = get_by_approval(identifier)
    if execution is None:
        raise ExternalExecutionNotFoundError(
            f"No external purchase execution exists for approval {identifier}"
        )

    # --- deterministic answers for every state that is not capturable ---

    if execution.status is ExternalOrderStatus.CAPTURED:
        return _capture_replay(execution)

    if execution.status is ExternalOrderStatus.CAPTURING:
        raise _capture_fail(
            ExternalCaptureFailure.CAPTURE_IN_PROGRESS,
            f"A capture for execution {execution.execution_id} is already in "
            f"progress or its outcome was never learned. Refusing is the "
            f"only answer that cannot produce a second capture. If this "
            f"persists, the execution needs explicit reconciliation against "
            f"PayPal -- it is not retried automatically.",
            execution,
            execution_status=execution.status.value,
        )

    if execution.status in _CAPTURE_ATTEMPTED_STATUSES:
        raise _capture_fail(
            ExternalCaptureFailure.CAPTURE_ALREADY_ATTEMPTED,
            f"Execution {execution.execution_id} already has a capture "
            f"attempt that ended as {execution.status.value}. It is not "
            f"retried: a second capture could double-charge one that "
            f"succeeded upstream without this server learning of it.",
            execution,
            execution_status=execution.status.value,
            paypal_capture_status=execution.paypal_capture_status,
        )

    if execution.status is not ExternalOrderStatus.BUYER_APPROVED:
        raise _capture_fail(
            ExternalCaptureFailure.CAPTURE_NOT_PERMITTED_IN_STATE,
            f"Execution {execution.execution_id} is "
            f"{execution.status.value}. Capture requires a buyer approval "
            f"that this server has verified against PayPal's own answer; "
            f"nothing else is a licence to collect money.",
            execution,
            execution_status=execution.status.value,
        )

    # --- the explicit, time-bounded authorization ---
    _check_capture_authorization(execution)

    order_id = (execution.paypal_order_id or "").strip()
    if (
        not order_id
        or not (execution.paypal_amount or "").strip()
        or not (execution.paypal_currency or "").strip()
    ):
        raise _capture_fail(
            ExternalCaptureFailure.RECORDED_ORDER_INCOMPLETE,
            f"Execution {execution.execution_id} does not record a complete "
            f"PayPal order id, amount and currency, so nothing authoritative "
            f"exists to check PayPal's answer against.",
            execution,
        )

    # --- the order must STILL be the approved one, at the approved amount ---
    logger.info(
        "Capturing external execution %s: re-reading order %s before any "
        "capture call",
        execution.execution_id,
        order_id,
    )
    try:
        _reread_and_match_order(execution, order_id)
    except ExternalApprovalVerificationError as exc:
        raise _capture_fail(
            _as_capture_failure(exc),
            f"{exc.message} No capture was attempted.",
            execution,
            **exc.observed,
        ) from exc

    # --- claim the single capture slot BEFORE contacting PayPal ---
    reserved, reserved_record = _reserve_capture(
        execution.execution_id, _now_dt().isoformat()
    )
    if not reserved or reserved_record is None:
        current = reserved_record or execution
        raise _capture_fail(
            ExternalCaptureFailure.CAPTURE_IN_PROGRESS,
            f"Another request claimed the capture slot for execution "
            f"{execution.execution_id} first (it is now "
            f"{current.status.value}). No second capture was attempted.",
            current,
            execution_status=current.status.value,
        )

    return _perform_capture(reserved_record, order_id)


def _perform_capture(
    execution: ExternalPurchaseOrder, order_id: str
) -> ExternalCaptureResult:
    """Call PayPal's capture ONCE and record the outcome honestly.

    Entered only with the CAPTURING reservation held, so every exit path
    below must settle the row -- leaving it CAPTURING would strand the
    execution. Every exit path also tells the truth about whether money may
    have moved, and none of them retries.
    """
    now = lambda: _now_dt().isoformat()  # noqa: E731 - evaluated per settle

    try:
        result = capture_order(order_id)
    except PayPalAlreadyCapturedError as exc:
        return _settle_already_captured(execution, order_id, exc)
    except PayPalOrderNotApprovedError as exc:
        # Our pre-capture read said APPROVED and PayPal now says otherwise.
        # A contradiction, not a transient: fail closed and terminal.
        settled = _settle_capture(
            execution.execution_id, ExternalOrderStatus.CAPTURE_FAILED, now()
        )
        raise _capture_fail(
            ExternalCaptureFailure.ORDER_NOT_APPROVED,
            f"PayPal refused the capture of order {order_id} because the "
            f"order is not approved, although this server read it as "
            f"APPROVED moments earlier. The contradiction is not retried.",
            settled,
            paypal_called=True,
        ) from exc
    except (
        PayPalAuthError,
        PayPalOrderNotFoundError,
        PayPalCaptureError,
    ) as exc:
        # AMBIGUOUS. The request may have reached PayPal and succeeded.
        settled = _settle_capture(
            execution.execution_id, ExternalOrderStatus.CAPTURE_FAILED, now()
        )
        raise _capture_fail(
            ExternalCaptureFailure.CAPTURE_UNCONFIRMED,
            f"The capture of PayPal order {order_id} did not return a usable "
            f"result, so whether money moved is UNKNOWN. Nothing is rolled "
            f"back and no second capture is attempted: a retry could "
            f"double-charge a capture that succeeded upstream. The execution "
            f"is CAPTURE_FAILED and needs explicit reconciliation.",
            settled,
            paypal_called=True,
        ) from exc

    return _settle_capture_result(execution, order_id, result)


def _settle_capture_result(
    execution: ExternalPurchaseOrder, order_id: str, result: dict
) -> ExternalCaptureResult:
    """Interpret PayPal's capture response strictly and settle the row."""
    now = _now_dt().isoformat()
    capture_id = result.get("capture_id")
    capture_status = result.get("capture_status")
    amount = result.get("amount")
    currency = result.get("currency")

    status_text = capture_status.strip() if isinstance(capture_status, str) else ""
    observed = {
        "paypal_capture_id": capture_id if isinstance(capture_id, str) else None,
        "paypal_capture_status": status_text or None,
        "captured_amount": amount if isinstance(amount, str) else None,
        "captured_currency": currency if isinstance(currency, str) else None,
    }

    def settle(status: ExternalOrderStatus) -> ExternalPurchaseOrder:
        return _settle_capture(
            execution.execution_id,
            status,
            now,
            capture_id=observed["paypal_capture_id"],
            capture_status=observed["paypal_capture_status"],
            amount=observed["captured_amount"],
            currency=observed["captured_currency"],
        )

    if status_text.upper() == _PENDING_CAPTURE_STATUS:
        settled = settle(ExternalOrderStatus.CAPTURE_PENDING)
        logger.warning(
            "External capture PENDING for execution %s (order %s): funds are "
            "NOT collected and this phase does not poll for settlement.",
            settled.execution_id,
            order_id,
        )
        return ExternalCaptureResult(
            execution=settled,
            outcome="PENDING",
            captured=False,
            replay=False,
            paypal_capture_id=settled.paypal_capture_id,
            paypal_capture_status=settled.paypal_capture_status,
            captured_amount=settled.captured_amount,
            captured_currency=settled.captured_currency,
            refundable=False,
        )

    if status_text.upper() == _DECLINED_CAPTURE_STATUS:
        settled = settle(ExternalOrderStatus.CAPTURE_DECLINED)
        raise _capture_fail(
            ExternalCaptureFailure.CAPTURE_DECLINED,
            f"PayPal declined the capture of order {order_id}. No money was "
            f"collected. The attempt is terminal and is not retried.",
            settled,
            paypal_called=True,
            **observed,
        )

    if status_text.upper() != _COMPLETED_CAPTURE_STATUS:
        settled = settle(ExternalOrderStatus.CAPTURE_FAILED)
        raise _capture_fail(
            ExternalCaptureFailure.CAPTURE_UNCONFIRMED,
            f"PayPal reported the capture of order {order_id} as "
            f"{status_text or 'an unreported status'}, which this server "
            f"does not recognise as collected, pending or declined. Whether "
            f"money moved is UNKNOWN; nothing is rolled back or retried.",
            settled,
            paypal_called=True,
            **observed,
        )

    # COMPLETED -- but only with a capture id and the RECORDED amount.
    problems = []
    if not observed["paypal_capture_id"]:
        problems.append("PayPal reported no capture id")
    if (observed["captured_currency"] or "").upper() != (
        execution.paypal_currency or ""
    ).upper():
        problems.append(
            f"captured currency {observed['captured_currency']!r} is not the "
            f"recorded {execution.paypal_currency!r}"
        )
    else:
        try:
            captured = Decimal(observed["captured_amount"] or "")
            recorded = Decimal(execution.paypal_amount or "")
            if not captured.is_finite() or captured != recorded:
                problems.append(
                    f"captured amount {observed['captured_amount']!r} is not "
                    f"the recorded {execution.paypal_amount!r}"
                )
        except (InvalidOperation, ValueError, ArithmeticError):
            problems.append(
                f"captured amount {observed['captured_amount']!r} is not a "
                f"decimal number"
            )

    if problems:
        settled = settle(ExternalOrderStatus.CAPTURE_FAILED)
        raise _capture_fail(
            ExternalCaptureFailure.CAPTURED_RESULT_MISMATCH,
            f"PayPal reported a COMPLETED capture for order {order_id}, but "
            f"{'; '.join(problems)}. MONEY MAY HAVE BEEN COLLECTED AND IS "
            f"NOT ROLLED BACK -- this server refuses to record it as a clean "
            f"capture. What PayPal reported is stored on the execution so "
            f"the discrepancy stays visible for reconciliation.",
            settled,
            paypal_called=True,
            **observed,
        )

    settled = settle(ExternalOrderStatus.CAPTURED)
    logger.info(
        "External capture COMPLETED for execution %s: order %s captured as "
        "%s %s %s. No catalog ledger row was written and no refund path "
        "exists for it.",
        settled.execution_id,
        order_id,
        settled.paypal_capture_id,
        settled.captured_currency,
        settled.captured_amount,
    )
    return ExternalCaptureResult(
        execution=settled,
        outcome="CAPTURED",
        captured=True,
        replay=False,
        paypal_capture_id=settled.paypal_capture_id,
        paypal_capture_status=settled.paypal_capture_status,
        captured_amount=settled.captured_amount,
        captured_currency=settled.captured_currency,
        refundable=False,
    )


def _settle_already_captured(
    execution: ExternalPurchaseOrder,
    order_id: str,
    cause: Exception,
) -> ExternalCaptureResult:
    """PayPal says the order was already captured. Verify that INDEPENDENTLY.

    ORDER_ALREADY_CAPTURED is never reported as a new successful capture on
    PayPal's word alone. The order is re-read, and the existing capture is
    accepted only if PayPal itself shows exactly one capture, COMPLETED, at
    the recorded amount and currency. Anything else -- unreadable, no
    capture, several captures, a different sum -- is
    EXISTING_CAPTURE_UNVERIFIABLE, and the execution is CAPTURE_FAILED for
    explicit reconciliation rather than being called a success.
    """
    now = _now_dt().isoformat()
    logger.warning(
        "PayPal reports order %s already captured for execution %s; "
        "re-reading it to verify the EXISTING capture independently",
        order_id,
        execution.execution_id,
    )

    def unverifiable(reason: str, **observed) -> ExternalCaptureRefusedError:
        settled = _settle_capture(
            execution.execution_id,
            ExternalOrderStatus.CAPTURE_FAILED,
            now,
            capture_status=observed.get("paypal_capture_status"),
            amount=observed.get("captured_amount"),
            currency=observed.get("captured_currency"),
        )
        return _capture_fail(
            ExternalCaptureFailure.EXISTING_CAPTURE_UNVERIFIABLE,
            f"PayPal reports order {order_id} as already captured, but "
            f"{reason}. It is NOT recorded as a successful capture. Money "
            f"may have been collected; nothing is rolled back or retried, "
            f"and the execution needs explicit reconciliation.",
            settled,
            paypal_called=True,
            **observed,
        )

    try:
        order = get_order(order_id)
    except (PayPalAuthError, PayPalOrderNotFoundError, PayPalGetOrderError):
        raise unverifiable("the order could not be re-read to confirm it") from cause

    observed, reason_code, reason_message = _match_single_completed_capture(
        order, execution
    )
    if reason_code is not None:
        raise unverifiable(reason_message, **observed) from cause

    settled = _settle_capture(
        execution.execution_id,
        ExternalOrderStatus.CAPTURED,
        now,
        capture_id=observed["paypal_capture_id"],
        capture_status=observed["paypal_capture_status"],
        amount=observed["captured_amount"],
        currency=observed["captured_currency"],
    )
    logger.info(
        "Existing capture independently VERIFIED for execution %s: order %s "
        "capture %s COMPLETED at %s %s",
        settled.execution_id,
        order_id,
        settled.paypal_capture_id,
        settled.captured_currency,
        settled.captured_amount,
    )
    return ExternalCaptureResult(
        execution=settled,
        outcome="CAPTURED",
        captured=True,
        replay=True,
        paypal_capture_id=settled.paypal_capture_id,
        paypal_capture_status=settled.paypal_capture_status,
        captured_amount=settled.captured_amount,
        captured_currency=settled.captured_currency,
        refundable=False,
    )


# ---------------------------------------------------------------------------
# The operation
# ---------------------------------------------------------------------------


def _store_approve_url(execution_id: str, approve_url: str, now: str) -> None:
    """Record a recovered checkout link on an ORDER_CREATED execution.

    The ONLY write this module performs outside create/settle, and it is
    deliberately narrow: one column, guarded on status = ORDER_CREATED so it
    can never resurrect a FAILED record or disturb a CREATING reservation,
    and it does not touch the order id, the amount or the currency. It
    changes no money provenance and creates no PayPal order.
    """
    with _connect() as conn:
        conn.execute(
            f"""UPDATE external_purchase_orders
                SET paypal_approve_url = ?, updated_at = ?
                WHERE execution_id = ?
                  AND status IN ({", ".join("?" * len(_ORDER_EXISTS_STATUSES))})""",
            (
                approve_url,
                now,
                execution_id,
                *[status.value for status in _ORDER_EXISTS_STATUSES],
            ),
        )


def recover_approve_url(
    execution: ExternalPurchaseOrder,
) -> tuple[ExternalPurchaseOrder, str]:
    """Return (execution, approve_url) for an ORDER_CREATED execution.

    The stored link is used when there is one, and no PayPal call is made.
    Otherwise the order is READ from PayPal -- get_order, which mutates
    nothing -- and its own approval href is adopted and persisted.

    create_order is NEVER called here. This module's at-most-once create
    attempt per approval is untouched: recovery only ever reads an order
    that already exists and fills in a column.

    Raises:
        ExternalOrderConflictError(APPROVAL_URL_UNAVAILABLE): no link is
            stored and none could be read back -- PayPal returned no usable
            https approval link, the order is gone, or PayPal could not be
            reached. No URL is invented from the order id, and the caller is
            told plainly that the order exists but has no checkout address
            this server can vouch for.
    """
    stored = (execution.paypal_approve_url or "").strip()
    if stored:
        return execution, stored

    order_id = (execution.paypal_order_id or "").strip()
    if not order_id:  # pragma: no cover - ORDER_CREATED always has an id
        raise ExternalOrderConflictError(
            ExternalOrderConflict.APPROVAL_URL_UNAVAILABLE,
            "This execution reached ORDER_CREATED without a PayPal order "
            "id, so there is nothing to look a checkout link up by.",
            execution,
        )

    logger.info(
        "No stored checkout link for execution %s; attempting READ-ONLY "
        "recovery via get_order(%s). create_order is NOT called.",
        execution.execution_id,
        order_id,
    )

    try:
        order = get_order(order_id)
    except (PayPalAuthError, PayPalOrderNotFoundError, PayPalGetOrderError) as exc:
        logger.error(
            "Checkout link recovery failed for execution %s: %s",
            execution.execution_id,
            exc,
        )
        raise ExternalOrderConflictError(
            ExternalOrderConflict.APPROVAL_URL_UNAVAILABLE,
            f"PayPal order {order_id} exists for this approval, but its "
            f"checkout link is not recorded and could not be read back from "
            f"PayPal. No checkout URL is guessed and no second order was "
            f"created. Retry later, or inspect the order by its id.",
            execution,
        ) from exc

    recovered = (order.get("approve_url") or "").strip()
    if not recovered:
        logger.error(
            "PayPal returned no usable approval link for order %s "
            "(execution %s)",
            order_id,
            execution.execution_id,
        )
        raise ExternalOrderConflictError(
            ExternalOrderConflict.APPROVAL_URL_UNAVAILABLE,
            f"PayPal order {order_id} exists for this approval, but PayPal "
            f"returned no usable https approval link for it, so this server "
            f"has no checkout URL to offer. One is not constructed from the "
            f"order id, and no second order was created.",
            execution,
        )

    _store_approve_url(execution.execution_id, recovered, _now_dt().isoformat())
    refreshed = get(execution.execution_id) or execution
    logger.info(
        "Recovered and stored the checkout link for execution %s from "
        "PayPal's own order links",
        execution.execution_id,
    )
    return refreshed, recovered


def _result(
    execution: ExternalPurchaseOrder,
    approval: ExternalApproval,
    *,
    created: bool,
    approve_url: Optional[str],
) -> ExternalOrderResult:
    return ExternalOrderResult(
        execution=execution,
        created=created,
        approve_url=approve_url,
        captured=False,  # this phase never calls capture_order
        approval=approval,
        observed_price_source=(
            f"provider-observed price of revalidation "
            f"{execution.revalidation_id}"
        ),
    )


def create_order_for_approval(approval_id: str) -> ExternalOrderResult:
    """Create ONE PayPal order for an APPROVED external product.

    The ONLY input is the approval's id. The amount, the currency, the
    description and the checkout URLs are all derived server-side; there is
    no argument through which a client, a provider or a model could supply
    any of them.

    The order is created and left awaiting the buyer's approval at PayPal.
    capture_order is never called, no refund is issued, and the catalog
    transaction ledger is neither read nor written.

    Idempotent per approval: if an execution record already exists and it
    reached ORDER_CREATED, that same order is returned with created=False
    and no PayPal call is made.

    Raises:
        ValueError: blank id.
        ApprovalNotFoundError: no such approval.
        ApprovalConfigError: EXTERNAL_REVALIDATION_TTL_SECONDS is unusable.
        ExternalOrderConflictError: the approval is not APPROVED, has
            expired, a binding moved, identity or lifecycle no longer
            admits a purchase, the amount is not payable as-is, or another
            execution already holds this approval's slot. No PayPal order
            is created in any of these cases.
        PayPalAuthError / PayPalOrderError: the PayPal call itself failed.
            The execution record is marked FAILED and the failure
            propagates -- never converted into a success.
    """
    identifier = (approval_id or "").strip()
    if not identifier:
        raise ValueError("approval_id must be a non-empty string")

    # An execution record may already exist from an earlier request. That is
    # settled FIRST, and deliberately without re-verifying the bindings:
    # handing back an order id that already exists creates nothing and moves
    # no money, so it must not start failing once the approved evidence
    # ages out. Only a request that would CREATE an order is gated.
    existing = get_by_approval(identifier)
    if existing is not None:
        if existing.status in _ORDER_EXISTS_STATUSES:
            # Phase 11F-B: a replay now hands back a USABLE checkout link,
            # not just an order id. The stored link is preferred; a record
            # written before the column existed recovers PayPal's own link
            # with a read-only get_order. create_order is still not called
            # on this path under any circumstance.
            refreshed, approve_url = recover_approve_url(existing)
            logger.info(
                "External order replay for approval %s: returning existing "
                "PayPal order %s with its checkout link (no create-order "
                "call)",
                identifier,
                refreshed.paypal_order_id,
            )
            return _result(
                refreshed,
                external_approval_service.get(identifier),
                created=False,
                approve_url=approve_url,
            )
        raise _existing_conflict(existing)

    # Everything is re-verified BEFORE a reservation is written, so a
    # refusal leaves no row behind to block a later legitimate attempt.
    approval, adopted, snapshot = _verify_approved_state(identifier)

    # The amount, from the approved snapshot's observed price alone.
    amount, currency = paypal_amount_from_snapshot(snapshot)
    description = order_description(adopted, approval)

    # Phase 11F-B: prove the checkout URLs are usable BEFORE the reservation
    # is written. create_order validates them too, but it does so after this
    # approval's single execution slot has been claimed -- and a PayPal
    # failure there is terminal by design. A deployment typo in
    # PAYPAL_RETURN_URL must not permanently burn a user's approval, so it
    # is caught here, where refusing leaves no row behind at all.
    checkout_urls()

    now = _now_dt().isoformat()
    reservation, concurrent = _reserve(approval, now)
    if reservation is None:
        assert concurrent is not None
        if concurrent.status in _ORDER_EXISTS_STATUSES:
            # The winner finished between our read and our insert. Its
            # checkout link is the winner's, read from the row it wrote.
            refreshed, approve_url = recover_approve_url(concurrent)
            logger.info(
                "External order race for approval %s resolved to existing "
                "PayPal order %s",
                approval.approval_id,
                refreshed.paypal_order_id,
            )
            return _result(
                refreshed, approval, created=False, approve_url=approve_url
            )
        raise _existing_conflict(concurrent)

    logger.info(
        "External order reserved %s for approval %s (product=%s "
        "revalidation=%s amount=%s %s) -- contacting PayPal, NO capture",
        reservation.execution_id,
        approval.approval_id,
        approval.adopted_product_id,
        approval.revalidation_id,
        currency,
        amount,
    )

    try:
        order = create_order(
            amount=amount,
            currency=currency,
            description=description,
            # Phase 11F-K.22a: the stable request id persisted on THIS
            # reservation, so the single create_order call is idempotent-tagged
            # at the provider. No retry is performed in this phase.
            request_id=reservation.paypal_request_id,
        )
    except (PayPalAuthError, PayPalOrderError):
        # A transient or permanent PayPal failure is NEVER a success. The
        # reservation becomes FAILED and is never retried, so an order
        # PayPal may have created without telling us cannot be duplicated.
        _settle(
            reservation.execution_id,
            ExternalOrderStatus.FAILED,
            _now_dt().isoformat(),
        )
        logger.error(
            "External order FAILED for approval %s (execution %s); no "
            "order id recorded and no retry will be attempted",
            approval.approval_id,
            reservation.execution_id,
        )
        raise

    order_id = order.get("order_id")
    if not order_id:  # pragma: no cover - the client raises on a missing id
        _settle(
            reservation.execution_id,
            ExternalOrderStatus.FAILED,
            _now_dt().isoformat(),
        )
        raise PayPalOrderError("PayPal response did not contain an order id")

    # Phase 11F-K.16: persist the PayPal order id on the STILL-CREATING
    # reservation in its OWN committed write, BEFORE the final settlement.
    # This closes the definite orphan window: a crash (or a failure of the
    # settle below) after this point leaves a CREATING record that still names
    # the order PayPal created, so existing lookup/reconciliation can find it
    # by order id. The claim is guarded (CREATING + NULL order id only) and
    # never overwrites a different id.
    claimed, claimed_record = _claim_order_id(
        reservation.execution_id, order_id, _now_dt().isoformat()
    )
    if not claimed:
        # Fail closed. A real PayPal order now exists, so we must NOT mark the
        # reservation FAILED (that would hide the order) and must NOT settle on
        # an inconsistent row. The ONLY benign case is an idempotent re-entry
        # where this exact id is already recorded on this CREATING row; then it
        # is safe to continue to settlement. Anything else is an invariant
        # breach that is surfaced, never swallowed, and never retried.
        benign_reentry = (
            claimed_record is not None
            and claimed_record.status is ExternalOrderStatus.CREATING
            and (claimed_record.paypal_order_id or "") == order_id
        )
        if not benign_reentry:
            logger.error(
                "External order id NOT recorded for execution %s; leaving the "
                "reservation CREATING for recovery. No second order created, "
                "no FAILED marked, no different id overwritten.",
                reservation.execution_id,
            )
            raise ExternalOrderPersistenceError(
                reservation.execution_id, order_id, claimed_record
            )

    # Phase 11F-B: persist PayPal's OWN approval href alongside the order id,
    # so a later replay can hand back a usable checkout link. create_order
    # parses it defensively, so this is either a real https URL PayPal sent
    # or None -- never a URL assembled from order_id. A None here is not an
    # error: the order exists, and the replay path recovers the link.
    approve_url = order.get("approve_url")
    if not approve_url:
        logger.warning(
            "PayPal returned no usable approval link for new order %s "
            "(execution %s); the order IS created and the link will be "
            "recovered by a read-only get_order on replay",
            order_id,
            reservation.execution_id,
        )

    execution = _settle(
        reservation.execution_id,
        ExternalOrderStatus.ORDER_CREATED,
        _now_dt().isoformat(),
        paypal_order_id=order_id,
        paypal_amount=amount,
        paypal_currency=currency,
        paypal_approve_url=approve_url,
    )

    logger.info(
        "External PayPal order CREATED: execution=%s approval=%s product=%s "
        "revalidation=%s paypal_order=%s amount=%s %s "
        "(approved policy price %s %s). NOT captured, NOT refunded, no "
        "catalog ledger row -- Phase 11E ends here.",
        execution.execution_id,
        approval.approval_id,
        approval.adopted_product_id,
        approval.revalidation_id,
        order_id,
        currency,
        amount,
        approval.approved_normalized_currency,
        approval.approved_normalized_price,
    )
    # The URL returned is the one just persisted, read back off the record,
    # so the response and the stored row can never disagree.
    return _result(
        execution, approval, created=True, approve_url=execution.paypal_approve_url
    )


# ===========================================================================
# Phase 11F-F: webhook-triggered, READ-ONLY reconciliation primitives.
#
# These functions NEVER call capture_order or refund_capture. They read the
# order through the existing read-only get_order and apply the SAME strict
# checks the live capture path uses, then move the record forward with a
# DATABASE compare-and-set so a concurrent event cannot overwrite a newer
# state. A webhook is only the trigger; PayPal's own order read is the
# evidence. The catalog ledger is neither imported nor touched here.
# ===========================================================================


#: The ONLY states a verified COMPLETED capture may advance to CAPTURED from.
#: CAPTURING is the crash-stuck reservation; CAPTURE_PENDING is the accepted-
#: but-not-settled outcome. Both are promoted ONLY on independent proof of a
#: single matching COMPLETED capture. CAPTURE_FAILED is deliberately ABSENT:
#: the stored row cannot distinguish an ambiguous CAPTURE_UNCONFIRMED timeout
#: from a definitive CAPTURED_RESULT_MISMATCH, so promoting it could record a
#: success PayPal never cleanly reported. It is left for manual review.
_RECONCILABLE_TO_CAPTURED = (
    ExternalOrderStatus.CAPTURING,
    ExternalOrderStatus.CAPTURE_PENDING,
)


def _match_single_completed_capture(
    order: dict, execution: ExternalPurchaseOrder
) -> tuple[dict, Optional[str], Optional[str]]:
    """Inspect a READ-ONLY order payload for ONE matching completed capture.

    This is the exact capture-matching logic the already-captured path uses,
    factored out so reconciliation reuses it rather than keeping a second,
    drifting copy. It is pure: no PayPal call, no database write. It reads
    only PayPal's own "captures" list (as get_order flattens it: a list of
    {"id", "status", "amount": {"currency_code", "value"}}).

    Returns (observed, reason_code, reason_message):
      observed       best-effort {paypal_capture_id, paypal_capture_status,
                     captured_amount, captured_currency} parsed from the one
                     capture (all-None when there is nothing to parse).
      reason_code    None when the order shows EXACTLY ONE COMPLETED capture
                     whose id is present and whose currency and amount equal
                     the recorded ones; otherwise one of NO_CAPTURE,
                     MULTIPLE_CAPTURES, NOT_COMPLETED, NO_CAPTURE_ID,
                     CURRENCY_MISMATCH, AMOUNT_MALFORMED, AMOUNT_MISMATCH.
      reason_message human text for the failure (None on success). The text
                     mirrors what the already-captured refusal emitted inline,
                     so that refusal reads exactly as before.
    """
    empty = {
        "paypal_capture_id": None,
        "paypal_capture_status": None,
        "captured_amount": None,
        "captured_currency": None,
    }

    captures = order.get("captures")
    if not isinstance(captures, list) or not captures:
        return empty, "NO_CAPTURE", "PayPal reports no capture on it"
    if len(captures) > 1:
        return (
            empty,
            "MULTIPLE_CAPTURES",
            f"PayPal reports {len(captures)} captures on it, so which one "
            f"this execution owns is ambiguous",
        )

    capture = captures[0] if isinstance(captures[0], dict) else {}
    capture_id = capture.get("id")
    capture_status = capture.get("status")
    amount = capture.get("amount") if isinstance(capture.get("amount"), dict) else {}
    value = amount.get("value")
    currency = amount.get("currency_code")
    status_text = capture_status.strip() if isinstance(capture_status, str) else ""
    observed = {
        "paypal_capture_id": capture_id if isinstance(capture_id, str) else None,
        "paypal_capture_status": status_text or None,
        "captured_amount": value if isinstance(value, str) else None,
        "captured_currency": currency if isinstance(currency, str) else None,
    }

    if status_text.upper() != _COMPLETED_CAPTURE_STATUS:
        return (
            observed,
            "NOT_COMPLETED",
            f"its capture status is {status_text or 'unreported'}, not "
            f"COMPLETED",
        )
    if not observed["paypal_capture_id"]:
        return observed, "NO_CAPTURE_ID", "it carries no capture id"
    if (observed["captured_currency"] or "").upper() != (
        execution.paypal_currency or ""
    ).upper():
        return (
            observed,
            "CURRENCY_MISMATCH",
            f"its currency {observed['captured_currency']!r} is not the "
            f"recorded {execution.paypal_currency!r}",
        )
    try:
        captured_amount = Decimal(observed["captured_amount"] or "")
        recorded_amount = Decimal(execution.paypal_amount or "")
    except (InvalidOperation, ValueError, ArithmeticError):
        return observed, "AMOUNT_MALFORMED", "its amount is not a decimal number"
    if not captured_amount.is_finite() or captured_amount != recorded_amount:
        return (
            observed,
            "AMOUNT_MISMATCH",
            f"its amount {observed['captured_amount']!r} is not the recorded "
            f"{execution.paypal_amount!r}",
        )
    return observed, None, None


def _reconcile_capture_cas(
    execution_id: str,
    now: str,
    *,
    allowed_from: tuple[ExternalOrderStatus, ...],
    capture_id: Optional[str],
    capture_status: Optional[str],
    amount: Optional[str],
    currency: Optional[str],
) -> tuple[bool, Optional[ExternalPurchaseOrder]]:
    """Compare-and-set {allowed_from...} -> CAPTURED, recording the capture.

    The guard is the WHERE clause, so a concurrent event that already moved
    the row cannot be overwritten: exactly one caller matches an allowed_from
    status and writes CAPTURED. A capture id already recorded against ANOTHER
    execution would violate the unique index; that is caught and reported as
    "not transitioned" rather than crashing or corrupting a second row.
    """
    placeholders = ", ".join("?" * len(allowed_from))
    with _connect() as conn:
        try:
            cursor = conn.execute(
                f"""UPDATE external_purchase_orders
                    SET status = ?, paypal_capture_id = ?,
                        paypal_capture_status = ?, captured_amount = ?,
                        captured_currency = ?, updated_at = ?
                    WHERE execution_id = ? AND status IN ({placeholders})""",
                (
                    ExternalOrderStatus.CAPTURED.value,
                    capture_id,
                    capture_status,
                    amount,
                    currency,
                    now,
                    execution_id,
                    *[status.value for status in allowed_from],
                ),
            )
            transitioned = cursor.rowcount == 1
        except sqlite3.IntegrityError:
            logger.error(
                "Reconcile: capture id for execution %s already belongs to "
                "another execution; not transitioning",
                execution_id,
            )
            transitioned = False
    return transitioned, get(execution_id)


def reconcile_buyer_approval(
    execution: ExternalPurchaseOrder,
) -> tuple[str, ExternalPurchaseOrder, dict]:
    """Reconcile ORDER_CREATED -> BUYER_APPROVED from an INDEPENDENT read.

    Calls the existing read-only get_order through the SAME strict
    _reread_and_match_order the live verification uses (exact stored order id,
    APPROVED, exactly one purchase unit, matching amount and currency), then
    performs the existing ORDER_CREATED -> BUYER_APPROVED compare-and-set. No
    capture, no refund, no catalog write.

    Returns (outcome, refreshed_execution, detail). outcome is one of:
      already_applied    buyer approval (or a downstream capture state) is
                         already recorded; PayPal is NOT contacted.
      not_applicable     CREATING (no confirmed order) or FAILED (terminal).
      approved           the compare-and-set moved it to BUYER_APPROVED (or a
                         concurrent event had just done so).
      order_not_approved PayPal's own read says the order is not APPROVED.
      inconclusive       the record is incomplete, PayPal was unreadable, or
                         its answer did not match — detail carries a reason
                         and whether a retry could help.
    """
    # Idempotent: buyer approval already established (every capture state is
    # downstream of it). Nothing to do and PayPal is NOT contacted.
    if execution.status in _BUYER_APPROVED_STATUSES:
        return "already_applied", execution, {}

    if execution.status is not ExternalOrderStatus.ORDER_CREATED:
        # CREATING (its create-order outcome was never learned) or FAILED
        # (terminal): neither carries a confirmed order to verify.
        return "not_applicable", execution, {"status": execution.status.value}

    order_id = (execution.paypal_order_id or "").strip()
    if (
        not order_id
        or not (execution.paypal_amount or "").strip()
        or not (execution.paypal_currency or "").strip()
    ):
        return (
            "inconclusive",
            execution,
            {"reason": "RECORDED_ORDER_INCOMPLETE", "retryable": False},
        )

    try:
        _reread_and_match_order(execution, order_id)
    except ExternalApprovalVerificationError as exc:
        retryable = (
            exc.code is ExternalApprovalVerificationFailure.ORDER_UNREADABLE
        )
        outcome = (
            "order_not_approved"
            if exc.code is ExternalApprovalVerificationFailure.ORDER_NOT_APPROVED
            else "inconclusive"
        )
        detail = {"reason": exc.code.value, "retryable": retryable}
        detail.update(exc.observed or {})
        return outcome, execution, detail

    transitioned, updated = _mark_buyer_approved(
        execution.execution_id, _now_dt().isoformat()
    )
    current = updated or execution
    if current.status is ExternalOrderStatus.BUYER_APPROVED:
        logger.info(
            "Reconciled external execution %s to BUYER_APPROVED from an "
            "independent PayPal read (order %s). No money moved.",
            current.execution_id,
            order_id,
        )
        return "approved", current, {"transitioned": transitioned}
    # Lost the compare-and-set to something that did not leave it approved.
    return (
        "inconclusive",
        current,
        {"reason": "STATE_CHANGED", "retryable": True, "status": current.status.value},
    )


def reconcile_capture(
    execution: ExternalPurchaseOrder,
) -> tuple[str, ExternalPurchaseOrder, dict]:
    """Reconcile CAPTURING / CAPTURE_PENDING -> CAPTURED from an independent read.

    NEVER calls capture_order. It re-reads the order with the read-only
    get_order and promotes to CAPTURED only when PayPal's OWN order shows
    exactly one COMPLETED capture at the stored amount and currency. The
    write is a compare-and-set from CAPTURING/CAPTURE_PENDING, so a concurrent
    event cannot overwrite a newer state, and a stored capture id is never
    rewritten with a different one.

    Returns (outcome, refreshed_execution, detail). outcome is one of:
      already_applied        already CAPTURED; PayPal is NOT contacted.
      not_applicable         not in a reconcilable state (includes
                             CAPTURE_FAILED and CAPTURE_DECLINED — see
                             _RECONCILABLE_TO_CAPTURED).
      captured               an independently verified COMPLETED capture
                             advanced it to CAPTURED.
      unresolved_no_capture  PayPal shows NO capture yet; the execution stays
                             CAPTURING/CAPTURE_PENDING. Its reservation is NOT
                             released and it does NOT become recapturable: the
                             capture may have reached PayPal and simply not be
                             visible yet.
      inconclusive           PayPal was unreadable or the record is
                             incomplete (retryable).
      review_required        a definitive-looking discrepancy (multiple
                             captures, mismatch, id/ order-id disagreement, or
                             the row moved unexpectedly). The state is left
                             untouched for a human.
    """
    if execution.status is ExternalOrderStatus.CAPTURED:
        return "already_applied", execution, {}

    if execution.status not in _RECONCILABLE_TO_CAPTURED:
        return "not_applicable", execution, {"status": execution.status.value}

    order_id = (execution.paypal_order_id or "").strip()
    if not order_id:
        return (
            "inconclusive",
            execution,
            {"reason": "RECORDED_ORDER_INCOMPLETE", "retryable": False},
        )

    try:
        order = get_order(order_id)
    except (PayPalAuthError, PayPalOrderNotFoundError, PayPalGetOrderError):
        # Unknown is never treated as captured: keep the state, allow a retry.
        return (
            "inconclusive",
            execution,
            {"reason": "ORDER_UNREADABLE", "retryable": True},
        )

    returned_id = order.get("order_id")
    if not isinstance(returned_id, str) or returned_id.strip() != order_id:
        return (
            "review_required",
            execution,
            {
                "reason": "ORDER_ID_MISMATCH",
                "retryable": False,
                "observed_order_id": returned_id
                if isinstance(returned_id, str)
                else None,
            },
        )

    observed, reason_code, reason_message = _match_single_completed_capture(
        order, execution
    )

    if reason_code == "NO_CAPTURE":
        # The capture may have reached PayPal and simply not be visible yet.
        # Keep the reservation; do NOT return it to BUYER_APPROVED or make it
        # recapturable.
        return (
            "unresolved_no_capture",
            execution,
            {"reason": reason_code, "retryable": True},
        )

    if reason_code is not None:
        # Multiple captures, not-completed, no id, amount/currency mismatch:
        # a definitive-looking discrepancy. NEVER promote; leave it for review.
        detail = {"reason": reason_code, "message": reason_message, "retryable": False}
        detail.update(observed)
        return "review_required", execution, detail

    # Exactly one COMPLETED capture at the recorded amount and currency.
    # Never rewrite a capture id already recorded against this execution.
    stored_capture_id = (execution.paypal_capture_id or "").strip()
    if stored_capture_id and stored_capture_id != observed["paypal_capture_id"]:
        detail = {
            "reason": "CAPTURE_ID_CHANGED",
            "retryable": False,
            "stored_capture_id": stored_capture_id,
        }
        detail.update(observed)
        return "review_required", execution, detail

    transitioned, updated = _reconcile_capture_cas(
        execution.execution_id,
        _now_dt().isoformat(),
        allowed_from=_RECONCILABLE_TO_CAPTURED,
        capture_id=observed["paypal_capture_id"],
        capture_status=observed["paypal_capture_status"],
        amount=observed["captured_amount"],
        currency=observed["captured_currency"],
    )
    current = updated or execution
    if transitioned and current.status is ExternalOrderStatus.CAPTURED:
        logger.info(
            "Reconciled external execution %s to CAPTURED from an independent "
            "PayPal read (order %s, capture %s). No capture_order call was "
            "made and no catalog ledger row was written.",
            current.execution_id,
            order_id,
            current.paypal_capture_id,
        )
        return "captured", current, observed
    if current.status is ExternalOrderStatus.CAPTURED:
        # A concurrent reconciliation won the compare-and-set; the end state
        # is already correct, so this is an idempotent no-op.
        return "already_applied", current, observed
    # The compare-and-set matched nothing and the row is not CAPTURED: it
    # moved to an unexpected state between the read and the write. Do not
    # force it.
    return (
        "review_required",
        current,
        {"reason": "STATE_CHANGED", "retryable": True, "status": current.status.value},
    )


# ===========================================================================
# Phase 11F-K.17: read-first recovery of a STUCK CREATING reservation.
#
# A CREATING row means a create-order call was in flight or its outcome was
# never learned. Since Phase 11F-K.16 the PayPal order id is committed to the
# row the instant create_order returns one, so a crash during the final
# settlement can leave a CREATING row that STILL NAMES its order. This path
# lets an operator recover such a row to ORDER_CREATED -- the non-money state
# meaning "the order exists and awaits the buyer" -- on conclusive evidence,
# and leaves everything else for manual review.
#
# IT MOVES NO MONEY and advances NO buyer/capture state. It only ever reads
# the order (get_order) and, at most, performs a CREATING -> ORDER_CREATED
# compare-and-set. create_order, capture_order, refund and authorize-capture
# are never called; an APPROVED order is NOT treated as captured, and the
# record is only ever settled to ORDER_CREATED, never BUYER_APPROVED (that
# transition keeps its own audited path).
# ===========================================================================


#: PayPal order statuses from which a CREATING reservation may be settled to
#: ORDER_CREATED: the order EXISTS and is still PRE-CAPTURE. APPROVED is
#: included because an order the buyer already approved still correctly maps to
#: ORDER_CREATED here (buyer-approval is recorded only through the audited
#: verify/reconcile-buyer-approval path, never inferred here). COMPLETED
#: (captured), VOIDED, PAYER_ACTION_REQUIRED and anything else are deliberately
#: excluded and routed to manual review -- settling them to ORDER_CREATED could
#: hide a capture or a dead order.
_RECOVERABLE_ORDER_STATUSES = frozenset({"CREATED", "APPROVED"})


def reconcile_creating_order(
    execution: ExternalPurchaseOrder,
) -> tuple[str, ExternalPurchaseOrder, dict]:
    """Reconcile a stuck CREATING reservation from an INDEPENDENT read (K.17).

    NEVER calls create_order, capture_order, refund or authorize-capture, and
    never advances buyer/capture state. It settles ONLY CREATING ->
    ORDER_CREATED, and only when PayPal's own order read proves the server's
    recorded order exists, is still pre-capture, answers about the stored order
    id, and carries exactly the amount and currency the bound revalidation
    snapshot derives. The write is a compare-and-set guarded on CREATING AND
    the stored order id, so it is safe to repeat and cannot regress a state
    that moved on.

    Returns (outcome, refreshed_execution, detail). outcome is one of:
      already_applied        not CREATING anymore (already settled/terminal);
                             PayPal is NOT contacted.
      manual_review_required CREATING with NO persisted order id: the
                             create-order outcome is AMBIGUOUS (e.g. a
                             timeout). The id is never guessed and the request
                             is never retried; a human must determine whether
                             an order exists.
      recovered_order_created settled CREATING -> ORDER_CREATED on conclusive,
                             independently verified evidence.
      inconclusive           PayPal was unreadable, or the bound evidence is
                             missing/unusable (retryable where sensible). The
                             state is preserved.
      review_required        a definitive-looking discrepancy (wrong order id,
                             a non-pre-capture status such as COMPLETED, an
                             amount/currency mismatch, or the row moved
                             unexpectedly). The state is preserved for a human.
    """
    # Idempotent / not-applicable: anything but CREATING is already past this
    # recovery. PayPal is NOT contacted.
    if execution.status is not ExternalOrderStatus.CREATING:
        return "already_applied", execution, {"status": execution.status.value}

    order_id = (execution.paypal_order_id or "").strip()
    if not order_id:
        # No persisted id: the create-order outcome is ambiguous. Never guess
        # an id from a description or payload, and never retry create_order.
        return (
            "manual_review_required",
            execution,
            {"reason": "NO_PERSISTED_ORDER_ID", "retryable": False},
        )

    # The expected amount/currency come from the bound snapshot's observed
    # price -- the SAME derivation create_order used -- not from the row (a
    # crash before the final settlement leaves paypal_amount NULL). This is a
    # by-id read of append-only history; it is the independent expectation the
    # observed order is checked against.
    snapshot = revalidation_service.get_revalidation(execution.revalidation_id)
    if snapshot is None:
        return (
            "inconclusive",
            execution,
            {"reason": "REVALIDATION_MISSING", "retryable": False},
        )
    try:
        expected_amount, expected_currency = paypal_amount_from_snapshot(snapshot)
    except ExternalOrderConflictError as exc:
        # The bound evidence can no longer produce a payable expected amount.
        # Leave the row for review rather than settling against no expectation.
        return (
            "review_required",
            execution,
            {"reason": exc.code.value, "retryable": False},
        )

    try:
        order = get_order(order_id)
    except (PayPalAuthError, PayPalOrderNotFoundError, PayPalGetOrderError):
        # Unknown is never treated as a confirmed order: keep CREATING, retry.
        return (
            "inconclusive",
            execution,
            {"reason": "ORDER_UNREADABLE", "retryable": True},
        )

    returned_id = order.get("order_id")
    if not isinstance(returned_id, str) or returned_id.strip() != order_id:
        return (
            "review_required",
            execution,
            {
                "reason": "ORDER_ID_MISMATCH",
                "retryable": False,
                "observed_order_id": returned_id
                if isinstance(returned_id, str)
                else None,
            },
        )

    status = order.get("order_status")
    status_text = status.strip() if isinstance(status, str) else ""
    if status_text.upper() not in _RECOVERABLE_ORDER_STATUSES:
        # COMPLETED (captured), VOIDED, or anything not pre-capture: settling
        # to ORDER_CREATED could hide a capture or a dead order. Review only.
        return (
            "review_required",
            execution,
            {
                "reason": "ORDER_NOT_RECOVERABLE_STATUS",
                "retryable": False,
                "observed_order_status": status_text or None,
            },
        )

    # Defensive parse of the single purchase unit's amount/currency, reusing
    # the shared helper. It raises on a missing/ambiguous unit.
    try:
        observed_value, observed_currency = _observed_amount(order, execution)
    except ExternalApprovalVerificationError as exc:
        return (
            "review_required",
            execution,
            {"reason": exc.code.value, "retryable": False},
        )

    # The order must carry exactly the expected amount and currency. Compared
    # as money (exact Decimal, finite) and a case-insensitive currency code.
    if observed_currency.upper() != expected_currency.upper():
        return (
            "review_required",
            execution,
            {
                "reason": "CURRENCY_MISMATCH",
                "retryable": False,
                "observed_currency": observed_currency,
            },
        )
    try:
        observed_dec = Decimal(observed_value)
        expected_dec = Decimal(expected_amount)
    except (InvalidOperation, ValueError, ArithmeticError):
        return (
            "review_required",
            execution,
            {"reason": "AMOUNT_MALFORMED", "retryable": False},
        )
    if not observed_dec.is_finite() or observed_dec != expected_dec:
        return (
            "review_required",
            execution,
            {"reason": "AMOUNT_MISMATCH", "retryable": False},
        )

    # Conclusive: the server's own order exists, is pre-capture, answers about
    # the stored id, and matches the expected amount and currency. Settle to
    # ORDER_CREATED, recording the derived amount/currency and PayPal's own
    # approval link if present (never reconstructed from the id).
    approve_url = order.get("approve_url")
    approve_url = approve_url.strip() if isinstance(approve_url, str) else None
    transitioned, updated = _settle_recovered_order_created(
        execution.execution_id,
        order_id,
        _now_dt().isoformat(),
        paypal_amount=expected_amount,
        paypal_currency=expected_currency,
        paypal_approve_url=approve_url or None,
    )
    current = updated or execution
    detail = {
        "observed_order_status": status_text,
        "paypal_amount": expected_amount,
        "paypal_currency": expected_currency,
    }
    if transitioned and current.status is ExternalOrderStatus.ORDER_CREATED:
        logger.info(
            "Recovered stuck CREATING execution %s to ORDER_CREATED from an "
            "independent PayPal read (order status %s). No order was created, "
            "nothing was captured, approved or refunded.",
            current.execution_id,
            status_text,
        )
        return "recovered_order_created", current, detail
    if current.status is ExternalOrderStatus.ORDER_CREATED:
        # A concurrent recovery won the compare-and-set; end state is correct.
        return "already_applied", current, detail
    # The guard matched nothing and the row is not ORDER_CREATED: it moved to
    # an unexpected state between the read and the write. Never force it.
    return (
        "review_required",
        current,
        {"reason": "STATE_CHANGED", "retryable": True, "status": current.status.value},
    )


# ===========================================================================
# Phase 11F-K.22b: operator-gated create-order REPLAY recovery.
#
# The AMBIGUOUS case the read-first K.17 path cannot settle: a CREATING
# reservation with NO persisted order id, whose create-order outcome was never
# learned (a transport timeout, say). Because K.22a persisted a stable
# PayPal-Request-Id, an operator can REPLAY the original create_order with that
# EXACT id. PayPal's idempotency means a replay inside its retention window
# returns the SAME order if one was created, or creates it now if none was, so
# at most one order ever exists. This is permitted ONLY when the reservation is
# demonstrably inside a CONSERVATIVE window below that retention.
#
# NEVER automatic, never from a webhook or the ordinary confirmation flow, and
# never a second request id. The replay response is NOT trusted as proof the
# order is still CREATED: the returned id is persisted via the guarded K.16
# claim and then INDEPENDENTLY re-read and verified through the existing K.17
# read-first path before the reservation is settled to ORDER_CREATED. A replay
# that fails leaves the reservation CREATING and is NEVER marked FAILED.
# ===========================================================================


def _external_replay_eligibility(
    execution: ExternalPurchaseOrder,
    *,
    now: Optional[datetime],
    window_seconds: Optional[int],
) -> tuple[bool, dict]:
    """Decide whether this ambiguous CREATING reservation admits a replay.

    Returns (eligible, detail). Every non-eligible detail fails closed and
    leaves the reservation UNCHANGED. Checks the persisted request id is
    present, valid, and the ORIGINAL value (derived from the execution id —
    recovery never generates a new one), then the conservative recovery window.
    """
    rid = (execution.paypal_request_id or "").strip()
    if not rid:
        return False, {"reason": "NO_PERSISTED_REQUEST_ID", "retryable": False}
    try:
        validated = validate_paypal_request_id(rid)
    except ValueError:
        return False, {"reason": "REQUEST_ID_INVALID", "retryable": False}
    if validated != execution.execution_id:
        # The stored id is not the one this execution was created with. Recovery
        # replays ONLY the original id and never substitutes another.
        return False, {"reason": "REQUEST_ID_NOT_ORIGINAL", "retryable": False}
    eligible, reason, age = classify_recovery_window(
        execution.created_at, now=now, window_seconds=window_seconds
    )
    if not eligible:
        return False, {"reason": reason, "retryable": False}
    return True, {"reason": "ELIGIBLE", "age_seconds": age}


def _external_replay_and_reconcile(
    execution: ExternalPurchaseOrder,
) -> tuple[str, ExternalPurchaseOrder, dict]:
    """Replay create_order with the persisted id, then verify independently."""
    # The amount/currency a NEW order must carry come from the bound snapshot's
    # observed price — the SAME derivation the original create used, and an
    # append-only row so it cannot have drifted. If it is missing or no longer
    # payable, a new order would be charged a number nobody approved, so the
    # replay is refused (nothing is created).
    snapshot = revalidation_service.get_revalidation(execution.revalidation_id)
    if snapshot is None:
        return (
            "unresolved",
            get(execution.execution_id) or execution,
            {"reason": "REVALIDATION_MISSING", "retryable": False},
        )
    try:
        amount, currency = paypal_amount_from_snapshot(snapshot)
    except ExternalOrderConflictError as exc:
        return (
            "review_required",
            get(execution.execution_id) or execution,
            {"reason": exc.code.value, "retryable": False},
        )

    # A best-effort description; PayPal ignores it when it returns the order
    # already created for this request id. Never fabricated money data.
    try:
        adopted = adoption_service.get(execution.adopted_product_id)
        approval = external_approval_service.get(execution.approval_id)
        description = order_description(adopted, approval)
    except Exception:  # noqa: BLE001 - description is cosmetic; fall back safely
        description = f"PayPilot {execution.adopted_product_id}"

    try:
        order = create_order(
            amount=amount,
            currency=currency,
            description=description,
            # The EXACT persisted request id — never a new one.
            request_id=execution.paypal_request_id,
        )
    except PayPalOrderNetworkError:
        # AMBIGUOUS transport outcome: an order may exist. Keep CREATING.
        return (
            "unresolved",
            get(execution.execution_id) or execution,
            {"reason": "REPLAY_TRANSPORT_AMBIGUOUS", "retryable": True},
        )
    except PayPalOrderError:
        # Non-2xx / malformed / missing id. Classified conservatively: an HTTP
        # error does not prove no order exists. Keep CREATING; never FAILED.
        return (
            "unresolved",
            get(execution.execution_id) or execution,
            {"reason": "REPLAY_UNRESOLVED", "retryable": True},
        )
    except (PayPalAuthError, PayPalCheckoutConfigError):
        # A precondition failing NOW says nothing about whether the ORIGINAL
        # call created an order; never conclude FAILED from a replay failure.
        return (
            "unresolved",
            get(execution.execution_id) or execution,
            {"reason": "REPLAY_PRECONDITION_UNVERIFIED", "retryable": True},
        )

    order_id = (order.get("order_id") or "").strip()
    if not order_id:
        return (
            "unresolved",
            get(execution.execution_id) or execution,
            {"reason": "REPLAY_NO_ORDER_ID", "retryable": True},
        )

    # Persist the returned id on the still-CREATING reservation via the GUARDED
    # K.16 claim. Never overwrite a different id; a conflict is review_required.
    claimed, record = _claim_order_id(
        execution.execution_id, order_id, _now_dt().isoformat()
    )
    if not claimed:
        benign_reentry = (
            record is not None
            and record.status is ExternalOrderStatus.CREATING
            and (record.paypal_order_id or "") == order_id
        )
        if not benign_reentry:
            return (
                "review_required",
                record or get(execution.execution_id) or execution,
                {
                    "reason": "ORDER_ID_CONFLICT",
                    "retryable": False,
                    "observed_order_id": order_id,
                },
            )

    # The replay response is NOT proof the order is still CREATED. Re-read and
    # verify INDEPENDENTLY, then settle to ORDER_CREATED, via the K.17 path.
    refreshed = get(execution.execution_id) or execution
    return reconcile_creating_order(refreshed)


def recover_creating_order(
    execution: ExternalPurchaseOrder,
    *,
    now: Optional[datetime] = None,
    window_seconds: Optional[int] = None,
) -> tuple[str, ExternalPurchaseOrder, dict]:
    """Operator-gated recovery of a stuck CREATING reservation (K.22b).

    Returns (outcome, refreshed_execution, detail). Behaviour by case:

      - NOT CREATING, or CREATING WITH a persisted order id: no replay is
        needed or allowed; delegates to the read-first K.17 recovery
        (reconcile_creating_order), which contacts PayPal only to READ.
      - CREATING with NO order id and INELIGIBLE (no/invalid/non-original
        request id, malformed/ambiguous/future timestamp, or outside the
        conservative window): manual_review_required, reservation UNCHANGED,
        no provider call.
      - CREATING with NO order id, eligible, but a replay already claimed by
        another worker: replay_in_progress, no provider call.
      - CREATING with NO order id and eligible: replays create_order with the
        EXACT persisted request id under a durable single-flight claim, then
        independently verifies and settles via the K.17 read-first path.

    outcomes add replay_in_progress and unresolved to the K.17 set
    (recovered_order_created, already_applied, review_required, inconclusive,
    manual_review_required). It NEVER captures, authorizes, refunds, marks the
    reservation FAILED, overwrites an order id, or replaces the request id.
    """
    if execution.status is not ExternalOrderStatus.CREATING:
        return reconcile_creating_order(execution)
    order_id = (execution.paypal_order_id or "").strip()
    if order_id:
        # An order already exists; the read-first path is correct and safe.
        return reconcile_creating_order(execution)

    eligible, detail = _external_replay_eligibility(
        execution, now=now, window_seconds=window_seconds
    )
    if not eligible:
        return "manual_review_required", execution, detail

    claimed, current, claim_token = claim_recovery(
        execution.execution_id, now=now
    )
    if not claimed:
        return (
            "replay_in_progress",
            current or execution,
            {"reason": "REPLAY_IN_PROGRESS", "retryable": True},
        )

    try:
        return _external_replay_and_reconcile(current or execution)
    finally:
        # Free the claim so a later in-window operator retry can re-claim.
        # Owner-guarded: only this worker's token matches; a stale-reclaim
        # by another worker wrote a new token, so this release is a no-op.
        release_recovery_claim(execution.execution_id, claim_token)
