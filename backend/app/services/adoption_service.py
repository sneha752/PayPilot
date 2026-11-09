"""External-product adoption boundary (Phase 11A).

WHAT ADOPTION IS
    Turning a discovery result the server itself produced into a
    PayPilot-OWNED persisted record. That record becomes PayPilot's
    authoritative representation of the selected external product.

WHAT ADOPTION IS NOT
    It does not make anything purchasable. There is no bridge from here to
    payment, and building one is a later phase's job, not an oversight.

    This module imports NO payment code: not paypal, not the policy engine,
    not purchase execution, not the capture or refund services, not the
    transaction ledger, not sessions. It also calls no AI provider and makes
    no external request of any kind. Adoption is a database read followed by
    a database write.

THE TRUST BOUNDARY, CONCRETELY
    adopt() takes (source, external_id) and NOTHING else. Every product fact
    it writes is read from discovery_evidence_store — data only the discovery
    layer writes, from provider responses the server fetched itself.

    There is therefore no parameter, keyword, or dict through which a caller
    could supply a title, price, currency, merchant, URL, PayPal amount, or
    order id. The absence is structural, not a matter of filtering: the
    function signature simply has nowhere to put one.

OBSERVED PRICE IS NOT A PURCHASE PRICE
    The adopted record preserves observed_price / observed_currency as
    discovery evidence, and the derived normalized price alongside it. Both
    are snapshots of what was seen, not amounts to charge. An adopted record
    is stale the moment the retailer changes a price, which is exactly why a
    future purchase phase must re-validate against a live source rather than
    read these fields. Nothing here produces a payable amount.

IDENTITY AND DUPLICATES
    The provider identity (discovery_source, discovery_external_id) is the
    unique key, enforced by a UNIQUE constraint in SQL, not merely by a
    pre-check. Title, merchant and price are explicitly NOT identity: they
    change over time and are attacker-influenceable in a way a provider id
    is not.

    Adoption is IDEMPOTENT. Re-adopting the same provider identity returns
    the EXISTING record and reports created=False; it never inserts a
    duplicate and never overwrites the original snapshot. Refusing to
    overwrite matters: silently replacing an adopted record's observed price
    with a newer one would make the record look current when it is not.

FOLLOWS THE LEDGER'S CONVENTIONS
    stdlib sqlite3, no ORM, parameterized SQL only, the same
    backend/data/paypilot.db and PAYPILOT_DB_PATH test isolation as
    transaction_service — in its own table. The transactions, sessions and
    discovery_evidence tables are never written here (evidence is read-only
    to this module), and data/products.json is never read or touched.
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
from . import discovery_evidence_store
from .product_identity import classify as classify_identity
from .discovery_evidence_store import (  # re-exported for API error mapping
    DiscoveryEvidenceError,  # noqa: F401
    DiscoveryEvidenceNotFoundError,  # noqa: F401
    MalformedDiscoveryEvidenceError,  # noqa: F401
)

logger = logging.getLogger("paypilot.adoption")

_DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "paypilot.db"

# Prices are TEXT, holding the exact Decimal string ("19.99", "1824.09") —
# the same choice the ledger makes for paypal_amount. SQLite's REAL would
# turn an exact decimal into a binary float, and a money value that drifts
# during a round-trip is not evidence of anything.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS adopted_products (
    adopted_product_id    TEXT PRIMARY KEY,
    discovery_source      TEXT NOT NULL,
    discovery_external_id TEXT NOT NULL,
    title                 TEXT NOT NULL,
    description           TEXT,
    brand                 TEXT,
    category              TEXT,
    merchant_name         TEXT,
    merchant_domain       TEXT,
    product_url           TEXT,
    observed_price        TEXT,
    observed_currency     TEXT,
    normalized_price      TEXT,
    normalized_currency   TEXT,
    observed_at           TEXT NOT NULL,
    adopted_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    status                TEXT NOT NULL,
    UNIQUE (discovery_source, discovery_external_id)
);
CREATE INDEX IF NOT EXISTS idx_adopted_products_status
    ON adopted_products (status);
"""

