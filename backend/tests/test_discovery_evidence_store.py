"""Discovery evidence cache tests (Phase 11A).

The cache exists so the adoption endpoint can accept an identifier and
nothing else. These tests pin the two properties that makes possible:

  - a product the server recorded round-trips EXACTLY, Decimals included;
  - anything else (absent, corrupt, key/payload mismatch, blank identity)
    fails loudly rather than yielding a half-built product.

Every test runs against its own temporary SQLite file (conftest), so the
development database is never touched.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.schemas.discovery import NormalizedDiscoveredProduct
from app.services import discovery_evidence_store
from app.services.discovery_evidence_store import (
    DiscoveryEvidenceNotFoundError,
    MalformedDiscoveryEvidenceError,
    count_products,
    get_product,
    record_products,
)

NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)


def product(**overrides) -> NormalizedDiscoveredProduct:
    fields = {
        "external_id": "serper:111111",
        "source": "serper",
        "title": "Sony WH-CH520 Wireless Headphones",
        "description": "On-ear wireless headphones.",
        "brand": None,
        "category": None,
        "image_url": "https://img.example/ch520.jpg",
        "product_url": "https://www.google.com/shopping/product/1",
        "retailer_domain": "www.google.com",
        "availability": None,
        "observed_price": Decimal("19.99"),
        "observed_currency": "USD",
        "observed_at": NOW,
        "normalized_price": Decimal("1824.09"),
        "normalized_currency": "INR",
        "conversion_rate": Decimal("91.25"),
        "conversion_source": "configured_demo_rate",
        "rate_observed_at": None,
        "merchant_name": "Amazon",
        "merchant_domain": None,
    }
    fields.update(overrides)
    return NormalizedDiscoveredProduct(**fields)


def _raw_connect():
    """A direct connection to the same temporary DB, for corruption tests."""
    conn = sqlite3.connect(discovery_evidence_store._db_path())
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Round-trip fidelity
# ---------------------------------------------------------------------------


def test_recorded_product_round_trips_exactly():
    original = product()

    assert record_products([original]) == 1
    restored = get_product("serper", "serper:111111")

    assert restored == original


def test_decimal_prices_survive_the_round_trip_exactly():
    """SQLite REAL would corrupt these; the cache must not."""
    original = product(
        observed_price=Decimal("1299.95"), normalized_price=Decimal("118620.44")
    )

    record_products([original])
    restored = get_product("serper", "serper:111111")

    assert isinstance(restored.observed_price, Decimal)
    assert restored.observed_price == Decimal("1299.95")
    assert str(restored.observed_price) == "1299.95"
    assert str(restored.normalized_price) == "118620.44"


def test_merchant_identity_and_null_fields_are_preserved():
    record_products([product()])

    restored = get_product("serper", "serper:111111")

    assert restored.merchant_name == "Amazon"
    assert restored.merchant_domain is None
    assert restored.brand is None
    assert restored.availability is None
    # Provider evidence is kept verbatim, Google host included.
    assert restored.retailer_domain == "www.google.com"


def test_observed_at_is_preserved_as_an_aware_utc_datetime():
    record_products([product()])

    restored = get_product("serper", "serper:111111")

    assert restored.observed_at == NOW
    assert restored.observed_at.tzinfo is not None


def test_a_product_with_no_price_is_stored_without_inventing_one():
    record_products(
        [
            product(
                observed_price=None,
                observed_currency=None,
                normalized_price=None,
                normalized_currency=None,
            )
        ]
    )

    restored = get_product("serper", "serper:111111")

    assert restored.observed_price is None
    assert restored.observed_currency is None
    assert restored.normalized_price is None


def test_recording_a_batch_stores_every_product():
    products = [product(external_id=f"serper:{i}") for i in range(1, 6)]

    assert record_products(products) == 5
    assert count_products() == 5


def test_recording_nothing_is_a_no_op():
    assert record_products([]) == 0
    assert count_products() == 0


# ---------------------------------------------------------------------------
# Identity: composite key, upsert, cross-provider isolation
# ---------------------------------------------------------------------------


def test_re_recording_refreshes_rather_than_duplicating():
    record_products([product(title="Old Title", observed_price=Decimal("19.99"))])
    record_products([product(title="New Title", observed_price=Decimal("24.50"))])

    assert count_products() == 1
    restored = get_product("serper", "serper:111111")
    assert restored.title == "New Title"
    assert restored.observed_price == Decimal("24.50")


def test_the_same_external_id_under_two_providers_stays_separate():
    record_products(
        [
            product(source="serper", external_id="prod_1", title="Serper version"),
            product(source="channel3", external_id="prod_1", title="Channel3 version"),
        ]
    )

    assert count_products() == 2
    assert get_product("serper", "prod_1").title == "Serper version"
    assert get_product("channel3", "prod_1").title == "Channel3 version"


def test_identifiers_are_trimmed_on_lookup():
    record_products([product()])

    assert get_product("  serper  ", "  serper:111111  ").title == product().title


def test_lookup_is_case_sensitive_and_does_not_guess():
    record_products([product()])

    with pytest.raises(DiscoveryEvidenceNotFoundError):
        get_product("SERPER", "serper:111111")


def test_a_product_with_a_blank_identity_is_skipped_not_stored():
    """It could never be looked up, so storing it would be dead data."""
    assert record_products([product(source="   ")]) == 0
    assert count_products() == 0


# ---------------------------------------------------------------------------
# Failing safely
# ---------------------------------------------------------------------------


def test_unknown_product_raises_not_found():
    with pytest.raises(DiscoveryEvidenceNotFoundError) as excinfo:
        get_product("serper", "serper:does-not-exist")

    # The message must point at discovery, not invite a client payload.
    assert "serper:does-not-exist" in str(excinfo.value)
    assert "never accepted from the client" in str(excinfo.value)


@pytest.mark.parametrize(
    ("source", "external_id"),
    [("", "serper:1"), ("   ", "serper:1"), ("serper", ""), ("serper", "   ")],
)
def test_blank_identity_is_rejected_before_any_query(source, external_id):
    with pytest.raises(ValueError):
        get_product(source, external_id)


def test_unparseable_stored_evidence_raises_malformed():
    record_products([product()])
    with _raw_connect() as conn:
        conn.execute(
            "UPDATE discovery_evidence SET evidence_json = ? WHERE source = ?",
            ("{not valid json", "serper"),
        )
        conn.commit()

    with pytest.raises(MalformedDiscoveryEvidenceError):
        get_product("serper", "serper:111111")


def test_schema_drifted_stored_evidence_raises_malformed():
    """Valid JSON that is not a product must not yield a partial product."""
    record_products([product()])
    with _raw_connect() as conn:
        conn.execute(
            "UPDATE discovery_evidence SET evidence_json = ? WHERE source = ?",
            ('{"title": "Only a title"}', "serper"),
        )
        conn.commit()

    with pytest.raises(MalformedDiscoveryEvidenceError):
        get_product("serper", "serper:111111")


def test_evidence_disagreeing_with_its_own_key_raises_malformed():
    """The row key is authoritative; a mismatched payload is untrustworthy."""
    record_products([product()])
    with _raw_connect() as conn:
        conn.execute(
            "UPDATE discovery_evidence SET evidence_json = ? WHERE source = ?",
            (product(external_id="serper:999999").model_dump_json(), "serper"),
        )
        conn.commit()

    with pytest.raises(MalformedDiscoveryEvidenceError) as excinfo:
        get_product("serper", "serper:111111")

    assert "does not match its own provider identity" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Table isolation: the cache lives beside the payment tables, never in them
# ---------------------------------------------------------------------------


def test_the_cache_uses_its_own_table_and_constraints():
    record_products([product()])

    with _raw_connect() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(discovery_evidence)")
        }
        pk = [
            row["name"]
            for row in conn.execute("PRAGMA table_info(discovery_evidence)")
            if row["pk"]
        ]

    assert "discovery_evidence" in tables
    assert columns == {
        "source",
        "external_id",
        "observed_at",
        "recorded_at",
        "evidence_json",
    }
    # Provider identity is the primary key, so uniqueness is enforced by SQL.
    assert set(pk) == {"source", "external_id"}


def test_the_cache_holds_no_payment_or_paypal_columns():
    record_products([product()])

    with _raw_connect() as conn:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(discovery_evidence)")
        }

    for forbidden in (
        "paypal_order_id",
        "paypal_capture_id",
        "paypal_amount",
        "paypal_currency",
        "refund_id",
        "transaction_id",
        "amount",
        "purchase_price",
        "price",
    ):
        assert forbidden not in columns


def test_recording_evidence_does_not_touch_the_payment_tables():
    """Stronger than "no rows": the cache never even creates them."""
    record_products([product()])

    with _raw_connect() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert "discovery_evidence" in tables
    assert "transactions" not in tables
    assert "sessions" not in tables
    assert "session_turns" not in tables
