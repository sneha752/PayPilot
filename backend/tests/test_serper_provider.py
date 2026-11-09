"""Serper discovery provider tests (Phase 10D).

httpx.post is monkeypatched inside the serper module — no real Serper
request (and no credit spend) ever happens here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from app.services.discovery_providers import serper as serper_module
from app.services.discovery_providers.base import (
    DiscoveryAuthError,
    DiscoveryConfigError,
    DiscoveryError,
    DiscoveryInvalidResponseError,
    DiscoveryProvider,
    DiscoveryQuotaError,
    DiscoveryRateLimitError,
    DiscoveryRequestError,
    DiscoveryUnavailableError,
)
from app.services.discovery_providers.serper import SerperProvider

NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)
TEST_KEY = "test-serper-key"
SHOPPING_URL = "https://google.serper.dev/shopping"


def shopping_item(**overrides):
    item = {
        "title": "Sony WH-1000XM5 Wireless Headphones",
        "source": "Best Buy",
        "link": "https://www.bestbuy.com/site/sony-wh1000xm5",
        "price": "$299.99",
        "imageUrl": "https://img.example/xm5.jpg",
        "productId": "7368763948836577026",
        "position": 1,
    }
    item.update(overrides)
    return item


class FakeResponse:
    def __init__(self, status_code: int, body=None, malformed: bool = False):
        self.status_code = status_code
        self._body = body if body is not None else {"shopping": []}
        self._malformed = malformed

    def json(self):
        if self._malformed:
            raise ValueError("not json")
        return self._body


@pytest.fixture
def serper(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", TEST_KEY)

    calls: list[dict] = []
    responses: list = [FakeResponse(200, {"shopping": [shopping_item()]})]

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(
            {"url": url, "json": json, "headers": headers, "timeout": timeout}
        )
        result = responses[0]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(serper_module.httpx, "post", fake_post)

    class Ctx:
        provider = SerperProvider()

        @staticmethod
        def set_response(r):
            responses[0] = r

        @staticmethod
        def call_count():
            return len(calls)

        @staticmethod
        def last_call():
            return calls[-1]

    return Ctx


def search(ctx, **kwargs):
    kwargs.setdefault("limit", 10)
    kwargs.setdefault("observed_at", NOW)
    return ctx.provider.search("wireless headphones", **kwargs)


# ---------------------------------------------------------------------------
# Interface + request contract
# ---------------------------------------------------------------------------


def test_serper_provider_satisfies_the_provider_interface(serper):
    assert isinstance(serper.provider, DiscoveryProvider)
    assert serper.provider.source == "serper"


def test_request_method_endpoint_payload_and_header(serper):
    search(serper, limit=5)

    call = serper.last_call()
    assert call["url"] == SHOPPING_URL  # POST via httpx.post
    assert call["json"] == {
        "q": "wireless headphones",
        "gl": "us",
        "hl": "en",
        "num": 5,
    }
    assert call["headers"]["X-API-KEY"] == TEST_KEY
    assert call["timeout"] == 15.0
    # Key never in the URL or body.
    assert TEST_KEY not in call["url"]
    assert TEST_KEY not in str(call["json"])


def test_missing_api_key_fails_clearly_with_no_http_request(monkeypatch):
    monkeypatch.delenv("SERPER_API_KEY", raising=False)

    def bomb(*a, **k):  # pragma: no cover
        raise AssertionError("no HTTP request may happen without a key")

    monkeypatch.setattr(serper_module.httpx, "post", bomb)

    with pytest.raises(DiscoveryConfigError) as excinfo:
        SerperProvider()

    assert "SERPER_API_KEY" in str(excinfo.value)


def test_api_key_never_appears_in_logs_or_errors(serper, caplog):
    serper.set_response(FakeResponse(500))

    with caplog.at_level(logging.DEBUG, logger="paypilot.discovery.serper"):
        with pytest.raises(DiscoveryUnavailableError) as excinfo:
            search(serper)

    assert TEST_KEY not in str(excinfo.value)
    for record in caplog.records:
        assert TEST_KEY not in record.getMessage()


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def test_successful_response_maps_all_available_fields(serper):
    result = search(serper)

    assert result.next_page_token is None  # documented: no token pagination
    product = result.products[0]
    assert product.external_id == "serper:7368763948836577026"
    assert product.source == "serper"
    assert product.title == "Sony WH-1000XM5 Wireless Headphones"
    assert product.product_url == "https://www.bestbuy.com/site/sony-wh1000xm5"
    assert product.retailer_domain == "www.bestbuy.com"  # from the link
    assert product.image_url == "https://img.example/xm5.jpg"
    assert isinstance(product.observed_price, Decimal)
    assert product.observed_price == Decimal("299.99")
    assert product.observed_currency == "USD"
    assert product.observed_at == NOW
    # Not provided by Serper shopping -> null, never invented/inferred.
    assert product.description is None
    assert product.brand is None
    assert product.category is None
    assert product.availability is None


def test_multiple_results_map_in_order_and_respect_limit(serper):
    items = [
        shopping_item(productId=str(i), title=f"Item {i}", price=f"${i}.00")
        for i in range(1, 6)
    ]
    serper.set_response(FakeResponse(200, {"shopping": items}))

    result = search(serper, limit=3)

    assert [p.external_id for p in result.products] == [
        "serper:1",
        "serper:2",
        "serper:3",
    ]


def test_missing_optional_fields_become_none(serper):
    serper.set_response(
        FakeResponse(
            200,
            {
                "shopping": [
                    {
                        "title": "Mystery Item",
                        "link": "https://shop.example/mystery",
                    }
                ]
            },
        )
    )

    product = search(serper).products[0]

    assert product.image_url is None
    assert product.observed_price is None
    assert product.observed_currency is None
    assert product.retailer_domain == "shop.example"


def test_prices_are_exact_decimals_with_thousands_separators(serper):
    serper.set_response(
        FakeResponse(
            200, {"shopping": [shopping_item(price="$1,299.99")]}
        )
    )

    price = search(serper).products[0].observed_price

    assert isinstance(price, Decimal)
    assert price == Decimal("1299.99")


@pytest.mark.parametrize("raw", ["₹1,499", "From $20", "free", "", None, 299.99])
def test_unparseable_price_yields_null_price_and_currency(serper, raw):
    serper.set_response(
        FakeResponse(200, {"shopping": [shopping_item(price=raw)]})
    )

    product = search(serper).products[0]

    assert product.observed_price is None
    assert product.observed_currency is None  # never guessed


def test_external_id_falls_back_to_stable_link_hash(serper):
    item = shopping_item()
    del item["productId"]
    serper.set_response(FakeResponse(200, {"shopping": [item]}))

    first = search(serper).products[0].external_id
    second = search(serper).products[0].external_id

    assert first.startswith("serper:url:")
    assert first == second  # deterministic across calls
    assert len(first) == len("serper:url:") + 16


def test_result_without_id_or_link_is_skipped(serper):
    serper.set_response(
        FakeResponse(
            200,
            {"shopping": [{"title": "No identity"}, shopping_item()]},
        )
    )

    result = search(serper)

    assert [p.external_id for p in result.products] == [
        "serper:7368763948836577026"
    ]


def test_missing_shopping_key_means_empty_results(serper):
    serper.set_response(FakeResponse(200, {"searchParameters": {}}))

    assert search(serper).products == []


# ---------------------------------------------------------------------------
# Errors — never a fake empty success
# ---------------------------------------------------------------------------


def test_malformed_json_raises_invalid_response(serper):
    serper.set_response(FakeResponse(200, malformed=True))

    with pytest.raises(DiscoveryInvalidResponseError):
        search(serper)


def test_wrong_schema_raises_invalid_response(serper):
    serper.set_response(FakeResponse(200, {"shopping": "nope"}))

    with pytest.raises(DiscoveryInvalidResponseError):
        search(serper)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, DiscoveryAuthError),
        (403, DiscoveryAuthError),
        (402, DiscoveryQuotaError),
        (429, DiscoveryRateLimitError),
        (400, DiscoveryRequestError),
        (500, DiscoveryUnavailableError),
        (502, DiscoveryUnavailableError),
        (503, DiscoveryUnavailableError),
        (504, DiscoveryUnavailableError),
    ],
)
def test_upstream_statuses_map_to_typed_errors(serper, status, expected):
    serper.set_response(FakeResponse(status))

    with pytest.raises(expected) as excinfo:
        search(serper)

    assert isinstance(excinfo.value, DiscoveryError)


def test_network_timeout_maps_to_unavailable(serper):
    serper.set_response(httpx.ConnectTimeout("timed out"))

    with pytest.raises(DiscoveryUnavailableError) as excinfo:
        search(serper)

    assert excinfo.value.retryable


def test_page_token_is_rejected_not_faked(serper):
    with pytest.raises(ValueError):
        search(serper, page_token="tok_1")

    assert serper.call_count() == 0  # rejected before any HTTP request


# ---------------------------------------------------------------------------
# Merchant evidence (Phase 10H)
# ---------------------------------------------------------------------------


def test_merchant_display_name_is_reported_as_evidence(serper):
    """Serper's `source` is the SELLER's name; it must not be discarded."""
    result = search(serper)

    assert [
        (e.external_id, e.merchant_name) for e in result.merchant_evidence
    ] == [("serper:7368763948836577026", "Best Buy")]


def test_merchant_name_is_not_written_into_source_or_retailer_domain(serper):
    """`source` stays provider provenance; the domain stays the link's host."""
    product = search(serper).products[0]

    assert product.source == "serper"
    assert product.retailer_domain == "www.bestbuy.com"
    assert "Best Buy" not in (product.retailer_domain or "")


