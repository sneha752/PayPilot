"""Discovery comparison schemas (Phase 10E).

Three distinct kinds of data live here, and the boundary between them is the
whole point of this module:

  1. ComparisonRequirements - what the USER wants. Pure preference data.
     Price bounds are a shopping preference expressed in an explicit
     currency; they are NEVER a payment amount, a PayPal value, or a Policy
     Engine input. There is deliberately no authorization block: the
     intent's authorization is dropped when requirements are derived, so
     financial authorization data can never reach the ranking model.

  2. ProductRanking - what the MODEL is allowed to say. It can return
     candidate ids it was given, plus prose. There is no price, currency,
     URL, image, availability, rating, brand, merchant, or spec field
     anywhere in this schema, so a hallucinated attribute has nowhere to
     land.

  3. ComparisonResponse - what the BACKEND returns. Every product object is
     a DiscoveredProduct copied verbatim from the discovery layer. The model
     contributes rank order and a reason string, nothing else.

`ranking_method` exists so a deterministic fallback is never mislabeled as
AI-ranked.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .discovery import NormalizedDiscoveredProduct
from .intent import ProductIntent

#: Inclusive bounds for how many ranked results a caller may ask for. The
#: backend - never the client and never the model - owns the final count.
MIN_TOP_K = 1
MAX_TOP_K = 5
DEFAULT_TOP_K = 3


# ---------------------------------------------------------------------------
# 1. User requirements (preference data only)
# ---------------------------------------------------------------------------


class ComparisonRequirements(BaseModel):
    """Structured shopping requirements for deterministic filtering + ranking.

    Fields split into two groups with very different authority:

    HARD (the deterministic filter enforces these, no AI involved):
        category, brand, max_price/min_price/price_currency,
        required_keywords, excluded_keywords

    SOFT (passed to the ranking model as context; the filter ignores them):
        preferred_brand, preferred_keywords, use_case

    extra="forbid" is a security boundary, not tidiness: a client cannot
    smuggle a paypal_amount, capture_id, refund_id, product object, or any
    other authoritative field in through this model.
    """

    model_config = ConfigDict(extra="forbid")

    # --- hard, deterministically enforced ---
    category: Optional[str] = Field(
        default=None,
        description="Required normalized category. Only applied to candidates "
        "that actually carry a category (see discovery_filter_service).",
    )
    brand: Optional[str] = Field(
        default=None,
        description="REQUIRED brand. Never derived from a soft brand "
        "preference, and never inferred from a product title.",
    )
    max_price: Optional[Decimal] = Field(
        default=None,
        ge=0,
        description="Inclusive upper bound on the OBSERVED price. A shopping "
        "preference - not a payment amount and not a spend authorization.",
    )
    min_price: Optional[Decimal] = Field(
        default=None,
        ge=0,
        description="Inclusive lower bound on the OBSERVED price.",
    )
    price_currency: Optional[str] = Field(
        default=None,
        description="ISO currency the price bounds are expressed in. Required "
        "whenever a bound is set: no currency conversion happens in this "
        "phase, so a bound without a currency is not comparable to anything.",
    )
    required_keywords: list[str] = Field(
        default_factory=list,
        description="Phrases that MUST appear in the candidate's title or "
        "description (case-insensitive substring match).",
    )
    excluded_keywords: list[str] = Field(
        default_factory=list,
        description="Phrases that must NOT appear in the candidate's title or "
        "description.",
    )

    # --- soft, ranking context only ---
    preferred_brand: Optional[str] = Field(
        default=None,
        description="Soft brand preference. Ranking signal only - the "
        "deterministic filter never excludes a candidate for this.",
    )
    preferred_keywords: list[str] = Field(
        default_factory=list,
        description="Soft nice-to-have phrases. Ranking signal only.",
    )
    use_case: Optional[str] = Field(
        default=None,
        description="Stated use case, e.g. 'travel'. Ranking signal only.",
    )

    @field_validator("price_currency")
    @classmethod
    def _normalize_currency(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return value.strip().upper() or None

    @field_validator("category", "brand", "preferred_brand", "use_case")
    @classmethod
    def _blank_to_none(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("required_keywords", "excluded_keywords", "preferred_keywords")
    @classmethod
    def _drop_blank_keywords(cls, values: list[str]) -> list[str]:
        # A blank keyword would otherwise match everything (required) or
        # nothing (excluded) by accident.
        return [v.strip() for v in values if v and v.strip()]

    @model_validator(mode="after")
    def _check_price_bounds(self) -> ComparisonRequirements:
        if (
            self.max_price is not None or self.min_price is not None
        ) and self.price_currency is None:
            raise ValueError(
                "price_currency is required when max_price or min_price is set "
                "(no currency conversion is performed)"
            )
        if (
            self.max_price is not None
            and self.min_price is not None
            and self.max_price < self.min_price
        ):
            raise ValueError("max_price must be greater than or equal to min_price")
        return self

    @property
    def has_price_constraint(self) -> bool:
        return self.max_price is not None or self.min_price is not None


def requirements_from_intent(intent: ProductIntent) -> ComparisonRequirements:
    """Derive comparison requirements from an extracted ProductIntent.

    Deliberate mapping decisions:

      - `authorization` is dropped entirely. Financial authorization is not
        shopping data and never reaches the filter or the ranking model.
      - `hard_constraints.max_price_inr` / `min_price_inr` are INR by
        definition, so price_currency is "INR". Because this phase performs
        NO currency conversion, an INR bound is only comparable to
        INR-priced candidates (see discovery_filter_service's docstring).
      - `preferences.brand` becomes `preferred_brand`, NOT `brand`. The
        intent schema defines preferences as soft ("used to rank/nudge,
        never to hard-filter"), so promoting one to a hard filter here would
        contradict it. A hard brand requirement must be supplied explicitly.
      - `hard_constraints.required_features` become required_keywords and
        `excluded_features` become excluded_keywords, because the user
        stated those as mandatory.
      - `hard_constraints.min_rating` / `ram_gb_min` / `storage_gb_min` are
        NOT mapped: a DiscoveredProduct carries no rating or spec fields,
        and inferring them from a title would be invention.
      - `keywords` are NOT mapped to required_keywords: the intent schema
        describes them as salient free text, not stated requirements. The
        query itself already reaches the ranking model.
    """
    hard = intent.hard_constraints
    prefs = intent.preferences

    preferred_keywords = list(prefs.preferred_features)
    if prefs.gpu:
        preferred_keywords.append(prefs.gpu)

    has_bound = hard.max_price_inr is not None or hard.min_price_inr is not None

    return ComparisonRequirements(
        category=intent.category,
        brand=None,
        max_price=(
            Decimal(hard.max_price_inr) if hard.max_price_inr is not None else None
        ),
        min_price=(
            Decimal(hard.min_price_inr) if hard.min_price_inr is not None else None
        ),
        price_currency="INR" if has_bound else None,
        required_keywords=list(hard.required_features),
        excluded_keywords=list(intent.excluded_features),
        preferred_brand=prefs.brand,
        preferred_keywords=preferred_keywords,
        use_case=intent.use_case,
    )


# ---------------------------------------------------------------------------
# 2. Raw AI ranking output
# ---------------------------------------------------------------------------


class CandidateReason(BaseModel):
    """One model-supplied reason, bound to the candidate id it explains."""

    product_id: str
    reason: str


class ProductRanking(BaseModel):
    """The AI's raw ranking payload.

    This is the complete set of things the model is permitted to return: it
    may order ids it was handed and write prose about them. It cannot assert
    a price, currency, URL, image, retailer, availability, rating, brand, or
    specification, because no such field exists here.

    Nothing in this model is trusted. The comparison agent re-checks every
    id against the candidate set it actually sent.
    """

    ranked_ids: list[str] = Field(
        default_factory=list,
        description="Candidate ids, best first. Must be ids that were supplied.",
    )
    reasons: list[CandidateReason] = Field(
        default_factory=list,
        description="Per-candidate reasons, keyed by candidate id.",
    )

    @field_validator("reasons", mode="before")
    @classmethod
    def _accept_reason_mapping(cls, value):
        """Also accept the {"id": "reason"} object form.

        Models reasonably emit either shape; normalizing here keeps the
        validated form single and keeps a mapping response from being
        discarded as malformed.
        """
        if isinstance(value, dict):
            return [
                {"product_id": key, "reason": reason} for key, reason in value.items()
            ]
        return value


# ---------------------------------------------------------------------------
# 3. Endpoint request / response
# ---------------------------------------------------------------------------


class ComparisonRequest(BaseModel):
    """Request body for POST /discovery/compare.

    There is NO field here through which a client can supply a product
    record, an observed price, a PayPal amount, an order/capture/refund id,
    or a policy override. The server performs its own discovery and owns
    every product fact in the response. extra="forbid" makes an attempt to
    add one a 422 rather than a silently ignored key.
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        min_length=1,
        max_length=2000,
        description="Free-text shopping query, used for discovery and ranking.",
    )
    requirements: Optional[ComparisonRequirements] = Field(
        default=None,
        description="Explicit structured requirements. When omitted, they are "
        "derived from AI intent extraction on `query`.",
    )
    limit: int = Field(
        default=10, ge=1, le=30, description="Discovery results to fetch (1-30)."
    )
    top_k: int = Field(
        default=DEFAULT_TOP_K,
        ge=MIN_TOP_K,
        le=MAX_TOP_K,
        description=f"Ranked results to return ({MIN_TOP_K}-{MAX_TOP_K}).",
    )
    allow_deterministic_fallback: bool = Field(
        default=False,
        description="If true, an AI ranking failure yields a deterministic "
        "ordering explicitly labeled ranking_method='deterministic' instead "
        "of an error. A fallback is never labeled as AI-ranked.",
    )


class ComparisonResult(BaseModel):
    """One ranked entry: a backend-owned product plus explanations."""

    rank: int = Field(ge=1, description="1-based position in the ranking.")
    product: NormalizedDiscoveredProduct = Field(
        description="The authoritative discovery record, copied verbatim from "
        "the discovery layer, including any PayPilot-derived normalized "
        "price. Never populated from AI output."
    )
    reason: str = Field(
        description="Why this candidate placed here, scoped to the supplied "
        "candidate data. Claims are about the returned candidate set only."
    )
    price_note: Optional[str] = Field(
        default=None,
        description="A deterministic, backend-ASSEMBLED sentence stating the "
        "currency conversion that was performed, including the rate's "
        "provenance. Null when nothing was derived. Never written by the AI, "
        "so a converted figure in the response is always the backend's own "
        "arithmetic.",
    )


class ComparisonResponse(BaseModel):
    """Response body for POST /discovery/compare - research output only.

    Nothing here is purchasable: these are the same inert DiscoveredProduct
    records GET /discovery/search returns, with an ordering and an
    explanation attached.
    """

    query: str
    requirements: ComparisonRequirements
    ranking_method: Literal["ai", "deterministic"] = Field(
        description="'ai' only when a validated model ranking was used. A "
        "deterministic fallback is always labeled 'deterministic'."
    )
    ranking_provider: Optional[str] = Field(
        default=None,
        description="AI provider name when ranking_method is 'ai'; null for a "
        "deterministic ordering.",
    )
    discovery_source: Optional[str] = Field(
        default=None, description="Discovery provider the candidates came from."
    )
    normalization_currency: Optional[str] = Field(
        default=None,
        description="Currency the backend derived normalized prices in for "
        "this request. Observed prices are unaffected by it.",
    )
    discovered_count: int = Field(
        ge=0, description="Normalized products returned by the discovery provider."
    )
    candidates_considered: int = Field(
        ge=0, description="Candidates that survived filtering and were ranked."
    )
    results: list[ComparisonResult] = Field(default_factory=list)