#: Columns added after the table first shipped, with their definitions.
#: Applied by _migrate() to databases created before Phase 11B.1.
#:
#: The UNIQUE key is deliberately NOT changed. It keys on the provider
#: OBSERVATION (discovery_source, discovery_external_id), which is exactly
#: what adoption deduplicates: "the user picked this result". Re-keying on
#: product_identity_key would be wrong twice over — it is null for
#: observation-only products (so it could not be unique), and it would merge
#: two genuinely separate adoptions of the same product.
_ADDED_COLUMNS = (
    (
        "product_identity_kind",
        "TEXT NOT NULL DEFAULT 'PROVIDER_OBSERVATION_ONLY'",
    ),
    ("product_identity_key", "TEXT"),
)

_COLUMNS = (
    "adopted_product_id",
    "discovery_source",
    "discovery_external_id",
    "title",
    "description",
    "brand",
    "category",
    "merchant_name",
    "merchant_domain",
    "product_url",
    "observed_price",
    "observed_currency",
    "normalized_price",
    "normalized_currency",
    "observed_at",
    "adopted_at",
    "updated_at",
    "status",
    "product_identity_kind",
    "product_identity_key",
)


def _migrate(conn: sqlite3.Connection) -> None:
    """Add post-launch columns to an existing table, then backfill them.

    Additive only: no column is dropped, renamed or retyped, and no
    constraint changes, so a database written by an earlier phase stays
    readable throughout. ALTER TABLE ... ADD COLUMN is guarded by a
    PRAGMA check rather than a try/except, so a real error is not masked.

    Backfill is computed from columns the row already has — the provider
    name and its id — so pre-11B.1 rows get the same classification they
    would have received at adoption time. No row is left claiming a
    durability it was never assessed for.
    """
    existing = {
        row["name"] for row in conn.execute("PRAGMA table_info(adopted_products)")
    }
    added = [name for name, _ in _ADDED_COLUMNS if name not in existing]
    for name, definition in _ADDED_COLUMNS:
        if name in existing:
            continue
        conn.execute(f"ALTER TABLE adopted_products ADD COLUMN {name} {definition}")

    if not added:
        return

    rows = conn.execute(
        "SELECT adopted_product_id, discovery_source, discovery_external_id "
        "FROM adopted_products"
    ).fetchall()
    for row in rows:
        identity = classify_identity(
            row["discovery_source"], row["discovery_external_id"]
        )
        conn.execute(
            """UPDATE adopted_products
               SET product_identity_kind = ?, product_identity_key = ?
               WHERE adopted_product_id = ?""",
            (
                identity.kind.value,
                identity.product_identity_key,
                row["adopted_product_id"],
            ),
        )
    logger.info(
        "Migrated adopted_products: added %s, backfilled %d row(s)",
        ", ".join(added),
        len(rows),
    )


class AdoptedProductNotFoundError(Exception):
    """Raised when no adopted product matches the given id / identity."""


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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decimal_to_text(value: Optional[Decimal]) -> Optional[str]:
    """Exact string form of a Decimal, or None. No rounding, no float."""
    return None if value is None else str(value)


def _text_to_decimal(value: Optional[str]) -> Optional[Decimal]:
    return None if value is None else Decimal(value)


def _to_record(row: sqlite3.Row) -> AdoptedProduct:
    data = dict(row)
    data["observed_price"] = _text_to_decimal(data["observed_price"])
    data["normalized_price"] = _text_to_decimal(data["normalized_price"])
    return AdoptedProduct(**data)


