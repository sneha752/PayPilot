"""Deterministic adopted-product revalidation (Phase 11B).

WHAT THIS DOES
    Re-queries the discovery provider a product was ORIGINALLY adopted from,
    looks for that exact product in the fresh results, and records what the
    provider says about it now as an append-only snapshot.

WHAT THIS DOES NOT DO
    It does not decide anything about money. No status it produces means
    "purchasable", "allowed", or "approved". It imports no payment code —
    not paypal, not the policy engine, not purchase execution, not the
    capital ledger, not refunds, not sessions — and calls no AI provider.
    Whether a price change is acceptable is Phase 11C's question.

THE IDENTITY RULE (the whole security story)
    A fresh result is accepted ONLY when

        fresh.external_id == adopted.discovery_external_id

    byte for byte, from the same provider. That is the provider's own
    native identity, computed by the provider adapter from the provider's
    own response — not by this module, and not from anything a client sent.

    Title, merchant name, merchant domain, price and availability are NEVER
    used to establish identity. They are precisely the fields that drift,
    that a reshuffled search ranking changes, and that an attacker would
    influence. Matching on them is how "product A" silently becomes
    "product B", so there is no code path here that can do it.

    No match -> IDENTITY_NOT_FOUND. Never a substitution, never a
    best-effort guess, never "the first result".

PROVIDER LIMITATION, STATED PLAINLY
    Neither provider supports exact-product lookup. Channel3Client and
    SerperProvider expose search(query, limit, page_token) and nothing else
    — there is no get-by-id endpoint in either contract. So a fresh request
    must be a SEARCH, and a search needs query text.

    This module uses the adopted title as that query text, and the
    distinction matters enormously:

        the title is a RETRIEVAL HINT  (which pages of results to fetch)
        the external_id is the IDENTITY GATE  (what we will accept)

    A wrong, stale, or even attacker-chosen title can therefore only cause
    a MISS (IDENTITY_NOT_FOUND). It can never cause a false match, because
    the title is not consulted when deciding whether a result is the adopted
    product. The worst outcome of a bad hint is "we could not confirm it",
    which is the safe direction to fail in.

    Consequence worth knowing: a Serper external_id derived from a link hash
    (serper:url:<sha256>) embeds query-specific URL parameters, so re-
    searching with different text can legitimately fail to match a product
    that is still on sale. That is reported honestly as IDENTITY_NOT_FOUND
    rather than papered over.

FRESH EVIDENCE GOES THROUGH THE EXISTING PIPELINE
    The fresh result is normalized by the existing Phase 10F currency
    service and given merchant identity by the existing Phase 10H service.
    No second normalization, no second HTTP client, no duplicated provider
    logic — so 10F/10H semantics stay single-sourced and unchanged.

HISTORY IS APPEND-ONLY
    Every attempt inserts a new row in adopted_product_revalidations. The
    adopted record's own snapshot is never overwritten; at most its
    lifecycle `status` moves (see _next_status), which writes no product
    fact.

FOLLOWS THE LEDGER'S CONVENTIONS
    stdlib sqlite3, no ORM, parameterized SQL, the same
    backend/data/paypilot.db and PAYPILOT_DB_PATH test isolation — in its
    own table. Prices are TEXT holding exact Decimal strings; SQLite REAL
    would corrupt them into binary floats.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from ..schemas.adoption import AdoptedProduct, AdoptedProductStatus
from ..schemas.discovery import NormalizedDiscoveredProduct
from ..schemas.revalidation import (
    AvailabilityStatus,
    IdentityMatch,
    MerchantStatus,
    PriceStatus,
    RevalidationReason,
    RevalidationResponse,
    RevalidationSnapshot,
    RevalidationStatus,
)
from . import adoption_service
from .discovery_currency_service import (
    DEFAULT_NORMALIZATION_CURRENCY,
    normalize_products,
)
from .discovery_merchant_service import attach_merchant_identity
from .product_identity import classify as classify_identity
from .discovery_providers.base import (
    DiscoveryAuthError,
    DiscoveryConfigError,
    DiscoveryError,
    DiscoveryInvalidResponseError,
    DiscoveryQuotaError,
    DiscoveryRateLimitError,
    DiscoveryRequestError,
)
from .discovery_service import provider_for_source

logger = logging.getLogger("paypilot.revalidation")

#: How many fresh results to scan for the adopted identity. The provider
#: bound is 1..30 for both adapters; scanning the maximum maximizes the
#: chance of seeing the adopted product and costs the same single request.
DEFAULT_REVALIDATION_LIMIT = 30

#: Columns added after this table first shipped (Phase 11B.1), applied by
#: _migrate(). Additive and nullable, so snapshots written by Phase 11B stay
#: readable with these reported as null.
_ADDED_COLUMNS = (
    ("identity_kind", "TEXT"),
    ("product_identity_key", "TEXT"),
)

#: Deterministic retrieval queries are capped, so one revalidation can never
#: fan out into an unbounded number of billed provider requests.
MAX_RETRIEVAL_ATTEMPTS = 3

#: Words kept when shortening a title into a broader retrieval query. Chosen
#: because retailers edit titles by appending or trimming trailing
#: attributes ("... Built-In Mic Micro USB"), while the leading words stay.
_TITLE_PREFIX_WORDS = 6

#: Provider availability strings that state stock explicitly. Anything else
#: — including absence — is UNKNOWN, never inferred from a missing price.
_AVAILABLE_VALUES = frozenset({"instock", "in_stock", "available"})
_UNAVAILABLE_VALUES = frozenset(
    {"outofstock", "out_of_stock", "unavailable", "soldout", "sold_out"}
)

_DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "paypilot.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS adopted_product_revalidations (
    revalidation_id             TEXT PRIMARY KEY,
    adopted_product_id          TEXT NOT NULL,
    checked_at                  TEXT NOT NULL,
    discovery_source            TEXT NOT NULL,
    discovery_external_id       TEXT NOT NULL,
    revalidation_status         TEXT NOT NULL,
    identity_match              TEXT NOT NULL,
    reason_code                 TEXT NOT NULL,
    price_status                TEXT NOT NULL,
    availability                TEXT NOT NULL,
    merchant_status             TEXT NOT NULL,
    current_title               TEXT,
    current_merchant_name       TEXT,
    current_merchant_domain     TEXT,
    current_product_url         TEXT,
    current_observed_price      TEXT,
    current_observed_currency   TEXT,
    current_normalized_price    TEXT,
    current_normalized_currency TEXT,
    previous_observed_price     TEXT,
    previous_observed_currency  TEXT
);
CREATE INDEX IF NOT EXISTS idx_revalidations_adopted_product
    ON adopted_product_revalidations (adopted_product_id, checked_at);
"""

