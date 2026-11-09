"""Deterministic candidate filtering and reduction (Phase 10E).

This module is the deterministic half of discovery comparison. It takes
normalized DiscoveredProduct records plus structured ComparisonRequirements
and decides which candidates are eligible, and in what stable order.

It contains NO intelligence and NO network calls. By construction it never:
  - calls an LLM or any AI provider
  - calls PayPal, the Policy Engine, the transaction ledger, or the catalog
  - converts currency or computes a payment amount
  - mutates a DiscoveredProduct (records are passed through by reference,
    never copied-with-changes and never re-priced)
  - infers a missing attribute (no brand from title, no spec from a
    description, nothing from an image)

DOCUMENTED FILTER SEMANTICS
===========================

PRICE  (currency-aware since Phase 10F)
  Applied only when the requirements carry a bound (max_price/min_price);
  the requirements schema guarantees a price_currency accompanies any bound.

  The amount compared is resolved by discovery_currency_service.
  comparable_amount, which takes, in order:
    1. observed_price, when observed_currency already IS the requirement
       currency (an exact observation always wins);
    2. normalized_price, when the backend DERIVED one in that currency
       (Phase 10F — this is what lets an INR budget admit a USD candidate);
    3. nothing, in which case the candidate is EXCLUDED.

  Consequences, deliberate and honest:
    - unknown price                    -> EXCLUDED under a price constraint
    - unknown currency                 -> EXCLUDED under a price constraint
    - other currency, derivable        -> COMPARED via normalized_price
    - other currency, not derivable    -> EXCLUDED under a price constraint
  A missing price is never treated as zero and never as infinity. USD 34.99
  is NEVER compared against an INR 3000 budget directly — only a
  backend-derived INR amount is. No AI performs any conversion, and the
  PayPal demo rate is never consulted. With no price constraint,
  unknown-price candidates remain eligible (unchanged from Phase 10E).

BRAND
  Applied only when requirements.brand is set. A candidate passes only if it
  carries a non-blank brand that matches after trim + case folding. A
  candidate with no reliable brand is EXCLUDED from a brand-constrained set.
  Brand is never guessed from the title.

CATEGORY
  Applied only when requirements.category is set AND the candidate actually
  carries a non-blank category, in which case it must match after trim +
  case folding. A candidate with NO category is KEPT: provider category
  vocabularies differ (Serper supplies none at all), and this phase does not
  translate slugs across providers. This asymmetry with brand is intentional
  and tested.

KEYWORDS
  Matched as case-insensitive substrings against the candidate's searchable
  text, which is title plus description where a description actually exists
  (whitespace collapsed). Multi-word phrases must appear as a phrase. Every
  required_keyword must be present; any excluded_keyword present rejects the
  candidate. No semantic similarity - that is the AI ranking layer's job.

DETERMINISTIC ORDERING AND REDUCTION
====================================

Survivors are sorted by a total, data-only key.

WITH a target currency (a price constraint exists), amounts are already
commensurable, so the key is:

  1. candidates with a comparable amount, before those without
  2. then by that comparable amount ascending (observed when the currency
     already matches, otherwise the backend-derived normalized amount)
  3. then by source, then external_id (stable final tiebreak)

WITHOUT a target currency there is nothing to make amounts commensurable,
so the Phase 10E key is kept unchanged:

  1. priced-and-currency-known candidates before unknown-price candidates
  2. then by observed_currency (so amounts are only ever ordered WITHIN one
     currency - this layer never implies a cross-currency comparison)
  3. then by observed_price ascending
  4. then by source, then external_id

The top MAX_AI_CANDIDATES are what the AI is allowed to see.

This ordering is NOT a quality judgement and is not called "best". It is a
reproducible reduction step over trustworthy observed fields only.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Optional

from ..schemas.comparison import ComparisonRequirements
from ..schemas.discovery import DiscoveredProduct
from .discovery_currency_service import comparable_amount

logger = logging.getLogger("paypilot.discovery_filter")

#: Hard ceiling on how many candidates may be handed to the AI ranker. The
#: model is never asked to choose from hundreds of results.
MAX_AI_CANDIDATES = 10

_WHITESPACE = re.compile(r"\s+")


def _normalize_text(value: Optional[str]) -> str:
    """Trim, case-fold and collapse whitespace. None/blank -> ""."""
    if not value:
        return ""
    return _WHITESPACE.sub(" ", value).strip().casefold()


def _searchable_text(product: DiscoveredProduct) -> str:
    """Title plus description, but only where each actually exists."""
    parts = [product.title, product.description]
    return " ".join(_normalize_text(p) for p in parts if p and p.strip()).strip()


def _passes_price(
    product: DiscoveredProduct, requirements: ComparisonRequirements
) -> bool:
    if not requirements.has_price_constraint:
        # No constraint: an unknown price is not a disqualification.
        return True

    # Resolves to an exact observation in the requirement currency, or to a
    # backend-DERIVED amount in it, or to nothing at all. Cross-currency
    # amounts are never compared directly.
    price = comparable_amount(product, requirements.price_currency)
    if price is None:
        # Unknown, unconvertible, or incompatible price under a strict
        # bound. Not fabricated as 0, not fabricated as infinity - simply
        # not comparable.
        return False

    if requirements.max_price is not None and price > requirements.max_price:
        return False
    if requirements.min_price is not None and price < requirements.min_price:
        return False
    return True


def _passes_brand(
    product: DiscoveredProduct, requirements: ComparisonRequirements
) -> bool:
    if requirements.brand is None:
        return True
    candidate_brand = _normalize_text(product.brand)
    if not candidate_brand:
        # Required brand, but no reliable brand on the record. Excluded
        # rather than guessed from the title.
        return False
    return candidate_brand == _normalize_text(requirements.brand)


def _passes_category(
    product: DiscoveredProduct, requirements: ComparisonRequirements
) -> bool:
    if requirements.category is None:
        return True
    candidate_category = _normalize_text(product.category)
    if not candidate_category:
        # No reliable category to filter on; providers differ and slugs are
        # not translated in this phase, so the candidate survives.
        return True
    return candidate_category == _normalize_text(requirements.category)


def _passes_keywords(
    product: DiscoveredProduct, requirements: ComparisonRequirements
) -> bool:
    if not requirements.required_keywords and not requirements.excluded_keywords:
        return True

    text = _searchable_text(product)

    for keyword in requirements.required_keywords:
        if _normalize_text(keyword) not in text:
            return False
    for keyword in requirements.excluded_keywords:
        if _normalize_text(keyword) in text:
            return False
    return True


def filter_candidates(
    products: list[DiscoveredProduct], requirements: ComparisonRequirements
) -> list[DiscoveredProduct]:
    """Return the candidates that satisfy every hard requirement.

    Input order is preserved; records are returned by reference, unmodified.
    Soft fields (preferred_brand, preferred_keywords, use_case) are ignored
    here by design - they are ranking signals, not filters.
    """
    survivors = [
        p
        for p in products
        if _passes_price(p, requirements)
        and _passes_brand(p, requirements)
        and _passes_category(p, requirements)
        and _passes_keywords(p, requirements)
    ]

    logger.info(
        "Deterministic filter kept %d/%d candidate(s) "
        "(price_constraint=%s brand=%s category=%s required_kw=%d excluded_kw=%d)",
        len(survivors),
        len(products),
        requirements.has_price_constraint,
        requirements.brand is not None,
        requirements.category is not None,
        len(requirements.required_keywords),
        len(requirements.excluded_keywords),
    )
    return survivors


def _ordering_key(
    product: DiscoveredProduct, currency: Optional[str]
) -> tuple[int, str, Decimal, str, str]:
    """Total, reproducible sort key built only from price-bearing fields.

    With a target currency, amounts are commensurable (observed when the
    currency matches, else backend-derived) and are ordered directly. The
    currency slot is held constant because grouping by observed currency
    would otherwise override the amounts the normalization made comparable.

    With no target currency, the Phase 10E key is reproduced exactly:
    priced first, grouped by observed currency, then by observed amount.
    """
    if currency is not None:
        amount = comparable_amount(product, currency)
        return (
            0 if amount is not None else 1,
            "",
            amount if amount is not None else Decimal(0),
            product.source or "",
            product.external_id or "",
        )

    priced = product.observed_price is not None and bool(product.observed_currency)
    return (
        0 if priced else 1,
        (product.observed_currency or "").strip().upper(),
        product.observed_price if product.observed_price is not None else Decimal(0),
        product.source or "",
        product.external_id or "",
    )


def order_candidates(
    products: list[DiscoveredProduct], requirements: ComparisonRequirements
) -> list[DiscoveredProduct]:
    """Sort candidates by the documented deterministic ordering.

    Not a quality ranking: see the module docstring. Returns a new list; the
    product records themselves are untouched.
    """
    currency = requirements.price_currency
    return sorted(products, key=lambda p: _ordering_key(p, currency))


def reduce_candidates(
    products: list[DiscoveredProduct],
    requirements: ComparisonRequirements,
    max_candidates: int = MAX_AI_CANDIDATES,
) -> list[DiscoveredProduct]:
    """Order candidates deterministically and cap the list for AI ranking."""
    if max_candidates < 1:
        raise ValueError("max_candidates must be at least 1")

    ordered = order_candidates(products, requirements)
    if len(ordered) > max_candidates:
        logger.info(
            "Reducing %d candidate(s) to the deterministic cap of %d",
            len(ordered),
            max_candidates,
        )
    return ordered[:max_candidates]


def select_candidates(
    products: list[DiscoveredProduct],
    requirements: ComparisonRequirements,
    max_candidates: int = MAX_AI_CANDIDATES,
) -> list[DiscoveredProduct]:
    """filter -> order -> cap, the full deterministic candidate pipeline."""
    return reduce_candidates(
        filter_candidates(products, requirements), requirements, max_candidates
    )
