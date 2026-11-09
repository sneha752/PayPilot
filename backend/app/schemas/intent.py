"""Structured purchasing-intent schema.

These models describe what the Intent Agent extracts from a natural-language
request. They are pure data: nothing here authorizes, executes, or touches a
payment. `purchase_requested` records only that the user *asked* to buy —
acting on it is out of scope for this phase and must stay that way.

The design separates hard constraints (things that MUST hold) from soft
preferences (things that are nice to have), which is the key distinction the
agent is responsible for.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class HardConstraints(BaseModel):
    """Requirements the user stated as mandatory (filters, in catalog terms)."""

    max_price_inr: Optional[int] = Field(
        default=None, ge=0, description="Upper price bound in INR, inclusive."
    )
    min_price_inr: Optional[int] = Field(
        default=None, ge=0, description="Lower price bound in INR, inclusive."
    )
    min_rating: Optional[float] = Field(
        default=None, ge=0, le=5, description="Minimum acceptable rating (0-5)."
    )
    ram_gb_min: Optional[int] = Field(
        default=None, ge=0, description="Minimum RAM in GB, if the user required it."
    )
    storage_gb_min: Optional[int] = Field(
        default=None, ge=0, description="Minimum storage in GB, if required."
    )
    required_features: list[str] = Field(
        default_factory=list,
        description="Free-form must-have features, e.g. 'backlit keyboard'.",
    )


class Preferences(BaseModel):
    """Soft preferences — used to rank/nudge, never to hard-filter."""

    brand: Optional[str] = Field(default=None, description="Preferred brand, if any.")
    gpu: Optional[str] = Field(
        default=None, description="Preferred GPU family/keyword, e.g. 'RTX'."
    )
    preferred_features: list[str] = Field(
        default_factory=list,
        description="Free-form nice-to-have features.",
    )


class Authorization(BaseModel):
    """Financial authorization the user expressed — SEPARATE from shopping
    constraints. A deterministic Policy Engine (future phase) will compare a
    product's price against these fields; the model never decides to spend.
    """

    purchase_requested: bool = Field(
        default=False,
        description=(
            "True if the user expressed any intention to eventually purchase, "
            "including a conditional authorization. False for pure find/search/"
            "compare requests. Never authorizes a payment by itself."
        ),
    )
    autonomous_purchase_allowed: bool = Field(
        default=False,
        description=(
            "True only if the user explicitly granted automatic/autonomous "
            "buying authority (e.g. 'you may buy it automatically if...'). A "
            "plain 'buy me X' is a purchase request, NOT autonomous authority."
        ),
    )
    auto_approve_below_inr: Optional[float] = Field(
        default=None,
        ge=0,
        description=(
            "Spend ceiling (INR) under which autonomous purchase is permitted. "
            "This is a FINANCIAL AUTHORIZATION threshold and MUST NEVER be "
            "written to hard_constraints.max_price_inr."
        ),
    )


class ProductIntent(BaseModel):
    """A validated, structured representation of a purchasing request."""

    category: Optional[str] = Field(
        default=None,
        description="Product category in lowercase, e.g. 'laptop', 'headphones'.",
    )
    keywords: list[str] = Field(
        default_factory=list,
        description="Salient free-text keywords from the request.",
    )
    use_case: Optional[str] = Field(
        default=None,
        description="Intended use, e.g. 'gaming', 'office work', 'travel'.",
    )
    quantity: int = Field(
        default=1, ge=1, description="How many units the user wants (default 1)."
    )

    hard_constraints: HardConstraints = Field(default_factory=HardConstraints)
    preferences: Preferences = Field(default_factory=Preferences)

    excluded_features: list[str] = Field(
        default_factory=list,
        description="Features/attributes the user explicitly does NOT want.",
    )

    authorization: Authorization = Field(
        default_factory=Authorization,
        description="Financial authorization, kept separate from shopping constraints.",
    )


class IntentRequest(BaseModel):
    """Request body for POST /ai/intent."""

    message: str = Field(
        min_length=1,
        max_length=2000,
        description="The user's natural-language purchasing request.",
    )
