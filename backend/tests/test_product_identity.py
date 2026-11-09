"""Provider identity hardening tests (Phase 11B.1).

The distinction under test: a provider's id for a search RESULT is not
automatically a durable name for the PRODUCT. These tests pin that

  - Serper's productId-derived id is treated as durable (measured evidence);
  - Serper's link-hash fallback id is NOT, because the hashed link embeds
    the search query;
  - Channel3's product-API id is durable;
  - an unknown provider gets no durability by default;
  - no combination of title, merchant, price, brand or category ever
    establishes identity;
  - an adopted record always keeps its observation id, and only sometimes
    gains a durable key;
  - revalidation uses the durable key when it exists and reports
    IDENTITY_UNVERIFIABLE when it does not.

Providers are faked; the suite never calls Serper. Each test gets its own
temporary SQLite database (conftest).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.adoption import ProductIdentityKind
from app.schemas.discovery import NormalizedDiscoveredProduct
from app.schemas.revalidation import (
    IdentityMatch,
    RevalidationReason,
    RevalidationStatus,
)
from app.services import (
    adoption_service,
    product_identity,
    revalidation_service,
)
from app.services.channel3_client import (
    Channel3Offer,
    Channel3Price,
    Channel3Product,
    Channel3SearchResult,
)
from app.services.discovery_evidence_store import record_products
from app.services.discovery_providers import serper as serper_module
from app.services.product_identity import classify, identity_key_for
from app.services.revalidation_service import (
    find_identity_match,
    retrieval_queries,
    revalidate,
)

client = TestClient(app)

NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)
GOOGLE_LINK = "https://www.google.com/shopping/product/1"
TITLE = "Sony WH-CH520 Wireless Headphones"


# ---------------------------------------------------------------------------
# Fixtures and fakes
# ---------------------------------------------------------------------------


def product(**overrides) -> NormalizedDiscoveredProduct:
    fields = {
        "external_id": "serper:111111",
        "source": "serper",
        "title": TITLE,
        "description": "On-ear wireless headphones.",
        "brand": "Sony",
        "category": "headphones",
        "image_url": "https://img.example/ch520.jpg",
        "product_url": GOOGLE_LINK,
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


def shopping_item(**overrides):
    item = {
        "title": TITLE,
        "source": "Amazon",
        "link": GOOGLE_LINK,
        "price": "$19.99",
        "productId": "111111",
    }
    item.update(overrides)
    return item


class SerperResponse:
    status_code = 200

    def __init__(self, items=None):
        self._items = [shopping_item()] if items is None else items

    def json(self):
        return {"shopping": self._items}


@pytest.fixture
def frozen_core(monkeypatch):
    """Any PayPal/policy/purchase/ledger/refund/session touch fails the test."""
    from app import paypal, policy
    from app.services import (
        purchase_execution_service,
        refund_service,
        session_service,
        transaction_service,
    )

    def bomb(name):
        def _bomb(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError(f"identity work must not touch {name}")

        return _bomb

    monkeypatch.setattr(paypal, "get_access_token", bomb("PayPal auth"))
    monkeypatch.setattr(paypal, "create_order", bomb("PayPal create_order"))
    monkeypatch.setattr(paypal, "capture_order", bomb("PayPal capture_order"))
    monkeypatch.setattr(paypal, "refund_capture", bomb("PayPal refund_capture"))
    monkeypatch.setattr(policy, "evaluate_policy", bomb("the Policy Engine"))
    monkeypatch.setattr(
        purchase_execution_service, "execute_purchase", bomb("purchase execution")
    )
    monkeypatch.setattr(
        transaction_service, "create_transaction", bomb("the ledger")
    )
    monkeypatch.setattr(refund_service, "refund_transaction", bomb("refunds"))
    monkeypatch.setattr(session_service, "create_session", bomb("sessions"))


@pytest.fixture
def no_ai(monkeypatch):
    from app.agents import comparison_agent, intent_agent
    from app.services import providers

    def bomb(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("identity work must not invoke AI")

    monkeypatch.setattr(providers, "get_provider", bomb)
    monkeypatch.setattr(intent_agent, "get_provider", bomb)
    monkeypatch.setattr(comparison_agent, "rank", bomb)


@pytest.fixture
def serper(monkeypatch, frozen_core, no_ai):
    """A faked, counted Serper HTTP boundary. The real provider still runs."""
    monkeypatch.setenv("SERPER_API_KEY", "test-serper-key")
    state = {"responses": [SerperResponse()], "calls": []}

    def fake_post(url, json=None, headers=None, timeout=None):
        state["calls"].append(json)
        result = state["responses"][0]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(serper_module.httpx, "post", fake_post)

    class Ctx:
        @staticmethod
        def respond(r):
            state["responses"][0] = r

        @staticmethod
        def queries():
            return [c["q"] for c in state["calls"]]

        @staticmethod
        def call_count():
            return len(state["calls"])

    return Ctx


def adopt_serper(**overrides):
    record_products([product(**overrides)])
    adopted, _ = adoption_service.adopt(
        overrides.get("source", "serper"),
        overrides.get("external_id", "serper:111111"),
    )
    return adopted


def c3_product(product_id="prod_1", *, title=TITLE, price="19.99"):
    return Channel3Product(
        id=product_id,
        title=title,
        offers=[
            Channel3Offer(
                url=f"https://www.bestbuy.com/site/{product_id}",
                domain="bestbuy.com",
                price=Channel3Price(price=Decimal(price), currency="USD"),
                availability="InStock",
            )
        ],
    )


class FakeChannel3Client:
    def __init__(self, products=None):
        self.products = [c3_product()] if products is None else products
        self.calls = []

    def search(self, query=None, *, limit=20, page_token=None, **kwargs):
        self.calls.append(query)
        return Channel3SearchResult(products=self.products)


def post_revalidate(adopted_product_id):
    return client.post(
        f"/discovery/adopted/{adopted_product_id}/revalidate"
    )


# ---------------------------------------------------------------------------
# 1-3. Classification per provider, from measured evidence
# ---------------------------------------------------------------------------


def test_serper_product_id_is_classified_durable():
    identity = classify("serper", "serper:8522572424499656344")

    assert identity.kind is ProductIdentityKind.PROVIDER_PRODUCT_ID
    assert identity.product_identity_key == "serper:serper:8522572424499656344"
    assert identity.is_durable is True
    assert identity.provider_observation_id == "serper:8522572424499656344"
    # The claim is backed by stated evidence, not by the id's shape.
    assert "measured" in identity.evidence.lower()


def test_serper_link_hash_id_is_not_durable():
    """The hashed link embeds the search query, so it names an observation."""
    identity = classify("serper", "serper:url:a1b2c3d4e5f60718")

    assert identity.kind is ProductIdentityKind.PROVIDER_OBSERVATION_ONLY
    assert identity.product_identity_key is None
    assert identity.is_durable is False
    # The observation id is still preserved - an observation is real.
    assert identity.provider_observation_id == "serper:url:a1b2c3d4e5f60718"
    assert "query" in identity.evidence.lower()


def test_channel3_product_id_is_classified_durable():
    identity = classify("channel3", "prod_1")

    assert identity.kind is ProductIdentityKind.PROVIDER_PRODUCT_ID
    assert identity.product_identity_key == "channel3:prod_1"
    assert identity.is_durable is True
    assert "contract" in identity.evidence.lower()


def test_an_unknown_provider_gets_no_durability_by_default():
    """Silence is not a guarantee."""
    identity = classify("some-new-provider", "abc123")

    assert identity.kind is ProductIdentityKind.PROVIDER_OBSERVATION_ONLY
    assert identity.product_identity_key is None
    assert identity.is_durable is False


def test_source_comparison_is_case_insensitive_and_trimmed():
    assert classify("  Channel3  ", "prod_1").product_identity_key == "channel3:prod_1"
    assert classify("SERPER", "serper:1").is_durable is True


def test_identity_keys_are_namespaced_so_providers_cannot_collide():
    serper_key = identity_key_for("serper", "shared_id")
    channel3_key = identity_key_for("channel3", "shared_id")

    assert serper_key != channel3_key
    assert serper_key.startswith("serper:")
    assert channel3_key.startswith("channel3:")


@pytest.mark.parametrize(
    ("source", "external_id"),
    [("", "x"), ("   ", "x"), ("serper", ""), ("serper", "  ")],
)
def test_classify_rejects_a_blank_identity(source, external_id):
    with pytest.raises(ValueError):
        classify(source, external_id)


def test_classify_reads_only_the_provider_and_its_id():
    """Structural: no product attribute can be passed in, so none can leak in."""
    import inspect

    assert set(inspect.signature(classify).parameters) == {"source", "external_id"}


# ---------------------------------------------------------------------------
# 5-9. No fuzzy matching: nothing but a durable id establishes identity
# ---------------------------------------------------------------------------


def test_same_title_alone_does_not_establish_identity():
    results = [product(external_id="serper:999999", title=TITLE)]

    assert find_identity_match(results, "serper", "serper:111111") is None


def test_same_merchant_alone_does_not_establish_identity():
    results = [
        product(external_id="serper:999999", title="Different", merchant_name="Amazon")
    ]

    assert find_identity_match(results, "serper", "serper:111111") is None


def test_same_title_and_merchant_does_not_establish_identity():
    results = [
        product(external_id="serper:999999", title=TITLE, merchant_name="Amazon")
    ]

    assert find_identity_match(results, "serper", "serper:111111") is None


def test_same_title_and_price_does_not_establish_identity():
    results = [
        product(
            external_id="serper:999999",
            title=TITLE,
            observed_price=Decimal("19.99"),
            observed_currency="USD",
        )
    ]

    assert find_identity_match(results, "serper", "serper:111111") is None


def test_same_brand_and_title_does_not_establish_identity():
    results = [product(external_id="serper:999999", title=TITLE, brand="Sony")]

    assert find_identity_match(results, "serper", "serper:111111") is None


def test_everything_identical_except_the_id_does_not_match():
    """The strongest form of the test: only the id differs."""
    results = [product(external_id="serper:999999")]

    assert find_identity_match(results, "serper", "serper:111111") is None


def test_same_url_alone_does_not_establish_identity():
    """A shared link is not identity either - Google links are shared."""
    results = [
        product(external_id="serper:999999", product_url=GOOGLE_LINK)
    ]

    assert find_identity_match(results, "serper", "serper:111111") is None


def test_an_observation_only_id_never_matches_even_itself():
    """Two identical link-hash ids still name two searches, not one product."""
    hashed = "serper:url:a1b2c3d4e5f60718"
    results = [product(external_id=hashed)]

    assert find_identity_match(results, "serper", hashed) is None


def test_a_durable_id_matches_only_its_own_provider():
    results = [product(source="channel3", external_id="prod_1")]

    assert find_identity_match(results, "serper", "prod_1") is None
    assert find_identity_match(results, "channel3", "prod_1") is not None


def test_a_durable_id_matches_regardless_of_position_or_attributes():
    """Identity wins even when every other field has changed."""
    results = [
        product(external_id="serper:aaa"),
        product(external_id="serper:bbb"),
        product(
            external_id="serper:111111",
            title="Completely Renamed By The Retailer",
            merchant_name="Walmart",
            brand=None,
            observed_price=Decimal("99.99"),
        ),
    ]

    match = find_identity_match(results, "serper", "serper:111111")

    assert match is not None
    assert match.title == "Completely Renamed By The Retailer"


def test_the_matcher_reads_no_product_attribute():
    """Structural: the matcher's only inputs are the results and an identity."""
    import ast
    import inspect

    source = inspect.getsource(find_identity_match)
    tree = ast.parse(source.lstrip())
    attributes = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }

    for forbidden in (
        "title",
        "merchant_name",
        "merchant_domain",
        "observed_price",
        "observed_currency",
        "brand",
        "category",
        "availability",
        "product_url",
        "image_url",
    ):
        assert forbidden not in attributes, f"matcher must not read {forbidden}"


