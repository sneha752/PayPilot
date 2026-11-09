"""Merchant identity tests (Phase 10H).

The question this module exists to answer: when Serper hands us a product
sold by Amazon behind a Google Shopping link, does PayPilot say "Amazon"
without ever claiming to know amazon.in, and without ever presenting Google
as the seller?

Every provider is faked, so no real Serper, Channel3 or AI request happens
and no credit is spent. The payment core is boobytrapped: a single touch
from any merchant-identity path fails the test.
"""

from __future__ import annotations

import pathlib
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.agents import intent_agent
from app.main import app
from app.schemas.discovery import (
    DiscoveredProduct,
    MerchantEvidence,
    NormalizedDiscoveredProduct,
)
from app.services import (
    discovery_comparison_service,
    discovery_merchant_service,
    discovery_service,
)
from app.services.channel3_client import (
    Channel3Offer,
    Channel3Price,
    Channel3Product,
    Channel3SearchResult,
)
from app.services.discovery_merchant_service import (
    MERCHANT_FIELDS,
    attach_merchant_identity,
    clean_merchant_name,
    derive_merchant_domain,
    is_aggregator_host,
    merchant_names_by_external_id,
    normalize_host,
)
from app.services.discovery_providers import serper as serper_module
from app.services.discovery_providers.channel3 import Channel3Provider
from app.services.discovery_providers.serper import SerperProvider
from app.services.providers import AIProvider

client = TestClient(app)

NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)
TEST_KEY = "test-serper-key"

#: A Serper result as it really arrives: the seller is named in `source`,
#: but the link is a Google Shopping redirect, so the link host is Google.
GOOGLE_LINK = "https://www.google.com/shopping/product/1?gl=us"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def shopping_item(**overrides):
    item = {
        "title": "Sony WH-CH520 Wireless Headphones",
        "source": "Amazon",
        "link": GOOGLE_LINK,
        "price": "$19.99",
        "imageUrl": "https://img.example/ch520.jpg",
        "productId": "111111",
    }
    item.update(overrides)
    return item


class SerperResponse:
    status_code = 200

    def __init__(self, items):
        self._items = items

    def json(self):
        return {"shopping": self._items}


def discovered(**overrides) -> DiscoveredProduct:
    """A plain 13-field provider-evidence record."""
    fields = {
        "external_id": "serper:111111",
        "source": "serper",
        "title": "Sony WH-CH520 Wireless Headphones",
        "product_url": GOOGLE_LINK,
        "retailer_domain": "www.google.com",
        "observed_price": Decimal("19.99"),
        "observed_currency": "USD",
        "observed_at": NOW,
    }
    fields.update(overrides)
    return DiscoveredProduct(**fields)


def normalized(**overrides) -> NormalizedDiscoveredProduct:
    """A post-Phase-10F record, with derived INR alongside observed USD."""
    fields = {
        **discovered().model_dump(),
        "normalized_price": Decimal("1824.09"),
        "normalized_currency": "INR",
        "conversion_rate": Decimal("91.25"),
        "conversion_source": "configured_demo_rate",
    }
    fields.update(overrides)
    return NormalizedDiscoveredProduct(**fields)


def c3_product(product_id="prod_1", *, domain="bestbuy.com", url=None):
    return Channel3Product(
        id=product_id,
        title="Sony WH-CH520 Wireless Headphones",
        offers=[
            Channel3Offer(
                url=url or f"https://www.{domain}/site/{product_id}",
                domain=domain,
                price=Channel3Price(price=Decimal("19.99"), currency="USD"),
                availability="InStock",
            )
        ],
    )


class FakeChannel3Client:
    def __init__(self, products=None):
        self.products = products if products is not None else [c3_product()]

    def search(self, query=None, *, limit=20, page_token=None, **kwargs):
        return Channel3SearchResult(products=self.products)


class FakeAIProvider(AIProvider):
    name = "fake-ai"

    def extract_intent(self, message: str) -> dict:
        return {"category": None}

    def rank_products(self, requirements, candidates, top_k):
        ids = [c["external_id"] for c in candidates][:top_k]
        return {
            "ranked_ids": ids,
            "reasons": [
                {"product_id": i, "reason": f"Fits the candidate set ({i})."}
                for i in ids
            ],
        }


