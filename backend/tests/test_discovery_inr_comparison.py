"""INR-aware filtering and comparison tests (Phase 10F).

The scenario Phase 10E could not serve, now end to end: a Serper-shaped
USD-priced result judged against an INR budget.

Everything external is faked - no real discovery request, no real AI
request, no real exchange-rate request. The frozen subsystems (PayPal, the
PayPal currency converter, Policy Engine, ledger, refunds, sessions) are
boobytrapped so any touch fails the test.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.agents import intent_agent
from app.main import app
from app.schemas.comparison import ComparisonRequirements
from app.schemas.discovery import DiscoveredProduct
from app.services import discovery_comparison_service, discovery_service
from app.services import discovery_currency_service as dcs
from app.services import discovery_exchange_rate_provider as drp
from app.services import discovery_filter_service as dfs
from app.services.currency_service import DEMO_INR_PER_USD
from app.services.discovery_exchange_rate_provider import (
    DEFAULT_DEMO_USD_INR_RATE,
    SOURCE_CONFIGURED_DEMO,
)
from app.services.discovery_providers import serper as serper_module
from app.services.product_service import list_products
from app.services.providers import AIProvider

client = TestClient(app)

OBSERVED_AT = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

# With the default configured demo rate of 91.25 INR/USD:
#   USD 34.99 -> INR 3192.84   (above a 3000 budget)
#   USD 32.87 -> INR 2999.39   (within a 3000 budget)
#   USD 32.88 -> INR 3000.30   (just above it)
RATE = DEFAULT_DEMO_USD_INR_RATE


@pytest.fixture(autouse=True)
def _default_rate(monkeypatch):
    monkeypatch.delenv(drp.RATE_ENV_VAR, raising=False)


def usd_product(external_id, price, *, title="Wireless Headphones") -> DiscoveredProduct:
    return DiscoveredProduct(
        external_id=external_id,
        source="serper",
        title=title,
        observed_price=Decimal(price),
        observed_currency="USD",
        observed_at=OBSERVED_AT,
    )


def other_currency_product(external_id, price, currency) -> DiscoveredProduct:
    return DiscoveredProduct(
        external_id=external_id,
        source="serper",
        title="Wireless Headphones",
        observed_price=Decimal(price),
        observed_currency=currency,
        observed_at=OBSERVED_AT,
    )


def inr_budget(amount="3000") -> ComparisonRequirements:
    return ComparisonRequirements(max_price=Decimal(amount), price_currency="INR")


def normalized(products, target="INR"):
    return dcs.normalize_products(products, target)


def ids(products):
    return [p.external_id for p in products]


# ---------------------------------------------------------------------------
# 16-21. Filtering with normalized prices
# ---------------------------------------------------------------------------


def test_inr_budget_filters_using_the_normalized_inr_price():
    # The Phase 10E limitation, fixed: a USD candidate is now judged.
    candidates = normalized([usd_product("cheap", "32.87")])

    survivors = dfs.filter_candidates(candidates, inr_budget("3000"))

    assert ids(survivors) == ["cheap"]
    assert survivors[0].normalized_price == Decimal("2999.39")


def test_usd_price_is_never_compared_directly_against_an_inr_budget():
    # USD 34.99 is numerically far below 3000. If the filter compared the
    # raw observed number it would wrongly survive; via INR 3192.84 it does
    # not.
    candidates = normalized([usd_product("dear", "34.99")])

    assert candidates[0].observed_price < Decimal("3000")
    assert candidates[0].normalized_price > Decimal("3000")
    assert dfs.filter_candidates(candidates, inr_budget("3000")) == []


def test_candidate_above_the_inr_budget_is_excluded():
    candidates = normalized([usd_product("over", "32.88")])

    assert candidates[0].normalized_price == Decimal("3000.30")
    assert dfs.filter_candidates(candidates, inr_budget("3000")) == []


def test_candidate_below_the_inr_budget_survives():
    candidates = normalized([usd_product("under", "20.00")])

    assert ids(dfs.filter_candidates(candidates, inr_budget("3000"))) == ["under"]


def test_candidate_exactly_at_the_inr_budget_survives():
    # 32.876712... would be exact; use a value whose derived price is the
    # bound itself: 3000 / 91.25 is not exact, so assert the boundary via a
    # budget set to the derived figure.
    candidates = normalized([usd_product("exact", "32.87")])

    assert ids(dfs.filter_candidates(candidates, inr_budget("2999.39"))) == ["exact"]


def test_inr_minimum_also_uses_the_normalized_price():
    candidates = normalized([usd_product("low", "10.00"), usd_product("high", "50.00")])
    requirements = ComparisonRequirements(
        min_price=Decimal("3000"), price_currency="INR"
    )

    assert ids(dfs.filter_candidates(candidates, requirements)) == ["high"]


def test_an_inr_range_bounds_normalized_prices():
    candidates = normalized(
        [
            usd_product("too_low", "5.00"),
            usd_product("inside", "30.00"),
            usd_product("too_high", "60.00"),
        ]
    )
    requirements = ComparisonRequirements(
        min_price=Decimal("1000"), max_price=Decimal("3000"), price_currency="INR"
    )

    assert ids(dfs.filter_candidates(candidates, requirements)) == ["inside"]


@pytest.mark.parametrize("currency", ["EUR", "GBP", "JPY"])
def test_unsupported_currency_is_excluded_under_a_strict_inr_budget(currency):
    candidates = normalized([other_currency_product("x", "10.00", currency)])

    assert candidates[0].normalized_price is None
    assert dfs.filter_candidates(candidates, inr_budget("3000")) == []


def test_candidate_without_a_price_is_excluded_under_a_monetary_constraint():
    bare = DiscoveredProduct(
        external_id="bare", source="serper", title="Thing", observed_at=OBSERVED_AT
    )

    assert dfs.filter_candidates(normalized([bare]), inr_budget("3000")) == []


def test_no_monetary_constraint_keeps_unknown_price_candidates():
    # Phase 10E semantics, preserved.
    bare = DiscoveredProduct(
        external_id="bare", source="serper", title="Thing", observed_at=OBSERVED_AT
    )
    candidates = normalized([bare, usd_product("priced", "10.00")])

    survivors = dfs.filter_candidates(candidates, ComparisonRequirements())

    assert set(ids(survivors)) == {"bare", "priced"}


def test_an_inr_candidate_and_a_usd_candidate_are_judged_on_one_scale():
    candidates = normalized(
        [
            other_currency_product("inr_cheap", "2500", "INR"),
            usd_product("usd_dear", "34.99"),
        ]
    )

    survivors = dfs.filter_candidates(candidates, inr_budget("3000"))

    assert ids(survivors) == ["inr_cheap"]


def test_a_mixed_batch_filters_each_candidate_on_its_own_merits():
    candidates = normalized(
        [
            usd_product("usd_ok", "20.00"),
            usd_product("usd_over", "40.00"),
            other_currency_product("inr_ok", "1500", "INR"),
            other_currency_product("eur_unconvertible", "10.00", "EUR"),
        ]
    )

    survivors = dfs.filter_candidates(candidates, inr_budget("3000"))

    assert set(ids(survivors)) == {"usd_ok", "inr_ok"}


def test_normalization_does_not_change_non_price_filtering():
    candidates = normalized([usd_product("p", "10.00", title="Refurbished Headphones")])
    requirements = ComparisonRequirements(excluded_keywords=["refurbished"])

    assert dfs.filter_candidates(candidates, requirements) == []


# ---------------------------------------------------------------------------
# Ordering across currencies
# ---------------------------------------------------------------------------


def test_ordering_uses_the_comparable_amount_when_a_target_currency_exists():
    candidates = normalized(
        [
            usd_product("usd_20", "20.00"),  # INR 1825.00
            other_currency_product("inr_500", "500", "INR"),  # INR 500.00
            usd_product("usd_5", "5.00"),  # INR 456.25
        ]
    )

    ordered = dfs.order_candidates(candidates, inr_budget("100000"))

    assert ids(ordered) == ["usd_5", "inr_500", "usd_20"]


def test_unconvertible_candidates_sort_after_comparable_ones():
    candidates = normalized(
        [
            other_currency_product("eur", "1.00", "EUR"),
            usd_product("usd", "90.00"),
        ]
    )

    ordered = dfs.order_candidates(candidates, inr_budget("100000"))

    assert ids(ordered) == ["usd", "eur"]


def test_ordering_without_a_target_currency_keeps_phase_10e_grouping():
    candidates = normalized(
        [
            usd_product("usd_high", "900.00"),
            other_currency_product("inr_low", "100", "INR"),
        ]
    )

    # No price constraint -> no target currency -> group by observed currency.
    ordered = dfs.order_candidates(candidates, ComparisonRequirements())

    assert ids(ordered) == ["inr_low", "usd_high"]


def test_ordering_is_stable_across_input_permutations_with_normalization():
    base = normalized(
        [
            usd_product("a", "30.00"),
            other_currency_product("b", "500", "INR"),
            usd_product("c", "10.00"),
        ]
    )
    requirements = inr_budget("100000")

    first = ids(dfs.order_candidates(base, requirements))
    backwards = ids(dfs.order_candidates(list(reversed(base)), requirements))

    assert first == backwards == ["b", "c", "a"]


# ---------------------------------------------------------------------------
# Endpoint: the full INR-aware comparison
# ---------------------------------------------------------------------------


class FakeAIProvider(AIProvider):
    name = "fake-ai"

    def __init__(self, ranking=None, intent=None):
        self.intent = intent if intent is not None else {"category": None}
        self.ranking = ranking
        self.intent_calls = []
        self.rank_calls = []

    def extract_intent(self, message: str) -> dict:
        self.intent_calls.append(message)
        return self.intent

    def rank_products(self, requirements, candidates, top_k):
        self.rank_calls.append(
            {"requirements": requirements, "candidates": candidates, "top_k": top_k}
        )
        if self.ranking is not None:
            return self.ranking
        ids_ = [c["external_id"] for c in candidates][:top_k]
        return {
            "ranked_ids": ids_,
            "reasons": [
                {"product_id": i, "reason": "Within the stated budget."} for i in ids_
            ],
        }


class SerperFakeResponse:
    """Serper-shaped USD results, the real-world Phase 10F scenario."""

    status_code = 200

    @staticmethod
    def json():
        return {
            "shopping": [
                {
                    "title": "Sony WH-CH520 Wireless Headphones",
                    "source": "Best Buy",
                    "link": "https://www.bestbuy.com/site/sony-ch520",
                    "price": "$34.99",
                    "productId": "111111",
                },
                {
                    "title": "Budget Wireless Headphones",
                    "source": "Walmart",
                    "link": "https://www.walmart.com/ip/budget",
                    "price": "$19.99",
                    "productId": "222222",
                },
            ]
        }


@pytest.fixture
def frozen_core(monkeypatch):
    """Any touch of a frozen subsystem fails the test."""
    from app import paypal, policy
    from app.services import (
        currency_service,
        refund_service,
        session_service,
        transaction_service,
    )

    def bomb(name):
        def _bomb(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError(f"discovery comparison must not touch {name}")

        return _bomb

    monkeypatch.setattr(paypal, "get_access_token", bomb("PayPal auth"))
    monkeypatch.setattr(paypal, "create_order", bomb("PayPal create_order"))
    monkeypatch.setattr(paypal, "capture_order", bomb("PayPal capture_order"))
    monkeypatch.setattr(paypal, "refund_capture", bomb("PayPal refund_capture"))
    # The PayPal demo converter specifically: discovery has its own rate.
    monkeypatch.setattr(
        currency_service, "convert_inr_to_usd", bomb("the PayPal currency converter")
    )
    monkeypatch.setattr(policy, "evaluate_policy", bomb("the Policy Engine"))
    monkeypatch.setattr(
        transaction_service, "create_transaction", bomb("the transaction ledger")
    )
    monkeypatch.setattr(
        transaction_service, "update_status", bomb("the transaction ledger")
    )
    monkeypatch.setattr(refund_service, "refund_transaction", bomb("the refund service"))
    monkeypatch.setattr(session_service, "create_session", bomb("sessions"))
    monkeypatch.setattr(session_service, "append_turn", bomb("sessions"))


@pytest.fixture
def serper_setup(monkeypatch, frozen_core):
    """Serper discovery + fake AI, the USD-results-meet-INR-budget path."""
    ai = FakeAIProvider()
    monkeypatch.setenv("DISCOVERY_PROVIDER", "serper")
    monkeypatch.setenv("SERPER_API_KEY", "test-serper-key")
    monkeypatch.setattr(
        serper_module.httpx, "post", lambda *a, **k: SerperFakeResponse()
    )
    monkeypatch.setattr(discovery_comparison_service, "get_provider", lambda: ai)
    monkeypatch.setattr(intent_agent, "get_provider", lambda: ai)
    return ai


def post(payload):
    return client.post("/discovery/compare", json=payload)


# ---------------------------------------------------------------------------
# 23. Comparison exposes observed AND normalized price
# ---------------------------------------------------------------------------


def test_comparison_exposes_both_observed_and_normalized_price(serper_setup):
    body = post(
        {
            "query": "wireless headphones",
            "requirements": {"max_price": "5000", "price_currency": "INR"},
            "top_k": 2,
        }
    ).json()

    assert body["normalization_currency"] == "INR"
    product = next(
        r["product"] for r in body["results"] if r["product"]["external_id"] == "serper:111111"
    )
    # Observed stays exactly what Serper displayed...
    assert product["observed_price"] == "34.99"
    assert product["observed_currency"] == "USD"
    # ...and the derived value sits alongside it, clearly labeled.
    assert product["normalized_price"] == "3192.84"
    assert product["normalized_currency"] == "INR"
    assert product["conversion_rate"] == str(RATE)
    assert product["conversion_source"] == SOURCE_CONFIGURED_DEMO
    assert product["rate_observed_at"] is None


def test_the_normalized_price_is_never_presented_as_the_retailer_price(serper_setup):
    response = post(
        {
            "query": "headphones",
            "requirements": {"max_price": "5000", "price_currency": "INR"},
            "top_k": 1,
        }
    )
    product = response.json()["results"][0]["product"]

    # There is no bare "price" field that could be mistaken for one.
    assert "price" not in product
    assert product["observed_price"] != product["normalized_price"]


def test_price_note_states_the_conversion_deterministically(serper_setup):
    body = post(
        {
            "query": "headphones",
            "requirements": {"max_price": "5000", "price_currency": "INR"},
            "top_k": 2,
        }
    ).json()

    note = next(
        r["price_note"]
        for r in body["results"]
        if r["product"]["external_id"] == "serper:111111"
    )
    assert "USD 34.99" in note
    assert "3,192.84" in note
    assert "configured demonstration rate" in note
    assert "live" not in note.lower()


def test_an_inr_budget_admits_the_cheaper_usd_candidate_and_excludes_the_dearer(
    serper_setup,
):
    # THE Phase 10F scenario: a 3000 INR budget against USD results.
    #   $19.99 -> INR 1824.09  (admitted)
    #   $34.99 -> INR 3192.84  (excluded)
    body = post(
        {
            "query": "wireless headphones under 3000",
            "requirements": {"max_price": "3000", "price_currency": "INR"},
            "top_k": 3,
        }
    ).json()

    assert body["discovered_count"] == 2
    assert body["candidates_considered"] == 1
    assert [r["product"]["external_id"] for r in body["results"]] == ["serper:222222"]
    assert body["results"][0]["product"]["normalized_price"] == "1824.09"


def test_an_intent_derived_inr_budget_now_works_end_to_end(serper_setup):
    # Phase 10E returned zero candidates here; the budget was INR and the
    # candidates USD with no way to compare them.
    ai = serper_setup
    ai.intent = {"hard_constraints": {"max_price_inr": 3000}}

    body = post({"query": "wireless headphones under 3000"}).json()

    assert ai.intent_calls == ["wireless headphones under 3000"]
    assert body["requirements"]["price_currency"] == "INR"
    assert body["requirements"]["max_price"] == "3000"
    assert body["candidates_considered"] == 1
    assert body["results"][0]["product"]["observed_currency"] == "USD"
    assert body["results"][0]["product"]["normalized_currency"] == "INR"


# ---------------------------------------------------------------------------
# 24-27. The AI receives normalized data but owns none of it
# ---------------------------------------------------------------------------


def test_ai_receives_the_normalized_value(serper_setup):
    ai = serper_setup

    post(
        {
            "query": "headphones",
            "requirements": {"max_price": "5000", "price_currency": "INR"},
        }
    )

    sent = {c["external_id"]: c for c in ai.rank_calls[0]["candidates"]}
    assert sent["serper:111111"]["normalized_price"] == "3192.84"
    assert sent["serper:111111"]["normalized_currency"] == "INR"
    assert sent["serper:111111"]["observed_price"] == "34.99"


def test_ai_is_not_given_the_exchange_rate_to_recompute_with(serper_setup):
    ai = serper_setup

    post(
        {
            "query": "headphones",
            "requirements": {"max_price": "5000", "price_currency": "INR"},
        }
    )

    for candidate in ai.rank_calls[0]["candidates"]:
        assert "conversion_rate" not in candidate
        assert "conversion_source" not in candidate
        assert "rate_observed_at" not in candidate
    assert str(RATE) not in repr(ai.rank_calls[0]["candidates"])


def test_the_ranking_prompt_forbids_the_model_from_converting():
    from app.services.providers.base import RANKING_SYSTEM_PROMPT

    lowered = RANKING_SYSTEM_PROMPT.lower()
    assert "do not perform currency conversion" in lowered
    assert "use normalized_price when supplied" in lowered
    assert "do not invent or recompute prices" in lowered


def test_ai_cannot_overwrite_the_normalized_price(serper_setup):
    ai = serper_setup
    ai.ranking = {
        "ranked_ids": ["serper:111111"],
        "reasons": [{"product_id": "serper:111111", "reason": "Fine."}],
        "normalized_price": "1.00",
        "normalized_currency": "XXX",
        "conversion_rate": "0.0001",
        "conversion_source": "model_invented",
        "observed_price": "0.01",
    }

    response = post(
        {
            "query": "headphones",
            "requirements": {"max_price": "5000", "price_currency": "INR"},
            "top_k": 1,
        }
    )
    product = response.json()["results"][0]["product"]

    assert product["normalized_price"] == "3192.84"
    assert product["normalized_currency"] == "INR"
    assert product["conversion_rate"] == str(RATE)
    assert product["conversion_source"] == SOURCE_CONFIGURED_DEMO
    assert product["observed_price"] == "34.99"
    for invented in ("model_invented", "0.0001", "XXX", "1.00", "0.01"):
        assert invented not in response.text


def test_ai_cannot_create_a_conversion_for_an_unconvertible_candidate(
    monkeypatch, frozen_core
):
    # A EUR candidate has no derived price. The model claiming one must not
    # put a number anywhere in the response.
    ai = FakeAIProvider(
        ranking={
            "ranked_ids": ["eur_1"],
            "reasons": [{"product_id": "eur_1", "reason": "About 2500 rupees."}],
            "normalized_price": "2500.00",
            "normalized_currency": "INR",
        }
    )

    # Driven at the agent directly: the faked Serper response is USD-only,
    # so a EUR candidate is constructed here instead.
    from app.agents.comparison_agent import rank

    candidates = normalized([other_currency_product("eur_1", "29.99", "EUR")])
    results = rank(ComparisonRequirements(), candidates, top_k=1, provider=ai)

    assert results[0].product.normalized_price is None
    assert results[0].product.normalized_currency is None
    assert results[0].product.conversion_rate is None
    # The model's prose is kept, but it created no structured conversion.
    dumped = results[0].model_dump()
    assert dumped["product"]["normalized_price"] is None


def test_price_note_is_absent_when_nothing_was_derived():
    candidates = normalized([other_currency_product("eur", "29.99", "EUR")])

    assert dcs.describe_conversion(candidates[0]) is None


# ---------------------------------------------------------------------------
# 14 (spec): the AI only ever sees candidates that already passed the budget
# ---------------------------------------------------------------------------


def test_candidates_over_budget_never_reach_the_ai(serper_setup):
    ai = serper_setup

    post(
        {
            "query": "headphones",
            "requirements": {"max_price": "3000", "price_currency": "INR"},
        }
    )

    sent_ids = [c["external_id"] for c in ai.rank_calls[0]["candidates"]]
    # $34.99 -> INR 3192.84 was filtered out before the AI call, so the model
    # has no opportunity to call it "within budget".
    assert sent_ids == ["serper:222222"]


def test_every_candidate_the_ai_sees_satisfies_the_budget(serper_setup):
    ai = serper_setup

    post(
        {
            "query": "headphones",
            "requirements": {"max_price": "3000", "price_currency": "INR"},
        }
    )

    for candidate in ai.rank_calls[0]["candidates"]:
        assert Decimal(candidate["normalized_price"]) <= Decimal("3000")


# ---------------------------------------------------------------------------
# 28-33. Backend ownership, security, catalog
# ---------------------------------------------------------------------------


def test_results_retain_backend_generated_normalized_data(serper_setup):
    body = post(
        {
            "query": "headphones",
            "requirements": {"max_price": "5000", "price_currency": "INR"},
            "top_k": 2,
        }
    ).json()

    for result in body["results"]:
        product = result["product"]
        derived = Decimal(product["normalized_price"])
        expected = (Decimal(product["observed_price"]) * RATE).quantize(Decimal("0.01"))
        assert derived == expected


def test_paypal_policy_ledger_sessions_and_the_paypal_converter_are_untouched(
    serper_setup,
):
    # frozen_core boobytraps all of them; a clean 200 is the assertion.
    response = post(
        {
            "query": "headphones",
            "requirements": {"max_price": "5000", "price_currency": "INR"},
        }
    )

    assert response.status_code == 200


def test_the_paypal_demo_rate_never_appears_in_a_comparison(serper_setup):
    response = post(
        {
            "query": "headphones",
            "requirements": {"max_price": "5000", "price_currency": "INR"},
        }
    )
    body = response.json()

    for result in body["results"]:
        assert result["product"]["conversion_rate"] != str(DEMO_INR_PER_USD)
    assert DEMO_INR_PER_USD != RATE


def test_the_catalog_is_unchanged_by_an_inr_comparison(serper_setup):
    before = [p.model_dump() for p in list_products()]

    post(
        {
            "query": "headphones",
            "requirements": {"max_price": "5000", "price_currency": "INR"},
        }
    )

    assert [p.model_dump() for p in list_products()] == before


def test_a_client_cannot_supply_a_normalized_price_or_rate(serper_setup):
    for smuggled in (
        {"query": "x", "requirements": {"normalized_price": "1.00"}},
        {"query": "x", "requirements": {"conversion_rate": "0.0001"}},
        {"query": "x", "requirements": {"conversion_source": "client"}},
        {"query": "x", "conversion_rate": "0.0001"},
        {"query": "x", "normalization_currency": "XXX"},
    ):
        assert post(smuggled).status_code == 422, smuggled


def test_the_comparison_response_still_exposes_no_purchase_affordance(serper_setup):
    response = post(
        {
            "query": "headphones",
            "requirements": {"max_price": "5000", "price_currency": "INR"},
        }
    )

    for forbidden in ("paypal", "approve_url", "capture_id", "refund_id", "checkout"):
        assert forbidden not in response.text.lower()


# ---------------------------------------------------------------------------
# Rate-source failure does not break discovery
# ---------------------------------------------------------------------------


def test_a_broken_rate_source_still_returns_a_successful_comparison(
    monkeypatch, serper_setup
):
    monkeypatch.setenv(drp.RATE_ENV_VAR, "not-a-number")

    body = post({"query": "headphones", "top_k": 2}).json()

    # No price constraint, so unknown-price candidates stay eligible and the
    # request succeeds with observed prices intact and derived fields null.
    assert body["discovered_count"] == 2
    assert body["candidates_considered"] == 2
    for result in body["results"]:
        assert result["product"]["observed_price"] in {"34.99", "19.99"}
        assert result["product"]["normalized_price"] is None
        assert result["price_note"] is None


def test_a_broken_rate_source_excludes_everything_under_a_strict_inr_budget(
    monkeypatch, serper_setup
):
    ai = serper_setup
    monkeypatch.setenv(drp.RATE_ENV_VAR, "0")

    body = post(
        {
            "query": "headphones",
            "requirements": {"max_price": "3000", "price_currency": "INR"},
        }
    ).json()

    # Honest: nothing could be compared, so nothing is claimed.
    assert body["discovered_count"] == 2
    assert body["candidates_considered"] == 0
    assert body["results"] == []
    assert ai.rank_calls == []


def test_a_custom_configured_rate_flows_through_the_whole_comparison(
    monkeypatch, serper_setup
):
    monkeypatch.setenv(drp.RATE_ENV_VAR, "80")

    body = post(
        {
            "query": "headphones",
            "requirements": {"max_price": "3000", "price_currency": "INR"},
            "top_k": 2,
        }
    ).json()

    # At 80 INR/USD, $34.99 -> 2799.20, which now fits a 3000 budget.
    assert body["candidates_considered"] == 2
    product = next(
        r["product"]
        for r in body["results"]
        if r["product"]["external_id"] == "serper:111111"
    )
    assert product["normalized_price"] == "2799.20"
    assert product["conversion_rate"] == "80"


# ---------------------------------------------------------------------------
# Deterministic fallback keeps the derived data
# ---------------------------------------------------------------------------


def test_the_deterministic_fallback_still_reports_normalized_prices(
    monkeypatch, serper_setup
):
    from app.services.providers import AIServiceError

    ai = serper_setup

    def boom(*args, **kwargs):
        raise AIServiceError("down")

    monkeypatch.setattr(ai, "rank_products", boom)

    body = post(
        {
            "query": "headphones",
            "requirements": {"max_price": "5000", "price_currency": "INR"},
            "top_k": 2,
            "allow_deterministic_fallback": True,
        }
    ).json()

    assert body["ranking_method"] == "deterministic"
    # Cheapest derived INR price first.
    assert [r["product"]["normalized_price"] for r in body["results"]] == [
        "1824.09",
        "3192.84",
    ]
    assert all("not AI-ranked" in r["reason"] for r in body["results"])
    assert all(r["price_note"] for r in body["results"])


# ---------------------------------------------------------------------------
# GET /discovery/search is unchanged by Phase 10F
# ---------------------------------------------------------------------------


def test_get_discovery_search_still_returns_pure_provider_evidence(serper_setup):
    response = client.get("/discovery/search", params={"q": "headphones"})

    assert response.status_code == 200
    product = response.json()["products"][0]
    # Exactly the Phase 10C/10D contract: no PayPilot-derived money mixed in.
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
    }
    for derived in (
        "normalized_price",
        "normalized_currency",
        "conversion_rate",
        "conversion_source",
        "rate_observed_at",
    ):
        assert derived not in product
