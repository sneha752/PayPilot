"""Channel3 client tests (Phase 10B).

httpx.post is monkeypatched inside the channel3_client module, so NO real
Channel3 request (and no credit spend) ever happens here. The backoff sleep
is neutralized the same way, so retry tests run instantly.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import httpx
import pytest

from app.services import channel3_client
from app.services.channel3_client import (
    Channel3AuthError,
    Channel3Client,
    Channel3Error,
    Channel3InvalidResponseError,
    Channel3QuotaError,
    Channel3RateLimitError,
    Channel3RequestError,
    Channel3UnavailableError,
)

API_URL = "https://api.trychannel3.com/v1/search"
TEST_KEY = "test-channel3-key"


class FakeResponse:
    def __init__(self, status_code: int, body=None, malformed: bool = False):
        self.status_code = status_code
        self._body = body if body is not None else {"products": []}
        self._malformed = malformed

    def json(self):
        if self._malformed:
            raise ValueError("not json")
        return self._body


def product_body(**overrides):
    """A realistic documented product record."""
    product = {
        "id": "prod_123",
        "title": "Sony WH-1000XM5",
        "description": "Wireless noise cancelling headphones.",
        "brands": [{"id": "brand_sony", "name": "Sony"}],
        "images": [{"url": "https://img.example/x.jpg", "is_main_image": True}],
        "category": {"slug": "headphones", "title": "Headphones"},
        "offers": [
            {
                "url": "https://shop.example/sony-xm5",
                "domain": "shop.example",
                "price": {
                    "price": 19.99,
                    "compare_at_price": 24.99,
                    "currency": "USD",
                },
                "availability": "InStock",
                "condition": "new",
            }
        ],
    }
    product.update(overrides)
    return product


@pytest.fixture
def c3(monkeypatch):
    """Configured client + recorded HTTP calls; default response is empty OK."""
    monkeypatch.setenv("CHANNEL3_API_KEY", TEST_KEY)
    monkeypatch.setattr(channel3_client, "_sleep", lambda s: None)

    calls: list[dict] = []
    responses: list = [FakeResponse(200, {"products": []})]

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(
            {"url": url, "json": json, "headers": headers, "timeout": timeout}
        )
        result = responses.pop(0) if len(responses) > 1 else responses[0]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(channel3_client.httpx, "post", fake_post)

    class Ctx:
        client = Channel3Client()

        @staticmethod
        def set_responses(*rs):
            responses.clear()
            responses.extend(rs)

        @staticmethod
        def call_count():
            return len(calls)

        @staticmethod
        def last_call():
            return calls[-1]

    return Ctx


# ---------------------------------------------------------------------------
# Success path + request contract
# ---------------------------------------------------------------------------


def test_successful_search_parses_products(c3):
    c3.set_responses(
        FakeResponse(200, {"products": [product_body()], "next_page_token": None})
    )

    result = c3.client.search("wireless headphones", limit=5)

    assert len(result.products) == 1
    product = result.products[0]
    assert product.id == "prod_123"
    assert product.title == "Sony WH-1000XM5"
    assert product.brands[0].name == "Sony"
    assert product.images[0].is_main_image is True
    assert product.category.slug == "headphones"
    assert product.offers[0].domain == "shop.example"
    assert product.offers[0].availability == "InStock"
    assert result.next_page_token is None


def test_request_payload_is_exactly_the_documented_contract(c3):
    c3.client.search("wireless headphones", limit=5)

    call = c3.last_call()
    assert call["url"] == API_URL
    assert call["json"] == {"query": "wireless headphones", "limit": 5}
    assert call["timeout"] == 15.0  # explicit default timeout, never unbounded


def test_api_key_sent_in_x_api_key_header_only(c3):
    c3.client.search("headphones")

    call = c3.last_call()
    assert call["headers"]["x-api-key"] == TEST_KEY
    # Never in the URL or the body.
    assert TEST_KEY not in call["url"]
    assert TEST_KEY not in str(call["json"])


def test_api_key_never_appears_in_logs_or_errors(c3, caplog):
    c3.set_responses(FakeResponse(503))

    with caplog.at_level(logging.DEBUG, logger="paypilot.channel3"):
        with pytest.raises(Channel3UnavailableError) as excinfo:
            c3.client.search("headphones")

    assert TEST_KEY not in str(excinfo.value)
    for record in caplog.records:
        assert TEST_KEY not in record.getMessage()


def test_missing_api_key_is_a_clear_config_error(monkeypatch):
    monkeypatch.delenv("CHANNEL3_API_KEY", raising=False)

    with pytest.raises(channel3_client.Channel3ConfigError) as excinfo:
        Channel3Client()

    assert "CHANNEL3_API_KEY" in str(excinfo.value)
    assert not excinfo.value.retryable


def test_invalid_arguments_rejected_before_any_http_call(c3):
    with pytest.raises(ValueError):
        c3.client.search("")  # no query, no page_token
    with pytest.raises(ValueError):
        c3.client.search("x", limit=0)
    with pytest.raises(ValueError):
        c3.client.search("x", limit=31)
    assert c3.call_count() == 0


# ---------------------------------------------------------------------------
# Error mapping: non-retryable statuses never retry
# ---------------------------------------------------------------------------


def test_400_is_request_error_without_retry(c3):
    c3.set_responses(FakeResponse(400))

    with pytest.raises(Channel3RequestError) as excinfo:
        c3.client.search("headphones")

    assert c3.call_count() == 1
    assert not excinfo.value.retryable


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_do_not_retry(c3, status):
    c3.set_responses(FakeResponse(status))

    with pytest.raises(Channel3AuthError) as excinfo:
        c3.client.search("headphones")

    assert c3.call_count() == 1
    assert not excinfo.value.retryable


def test_402_quota_exhausted_does_not_retry(c3):
    c3.set_responses(FakeResponse(402))

    with pytest.raises(Channel3QuotaError):
        c3.client.search("headphones")

    assert c3.call_count() == 1


def test_422_validation_is_request_error(c3):
    c3.set_responses(FakeResponse(422))

    with pytest.raises(Channel3RequestError):
        c3.client.search("headphones")

    assert c3.call_count() == 1


# ---------------------------------------------------------------------------
# Transient failures: bounded retries with exact attempt counts
# ---------------------------------------------------------------------------


def test_429_retries_then_raises_rate_limit(c3):
    c3.set_responses(FakeResponse(429))

    with pytest.raises(Channel3RateLimitError) as excinfo:
        c3.client.search("headphones")

    assert c3.call_count() == 3  # exactly _MAX_ATTEMPTS
    assert excinfo.value.retryable


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_retries_then_raises_unavailable(c3, status):
    c3.set_responses(FakeResponse(status))

    with pytest.raises(Channel3UnavailableError) as excinfo:
        c3.client.search("headphones")

    assert c3.call_count() == 3
    assert excinfo.value.retryable


def test_transient_failure_then_success_retries_and_succeeds(c3):
    c3.set_responses(
        FakeResponse(503),
        FakeResponse(200, {"products": [product_body()]}),
    )

    result = c3.client.search("headphones")

    assert c3.call_count() == 2  # one retry, then success — no extra attempts
    assert len(result.products) == 1


def test_network_timeout_retries_then_raises_unavailable(c3):
    c3.set_responses(httpx.ConnectTimeout("connect timed out"))

    with pytest.raises(Channel3UnavailableError) as excinfo:
        c3.client.search("headphones")

    assert c3.call_count() == 3
    assert excinfo.value.retryable


# ---------------------------------------------------------------------------
# Response validation
# ---------------------------------------------------------------------------


def test_malformed_json_raises_invalid_response_without_retry(c3):
    c3.set_responses(FakeResponse(200, malformed=True))

    with pytest.raises(Channel3InvalidResponseError):
        c3.client.search("headphones")

    assert c3.call_count() == 1


def test_wrong_schema_raises_invalid_response(c3):
    c3.set_responses(FakeResponse(200, {"products": "not-a-list"}))

    with pytest.raises(Channel3InvalidResponseError):
        c3.client.search("headphones")


def test_missing_optional_fields_do_not_crash(c3):
    # Bare-minimum documented product: id + title only.
    c3.set_responses(
        FakeResponse(200, {"products": [{"id": "p1", "title": "Mystery Item"}]})
    )

    result = c3.client.search("anything")

    product = result.products[0]
    assert product.description is None
    assert product.brands == []
    assert product.images == []
    assert product.category is None
    assert product.offers == []


def test_invalid_product_record_is_skipped_not_fatal(c3):
    c3.set_responses(
        FakeResponse(
            200,
            {"products": [{"title": "no id"}, product_body(id="p2")]},
        )
    )

    result = c3.client.search("headphones")

    assert [p.id for p in result.products] == ["p2"]


def test_multiple_offers_preserved_in_order(c3):
    offers = [
        {
            "url": "https://a.example/p",
            "domain": "a.example",
            "price": {"price": 101.5, "currency": "USD"},
            "availability": "InStock",
        },
        {
            "url": "https://b.example/p",
            "domain": "b.example",
            "price": {"price": 99.99, "currency": "EUR"},
            "availability": "OutOfStock",
        },
    ]
    c3.set_responses(
        FakeResponse(200, {"products": [product_body(offers=offers)]})
    )

    result = c3.client.search("headphones")

    got = result.products[0].offers
    assert [o.domain for o in got] == ["a.example", "b.example"]
    assert got[1].price.currency == "EUR"


def test_observed_prices_are_exact_decimals(c3):
    c3.set_responses(FakeResponse(200, {"products": [product_body()]}))

    result = c3.client.search("headphones")

    price = result.products[0].offers[0].price
    assert isinstance(price.price, Decimal)
    assert price.price == Decimal("19.99")
    assert isinstance(price.compare_at_price, Decimal)
    assert price.compare_at_price == Decimal("24.99")
    assert price.currency == "USD"


def test_pagination_token_round_trip(c3):
    c3.set_responses(
        FakeResponse(
            200, {"products": [product_body()], "next_page_token": "tok_next"}
        )
    )

    result = c3.client.search("headphones")
    assert result.next_page_token == "tok_next"

    # Continue with the token — it must appear in the request payload, and
    # the query stays optional for continuation pages.
    c3.set_responses(FakeResponse(200, {"products": []}))
    c3.client.search(page_token="tok_next", limit=20)
    assert c3.last_call()["json"] == {"limit": 20, "page_token": "tok_next"}


def test_empty_result_set_is_a_normal_result(c3):
    c3.set_responses(FakeResponse(200, {"products": []}))

    result = c3.client.search("completely unobtainable item")

    assert result.products == []
    assert result.next_page_token is None


def test_filters_and_config_pass_through_verbatim(c3):
    filters = {"price": {"max_price": 100}, "availability": ["InStock"]}
    config = {"mode": "keyword", "currency": "USD"}

    c3.client.search("headphones", limit=10, filters=filters, config=config)

    assert c3.last_call()["json"] == {
        "query": "headphones",
        "limit": 10,
        "filters": filters,
        "config": config,
    }


def test_unexpected_status_maps_to_base_error(c3):
    c3.set_responses(FakeResponse(302))

    with pytest.raises(Channel3Error) as excinfo:
        c3.client.search("headphones")

    assert not isinstance(excinfo.value, Channel3UnavailableError)
    assert not excinfo.value.retryable