@pytest.fixture
def frozen_core(monkeypatch):
    """Any PayPal/ledger/policy/session/refund touch fails the test."""
    from app import paypal, policy
    from app.services import refund_service, session_service, transaction_service

    def bomb(name):
        def _bomb(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError(f"merchant identity must not touch {name}")

        return _bomb

    monkeypatch.setattr(paypal, "get_access_token", bomb("PayPal auth"))
    monkeypatch.setattr(paypal, "create_order", bomb("PayPal create_order"))
    monkeypatch.setattr(paypal, "capture_order", bomb("PayPal capture_order"))
    monkeypatch.setattr(paypal, "refund_capture", bomb("PayPal refund_capture"))
    monkeypatch.setattr(policy, "evaluate_policy", bomb("the Policy Engine"))
    monkeypatch.setattr(
        transaction_service, "create_transaction", bomb("the transaction ledger")
    )
    monkeypatch.setattr(
        transaction_service, "update_status", bomb("the transaction ledger")
    )
    monkeypatch.setattr(refund_service, "refund_transaction", bomb("refunds"))
    monkeypatch.setattr(session_service, "create_session", bomb("sessions"))


@pytest.fixture
def serper(monkeypatch):
    """A SerperProvider whose HTTP calls are counted, never made."""
    monkeypatch.setenv("SERPER_API_KEY", TEST_KEY)
    state = {"items": [shopping_item()], "calls": 0}

    def fake_post(url, json=None, headers=None, timeout=None):
        state["calls"] += 1
        return SerperResponse(state["items"])

    monkeypatch.setattr(serper_module.httpx, "post", fake_post)

    class Ctx:
        provider = SerperProvider()

        @staticmethod
        def set_items(items):
            state["items"] = items

        @staticmethod
        def call_count():
            return state["calls"]

    return Ctx


def serper_search(ctx, limit=10):
    return ctx.provider.search("wireless headphones", limit=limit, observed_at=NOW)


def _code_without_text(source: str) -> str:
    """Strip comments and string literals, leaving executable code only."""
    import io
    import tokenize

    kept = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        kept.append(token.string)
    return "\n".join(kept)


# ---------------------------------------------------------------------------
# 1. Serper merchant name extraction
# ---------------------------------------------------------------------------


def test_serper_reports_the_merchant_display_name_as_evidence(serper, frozen_core):
    result = serper_search(serper)

    assert [
        (e.external_id, e.merchant_name) for e in result.merchant_evidence
    ] == [("serper:111111", "Amazon")]


def test_serper_keeps_its_own_provenance_in_source(serper, frozen_core):
    """`source` names the PROVIDER, not the shop, so discovery_source is honest."""
    result = serper_search(serper)

    assert result.products[0].source == "serper"
    assert result.products[0].retailer_domain == "www.google.com"


def test_serper_merchant_name_is_trimmed_not_reshaped(serper, frozen_core):
    serper.set_items([shopping_item(source="  Best Buy  ")])

    evidence = serper_search(serper).merchant_evidence

    # Trimmed only: not title-cased, not expanded, not matched to a list.
    assert evidence[0].merchant_name == "Best Buy"


@pytest.mark.parametrize("raw", [None, "", "   ", 42, {"name": "Amazon"}, []])
def test_serper_absent_or_unusable_merchant_name_is_omitted(
    serper, frozen_core, raw
):
    serper.set_items([shopping_item(source=raw)])

    result = serper_search(serper)

    # The product still maps; only the merchant claim is withheld.
    assert len(result.products) == 1
    assert result.merchant_evidence == []


def test_serper_merchant_evidence_is_per_result(serper, frozen_core):
    serper.set_items(
        [
            shopping_item(productId="1", source="Amazon"),
            shopping_item(productId="2", source=None),
            shopping_item(productId="3", source="Walmart"),
        ]
    )

    evidence = serper_search(serper).merchant_evidence

    assert [(e.external_id, e.merchant_name) for e in evidence] == [
        ("serper:1", "Amazon"),
        ("serper:3", "Walmart"),
    ]


def test_serper_merchant_evidence_costs_no_extra_request(serper, frozen_core):
    serper_search(serper)

    assert serper.call_count() == 1


def test_serper_merchant_name_does_not_leak_into_the_search_endpoint(
    monkeypatch, frozen_core
):
    """GET /discovery/search keeps its exact 13-key Phase 10C/10D contract."""
    monkeypatch.setenv("DISCOVERY_PROVIDER", "serper")
    monkeypatch.setenv("SERPER_API_KEY", TEST_KEY)
    monkeypatch.setattr(
        serper_module.httpx,
        "post",
        lambda *a, **k: SerperResponse([shopping_item()]),
    )

    body = client.get("/discovery/search", params={"q": "headphones"}).json()

    product = body["products"][0]
    assert "merchant_name" not in product
    assert "merchant_domain" not in product
    assert len(product) == 13


# ---------------------------------------------------------------------------
# 2. Host normalization and the aggregator rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("bestbuy.com", "bestbuy.com"),
        ("www.bestbuy.com", "bestbuy.com"),
        ("WWW.BestBuy.COM", "bestbuy.com"),
        ("www.bestbuy.com:443", "bestbuy.com"),
        ("www.bestbuy.com.", "bestbuy.com"),
        ("https://www.bestbuy.com/site/x?a=1", "bestbuy.com"),
        ("user:pw@shop.example", "shop.example"),
        ("shop.example/path", "shop.example"),
        # Not usable public merchant identities.
        (None, None),
        ("", None),
        ("   ", None),
        ("localhost", None),
        ("www.", None),
        ("[::1]", None),
    ],
)
def test_normalize_host(raw, expected):
    assert normalize_host(raw) == expected


