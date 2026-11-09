"""Comparison Agent: validated candidates -> AI ranking (Phase 10E).

Pipeline:
    (requirements, candidates, top_k)
        -> build a minimal, scrubbed candidate payload
        -> AIProvider.rank_products
        -> Pydantic validation (ProductRanking)
        -> candidate-membership / duplicate / count enforcement
        -> ranked candidate ids + reasons

GROUNDING IS ENFORCED HERE, not merely requested in the prompt:

  - The payload is validated into ProductRanking, which has no price,
    currency, URL, image, retailer, availability, rating, brand, or spec
    field. Extra keys the model invents are discarded by validation and can
    never become application state.
  - Every returned id must appear in the candidate set that was actually
    sent. An unknown id is a hard RankingValidationError: no product is
    created, and the hallucinated id is never accepted.
  - A duplicated id is a hard error (a ranking that repeats a candidate is
    not a ranking).
  - An empty ranking is a hard error: the candidates were already validated
    as eligible, so "nothing ranks" is unusable output, not a judgement.
  - Over-long rankings are truncated to top_k. The prefix of a validated
    ordering is still a validated ordering, so truncating loses nothing and
    keeps the backend in control of the final count.
  - Reasons are matched to ids. A reason for an id that is not in the final
    ranking is dropped; a missing reason is filled with a neutral
    deterministic fallback that makes no factual claim.

WHAT THE MODEL IS SENT
    external_id, title, description, brand, category, availability,
    observed_price (as an exact string), observed_currency,
    normalized_price / normalized_currency (the backend-DERIVED amount, when
    one exists — Phase 10F), retailer_domain.

    The model receives the derived amount as READY-MADE evidence precisely
    so it never has to convert anything. conversion_rate is deliberately
    withheld: ranking does not need it, and handing over a rate invites
    recomputation. Every conversion is the backend's arithmetic, and the
    ProductRanking schema gives the model no field to return a price in.

WHAT THE MODEL IS NEVER SENT
    PayPal credentials or amounts, order/capture/refund ids, transaction
    records, policy decisions, authorization thresholds, session ids or
    secrets, product_url, image_url, or observed_at. The requirements model
    (ComparisonRequirements) has no authorization block at all, so there is
    nothing financial to leak through it.

This agent never calls PayPal, the Policy Engine, the transaction ledger,
the catalog, or any discovery provider.
"""

from __future__ import annotations

import logging
from typing import Optional

from pydantic import BaseModel, ValidationError

from ..schemas.comparison import ComparisonRequirements, ProductRanking
from ..schemas.discovery import NormalizedDiscoveredProduct
from ..services.providers import AIProvider, get_provider

logger = logging.getLogger("paypilot.comparison_agent")

#: Used when the model ranks a candidate but supplies no reason for it. It
#: deliberately asserts nothing about the product beyond its membership in
#: the returned candidate set.
NEUTRAL_REASON = "Ranked among the returned candidates; no specific reason supplied."

#: The only discovery-record fields the ranking model ever sees. Everything
#: else (product_url, image_url, source, observed_at) is withheld as
#: unnecessary for ranking.
_AI_CANDIDATE_FIELDS = (
    "external_id",
    "title",
    "description",
    "brand",
    "category",
    "availability",
    "observed_price",
    "observed_currency",
    # Phase 10F: the backend's already-derived amount, so the model never
    # converts. conversion_rate / conversion_source / rate_observed_at are
    # intentionally absent.
    "normalized_price",
    "normalized_currency",
    "retailer_domain",
)


class RankingValidationError(Exception):
    """AI ranking payload failed validation or referenced a non-candidate id."""


class RankedCandidate(BaseModel):
    """One validated ranking entry: a real candidate plus the model's reason.

    `product` is the discovery object the caller supplied — the same
    instance, not a reconstruction — so no AI output can reach it, including
    its backend-derived normalized price.
    """

    rank: int
    product: NormalizedDiscoveredProduct
    reason: str


def build_ai_candidate_payload(
    candidates: list[NormalizedDiscoveredProduct],
) -> list[dict]:
    """Project candidates down to the minimal ranking payload.

    Prices are rendered as exact strings (Decimal -> str), so the model sees
    the values without any float rounding. Both are read-only evidence: the
    authoritative observed price AND the derived normalized price in the
    response always come from the backend record, never from the model.
    """
    payload = []
    for product in candidates:
        dumped = product.model_dump(mode="json", include=set(_AI_CANDIDATE_FIELDS))
        # Drop nulls so a missing field reads as absent rather than as a
        # value the model might feel invited to fill in.
        payload.append({k: v for k, v in dumped.items() if v is not None})
    return payload