_COLUMNS = (
    "revalidation_id",
    "adopted_product_id",
    "checked_at",
    "discovery_source",
    "discovery_external_id",
    "identity_kind",
    "product_identity_key",
    "revalidation_status",
    "identity_match",
    "reason_code",
    "price_status",
    "availability",
    "merchant_status",
    "current_title",
    "current_merchant_name",
    "current_merchant_domain",
    "current_product_url",
    "current_observed_price",
    "current_observed_currency",
    "current_normalized_price",
    "current_normalized_currency",
    "previous_observed_price",
    "previous_observed_currency",
)

_DECIMAL_COLUMNS = (
    "current_observed_price",
    "current_normalized_price",
    "previous_observed_price",
)


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
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Add post-launch columns. Additive only; old snapshots stay readable.

    Nothing is backfilled: a snapshot recorded before Phase 11B.1 was taken
    without an identity classification, and inventing one retroactively
    would misreport what that check actually did. Null means "not recorded",
    which is the truth.
    """
    existing = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(adopted_product_revalidations)")
    }
    for name, definition in _ADDED_COLUMNS:
        if name in existing:
            continue
        conn.execute(
            "ALTER TABLE adopted_product_revalidations "
            f"ADD COLUMN {name} {definition}"
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decimal_to_text(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else str(value)


def _to_snapshot(row: sqlite3.Row) -> RevalidationSnapshot:
    data = dict(row)
    for column in _DECIMAL_COLUMNS:
        if data[column] is not None:
            data[column] = Decimal(data[column])
    return RevalidationSnapshot(**data)


# ---------------------------------------------------------------------------
# Deterministic comparisons
# ---------------------------------------------------------------------------


def compare_price(
    previous_price: Optional[Decimal],
    previous_currency: Optional[str],
    current_price: Optional[Decimal],
    current_currency: Optional[str],
) -> PriceStatus:
    """Compare two observed prices. Exact Decimal, same currency only.

    No float, no tolerance, no conversion. Comparing across currencies
    would need a rate, and a rate-dependent "unchanged" is a fiction — so
    that case is reported as PRICE_NOT_COMPARABLE instead.
    """
    if current_price is None or not current_currency:
        return PriceStatus.PRICE_UNAVAILABLE
    if previous_price is None or not previous_currency:
        # Nothing to compare against; the fresh figure still stands alone.
        return PriceStatus.PRICE_NOT_COMPARABLE
    if previous_currency.strip().upper() != current_currency.strip().upper():
        return PriceStatus.PRICE_NOT_COMPARABLE
    return (
        PriceStatus.PRICE_UNCHANGED
        if previous_price == current_price
        else PriceStatus.PRICE_CHANGED
    )


def classify_availability(raw: Optional[str]) -> AvailabilityStatus:
    """Map a provider availability string to a status, or UNKNOWN.

    Only an EXPLICIT provider statement produces AVAILABLE or UNAVAILABLE.
    A missing value, an unrecognized value, or a provider that has no such
    field (Serper) yields UNKNOWN. Availability is never inferred from the
    presence or absence of a price.
    """
    if not raw or not raw.strip():
        return AvailabilityStatus.UNKNOWN
    token = raw.strip().lower().replace("-", "_").replace(" ", "")
    if token in _AVAILABLE_VALUES:
        return AvailabilityStatus.AVAILABLE
    if token in _UNAVAILABLE_VALUES:
        return AvailabilityStatus.UNAVAILABLE
    return AvailabilityStatus.UNKNOWN


def _merchant_key(name: Optional[str], domain: Optional[str]) -> Optional[str]:
    """A comparable merchant identity, or None when none is stated.

    Domain wins when present (it is already normalized by Phase 10H and is
    far more stable than a display name). Otherwise the name, trimmed and
    case-folded so pure formatting changes do not read as a different
    merchant.
    """
    if domain and domain.strip():
        return domain.strip().lower()
    if name and name.strip():
        return name.strip().casefold()
    return None


def compare_merchant(
    previous_name: Optional[str],
    previous_domain: Optional[str],
    current_name: Optional[str],
    current_domain: Optional[str],
) -> MerchantStatus:
    """Compare adopted vs fresh merchant identity. Informational only.

    A difference never fails revalidation — identity is the provider's
    external_id. Formatting and casing differences are normalized away so
    they are not reported as a change. Nothing here invents a domain.
    """
    previous = _merchant_key(previous_name, previous_domain)
    current = _merchant_key(current_name, current_domain)
    if previous is None or current is None:
        return MerchantStatus.MERCHANT_UNKNOWN
    return (
        MerchantStatus.MERCHANT_UNCHANGED
        if previous == current
        else MerchantStatus.MERCHANT_CHANGED
    )


def _next_status(
    adopted: AdoptedProduct, outcome: RevalidationStatus
) -> Optional[AdoptedProductStatus]:
    """The adopted record's status after this attempt, or None for no change.

    Deterministic rules, and the reasoning for each:

      DISABLED stays DISABLED, always. It is an operator decision; a
      revalidation is an observation and must never override it.

      REVALIDATED -> ACTIVE. Confirmed identity clears a previous STALE
      mark, so a product that came back is usable again.

      IDENTITY_NOT_FOUND -> STALE. The provider answered and the adopted
      product was not among its results, so the adopted evidence no longer
      corresponds to something being served. STALE, not DISABLED: this may
      be a catalog reshuffle rather than a withdrawn product, and only an
      operator disables a record permanently.

      PROVIDER_UNAVAILABLE / PROVIDER_ERROR / NOT_REVALIDATABLE -> NO
      CHANGE. These describe the provider or our own configuration, not the
      product. Marking a record stale because someone else had an outage
      would punish our data for their downtime.
    """
    if adopted.status is AdoptedProductStatus.DISABLED:
        return None
    if outcome is RevalidationStatus.REVALIDATED:
        return (
            None
            if adopted.status is AdoptedProductStatus.ACTIVE
            else AdoptedProductStatus.ACTIVE
        )
    if outcome is RevalidationStatus.IDENTITY_NOT_FOUND:
        return (
            None
            if adopted.status is AdoptedProductStatus.STALE
            else AdoptedProductStatus.STALE
        )
    return None


def _classify_provider_error(
    exc: DiscoveryError,
) -> tuple[RevalidationStatus, RevalidationReason]:
    """Split provider failures into transient vs permanent, deterministically.

    Keyed off the provider hierarchy's own `retryable` flag where it
    applies, so this never drifts from the providers' own semantics.
    """
    if isinstance(exc, DiscoveryRateLimitError):
        return (
            RevalidationStatus.PROVIDER_UNAVAILABLE,
            RevalidationReason.PROVIDER_RATE_LIMITED,
        )
    if isinstance(exc, DiscoveryAuthError):
        return (
            RevalidationStatus.PROVIDER_ERROR,
            RevalidationReason.PROVIDER_AUTH_REJECTED,
        )
    if isinstance(exc, DiscoveryQuotaError):
        return (
            RevalidationStatus.PROVIDER_ERROR,
            RevalidationReason.PROVIDER_QUOTA_EXHAUSTED,
        )
    if isinstance(exc, DiscoveryRequestError):
        return (
            RevalidationStatus.PROVIDER_ERROR,
            RevalidationReason.PROVIDER_REJECTED_REQUEST,
        )
    if isinstance(exc, DiscoveryInvalidResponseError):
        return (
            RevalidationStatus.PROVIDER_ERROR,
            RevalidationReason.PROVIDER_RESPONSE_UNUSABLE,
        )
    # DiscoveryUnavailableError and any future retryable subclass.
    if getattr(exc, "retryable", False):
        return (
            RevalidationStatus.PROVIDER_UNAVAILABLE,
            RevalidationReason.PROVIDER_UPSTREAM_FAILURE,
        )
    return (
        RevalidationStatus.PROVIDER_ERROR,
        RevalidationReason.PROVIDER_UPSTREAM_FAILURE,
    )


def retrieval_queries(adopted: AdoptedProduct) -> list[str]:
    """Deterministic search queries used to FETCH candidates. Not identity.

    Why more than one: Phase 11B used the adopted title alone, and the live
    smoke test failed because the retailer had SHORTENED the title between
    adoption and the check ("... Purple Built-In Mic Micro USB" became
    "... Purple"). Searching the stale full title returned 30 unrelated
    results, so a product whose durable id was perfectly intact and still
    being served was reported IDENTITY_NOT_FOUND.

    The fix belongs here, in RETRIEVAL, and not in matching. Every query
    below is only a way to ask the provider for more candidates; each
    candidate is still admitted solely by exact durable-identity equality.
    So a broader or stale query can only change WHETHER we find the product,
    never WHICH product we accept. Adding queries cannot create a false
    match — it can only reduce false misses.

    Order is most-specific first, deduplicated, and capped by
    MAX_RETRIEVAL_ATTEMPTS so a single revalidation cannot fan out into an
    unbounded number of billed requests.
    """
    title = (adopted.title or "").strip()
    if not title:
        return []

    candidates = [title]

    brand = (adopted.brand or "").strip()
    if brand and brand.casefold() not in title.casefold():
        candidates.append(f"{brand} {title}")

    # A leading-words prefix survives the trailing-attribute edits that
    # retailers actually make.
    words = title.split()
    if len(words) > _TITLE_PREFIX_WORDS:
        candidates.append(" ".join(words[:_TITLE_PREFIX_WORDS]))

    seen: set[str] = set()
    ordered: list[str] = []
    for query in candidates:
        key = query.casefold()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(query)
    return ordered[:MAX_RETRIEVAL_ATTEMPTS]


def find_identity_match(
    products: list[NormalizedDiscoveredProduct],
    source: str,
    external_id: str,
) -> Optional[NormalizedDiscoveredProduct]:
    """The fresh result whose DURABLE PRODUCT IDENTITY equals the adopted one.

    Both sides are classified through product_identity, and a result is
    returned ONLY when both resolve to the same non-null
    product_identity_key. So an observation-only id can never match — not
    even against a byte-identical observation-only id, because such an id
    names a search result rather than a product.

    Nothing else is consulted: not the title, not the merchant, not the
    price, not the brand, not the position in the result list. A product
    that merely looks similar is not a match, and the first result is never
    a fallback.
    """
    wanted_source = (source or "").strip()
    wanted_id = (external_id or "").strip()
    if not wanted_source or not wanted_id:
        return None

    wanted = classify_identity(wanted_source, wanted_id)
    if not wanted.is_durable:
        # Nothing could legitimately match it.
        return None

    for product in products:
        candidate_source = (product.source or "").strip()
        candidate_id = (product.external_id or "").strip()
        if not candidate_source or not candidate_id:
            continue
        candidate = classify_identity(candidate_source, candidate_id)
        if not candidate.is_durable:
            continue
        if candidate.product_identity_key == wanted.product_identity_key:
            return product
    return None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _insert(snapshot: RevalidationSnapshot) -> RevalidationSnapshot:
    with _connect() as conn:
        conn.execute(
            f"""INSERT INTO adopted_product_revalidations
                ({", ".join(_COLUMNS)})
                VALUES ({", ".join("?" * len(_COLUMNS))})""",
            (
                snapshot.revalidation_id,
                snapshot.adopted_product_id,
                snapshot.checked_at,
                snapshot.discovery_source,
                snapshot.discovery_external_id,
                snapshot.identity_kind,
                snapshot.product_identity_key,
                snapshot.revalidation_status.value,
                snapshot.identity_match.value,
                snapshot.reason_code.value,
                snapshot.price_status.value,
                snapshot.availability.value,
                snapshot.merchant_status.value,
                snapshot.current_title,
                snapshot.current_merchant_name,
                snapshot.current_merchant_domain,
                snapshot.current_product_url,
                _decimal_to_text(snapshot.current_observed_price),
                snapshot.current_observed_currency,
                _decimal_to_text(snapshot.current_normalized_price),
                snapshot.current_normalized_currency,
                _decimal_to_text(snapshot.previous_observed_price),
                snapshot.previous_observed_currency,
            ),
        )
    return snapshot


def list_revalidations(
    adopted_product_id: str, limit: int = 20
) -> list[RevalidationSnapshot]:
    """Snapshots for one adopted product, newest first.

    Read-only history. Ordering is deterministic: checked_at DESC with
    revalidation_id DESC as a tie-breaker, never SQLite insertion order.
    """
    identifier = (adopted_product_id or "").strip()
    if not identifier:
        raise ValueError("adopted_product_id must be a non-empty string")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")

    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM adopted_product_revalidations
               WHERE adopted_product_id = ?
               ORDER BY checked_at DESC, revalidation_id DESC
               LIMIT ?""",
            (identifier, limit),
        ).fetchall()
    return [_to_snapshot(row) for row in rows]