@pytest.mark.parametrize(
    "host",
    [
        "google.com",
        "www.google.com",
        "shopping.google.com",
        "google.co.in",
        "google.com.au",
        "serper.dev",
        "google.serper.dev",
    ],
)
def test_aggregator_hosts_are_recognized(host):
    assert is_aggregator_host(host) is True


@pytest.mark.parametrize(
    "host",
    [
        "amazon.com",
        "bestbuy.com",
        "flipkart.com",
        # Label boundary matters: these merely contain the word.
        "mygoogle.com",
        "googleadservices.com",
        "notserper.dev",
        None,
        "",
    ],
)
def test_non_aggregator_hosts_are_not_recognized(host):
    assert is_aggregator_host(host) is False


# ---------------------------------------------------------------------------
# 3. Merchant domain derivation: deterministic or null, never guessed
# ---------------------------------------------------------------------------


def test_merchant_domain_is_null_for_a_google_shopping_link():
    product = discovered(retailer_domain="www.google.com", product_url=GOOGLE_LINK)

    assert derive_merchant_domain(product) is None


def test_merchant_domain_comes_from_a_real_retailer_host():
    product = discovered(
        retailer_domain="www.bestbuy.com",
        product_url="https://www.bestbuy.com/site/sony-ch520",
    )

    assert derive_merchant_domain(product) == "bestbuy.com"


def test_merchant_domain_falls_back_to_the_product_url_host():
    product = discovered(
        retailer_domain=None, product_url="https://shop.example/mystery"
    )

    assert derive_merchant_domain(product) == "shop.example"


def test_merchant_domain_prefers_a_real_host_over_an_aggregator_one():
    product = discovered(
        retailer_domain="www.google.com",
        product_url="https://www.amazon.com/dp/B000",
    )

    assert derive_merchant_domain(product) == "amazon.com"


def test_merchant_domain_is_null_when_there_is_no_link_at_all():
    product = discovered(retailer_domain=None, product_url=None)

    assert derive_merchant_domain(product) is None


def test_merchant_name_is_never_turned_into_a_domain():
    """The whole point: a name is not a domain, and never becomes one."""
    products = attach_merchant_identity(
        [normalized()],
        [MerchantEvidence(external_id="serper:111111", merchant_name="Amazon")],
    )

    product = products[0]
    assert product.merchant_name == "Amazon"
    # No amazon.in, amazon.com, or anything else conjured from "Amazon".
    assert product.merchant_domain is None


@pytest.mark.parametrize(
    "name",
    ["Amazon", "Walmart", "Flipkart", "Best Buy", "Target", "eBay", "Croma"],
)
def test_no_well_known_merchant_name_resolves_to_a_domain(name):
    products = attach_merchant_identity(
        [normalized()],
        [MerchantEvidence(external_id="serper:111111", merchant_name=name)],
    )

    assert products[0].merchant_name == name
    assert products[0].merchant_domain is None