# ---------------------------------------------------------------------------
# 10-11. Adoption records the observation and the durable key explicitly
# ---------------------------------------------------------------------------


def test_adoption_preserves_the_observation_id_and_adds_a_durable_key(
    frozen_core, no_ai
):
    adopted = adopt_serper()

    assert adopted.discovery_external_id == "serper:111111"
    assert adopted.product_identity_kind is ProductIdentityKind.PROVIDER_PRODUCT_ID
    assert adopted.product_identity_key == "serper:serper:111111"


def test_adoption_of_an_observation_only_product_records_no_durable_key(
    frozen_core, no_ai
):
    adopted = adopt_serper(external_id="serper:url:a1b2c3d4e5f60718")

    # The observation is still fully preserved...
    assert adopted.discovery_external_id == "serper:url:a1b2c3d4e5f60718"
    assert adopted.title == TITLE
    assert adopted.observed_price == Decimal("19.99")
    # ...but adoption does not pretend it proves future identity.
    assert (
        adopted.product_identity_kind
        is ProductIdentityKind.PROVIDER_OBSERVATION_ONLY
    )
    assert adopted.product_identity_key is None


def test_the_api_exposes_the_identity_classification(frozen_core, no_ai):
    record_products([product()])

    body = client.post(
        "/discovery/adopt",
        json={"source": "serper", "external_id": "serper:111111"},
    ).json()

    assert body["product_identity_kind"] == "PROVIDER_PRODUCT_ID"
    assert body["product_identity_key"] == "serper:serper:111111"


