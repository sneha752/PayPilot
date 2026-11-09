"""External-product adoption tests (Phase 11A).

The property these tests exist to defend: a client can say WHICH discovery
result to adopt, and nothing else. Every product fact in an adopted record
comes from evidence the server itself recorded, so a forged title, price,
currency, merchant, URL, PayPal amount or order id has no path to becoming
authoritative.

A second, equally important property: adoption is NOT purchase. The payment
core is boobytrapped throughout — any PayPal, Policy Engine, purchase
execution, capture, refund, ledger or session touch fails the test — and an
adopted product is shown to remain non-purchasable.

No real Serper, Channel3 or AI request occurs: providers are faked and each
test gets its own temporary SQLite database (conftest).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.agents import intent_agent
from app.main import app
from app.schemas.adoption import AdoptedProductStatus, AdoptionRequest
from app.schemas.discovery import NormalizedDiscoveredProduct
from app.services import (
    adoption_service,
    discovery_comparison_service,
    discovery_evidence_store,
    discovery_service,
)
from app.services.adoption_service import (
    AdoptedProductNotFoundError,
    adopt,
    get_by_identity,
)
from app.services.channel3_client import (
    Channel3Offer,
    Channel3Price,
    Channel3Product,
    Channel3SearchResult,
)
from app.services.discovery_evidence_store import (
    DiscoveryEvidenceNotFoundError,
    MalformedDiscoveryEvidenceError,
    record_products,
)
from app.services.providers import AIProvider

client = TestClient(app)

NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)

#: A Google Shopping link: the merchant is Amazon, the host is Google.
GOOGLE_LINK = "https://www.google.com/shopping/product/1"


# ---------------------------------------------------------------------------
# Fixtures and fakes
# ---------------------------------------------------------------------------


def evidence_product(**overrides) -> NormalizedDiscoveredProduct:
    """One fully-normalized discovery record, as the server would build it."""
    fields = {
        "external_id": "serper:111111",
        "source": "serper",
        "title": "Sony WH-CH520 Wireless Headphones",
        "description": "On-ear wireless headphones.",
        "brand": "Sony",
        "category": "headphones",
        "image_url": "https://img.example/ch520.jpg",
        "product_url": GOOGLE_LINK,
        "retailer_domain": "www.google.com",
        "availability": "InStock",
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


@pytest.fixture
def frozen_core(monkeypatch):
    """Boobytrap every frozen subsystem: a touch from adoption fails."""
    from app import paypal, policy
    from app.services import (
        purchase_execution_service,
        refund_service,
        session_service,
        transaction_service,
    )

    def bomb(name):
        def _bomb(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError(f"adoption must not touch {name}")

        return _bomb

    monkeypatch.setattr(paypal, "get_access_token", bomb("PayPal auth"))
    monkeypatch.setattr(paypal, "create_order", bomb("PayPal create_order"))
    monkeypatch.setattr(paypal, "capture_order", bomb("PayPal capture_order"))
    monkeypatch.setattr(paypal, "refund_capture", bomb("PayPal refund_capture"))
    monkeypatch.setattr(paypal, "get_order", bomb("PayPal get_order"))
    monkeypatch.setattr(policy, "evaluate_policy", bomb("the Policy Engine"))
    monkeypatch.setattr(
        purchase_execution_service, "execute_purchase", bomb("purchase execution")
    )
    monkeypatch.setattr(
        transaction_service, "create_transaction", bomb("the transaction ledger")
    )
    monkeypatch.setattr(
        transaction_service, "update_status", bomb("the transaction ledger")
    )
    monkeypatch.setattr(
        transaction_service, "mark_captured", bomb("the transaction ledger")
    )
    monkeypatch.setattr(refund_service, "refund_transaction", bomb("refunds"))
    monkeypatch.setattr(session_service, "create_session", bomb("sessions"))
    monkeypatch.setattr(session_service, "set_pending_action", bomb("sessions"))


@pytest.fixture
def no_ai(monkeypatch):
    """Any AI provider resolution from the adoption path fails the test."""
    from app.services import providers

    def bomb(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("adoption must not invoke AI")

    monkeypatch.setattr(providers, "get_provider", bomb)
    monkeypatch.setattr(intent_agent, "get_provider", bomb)
    monkeypatch.setattr(discovery_comparison_service, "get_provider", bomb)


@pytest.fixture
def stored(frozen_core):
    """One product already recorded as server-side discovery evidence."""
    product = evidence_product()
    record_products([product])
    return product


def post_adopt(payload):
    return client.post("/discovery/adopt", json=payload)


def _raw_connect():
    conn = sqlite3.connect(adoption_service._db_path())
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# 1-2. Successful adoption
# ---------------------------------------------------------------------------


def test_successful_adoption_returns_201_and_the_record(stored, no_ai):
    response = post_adopt({"source": "serper", "external_id": "serper:111111"})

    assert response.status_code == 201
    body = response.json()
    assert body["adopted_product_id"].startswith("adopt_")
    assert body["discovery_source"] == "serper"
    assert body["discovery_external_id"] == "serper:111111"
    assert body["title"] == "Sony WH-CH520 Wireless Headphones"
    assert body["status"] == "ACTIVE"


def test_successful_adoption_persists_exactly_one_row(stored, no_ai):
    post_adopt({"source": "serper", "external_id": "serper:111111"})

    assert adoption_service.count() == 1
    record = get_by_identity("serper", "serper:111111")
    assert record.title == "Sony WH-CH520 Wireless Headphones"


def test_adoption_of_a_product_discovered_through_the_comparison_endpoint(
    monkeypatch, frozen_core
):
    """End to end: a real discovery run, then adopt what it served."""

    class FakeChannel3Client:
        def search(self, query=None, *, limit=20, page_token=None, **kwargs):
            return Channel3SearchResult(
                products=[
                    Channel3Product(
                        id="prod_42",
                        title="Sony Wireless Headphones",
                        description="Noise cancelling.",
                        offers=[
                            Channel3Offer(
                                url="https://www.bestbuy.com/site/prod_42",
                                domain="bestbuy.com",
                                price=Channel3Price(
                                    price=Decimal("299.99"), currency="USD"
                                ),
                                availability="InStock",
                            )
                        ],
                    )
                ]
            )

    class FakeAI(AIProvider):
        name = "fake-ai"

        def extract_intent(self, message):
            return {"category": None}

        def rank_products(self, requirements, candidates, top_k):
            ids = [c["external_id"] for c in candidates][:top_k]
            return {
                "ranked_ids": ids,
                "reasons": [{"product_id": i, "reason": "Fits."} for i in ids],
            }

    ai = FakeAI()
    monkeypatch.setenv("DISCOVERY_PROVIDER", "channel3")
    monkeypatch.setattr(discovery_service, "_make_client", lambda: FakeChannel3Client())
    monkeypatch.setattr(discovery_comparison_service, "get_provider", lambda: ai)
    monkeypatch.setattr(intent_agent, "get_provider", lambda: ai)

    compared = client.post(
        "/discovery/compare", json={"query": "headphones", "top_k": 1}
    )
    assert compared.status_code == 200
    served = compared.json()["results"][0]["product"]

    # The comparison recorded evidence, so adoption needs only the identity.
    response = post_adopt(
        {"source": served["source"], "external_id": served["external_id"]}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["discovery_source"] == "channel3"
    assert body["discovery_external_id"] == "prod_42"
    assert body["title"] == "Sony Wireless Headphones"
    assert body["observed_price"] == "299.99"
    assert body["observed_currency"] == "USD"
    assert body["merchant_domain"] == "bestbuy.com"
    assert body["product_url"] == "https://www.bestbuy.com/site/prod_42"


# ---------------------------------------------------------------------------
# 3-6. Identifier validation
# ---------------------------------------------------------------------------


def test_missing_source_is_rejected(stored, no_ai):
    response = post_adopt({"external_id": "serper:111111"})

    assert response.status_code == 422
    assert adoption_service.count() == 0


def test_missing_external_id_is_rejected(stored, no_ai):
    response = post_adopt({"source": "serper"})

    assert response.status_code == 422
    assert adoption_service.count() == 0


def test_missing_both_identifiers_is_rejected(stored, no_ai):
    assert post_adopt({}).status_code == 422
    assert adoption_service.count() == 0


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n"])
def test_empty_source_is_rejected(stored, no_ai, blank):
    response = post_adopt({"source": blank, "external_id": "serper:111111"})

    assert response.status_code == 422
    assert adoption_service.count() == 0


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n"])
def test_empty_external_id_is_rejected(stored, no_ai, blank):
    response = post_adopt({"source": "serper", "external_id": blank})

    assert response.status_code == 422
    assert adoption_service.count() == 0


def test_oversized_identifiers_are_rejected(stored, no_ai):
    assert post_adopt(
        {"source": "s" * 65, "external_id": "serper:111111"}
    ).status_code == 422
    assert post_adopt(
        {"source": "serper", "external_id": "e" * 513}
    ).status_code == 422
    assert adoption_service.count() == 0


@pytest.mark.parametrize("wrong_type", [123, None, True, ["serper"], {"a": 1}])
def test_non_string_identifiers_are_rejected(stored, no_ai, wrong_type):
    response = post_adopt({"source": wrong_type, "external_id": "serper:111111"})

    assert response.status_code == 422


def test_identifiers_are_trimmed_before_lookup(stored, no_ai):
    response = post_adopt(
        {"source": "  serper  ", "external_id": "  serper:111111  "}
    )

    assert response.status_code == 201
    assert response.json()["discovery_source"] == "serper"
    assert response.json()["discovery_external_id"] == "serper:111111"


# ---------------------------------------------------------------------------
# 7. Unknown discovery product
# ---------------------------------------------------------------------------


def test_unknown_discovery_product_is_404(stored, no_ai):
    response = post_adopt({"source": "serper", "external_id": "serper:nope"})

    assert response.status_code == 404
    assert "serper:nope" in response.json()["detail"]
    assert adoption_service.count() == 0


def test_unknown_provider_is_404_not_invented(stored, no_ai):
    """An unrecognised source has no evidence, so there is nothing to adopt."""
    response = post_adopt({"source": "amazon", "external_id": "serper:111111"})

    assert response.status_code == 404
    assert adoption_service.count() == 0


def test_adopting_with_an_empty_evidence_store_is_404(frozen_core, no_ai):
    assert discovery_evidence_store.count_products() == 0

    response = post_adopt({"source": "serper", "external_id": "serper:111111"})

    assert response.status_code == 404


def test_service_level_unknown_product_raises_typed_error(frozen_core):
    with pytest.raises(DiscoveryEvidenceNotFoundError):
        adopt("serper", "serper:111111")


# ---------------------------------------------------------------------------
# 8, 26. Duplicate adoption is deterministic and idempotent
# ---------------------------------------------------------------------------


def test_duplicate_adoption_returns_200_with_the_same_record(stored, no_ai):
    first = post_adopt({"source": "serper", "external_id": "serper:111111"})
    second = post_adopt({"source": "serper", "external_id": "serper:111111"})

    assert first.status_code == 201
    assert second.status_code == 200
    assert (
        second.json()["adopted_product_id"] == first.json()["adopted_product_id"]
    )
    assert adoption_service.count() == 1


def test_repeated_adoption_never_accumulates_rows(stored, no_ai):
    ids = {
        post_adopt(
            {"source": "serper", "external_id": "serper:111111"}
        ).json()["adopted_product_id"]
        for _ in range(5)
    }

    assert len(ids) == 1
    assert adoption_service.count() == 1


def test_re_adoption_does_not_overwrite_the_original_snapshot(stored, no_ai):
    """A newer observation must not silently rewrite an adopted record."""
    first = post_adopt(
        {"source": "serper", "external_id": "serper:111111"}
    ).json()

    # The retailer changed the price and a later comparison refreshed it.
    record_products(
        [evidence_product(title="Renamed", observed_price=Decimal("99.99"))]
    )
    second = post_adopt(
        {"source": "serper", "external_id": "serper:111111"}
    ).json()

    assert second == first
    assert second["title"] == "Sony WH-CH520 Wireless Headphones"
    assert second["observed_price"] == "19.99"


def test_the_unique_constraint_not_the_precheck_enforces_uniqueness(stored, no_ai):
    """Uniqueness is enforced in SQL, so a race cannot create a duplicate."""
    adopt("serper", "serper:111111")

    with _raw_connect() as conn:
        # A table-level UNIQUE(...) is backed by an auto-index, so read the
        # pragmas rather than sqlite_master.sql (which is NULL for those).
        unique_column_sets = []
        for index in conn.execute("PRAGMA index_list(adopted_products)"):
            if not index["unique"]:
                continue
            unique_column_sets.append(
                {
                    row["name"]
                    for row in conn.execute(
                        f"PRAGMA index_info({index['name']})"
                    )
                }
            )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO adopted_products (
                       adopted_product_id, discovery_source,
                       discovery_external_id, title, observed_at,
                       adopted_at, updated_at, status
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "adopt_duplicate",
                    "serper",
                    "serper:111111",
                    "Smuggled duplicate",
                    NOW.isoformat(),
                    NOW.isoformat(),
                    NOW.isoformat(),
                    "ACTIVE",
                ),
            )

    assert {"discovery_source", "discovery_external_id"} in unique_column_sets, (
        "a UNIQUE index must cover the provider identity"
    )
    # Identity is the provider pair and nothing else: title, merchant and
    # price must NOT be part of any uniqueness constraint.
    for columns in unique_column_sets:
        assert "title" not in columns
        assert "merchant_name" not in columns
        assert "observed_price" not in columns


def test_concurrent_adoption_reuses_the_winner(stored, no_ai, monkeypatch):
    """If the row appears between the pre-check and the insert, reuse it."""
    real_get = adoption_service.get_by_identity
    calls = {"n": 0}

    def flaky_get(source, external_id):
        calls["n"] += 1
        if calls["n"] == 1:
            # First call: pretend nothing is adopted yet...
            raise AdoptedProductNotFoundError("not yet")
        return real_get(source, external_id)

    # ...while a competing request actually inserted it.
    winner, _ = adopt("serper", "serper:111111")
    monkeypatch.setattr(adoption_service, "get_by_identity", flaky_get)

    record, created = adopt("serper", "serper:111111")

    assert created is False
    assert record.adopted_product_id == winner.adopted_product_id
    assert adoption_service.count() == 1


# ---------------------------------------------------------------------------
# 9, 27. Cross-provider identity isolation
# ---------------------------------------------------------------------------


def test_same_external_id_under_two_providers_adopts_separately(
    frozen_core, no_ai
):
    record_products(
        [
            evidence_product(
                source="serper", external_id="prod_1", title="Serper version"
            ),
            evidence_product(
                source="channel3", external_id="prod_1", title="Channel3 version"
            ),
        ]
    )

    first = post_adopt({"source": "serper", "external_id": "prod_1"})
    second = post_adopt({"source": "channel3", "external_id": "prod_1"})

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["title"] == "Serper version"
    assert second.json()["title"] == "Channel3 version"
    assert (
        first.json()["adopted_product_id"] != second.json()["adopted_product_id"]
    )
    assert adoption_service.count() == 2


def test_adopting_under_the_wrong_provider_is_404(frozen_core, no_ai):
    record_products([evidence_product(source="serper", external_id="prod_1")])

    response = post_adopt({"source": "channel3", "external_id": "prod_1"})

    assert response.status_code == 404
    assert adoption_service.count() == 0


# ---------------------------------------------------------------------------
# 10-17, security. The client cannot supply authoritative product data.
# ---------------------------------------------------------------------------


MALICIOUS_PAYLOAD = {
    "source": "serper",
    "external_id": "serper:111111",
    "title": "Cheap Laptop",
    "observed_price": "1.00",
    "observed_currency": "USD",
    "merchant_name": "Fake Merchant",
    "product_url": "https://evil.example",
    "paypal_amount": "0.01",
    "paypal_order_id": "fake",
}


def test_malicious_payload_is_rejected_outright(stored, no_ai):
    """THE security test: forged fields are refused, not quietly dropped."""
    response = post_adopt(MALICIOUS_PAYLOAD)

    assert response.status_code == 422
    # Nothing was created, so no forged value could have been persisted.
    assert adoption_service.count() == 0

    # The rejection names the offending keys as unexpected.
    detail = str(response.json()["detail"])
    for forged in (
        "title",
        "observed_price",
        "observed_currency",
        "merchant_name",
        "product_url",
        "paypal_amount",
        "paypal_order_id",
    ):
        assert forged in detail


def test_no_forged_value_appears_anywhere_in_the_database(stored, no_ai):
    post_adopt(MALICIOUS_PAYLOAD)
    # ...and the legitimate adoption that follows uses only server evidence.
    post_adopt({"source": "serper", "external_id": "serper:111111"})

    with _raw_connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM adopted_products")]

    dumped = str(rows)
    for forged in (
        "Cheap Laptop",
        "Fake Merchant",
        "evil.example",
        "1.00",
        "0.01",
        "fake",
    ):
        assert forged not in dumped, f"forged value {forged!r} reached the database"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "Cheap Laptop"),
        ("observed_price", "1.00"),
        ("observed_currency", "EUR"),
        ("normalized_price", "1.00"),
        ("normalized_currency", "EUR"),
        ("merchant_name", "Fake Merchant"),
        ("merchant_domain", "evil.example"),
        ("product_url", "https://evil.example"),
        ("description", "Forged description"),
        ("brand", "Forged brand"),
        ("category", "forged"),
        ("status", "ACTIVE"),
        ("adopted_product_id", "adopt_attacker_chosen"),
        ("observed_at", "1999-01-01T00:00:00Z"),
        ("adopted_at", "1999-01-01T00:00:00Z"),
        ("paypal_order_id", "ORDER-FAKE"),
        ("paypal_capture_id", "CAPTURE-FAKE"),
        ("paypal_amount", "0.01"),
        ("paypal_currency", "USD"),
        ("refund_id", "REFUND-FAKE"),
        ("transaction_id", "txn_fake"),
        ("purchase_price", "0.01"),
        ("amount", "0.01"),
        ("policy_decision", "ALLOW_AUTONOMOUS_PURCHASE"),
    ],
)
def test_every_unauthorized_field_is_refused_individually(
    stored, no_ai, field, value
):
    response = post_adopt(
        {"source": "serper", "external_id": "serper:111111", field: value}
    )

    assert response.status_code == 422, f"{field} must not be accepted"
    assert adoption_service.count() == 0


def test_the_request_schema_accepts_only_two_fields():
    """Structural: there is nowhere for a product fact to be supplied."""
    assert set(AdoptionRequest.model_fields) == {"source", "external_id"}
    assert AdoptionRequest.model_config.get("extra") == "forbid"


def test_the_adopt_function_takes_no_product_data():
    """Structural: even an internal caller cannot inject a product fact."""
    import inspect

    params = set(inspect.signature(adopt).parameters)

    assert params == {"source", "external_id"}


def test_server_evidence_is_the_only_source_of_product_facts(stored, no_ai):
    """Point-by-point: the record mirrors stored evidence, not the request."""
    body = post_adopt(
        {"source": "serper", "external_id": "serper:111111"}
    ).json()
    evidence = discovery_evidence_store.get_product("serper", "serper:111111")

    assert body["title"] == evidence.title
    assert body["description"] == evidence.description
    assert body["brand"] == evidence.brand
    assert body["category"] == evidence.category
    assert body["merchant_name"] == evidence.merchant_name
    assert body["merchant_domain"] == evidence.merchant_domain
    assert body["product_url"] == evidence.product_url
    assert body["observed_price"] == str(evidence.observed_price)
    assert body["observed_currency"] == evidence.observed_currency
    assert body["normalized_price"] == str(evidence.normalized_price)
    assert body["normalized_currency"] == evidence.normalized_currency


# ---------------------------------------------------------------------------
# 18-19, 24. No PayPal, no policy, no AI
# ---------------------------------------------------------------------------


def test_adoption_makes_no_paypal_policy_or_ai_call(stored, no_ai):
    """The boobytraps in frozen_core and no_ai would raise if touched."""
    response = post_adopt({"source": "serper", "external_id": "serper:111111"})

    assert response.status_code == 201


def test_adoption_service_imports_no_payment_or_ai_module():
    """Structural guard: the import graph itself forbids a payment bridge."""
    import pathlib
    import re

    source = pathlib.Path(adoption_service.__file__).read_text(encoding="utf-8")
    imports = "\n".join(
        line
        for line in source.splitlines()
        if re.match(r"\s*(import|from)\s", line)
    )

    for forbidden in (
        "paypal",
        "policy",
        "purchase_execution",
        "purchase_orchestrator",
        "transaction_service",
        "refund_service",
        "session_service",
        "confirmation_service",
        "webhook",
        "providers",
        "agents",
        "product_service",
    ):
        assert forbidden not in imports, f"adoption must not import {forbidden}"


def test_the_response_exposes_no_paypal_or_payment_field(stored, no_ai):
    body = post_adopt({"source": "serper", "external_id": "serper:111111"}).json()

    for forbidden in (
        "paypal_order_id",
        "paypal_capture_id",
        "paypal_amount",
        "paypal_currency",
        "refund_id",
        "transaction_id",
        "purchase_price",
        "amount",
        "price",
        "policy_decision",
    ):
        assert forbidden not in body

    text = str(body).lower()
    assert "paypal" not in text
    assert "capture" not in text


def test_the_adopted_table_holds_no_payment_columns(stored, no_ai):
    post_adopt({"source": "serper", "external_id": "serper:111111"})

    with _raw_connect() as conn:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(adopted_products)")
        }

    for forbidden in (
        "paypal_order_id",
        "paypal_capture_id",
        "paypal_amount",
        "paypal_currency",
        "refund_id",
        "transaction_id",
        "purchase_price",
        "amount",
        "price",
        "policy_decision",
    ):
        assert forbidden not in columns


def test_adoption_does_not_create_the_payment_tables(stored, no_ai):
    post_adopt({"source": "serper", "external_id": "serper:111111"})

    with _raw_connect() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert "adopted_products" in tables
    assert "transactions" not in tables
    assert "sessions" not in tables


# ---------------------------------------------------------------------------
# 20-21. The existing catalog and purchase flow are unaffected
# ---------------------------------------------------------------------------


def test_local_catalog_is_unchanged_by_adoption(stored, no_ai):
    from app.services.product_service import list_products

    before = [p.model_dump() for p in list_products()]

    post_adopt({"source": "serper", "external_id": "serper:111111"})

    assert [p.model_dump() for p in list_products()] == before


def test_products_json_is_not_modified_by_adoption(stored, no_ai):
    import pathlib

    from app.services import product_service

    path = pathlib.Path(product_service._DATA_FILE)
    before = path.read_bytes()
    before_mtime = path.stat().st_mtime

    post_adopt({"source": "serper", "external_id": "serper:111111"})

    assert path.read_bytes() == before
    assert path.stat().st_mtime == before_mtime


def test_an_adopted_product_does_not_appear_in_the_catalog(stored, no_ai):
    body = post_adopt(
        {"source": "serper", "external_id": "serper:111111"}
    ).json()

    listed = client.get("/products")
    assert listed.status_code == 200
    ids = {p["id"] for p in listed.json()}

    assert body["adopted_product_id"] not in ids
    assert body["discovery_external_id"] not in ids
    assert client.get(f"/products/{body['adopted_product_id']}").status_code == 404


def test_an_adopted_product_is_not_purchasable(stored):
    """The decisive check: adoption built NO bridge to purchase execution."""
    body = post_adopt(
        {"source": "serper", "external_id": "serper:111111"}
    ).json()

    authorization = {
        "purchase_requested": True,
        "autonomous_purchase_allowed": True,
        "auto_approve_below_inr": 1000000,
    }

    for identifier in (
        body["adopted_product_id"],
        body["discovery_external_id"],
        body["discovery_source"],
    ):
        response = client.post(
            "/purchase/execute",
            json={"product_id": identifier, "authorization": authorization},
        )
        # Not a catalog product, so execution refuses it. No PayPal order
        # exists, and frozen_core would have fired if PayPal were reached.
        assert response.status_code == 404, identifier


def test_the_existing_purchase_flow_still_rejects_unknown_products(stored, no_ai):
    """Regression: /purchase/execute behaviour is untouched by this phase."""
    response = client.post(
        "/purchase/execute",
        json={
            "product_id": "NOT_A_PRODUCT",
            "authorization": {"purchase_requested": True},
        },
    )

    assert response.status_code == 404


def test_the_transactions_endpoint_still_reports_an_empty_ledger(stored, no_ai):
    post_adopt({"source": "serper", "external_id": "serper:111111"})

    response = client.get("/transactions")

    assert response.status_code == 200
    assert response.json() == {"transactions": [], "count": 0}


# ---------------------------------------------------------------------------
# 22. Observed price is not purchase authority
# ---------------------------------------------------------------------------


def test_observed_price_is_named_observed_and_never_a_purchase_price(stored, no_ai):
    body = post_adopt({"source": "serper", "external_id": "serper:111111"}).json()

    # The evidence is preserved...
    assert body["observed_price"] == "19.99"
    assert body["observed_currency"] == "USD"
    # ...under names that cannot be mistaken for an amount to charge, and
    # there is no field that could be.
    assert "purchase_price" not in body
    assert "price" not in body
    assert "amount" not in body
    assert "paypal_amount" not in body


def test_prices_are_exact_decimal_strings_not_floats(stored, no_ai):
    record_products(
        [
            evidence_product(
                external_id="serper:exact",
                observed_price=Decimal("1299.95"),
                normalized_price=Decimal("118620.44"),
            )
        ]
    )

    body = post_adopt(
        {"source": "serper", "external_id": "serper:exact"}
    ).json()

    assert body["observed_price"] == "1299.95"
    assert body["normalized_price"] == "118620.44"
    assert isinstance(body["observed_price"], str)


def test_a_product_with_no_observed_price_adopts_with_nulls(frozen_core, no_ai):
    record_products(
        [
            evidence_product(
                external_id="serper:unpriced",
                observed_price=None,
                observed_currency=None,
                normalized_price=None,
                normalized_currency=None,
            )
        ]
    )

    response = post_adopt(
        {"source": "serper", "external_id": "serper:unpriced"}
    )

    assert response.status_code == 201
    body = response.json()
    # Null, never 0 and never a substituted figure.
    assert body["observed_price"] is None
    assert body["observed_currency"] is None
    assert body["normalized_price"] is None
    assert body["status"] == "ACTIVE"


def test_the_derived_normalized_price_is_preserved_as_derived(stored, no_ai):
    body = post_adopt({"source": "serper", "external_id": "serper:111111"}).json()

    assert body["normalized_price"] == "1824.09"
    assert body["normalized_currency"] == "INR"
    # The conversion mechanics stay in the discovery layer; an adopted
    # record is a snapshot, not a re-derivable calculation.
    assert "conversion_rate" not in body
    assert "conversion_source" not in body


# ---------------------------------------------------------------------------
# 23. Provider evidence is preserved
# ---------------------------------------------------------------------------


def test_provider_identity_and_merchant_evidence_are_preserved(stored, no_ai):
    body = post_adopt({"source": "serper", "external_id": "serper:111111"}).json()

    assert body["discovery_source"] == "serper"
    assert body["discovery_external_id"] == "serper:111111"
    # Phase 10H identity survives adoption: the named seller, and an honest
    # null domain because the link host is Google.
    assert body["merchant_name"] == "Amazon"
    assert body["merchant_domain"] is None


def test_the_product_url_is_preserved_verbatim(stored, no_ai):
    body = post_adopt({"source": "serper", "external_id": "serper:111111"}).json()

    # Not rewritten toward the merchant, not cleaned up.
    assert body["product_url"] == GOOGLE_LINK


def test_optional_evidence_fields_are_preserved_including_nulls(frozen_core, no_ai):
    record_products(
        [
            evidence_product(
                external_id="serper:sparse",
                description=None,
                brand=None,
                category=None,
                merchant_name=None,
            )
        ]
    )

    body = post_adopt(
        {"source": "serper", "external_id": "serper:sparse"}
    ).json()

    assert body["description"] is None
    assert body["brand"] is None
    assert body["category"] is None
    assert body["merchant_name"] is None


# ---------------------------------------------------------------------------
# 25. Malformed stored evidence fails safely
# ---------------------------------------------------------------------------


def test_malformed_stored_evidence_fails_without_adopting(stored, no_ai):
    with sqlite3.connect(discovery_evidence_store._db_path()) as conn:
        conn.execute(
            "UPDATE discovery_evidence SET evidence_json = ? WHERE source = ?",
            ("{truncated", "serper"),
        )
        conn.commit()

    response = post_adopt({"source": "serper", "external_id": "serper:111111"})

    assert response.status_code == 500
    assert "malformed" in response.json()["detail"].lower()
    # Critically: no partial record was written.
    assert adoption_service.count() == 0


def test_malformed_stored_evidence_raises_a_typed_error(stored):
    with sqlite3.connect(discovery_evidence_store._db_path()) as conn:
        conn.execute(
            "UPDATE discovery_evidence SET evidence_json = ? WHERE source = ?",
            ('{"title": "partial"}', "serper"),
        )
        conn.commit()

    with pytest.raises(MalformedDiscoveryEvidenceError):
        adopt("serper", "serper:111111")

    assert adoption_service.count() == 0


# ---------------------------------------------------------------------------
# 28. Timestamps
# ---------------------------------------------------------------------------


def test_observed_at_comes_from_the_evidence_not_the_adoption(stored, no_ai):
    body = post_adopt({"source": "serper", "external_id": "serper:111111"}).json()

    assert body["observed_at"] == NOW.isoformat()
    assert body["adopted_at"] != body["observed_at"]


def test_adopted_at_is_server_generated_and_utc(stored, no_ai):
    before = datetime.now(timezone.utc)
    body = post_adopt({"source": "serper", "external_id": "serper:111111"}).json()
    after = datetime.now(timezone.utc)

    adopted_at = datetime.fromisoformat(body["adopted_at"])
    assert adopted_at.tzinfo is not None
    assert before <= adopted_at <= after


def test_updated_at_starts_equal_to_adopted_at(stored, no_ai):
    body = post_adopt({"source": "serper", "external_id": "serper:111111"}).json()

    assert body["updated_at"] == body["adopted_at"]


def test_a_client_supplied_timestamp_cannot_influence_the_record(stored, no_ai):
    rejected = post_adopt(
        {
            "source": "serper",
            "external_id": "serper:111111",
            "adopted_at": "1999-01-01T00:00:00+00:00",
        }
    )
    assert rejected.status_code == 422

    body = post_adopt({"source": "serper", "external_id": "serper:111111"}).json()
    assert not body["adopted_at"].startswith("1999")


# ---------------------------------------------------------------------------
# 29. Status
# ---------------------------------------------------------------------------


def test_valid_evidence_produces_an_active_record(stored, no_ai):
    body = post_adopt({"source": "serper", "external_id": "serper:111111"}).json()

    assert body["status"] == AdoptedProductStatus.ACTIVE.value


def test_the_lifecycle_contains_no_purchase_states():
    values = {status.value for status in AdoptedProductStatus}

    assert values == {"ACTIVE", "STALE", "DISABLED"}
    for forbidden in (
        "ORDER_CREATED",
        "ORDER_APPROVED",
        "CAPTURED",
        "REFUNDED",
        "PURCHASED",
        "PAID",
        "APPROVED",
    ):
        assert forbidden not in values


def test_active_is_the_only_status_phase_11a_produces(frozen_core, no_ai):
    record_products(
        [evidence_product(external_id=f"serper:{i}") for i in range(1, 4)]
    )

    statuses = {
        post_adopt(
            {"source": "serper", "external_id": f"serper:{i}"}
        ).json()["status"]
        for i in range(1, 4)
    }

    assert statuses == {"ACTIVE"}


def test_status_is_persisted_and_read_back(stored, no_ai):
    post_adopt({"source": "serper", "external_id": "serper:111111"})

    record = get_by_identity("serper", "serper:111111")

    assert record.status is AdoptedProductStatus.ACTIVE


# ---------------------------------------------------------------------------
# Record retrieval helpers
# ---------------------------------------------------------------------------


def test_get_by_id_round_trips_the_record(stored, no_ai):
    body = post_adopt({"source": "serper", "external_id": "serper:111111"}).json()

    record = adoption_service.get(body["adopted_product_id"])

    assert record.adopted_product_id == body["adopted_product_id"]
    assert record.observed_price == Decimal("19.99")


def test_get_by_unknown_id_raises_not_found(frozen_core):
    with pytest.raises(AdoptedProductNotFoundError):
        adoption_service.get("adopt_does_not_exist")


@pytest.mark.parametrize("blank", ["", "   "])
def test_get_rejects_a_blank_id(frozen_core, blank):
    with pytest.raises(ValueError):
        adoption_service.get(blank)


def test_there_is_no_adoption_listing_or_mutation_endpoint():
    """The adopted-product HTTP surface is exactly these routes.

    Phase 11A added POST /discovery/adopt, 11B the revalidate route, 11C
    policy-evaluate, and 11D the approval route. All are POST. There is
    deliberately NO list, GET, PUT, PATCH or DELETE surface on adopted
    products: nothing lets a client edit or remove an adopted record, so the
    snapshot cannot be tampered with over HTTP.
    """
    adoption_routes = {
        (route.path, tuple(sorted(route.methods)))
        for route in app.routes
        if "adopt" in getattr(route, "path", "")
    }

    assert adoption_routes == {
        ("/discovery/adopt", ("POST",)),
        ("/discovery/adopted/{adopted_product_id}/revalidate", ("POST",)),
        ("/discovery/adopted/{adopted_product_id}/policy-evaluate", ("POST",)),
        ("/discovery/adopted/{adopted_product_id}/approval", ("POST",)),
    }