def _identity(source: str, external_id: str) -> tuple[str, str]:
    """Normalize and validate the provider identity.

    Raises:
        ValueError: either part is blank after trimming.
    """
    cleaned_source = (source or "").strip()
    cleaned_external_id = (external_id or "").strip()
    if not cleaned_source:
        raise ValueError("source must be a non-empty string")
    if not cleaned_external_id:
        raise ValueError("external_id must be a non-empty string")
    return cleaned_source, cleaned_external_id


def _build_record(product: NormalizedDiscoveredProduct) -> AdoptedProduct:
    """Assemble an adopted record from stored discovery evidence ONLY.

    `product` came out of discovery_evidence_store, so every value below is
    the server's own. Note what is NOT copied: no ranking text, no AI reason
    string, no price_note — explanatory prose is not product data and would
    make a model's words look authoritative.
    """
    now = _now()
    # Classified from the provider name and its id ONLY. Adoption does not
    # upgrade an observation id into durable identity: picking a product
    # proves nothing about whether that id still means it tomorrow.
    identity = classify_identity(product.source, product.external_id)
    return AdoptedProduct(
        adopted_product_id=f"adopt_{uuid.uuid4().hex}",
        discovery_source=product.source,
        discovery_external_id=product.external_id,
        title=product.title,
        description=product.description,
        brand=product.brand,
        category=product.category,
        merchant_name=product.merchant_name,
        merchant_domain=product.merchant_domain,
        product_url=product.product_url,
        # OBSERVED evidence, carried over unchanged. Not a payment amount.
        observed_price=product.observed_price,
        observed_currency=product.observed_currency,
        normalized_price=product.normalized_price,
        normalized_currency=product.normalized_currency,
        # The observation's own timestamp, not the adoption's.
        observed_at=product.observed_at.isoformat(),
        adopted_at=now,
        updated_at=now,
        # Valid evidence produced a record. ACTIVE != purchasable.
        status=AdoptedProductStatus.ACTIVE,
        product_identity_kind=identity.kind,
        product_identity_key=identity.product_identity_key,
    )


def get_by_identity(source: str, external_id: str) -> AdoptedProduct:
    """Fetch the adopted product for a provider identity, or raise not-found."""
    cleaned_source, cleaned_external_id = _identity(source, external_id)
    with _connect() as conn:
        row = conn.execute(
            """SELECT * FROM adopted_products
               WHERE discovery_source = ? AND discovery_external_id = ?""",
            (cleaned_source, cleaned_external_id),
        ).fetchone()
    if row is None:
        raise AdoptedProductNotFoundError(
            f"No adopted product for {cleaned_source} product {cleaned_external_id}"
        )
    return _to_record(row)


def get(adopted_product_id: str) -> AdoptedProduct:
    """Fetch one adopted product by its PayPilot id, or raise not-found."""
    identifier = (adopted_product_id or "").strip()
    if not identifier:
        raise ValueError("adopted_product_id must be a non-empty string")
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM adopted_products WHERE adopted_product_id = ?",
            (identifier,),
        ).fetchone()
    if row is None:
        raise AdoptedProductNotFoundError(
            f"No adopted product found with id {identifier}"
        )
    return _to_record(row)