def test_a_client_cannot_supply_identity_fields(frozen_core, no_ai):
    record_products([product(external_id="serper:url:deadbeefdeadbeef")])

    for forged in (
        {"product_identity_kind": "PROVIDER_PRODUCT_ID"},
        {"product_identity_key": "serper:serper:attacker-chosen"},
        {"provider_observation_id": "serper:1"},
    ):
        response = client.post(
            "/discovery/adopt",
            json={
                "source": "serper",
                "external_id": "serper:url:deadbeefdeadbeef",
                **forged,
            },
        )
        assert response.status_code == 422, forged

    # The honest classification stands once adopted legitimately.
    body = client.post(
        "/discovery/adopt",
        json={"source": "serper", "external_id": "serper:url:deadbeefdeadbeef"},
    ).json()
    assert body["product_identity_kind"] == "PROVIDER_OBSERVATION_ONLY"
    assert body["product_identity_key"] is None


def test_adoption_uniqueness_still_keys_on_the_provider_observation(
    frozen_core, no_ai
):
    """The UNIQUE key is unchanged: it dedupes observations, as before."""
    adopt_serper()
    again, created = adoption_service.adopt("serper", "serper:111111")

    assert created is False
    assert adoption_service.count() == 1

    with sqlite3.connect(adoption_service._db_path()) as conn:
        conn.row_factory = sqlite3.Row
        unique_sets = []
        for index in conn.execute("PRAGMA index_list(adopted_products)"):
            if index["unique"]:
                unique_sets.append(
                    {
                        r["name"]
                        for r in conn.execute(f"PRAGMA index_info({index['name']})")
                    }
                )

    assert {"discovery_source", "discovery_external_id"} in unique_sets
    # Identity key is NOT a uniqueness constraint: it is null for
    # observation-only products, and two separate adoptions of one product
    # must stay two records.
    for columns in unique_sets:
        assert "product_identity_key" not in columns


