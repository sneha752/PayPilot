"""Server-side discovery evidence cache (Phase 11A).

WHY THIS EXISTS
    Before Phase 11A the discovery layer was entirely transient: a search or
    comparison called Serper/Channel3, normalized the results, returned them,
    and kept nothing. So when a client later said "adopt the product with
    external_id serper:111111", the server had no record of what that product
    was.

    There are only two honest ways to close that gap. Either the client sends
    the product back (and a client-supplied title, price, merchant and URL
    become authoritative — unacceptable), or the SERVER remembers what it
    served. This module is the second option, and it is the reason the
    adoption endpoint can accept nothing but an identifier.

WHAT THIS IS
    A cache of DISCOVERY EVIDENCE, keyed by the provider identity
    (source, external_id). It records what the discovery layer itself
    produced, so adoption can reconstruct a product from the server's own
    data instead of from a request body.

WHAT THIS IS NOT
    NOT a payment authority. NOT a catalog. NOT a price quote. NOT an offer.
    A row here means "PayPilot observed this offer at this time", never
    "PayPilot will sell this at this price". Nothing in this module may be
    read as an amount to charge: a purchase phase must re-validate against a
    live source, because a cached observation is stale by definition the
    moment it is written.

    It is also not a write surface for clients. The only writer is the
    discovery layer, and record_products() accepts NormalizedDiscoveredProduct
    instances the server built — never a dict, never a request payload. A
    client cannot reach this table except by running a real discovery search.

STORAGE SHAPE
    Identity and freshness live in explicit columns, because those are what
    gets queried. The product itself is stored as the exact JSON of the
    NormalizedDiscoveredProduct, because it is only ever read back whole and
    must round-trip EXACTLY — including Decimal prices, which SQLite's REAL
    type would silently corrupt into binary floats.

    Reads re-validate that JSON through the Pydantic model, so corrupt or
    schema-drifted evidence raises MalformedDiscoveryEvidenceError instead of
    producing a half-built product. Failing loudly is the safe outcome: a
    record nobody can parse must never become an adopted product.

FOLLOWS THE LEDGER'S CONVENTIONS
    stdlib sqlite3, no ORM, parameterized SQL only, the same
    backend/data/paypilot.db file and the same PAYPILOT_DB_PATH test
    isolation as transaction_service and session_service — in its own table.
    The transactions and sessions tables are never touched.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from pydantic import ValidationError

from ..schemas.discovery import NormalizedDiscoveredProduct

logger = logging.getLogger("paypilot.discovery_evidence")

# backend/data/paypilot.db (this file is backend/app/services/...), the same
# database the ledger and session store use.
_DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "paypilot.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS discovery_evidence (
    source        TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    observed_at   TEXT NOT NULL,
    recorded_at   TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    PRIMARY KEY (source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_discovery_evidence_recorded_at
    ON discovery_evidence (recorded_at);
"""


class DiscoveryEvidenceError(Exception):
    """Base class for evidence-store failures."""


class DiscoveryEvidenceNotFoundError(DiscoveryEvidenceError):
    """No evidence is stored for the given provider identity.

    Raised when a client names a product the server never served. The
    correct response is to refuse, NOT to accept client-supplied details.
    """