def adopt(source: str, external_id: str) -> tuple[AdoptedProduct, bool]:
    """Adopt the discovery result identified by (source, external_id).

    The ONLY inputs are the two identifier parts. Product facts are read
    from the server's stored discovery evidence; nothing a caller passes can
    become product data, because nothing else can be passed.

    Args:
        source: Discovery provider name, e.g. "serper".
        external_id: The provider's product id, e.g. "serper:111111".

    Returns:
        (record, created) — `created` is False when an existing adopted
        record for this provider identity was returned unchanged.

    Raises:
        ValueError: blank source or external_id (no lookup is attempted).
        DiscoveryEvidenceNotFoundError: the server has no evidence for that
            identity, so there is nothing it can honestly adopt.
        MalformedDiscoveryEvidenceError: stored evidence will not parse.
    """
    cleaned_source, cleaned_external_id = _identity(source, external_id)

    # Idempotence: an existing adoption wins outright. Checked before the
    # evidence read so a re-adopt cannot be affected by refreshed evidence.
    try:
        existing = get_by_identity(cleaned_source, cleaned_external_id)
    except AdoptedProductNotFoundError:
        pass
    else:
        logger.info(
            "Adoption reused existing record %s for %s/%s",
            existing.adopted_product_id,
            cleaned_source,
            cleaned_external_id,
        )
        return existing, False

    # The authoritative read. Raises rather than falling back to anything
    # client-supplied when the server has no evidence.
    product = discovery_evidence_store.get_product(
        cleaned_source, cleaned_external_id
    )

    record = _build_record(product)

    with _connect() as conn:
        try:
            conn.execute(
                f"""INSERT INTO adopted_products ({", ".join(_COLUMNS)})
                    VALUES ({", ".join("?" * len(_COLUMNS))})""",
                (
                    record.adopted_product_id,
                    record.discovery_source,
                    record.discovery_external_id,
                    record.title,
                    record.description,
                    record.brand,
                    record.category,
                    record.merchant_name,
                    record.merchant_domain,
                    record.product_url,
                    _decimal_to_text(record.observed_price),
                    record.observed_currency,
                    _decimal_to_text(record.normalized_price),
                    record.normalized_currency,
                    record.observed_at,
                    record.adopted_at,
                    record.updated_at,
                    record.status.value,
                    record.product_identity_kind.value,
                    record.product_identity_key,
                ),
            )
        except sqlite3.IntegrityError:
            # The UNIQUE constraint fired: a concurrent request adopted the
            # same identity between our check and this insert. The constraint
            # — not the pre-check — is what actually guarantees no duplicate.
            logger.info(
                "Concurrent adoption detected for %s/%s; reusing the winner",
                cleaned_source,
                cleaned_external_id,
            )
            return get_by_identity(cleaned_source, cleaned_external_id), False

    logger.info(
        "Adopted %s product %s as %s (status=%s)",
        cleaned_source,
        cleaned_external_id,
        record.adopted_product_id,
        record.status.value,
    )
    return record, True


def set_status(
    adopted_product_id: str, status: AdoptedProductStatus
) -> AdoptedProduct:
    """Transition an adopted record's lifecycle status. Facts stay frozen.

    The ONLY mutating operation on an adopted product, and deliberately the
    narrowest one possible: it writes `status` and `updated_at` and touches
    no other column. The adopted snapshot records what was adopted, so its
    title, merchant, URL, observed price and currency can never be rewritten
    — not by revalidation, and not by any caller of this module.

    The caller decides WHETHER a transition is warranted (see
    revalidation_service, which refuses to transition on a transient
    provider outage). This function only records it.

    Raises:
        ValueError: blank id.
        AdoptedProductNotFoundError: no such adopted product.
    """
    identifier = (adopted_product_id or "").strip()
    if not identifier:
        raise ValueError("adopted_product_id must be a non-empty string")

    with _connect() as conn:
        row = conn.execute(
            "SELECT adopted_product_id FROM adopted_products "
            "WHERE adopted_product_id = ?",
            (identifier,),
        ).fetchone()
        if row is None:
            raise AdoptedProductNotFoundError(
                f"No adopted product found with id {identifier}"
            )
        conn.execute(
            """UPDATE adopted_products
               SET status = ?, updated_at = ?
               WHERE adopted_product_id = ?""",
            (status.value, _now(), identifier),
        )

    logger.info(
        "Adopted product %s status -> %s", identifier, status.value
    )
    return get(identifier)


def count() -> int:
    """How many adopted products exist. Diagnostics and tests only."""
    with _connect() as conn:
        return int(
            conn.execute("SELECT COUNT(*) AS n FROM adopted_products").fetchone()["n"]
        )