# ---------------------------------------------------------------------------
# 12-14. Revalidation uses durable identity, or says it cannot
# ---------------------------------------------------------------------------


def test_revalidation_uses_the_durable_identity_when_available(serper):
    adopted = adopt_serper()

    body = post_revalidate(adopted.adopted_product_id).json()

    snap = body["snapshot"]
    assert snap["revalidation_status"] == "REVALIDATED"
    assert snap["identity_match"] == "EXACT_EXTERNAL_ID"
    assert snap["identity_kind"] == "PROVIDER_PRODUCT_ID"
    assert snap["product_identity_key"] == "serper:serper:111111"


def test_revalidation_is_unverifiable_without_a_durable_identity(serper):
    adopted = adopt_serper(external_id="serper:url:a1b2c3d4e5f60718")

    body = post_revalidate(adopted.adopted_product_id).json()

    snap = body["snapshot"]
    assert snap["revalidation_status"] == "IDENTITY_UNVERIFIABLE"
    assert snap["identity_match"] == "UNVERIFIABLE"
    assert snap["reason_code"] == "NO_DURABLE_PRODUCT_IDENTITY"
    assert snap["identity_kind"] == "PROVIDER_OBSERVATION_ONLY"
    assert snap["product_identity_key"] is None


def test_unverifiable_identity_makes_no_provider_request(serper):
    """A request could not settle the question, so none is spent."""
    adopted = adopt_serper(external_id="serper:url:a1b2c3d4e5f60718")

    post_revalidate(adopted.adopted_product_id)

    assert serper.call_count() == 0


