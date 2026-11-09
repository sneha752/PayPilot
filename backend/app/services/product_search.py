"""Deterministic product search over the local catalog, driven by ProductIntent.

This is the bridge between the Intent Agent's output and the catalog: it takes
a validated ProductIntent and returns the candidate products that satisfy every
HARD shopping constraint. Soft preferences (preferences.*) never filter here —
they are left to the Recommendation Agent for ranking.

SAFETY BOUNDARY: only SHOPPING constraints (intent.hard_constraints and
excluded_features) may filter products. Authorization fields are financial
data for the future Policy Engine; in particular, auto_approve_below_inr is a
spend-approval threshold and MUST NEVER be used as a product price cap. This
module therefore never reads intent.authorization at all.
"""

from __future__ import annotations

from ..schemas.intent import ProductIntent
from .product_service import Product, list_products


def _category_matches(product_category: str, wanted: str) -> bool:
    """Case-insensitive match tolerating singular/plural.

    The Intent Agent emits singular categories ("laptop") while the catalog
    uses plurals ("laptops"), so compare with a trailing 's' stripped too.
    """
    a = product_category.strip().lower()
    b = wanted.strip().lower()
    return a == b or a.rstrip("s") == b.rstrip("s")


def _spec_text(product: Product) -> str:
    """A searchable lowercase blob of the product's textual attributes.

    Used for required/excluded feature matching, so a requirement like
    "noise cancelling" or "RTX" can match spec fields, not just the
    description prose.
    """
    parts = [product.name, product.brand, product.description]
    for attr in ("processor", "gpu", "resolution", "panel_type", "form_factor"):
        value = getattr(product, attr)
        if value:
            parts.append(str(value))
    if product.wireless:
        parts.append("wireless")
    if product.noise_cancelling:
        parts.append("noise cancelling noise-cancelling")
    return " ".join(parts).lower()


def find_candidates(intent: ProductIntent) -> list[Product]:
    """Return catalog products satisfying every hard constraint in `intent`.

    Filters applied (AND semantics, all optional):
      - category (singular/plural tolerant)
      - hard_constraints.max_price_inr / min_price_inr (inclusive)
      - hard_constraints.min_rating
      - hard_constraints.ram_gb_min / storage_gb_min — products without the
        spec (e.g. headphones have no RAM) are excluded, since they cannot
        satisfy the requirement
      - hard_constraints.required_features (substring match on spec text)
      - excluded_features (product dropped on any match)

    Preferences and authorization fields are deliberately ignored.
    """
    hc = intent.hard_constraints
    results = list_products()

    if intent.category:
        results = [p for p in results if _category_matches(p.category, intent.category)]

    if hc.max_price_inr is not None:
        results = [p for p in results if p.price_inr <= hc.max_price_inr]

    if hc.min_price_inr is not None:
        results = [p for p in results if p.price_inr >= hc.min_price_inr]

    if hc.min_rating is not None:
        results = [p for p in results if p.rating >= hc.min_rating]

    if hc.ram_gb_min is not None:
        results = [
            p for p in results if p.ram_gb is not None and p.ram_gb >= hc.ram_gb_min
        ]

    if hc.storage_gb_min is not None:
        results = [
            p
            for p in results
            if p.storage_gb is not None and p.storage_gb >= hc.storage_gb_min
        ]

    for feature in hc.required_features:
        term = feature.strip().lower()
        if term:
            results = [p for p in results if term in _spec_text(p)]

    for feature in intent.excluded_features:
        term = feature.strip().lower()
        if term:
            results = [p for p in results if term not in _spec_text(p)]

    return results
