"""Deterministic candidate filtering tests (Phase 10E).

Pure unit tests: no HTTP, no AI provider, no discovery provider. The filter
service is asserted to be deterministic, non-mutating, and honest about
missing data (nothing is inferred, no price is fabricated, no currency is
converted).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.comparison import (
    ComparisonRequirements,
    requirements_from_intent,
)
from app.schemas.discovery import DiscoveredProduct
from app.schemas.intent import (
    Authorization,
    HardConstraints,
    Preferences,
    ProductIntent,
)
from app.services import discovery_filter_service as dfs

OBSERVED_AT = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def product(
    external_id="p1",
    *,
    title="Wireless Headphones",
    description=None,
    brand=None,
    category=None,
    price=None,
    currency=None,
    source="channel3",
    availability=None,
) -> DiscoveredProduct:
    return DiscoveredProduct(
        external_id=external_id,
        source=source,
        title=title,
        description=description,
        brand=brand,
        category=category,
        availability=availability,
        observed_price=Decimal(price) if price is not None else None,
        observed_currency=currency,
        observed_at=OBSERVED_AT,
    )


def ids(products):
    return [p.external_id for p in products]


# ---------------------------------------------------------------------------
# 1-2. Price bounds
# ---------------------------------------------------------------------------


def test_max_price_filter_keeps_only_candidates_at_or_below_the_bound():
    products = [
        product("cheap", price="100", currency="USD"),
        product("exact", price="150", currency="USD"),
        product("dear", price="151", currency="USD"),
    ]
    reqs = ComparisonRequirements(max_price=Decimal("150"), price_currency="USD")

    assert ids(dfs.filter_candidates(products, reqs)) == ["cheap", "exact"]


def test_min_price_filter_keeps_only_candidates_at_or_above_the_bound():
    products = [
        product("low", price="99", currency="USD"),
        product("exact", price="100", currency="USD"),
        product("high", price="500", currency="USD"),
    ]
    reqs = ComparisonRequirements(min_price=Decimal("100"), price_currency="USD")

    assert ids(dfs.filter_candidates(products, reqs)) == ["exact", "high"]


def test_min_and_max_price_bound_a_range():
    products = [
        product("under", price="50", currency="USD"),
        product("inside", price="150", currency="USD"),
        product("over", price="400", currency="USD"),
    ]
    reqs = ComparisonRequirements(
        min_price=Decimal("100"), max_price=Decimal("200"), price_currency="USD"
    )

    assert ids(dfs.filter_candidates(products, reqs)) == ["inside"]


def test_price_comparison_is_exact_decimal_not_float():
    # 0.1 + 0.2 float arithmetic would misjudge this boundary.
    products = [product("p", price="0.30000000000000004", currency="USD")]
    reqs = ComparisonRequirements(max_price=Decimal("0.3"), price_currency="USD")

    assert dfs.filter_candidates(products, reqs) == []


# ---------------------------------------------------------------------------
# 3. Currency compatibility (no conversion in this phase)
# ---------------------------------------------------------------------------


def test_currency_mismatch_is_excluded_rather_than_converted():
    products = [
        product("usd", price="100", currency="USD"),
        product("inr", price="100", currency="INR"),
    ]
    reqs = ComparisonRequirements(max_price=Decimal("200"), price_currency="USD")

    # The INR candidate is numerically under the bound, but 100 INR is not
    # 100 USD; it is excluded, never converted.
    assert ids(dfs.filter_candidates(products, reqs)) == ["usd"]


def test_currency_comparison_is_case_and_whitespace_insensitive():
    products = [product("p", price="100", currency=" usd ")]
    reqs = ComparisonRequirements(max_price=Decimal("200"), price_currency="usd")

    assert ids(dfs.filter_candidates(products, reqs)) == ["p"]


def test_price_bound_without_currency_is_a_validation_error():
    with pytest.raises(ValidationError):
        ComparisonRequirements(max_price=Decimal("100"))


def test_max_price_below_min_price_is_a_validation_error():
    with pytest.raises(ValidationError):
        ComparisonRequirements(
            min_price=Decimal("200"), max_price=Decimal("100"), price_currency="USD"
        )


# ---------------------------------------------------------------------------
# 4. Unknown price behavior
# ---------------------------------------------------------------------------


def test_unknown_price_is_excluded_under_a_price_constraint():
    products = [
        product("priced", price="100", currency="USD"),
        product("no_price"),
        product("price_without_currency", price="100", currency=None),
    ]
    reqs = ComparisonRequirements(max_price=Decimal("200"), price_currency="USD")

    assert ids(dfs.filter_candidates(products, reqs)) == ["priced"]


def test_unknown_price_is_not_treated_as_zero():
    # Treating a missing price as 0 would let it pass a min_price bound.
    products = [product("no_price")]
    reqs = ComparisonRequirements(min_price=Decimal("0"), price_currency="USD")

    assert dfs.filter_candidates(products, reqs) == []


def test_unknown_price_is_not_treated_as_infinity_for_a_minimum():
    products = [product("no_price")]
    reqs = ComparisonRequirements(min_price=Decimal("1"), price_currency="USD")

    assert dfs.filter_candidates(products, reqs) == []


def test_unknown_price_survives_when_there_is_no_price_constraint():
    products = [product("no_price"), product("priced", price="100", currency="USD")]

    survivors = dfs.filter_candidates(products, ComparisonRequirements())

    assert set(ids(survivors)) == {"no_price", "priced"}


# ---------------------------------------------------------------------------
# 5. Brand
# ---------------------------------------------------------------------------


def test_brand_filter_matches_after_trim_and_case_normalization():
    products = [
        product("sony", brand="  sOnY "),
        product("bose", brand="Bose"),
    ]
    reqs = ComparisonRequirements(brand="Sony")

    assert ids(dfs.filter_candidates(products, reqs)) == ["sony"]


def test_candidate_without_a_brand_is_excluded_when_brand_is_required():
    products = [product("nobrand", title="Sony WH-1000XM5", brand=None)]
    reqs = ComparisonRequirements(brand="Sony")

    # The title says Sony, but brand is never inferred from a title.
    assert dfs.filter_candidates(products, reqs) == []


def test_blank_brand_counts_as_no_brand():
    products = [product("blank", brand="   ")]

    assert dfs.filter_candidates(products, ComparisonRequirements(brand="Sony")) == []


def test_brand_is_ignored_when_not_required():
    products = [product("a", brand="Sony"), product("b", brand=None)]

    assert len(dfs.filter_candidates(products, ComparisonRequirements())) == 2


def test_preferred_brand_never_filters():
    products = [product("a", brand="Bose"), product("b", brand=None)]
    reqs = ComparisonRequirements(preferred_brand="Sony")

    # A soft preference is a ranking signal only.
    assert len(dfs.filter_candidates(products, reqs)) == 2


# ---------------------------------------------------------------------------
# 6. Category
# ---------------------------------------------------------------------------


def test_category_filter_excludes_a_mismatched_category():
    products = [
        product("match", category="Headphones"),
        product("other", category="laptops"),
    ]
    reqs = ComparisonRequirements(category="headphones")

    assert ids(dfs.filter_candidates(products, reqs)) == ["match"]


def test_candidate_without_a_category_survives_a_category_requirement():
    # Documented asymmetry with brand: provider category vocabularies differ
    # (Serper supplies none), and slugs are not translated in this phase.
    products = [product("nocat", category=None), product("blank", category="  ")]
    reqs = ComparisonRequirements(category="headphones")

    assert set(ids(dfs.filter_candidates(products, reqs))) == {"nocat", "blank"}


def test_category_is_ignored_when_not_required():
    products = [product("a", category="laptops")]

    assert len(dfs.filter_candidates(products, ComparisonRequirements())) == 1


# ---------------------------------------------------------------------------
# 7. Keywords
# ---------------------------------------------------------------------------


def test_required_keyword_must_appear_in_title_or_description():
    products = [
        product("title_hit", title="Noise Cancelling Headphones"),
        product("desc_hit", title="Headphones", description="With noise cancelling."),
        product("miss", title="Wired Earbuds", description="Basic."),
    ]
    reqs = ComparisonRequirements(required_keywords=["noise cancelling"])

    assert set(ids(dfs.filter_candidates(products, reqs))) == {"title_hit", "desc_hit"}


def test_all_required_keywords_must_be_present():
    products = [
        product("both", title="Wireless Noise Cancelling Headphones"),
        product("one", title="Wireless Headphones"),
    ]
    reqs = ComparisonRequirements(required_keywords=["wireless", "noise cancelling"])

    assert ids(dfs.filter_candidates(products, reqs)) == ["both"]


def test_keyword_matching_is_case_insensitive_and_whitespace_collapsed():
    products = [product("p", title="WIRELESS   Noise\tCancelling")]
    reqs = ComparisonRequirements(required_keywords=["wireless noise cancelling"])

    assert ids(dfs.filter_candidates(products, reqs)) == ["p"]


def test_excluded_keyword_rejects_a_candidate():
    products = [
        product("refurb", title="Headphones (Refurbished)"),
        product("new", title="Headphones"),
    ]
    reqs = ComparisonRequirements(excluded_keywords=["refurbished"])

    assert ids(dfs.filter_candidates(products, reqs)) == ["new"]


def test_excluded_keyword_also_matches_the_description():
    products = [product("p", title="Headphones", description="Renewed unit.")]
    reqs = ComparisonRequirements(excluded_keywords=["renewed"])

    assert dfs.filter_candidates(products, reqs) == []


def test_blank_keywords_are_dropped_and_never_match_everything():
    reqs = ComparisonRequirements(required_keywords=["", "   "], excluded_keywords=["  "])

    assert reqs.required_keywords == []
    assert reqs.excluded_keywords == []
    assert len(dfs.filter_candidates([product("p")], reqs)) == 1


def test_missing_description_does_not_crash_keyword_matching():
    products = [product("p", title="Headphones", description=None)]
    reqs = ComparisonRequirements(required_keywords=["headphones"])

    assert ids(dfs.filter_candidates(products, reqs)) == ["p"]


def test_preferred_keywords_never_filter():
    products = [product("p", title="Headphones")]
    reqs = ComparisonRequirements(preferred_keywords=["anc", "foldable"])

    assert len(dfs.filter_candidates(products, reqs)) == 1


# ---------------------------------------------------------------------------
# 8. Missing optional fields / combined requirements
# ---------------------------------------------------------------------------


def test_candidate_with_every_optional_field_missing_passes_empty_requirements():
    bare = DiscoveredProduct(
        external_id="bare", source="serper", title="Thing", observed_at=OBSERVED_AT
    )

    assert dfs.filter_candidates([bare], ComparisonRequirements()) == [bare]


def test_empty_requirements_keep_everything_in_input_order():
    products = [product("c"), product("a"), product("b")]

    assert ids(dfs.filter_candidates(products, ComparisonRequirements())) == [
        "c",
        "a",
        "b",
    ]


def test_all_requirements_combine_conjunctively():
    products = [
        product(
            "winner",
            title="Sony Wireless Headphones",
            description="Noise cancelling.",
            brand="Sony",
            category="headphones",
            price="150",
            currency="USD",
        ),
        product(
            "wrong_brand",
            title="Bose Wireless Headphones",
            description="Noise cancelling.",
            brand="Bose",
            category="headphones",
            price="150",
            currency="USD",
        ),
        product(
            "too_dear",
            title="Sony Wireless Headphones",
            description="Noise cancelling.",
            brand="Sony",
            category="headphones",
            price="900",
            currency="USD",
        ),
        product(
            "excluded_word",
            title="Sony Wireless Headphones (Refurbished)",
            description="Noise cancelling.",
            brand="Sony",
            category="headphones",
            price="120",
            currency="USD",
        ),
    ]
    reqs = ComparisonRequirements(
        category="headphones",
        brand="Sony",
        max_price=Decimal("200"),
        price_currency="USD",
        required_keywords=["noise cancelling"],
        excluded_keywords=["refurbished"],
    )

    assert ids(dfs.filter_candidates(products, reqs)) == ["winner"]


def test_filtering_does_not_mutate_candidate_records():
    original = product("p", price="100", currency="USD", brand="Sony")
    snapshot = original.model_dump()
    reqs = ComparisonRequirements(max_price=Decimal("200"), price_currency="USD")

    survivors = dfs.filter_candidates([original], reqs)

    assert survivors[0] is original  # by reference, never rebuilt
    assert original.model_dump() == snapshot


# ---------------------------------------------------------------------------
# 9. Deterministic ordering
# ---------------------------------------------------------------------------


def test_ordering_puts_lower_observed_price_first():
    products = [
        product("mid", price="200", currency="USD"),
        product("low", price="100", currency="USD"),
        product("high", price="300", currency="USD"),
    ]
    reqs = ComparisonRequirements(max_price=Decimal("1000"), price_currency="USD")

    assert ids(dfs.order_candidates(products, reqs)) == ["low", "mid", "high"]


def test_ordering_puts_priced_candidates_before_unknown_price():
    products = [
        product("unknown"),
        product("priced", price="500", currency="USD"),
    ]

    assert ids(dfs.order_candidates(products, ComparisonRequirements())) == [
        "priced",
        "unknown",
    ]


def test_ordering_groups_by_currency_so_amounts_never_cross_currencies():
    # 100 INR must not be judged "cheaper" than 5 USD; amounts only compete
    # within one currency.
    products = [
        product("usd_high", price="900", currency="USD"),
        product("inr_low", price="100", currency="INR"),
    ]

    ordered = ids(dfs.order_candidates(products, ComparisonRequirements()))

    assert ordered == ["inr_low", "usd_high"]  # grouped by currency, not amount


def test_ordering_breaks_ties_on_source_then_external_id():
    products = [
        product("zeta", price="100", currency="USD", source="channel3"),
        product("alpha", price="100", currency="USD", source="channel3"),
        product("mid", price="100", currency="USD", source="aaa"),
    ]

    assert ids(dfs.order_candidates(products, ComparisonRequirements())) == [
        "mid",
        "alpha",
        "zeta",
    ]


def test_ordering_is_stable_across_input_permutations():
    base = [
        product("a", price="300", currency="USD"),
        product("b", price="100", currency="USD"),
        product("c", price="200", currency="USD"),
        product("d"),
    ]
    reqs = ComparisonRequirements()

    first = ids(dfs.order_candidates(base, reqs))
    reversed_order = ids(dfs.order_candidates(list(reversed(base)), reqs))
    rotated = ids(dfs.order_candidates(base[2:] + base[:2], reqs))

    assert first == reversed_order == rotated == ["b", "c", "a", "d"]


def test_ordering_returns_a_new_list_and_leaves_the_input_untouched():
    products = [product("b", price="200", currency="USD"), product("a", price="100", currency="USD")]

    ordered = dfs.order_candidates(products, ComparisonRequirements())

    assert ids(products) == ["b", "a"]
    assert ordered is not products


# ---------------------------------------------------------------------------
# 10. Candidate cap
# ---------------------------------------------------------------------------


def test_max_ai_candidates_default_is_ten():
    assert dfs.MAX_AI_CANDIDATES == 10


def test_reduce_caps_the_candidate_count_at_the_default():
    products = [
        product(f"p{i:02d}", price=str(100 + i), currency="USD") for i in range(25)
    ]

    reduced = dfs.reduce_candidates(products, ComparisonRequirements())

    assert len(reduced) == dfs.MAX_AI_CANDIDATES


def test_reduce_keeps_the_deterministically_best_candidates():
    products = [
        product(f"p{i:02d}", price=str(500 - i), currency="USD") for i in range(15)
    ]
    reqs = ComparisonRequirements(max_price=Decimal("1000"), price_currency="USD")

    reduced = dfs.reduce_candidates(products, reqs, max_candidates=3)

    # Lowest observed prices are 486, 487, 488 (p14, p13, p12).
    assert ids(reduced) == ["p14", "p13", "p12"]


def test_reduce_is_a_no_op_below_the_cap():
    products = [product("a", price="1", currency="USD")]

    assert ids(dfs.reduce_candidates(products, ComparisonRequirements())) == ["a"]


def test_reduce_rejects_a_nonsensical_cap():
    with pytest.raises(ValueError):
        dfs.reduce_candidates([product("a")], ComparisonRequirements(), max_candidates=0)


def test_select_candidates_filters_then_orders_then_caps():
    products = [
        product("over", price="900", currency="USD"),
        product("c", price="300", currency="USD"),
        product("a", price="100", currency="USD"),
        product("b", price="200", currency="USD"),
    ]
    reqs = ComparisonRequirements(max_price=Decimal("500"), price_currency="USD")

    assert ids(dfs.select_candidates(products, reqs, max_candidates=2)) == ["a", "b"]


def test_select_candidates_on_an_empty_input_is_empty():
    assert dfs.select_candidates([], ComparisonRequirements()) == []


# ---------------------------------------------------------------------------
# Requirement derivation from intent
# ---------------------------------------------------------------------------


def test_requirements_from_intent_maps_hard_constraints():
    intent = ProductIntent(
        category="headphones",
        use_case="travel",
        hard_constraints=HardConstraints(
            max_price_inr=3000,
            min_price_inr=1000,
            required_features=["noise cancelling"],
        ),
        excluded_features=["refurbished"],
    )

    reqs = requirements_from_intent(intent)

    assert reqs.category == "headphones"
    assert reqs.max_price == Decimal("3000")
    assert reqs.min_price == Decimal("1000")
    assert reqs.price_currency == "INR"
    assert reqs.required_keywords == ["noise cancelling"]
    assert reqs.excluded_keywords == ["refurbished"]
    assert reqs.use_case == "travel"


def test_requirements_from_intent_treats_brand_preference_as_soft():
    intent = ProductIntent(preferences=Preferences(brand="Sony", gpu="RTX"))

    reqs = requirements_from_intent(intent)

    assert reqs.brand is None  # a soft preference never becomes a hard filter
    assert reqs.preferred_brand == "Sony"
    assert "RTX" in reqs.preferred_keywords


def test_requirements_from_intent_drops_the_authorization_block_entirely():
    intent = ProductIntent(
        hard_constraints=HardConstraints(max_price_inr=3000),
        authorization=Authorization(
            purchase_requested=True,
            autonomous_purchase_allowed=True,
            auto_approve_below_inr=50000,
        ),
    )

    reqs = requirements_from_intent(intent)
    dumped = reqs.model_dump()

    # The spend ceiling must not appear anywhere, and must never have become
    # a price bound.
    assert "authorization" not in dumped
    assert "auto_approve_below_inr" not in dumped
    assert reqs.max_price == Decimal("3000")
    assert Decimal("50000") not in [v for v in dumped.values() if isinstance(v, Decimal)]


def test_requirements_from_intent_sets_no_currency_without_a_price_bound():
    reqs = requirements_from_intent(ProductIntent(category="headphones"))

    assert reqs.price_currency is None
    assert reqs.has_price_constraint is False


def test_requirements_from_intent_does_not_map_rating_or_specs():
    # DiscoveredProduct has no rating/RAM/storage fields; mapping them would
    # invite inference from titles.
    intent = ProductIntent(
        hard_constraints=HardConstraints(min_rating=4.5, ram_gb_min=16, storage_gb_min=512)
    )

    reqs = requirements_from_intent(intent)

    assert reqs.required_keywords == []
    assert reqs.model_dump(exclude_none=True) == {
        "required_keywords": [],
        "excluded_keywords": [],
        "preferred_keywords": [],
    }


def test_requirements_forbid_unknown_fields():
    # A client cannot smuggle payment-authoritative data in as a requirement.
    for smuggled in (
        {"paypal_amount": "1.00"},
        {"capture_id": "CAP-1"},
        {"refund_id": "REF-1"},
        {"observed_price": "1.00"},
        {"product": {"external_id": "x"}},
    ):
        with pytest.raises(ValidationError):
            ComparisonRequirements(**smuggled)