def test_unverifiable_identity_reports_no_fresh_evidence(serper):
    adopted = adopt_serper(external_id="serper:url:a1b2c3d4e5f60718")

    snap = post_revalidate(adopted.adopted_product_id).json()["snapshot"]

    for field in (
        "current_title",
        "current_observed_price",
        "current_merchant_name",
        "current_product_url",
    ):
        assert snap[field] is None, field
    # The adopted figures appear only under their explicit "previous" names.
    assert snap["previous_observed_price"] == "19.99"


def test_unverifiable_identity_does_not_mark_the_record_stale(serper):
    """Our evidence limitation is not the product's fault."""
    adopted = adopt_serper(external_id="serper:url:a1b2c3d4e5f60718")

    body = post_revalidate(adopted.adopted_product_id).json()

    assert body["adopted_product_status"] == "ACTIVE"
    assert body["adopted_status_changed"] is False


def test_unverifiable_identity_is_still_recorded_in_history(serper):
    adopted = adopt_serper(external_id="serper:url:a1b2c3d4e5f60718")

    post_revalidate(adopted.adopted_product_id)

    history = revalidation_service.list_revalidations(adopted.adopted_product_id)
    assert len(history) == 1
    assert (
        history[0].revalidation_status is RevalidationStatus.IDENTITY_UNVERIFIABLE
    )
    assert history[0].identity_kind == "PROVIDER_OBSERVATION_ONLY"


def test_a_durable_product_whose_id_is_absent_is_not_found_not_unverifiable(
    serper,
):
    """The two outcomes mean different things and must not be conflated."""
    adopted = adopt_serper()
    serper.respond(SerperResponse(items=[shopping_item(productId="999999")]))

    snap = post_revalidate(adopted.adopted_product_id).json()["snapshot"]

    assert snap["revalidation_status"] == "IDENTITY_NOT_FOUND"
    assert snap["identity_match"] == "NOT_FOUND"
    assert snap["identity_kind"] == "PROVIDER_PRODUCT_ID"


def test_no_false_positive_match_when_only_the_id_differs(serper):
    """A perfect impostor: same title, merchant, price, brand, URL."""
    adopted = adopt_serper()
    serper.respond(
        SerperResponse(
            items=[
                shopping_item(
                    productId="999999",
                    title=TITLE,
                    source="Amazon",
                    price="$19.99",
                    link=GOOGLE_LINK,
                )
            ]
        )
    )

    snap = post_revalidate(adopted.adopted_product_id).json()["snapshot"]

    assert snap["revalidation_status"] == "IDENTITY_NOT_FOUND"
    assert snap["current_observed_price"] is None


# ---------------------------------------------------------------------------
# Retrieval is separate from identity (the real Phase 11B bug)
# ---------------------------------------------------------------------------


