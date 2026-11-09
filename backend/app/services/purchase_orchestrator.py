"""Decision-pipeline orchestrator: message -> intent -> candidates ->
recommendation -> policy decision.

This service only CONNECTS the existing components; it adds no logic of its
own beyond sequencing and data hand-off:

    Intent Agent          (AI: natural language -> ProductIntent)
    Product Search        (deterministic: hard shopping constraints only)
    Recommendation Agent  (AI: pick one candidate, grounding enforced)
    Policy Engine         (deterministic: the ONLY authorization authority)

Boundaries upheld here:
  - It never duplicates or pre-empts a Policy Engine rule; whenever a product
    is selected, the engine is asked, and its ruling is returned verbatim.
  - The price handed to the Policy Engine is the selected product's
    price_inr from the trusted local catalog — never a number produced by
    the model (the Recommendation schema cannot even carry one).
  - It never touches PayPal: no order, no capture, no refund, no approval
    URL. A "plan" is a decision, not an executed payment.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..agents.intent_agent import interpret as interpret_intent
from ..agents.recommendation_agent import recommend
from ..policy import evaluate_policy
from ..schemas.plan import PlanResponse
from ..schemas.policy import PolicySelectedProduct
from .product_search import find_candidates
from .providers import AIProvider

logger = logging.getLogger("paypilot.orchestrator")


def plan(message: str, provider: Optional[AIProvider] = None) -> PlanResponse:
    """Run the full decision pipeline for one natural-language request.

    Args:
        message: The user's request.
        provider: Optional explicit AI provider (testing/DI); passed through
            to both agents, which otherwise use the configured default.

    Raises (propagated for the endpoint to map to HTTP codes):
        AIConfigError / AIServiceError / InvalidAIResponseError,
        IntentValidationError, RecommendationValidationError.
    """
    intent = interpret_intent(message, provider=provider)

    candidates = find_candidates(intent)
    if not candidates:
        # Nothing satisfies the hard constraints: report honestly, spend no
        # AI call, and leave nothing for the Policy Engine to rule on.
        logger.info("Plan: no candidates for category=%s", intent.category)
        return PlanResponse(
            status="no_candidates",
            intent=intent,
            candidates=[],
            user_authorization=intent.authorization,
        )

    recommendation = recommend(intent, candidates, provider=provider)

    if recommendation.recommended_product_id is None:
        # The model declined every candidate; no product, no policy ruling.
        logger.info("Plan: model declined all %d candidates", len(candidates))
        return PlanResponse(
            status="no_suitable_candidate",
            intent=intent,
            candidates=candidates,
            recommendation=recommendation,
            user_authorization=intent.authorization,
        )

    # The agent guarantees the id is in the candidate list; take the product
    # record (and therefore the PRICE) from the catalog data, not the model.
    selected = next(
        p for p in candidates if p.id == recommendation.recommended_product_id
    )

    policy_decision = evaluate_policy(
        product=PolicySelectedProduct(
            product_id=selected.id,
            price_inr=selected.price_inr,
            currency="INR",  # the local catalog is INR
        ),
        authorization=intent.authorization,
        hard_constraints=intent.hard_constraints,
    )

    logger.info(
        "Plan: selected=%s price=%s decision=%s (%s)",
        selected.id,
        selected.price_inr,
        policy_decision.decision.value,
        policy_decision.reason_code.value,
    )
    return PlanResponse(
        status="planned",
        intent=intent,
        candidates=candidates,
        recommendation=recommendation,
        selected_product=selected,
        user_authorization=intent.authorization,
        policy_decision=policy_decision,
    )
