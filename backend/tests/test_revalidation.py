"""Adopted-product revalidation tests (Phase 11B).

The property these tests defend: revalidation either confirms the EXACT
adopted product against fresh provider data, or it says it could not — and
there is no third path where a different product quietly takes its place.

Concretely pinned here:
  - a real provider request happens (the evidence cache alone is never
    treated as revalidation);
  - identity is the provider's external_id, so same-title and
    same-merchant impostors do not match;
  - the adopted snapshot is never overwritten, and history is append-only;
  - provider outages are reported, not converted into product verdicts;
  - no PayPal, Policy Engine, purchase-execution, ledger or AI path runs.

Providers are faked at the HTTP boundary (Serper) or injected (Channel3), so
the suite never makes a real Serper call. Each test gets its own temporary
SQLite database (conftest).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.adoption import AdoptedProductStatus
from app.schemas.discovery import NormalizedDiscoveredProduct
from app.schemas.revalidation import (
    AvailabilityStatus,
    IdentityMatch,
    MerchantStatus,
    PriceStatus,
    RevalidationReason,
    RevalidationStatus,
)
from app.services import (
    adoption_service,
    discovery_service,
    revalidation_service,
)
from app.services.adoption_service import AdoptedProductNotFoundError
from app.services.channel3_client import (
    Channel3Offer,
    Channel3Price,
    Channel3Product,
    Channel3SearchResult,
)
from app.services.discovery_evidence_store import record_products
from app.services.discovery_providers import serper as serper_module
from app.services.revalidation_service import (
    classify_availability,
    compare_merchant,
    compare_price,
    find_identity_match,
    revalidate,
)

client = TestClient(app)

ADOPTED_AT = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)
GOOGLE_LINK = "https://www.google.com/shopping/product/1"
TITLE = "Sony WH-CH520 Wireless Headphones"


# ---------------------------------------------------------------------------
# Fixtures and fakes
# ---------------------------------------------------------------------------


def evidence_product(**overrides) -> NormalizedDiscoveredProduct:
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
        "observed_at": ADOPTED_AT,
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
    """One Serper shopping result. productId drives the external_id."""
    item = {
        "title": TITLE,
        "source": "Amazon",
        "link": GOOGLE_LINK,
        "price": "$19.99",
        "imageUrl": "https://img.example/ch520.jpg",
        "productId": "111111",
    }
    item.update(overrides)
    return item


class SerperResponse:
    def __init__(self, items=None, status_code=200, malformed=False, body=None):
        self.status_code = status_code
        self._items = items if items is not None else [shopping_item()]
        self._malformed = malformed
        self._body = body

    def json(self):
        if self._malformed:
            raise ValueError("not json")
        if self._body is not None:
            return self._body
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
            raise AssertionError(f"revalidation must not touch {name}")

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
        transaction_service, "create_transaction", bomb("the ledger")
    )
    monkeypatch.setattr(transaction_service, "update_status", bomb("the ledger"))
    monkeypatch.setattr(transaction_service, "mark_captured", bomb("the ledger"))
    monkeypatch.setattr(refund_service, "refund_transaction", bomb("refunds"))
    monkeypatch.setattr(session_service, "create_session", bomb("sessions"))


@pytest.fixture
def no_ai(monkeypatch):
    """Any AI provider resolution fails the test."""
    from app.agents import comparison_agent, intent_agent
    from app.services import providers

    def bomb(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("revalidation must not invoke AI")

    monkeypatch.setattr(providers, "get_provider", bomb)
    monkeypatch.setattr(intent_agent, "get_provider", bomb)
    monkeypatch.setattr(comparison_agent, "rank", bomb)


@pytest.fixture
def serper(monkeypatch, frozen_core, no_ai):
    """An adopted Serper product plus a counted, faked Serper HTTP boundary.

    The real SerperProvider runs — only httpx.post is replaced — so these
    tests exercise the existing provider abstraction rather than a stand-in.
    """
    monkeypatch.setenv("SERPER_API_KEY", "test-serper-key")
    state = {"responses": [SerperResponse()], "calls": []}

    def fake_post(url, json=None, headers=None, timeout=None):
        state["calls"].append({"url": url, "json": json})
        result = state["responses"][0]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(serper_module.httpx, "post", fake_post)

    record_products([evidence_product()])
    adopted, _ = adoption_service.adopt("serper", "serper:111111")

    class Ctx:
        def __init__(self):
            self.adopted = adopted

        @staticmethod
        def respond(response):
            state["responses"][0] = response

        @staticmethod
        def calls():
            return state["calls"]

        @staticmethod
        def call_count():
            return len(state["calls"])

    return Ctx()


def c3_product(product_id="prod_1", *, title=TITLE, price="19.99", availability="InStock", domain="bestbuy.com"):
    return Channel3Product(
        id=product_id,
        title=title,
        offers=[
            Channel3Offer(
                url=f"https://www.{domain}/site/{product_id}",
                domain=domain,
                price=Channel3Price(price=Decimal(price), currency="USD"),
                availability=availability,
            )
        ],
    )


class FakeChannel3Client:
    def __init__(self, products=None, error=None):
        self.products = [c3_product()] if products is None else products
        self.error = error
        self.calls = []

    def search(self, query=None, *, limit=20, page_token=None, **kwargs):
        self.calls.append({"query": query, "limit": limit})
        if self.error:
            raise self.error
        return Channel3SearchResult(products=self.products)


@pytest.fixture
def channel3(frozen_core, no_ai):
    """An adopted Channel3 product, ready to revalidate with an injected client."""
    record_products(
        [
            evidence_product(
                source="channel3",
                external_id="prod_1",
                product_url="https://www.bestbuy.com/site/prod_1",
                retailer_domain="bestbuy.com",
                merchant_name=None,
                merchant_domain="bestbuy.com",
                availability="InStock",
            )
        ]
    )
    adopted, _ = adoption_service.adopt("channel3", "prod_1")
    return adopted


def post_revalidate(adopted_product_id, **kwargs):
    return client.post(
        f"/discovery/adopted/{adopted_product_id}/revalidate", **kwargs
    )


def _raw_connect():
    conn = sqlite3.connect(revalidation_service._db_path())
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# 1-2. Successful revalidation, with a real provider request
# ---------------------------------------------------------------------------


def test_successful_revalidation_confirms_identity(serper):
    response = post_revalidate(serper.adopted.adopted_product_id)

    assert response.status_code == 200
    snap = response.json()["snapshot"]
    assert snap["revalidation_status"] == "REVALIDATED"
    assert snap["identity_match"] == "EXACT_EXTERNAL_ID"
    assert snap["reason_code"] == "IDENTITY_CONFIRMED"
    assert snap["revalidation_id"].startswith("reval_")
    assert snap["adopted_product_id"] == serper.adopted.adopted_product_id


def test_a_fresh_provider_request_actually_occurs(serper):
    assert serper.call_count() == 0

    post_revalidate(serper.adopted.adopted_product_id)

    assert serper.call_count() == 1
    call = serper.calls()[0]
    assert call["url"] == "https://google.serper.dev/shopping"
    # The adopted title is the retrieval hint, read from the server record.
    assert call["json"]["q"] == TITLE


def test_the_evidence_cache_alone_is_not_revalidation(serper):
    """Cached evidence exists, yet an unreachable provider cannot confirm it."""
    serper.respond(httpx.ConnectError("boom"))

    snap = post_revalidate(serper.adopted.adopted_product_id).json()["snapshot"]

    # The product is in discovery_evidence, but that is history, not proof.
    assert snap["revalidation_status"] == "PROVIDER_UNAVAILABLE"
    assert snap["identity_match"] == "NOT_ATTEMPTED"
    assert snap["current_observed_price"] is None
    # And the provider really was contacted.
    assert serper.call_count() == 1


def test_fresh_evidence_is_never_backfilled_from_the_adopted_record(serper):
    """A failed attempt must not present stale values as fresh ones."""
    serper.respond(SerperResponse(items=[]))

    snap = post_revalidate(serper.adopted.adopted_product_id).json()["snapshot"]

    for field in (
        "current_title",
        "current_merchant_name",
        "current_merchant_domain",
        "current_product_url",
        "current_observed_price",
        "current_observed_currency",
        "current_normalized_price",
        "current_normalized_currency",
    ):
        assert snap[field] is None, field
    # The adopted figures appear only under their explicit "previous" names.
    assert snap["previous_observed_price"] == "19.99"
    assert snap["previous_observed_currency"] == "USD"


# ---------------------------------------------------------------------------
# 3-9. The server owns every input
# ---------------------------------------------------------------------------


def test_provider_selection_comes_from_the_adopted_record_not_the_env(
    monkeypatch, channel3
):
    """A Channel3 product revalidates against Channel3 even when env says serper."""
    monkeypatch.setenv("DISCOVERY_PROVIDER", "serper")

    def exploding_serper(*args, **kwargs):  # pragma: no cover
        raise AssertionError("must not revalidate a channel3 product via serper")

    monkeypatch.setattr(serper_module.httpx, "post", exploding_serper)
    fake = FakeChannel3Client()

    result = revalidate(channel3.adopted_product_id, client=fake)

    assert result.snapshot.revalidation_status is RevalidationStatus.REVALIDATED
    assert result.snapshot.discovery_source == "channel3"
    assert len(fake.calls) == 1


def test_provider_for_source_ignores_the_environment(monkeypatch):
    monkeypatch.setenv("DISCOVERY_PROVIDER", "channel3")
    monkeypatch.setenv("SERPER_API_KEY", "test-serper-key")

    assert discovery_service.provider_for_source("serper").source == "serper"
    assert (
        discovery_service.provider_for_source(
            "channel3", client=FakeChannel3Client()
        ).source
        == "channel3"
    )


def test_the_endpoint_takes_no_request_body(serper):
    """Structural: the route function has no body parameter at all."""
    import inspect

    from app.main import discovery_revalidate

    assert set(inspect.signature(discovery_revalidate).parameters) == {
        "adopted_product_id"
    }


@pytest.mark.parametrize(
    "forged",
    [
        {"source": "channel3"},
        {"discovery_source": "channel3"},
        {"external_id": "serper:999999"},
        {"discovery_external_id": "serper:999999"},
        {"observed_price": "1.00"},
        {"current_observed_price": "1.00"},
        {"observed_currency": "EUR"},
        {"merchant_name": "Fake Merchant"},
        {"merchant_domain": "evil.example"},
        {"product_url": "https://evil.example"},
        {"title": "Cheap Laptop"},
        {"paypal_order_id": "fake"},
        {"paypal_amount": "0.01"},
        {"revalidation_status": "REVALIDATED"},
        {"identity_match": "EXACT_EXTERNAL_ID"},
    ],
)
def test_a_forged_request_body_cannot_influence_the_outcome(serper, forged):
    """Whatever the client sends, the server uses its own record."""
    snap = post_revalidate(
        serper.adopted.adopted_product_id, json=forged
    ).json()["snapshot"]

    # Identity and provider are the server's, not the payload's.
    assert snap["discovery_source"] == "serper"
    assert snap["discovery_external_id"] == "serper:111111"
    assert snap["revalidation_status"] == "REVALIDATED"
    # Fresh facts came from the provider response, not the body.
    assert snap["current_title"] == TITLE
    assert snap["current_observed_price"] == "19.99"
    assert snap["current_observed_currency"] == "USD"
    assert snap["current_merchant_name"] == "Amazon"
    assert snap["current_product_url"] == GOOGLE_LINK
    # The query sent upstream came from the adopted title.
    assert serper.calls()[0]["json"]["q"] == TITLE


def test_no_forged_value_reaches_the_revalidation_table(serper):
    post_revalidate(
        serper.adopted.adopted_product_id,
        json={
            "title": "Cheap Laptop",
            "observed_price": "1.00",
            "merchant_name": "Fake Merchant",
            "product_url": "https://evil.example",
            "paypal_order_id": "fake",
            "external_id": "serper:999999",
        },
    )

    with _raw_connect() as conn:
        rows = [
            dict(r)
            for r in conn.execute("SELECT * FROM adopted_product_revalidations")
        ]

    dumped = str(rows)
    for forged in (
        "Cheap Laptop",
        "Fake Merchant",
        "evil.example",
        "1.00",
        "fake",
        "serper:999999",
    ):
        assert forged not in dumped, forged


def test_the_revalidate_function_takes_no_product_data():
    import inspect

    params = set(inspect.signature(revalidate).parameters)

    # Only the id, a scan bound, and the Channel3 DI hook.
    assert params == {"adopted_product_id", "limit", "client"}


# ---------------------------------------------------------------------------
# 10-13. Identity matching
# ---------------------------------------------------------------------------


def test_exact_external_id_match_succeeds(serper):
    """The matching product may be anywhere in the result list."""
    serper.respond(
        SerperResponse(
            items=[
                shopping_item(productId="999", title="Something else"),
                shopping_item(productId="888", title="Another thing"),
                shopping_item(productId="111111", price="$21.49"),
            ]
        )
    )

    snap = post_revalidate(serper.adopted.adopted_product_id).json()["snapshot"]

    assert snap["identity_match"] == "EXACT_EXTERNAL_ID"
    assert snap["current_observed_price"] == "21.49"


def test_identity_mismatch_fails_safely(serper):
    serper.respond(SerperResponse(items=[shopping_item(productId="999999")]))

    snap = post_revalidate(serper.adopted.adopted_product_id).json()["snapshot"]

    assert snap["revalidation_status"] == "IDENTITY_NOT_FOUND"
    assert snap["identity_match"] == "NOT_FOUND"
    assert snap["reason_code"] == "NO_RESULT_MATCHED_IDENTITY"
    # No fresh facts are adopted from the non-matching result.
    assert snap["current_title"] is None
    assert snap["current_observed_price"] is None


def test_the_first_result_is_never_used_as_a_fallback(serper):
    """The classic dangerous implementation, explicitly ruled out."""
    serper.respond(
        SerperResponse(
            items=[shopping_item(productId="aaa", title=TITLE, price="$4.99")]
        )
    )

    snap = post_revalidate(serper.adopted.adopted_product_id).json()["snapshot"]

    assert snap["revalidation_status"] == "IDENTITY_NOT_FOUND"
    assert snap["current_observed_price"] is None


def test_same_title_different_product_does_not_match(serper):
    """Identical title, identical merchant, different provider id."""
    serper.respond(
        SerperResponse(
            items=[
                shopping_item(productId="222222", title=TITLE, source="Amazon")
            ]
        )
    )

    snap = post_revalidate(serper.adopted.adopted_product_id).json()["snapshot"]

    assert snap["revalidation_status"] == "IDENTITY_NOT_FOUND"


def test_same_merchant_different_product_does_not_match(serper):
    serper.respond(
        SerperResponse(
            items=[
                shopping_item(
                    productId="333333", title="Totally different", source="Amazon"
                )
            ]
        )
    )

    snap = post_revalidate(serper.adopted.adopted_product_id).json()["snapshot"]

    assert snap["revalidation_status"] == "IDENTITY_NOT_FOUND"


def test_same_price_different_product_does_not_match(serper):
    serper.respond(
        SerperResponse(
            items=[shopping_item(productId="444444", price="$19.99")]
        )
    )

    snap = post_revalidate(serper.adopted.adopted_product_id).json()["snapshot"]

    assert snap["revalidation_status"] == "IDENTITY_NOT_FOUND"


def test_a_matching_id_from_another_provider_does_not_match():
    """Identity is (source, external_id) — the pair, not the id alone."""
    products = [
        evidence_product(source="channel3", external_id="shared_id", title="C3")
    ]

    assert find_identity_match(products, "serper", "shared_id") is None
    assert find_identity_match(products, "channel3", "shared_id") is not None


def test_provider_returning_no_results_is_identity_not_found(serper):
    serper.respond(SerperResponse(items=[]))

    snap = post_revalidate(serper.adopted.adopted_product_id).json()["snapshot"]

    assert snap["revalidation_status"] == "IDENTITY_NOT_FOUND"
    assert snap["reason_code"] == "PROVIDER_RETURNED_NO_RESULTS"


@pytest.mark.parametrize(
    ("source", "external_id"), [("", "serper:1"), ("serper", ""), ("", "")]
)
def test_find_identity_match_refuses_a_blank_identity(source, external_id):
    assert find_identity_match([evidence_product()], source, external_id) is None


# ---------------------------------------------------------------------------
# 14-17. Price semantics
# ---------------------------------------------------------------------------


def test_price_unchanged(serper):
    snap = post_revalidate(serper.adopted.adopted_product_id).json()["snapshot"]

    assert snap["price_status"] == "PRICE_UNCHANGED"
    assert snap["previous_observed_price"] == "19.99"
    assert snap["current_observed_price"] == "19.99"


def test_price_changed_does_not_mutate_the_adopted_record(serper):
    serper.respond(SerperResponse(items=[shopping_item(price="$24.99")]))

    snap = post_revalidate(serper.adopted.adopted_product_id).json()["snapshot"]

    assert snap["price_status"] == "PRICE_CHANGED"
    assert snap["previous_observed_price"] == "19.99"
    assert snap["current_observed_price"] == "24.99"
    # The adopted snapshot still records what was adopted.
    assert adoption_service.get(
        serper.adopted.adopted_product_id
    ).observed_price == Decimal("19.99")


def test_price_unavailable_when_the_provider_supplies_none(serper):
    serper.respond(SerperResponse(items=[shopping_item(price=None)]))

    snap = post_revalidate(serper.adopted.adopted_product_id).json()["snapshot"]

    # Identity still confirmed; only the price is missing.
    assert snap["revalidation_status"] == "REVALIDATED"
    assert snap["price_status"] == "PRICE_UNAVAILABLE"
    assert snap["current_observed_price"] is None


def test_currency_unchanged_is_reported_with_the_price(serper):
    snap = post_revalidate(serper.adopted.adopted_product_id).json()["snapshot"]

    assert snap["previous_observed_currency"] == "USD"
    assert snap["current_observed_currency"] == "USD"
    assert snap["price_status"] == "PRICE_UNCHANGED"


def test_the_fresh_normalized_price_is_derived_by_the_existing_pipeline(serper):
    serper.respond(SerperResponse(items=[shopping_item(price="$24.99")]))

    snap = post_revalidate(serper.adopted.adopted_product_id).json()["snapshot"]

    # 24.99 * 91.25 = 2280.3375 -> 2280.34 (ROUND_HALF_UP), Phase 10F.
    assert snap["current_normalized_price"] == "2280.34"
    assert snap["current_normalized_currency"] == "INR"


@pytest.mark.parametrize(
    ("prev", "prev_cur", "cur", "cur_cur", "expected"),
    [
        (Decimal("19.99"), "USD", Decimal("19.99"), "USD", PriceStatus.PRICE_UNCHANGED),
        (Decimal("19.99"), "USD", Decimal("24.99"), "USD", PriceStatus.PRICE_CHANGED),
        (Decimal("19.99"), "usd", Decimal("19.99"), "USD", PriceStatus.PRICE_UNCHANGED),
        (Decimal("19.99"), "USD", None, "USD", PriceStatus.PRICE_UNAVAILABLE),
        (Decimal("19.99"), "USD", Decimal("19.99"), None, PriceStatus.PRICE_UNAVAILABLE),
        # Cross-currency is NOT silently called changed or unchanged.
        (Decimal("19.99"), "USD", Decimal("19.99"), "EUR", PriceStatus.PRICE_NOT_COMPARABLE),
        (None, None, Decimal("19.99"), "USD", PriceStatus.PRICE_NOT_COMPARABLE),
        (Decimal("19.99"), None, Decimal("19.99"), "USD", PriceStatus.PRICE_NOT_COMPARABLE),
        (None, None, None, None, PriceStatus.PRICE_UNAVAILABLE),
    ],
)
def test_compare_price_is_exact_and_currency_aware(
    prev, prev_cur, cur, cur_cur, expected
):
    assert compare_price(prev, prev_cur, cur, cur_cur) is expected


def test_compare_price_uses_no_float_arithmetic():
    """0.1 + 0.2 style drift must not make equal prices look different."""
    assert (
        compare_price(Decimal("0.30"), "USD", Decimal("0.30"), "USD")
        is PriceStatus.PRICE_UNCHANGED
    )
    # Decimal distinguishes scale-equal values correctly.
    assert (
        compare_price(Decimal("19.90"), "USD", Decimal("19.9"), "USD")
        is PriceStatus.PRICE_UNCHANGED
    )


# ---------------------------------------------------------------------------
# 18-20. Availability semantics
# ---------------------------------------------------------------------------


def test_availability_available_from_an_explicit_provider_statement(channel3):
    result = revalidate(
        channel3.adopted_product_id,
        client=FakeChannel3Client([c3_product(availability="InStock")]),
    )

    assert result.snapshot.revalidation_status is RevalidationStatus.REVALIDATED
    assert result.snapshot.availability is AvailabilityStatus.AVAILABLE


def test_availability_unavailable_from_an_explicit_provider_statement(channel3):
    result = revalidate(
        channel3.adopted_product_id,
        client=FakeChannel3Client([c3_product(availability="OutOfStock")]),
    )

    assert result.snapshot.availability is AvailabilityStatus.UNAVAILABLE


def test_availability_unknown_when_the_provider_states_nothing(serper):
    """Serper carries no availability field at all."""
    snap = post_revalidate(serper.adopted.adopted_product_id).json()["snapshot"]

    assert snap["revalidation_status"] == "REVALIDATED"
    assert snap["availability"] == "UNKNOWN"


def test_availability_is_not_inferred_from_a_missing_price(serper):
    serper.respond(SerperResponse(items=[shopping_item(price=None)]))

    snap = post_revalidate(serper.adopted.adopted_product_id).json()["snapshot"]

    assert snap["price_status"] == "PRICE_UNAVAILABLE"
    # Absent price does NOT become "out of stock".
    assert snap["availability"] == "UNKNOWN"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("InStock", AvailabilityStatus.AVAILABLE),
        ("in_stock", AvailabilityStatus.AVAILABLE),
        ("  available  ", AvailabilityStatus.AVAILABLE),
        ("OutOfStock", AvailabilityStatus.UNAVAILABLE),
        ("out-of-stock", AvailabilityStatus.UNAVAILABLE),
        ("SoldOut", AvailabilityStatus.UNAVAILABLE),
        (None, AvailabilityStatus.UNKNOWN),
        ("", AvailabilityStatus.UNKNOWN),
        ("   ", AvailabilityStatus.UNKNOWN),
        ("BackOrder", AvailabilityStatus.UNKNOWN),
        ("PreOrder", AvailabilityStatus.UNKNOWN),
    ],
)
def test_classify_availability(raw, expected):
    assert classify_availability(raw) is expected


# ---------------------------------------------------------------------------
# 21-22. Merchant semantics
# ---------------------------------------------------------------------------


def test_merchant_identity_is_refreshed_from_the_provider(serper):
    snap = post_revalidate(serper.adopted.adopted_product_id).json()["snapshot"]

    assert snap["current_merchant_name"] == "Amazon"
    assert snap["merchant_status"] == "MERCHANT_UNCHANGED"


def test_a_merchant_change_is_reported_but_does_not_fail_revalidation(serper):
    serper.respond(SerperResponse(items=[shopping_item(source="Walmart")]))

    snap = post_revalidate(serper.adopted.adopted_product_id).json()["snapshot"]

    # Identity is the external_id, so the product is still confirmed.
    assert snap["revalidation_status"] == "REVALIDATED"
    assert snap["current_merchant_name"] == "Walmart"
    assert snap["merchant_status"] == "MERCHANT_CHANGED"


def test_merchant_formatting_changes_are_not_reported_as_a_change(serper):
    serper.respond(SerperResponse(items=[shopping_item(source="  amazon  ")]))

    snap = post_revalidate(serper.adopted.adopted_product_id).json()["snapshot"]

    assert snap["current_merchant_name"] == "amazon"
    assert snap["merchant_status"] == "MERCHANT_UNCHANGED"


def test_merchant_domain_is_never_guessed_from_a_name(serper):
    """A Google Shopping link yields no domain, however named the seller is."""
    snap = post_revalidate(serper.adopted.adopted_product_id).json()["snapshot"]

    assert snap["current_merchant_name"] == "Amazon"
    assert snap["current_merchant_domain"] is None
    assert snap["current_product_url"] == GOOGLE_LINK


def test_merchant_domain_is_derived_when_the_link_is_direct(serper):
    serper.respond(
        SerperResponse(
            items=[
                shopping_item(
                    source="Best Buy", link="https://www.bestbuy.com/site/ch520"
                )
            ]
        )
    )

    snap = post_revalidate(serper.adopted.adopted_product_id).json()["snapshot"]

    assert snap["current_merchant_domain"] == "bestbuy.com"


@pytest.mark.parametrize(
    ("pn", "pd", "cn", "cd", "expected"),
    [
        ("Amazon", None, "Amazon", None, MerchantStatus.MERCHANT_UNCHANGED),
        ("Amazon", None, "  amazon ", None, MerchantStatus.MERCHANT_UNCHANGED),
        ("Amazon", None, "Walmart", None, MerchantStatus.MERCHANT_CHANGED),
        (None, "bestbuy.com", None, "bestbuy.com", MerchantStatus.MERCHANT_UNCHANGED),
        (None, "bestbuy.com", None, "walmart.com", MerchantStatus.MERCHANT_CHANGED),
        # Domain wins over name when present on both sides.
        ("Old Name", "shop.example", "New Name", "shop.example", MerchantStatus.MERCHANT_UNCHANGED),
        (None, None, "Amazon", None, MerchantStatus.MERCHANT_UNKNOWN),
        ("Amazon", None, None, None, MerchantStatus.MERCHANT_UNKNOWN),
        (None, None, None, None, MerchantStatus.MERCHANT_UNKNOWN),
    ],
)
def test_compare_merchant(pn, pd, cn, cd, expected):
    assert compare_merchant(pn, pd, cn, cd) is expected


# ---------------------------------------------------------------------------
# 23-25. History is append-only; the adopted snapshot is frozen
# ---------------------------------------------------------------------------


def test_the_adopted_snapshot_is_never_overwritten(serper):
    before = adoption_service.get(serper.adopted.adopted_product_id).model_dump()
    serper.respond(
        SerperResponse(
            items=[
                shopping_item(
                    productId="111111",
                    title="Renamed By Retailer",
                    price="$99.99",
                    source="Walmart",
                    link="https://www.walmart.com/ip/x",
                )
            ]
        )
    )

    post_revalidate(serper.adopted.adopted_product_id)

    after = adoption_service.get(serper.adopted.adopted_product_id).model_dump()
    # Only lifecycle bookkeeping may ever differ, and here nothing does:
    # the status was already ACTIVE and stays ACTIVE.
    assert after == before


def test_revalidation_is_persisted(serper):
    body = post_revalidate(serper.adopted.adopted_product_id).json()

    stored = revalidation_service.list_revalidations(
        serper.adopted.adopted_product_id
    )
    assert len(stored) == 1
    assert stored[0].revalidation_id == body["snapshot"]["revalidation_id"]
    assert stored[0].current_observed_price == Decimal("19.99")
    assert stored[0].previous_observed_price == Decimal("19.99")


def test_multiple_revalidations_produce_separate_snapshots(serper):
    first = post_revalidate(serper.adopted.adopted_product_id).json()["snapshot"]
    serper.respond(SerperResponse(items=[shopping_item(price="$24.99")]))
    second = post_revalidate(serper.adopted.adopted_product_id).json()["snapshot"]
    serper.respond(SerperResponse(items=[shopping_item(price="$29.99")]))
    third = post_revalidate(serper.adopted.adopted_product_id).json()["snapshot"]

    ids = {first["revalidation_id"], second["revalidation_id"], third["revalidation_id"]}
    assert len(ids) == 3
    assert revalidation_service.count(serper.adopted.adopted_product_id) == 3

    history = revalidation_service.list_revalidations(
        serper.adopted.adopted_product_id
    )
    # Each snapshot keeps its own fresh price; none rewrote an earlier one.
    assert {str(s.current_observed_price) for s in history} == {
        "19.99",
        "24.99",
        "29.99",
    }
    # Every snapshot compares against the SAME frozen adopted price.
    assert {str(s.previous_observed_price) for s in history} == {"19.99"}


def test_a_failed_attempt_is_also_recorded(serper):
    """History must show that a check was tried and why it produced nothing."""
    serper.respond(SerperResponse(status_code=500))

    post_revalidate(serper.adopted.adopted_product_id)

    stored = revalidation_service.list_revalidations(
        serper.adopted.adopted_product_id
    )
    assert len(stored) == 1
    assert stored[0].revalidation_status is RevalidationStatus.PROVIDER_UNAVAILABLE


def test_history_is_newest_first_and_bounded(serper):
    for _ in range(3):
        post_revalidate(serper.adopted.adopted_product_id)

    history = revalidation_service.list_revalidations(
        serper.adopted.adopted_product_id, limit=2
    )

    assert len(history) == 2
    assert history[0].checked_at >= history[1].checked_at


def test_history_of_another_product_is_not_returned(serper):
    record_products([evidence_product(external_id="serper:other")])
    other, _ = adoption_service.adopt("serper", "serper:other")

    post_revalidate(serper.adopted.adopted_product_id)

    assert revalidation_service.count(other.adopted_product_id) == 0
    assert revalidation_service.count(serper.adopted.adopted_product_id) == 1


@pytest.mark.parametrize("bad_limit", [0, -1, 101])
def test_history_limit_is_bounded(serper, bad_limit):
    with pytest.raises(ValueError):
        revalidation_service.list_revalidations(
            serper.adopted.adopted_product_id, limit=bad_limit
        )


# ---------------------------------------------------------------------------
# 26. Decimal precision
# ---------------------------------------------------------------------------


def test_decimal_precision_is_preserved_through_storage(serper):
    serper.respond(SerperResponse(items=[shopping_item(price="$1,299.95")]))

    snap = post_revalidate(serper.adopted.adopted_product_id).json()["snapshot"]

    assert snap["current_observed_price"] == "1299.95"
    assert isinstance(snap["current_observed_price"], str)

    stored = revalidation_service.list_revalidations(
        serper.adopted.adopted_product_id
    )[0]
    assert isinstance(stored.current_observed_price, Decimal)
    assert stored.current_observed_price == Decimal("1299.95")
    # 1299.95 * 91.25 = 118620.4375 -> 118620.44
    assert stored.current_normalized_price == Decimal("118620.44")


def test_prices_are_stored_as_text_not_real(serper):
    post_revalidate(serper.adopted.adopted_product_id)

    with _raw_connect() as conn:
        types = {
            row["name"]: row["type"]
            for row in conn.execute(
                "PRAGMA table_info(adopted_product_revalidations)"
            )
        }

    for column in (
        "current_observed_price",
        "current_normalized_price",
        "previous_observed_price",
    ):
        assert types[column] == "TEXT", column


# ---------------------------------------------------------------------------
# 27-31. Provider failure handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status_code", [401, 403])
def test_provider_auth_failure_is_a_permanent_provider_error(serper, status_code):
    serper.respond(SerperResponse(status_code=status_code))

    snap = post_revalidate(serper.adopted.adopted_product_id).json()["snapshot"]

    assert snap["revalidation_status"] == "PROVIDER_ERROR"
    assert snap["reason_code"] == "PROVIDER_AUTH_REJECTED"
    assert snap["identity_match"] == "NOT_ATTEMPTED"


def test_provider_quota_exhaustion_is_a_permanent_provider_error(serper):
    serper.respond(SerperResponse(status_code=402))

    snap = post_revalidate(serper.adopted.adopted_product_id).json()["snapshot"]

    assert snap["revalidation_status"] == "PROVIDER_ERROR"
    assert snap["reason_code"] == "PROVIDER_QUOTA_EXHAUSTED"


def test_provider_rate_limit_is_transient(serper):
    serper.respond(SerperResponse(status_code=429))

    snap = post_revalidate(serper.adopted.adopted_product_id).json()["snapshot"]

    assert snap["revalidation_status"] == "PROVIDER_UNAVAILABLE"
    assert snap["reason_code"] == "PROVIDER_RATE_LIMITED"


@pytest.mark.parametrize("status_code", [500, 502, 503, 504])
def test_provider_5xx_is_transient(serper, status_code):
    serper.respond(SerperResponse(status_code=status_code))

    snap = post_revalidate(serper.adopted.adopted_product_id).json()["snapshot"]

    assert snap["revalidation_status"] == "PROVIDER_UNAVAILABLE"
    assert snap["reason_code"] == "PROVIDER_UPSTREAM_FAILURE"


@pytest.mark.parametrize(
    "error",
    [
        httpx.TimeoutException("timed out"),
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
    ],
)
def test_provider_network_failure_is_transient(serper, error):
    serper.respond(error)

    snap = post_revalidate(serper.adopted.adopted_product_id).json()["snapshot"]

    assert snap["revalidation_status"] == "PROVIDER_UNAVAILABLE"
    assert snap["reason_code"] == "PROVIDER_UPSTREAM_FAILURE"


@pytest.mark.parametrize("status_code", [400, 404, 422])
def test_provider_rejecting_the_request_is_a_permanent_error(serper, status_code):
    serper.respond(SerperResponse(status_code=status_code))

    snap = post_revalidate(serper.adopted.adopted_product_id).json()["snapshot"]

    assert snap["revalidation_status"] == "PROVIDER_ERROR"
    assert snap["reason_code"] == "PROVIDER_REJECTED_REQUEST"


def test_malformed_provider_response_is_a_permanent_error(serper):
    serper.respond(SerperResponse(malformed=True))

    snap = post_revalidate(serper.adopted.adopted_product_id).json()["snapshot"]

    assert snap["revalidation_status"] == "PROVIDER_ERROR"
    assert snap["reason_code"] == "PROVIDER_RESPONSE_UNUSABLE"
    assert snap["current_observed_price"] is None


def test_wrong_schema_provider_response_is_a_permanent_error(serper):
    serper.respond(SerperResponse(body={"shopping": "not-a-list"}))

    snap = post_revalidate(serper.adopted.adopted_product_id).json()["snapshot"]

    assert snap["revalidation_status"] == "PROVIDER_ERROR"
    assert snap["reason_code"] == "PROVIDER_RESPONSE_UNUSABLE"


def test_an_unconfigured_provider_is_not_revalidatable(monkeypatch, channel3):
    """Channel3 has no key here, so the attempt cannot even be made."""
    monkeypatch.delenv("CHANNEL3_API_KEY", raising=False)

    result = revalidate(channel3.adopted_product_id)

    assert result.snapshot.revalidation_status is RevalidationStatus.NOT_REVALIDATABLE
    assert result.snapshot.reason_code is RevalidationReason.PROVIDER_NOT_CONFIGURED
    assert result.snapshot.identity_match is IdentityMatch.NOT_ATTEMPTED


def test_an_unknown_provider_name_is_unverifiable(frozen_core, no_ai):
    """Phase 11B.1: an unknown provider has no durability evidence here.

    This used to report NOT_REVALIDATABLE / PROVIDER_NOT_CONFIGURED. The
    11B.1 answer is more precise and strictly safer: with no evidence that
    the provider's id is durable, identity cannot be verified AT ALL, so the
    check short-circuits before a provider is even constructed. (The
    genuinely-unconfigured case is still covered by
    test_an_unconfigured_provider_is_not_revalidatable, which uses Channel3
    — a provider whose ids ARE durable.)
    """
    record_products(
        [evidence_product(source="mystery", external_id="m1")]
    )
    adopted, _ = adoption_service.adopt("mystery", "m1")

    result = revalidate(adopted.adopted_product_id)

    assert (
        result.snapshot.revalidation_status
        is RevalidationStatus.IDENTITY_UNVERIFIABLE
    )
    assert (
        result.snapshot.reason_code
        is RevalidationReason.NO_DURABLE_PRODUCT_IDENTITY
    )
    assert result.snapshot.identity_match is IdentityMatch.UNVERIFIABLE


def test_a_provider_failure_never_changes_the_adopted_status(serper):
    """A provider outage must not punish our record."""
    for response in (
        SerperResponse(status_code=500),
        SerperResponse(status_code=429),
        SerperResponse(status_code=401),
        httpx.ConnectError("boom"),
        SerperResponse(malformed=True),
    ):
        serper.respond(response)
        body = post_revalidate(serper.adopted.adopted_product_id).json()

        assert body["adopted_status_changed"] is False
        assert body["adopted_product_status"] == "ACTIVE"


# ---------------------------------------------------------------------------
# 32-33. Unknown and disabled adopted products
# ---------------------------------------------------------------------------


def test_unknown_adopted_product_is_404(frozen_core, no_ai):
    response = post_revalidate("adopt_does_not_exist")

    assert response.status_code == 404
    assert revalidation_service.count() == 0


def test_unknown_adopted_product_raises_a_typed_error(frozen_core):
    with pytest.raises(AdoptedProductNotFoundError):
        revalidate("adopt_does_not_exist")


def test_identity_not_found_marks_the_adopted_record_stale(serper):
    serper.respond(SerperResponse(items=[shopping_item(productId="999999")]))

    body = post_revalidate(serper.adopted.adopted_product_id).json()

    assert body["snapshot"]["revalidation_status"] == "IDENTITY_NOT_FOUND"
    assert body["adopted_product_status"] == "STALE"
    assert body["adopted_status_changed"] is True


def test_marking_stale_changes_no_product_fact(serper):
    before = adoption_service.get(serper.adopted.adopted_product_id).model_dump()
    serper.respond(SerperResponse(items=[shopping_item(productId="999999")]))

    post_revalidate(serper.adopted.adopted_product_id)

    after = adoption_service.get(serper.adopted.adopted_product_id).model_dump()
    for field, value in before.items():
        if field in ("status", "updated_at"):
            continue
        assert after[field] == value, field
    assert after["status"] == "STALE"


def test_a_recovered_product_returns_to_active(serper):
    serper.respond(SerperResponse(items=[shopping_item(productId="999999")]))
    post_revalidate(serper.adopted.adopted_product_id)
    assert (
        adoption_service.get(serper.adopted.adopted_product_id).status
        is AdoptedProductStatus.STALE
    )

    serper.respond(SerperResponse())
    body = post_revalidate(serper.adopted.adopted_product_id).json()

    assert body["snapshot"]["revalidation_status"] == "REVALIDATED"
    assert body["adopted_product_status"] == "ACTIVE"
    assert body["adopted_status_changed"] is True


def test_a_disabled_product_is_revalidated_but_never_re_enabled(serper):
    adoption_service.set_status(
        serper.adopted.adopted_product_id, AdoptedProductStatus.DISABLED
    )

    body = post_revalidate(serper.adopted.adopted_product_id).json()

    # The observation still happens and is recorded...
    assert body["snapshot"]["revalidation_status"] == "REVALIDATED"
    assert revalidation_service.count(serper.adopted.adopted_product_id) == 1
    # ...but an operator decision is never overridden by an observation.
    assert body["adopted_product_status"] == "DISABLED"
    assert body["adopted_status_changed"] is False


def test_a_disabled_product_is_not_marked_stale_either(serper):
    adoption_service.set_status(
        serper.adopted.adopted_product_id, AdoptedProductStatus.DISABLED
    )
    serper.respond(SerperResponse(items=[shopping_item(productId="999999")]))

    body = post_revalidate(serper.adopted.adopted_product_id).json()

    assert body["adopted_product_status"] == "DISABLED"
    assert body["adopted_status_changed"] is False


# ---------------------------------------------------------------------------
# 34-37. No payment, no AI
# ---------------------------------------------------------------------------


def test_revalidation_makes_no_paypal_policy_purchase_or_ai_call(serper):
    """frozen_core and no_ai boobytraps would raise if any were touched."""
    response = post_revalidate(serper.adopted.adopted_product_id)

    assert response.status_code == 200


def _imported_modules(module) -> set[str]:
    """Every module name `module` imports, as written in its source.

    Parsed from the AST rather than matched as substrings, so the legitimate
    `discovery_providers` abstraction is not confused with the AI
    `providers` module, and `provider_for_source` is not mistaken for either.
    """
    import ast
    import pathlib

    tree = ast.parse(
        pathlib.Path(module.__file__).read_text(encoding="utf-8")
    )
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            names.add(base)
            for alias in node.names:
                names.add(f"{base}.{alias.name}" if base else alias.name)
    return names


def test_revalidation_service_imports_no_payment_or_ai_module():
    """Structural guard: the import graph forbids a payment or AI bridge."""
    imported = _imported_modules(revalidation_service)
    # Leaf module names, so a relative import resolves the same way.
    leaves = {name.rsplit(".", 1)[-1] for name in imported}

    for forbidden in (
        "paypal",
        "policy",
        "policy_engine",
        "purchase_execution_service",
        "purchase_orchestrator",
        "transaction_service",
        "refund_service",
        "session_service",
        "confirmation_service",
        "webhook_service",
        "webhook_reconciliation_service",
        "providers",  # the AI provider registry
        "comparison_agent",
        "intent_agent",
        "recommendation_agent",
        "router_agent",
        "product_service",
        "currency_service",  # the PayPal demo FX rate
    ):
        assert forbidden not in leaves, f"revalidation must not import {forbidden}"

    # It DOES legitimately use the discovery provider abstraction.
    assert any("discovery_providers" in name for name in imported)


def test_revalidation_service_imports_no_agents_package():
    """No AI agent may be reachable from revalidation, even indirectly here."""
    imported = _imported_modules(revalidation_service)

    assert not any(
        name.startswith("agents") or ".agents" in name for name in imported
    )


def test_the_response_exposes_no_paypal_or_policy_field(serper):
    body = post_revalidate(serper.adopted.adopted_product_id).json()

    for forbidden in (
        "paypal_order_id",
        "paypal_capture_id",
        "paypal_amount",
        "paypal_currency",
        "refund_id",
        "transaction_id",
        "purchase_price",
        "policy_decision",
        "amount",
    ):
        assert forbidden not in body["snapshot"]

    text = str(body).lower()
    assert "paypal" not in text
    assert "purchase_price" not in text


def test_the_revalidation_table_holds_no_payment_columns(serper):
    post_revalidate(serper.adopted.adopted_product_id)

    with _raw_connect() as conn:
        columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(adopted_product_revalidations)"
            )
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
        "ai_reason",
    ):
        assert forbidden not in columns


def test_no_status_is_a_purchase_or_payment_state():
    values = {s.value for s in RevalidationStatus}

    assert values == {
        "REVALIDATED",
        "IDENTITY_NOT_FOUND",
        "IDENTITY_UNVERIFIABLE",  # added in Phase 11B.1
        "PROVIDER_UNAVAILABLE",
        "PROVIDER_ERROR",
        "NOT_REVALIDATABLE",
    }
    for forbidden in (
        "PURCHASE_ALLOWED",
        "PURCHASE_DENIED",
        "PAYMENT_APPROVED",
        "ORDER_CREATED",
        "CAPTURED",
        "REFUNDED",
    ):
        assert forbidden not in values


def test_revalidation_does_not_create_the_payment_tables(serper):
    post_revalidate(serper.adopted.adopted_product_id)

    with _raw_connect() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert "adopted_product_revalidations" in tables
    assert "transactions" not in tables
    assert "sessions" not in tables


def test_a_revalidated_product_is_still_not_purchasable(serper):
    """REVALIDATED is not a purchase permission."""
    body = post_revalidate(serper.adopted.adopted_product_id).json()
    assert body["snapshot"]["revalidation_status"] == "REVALIDATED"

    for identifier in (
        serper.adopted.adopted_product_id,
        serper.adopted.discovery_external_id,
    ):
        response = client.post(
            "/purchase/execute",
            json={
                "product_id": identifier,
                "authorization": {
                    "purchase_requested": True,
                    "autonomous_purchase_allowed": True,
                    "auto_approve_below_inr": 1000000,
                },
            },
        )
        assert response.status_code == 404, identifier


# ---------------------------------------------------------------------------
# 38-39. Existing catalog and ledger untouched
# ---------------------------------------------------------------------------


def test_local_catalog_is_unchanged_by_revalidation(serper):
    import pathlib

    from app.services import product_service
    from app.services.product_service import list_products

    before = [p.model_dump() for p in list_products()]
    path = pathlib.Path(product_service._DATA_FILE)
    before_bytes = path.read_bytes()

    post_revalidate(serper.adopted.adopted_product_id)

    assert [p.model_dump() for p in list_products()] == before
    assert path.read_bytes() == before_bytes


def test_the_transaction_schema_is_unchanged(serper):
    """Create the ledger table, then prove revalidation leaves it intact."""
    from app.services import transaction_service

    with sqlite3.connect(revalidation_service._db_path()) as conn:
        conn.executescript(transaction_service._SCHEMA)
        conn.commit()

    def columns():
        with _raw_connect() as conn:
            return [row["name"] for row in conn.execute("PRAGMA table_info(transactions)")]

    before = columns()

    post_revalidate(serper.adopted.adopted_product_id)

    assert columns() == before
    with _raw_connect() as conn:
        assert (
            conn.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"] == 0
        )


def test_the_transactions_endpoint_still_reports_an_empty_ledger(serper):
    post_revalidate(serper.adopted.adopted_product_id)

    response = client.get("/transactions")

    assert response.status_code == 200
    assert response.json() == {"transactions": [], "count": 0}


# ---------------------------------------------------------------------------
# 40. Repeated revalidation semantics
# ---------------------------------------------------------------------------


def test_repeated_revalidation_is_not_idempotent_by_design(serper):
    """Each call is a NEW observation, so each gets its own snapshot.

    Collapsing repeats would mean reporting a stale check as current, which
    is the opposite of what revalidation is for. Idempotence applies to the
    ADOPTED record (never duplicated, never overwritten), not to the act of
    observing.
    """
    first = post_revalidate(serper.adopted.adopted_product_id).json()
    second = post_revalidate(serper.adopted.adopted_product_id).json()

    assert (
        first["snapshot"]["revalidation_id"]
        != second["snapshot"]["revalidation_id"]
    )
    assert serper.call_count() == 2
    assert revalidation_service.count(serper.adopted.adopted_product_id) == 2
    # The adopted record itself was neither duplicated nor altered.
    assert adoption_service.count() == 1


def test_repeated_revalidation_with_identical_data_is_stable(serper):
    """Same provider answer -> same verdict, every time. No drift."""
    outcomes = [
        post_revalidate(serper.adopted.adopted_product_id).json()["snapshot"]
        for _ in range(3)
    ]

    for snap in outcomes:
        assert snap["revalidation_status"] == "REVALIDATED"
        assert snap["identity_match"] == "EXACT_EXTERNAL_ID"
        assert snap["price_status"] == "PRICE_UNCHANGED"
        assert snap["availability"] == "UNKNOWN"
        assert snap["merchant_status"] == "MERCHANT_UNCHANGED"
        assert snap["current_observed_price"] == "19.99"


def test_checked_at_is_server_generated_and_utc(serper):
    before = datetime.now(timezone.utc)
    snap = post_revalidate(serper.adopted.adopted_product_id).json()["snapshot"]
    after = datetime.now(timezone.utc)

    checked_at = datetime.fromisoformat(snap["checked_at"])
    assert checked_at.tzinfo is not None
    assert before <= checked_at <= after
    # Distinct from the adopted record's own timestamps.
    assert snap["checked_at"] != serper.adopted.observed_at


# ---------------------------------------------------------------------------
# Channel3 coverage
# ---------------------------------------------------------------------------


def test_channel3_revalidation_confirms_identity_and_refreshes_evidence(channel3):
    result = revalidate(
        channel3.adopted_product_id,
        client=FakeChannel3Client([c3_product(price="24.50")]),
    )

    snap = result.snapshot
    assert snap.revalidation_status is RevalidationStatus.REVALIDATED
    assert snap.identity_match is IdentityMatch.EXACT_EXTERNAL_ID
    assert snap.current_observed_price == Decimal("24.50")
    assert snap.price_status is PriceStatus.PRICE_CHANGED
    assert snap.current_merchant_domain == "bestbuy.com"
    assert snap.availability is AvailabilityStatus.AVAILABLE


def test_channel3_identity_mismatch_fails_safely(channel3):
    result = revalidate(
        channel3.adopted_product_id,
        client=FakeChannel3Client([c3_product(product_id="prod_999")]),
    )

    assert result.snapshot.revalidation_status is RevalidationStatus.IDENTITY_NOT_FOUND
    assert result.snapshot.current_observed_price is None


def test_channel3_provider_failure_is_classified(channel3):
    from app.services.channel3_client import Channel3RateLimitError

    result = revalidate(
        channel3.adopted_product_id,
        client=FakeChannel3Client(error=Channel3RateLimitError("429")),
    )

    assert result.snapshot.revalidation_status is RevalidationStatus.PROVIDER_UNAVAILABLE
    assert result.snapshot.reason_code is RevalidationReason.PROVIDER_RATE_LIMITED


def test_the_query_sent_to_channel3_is_the_adopted_title(channel3):
    fake = FakeChannel3Client()

    revalidate(channel3.adopted_product_id, client=fake)

    assert fake.calls[0]["query"] == TITLE