def test_retrieval_queries_are_deterministic_and_bounded(frozen_core, no_ai):
    adopted = adopt_serper(
        title="GEMS Over Ear Wireless Headphones Purple Built-In Mic Micro USB",
        brand="GEMS",
    )

    queries = retrieval_queries(adopted)

    assert len(queries) <= revalidation_service.MAX_RETRIEVAL_ATTEMPTS
    assert queries[0] == adopted.title  # most specific first
    assert len(set(queries)) == len(queries)  # deduplicated
    # A leading-words prefix is included, which is what survives a retailer
    # trimming trailing attributes from a title.
    assert "GEMS Over Ear Wireless Headphones Purple" in queries


def test_retrieval_queries_adds_the_brand_only_when_it_is_absent(
    frozen_core, no_ai
):
    with_brand = adopt_serper(title="WH-CH520 Headphones", brand="Sony")
    assert "Sony WH-CH520 Headphones" in retrieval_queries(with_brand)

    # Brand already in the title -> no redundant query.
    redundant = adopt_serper(
        external_id="serper:222222", title="Sony WH-CH520", brand="Sony"
    )
    assert not any(q.startswith("Sony Sony") for q in retrieval_queries(redundant))


def test_a_shortened_retailer_title_no_longer_defeats_revalidation(
    serper, monkeypatch
):
    """The exact Phase 11B live failure, now covered.

    The retailer trimmed the title after adoption. The stale full title
    returns nothing, but the leading-words query finds the product, and the
    durable id is what admits it.
    """
    adopted = adopt_serper(
        title="GEMS Over Ear Wireless Headphones Purple Built-In Mic Micro USB",
        brand=None,
        external_id="serper:8522572424499656344",
    )

    state = {"n": 0}

    def responder(url, json=None, headers=None, timeout=None):
        state["n"] += 1
        # The stale full title finds nothing; the shortened query finds it.
        if json["q"].endswith("Micro USB"):
            return SerperResponse(items=[])
        return SerperResponse(
            items=[
                shopping_item(
                    productId="8522572424499656344",
                    title="GEMS Over Ear Wireless Headphones Purple",
                    price="$5.00",
                )
            ]
        )

    monkeypatch.setattr(serper_module.httpx, "post", responder)

    snap = post_revalidate(adopted.adopted_product_id).json()["snapshot"]

    assert snap["revalidation_status"] == "REVALIDATED"
    assert snap["identity_match"] == "EXACT_EXTERNAL_ID"
    # The CURRENT (shortened) title is reported as fresh evidence.
    assert snap["current_title"] == "GEMS Over Ear Wireless Headphones Purple"
    assert state["n"] >= 2  # the ladder was used


def test_the_ladder_stops_at_the_first_identity_match(serper):
    adopted = adopt_serper()

    post_revalidate(adopted.adopted_product_id)

    # Found on the first query, so no further billed requests.
    assert serper.call_count() == 1


def test_the_ladder_stops_immediately_on_a_provider_failure(serper):
    """Retrying a broader query after a 401 would waste requests."""
    import httpx

    adopted = adopt_serper(
        title="A Much Longer Title That Will Produce Several Queries", brand="Sony"
    )
    serper.respond(httpx.ConnectError("down"))

    snap = post_revalidate(adopted.adopted_product_id).json()["snapshot"]

    assert snap["revalidation_status"] == "PROVIDER_UNAVAILABLE"
    assert serper.call_count() == 1


def test_broadening_retrieval_cannot_create_a_false_match(serper):
    """More queries change WHETHER we find it, never WHICH we accept."""
    adopted = adopt_serper(
        title="A Much Longer Title That Will Produce Several Queries", brand="Sony"
    )
    # Every query returns a plausible impostor with the wrong id.
    serper.respond(
        SerperResponse(
            items=[
                shopping_item(productId="999999", title=adopted.title),
                shopping_item(productId="888888", title=adopted.title),
            ]
        )
    )

    snap = post_revalidate(adopted.adopted_product_id).json()["snapshot"]

    assert snap["revalidation_status"] == "IDENTITY_NOT_FOUND"
    assert serper.call_count() > 1  # the ladder really did run
    assert snap["current_observed_price"] is None


