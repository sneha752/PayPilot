"""Schemas for the decision-pipeline orchestration (POST /ai/plan).

A "plan" is the full decision pipeline for one request: intent -> candidates
-> recommendation -> policy decision. It is ONLY a decision — nothing here
represents an executed payment, and no payment artifact (order_id,
capture_id, approve_url, refund_id) exists in this schema.

The response deliberately separates three concerns that must never blur:
  - PRODUCT INFORMATION: selected_product, served from the trusted local
    catalog (never from the model).
  - USER AUTHORIZATION: user_authorization, the inert financial-authorization
    facts extracted from the user's message.
  - POLICY DECISION: policy_decision, produced exclusively by the
    deterministic Policy Engine from the two above.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from ..services.product_service import Product
from .intent import Authorization, ProductIntent
from .policy import PolicyDecision
from .recommendation import Recommendation


class PlanRequest(BaseModel):
    """Request body for POST /ai/plan."""

    message: str = Field(
        min_length=1,
        max_length=2000,
        description="The user's natural-language shopping request.",
    )


class PlanResponse(BaseModel):
    """The full decision pipeline for one request.

    status:
      - "planned": a product was selected and the Policy Engine ruled on it.
      - "no_candidates": no catalog product satisfies the hard constraints;
        no recommendation or policy evaluation was performed.
      - "no_suitable_candidate": the model declined to pick any candidate;
        no policy evaluation was performed (there is nothing to evaluate).
    """

    status: Literal["planned", "no_candidates", "no_suitable_candidate"]

    # Pipeline trace.
    intent: ProductIntent
    candidates: list[Product]
    recommendation: Optional[Recommendation] = None

    # PRODUCT INFORMATION — from the local catalog, catalog-exact.
    selected_product: Optional[Product] = None

    # USER AUTHORIZATION — inert facts; echoed for transparency, decided on
    # only by the Policy Engine.
    user_authorization: Authorization

    # POLICY DECISION — the deterministic engine's ruling (None when no
    # product was selected).
    policy_decision: Optional[PolicyDecision] = None