def test_merchant_service_holds_no_retailer_lookup_table():
    """A structural guard against a name-to-domain table being introduced.

    The module is allowed to know about AGGREGATORS (hosts that are NOT
    merchants). It must never know about merchants themselves: the moment a
    retailer domain appears in this module's CODE, some name is being
    resolved to a domain by assumption rather than by evidence.

    Comments and docstrings are excluded, because prose about what must not
    be done (for example "'Amazon' does not entitle anyone to write
    amazon.in") is documentation, not a lookup table.
    """
    source = _code_without_text(
        pathlib.Path(discovery_merchant_service.__file__).read_text(encoding="utf-8")
    )

    for retailer in (
        "amazon.",
        "walmart.",
        "flipkart.",
        "bestbuy.",
        "ebay.",
        "target.",
        "croma.",
        "myntra.",
        "etsy.",
    ):
        assert retailer not in source, f"retailer domain {retailer!r} in source"

    # No module-level mapping of any kind holds merchant data; the host
    # rules are a frozenset and a compiled pattern.
    public_dicts = {
        name: value
        for name, value in vars(discovery_merchant_service).items()
        if not name.startswith("__") and isinstance(value, dict)
    }
    assert public_dicts == {}


# ---------------------------------------------------------------------------
# 4. Name carried through normalization, safely
# ---------------------------------------------------------------------------


def test_merchant_name_survives_normalization():
    products = attach_merchant_identity(
        [normalized()],
        [MerchantEvidence(external_id="serper:111111", merchant_name="Amazon")],
    )

    assert products[0].merchant_name == "Amazon"


def test_missing_merchant_evidence_yields_null_name_not_a_substitute():
    products = attach_merchant_identity([normalized()], [])

    product = products[0]
    assert product.merchant_name is None
    # Not backfilled from the domain, the title, or the provider name.
    assert product.merchant_name != product.source
    assert product.merchant_name != product.title


def test_evidence_for_another_product_is_not_applied():
    products = attach_merchant_identity(
        [normalized()],
        [MerchantEvidence(external_id="serper:999999", merchant_name="Amazon")],
    )

    assert products[0].merchant_name is None


def test_duplicate_evidence_resolves_to_the_first_name_deterministically():
    evidence = [
        MerchantEvidence(external_id="serper:111111", merchant_name="Amazon"),
        MerchantEvidence(external_id="serper:111111", merchant_name="Walmart"),
    ]

    assert merchant_names_by_external_id(evidence) == {"serper:111111": "Amazon"}
    assert attach_merchant_identity([normalized()], evidence)[0].merchant_name == (
        "Amazon"
    )


def test_blank_evidence_name_is_treated_as_absent():
    evidence = [MerchantEvidence(external_id="serper:111111", merchant_name="   ")]

    assert merchant_names_by_external_id(evidence) == {}
    assert attach_merchant_identity([normalized()], evidence)[0].merchant_name is None


@pytest.mark.parametrize("raw", [None, "", "  ", 7, ["Amazon"]])
def test_clean_merchant_name_rejects_unusable_values(raw):
    assert clean_merchant_name(raw) is None


def test_merchant_fields_are_always_set_explicitly():
    """A value arriving on the input can never survive unexamined."""
    smuggled = normalized(
        retailer_domain="www.google.com", product_url=GOOGLE_LINK
    ).model_copy(
        update={"merchant_name": "Totally Legit Shop", "merchant_domain": "evil.test"}
    )

    product = attach_merchant_identity([smuggled], [])[0]

    assert product.merchant_name is None
    assert product.merchant_domain is None


def test_attaching_to_an_empty_batch_is_safe():
    assert attach_merchant_identity([], []) == []


# ---------------------------------------------------------------------------
# 5. Observed product data is never mutated
# ---------------------------------------------------------------------------


