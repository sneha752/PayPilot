"""Recommendation request/response schemas.

The Recommendation model is also handed to the AI provider as its structured
output schema, which is itself an anti-hallucination measure: the model can
only return a product *id* plus prose — there is no field in which it could
assert a price or specification. All product data in the final response is
looked up from the catalog, never taken from the model.

Nothing here executes or authorizes a payment. The response may echo the
intent's authorization block as inert data, but no field of this module
evaluates it — that is the future Policy Engine's job.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from ..services.product_service import Product
from .intent import ProductIntent


class RecommendRequest(BaseModel):
    """Request body for POST /ai/recommend."""

    message: str = Field(
        min_length=1,
        max_length=2000,
        description="The user's natural-language shopping request.",
    )


class Recommendation(BaseModel):
    """The AI's structured pick from the candidate list.

    recommended_product_id must be the catalog id of one supplied candidate
    (enforced by the Recommendation Agent, not trusted from the model). It is
    None when the model judges no candidate suitable.
    """

    recommended_product_id: Optional[str] = Field(
        default=None,
        description="Catalog id of the chosen candidate, or null if none fits.",
    )
    reasoning: str = Field(
        description="Why this product was chosen (or why none was suitable)."
    )
    matched_constraints: list[str] = Field(
        default_factory=list,
        description="Constraints/preferences the chosen product satisfies.",
    )
    tradeoffs: list[str] = Field(
        default_factory=list,
        description="Compromises versus the user's stated preferences.",
    )


class RecommendResponse(BaseModel):
    """Full response for POST /ai/recommend: intent + candidates + pick."""

    status: Literal["recommended", "no_candidates", "no_suitable_candidate"]
    intent: ProductIntent
    candidates: list[Product]
    recommendation: Optional[Recommendation] = None
    # The chosen product's full record, copied from the catalog (never from
    # the model) so prices/specs in the response are catalog-exact.
    recommended_product: Optional[Product] = None