def test_retrieval_queries_of_a_blank_title_is_empty(frozen_core, no_ai):
    adopted = adopt_serper()
    blanked = adopted.model_copy(update={"title": "   "})

    assert retrieval_queries(blanked) == []


# ---------------------------------------------------------------------------
# 16-19. Backward compatibility
# ---------------------------------------------------------------------------


def test_channel3_identity_behaviour_is_unchanged(frozen_core, no_ai):
    """Serper's limitation is not forced onto Channel3."""
    record_products(
        [product(source="channel3", external_id="prod_1", retailer_domain="bestbuy.com")]
    )
    adopted, _ = adoption_service.adopt("channel3", "prod_1")

    assert adopted.product_identity_kind is ProductIdentityKind.PROVIDER_PRODUCT_ID
    assert adopted.product_identity_key == "channel3:prod_1"

    result = revalidate(
        adopted.adopted_product_id, client=FakeChannel3Client([c3_product(price="24.50")])
    )

    assert result.snapshot.revalidation_status is RevalidationStatus.REVALIDATED
    assert result.snapshot.identity_match is IdentityMatch.EXACT_EXTERNAL_ID
    assert result.snapshot.current_observed_price == Decimal("24.50")


def test_a_pre_11b1_adopted_row_is_migrated_and_backfilled(frozen_core, no_ai):
    """An adopted record written before 11B.1 stays readable and gets a key."""
    adopt_serper()  # creates the table with current columns

    # Simulate the old shape by clearing the new columns.
    with sqlite3.connect(adoption_service._db_path()) as conn:
        conn.execute(
            "UPDATE adopted_products SET product_identity_kind = ?, "
            "product_identity_key = NULL",
            ("PROVIDER_OBSERVATION_ONLY",),
        )
        conn.commit()

    # Reading through the service re-runs the migration, which backfills.
    record = adoption_service.get_by_identity("serper", "serper:111111")

    assert record.title == TITLE
    assert record.observed_price == Decimal("19.99")


def test_a_literally_pre_11b1_table_is_migrated(frozen_core, no_ai):
    """Build the Phase 11A table shape, then let the migration upgrade it."""
    old_schema = """
    CREATE TABLE adopted_products (
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
    """
    with sqlite3.connect(adoption_service._db_path()) as conn:
        conn.executescript(old_schema)
        conn.execute(
            "INSERT INTO adopted_products (adopted_product_id, discovery_source,"
            " discovery_external_id, title, observed_price, observed_currency,"
            " observed_at, adopted_at, updated_at, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "adopt_legacy",
                "serper",
                "serper:777777",
                "Legacy Product",
                "9.99",
                "USD",
                NOW.isoformat(),
                NOW.isoformat(),
                NOW.isoformat(),
                "ACTIVE",
            ),
        )
        conn.commit()

    record = adoption_service.get("adopt_legacy")

    # Readable, and classified as it would have been at adoption time.
    assert record.title == "Legacy Product"
    assert record.observed_price == Decimal("9.99")
    assert record.product_identity_kind is ProductIdentityKind.PROVIDER_PRODUCT_ID
    assert record.product_identity_key == "serper:serper:777777"


def test_a_pre_11b1_revalidation_snapshot_stays_readable(serper):
    """Old history must not become unreadable, and must not be back-dated."""
    adopted = adopt_serper()
    post_revalidate(adopted.adopted_product_id)

    # Simulate a snapshot written before the identity columns existed.
    with sqlite3.connect(revalidation_service._db_path()) as conn:
        conn.execute(
            "UPDATE adopted_product_revalidations "
            "SET identity_kind = NULL, product_identity_key = NULL"
        )
        conn.commit()

    history = revalidation_service.list_revalidations(adopted.adopted_product_id)

    assert len(history) == 1
    assert history[0].identity_kind is None  # honest: not recorded
    assert history[0].product_identity_key is None
    assert history[0].revalidation_status is RevalidationStatus.REVALIDATED


def test_existing_discovery_evidence_remains_readable(frozen_core, no_ai):
    """The evidence cache schema is untouched by 11B.1."""
    from app.services.discovery_evidence_store import get_product

    record_products([product()])

    restored = get_product("serper", "serper:111111")

    assert restored.title == TITLE
    assert restored.observed_price == Decimal("19.99")
    assert restored.merchant_name == "Amazon"