class MalformedDiscoveryEvidenceError(DiscoveryEvidenceError):
    """Stored evidence exists but cannot be parsed back into a product.

    Corrupt row, truncated JSON, or evidence written by an incompatible
    schema version. Adoption must fail rather than build a partial record
    from data the server cannot vouch for.
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
        # Two statements, so executescript (the session store's pattern).
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _identity(source: str, external_id: str) -> tuple[str, str]:
    """Normalize and validate a provider identity before it reaches SQL.

    Raises:
        ValueError: either part is blank after trimming. A blank identity
            identifies nothing and must never match a row.
    """
    cleaned_source = (source or "").strip()
    cleaned_external_id = (external_id or "").strip()
    if not cleaned_source:
        raise ValueError("source must be a non-empty string")
    if not cleaned_external_id:
        raise ValueError("external_id must be a non-empty string")
    return cleaned_source, cleaned_external_id


def record_products(products: Iterable[NormalizedDiscoveredProduct]) -> int:
    """Store the evidence for products the discovery layer just produced.

    Upserts on (source, external_id): re-observing a product refreshes its
    evidence, so the stored row is always the most recent observation rather
    than a frozen first sighting. Adoption snapshots the evidence at adoption
    time, so refreshing here never rewrites an already-adopted record.

    Accepts only NormalizedDiscoveredProduct instances the server built. A
    product with a blank identity is skipped rather than stored under a key
    nothing can look up.

    Returns:
        How many rows were written.
    """
    rows = []
    for product in products:
        try:
            source, external_id = _identity(product.source, product.external_id)
        except ValueError:
            logger.warning(
                "Skipping discovery evidence with a blank provider identity"
            )
            continue
        rows.append(
            (
                source,
                external_id,
                product.observed_at.isoformat(),
                _now(),
                # Exact JSON: Decimal prices serialize as strings, so the
                # round-trip is lossless.
                product.model_dump_json(),
            )
        )

    if not rows:
        return 0

    with _connect() as conn:
        conn.executemany(
            """INSERT INTO discovery_evidence (
                   source, external_id, observed_at, recorded_at, evidence_json
               ) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(source, external_id) DO UPDATE SET
                   observed_at   = excluded.observed_at,
                   recorded_at   = excluded.recorded_at,
                   evidence_json = excluded.evidence_json""",
            rows,
        )

    logger.info("Recorded discovery evidence for %d product(s)", len(rows))
    return len(rows)


def get_product(source: str, external_id: str) -> NormalizedDiscoveredProduct:
    """Reconstruct one stored product from the server's own evidence.

    This is the function that lets the adoption endpoint accept an identifier
    and nothing else.

    Raises:
        ValueError: a blank source or external_id.
        DiscoveryEvidenceNotFoundError: nothing is stored for that identity.
        MalformedDiscoveryEvidenceError: the row exists but will not parse.
    """
    cleaned_source, cleaned_external_id = _identity(source, external_id)

    with _connect() as conn:
        row = conn.execute(
            """SELECT evidence_json FROM discovery_evidence
               WHERE source = ? AND external_id = ?""",
            (cleaned_source, cleaned_external_id),
        ).fetchone()

    if row is None:
        raise DiscoveryEvidenceNotFoundError(
            f"No discovery evidence stored for {cleaned_source} product "
            f"{cleaned_external_id}. Run a discovery comparison for it first; "
            "product details are never accepted from the client."
        )

    try:
        product = NormalizedDiscoveredProduct.model_validate_json(
            row["evidence_json"]
        )
    except ValidationError as exc:
        logger.error(
            "Stored discovery evidence for %s/%s is unparseable",
            cleaned_source,
            cleaned_external_id,
        )
        raise MalformedDiscoveryEvidenceError(
            f"Stored discovery evidence for {cleaned_source} product "
            f"{cleaned_external_id} is malformed and cannot be adopted"
        ) from exc

    # Defence in depth: the row key is authoritative for identity, so a row
    # whose payload disagrees with its own key is not trustworthy evidence.
    if (
        product.source.strip() != cleaned_source
        or product.external_id.strip() != cleaned_external_id
    ):
        logger.error(
            "Stored discovery evidence for %s/%s disagrees with its own key",
            cleaned_source,
            cleaned_external_id,
        )
        raise MalformedDiscoveryEvidenceError(
            f"Stored discovery evidence for {cleaned_source} product "
            f"{cleaned_external_id} does not match its own provider identity"
        )

    return product


def count_products() -> int:
    """How many evidence rows are stored. Diagnostics and tests only."""
    with _connect() as conn:
        return int(
            conn.execute("SELECT COUNT(*) AS n FROM discovery_evidence").fetchone()[
                "n"
            ]
        )
