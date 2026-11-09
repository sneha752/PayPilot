"""Unit tests for the deterministic product search layer.

find_candidates() is pure and local — no AI, no network — so these tests
assert exact candidate sets against the known data/products.json catalog.
"""

from app.schemas.intent import (
    Authorization,
    HardConstraints,
    ProductIntent,
)
from app.services.product_search import find_candidates


def _intent(**kwargs) -> ProductIntent:
    return ProductIntent(**kwargs)


def _ids(products) -> set[str]:
    return {p.id for p in products}


# --- Category --------------------------------------------------------------


def test_singular_category_matches_plural_catalog():
    # Intent Agent emits "laptop"; the catalog category is "laptops".
    results = find_candidates(_intent(category="laptop"))
    assert _ids(results) == {"LAP001", "LAP002", "LAP003", "LAP004", "LAP005", "LAP006"}


def test_no_category_searches_whole_catalog():
    results = find_candidates(_intent())
    assert len(results) == 30  # full catalog


# --- Price bounds ----------------------------------------------------------


def test_max_price_is_inclusive_upper_bound():
    results = find_candidates(
        _intent(category="laptops", hard_constraints=HardConstraints(max_price_inr=80000))
    )
    assert _ids(results) == {"LAP002", "LAP006"}
    assert all(p.price_inr <= 80000 for p in results)


def test_min_price_filters_low_end():
    results = find_candidates(
        _intent(category="laptops", hard_constraints=HardConstraints(min_price_inr=100000))
    )
    assert _ids(results) == {"LAP001", "LAP003", "LAP004", "LAP005"}


# --- Rating / specs ---------------------------------------------------------


def test_min_rating_filter():
    results = find_candidates(
        _intent(category="laptops", hard_constraints=HardConstraints(min_rating=4.5))
    )
    assert _ids(results) == {"LAP001", "LAP003", "LAP004", "LAP005"}


def test_min_ram_excludes_lower_and_specless_products():
    results = find_candidates(_intent(hard_constraints=HardConstraints(ram_gb_min=16)))
    # Only 16GB laptops qualify; headphones/monitors (no ram_gb) are excluded.
    assert _ids(results) == {"LAP001", "LAP002", "LAP003", "LAP005"}


def test_min_storage_filter():
    results = find_candidates(
        _intent(category="laptops", hard_constraints=HardConstraints(storage_gb_min=1024))
    )
    assert _ids(results) == {"LAP003", "LAP005"}


# --- Features ---------------------------------------------------------------


def test_required_feature_matches_spec_fields():
    results = find_candidates(
        _intent(
            category="headphones",
            hard_constraints=HardConstraints(required_features=["noise cancelling"]),
        )
    )
    # boAt Rockerz 450 has noise_cancelling=false and no such text.
    assert "HDP006" not in _ids(results)
    assert "HDP001" in _ids(results)


def test_required_feature_rtx_matches_gpu_field():
    results = find_candidates(
        _intent(category="laptops", hard_constraints=HardConstraints(required_features=["RTX"]))
    )
    assert _ids(results) == {"LAP005"}


def test_excluded_feature_drops_matches():
    results = find_candidates(
        _intent(category="laptops", excluded_features=["Apple"])
    )
    assert "LAP004" not in _ids(results)
    assert len(results) == 5


# --- Authorization must never filter -----------------------------------------


def test_auto_approve_threshold_does_not_cap_price():
    # Budget 80000, autonomous-approval threshold 50000: products between
    # 50000 and 80000 MUST still be returned.
    intent = _intent(
        category="laptops",
        hard_constraints=HardConstraints(max_price_inr=80000),
        authorization=Authorization(
            purchase_requested=True,
            autonomous_purchase_allowed=True,
            auto_approve_below_inr=50000,
        ),
    )
    results = find_candidates(intent)
    assert "LAP002" in _ids(results)  # 68,990 > 50,000 and <= 80,000
    assert _ids(results) == {"LAP002", "LAP006"}


def test_authorization_alone_filters_nothing():
    # Identical shopping constraints with and without authorization data
    # must yield identical candidates.
    base = _intent(category="laptops")
    with_auth = _intent(
        category="laptops",
        authorization=Authorization(
            purchase_requested=True,
            autonomous_purchase_allowed=True,
            auto_approve_below_inr=1,  # absurdly low — must change nothing
        ),
    )
    assert _ids(find_candidates(base)) == _ids(find_candidates(with_auth))
