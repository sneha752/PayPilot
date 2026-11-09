"""POST /discovery/compare endpoint tests (Phase 10E).

Both the discovery provider and the AI provider are faked, so no real
Channel3, Serper, or AI request can occur and no credit is spent. The
payment core (PayPal, ledger, refunds, Policy Engine, sessions) is
boobytrapped so any touch from this endpoint fails the test.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.agents import intent_agent
from app.services import discovery_comparison_service, discovery_service
from app.services.channel3_client import (
    Channel3Brand,
    Channel3Offer,
    Channel3Price,
    Channel3Product,
    Channel3QuotaError,
    Channel3RateLimitError,
    Channel3SearchResult,
    Channel3UnavailableError,
)
from app.services.discovery_providers import serper as serper_module
from app.services.product_service import list_products
from app.services.providers import (
    AIConfigError,
    AIProvider,
    AIServiceError,
    InvalidAIResponseError,
)

client = TestClient(app)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def c3_product(product_id, title, price, *, currency="USD", brand="Sony", desc=None):
    return Channel3Product(
        id=product_id,
        title=title,
        description=desc,
        brands=[Channel3Brand(id="b1", name=brand)] if brand else [],
        offers=[
            Channel3Offer(
                url=f"https://shop.example/{product_id}",
                domain="shop.example",
                price=Channel3Price(price=Decimal(price), currency=currency),
                availability="InStock",
            )
        ],
    )


DEFAULT_C3_PRODUCTS = [
    c3_product("prod_1", "Sony Wireless Headphones", "299.99", desc="Noise cancelling."),
    c3_product("prod_2", "Sony Budget Headphones", "99.50", desc="Entry level."),
    c3_product("prod_3", "Sony Mid Headphones", "149.00", desc="Mid range."),
]


class FakeChannel3Client:
    def __init__(self, products=None, error=None):
        self.products = DEFAULT_C3_PRODUCTS if products is None else products
        self.error = error
        self.calls = []

    def search(self, query=None, *, limit=20, page_token=None, **kwargs):
        self.calls.append({"query": query, "limit": limit, "page_token": page_token})
        if self.error:
            raise self.error
        return Channel3SearchResult(products=self.products)


class FakeAIProvider(AIProvider):
    """Scripted AI provider for both intent extraction and ranking."""

    name = "fake-ai"

    def __init__(self, ranking=None, rank_error=None, intent=None):
        # By default the intent carries no constraints at all, so the
        # deterministic filter keeps every discovered candidate.
        self.intent = intent if intent is not None else {"category": None}
        self.ranking = ranking
        self.rank_error = rank_error
        self.intent_calls = []
        self.rank_calls = []

    def extract_intent(self, message: str) -> dict:
        self.intent_calls.append(message)
        return self.intent

    def rank_products(self, requirements, candidates, top_k):
        self.rank_calls.append(
            {"requirements": requirements, "candidates": candidates, "top_k": top_k}
        )
        if self.rank_error:
            raise self.rank_error
        if self.ranking is not None:
            return self.ranking
        # Default: rank every supplied candidate, best-first as given.
        ids = [c["external_id"] for c in candidates][:top_k]
        return {
            "ranked_ids": ids,
            "reasons": [
                {"product_id": i, "reason": f"Good fit among the candidates ({i})."}
                for i in ids
            ],
        }


@pytest.fixture
def frozen_core(monkeypatch):
    """Boobytrap every frozen subsystem: a touch from /discovery/compare fails."""
    from app import paypal, policy
    from app.services import (
        refund_service,
        session_service,
        transaction_service,
    )

    def bomb(name):
        def _bomb(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError(f"comparison must not touch {name}")

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
    monkeypatch.setattr(
        refund_service, "refund_transaction", bomb("the refund service")
    )
    monkeypatch.setattr(session_service, "create_session", bomb("sessions"))
    monkeypatch.setattr(session_service, "append_turn", bomb("sessions"))
    monkeypatch.setattr(session_service, "set_pending_action", bomb("sessions"))


@pytest.fixture
def setup(monkeypatch, frozen_core):
    """Fake the Channel3 client and the AI provider; return both fakes."""
    discovery = FakeChannel3Client()
    ai = FakeAIProvider()

    monkeypatch.setenv("DISCOVERY_PROVIDER", "channel3")
    monkeypatch.setattr(discovery_service, "_make_client", lambda: discovery)
    monkeypatch.setattr(discovery_comparison_service, "get_provider", lambda: ai)
    monkeypatch.setattr(intent_agent, "get_provider", lambda: ai)

    return discovery, ai


def post(payload):
    return client.post("/discovery/compare", json=payload)


# ---------------------------------------------------------------------------
# 21-24. top_k behavior and bounds
# ---------------------------------------------------------------------------


def test_top_k_defaults_to_three(setup):
    response = post({"query": "headphones"})

    assert response.status_code == 200
    body = response.json()
    assert len(body["results"]) == 3
    assert [r["rank"] for r in body["results"]] == [1, 2, 3]


def test_top_k_one(setup):
    body = post({"query": "headphones", "top_k": 1}).json()

    assert len(body["results"]) == 1
    assert body["results"][0]["rank"] == 1


def test_top_k_three(setup):
    body = post({"query": "headphones", "top_k": 3}).json()

    assert len(body["results"]) == 3


def test_top_k_five_is_allowed_and_bounded_by_candidate_count(setup):
    body = post({"query": "headphones", "top_k": 5}).json()

    # Only three candidates exist; the response never pads the list.
    assert len(body["results"]) == 3
    assert body["candidates_considered"] == 3


@pytest.mark.parametrize("top_k", [0, 6, -1, 100])
def test_top_k_out_of_bounds_is_rejected(setup, top_k):
    discovery, ai = setup

    response = post({"query": "headphones", "top_k": top_k})

    assert response.status_code == 422
    # Rejected before anything upstream is called.
    assert discovery.calls == []
    assert ai.rank_calls == []


def test_top_k_is_forwarded_to_the_ai_provider(setup):
    _, ai = setup

    post({"query": "headphones", "top_k": 2})

    assert ai.rank_calls[0]["top_k"] == 2


# ---------------------------------------------------------------------------
# 25-28. Authoritative product fields, reasoning, ordering
# ---------------------------------------------------------------------------


def test_product_fields_come_from_backend_discovery_records(setup):
    body = post({"query": "headphones", "top_k": 1}).json()

    product = body["results"][0]["product"]
    assert product["source"] == "channel3"
    assert product["observed_price"] in {"299.99", "99.50", "149.00"}
    assert product["observed_currency"] == "USD"
    assert product["retailer_domain"] == "shop.example"
    assert product["product_url"].startswith("https://shop.example/")
    assert product["observed_at"]  # server-side timestamp
    # Provider evidence, the Phase 10F backend-derived price fields and the
    # Phase 10H backend-derived merchant identity — and nothing else. No
    # AI-supplied field appears.
    assert set(product) == {
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
        "merchant_name",
        "merchant_domain",
    }
    # Channel3 reports no merchant display name, so it stays null rather
    # than being filled from the domain, the brand, or the title. The
    # merchant domain comes from the offer the backend selected.
    assert product["merchant_name"] is None
    assert product["merchant_domain"] == "shop.example"


def test_ai_reasoning_is_attached_to_the_correct_ids(setup):
    _, ai = setup
    ai.ranking = {
        "ranked_ids": ["prod_3", "prod_1"],
        "reasons": {
            "prod_1": "Reason for one.",
            "prod_3": "Reason for three.",
        },
    }

    body = post({"query": "headphones", "top_k": 2}).json()

    assert {r["product"]["external_id"]: r["reason"] for r in body["results"]} == {
        "prod_3": "Reason for three.",
        "prod_1": "Reason for one.",
    }


def test_result_ordering_matches_the_validated_ai_ranking(setup):
    _, ai = setup
    # Deliberately NOT the deterministic price order (which is 2, 3, 1).
    ai.ranking = {"ranked_ids": ["prod_1", "prod_3", "prod_2"]}

    body = post({"query": "headphones", "top_k": 3}).json()

    assert [r["product"]["external_id"] for r in body["results"]] == [
        "prod_1",
        "prod_3",
        "prod_2",
    ]
    assert [r["rank"] for r in body["results"]] == [1, 2, 3]
    assert body["ranking_method"] == "ai"
    assert body["ranking_provider"] == "fake-ai"


def test_no_hallucinated_product_fields_appear_in_the_response(setup):
    _, ai = setup
    ai.ranking = {
        "ranked_ids": ["prod_1"],
        "reasons": [{"product_id": "prod_1", "reason": "Fine."}],
        "observed_price": "0.01",
        "observed_currency": "XXX",
        "product_url": "https://evil.example/buy",
        "rating": 5,
        "products": [{"external_id": "injected", "title": "Injected"}],
        "paypal_amount": "1.00",
    }

    response = post({"query": "headphones", "top_k": 1})
    body = response.json()

    assert body["results"][0]["product"]["observed_price"] == "299.99"
    for invented in ("evil.example", "injected", "XXX", "0.01", "paypal_amount"):
        assert invented not in response.text


def test_candidates_considered_and_discovered_count_are_reported(setup):
    body = post({"query": "headphones"}).json()

    assert body["discovered_count"] == 3
    assert body["candidates_considered"] == 3
    assert body["discovery_source"] == "channel3"
    assert body["query"] == "headphones"


def test_the_ai_only_sees_the_reduced_candidate_set(setup):
    discovery, ai = setup
    discovery.products = [
        c3_product(f"prod_{i:02d}", f"Headphones {i}", str(100 + i)) for i in range(25)
    ]

    body = post({"query": "headphones"}).json()

    # Deterministic cap: never hundreds of results.
    assert len(ai.rank_calls[0]["candidates"]) == 10
    assert body["discovered_count"] == 25
    assert body["candidates_considered"] == 10


def test_ai_candidate_payload_omits_urls_and_images(setup):
    _, ai = setup

    post({"query": "headphones"})

    for candidate in ai.rank_calls[0]["candidates"]:
        assert "product_url" not in candidate
        assert "image_url" not in candidate
        assert "observed_at" not in candidate


# ---------------------------------------------------------------------------
# Requirements: explicit vs intent-derived
# ---------------------------------------------------------------------------


def test_explicit_requirements_skip_intent_extraction(setup):
    _, ai = setup

    body = post(
        {
            "query": "headphones",
            "requirements": {
                "max_price": "150",
                "price_currency": "USD",
                "brand": "Sony",
            },
            "top_k": 3,
        }
    ).json()

    assert ai.intent_calls == []  # no AI intent call spent
    assert body["requirements"]["max_price"] == "150"
    # prod_1 at 299.99 is filtered out deterministically.
    assert body["candidates_considered"] == 2
    assert {r["product"]["external_id"] for r in body["results"]} == {
        "prod_2",
        "prod_3",
    }


def test_requirements_are_derived_from_intent_when_omitted(setup):
    _, ai = setup
    ai.intent = {"category": None, "excluded_features": ["entry level"]}

    body = post({"query": "good headphones"}).json()

    assert ai.intent_calls == ["good headphones"]
    assert body["requirements"]["excluded_keywords"] == ["entry level"]
    # prod_2's description says "Entry level." -> deterministically excluded.
    assert body["candidates_considered"] == 2
    assert "prod_2" not in {r["product"]["external_id"] for r in body["results"]}


def test_derived_inr_budget_excludes_usd_candidates_without_converting(setup):
    _, ai = setup
    ai.intent = {"hard_constraints": {"max_price_inr": 3000}}

    body = post({"query": "headphones under 3000"}).json()

    # The bound is INR, the candidates are USD; no conversion is performed,
    # so nothing is comparable and nothing is claimed.
    assert body["requirements"]["price_currency"] == "INR"
    assert body["candidates_considered"] == 0
    assert body["results"] == []
    assert ai.rank_calls == []  # no AI ranking call spent on an empty set


def test_authorization_threshold_never_becomes_a_price_bound(setup):
    _, ai = setup
    ai.intent = {
        "hard_constraints": {},
        "authorization": {
            "purchase_requested": True,
            "autonomous_purchase_allowed": True,
            "auto_approve_below_inr": 50000,
        },
    }

    response = post({"query": "headphones"})
    body = response.json()

    assert body["requirements"]["max_price"] is None
    assert body["requirements"]["min_price"] is None
    assert "authorization" not in response.text
    assert "50000" not in response.text


def test_client_cannot_supply_a_product_or_payment_field(setup):
    discovery, ai = setup

    for smuggled in (
        {"query": "x", "products": [{"external_id": "fake", "observed_price": "1.00"}]},
        {"query": "x", "requirements": {"paypal_amount": "1.00"}},
        {"query": "x", "requirements": {"capture_id": "CAP-1"}},
        {"query": "x", "requirements": {"refund_id": "REF-1"}},
        {"query": "x", "paypal_order_id": "ORDER-1"},
        {"query": "x", "requirements": {"observed_price": "1.00"}},
    ):
        response = post(smuggled)
        assert response.status_code == 422, smuggled

    assert discovery.calls == []
    assert ai.rank_calls == []


def test_requirements_with_a_price_bound_but_no_currency_is_rejected(setup):
    response = post({"query": "x", "requirements": {"max_price": "100"}})

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# 29-34. Security / freeze boundary
# ---------------------------------------------------------------------------


def test_paypal_policy_ledger_and_sessions_are_never_called(setup):
    # The frozen_core fixture boobytraps PayPal, the Policy Engine, the
    # transaction ledger, the refund service, and sessions. A clean 200 is
    # the assertion.
    assert post({"query": "headphones"}).status_code == 200


def test_the_local_catalog_is_unchanged_by_a_comparison(setup):
    before = [p.model_dump() for p in list_products()]

    post({"query": "headphones"})

    assert [p.model_dump() for p in list_products()] == before


def test_the_response_exposes_no_purchase_affordance(setup):
    response = post({"query": "headphones"})
    body = response.json()

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
    assert set(body["results"][0]) == {"rank", "product", "reason", "price_note"}
    for forbidden in (
        "paypal",
        "approve_url",
        "order_id",
        "capture_id",
        "refund_id",
        "transaction_id",
        "policy",
        "purchasable",
        "checkout",
    ):
        assert forbidden not in response.text.lower()


def test_comparison_is_read_only_for_http_verbs(setup):
    # Only POST exists; the endpoint has no mutating siblings.
    assert client.get("/discovery/compare").status_code == 405
    assert client.delete("/discovery/compare").status_code == 405
    assert client.put("/discovery/compare", json={"query": "x"}).status_code == 405


def test_get_discovery_search_contract_is_unchanged(setup):
    response = client.get("/discovery/search", params={"q": "headphones"})

    assert response.status_code == 200
    assert set(response.json()) == {"query", "products", "next_page_token"}


# ---------------------------------------------------------------------------
# 35-38. Errors and empty sets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (Channel3QuotaError("credits exhausted"), 503),
        (Channel3RateLimitError("rate limited"), 429),
        (Channel3UnavailableError("upstream down"), 503),
    ],
)
def test_discovery_provider_failures_map_to_honest_statuses(
    setup, error, expected_status
):
    discovery, ai = setup
    discovery.error = error

    response = post({"query": "headphones"})

    assert response.status_code == expected_status
    assert "detail" in response.json()
    assert ai.rank_calls == []  # no AI call after a discovery failure


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (AIConfigError("GROQ_API_KEY is not set"), 503),
        (AIServiceError("request timed out"), 502),
        (InvalidAIResponseError("non-JSON content"), 502),
    ],
)
def test_ai_provider_failures_map_to_honest_statuses(setup, error, expected_status):
    _, ai = setup
    ai.rank_error = error

    response = post({"query": "headphones"})

    assert response.status_code == expected_status
    # No fabricated ranking.
    assert "results" not in response.json()


@pytest.mark.parametrize(
    "ranking",
    [
        {"ranked_ids": ["prod_1", "hallucinated-999"]},  # unknown id
        {"ranked_ids": ["prod_1", "prod_1"]},  # duplicate
        {"ranked_ids": []},  # nothing ranked
        {"ranked_ids": "prod_1"},  # malformed
    ],
)
def test_invalid_ai_output_is_a_502_not_a_fabricated_ranking(setup, ranking):
    _, ai = setup
    ai.ranking = ranking

    response = post({"query": "headphones"})

    assert response.status_code == 502
    assert "results" not in response.json()


def test_empty_discovery_result_is_an_honest_empty_200(setup):
    discovery, ai = setup
    discovery.products = []

    body = post({"query": "unfindable"}).json()

    assert body["discovered_count"] == 0
    assert body["candidates_considered"] == 0
    assert body["results"] == []
    assert body["ranking_method"] == "deterministic"
    assert body["ranking_provider"] is None
    assert ai.rank_calls == []  # no AI call spent on an empty set


def test_empty_candidate_set_after_filtering_is_an_honest_empty_200(setup):
    _, ai = setup

    body = post(
        {
            "query": "headphones",
            "requirements": {"max_price": "1", "price_currency": "USD"},
        }
    ).json()

    assert body["discovered_count"] == 3
    assert body["candidates_considered"] == 0
    assert body["results"] == []
    assert ai.rank_calls == []


def test_blank_query_is_rejected_before_any_upstream_call(setup):
    discovery, ai = setup

    response = post({"query": "   "})

    assert response.status_code == 400
    assert discovery.calls == []
    assert ai.intent_calls == []


def test_missing_query_is_a_422(setup):
    assert post({}).status_code == 422


@pytest.mark.parametrize("limit", [0, 31, -5])
def test_out_of_range_limit_is_rejected(setup, limit):
    discovery, _ = setup

    assert post({"query": "x", "limit": limit}).status_code == 422
    assert discovery.calls == []


def test_limit_reaches_the_discovery_provider(setup):
    discovery, _ = setup

    post({"query": "headphones", "limit": 7})

    assert discovery.calls[0]["limit"] == 7


# ---------------------------------------------------------------------------
# Deterministic fallback (never labeled as AI-ranked)
# ---------------------------------------------------------------------------


def test_fallback_is_off_by_default(setup):
    _, ai = setup
    ai.rank_error = AIServiceError("timeout")

    assert post({"query": "headphones"}).status_code == 502


@pytest.mark.parametrize(
    "error",
    [
        AIConfigError("no key"),
        AIServiceError("timeout"),
        InvalidAIResponseError("malformed JSON"),
    ],
)
def test_opt_in_fallback_returns_a_clearly_deterministic_ranking(setup, error):
    _, ai = setup
    ai.rank_error = error

    body = post(
        {
            "query": "headphones",
            "top_k": 3,
            "allow_deterministic_fallback": True,
        }
    ).json()

    assert body["ranking_method"] == "deterministic"
    assert body["ranking_provider"] is None
    # Deterministic order: lowest observed price first.
    assert [r["product"]["external_id"] for r in body["results"]] == [
        "prod_2",
        "prod_3",
        "prod_1",
    ]
    for result in body["results"]:
        assert "not AI-ranked" in result["reason"]


def test_fallback_also_covers_invalid_ai_output(setup):
    _, ai = setup
    ai.ranking = {"ranked_ids": ["hallucinated-999"]}

    body = post(
        {"query": "headphones", "top_k": 1, "allow_deterministic_fallback": True}
    ).json()

    assert body["ranking_method"] == "deterministic"
    assert body["results"][0]["product"]["external_id"] == "prod_2"


def test_fallback_never_claims_to_be_ai_generated(setup):
    _, ai = setup
    ai.rank_error = AIServiceError("down")

    response = post({"query": "headphones", "allow_deterministic_fallback": True})

    body = response.json()
    assert body["ranking_method"] != "ai"
    assert body["ranking_provider"] is None
    # Every reason explicitly disclaims AI ranking rather than merely
    # omitting the claim.
    for result in body["results"]:
        lowered = result["reason"].lower()
        assert "not ai-ranked" in lowered
        assert "ai-ranked" not in lowered.replace("not ai-ranked", "")


def test_fallback_respects_top_k(setup):
    _, ai = setup
    ai.rank_error = AIServiceError("down")

    body = post(
        {"query": "headphones", "top_k": 1, "allow_deterministic_fallback": True}
    ).json()

    assert len(body["results"]) == 1


def test_intent_extraction_failure_is_never_absorbed_by_the_fallback(
    setup, monkeypatch
):
    # Silently dropping the user's stated constraints would be worse than an
    # honest error, so a requirements-derivation failure always propagates.
    _, ai = setup

    def boom(message: str) -> dict:
        raise AIServiceError("intent extraction failed")

    monkeypatch.setattr(ai, "extract_intent", boom)

    response = post({"query": "headphones", "allow_deterministic_fallback": True})

    assert response.status_code == 502


# ---------------------------------------------------------------------------
# 39-40. Provider compatibility (both mocked)
# ---------------------------------------------------------------------------


def test_comparison_works_with_a_mocked_channel3_provider(setup):
    body = post({"query": "headphones", "top_k": 1}).json()

    assert body["discovery_source"] == "channel3"
    assert body["results"][0]["product"]["source"] == "channel3"
    assert body["ranking_method"] == "ai"


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
                },
                {
                    "title": "Sony WH-CH720N Headphones",
                    "source": "Walmart",
                    "link": "https://www.walmart.com/ip/sony-ch720n",
                    "price": "$118.00",
                    "productId": "998877",
                },
            ]
        }


def test_comparison_works_with_a_mocked_serper_provider(monkeypatch, frozen_core):
    ai = FakeAIProvider()
    monkeypatch.setenv("DISCOVERY_PROVIDER", "serper")
    monkeypatch.setenv("SERPER_API_KEY", "test-serper-key")
    monkeypatch.setattr(
        serper_module.httpx, "post", lambda *a, **k: SerperFakeResponse()
    )
    monkeypatch.setattr(discovery_comparison_service, "get_provider", lambda: ai)
    monkeypatch.setattr(intent_agent, "get_provider", lambda: ai)

    body = post({"query": "headphones", "top_k": 2}).json()

    assert body["discovery_source"] == "serper"
    assert body["candidates_considered"] == 2
    ids = [r["product"]["external_id"] for r in body["results"]]
    assert ids == ["serper:998877", "serper:736876"]  # deterministic: cheaper first
    assert body["results"][0]["product"]["observed_price"] == "118.00"
    assert body["ranking_method"] == "ai"


def test_both_providers_produce_the_same_result_shape(monkeypatch, frozen_core):
    ai = FakeAIProvider()
    monkeypatch.setattr(discovery_comparison_service, "get_provider", lambda: ai)
    monkeypatch.setattr(intent_agent, "get_provider", lambda: ai)

    monkeypatch.setenv("DISCOVERY_PROVIDER", "channel3")
    monkeypatch.setattr(
        discovery_service, "_make_client", lambda: FakeChannel3Client()
    )
    channel3 = post({"query": "x", "top_k": 1}).json()

    monkeypatch.setenv("DISCOVERY_PROVIDER", "serper")
    monkeypatch.setenv("SERPER_API_KEY", "test-serper-key")
    monkeypatch.setattr(
        serper_module.httpx, "post", lambda *a, **k: SerperFakeResponse()
    )
    serper = post({"query": "x", "top_k": 1}).json()

    assert set(channel3) == set(serper)
    assert set(channel3["results"][0]["product"]) == set(
        serper["results"][0]["product"]
    )
