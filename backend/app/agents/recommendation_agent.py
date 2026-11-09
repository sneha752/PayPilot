"""Recommendation Agent: ProductIntent + candidate products -> Recommendation.

Pipeline:
    (intent, candidates) -> AIProvider.recommend_product -> Pydantic validation
    -> candidate-membership check.

Grounding is enforced here, not merely requested in the prompt:
  - The provider payload is validated into the Recommendation schema, which
    has no price/spec fields — extra keys the model invents are discarded.
  - recommended_product_id must match a product in the supplied candidate
    list, else RecommendationValidationError. The model can therefore never
    smuggle in a product the deterministic search did not return.

This agent only sees the SHOPPING portion of the intent: the authorization
block is stripped before the provider call. It never evaluates spending
thresholds (future Policy Engine) and never calls PayPal.
"""

from __future__ import annotations

import logging
from typing import Optional

from pydantic import ValidationError

from ..schemas.intent import ProductIntent
from ..schemas.recommendation import Recommendation
from ..services.product_service import Product
from ..services.providers import AIProvider, get_provider

logger = logging.getLogger("paypilot.recommendation_agent")


class RecommendationValidationError(Exception):
    """Provider payload failed validation or referenced a non-candidate id."""


def recommend(
    intent: ProductIntent,
    candidates: list[Product],
    provider: Optional[AIProvider] = None,
) -> Recommendation:
    """Pick the best candidate for `intent` via the AI provider.

    Args:
        intent: Validated intent from the Intent Agent.
        candidates: Products that already satisfy every hard constraint
            (from product_search.find_candidates). Must be non-empty — with
            no candidates there is nothing to recommend and the caller should
            not spend an AI call.
        provider: Optional explicit provider (for testing/DI).

    Raises:
        ValueError: candidates is empty (caller bug — short-circuit upstream).
        AIConfigError / AIServiceError / InvalidAIResponseError: provider-level.
        RecommendationValidationError: payload invalid, or the recommended id
            is not in the candidate list.
    """
    if not candidates:
        raise ValueError("recommend() requires at least one candidate product")

    provider = provider or get_provider()

    # Authorization is financial data, not shopping data: the recommender
    # neither needs nor receives it.
    intent_payload = intent.model_dump(mode="json", exclude={"authorization"})
    candidate_payloads = [p.model_dump(mode="json", exclude_none=True) for p in candidates]

    payload = provider.recommend_product(intent_payload, candidate_payloads)

    try:
        recommendation = Recommendation(**payload)
    except (ValidationError, TypeError) as exc:
        logger.error("Recommendation payload failed validation")
        raise RecommendationValidationError(
            "AI provider returned a recommendation that failed validation"
        ) from exc

    if recommendation.recommended_product_id is not None:
        by_id = {p.id.lower(): p.id for p in candidates}
        key = recommendation.recommended_product_id.strip().lower()
        if key not in by_id:
            logger.error(
                "AI recommended id %r which is not among the %d candidates",
                recommendation.recommended_product_id,
                len(candidates),
            )
            raise RecommendationValidationError(
                "AI recommended a product that is not in the candidate list"
            )
        # Canonicalize to the exact catalog id spelling.
        recommendation.recommended_product_id = by_id[key]

    logger.info(
        "Recommendation via %s: product_id=%s (from %d candidates)",
        provider.name,
        recommendation.recommended_product_id,
        len(candidates),
    )
    return recommendation
