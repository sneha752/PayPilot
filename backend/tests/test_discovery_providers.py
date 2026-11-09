"""Provider abstraction, selection, and multi-provider endpoint tests
(Phase 10D). No real HTTP anywhere; the payment core is boobytrapped.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import discovery_service
from app.services.channel3_client import (
    Channel3Brand,
    Channel3Offer,
    Channel3Price,
    Channel3Product,
    Channel3RateLimitError,
    Channel3SearchResult,
)
from app.services.discovery_providers import serper as serper_module
from app.services.discovery_providers.base import (
    DiscoveryConfigError,
    DiscoveryProvider,
    DiscoveryRateLimitError,
)
from app.services.discovery_providers.channel3 import Channel3Provider
from app.services.discovery_providers.serper import SerperProvider

client = TestClient(app)

NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)

EXPECTED_PRODUCT_KEYS = {
    "external_id",
    "source",
    "title",
    "description",
    "brand",
    "category",
    "image_url",
    "product_url",
    "retailer_domain",
    "availability",
    "observed_price",
    "observed_currency",
    "observed_at",
}


def c3_product():
    return Channel3Product(
        id="prod_1",
        title="Sony WH-1000XM5",
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


class FakeChannel3Client:
    def __init__(self, result=None, error=None):
        self.result = result or Channel3SearchResult(products=[c3_product()])
        self.error = error
        self.calls = []

    def search(self, query=None, *, limit=20, page_token=None, **kwargs):
        self.calls.append({"query": query, "limit": limit, "page_token": page_token})
        if self.error:
            raise self.error
        return self.result


class SerperFakeResponse:
    status_code = 200

    @staticmethod
    def json():
        return {
            "shopping": [
                {
                    "title": "Sony WH-1000XM5 Wireless Headphones",
                    "source": "Best Buy",
                    "link": "https://www.bestbuy.com/site/sony-xm5",
                    "price": "$299.99",
                    "imageUrl": "https://img.example/xm5.jpg",
                    "productId": "736876",
                }
            ]
        }


@pytest.fixture
def payment_core_boobytrap(monkeypatch):
    """Any PayPal/ledger/policy touch from discovery fails the test."""
    from app import paypal, policy
    from app.services import transaction_service

    def bomb(*args, **kwargs):  # pragma: no cover
        raise AssertionError("discovery must not touch the payment core")

    monkeypatch.setattr(paypal, "get_access_token", bomb)
    monkeypatch.setattr(transaction_service, "create_transaction", bomb)
    monkeypatch.setattr(policy, "evaluate_policy", bomb)


# ---------------------------------------------------------------------------
# Abstraction
# ---------------------------------------------------------------------------


def test_channel3_adapter_satisfies_the_provider_interface():
    provider = Channel3Provider(FakeChannel3Client())
    assert isinstance(provider, DiscoveryProvider)
    assert provider.source == "channel3"


def test_channel3_adapter_preserves_phase_10c_normalization():
    fake = FakeChannel3Client()
    provider = Channel3Provider(fake)

    result = provider.search("headphones", limit=5, observed_at=NOW)

    assert fake.calls == [{"query": "headphones", "limit": 5, "page_token": None}]
    product = result.products[0]
    assert product.source == "channel3"
    assert product.external_id == "prod_1"
    assert product.brand == "Sony"
    assert product.observed_price == Decimal("19.99")
    assert product.observed_at == NOW


def test_channel3_errors_are_provider_neutral_discovery_errors():
    fake = FakeChannel3Client(error=Channel3RateLimitError("rl"))
    provider = Channel3Provider(fake)

    with pytest.raises(DiscoveryRateLimitError):
        provider.search("headphones", limit=5, observed_at=NOW)


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------


def test_env_selects_channel3(monkeypatch):
    monkeypatch.setenv("DISCOVERY_PROVIDER", "channel3")
    monkeypatch.setattr(
        discovery_service, "_make_client", lambda: FakeChannel3Client()
    )

    assert isinstance(discovery_service._select_provider(), Channel3Provider)


def test_default_is_channel3(monkeypatch):
    monkeypatch.delenv("DISCOVERY_PROVIDER", raising=False)
    monkeypatch.setattr(
        discovery_service, "_make_client", lambda: FakeChannel3Client()
    )

    assert isinstance(discovery_service._select_provider(), Channel3Provider)


def test_env_selects_serper_without_requiring_channel3_key(monkeypatch):
    monkeypatch.setenv("DISCOVERY_PROVIDER", "serper")
    monkeypatch.setenv("SERPER_API_KEY", "test-serper-key")
    monkeypatch.delenv("CHANNEL3_API_KEY", raising=False)

    assert isinstance(discovery_service._select_provider(), SerperProvider)


def test_channel3_selection_does_not_require_serper_key(monkeypatch):
    monkeypatch.setenv("DISCOVERY_PROVIDER", "channel3")
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    monkeypatch.setattr(
        discovery_service, "_make_client", lambda: FakeChannel3Client()
    )

    assert isinstance(discovery_service._select_provider(), Channel3Provider)


def test_invalid_provider_name_fails_clearly(monkeypatch):
    monkeypatch.setenv("DISCOVERY_PROVIDER", "amazon")

    with pytest.raises(DiscoveryConfigError) as excinfo:
        discovery_service._select_provider()

    assert "amazon" in str(excinfo.value)
    assert "channel3" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Endpoint, per provider — identical normalized shape either way
# ---------------------------------------------------------------------------


def _get_products(params):
    response = client.get("/discovery/search", params=params)
    assert response.status_code == 200
    return response.json()


def test_endpoint_with_channel3_provider(
    monkeypatch, payment_core_boobytrap
):
    monkeypatch.setenv("DISCOVERY_PROVIDER", "channel3")
    monkeypatch.setattr(
        discovery_service, "_make_client", lambda: FakeChannel3Client()
    )

    body = _get_products({"q": "headphones"})

    assert body["products"][0]["source"] == "channel3"
    assert set(body["products"][0].keys()) == EXPECTED_PRODUCT_KEYS


def test_endpoint_with_serper_provider(monkeypatch, payment_core_boobytrap):
    monkeypatch.setenv("DISCOVERY_PROVIDER", "serper")
    monkeypatch.setenv("SERPER_API_KEY", "test-serper-key")
    monkeypatch.setattr(
        serper_module.httpx, "post", lambda *a, **k: SerperFakeResponse()
    )

    body = _get_products({"q": "headphones"})

    product = body["products"][0]
    assert product["source"] == "serper"
    assert product["external_id"] == "serper:736876"
    assert product["observed_price"] == "299.99"
    assert product["observed_currency"] == "USD"
    assert body["next_page_token"] is None


def test_normalized_shape_is_identical_across_providers(
    monkeypatch, payment_core_boobytrap
):
    monkeypatch.setenv("DISCOVERY_PROVIDER", "channel3")
    monkeypatch.setattr(
        discovery_service, "_make_client", lambda: FakeChannel3Client()
    )
    channel3_keys = set(_get_products({"q": "x"})["products"][0].keys())

    monkeypatch.setenv("DISCOVERY_PROVIDER", "serper")
    monkeypatch.setenv("SERPER_API_KEY", "test-serper-key")
    monkeypatch.setattr(
        serper_module.httpx, "post", lambda *a, **k: SerperFakeResponse()
    )
    serper_keys = set(_get_products({"q": "x"})["products"][0].keys())

    assert channel3_keys == serper_keys == EXPECTED_PRODUCT_KEYS


def test_no_raw_serper_fields_leak_into_the_response(
    monkeypatch, payment_core_boobytrap
):
    monkeypatch.setenv("DISCOVERY_PROVIDER", "serper")
    monkeypatch.setenv("SERPER_API_KEY", "test-serper-key")
    monkeypatch.setattr(
        serper_module.httpx, "post", lambda *a, **k: SerperFakeResponse()
    )

    body = _get_products({"q": "headphones"})

    text = str(body)
    for leaked in ("imageUrl", "productId", "position", "ratingCount", "shopping"):
        assert leaked not in body["products"][0]
    assert "test-serper-key" not in text


def test_serper_page_token_is_rejected_with_400(
    monkeypatch, payment_core_boobytrap
):
    monkeypatch.setenv("DISCOVERY_PROVIDER", "serper")
    monkeypatch.setenv("SERPER_API_KEY", "test-serper-key")
    monkeypatch.setattr(
        serper_module.httpx, "post", lambda *a, **k: SerperFakeResponse()
    )

    response = client.get(
        "/discovery/search", params={"q": "x", "page_token": "tok"}
    )

    assert response.status_code == 400


def test_missing_serper_key_maps_to_503(monkeypatch, payment_core_boobytrap):
    monkeypatch.setenv("DISCOVERY_PROVIDER", "serper")
    monkeypatch.delenv("SERPER_API_KEY", raising=False)

    response = client.get("/discovery/search", params={"q": "x"})

    assert response.status_code == 503


def test_invalid_provider_config_maps_to_503(
    monkeypatch, payment_core_boobytrap
):
    monkeypatch.setenv("DISCOVERY_PROVIDER", "not-a-provider")

    response = client.get("/discovery/search", params={"q": "x"})

    assert response.status_code == 503


def test_catalog_is_not_mutated_by_either_provider(
    monkeypatch, payment_core_boobytrap
):
    from app.services.product_service import list_products

    before = [p.id for p in list_products()]

    monkeypatch.setenv("DISCOVERY_PROVIDER", "serper")
    monkeypatch.setenv("SERPER_API_KEY", "test-serper-key")
    monkeypatch.setattr(
        serper_module.httpx, "post", lambda *a, **k: SerperFakeResponse()
    )
    _get_products({"q": "headphones"})

    assert [p.id for p in list_products()] == before


# ---------------------------------------------------------------------------
# Merchant evidence travels beside the products, never inside them (10H)
# ---------------------------------------------------------------------------


def test_product_evidence_contract_is_still_exactly_thirteen_fields():
    """Phase 10H adds merchant identity WITHOUT widening this contract."""
    from app.schemas.discovery import DiscoveredProduct

    assert set(DiscoveredProduct.model_fields) == EXPECTED_PRODUCT_KEYS
    assert "merchant_name" not in DiscoveredProduct.model_fields
    assert "merchant_domain" not in DiscoveredProduct.model_fields


def test_search_endpoint_exposes_no_merchant_fields(
    monkeypatch, payment_core_boobytrap
):
    monkeypatch.setenv("DISCOVERY_PROVIDER", "serper")
    monkeypatch.setenv("SERPER_API_KEY", "test-serper-key")
    monkeypatch.setattr(
        serper_module.httpx, "post", lambda *a, **k: SerperFakeResponse()
    )

    body = _get_products({"q": "headphones"})

    assert set(body["products"][0].keys()) == EXPECTED_PRODUCT_KEYS
    assert set(body.keys()) == {"query", "products", "next_page_token"}


def test_serper_supplies_merchant_evidence_and_channel3_does_not(monkeypatch):
    """Each provider reports only what its own contract actually carries."""
    observed_at = datetime(2026, 10, 7, tzinfo=timezone.utc)

    monkeypatch.setenv("SERPER_API_KEY", "test-serper-key")
    monkeypatch.setattr(
        serper_module.httpx, "post", lambda *a, **k: SerperFakeResponse()
    )
    serper_result = SerperProvider().search("x", limit=5, observed_at=observed_at)

    channel3_result = Channel3Provider(FakeChannel3Client()).search(
        "x", limit=5, observed_at=observed_at
    )

    # Serper names the shop in `source`; Channel3 has no such field.
    assert [e.merchant_name for e in serper_result.merchant_evidence] == ["Best Buy"]
    assert channel3_result.merchant_evidence == []
    # Both still report the provider in `source`, not a shop.
    assert serper_result.products[0].source == "serper"
    assert channel3_result.products[0].source == "channel3"


def test_merchant_evidence_is_dropped_by_the_plain_search_entry_point(
    monkeypatch,
):
    """search_discovery() keeps its exact Phase 10C/10D return type."""
    monkeypatch.setenv("DISCOVERY_PROVIDER", "serper")
    monkeypatch.setenv("SERPER_API_KEY", "test-serper-key")
    monkeypatch.setattr(
        serper_module.httpx, "post", lambda *a, **k: SerperFakeResponse()
    )

    response = discovery_service.search_discovery("headphones", limit=5)
    outcome = discovery_service.search_discovery_with_evidence("headphones", limit=5)

    assert not hasattr(response, "merchant_evidence")
    assert isinstance(outcome.response, type(response))
    assert [e.merchant_name for e in outcome.merchant_evidence] == ["Best Buy"]