def test_google_shopping_link_still_reports_its_own_host_verbatim(serper):
    """retailer_domain is observed evidence: it is never cleaned up here."""
    serper.set_response(
        FakeResponse(
            200,
            {
                "shopping": [
                    shopping_item(
                        source="Amazon",
                        link="https://www.google.com/shopping/product/1",
                    )
                ]
            },
        )
    )

    result = search(serper)

    assert result.products[0].retailer_domain == "www.google.com"
    assert result.merchant_evidence[0].merchant_name == "Amazon"


def test_missing_merchant_name_yields_no_evidence_record(serper):
    serper.set_response(
        FakeResponse(
            200,
            {"shopping": [{"title": "Mystery Item", "link": "https://shop.example/m"}]},
        )
    )

    result = search(serper)

    assert len(result.products) == 1
    assert result.merchant_evidence == []


def test_skipped_results_contribute_no_merchant_evidence(serper):
    """An unusable result must not leave an orphan merchant claim behind."""
    serper.set_response(
        FakeResponse(
            200,
            {
                "shopping": [
                    {"source": "Amazon"},  # no title, no id -> skipped
                    shopping_item(productId="2", source="Walmart"),
                ]
            },
        )
    )

    result = search(serper)

    assert [p.external_id for p in result.products] == ["serper:2"]
    assert [e.external_id for e in result.merchant_evidence] == ["serper:2"]


def test_merchant_evidence_requires_no_additional_request(serper):
    search(serper)

    assert serper.call_count() == 1