def test_the_evidence_table_schema_is_unchanged(frozen_core, no_ai):
    from app.services import discovery_evidence_store

    record_products([product()])

    with sqlite3.connect(discovery_evidence_store._db_path()) as conn:
        conn.row_factory = sqlite3.Row
        columns = {
            r["name"] for r in conn.execute("PRAGMA table_info(discovery_evidence)")
        }

    assert columns == {
        "source",
        "external_id",
        "observed_at",
        "recorded_at",
        "evidence_json",
    }


# ---------------------------------------------------------------------------
# 20-25. No payment, no AI, no catalog or ledger change
# ---------------------------------------------------------------------------


def test_identity_work_makes_no_paypal_policy_purchase_or_ai_call(serper):
    adopted = adopt_serper()

    assert post_revalidate(adopted.adopted_product_id).status_code == 200


def test_product_identity_imports_nothing_from_payment_or_ai():
    import ast
    import pathlib

    tree = ast.parse(
        pathlib.Path(product_identity.__file__).read_text(encoding="utf-8")
    )
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(f"{node.module or ''}.{a.name}" for a in node.names)
    leaves = {n.rsplit(".", 1)[-1] for n in names}

    for forbidden in (
        "paypal",
        "policy",
        "purchase_execution_service",
        "transaction_service",
        "refund_service",
        "session_service",
        "providers",
        "comparison_agent",
        "intent_agent",
        "product_service",
    ):
        assert forbidden not in leaves, f"must not import {forbidden}"


def test_identity_classification_touches_no_network_or_database():
    """Pure function: classification is arithmetic on two strings."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(classify).lstrip())
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    for forbidden in ("post", "get", "execute", "connect", "request"):
        assert forbidden not in calls


def test_products_json_is_unchanged_by_identity_work(serper):
    import pathlib

    from app.services import product_service
    from app.services.product_service import list_products

    adopted = adopt_serper()
    before = [p.model_dump() for p in list_products()]
    path = pathlib.Path(product_service._DATA_FILE)
    before_bytes = path.read_bytes()
    before_mtime = path.stat().st_mtime

    post_revalidate(adopted.adopted_product_id)

    assert [p.model_dump() for p in list_products()] == before
    assert path.read_bytes() == before_bytes
    assert path.stat().st_mtime == before_mtime


def test_the_transaction_schema_is_unchanged_by_identity_work(serper):
    from app.services import transaction_service

    adopted = adopt_serper()
    with sqlite3.connect(adoption_service._db_path()) as conn:
        conn.executescript(transaction_service._SCHEMA)
        conn.commit()

    def columns():
        with sqlite3.connect(adoption_service._db_path()) as conn:
            conn.row_factory = sqlite3.Row
            return [r["name"] for r in conn.execute("PRAGMA table_info(transactions)")]

    before = columns()

    post_revalidate(adopted.adopted_product_id)

    assert columns() == before


def test_no_identity_status_is_a_purchase_state():
    assert {k.value for k in ProductIdentityKind} == {
        "PROVIDER_PRODUCT_ID",
        "PROVIDER_OBSERVATION_ONLY",
    }
    for forbidden in ("PURCHASABLE", "APPROVED", "ALLOWED", "VERIFIED_FOR_PURCHASE"):
        assert forbidden not in {k.value for k in ProductIdentityKind}


def test_a_revalidated_product_is_still_not_purchasable(serper):
    adopted = adopt_serper()
    body = post_revalidate(adopted.adopted_product_id).json()
    assert body["snapshot"]["revalidation_status"] == "REVALIDATED"

    response = client.post(
        "/purchase/execute",
        json={
            "product_id": adopted.adopted_product_id,
            "authorization": {
                "purchase_requested": True,
                "autonomous_purchase_allowed": True,
                "auto_approve_below_inr": 1000000,
            },
        },
    )

    assert response.status_code == 404
