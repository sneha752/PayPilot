"""Discovery normalization tests (Phase 10C).

The Channel3 client is faked at the service boundary — no HTTP, no
credits. Everything asserted here is deterministic normalization.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.schemas.discovery import DiscoveredProduct
from app.services import discovery_service
from app.services.channel3_client import (
    Channel3Brand,
    Channel3Category,
    Channel3Image,
    Channel3Offer,
    Channel3Price,
    Channel3Product,
    Channel3RateLimitError,
    Channel3SearchResult,
    Channel3UnavailableError,
)
from app.services.discovery_service import (
    _select_offer,
    normalize_product,
    search_discovery,
)

NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)


def offer(
    url="https://shop.example/p",
    domain="shop.example",
    price="19.99",
    currency="USD",
    availability="InStock",
    compare_at=None,
):
    return Channel3Offer(
        url=url,
        domain=domain,
        price=Channel3Price(
            price=Decimal(price),
            compare_at_price=Decimal(compare_at) if compare_at else None,
            currency=currency,
        ),
        availability=availability,
    )


def c3_product(**overrides):
    base = dict(
        id="prod_1",
        title="Sony WH-1000XM5",
        description="Noise cancelling headphones.",
        brands=[Channel3Brand(id="b1", name="Sony")],
        images=[
            Channel3Image(url="https://img.example/extra.jpg"),
            Channel3Image(url="https://img.example/main.jpg", is_main_image=True),
        ],
        category=Channel3Category(slug="headphones", title="Headphones"),
        offers=[offer()],
    )
    base.update(overrides)
    return Channel3Product(**base)


class FakeClient:
    def __init__(self, result=None, error=None):
        self.result = result or Channel3SearchResult(products=[])
        self.error = error
        self.calls = []

    def search(self, query=None, *, limit=20, page_token=None, **kwargs):
        self.calls.append({"query": query, "limit": limit, "page_token": page_token})
        if self.error:
            raise self.error
        return self.result


# ---------------------------------------------------------------------------
# Field mapping
# ---------------------------------------------------------------------------


def test_channel3_product_normalizes_all_fields():
    normalized = normalize_product(c3_product(), NOW)

    assert isinstance(normalized, DiscoveredProduct)
    assert normalized.external_id == "prod_1"
    assert normalized.source == "channel3"
    assert normalized.title == "Sony WH-1000XM5"
    assert normalized.description == "Noise cancelling headphones."
    assert normalized.brand == "Sony"
    assert normalized.category == "headphones"  # slug preferred over title
    assert normalized.image_url == "https://img.example/main.jpg"  # main image
    assert normalized.product_url == "https://shop.example/p"
    assert normalized.retailer_domain == "shop.example"
    assert normalized.availability == "InStock"
    assert normalized.observed_price == Decimal("19.99")
    assert normalized.observed_currency == "USD"
    assert normalized.observed_at == NOW


def test_decimal_price_is_preserved_exactly():
    normalized = normalize_product(
        c3_product(offers=[offer(price="1499.90")]), NOW
    )

    assert isinstance(normalized.observed_price, Decimal)
    assert normalized.observed_price == Decimal("1499.90")
    assert str(normalized.observed_price) == "1499.90"


def test_missing_optional_fields_become_none():
    bare = Channel3Product(id="p9", title="Mystery")

    normalized = normalize_product(bare, NOW)

    assert normalized.description is None
    assert normalized.brand is None
    assert normalized.category is None
    assert normalized.image_url is None
    assert normalized.product_url is None
    assert normalized.retailer_domain is None
    assert normalized.availability is None
    assert normalized.observed_price is None
    assert normalized.observed_currency is None


def test_multiple_brands_pick_first_non_empty_trimmed():
    product = c3_product(
        brands=[
            Channel3Brand(id="b0", name="   "),
            Channel3Brand(id="b1", name="  Sony  "),
            Channel3Brand(id="b2", name="Bose"),
        ]
    )

    assert normalize_product(product, NOW).brand == "Sony"


# ---------------------------------------------------------------------------
# Deterministic offer selection
# ---------------------------------------------------------------------------


def test_lowest_valid_available_offer_wins():
    product = c3_product(
        offers=[
            offer(url="https://a.example/p", domain="a.example", price="25.00"),
            offer(url="https://b.example/p", domain="b.example", price="19.50"),
            offer(url="https://c.example/p", domain="c.example", price="22.00"),
        ]
    )

    normalized = normalize_product(product, NOW)

    assert normalized.observed_price == Decimal("19.50")
    assert normalized.retailer_domain == "b.example"


def test_in_stock_preferred_over_cheaper_out_of_stock():
    product = c3_product(
        offers=[
            offer(domain="cheap.example", url="https://cheap.example/p",
                  price="10.00", availability="OutOfStock"),
            offer(domain="stocked.example", url="https://stocked.example/p",
                  price="15.00", availability="InStock"),
        ]
    )

    normalized = normalize_product(product, NOW)

    assert normalized.retailer_domain == "stocked.example"
    assert normalized.observed_price == Decimal("15.00")


def test_invalid_offers_are_ignored():
    product = c3_product(
        offers=[
            offer(url="   ", domain="blank-url.example", price="1.00"),
            Channel3Offer(url="https://noprice.example/p", domain="noprice.example"),
            offer(url="https://zero.example/p", domain="zero.example", price="0"),
            offer(url="https://neg.example/p", domain="neg.example", price="-5"),
            offer(url="https://ok.example/p", domain="ok.example", price="42.00"),
        ]
    )

    normalized = normalize_product(product, NOW)

    assert normalized.retailer_domain == "ok.example"
    assert normalized.observed_price == Decimal("42.00")


def test_price_tie_breaks_by_domain_then_url():
    tied = [
        offer(url="https://z.example/p", domain="z.example", price="20.00"),
        offer(url="https://a.example/p2", domain="a.example", price="20.00"),
        offer(url="https://a.example/p1", domain="a.example", price="20.00"),
    ]

    selected = _select_offer(tied)

    assert selected.domain == "a.example"
    assert selected.url == "https://a.example/p1"


def test_no_valid_offer_yields_null_price_and_retailer_fields():
    product = c3_product(
        offers=[Channel3Offer(url="https://noprice.example/p")]
    )

    normalized = normalize_product(product, NOW)

    assert normalized.title == "Sony WH-1000XM5"  # product still returned
    assert normalized.product_url is None
    assert normalized.retailer_domain is None
    assert normalized.availability is None
    assert normalized.observed_price is None
    assert normalized.observed_currency is None


# ---------------------------------------------------------------------------
# search_discovery (service orchestration)
# ---------------------------------------------------------------------------


def test_search_passes_arguments_to_client_and_preserves_token():
    fake = FakeClient(
        Channel3SearchResult(
            products=[c3_product()], next_page_token="tok_next"
        )
    )

    response = search_discovery(
        "  wireless headphones  ", limit=5, page_token="tok_prev", client=fake
    )

    assert fake.calls == [
        {"query": "wireless headphones", "limit": 5, "page_token": "tok_prev"}
    ]
    assert response.query == "wireless headphones"
    assert response.next_page_token == "tok_next"
    assert len(response.products) == 1


def test_observed_at_is_server_generated_utc():
    fake = FakeClient(Channel3SearchResult(products=[c3_product()]))

    before = datetime.now(timezone.utc)
    response = search_discovery("headphones", client=fake)
    after = datetime.now(timezone.utc)

    observed_at = response.products[0].observed_at
    assert observed_at.tzinfo is not None
    assert observed_at.utcoffset().total_seconds() == 0
    assert before <= observed_at <= after


def test_empty_channel3_result_returns_empty_products():
    fake = FakeClient(Channel3SearchResult(products=[]))

    response = search_discovery("nothing findable", client=fake)

    assert response.products == []
    assert response.next_page_token is None


def test_blank_query_rejected_before_any_upstream_call():
    fake = FakeClient()

    with pytest.raises(ValueError):
        search_discovery("   ", client=fake)

    assert fake.calls == []


@pytest.mark.parametrize(
    "error", [Channel3RateLimitError("rl"), Channel3UnavailableError("down")]
)
def test_channel3_exceptions_propagate(error):
    fake = FakeClient(error=error)

    with pytest.raises(type(error)):
        search_discovery("headphones", client=fake)


def test_discovery_module_has_no_payment_or_policy_imports():
    # Static guarantee: the discovery layer cannot reach PayPal, policy,
    # currency conversion, the catalog, or transactions.
    import inspect

    source = inspect.getsource(discovery_service)
    for forbidden in (
        "paypal",
        "policy",
        "currency_service",
        "transaction_service",
        "product_service",
        "session_service",
    ):
        assert forbidden not in source, f"discovery must not import {forbidden}"
