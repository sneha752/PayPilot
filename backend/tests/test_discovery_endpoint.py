"""GET /discovery/search endpoint tests (Phase 10C).

The Channel3 client is faked at the discovery-service factory boundary, so
no real HTTP request or credit spend can occur. PayPal/transaction entry
points are boobytrapped to prove discovery never touches the payment core.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import discovery_service
from app.services.channel3_client import (
    Channel3AuthError,
    Channel3Brand,
    Channel3ConfigError,
    Channel3InvalidResponseError,
    Channel3Offer,
    Channel3Price,
    Channel3Product,
    Channel3QuotaError,
    Channel3RateLimitError,
    Channel3SearchResult,
    Channel3UnavailableError,
)
from app.services.product_service import list_products

client = TestClient(app)

TEST_KEY = "test-channel3-key"


def c3_product():
    return Channel3Product(
        id="prod_1",
        title="Sony WH-1000XM5",
        description="Noise cancelling headphones.",
        brands=[Channel3Brand(id="b1", name="Sony")],
        offers=[
            Channel3Offer(
                url="https://shop.example/p",
                domain="shop.example",
                price=Channel3Price(price=Decimal("19.99"), currency="USD"),
                availability="InStock",
            )
        ],
    )


class FakeClient:
    def __init__(self, result=None, error=None):
        self.result = result or Channel3SearchResult(products=[c3_product()])
        self.error = error
        self.calls = []

    def search(self, query=None, *, limit=20, page_token=None, **kwargs):
        self.calls.append({"query": query, "limit": limit, "page_token": page_token})
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def discovery(monkeypatch):
    """Fake the Channel3 client factory; boobytrap the payment core."""
    fake = FakeClient()
    monkeypatch.setattr(discovery_service, "_make_client", lambda: fake)

    # Any PayPal or ledger touch from a discovery request is a test failure.
    from app import paypal
    from app.services import transaction_service

    def bomb(*args, **kwargs):  # pragma: no cover - should never run
        raise AssertionError("discovery must not touch PayPal or the ledger")

    monkeypatch.setattr(paypal, "get_access_token", bomb)
    monkeypatch.setattr(transaction_service, "create_transaction", bomb)
    return fake


def test_valid_request_returns_200_with_normalized_shape(discovery):
    catalog_before = [p.id for p in list_products()]

    response = client.get("/discovery/search", params={"q": "wireless headphones"})

    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "wireless headphones"
    assert body["next_page_token"] is None
    product = body["products"][0]
    assert product["external_id"] == "prod_1"
    assert product["source"] == "channel3"
    assert product["observed_price"] == "19.99"  # Decimal -> exact string
    assert product["observed_currency"] == "USD"
    assert product["retailer_domain"] == "shop.example"
    assert product["observed_at"]  # server-side timestamp present

    # The raw Channel3 structure is never exposed.
    for leaked in ("offers", "brands", "images", "key_features", "variants"):
        assert leaked not in product

    # No catalog mutation: the local catalog is untouched by discovery.
    assert [p.id for p in list_products()] == catalog_before


def test_query_limit_and_page_token_reach_the_client(discovery):
    response = client.get(
        "/discovery/search",
        params={"q": "laptop", "limit": 7, "page_token": "tok_1"},
    )

    assert response.status_code == 200
    assert discovery.calls == [
        {"query": "laptop", "limit": 7, "page_token": "tok_1"}
    ]


def test_limit_defaults_to_10(discovery):
    client.get("/discovery/search", params={"q": "laptop"})

    assert discovery.calls[0]["limit"] == 10


def test_api_key_never_appears_in_response(discovery, monkeypatch):
    monkeypatch.setenv("CHANNEL3_API_KEY", TEST_KEY)

    response = client.get("/discovery/search", params={"q": "headphones"})

    assert TEST_KEY not in response.text


def test_missing_q_is_rejected(discovery):
    assert client.get("/discovery/search").status_code == 422


def test_blank_q_is_rejected_without_upstream_call(discovery):
    response = client.get("/discovery/search", params={"q": "   "})

    assert response.status_code == 400
    assert discovery.calls == []


@pytest.mark.parametrize("limit", [0, 31, -5])
def test_out_of_range_limit_is_rejected(discovery, limit):
    response = client.get(
        "/discovery/search", params={"q": "laptop", "limit": limit}
    )

    assert response.status_code == 422
    assert discovery.calls == []


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (Channel3ConfigError("CHANNEL3_API_KEY must be set"), 503),
        (Channel3QuotaError("credits exhausted"), 503),
        (Channel3RateLimitError("rate limited"), 429),
        (Channel3UnavailableError("upstream down"), 503),
        (Channel3AuthError("key rejected"), 502),
        (Channel3InvalidResponseError("bad body"), 502),
    ],
)
def test_typed_upstream_failures_map_to_honest_statuses(
    discovery, error, expected_status
):
    discovery.error = error

    response = client.get("/discovery/search", params={"q": "headphones"})

    assert response.status_code == expected_status
    # Never an empty 200 on failure, and the body is a clean detail message.
    assert "detail" in response.json()


def test_empty_result_is_a_normal_200(discovery):
    discovery.result = Channel3SearchResult(products=[])

    response = client.get("/discovery/search", params={"q": "unfindable"})

    assert response.status_code == 200
    assert response.json()["products"] == []


def test_next_page_token_is_passed_through(discovery):
    discovery.result = Channel3SearchResult(
        products=[], next_page_token="tok_next"
    )

    response = client.get("/discovery/search", params={"q": "laptop"})

    assert response.json()["next_page_token"] == "tok_next"