def get_revalidation(revalidation_id: str) -> Optional[RevalidationSnapshot]:
    """One snapshot by its own id, or None. Read-only (Phase 11D).

    Added so a later consumer can re-read the EXACT snapshot it is bound to
    rather than inferring it from the history list. The table is append-only,
    so this returns the same bytes it returned the first time -- which is
    precisely why binding to a revalidation_id is a durable commitment.
    """
    identifier = (revalidation_id or "").strip()
    if not identifier:
        raise ValueError("revalidation_id must be a non-empty string")

    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM adopted_product_revalidations WHERE revalidation_id = ?",
            (identifier,),
        ).fetchone()
    return None if row is None else _to_snapshot(row)


def count(adopted_product_id: Optional[str] = None) -> int:
    """How many snapshots exist, overall or for one product. Tests/diagnostics."""
    with _connect() as conn:
        if adopted_product_id is None:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM adopted_product_revalidations"
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM adopted_product_revalidations "
                "WHERE adopted_product_id = ?",
                ((adopted_product_id or "").strip(),),
            ).fetchone()
    return int(row["n"])


# ---------------------------------------------------------------------------
# The operation
# ---------------------------------------------------------------------------


def revalidate(
    adopted_product_id: str,
    limit: int = DEFAULT_REVALIDATION_LIMIT,
    client=None,
) -> RevalidationResponse:
    """Re-query the original provider for one adopted product.

    The ONLY input is the adopted product's id. Provider, external id and
    query text are all read from the server-side adopted record, so no
    caller can redirect the lookup, substitute a provider, or supply a
    product fact.

    Always returns a response and always persists a snapshot — including
    for provider failures, so the history records that the attempt was made
    and why it produced nothing. A failure is reported in
    revalidation_status, never raised as a product verdict.

    Args:
        adopted_product_id: PayPilot's adopted-product id.
        limit: Fresh results to scan for the adopted identity.
        client: Optional pre-built Channel3 client (tests/DI).

    Raises:
        ValueError: blank id.
        AdoptedProductNotFoundError: no such adopted product.
    """
    adopted = adoption_service.get(adopted_product_id)
    checked_at = _now()
    # Classified from the stored provider name and id. Recorded on every
    # snapshot so an audit can see what identity the check actually relied
    # on, instead of inferring it from the id's shape later.
    product_identity = classify_identity(
        adopted.discovery_source, adopted.discovery_external_id
    )

    def snapshot(
        status: RevalidationStatus,
        identity: IdentityMatch,
        reason: RevalidationReason,
        fresh: Optional[NormalizedDiscoveredProduct] = None,
    ) -> RevalidationResponse:
        """Assemble, persist and return one outcome.

        Fresh fields are populated ONLY from `fresh` — the normalized
        provider record. When identity did not match there is no `fresh`,
        so every current_* field stays null rather than falling back to the
        adopted record's stale values, which would make old evidence look
        freshly confirmed.
        """
        price_status = compare_price(
            adopted.observed_price,
            adopted.observed_currency,
            fresh.observed_price if fresh else None,
            fresh.observed_currency if fresh else None,
        )
        record = RevalidationSnapshot(
            revalidation_id=f"reval_{uuid.uuid4().hex}",
            adopted_product_id=adopted.adopted_product_id,
            checked_at=checked_at,
            discovery_source=adopted.discovery_source,
            discovery_external_id=adopted.discovery_external_id,
            identity_kind=product_identity.kind.value,
            product_identity_key=product_identity.product_identity_key,
            revalidation_status=status,
            identity_match=identity,
            reason_code=reason,
            price_status=price_status,
            availability=classify_availability(fresh.availability if fresh else None),
            merchant_status=(
                compare_merchant(
                    adopted.merchant_name,
                    adopted.merchant_domain,
                    fresh.merchant_name,
                    fresh.merchant_domain,
                )
                if fresh
                else MerchantStatus.MERCHANT_UNKNOWN
            ),
            current_title=fresh.title if fresh else None,
            current_merchant_name=fresh.merchant_name if fresh else None,
            current_merchant_domain=fresh.merchant_domain if fresh else None,
            current_product_url=fresh.product_url if fresh else None,
            current_observed_price=fresh.observed_price if fresh else None,
            current_observed_currency=fresh.observed_currency if fresh else None,
            current_normalized_price=fresh.normalized_price if fresh else None,
            current_normalized_currency=(
                fresh.normalized_currency if fresh else None
            ),
            previous_observed_price=adopted.observed_price,
            previous_observed_currency=adopted.observed_currency,
        )
        _insert(record)

        transition = _next_status(adopted, status)
        if transition is None:
            final_status = adopted.status.value
            changed = False
        else:
            final_status = adoption_service.set_status(
                adopted.adopted_product_id, transition
            ).status.value
            changed = True

        logger.info(
            "Revalidation %s for %s: status=%s identity=%s price=%s reason=%s",
            record.revalidation_id,
            adopted.adopted_product_id,
            status.value,
            identity.value,
            price_status.value,
            reason.value,
        )
        return RevalidationResponse(
            snapshot=record,
            adopted_product_status=final_status,
            adopted_status_changed=changed,
        )

    # --- No durable identity -> no request at all. A provider call could
    # not settle the question either way, so making one would spend money to
    # learn nothing and invite a weaker match as consolation. ---
    if not product_identity.is_durable:
        logger.info(
            "Cannot verify %s: %s id %r is observation-only (%s)",
            adopted.adopted_product_id,
            adopted.discovery_source,
            adopted.discovery_external_id,
            product_identity.kind.value,
        )
        return snapshot(
            RevalidationStatus.IDENTITY_UNVERIFIABLE,
            IdentityMatch.UNVERIFIABLE,
            RevalidationReason.NO_DURABLE_PRODUCT_IDENTITY,
        )

    # --- retrieval hints. NOT the identity gate (see module docstring). ---
    queries = retrieval_queries(adopted)
    if not queries:
        return snapshot(
            RevalidationStatus.NOT_REVALIDATABLE,
            IdentityMatch.NOT_ATTEMPTED,
            RevalidationReason.NO_USABLE_QUERY,
        )

    # --- the provider the product was ORIGINALLY discovered from ---
    try:
        provider = provider_for_source(adopted.discovery_source, client=client)
    except (DiscoveryConfigError, ValueError):
        # Our configuration, not a statement about the product.
        logger.warning(
            "Cannot revalidate %s: provider %r is not configured here",
            adopted.adopted_product_id,
            adopted.discovery_source,
        )
        return snapshot(
            RevalidationStatus.NOT_REVALIDATABLE,
            IdentityMatch.NOT_ATTEMPTED,
            RevalidationReason.PROVIDER_NOT_CONFIGURED,
        )

    # --- FRESH provider requests, through the existing abstraction.
    # Each query is tried until the durable identity turns up. A provider
    # failure stops the ladder immediately: retrying a broader query after a
    # 401 or a rate limit would spend more requests on the same failure, and
    # would risk reporting "not found" for what is really an outage. ---
    any_results = False
    for query in queries:
        observed_at = datetime.now(timezone.utc)
        try:
            result = provider.search(query, limit=limit, observed_at=observed_at)
        except DiscoveryConfigError:
            return snapshot(
                RevalidationStatus.NOT_REVALIDATABLE,
                IdentityMatch.NOT_ATTEMPTED,
                RevalidationReason.PROVIDER_NOT_CONFIGURED,
            )
        except DiscoveryError as exc:
            status, reason = _classify_provider_error(exc)
            logger.warning(
                "Revalidation provider failure for %s (%s): %s",
                adopted.adopted_product_id,
                type(exc).__name__,
                status.value,
            )
            return snapshot(status, IdentityMatch.NOT_ATTEMPTED, reason)

        if not result.products:
            continue
        any_results = True

        # --- existing Phase 10F + 10H pipeline, single-sourced ---
        normalized = normalize_products(
            result.products, DEFAULT_NORMALIZATION_CURRENCY
        )
        normalized = attach_merchant_identity(
            normalized, result.merchant_evidence
        )

        fresh = find_identity_match(
            normalized, adopted.discovery_source, adopted.discovery_external_id
        )
        if fresh is not None:
            return snapshot(
                RevalidationStatus.REVALIDATED,
                IdentityMatch.EXACT_EXTERNAL_ID,
                RevalidationReason.IDENTITY_CONFIRMED,
                fresh,
            )

    # Every query was answered and none of the results carried the adopted
    # product's durable identity. Refuse rather than accept a different
    # product under the same record.
    return snapshot(
        RevalidationStatus.IDENTITY_NOT_FOUND,
        IdentityMatch.NOT_FOUND,
        (
            RevalidationReason.NO_RESULT_MATCHED_IDENTITY
            if any_results
            else RevalidationReason.PROVIDER_RETURNED_NO_RESULTS
        ),
    )
