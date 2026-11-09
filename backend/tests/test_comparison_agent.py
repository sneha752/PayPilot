"""AI ranking and output-validation tests (Phase 10E).

Every AI provider is faked — no real AI request is ever made. The point of
these tests is that the model is NOT trusted: unknown ids, duplicate ids,
over-long rankings, malformed payloads and invented product fields are all
caught by the agent rather than reaching application state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.agents import comparison_agent
from app.agents.comparison_agent import (
    NEUTRAL_REASON,
    RankingValidationError,
    build_ai_candidate_payload,
    rank,
)
from app.schemas.comparison import ComparisonRequirements
from app.schemas.discovery import NormalizedDiscoveredProduct
from app.services.providers import (
    AIConfigError,
    AIProvider,
    AIServiceError,
    InvalidAIResponseError,
)

OBSERVED_AT = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


# Phase 10F: the ranking pipeline operates on normalized records, so the
# candidates handed to the agent carry the derived-price fields (left null
# here unless a test sets them).
def product(
    external_id="p1", *, title=None, price=None, **kwargs
) -> NormalizedDiscoveredProduct:
    return NormalizedDiscoveredProduct(
        external_id=external_id,
        source=kwargs.pop("source", "channel3"),
        title=title or f"Product {external_id}",
        observed_price=Decimal(price) if price is not None else None,
        observed_currency=kwargs.pop("currency", "USD" if price else None),
        observed_at=OBSERVED_AT,
        **kwargs,
    )


CANDIDATES = [product("a"), product("b"), product("c")]


class FakeProvider(AIProvider):
    """Records what it was sent and returns a scripted payload."""

    name = "fake"

    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    def extract_intent(self, message: str) -> dict:  # pragma: no cover
        raise AssertionError("ranking must not extract intent")

    def rank_products(self, requirements, candidates, top_k):
        self.calls.append(
            {"requirements": requirements, "candidates": candidates, "top_k": top_k}
        )
        if self.error:
            raise self.error
        return self.payload


# ---------------------------------------------------------------------------
# 11. Valid ranking accepted
# ---------------------------------------------------------------------------


def test_valid_ranking_is_accepted_in_model_order():
    fake = FakeProvider(
        {
            "ranked_ids": ["c", "a"],
            "reasons": [
                {"product_id": "c", "reason": "Closest match among the candidates."},
                {"product_id": "a", "reason": "Lower observed price."},
            ],
        }
    )

    results = rank(ComparisonRequirements(), CANDIDATES, top_k=3, provider=fake)

    assert [r.product.external_id for r in results] == ["c", "a"]
    assert [r.rank for r in results] == [1, 2]
    assert results[0].reason == "Closest match among the candidates."


def test_ranked_entries_carry_the_caller_s_own_product_instances():
    fake = FakeProvider({"ranked_ids": ["b"]})

    results = rank(ComparisonRequirements(), CANDIDATES, top_k=3, provider=fake)

    # Identity, not equality: the AI cannot have rebuilt the record.
    assert results[0].product is CANDIDATES[1]


def test_reasons_may_be_supplied_as_a_mapping():
    fake = FakeProvider(
        {"ranked_ids": ["a", "b"], "reasons": {"a": "First.", "b": "Second."}}
    )

    results = rank(ComparisonRequirements(), CANDIDATES, top_k=3, provider=fake)

    assert [r.reason for r in results] == ["First.", "Second."]


def test_ids_are_matched_case_insensitively_and_canonicalized():
    fake = FakeProvider({"ranked_ids": ["  A  "]})

    results = rank(ComparisonRequirements(), CANDIDATES, top_k=3, provider=fake)

    # The backend's spelling wins, not the model's formatting.
    assert results[0].product.external_id == "a"


# ---------------------------------------------------------------------------
# 12-14. Unknown / duplicate / excess ids
# ---------------------------------------------------------------------------


def test_unknown_product_id_is_rejected():
    fake = FakeProvider({"ranked_ids": ["a", "hallucinated-999"]})

    with pytest.raises(RankingValidationError, match="not in the candidate list"):
        rank(ComparisonRequirements(), CANDIDATES, top_k=3, provider=fake)


def test_unknown_id_does_not_create_a_product_even_partially():
    fake = FakeProvider({"ranked_ids": ["ghost"]})

    with pytest.raises(RankingValidationError):
        rank(ComparisonRequirements(), CANDIDATES, top_k=3, provider=fake)

    # The candidate list is unchanged; no record was invented for "ghost".
    assert [p.external_id for p in CANDIDATES] == ["a", "b", "c"]


def test_duplicate_product_id_is_rejected():
    fake = FakeProvider({"ranked_ids": ["a", "b", "a"]})

    with pytest.raises(RankingValidationError, match="duplicate"):
        rank(ComparisonRequirements(), CANDIDATES, top_k=3, provider=fake)


def test_case_differing_duplicate_is_still_a_duplicate():
    fake = FakeProvider({"ranked_ids": ["a", "A"]})

    with pytest.raises(RankingValidationError, match="duplicate"):
        rank(ComparisonRequirements(), CANDIDATES, top_k=3, provider=fake)


def test_too_many_ids_are_truncated_to_top_k():
    fake = FakeProvider({"ranked_ids": ["c", "b", "a"]})

    results = rank(ComparisonRequirements(), CANDIDATES, top_k=1, provider=fake)

    # A validated ordering's prefix is still validated; the backend keeps
    # control of the count.
    assert [r.product.external_id for r in results] == ["c"]
    assert [r.rank for r in results] == [1]


def test_empty_ranking_is_rejected_rather_than_returned_as_a_judgement():
    fake = FakeProvider({"ranked_ids": []})

    with pytest.raises(RankingValidationError, match="no candidate ids"):
        rank(ComparisonRequirements(), CANDIDATES, top_k=3, provider=fake)


def test_fewer_ids_than_top_k_is_fine():
    fake = FakeProvider({"ranked_ids": ["b"]})

    results = rank(ComparisonRequirements(), CANDIDATES, top_k=3, provider=fake)

    assert len(results) == 1


# ---------------------------------------------------------------------------
# 15. Reasons
# ---------------------------------------------------------------------------


def test_missing_reason_is_filled_with_a_neutral_deterministic_fallback():
    fake = FakeProvider({"ranked_ids": ["a", "b"], "reasons": [{"product_id": "a", "reason": "Cheaper."}]})

    results = rank(ComparisonRequirements(), CANDIDATES, top_k=3, provider=fake)

    assert results[0].reason == "Cheaper."
    assert results[1].reason == NEUTRAL_REASON


def test_neutral_fallback_asserts_no_product_facts():
    lowered = NEUTRAL_REASON.lower()
    for claim in ("price", "best", "cheapest", "rating", "battery", "$"):
        assert claim not in lowered


def test_blank_reason_falls_back_to_the_neutral_reason():
    fake = FakeProvider({"ranked_ids": ["a"], "reasons": [{"product_id": "a", "reason": "   "}]})

    results = rank(ComparisonRequirements(), CANDIDATES, top_k=3, provider=fake)

    assert results[0].reason == NEUTRAL_REASON


def test_reason_for_an_unranked_candidate_is_discarded():
    fake = FakeProvider(
        {
            "ranked_ids": ["a"],
            "reasons": [
                {"product_id": "a", "reason": "Kept."},
                {"product_id": "c", "reason": "Dropped — c was not ranked."},
            ],
        }
    )

    results = rank(ComparisonRequirements(), CANDIDATES, top_k=3, provider=fake)

    assert len(results) == 1
    assert results[0].reason == "Kept."


def test_reason_for_an_unknown_id_is_discarded_not_fatal():
    fake = FakeProvider(
        {
            "ranked_ids": ["a"],
            "reasons": [
                {"product_id": "nonexistent", "reason": "Nowhere to attach."},
                {"product_id": "a", "reason": "Real."},
            ],
        }
    )

    results = rank(ComparisonRequirements(), CANDIDATES, top_k=3, provider=fake)

    assert results[0].reason == "Real."


def test_reasons_are_attached_to_the_correct_ids():
    fake = FakeProvider(
        {
            "ranked_ids": ["c", "a", "b"],
            "reasons": {"a": "reason-a", "b": "reason-b", "c": "reason-c"},
        }
    )

    results = rank(ComparisonRequirements(), CANDIDATES, top_k=5, provider=fake)

    assert {r.product.external_id: r.reason for r in results} == {
        "a": "reason-a",
        "b": "reason-b",
        "c": "reason-c",
    }


# ---------------------------------------------------------------------------
# 16-17. Malformed payloads and provider errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"ranked_ids": "a"},  # string, not an array
        {"ranked_ids": [{"id": "a"}]},  # objects, not ids
        {"ranked_ids": ["a"], "reasons": "nope"},
        {"ranked_ids": ["a"], "reasons": [{"product_id": "a"}]},  # reason missing
        {},  # no ranked_ids at all -> empty -> rejected
    ],
)
def test_malformed_ranking_payloads_are_rejected(payload):
    fake = FakeProvider(payload)

    with pytest.raises(RankingValidationError):
        rank(ComparisonRequirements(), CANDIDATES, top_k=3, provider=fake)


def test_non_dict_payload_is_rejected():
    fake = FakeProvider(None)
    fake.payload = ["not", "a", "dict"]

    with pytest.raises((RankingValidationError, TypeError)):
        rank(ComparisonRequirements(), CANDIDATES, top_k=3, provider=fake)


@pytest.mark.parametrize(
    "error",
    [
        AIConfigError("no key"),
        AIServiceError("timeout"),
        InvalidAIResponseError("non-JSON content"),
    ],
)
def test_provider_errors_propagate_unchanged(error):
    fake = FakeProvider(error=error)

    with pytest.raises(type(error)):
        rank(ComparisonRequirements(), CANDIDATES, top_k=3, provider=fake)


def test_empty_candidate_set_is_a_caller_error_and_spends_no_ai_call():
    fake = FakeProvider({"ranked_ids": []})

    with pytest.raises(ValueError):
        rank(ComparisonRequirements(), [], top_k=3, provider=fake)

    assert fake.calls == []


def test_top_k_below_one_is_a_caller_error():
    fake = FakeProvider({"ranked_ids": ["a"]})

    with pytest.raises(ValueError):
        rank(ComparisonRequirements(), CANDIDATES, top_k=0, provider=fake)

    assert fake.calls == []


# ---------------------------------------------------------------------------
# 18-20. What the AI receives, and what it cannot introduce
# ---------------------------------------------------------------------------


def test_ai_receives_only_the_intended_candidate_fields():
    rich = product(
        "rich",
        title="Sony WH-1000XM5",
        description="Noise cancelling.",
        brand="Sony",
        category="headphones",
        availability="InStock",
        price="299.99",
        product_url="https://shop.example/p",
        image_url="https://img.example/p.jpg",
        retailer_domain="shop.example",
        normalized_price=Decimal("27374.00"),
        normalized_currency="INR",
        conversion_rate=Decimal("91.25"),
        conversion_source="configured_demo_rate",
    )

    payload = build_ai_candidate_payload([rich])

    assert set(payload[0]) == {
        "external_id",
        "title",
        "description",
        "brand",
        "category",
        "availability",
        "observed_price",
        "observed_currency",
        "normalized_price",
        "normalized_currency",
        "retailer_domain",
    }
    # Withheld as unnecessary for ranking.
    for withheld in ("product_url", "image_url", "source", "observed_at"):
        assert withheld not in payload[0]
    # The rate itself is withheld: handing it over would invite the model to
    # recompute a conversion the backend already performed.
    for withheld in ("conversion_rate", "conversion_source", "rate_observed_at"):
        assert withheld not in payload[0]


def test_ai_receives_the_derived_amount_as_an_exact_string():
    candidate = product(
        "p",
        price="34.99",
        normalized_price=Decimal("3192.84"),
        normalized_currency="INR",
    )

    payload = build_ai_candidate_payload([candidate])

    assert payload[0]["observed_price"] == "34.99"
    assert payload[0]["observed_currency"] == "USD"
    assert payload[0]["normalized_price"] == "3192.84"
    assert payload[0]["normalized_currency"] == "INR"


def test_unconvertible_candidate_sends_no_normalized_fields_to_the_ai():
    payload = build_ai_candidate_payload([product("p", price="10.00")])

    # Null derived fields are omitted, not sent as nulls the model might fill.
    assert "normalized_price" not in payload[0]
    assert "normalized_currency" not in payload[0]


def test_observed_price_reaches_the_ai_as_an_exact_string():
    payload = build_ai_candidate_payload([product("p", price="19.99")])

    assert payload[0]["observed_price"] == "19.99"


def test_null_candidate_fields_are_omitted_rather_than_sent_as_null():
    payload = build_ai_candidate_payload([product("bare")])

    # An absent field reads as absent, not as a blank the model might fill.
    assert "brand" not in payload[0]
    assert "description" not in payload[0]
    assert payload[0]["external_id"] == "bare"


def test_ai_never_receives_payment_or_session_secrets():
    requirements = ComparisonRequirements(
        max_price=Decimal("300"),
        price_currency="USD",
        preferred_brand="Sony",
        use_case="travel",
    )
    fake = FakeProvider({"ranked_ids": ["a"]})

    rank(requirements, CANDIDATES, top_k=3, provider=fake)

    sent = repr(fake.calls[0]).lower()
    for forbidden in (
        "paypal",
        "client_secret",
        "access_token",
        "capture",
        "refund",
        "transaction",
        "session",
        "authorization",
        "auto_approve",
        "order_id",
        "policy",
    ):
        assert forbidden not in sent


def test_requirements_payload_carries_no_authorization_block():
    fake = FakeProvider({"ranked_ids": ["a"]})

    rank(ComparisonRequirements(use_case="travel"), CANDIDATES, top_k=3, provider=fake)

    assert "authorization" not in fake.calls[0]["requirements"]


def test_top_k_is_forwarded_to_the_provider():
    fake = FakeProvider({"ranked_ids": ["a"]})

    rank(ComparisonRequirements(), CANDIDATES, top_k=2, provider=fake)

    assert fake.calls[0]["top_k"] == 2


def test_ai_cannot_introduce_new_product_data_through_extra_fields():
    fake = FakeProvider(
        {
            "ranked_ids": ["a"],
            "reasons": [{"product_id": "a", "reason": "Fine."}],
            # Everything below is an invented fact with nowhere to land.
            "observed_price": "0.01",
            "observed_currency": "XXX",
            "product_url": "https://evil.example/buy",
            "image_url": "https://evil.example/i.png",
            "rating": 5,
            "availability": "InStock",
            "brand": "Fabricated",
            "retailer_domain": "evil.example",
            "products": [{"external_id": "injected", "title": "Injected", "observed_price": "1.00"}],
            "paypal_amount": "1.00",
        }
    )

    results = rank(ComparisonRequirements(), CANDIDATES, top_k=3, provider=fake)

    assert len(results) == 1
    served = results[0].product
    assert served is CANDIDATES[0]
    assert served.observed_price is None  # candidate "a" genuinely has no price
    assert served.observed_currency is None
    assert served.product_url is None
    assert served.brand is None
    # And the invented values appear nowhere in the serialized result.
    dumped = repr(results[0].model_dump())
    for invented in ("evil.example", "Fabricated", "injected", "XXX", "0.01"):
        assert invented not in dumped


def test_ranked_candidate_schema_has_no_price_or_url_field():
    # Structural guarantee: there is no field for the model to write into.
    fields = set(comparison_agent.RankedCandidate.model_fields)

    assert fields == {"rank", "product", "reason"}