def build_ai_requirements_payload(requirements: ComparisonRequirements) -> dict:
    """Serialize requirements for the model, omitting unset fields.

    ComparisonRequirements contains no authorization/payment fields by
    construction, so this is safe by schema rather than by filtering.
    """
    return requirements.model_dump(mode="json", exclude_none=True)


def rank(
    requirements: ComparisonRequirements,
    candidates: list[NormalizedDiscoveredProduct],
    top_k: int,
    provider: Optional[AIProvider] = None,
) -> list[RankedCandidate]:
    """Rank already-validated candidates via the configured AI provider.

    Args:
        requirements: The user's structured requirements (preference data).
        candidates: Deterministically filtered and capped discovery records.
            Must be non-empty — with nothing to rank, the caller should not
            spend an AI call.
        top_k: Maximum number of ranked entries to return (>= 1).
        provider: Optional explicit provider (for testing/DI).

    Returns:
        Validated RankedCandidate entries in the model's order, each holding
        the caller's own NormalizedDiscoveredProduct instance.

    Raises:
        ValueError: empty candidates or top_k < 1 (caller bug).
        AIConfigError / AIServiceError / InvalidAIResponseError: provider-level.
        RankingValidationError: payload invalid, or it named an unknown id,
            repeated an id, or ranked nothing.
    """
    if not candidates:
        raise ValueError("rank() requires at least one candidate product")
    if top_k < 1:
        raise ValueError("top_k must be at least 1")

    provider = provider or get_provider()

    payload = provider.rank_products(
        build_ai_requirements_payload(requirements),
        build_ai_candidate_payload(candidates),
        top_k,
    )

    try:
        ranking = ProductRanking(**payload)
    except (ValidationError, TypeError) as exc:
        logger.error("Ranking payload failed validation")
        raise RankingValidationError(
            "AI provider returned a ranking that failed validation"
        ) from exc

    # Membership check against the exact set that was sent. Matching is
    # case-insensitive on the way in, then canonicalized to the backend's
    # spelling so nothing downstream depends on the model's formatting.
    by_id = {p.external_id.strip().casefold(): p for p in candidates}

    seen: set[str] = set()
    ordered_keys: list[str] = []
    for raw_id in ranking.ranked_ids:
        key = (raw_id or "").strip().casefold()
        if key not in by_id:
            # Never create a product for an id we did not supply.
            logger.error(
                "AI ranked id %r which is not among the %d candidates",
                raw_id,
                len(candidates),
            )
            raise RankingValidationError(
                "AI ranked a product that is not in the candidate list"
            )
        if key in seen:
            logger.error("AI ranking repeated candidate id %r", raw_id)
            raise RankingValidationError("AI ranking contained a duplicate product id")
        seen.add(key)
        ordered_keys.append(key)

    if not ordered_keys:
        logger.error("AI ranking contained no candidate ids")
        raise RankingValidationError("AI ranking contained no candidate ids")

    if len(ordered_keys) > top_k:
        # A validated ordering's prefix is still a validated ordering, so
        # truncation is safe; the backend owns the final count.
        logger.warning(
            "AI returned %d ranked id(s) for top_k=%d; truncating",
            len(ordered_keys),
            top_k,
        )
        ordered_keys = ordered_keys[:top_k]

    # Reasons are only honored for ids that survived validation; anything
    # else (including a reason for an unknown id) is discarded as prose with
    # nothing to attach to.
    reasons: dict[str, str] = {}
    for entry in ranking.reasons:
        key = (entry.product_id or "").strip().casefold()
        if key in seen and entry.reason and entry.reason.strip():
            reasons.setdefault(key, entry.reason.strip())

    results = [
        RankedCandidate(
            rank=position,
            product=by_id[key],
            reason=reasons.get(key, NEUTRAL_REASON),
        )
        for position, key in enumerate(ordered_keys, start=1)
    ]

    logger.info(
        "AI ranking via %s: %d result(s) from %d candidate(s) (top_k=%d)",
        provider.name,
        len(results),
        len(candidates),
        top_k,
    )
    return results