def test_observed_and_derived_price_fields_are_untouched():
    original = normalized()
    snapshot = original.model_dump()

    product = attach_merchant_identity(
        [original],
        [MerchantEvidence(external_id="serper:111111", merchant_name="Amazon")],
    )[0]

    # The input object itself is unchanged...
    assert original.model_dump() == snapshot
    assert original.merchant_name is None
    # ...and the copy differs in the declared merchant fields, nothing else.
    # Keyed off the module's own MERCHANT_FIELDS, so widening that tuple
    # without widening this test is not possible.
    assert set(MERCHANT_FIELDS) == {"merchant_name", "merchant_domain"}
    before = {k: v for k, v in snapshot.items() if k not in MERCHANT_FIELDS}
    after = {
        k: v
        for k, v in product.model_dump().items()
        if k not in MERCHANT_FIELDS
    }
    assert after == before


def test_the_observed_product_url_is_never_rewritten():
    product = attach_merchant_identity(
        [normalized()],
        [MerchantEvidence(external_id="serper:111111", merchant_name="Amazon")],
    )[0]

    # The link stays the provider's own, even though it points at Google.
    assert product.product_url == GOOGLE_LINK
    assert product.retailer_domain == "www.google.com"


def test_every_price_field_survives_merchant_resolution():
    original = normalized()

    product = attach_merchant_identity([original], [])[0]

    assert product.observed_price == Decimal("19.99")
    assert product.observed_currency == "USD"
    assert product.normalized_price == Decimal("1824.09")
    assert product.normalized_currency == "INR"
    assert product.conversion_rate == Decimal("91.25")
    assert product.conversion_source == "configured_demo_rate"


# ---------------------------------------------------------------------------
# 6. Channel3 backward compatibility
# ---------------------------------------------------------------------------


def test_channel3_reports_no_merchant_evidence(frozen_core):
    result = Channel3Provider(FakeChannel3Client()).search(
        "headphones", limit=10, observed_at=NOW
    )

    # No store/merchant display-name field exists in the Channel3 contract.
    assert result.merchant_evidence == []
    assert result.products[0].source == "channel3"
    assert result.products[0].retailer_domain == "bestbuy.com"


def test_channel3_merchant_domain_comes_from_its_own_offer(frozen_core):
    result = Channel3Provider(FakeChannel3Client()).search(
        "headphones", limit=10, observed_at=NOW
    )
    product = NormalizedDiscoveredProduct(**result.products[0].model_dump())

    resolved = attach_merchant_identity([product], result.merchant_evidence)[0]

    assert resolved.merchant_name is None
    assert resolved.merchant_domain == "bestbuy.com"


def test_channel3_product_without_a_priced_offer_has_no_merchant_identity(
    frozen_core,
):
    bare = Channel3Product(id="prod_bare", title="Mystery Item", offers=[])
    result = Channel3Provider(FakeChannel3Client([bare])).search(
        "x", limit=10, observed_at=NOW
    )
    product = NormalizedDiscoveredProduct(**result.products[0].model_dump())

    resolved = attach_merchant_identity([product], result.merchant_evidence)[0]

    assert resolved.merchant_name is None
    assert resolved.merchant_domain is None


# ---------------------------------------------------------------------------
# 7. End to end through POST /discovery/compare
# ---------------------------------------------------------------------------


@pytest.fixture
def serper_compare(monkeypatch, frozen_core):
    """/discovery/compare backed by a faked Serper and a faked AI provider."""
    monkeypatch.setenv("DISCOVERY_PROVIDER", "serper")
    monkeypatch.setenv("SERPER_API_KEY", TEST_KEY)
    state = {"items": [shopping_item()]}
    monkeypatch.setattr(
        serper_module.httpx,
        "post",
        lambda *a, **k: SerperResponse(state["items"]),
    )
    ai = FakeAIProvider()
    monkeypatch.setattr(discovery_comparison_service, "get_provider", lambda: ai)
    monkeypatch.setattr(intent_agent, "get_provider", lambda: ai)
    return state


def compare(payload):
    return client.post("/discovery/compare", json=payload)


def test_comparison_response_names_the_merchant_not_google(serper_compare):
    body = compare({"query": "wireless headphones", "top_k": 1}).json()

    product = body["results"][0]["product"]
    assert product["merchant_name"] == "Amazon"
    # Google is the link's host, and it is reported as exactly that - the
    # observed retailer_domain - never as the merchant.
    assert product["merchant_domain"] is None
    assert product["retailer_domain"] == "www.google.com"
    assert product["source"] == "serper"


def test_comparison_response_keeps_every_existing_field(serper_compare):
    body = compare({"query": "wireless headphones", "top_k": 1}).json()

    # Response-level fields are unchanged by Phase 10H.
    assert set(body) == {
        "query",
        "requirements",
        "ranking_method",
        "ranking_provider",
        "discovery_source",
        "normalization_currency",
        "discovered_count",
        "candidates_considered",
        "results",
    }
    assert body["discovery_source"] == "serper"
    assert set(body["results"][0]) == {"rank", "product", "reason", "price_note"}

    # Every pre-10H product field is still present, and the ONLY additions
    # are the two merchant fields - nothing was removed or renamed.
    pre_10h = {
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
        "normalized_price",
        "normalized_currency",
        "conversion_rate",
        "conversion_source",
        "rate_observed_at",
    }
    product = body["results"][0]["product"]
    assert pre_10h <= set(product)
    assert set(product) - pre_10h == {"merchant_name", "merchant_domain"}


def test_comparison_preserves_observed_and_derived_prices(serper_compare):
    body = compare({"query": "wireless headphones", "top_k": 1}).json()

    product = body["results"][0]["product"]
    # Phase 10F semantics untouched by merchant identity.
    assert product["observed_price"] == "19.99"
    assert product["observed_currency"] == "USD"
    assert product["normalized_price"] == "1824.09"
    assert product["normalized_currency"] == "INR"
    assert product["conversion_rate"] == "91.25"
    assert product["conversion_source"] == "configured_demo_rate"
    assert "1,824.09" in body["results"][0]["price_note"]


def test_comparison_product_url_is_returned_verbatim(serper_compare):
    body = compare({"query": "wireless headphones", "top_k": 1}).json()

    assert body["results"][0]["product"]["product_url"] == GOOGLE_LINK


def test_comparison_without_a_merchant_name_returns_null(serper_compare):
    serper_compare["items"] = [shopping_item(source=None)]

    body = compare({"query": "wireless headphones", "top_k": 1}).json()

    assert body["results"][0]["product"]["merchant_name"] is None


def test_comparison_derives_a_domain_for_a_direct_merchant_link(serper_compare):
    serper_compare["items"] = [
        shopping_item(source="Best Buy", link="https://www.bestbuy.com/site/ch520")
    ]

    product = compare({"query": "headphones", "top_k": 1}).json()["results"][0][
        "product"
    ]

    assert product["merchant_name"] == "Best Buy"
    assert product["merchant_domain"] == "bestbuy.com"


def test_comparison_with_channel3_is_backward_compatible(
    monkeypatch, frozen_core
):
    monkeypatch.setenv("DISCOVERY_PROVIDER", "channel3")
    monkeypatch.setattr(
        discovery_service, "_make_client", lambda: FakeChannel3Client()
    )
    ai = FakeAIProvider()
    monkeypatch.setattr(discovery_comparison_service, "get_provider", lambda: ai)
    monkeypatch.setattr(intent_agent, "get_provider", lambda: ai)

    product = compare({"query": "headphones", "top_k": 1}).json()["results"][0][
        "product"
    ]

    assert product["source"] == "channel3"
    assert product["merchant_name"] is None
    assert product["merchant_domain"] == "bestbuy.com"
    assert product["observed_price"] == "19.99"


def test_merchant_identity_never_reaches_the_ai(serper_compare, monkeypatch):
    """The model is not given a merchant field it could hallucinate about."""
    seen = {}

    ai = FakeAIProvider()
    original = ai.rank_products

    def spy(requirements, candidates, top_k):
        seen["candidates"] = candidates
        return original(requirements, candidates, top_k)

    ai.rank_products = spy
    monkeypatch.setattr(discovery_comparison_service, "get_provider", lambda: ai)
    monkeypatch.setattr(intent_agent, "get_provider", lambda: ai)

    compare({"query": "wireless headphones", "top_k": 1})

    for candidate in seen["candidates"]:
        assert "merchant_name" not in candidate
        assert "merchant_domain" not in candidate


def test_comparison_does_not_mutate_the_catalog(serper_compare):
    from app.services.product_service import list_products

    before = [p.model_dump() for p in list_products()]

    compare({"query": "wireless headphones", "top_k": 1})

    assert [p.model_dump() for p in list_products()] == before
