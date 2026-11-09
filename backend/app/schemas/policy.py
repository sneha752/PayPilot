"""Policy Engine schemas: inputs, decision outcomes, and stable reason codes.

The Policy Engine is the ONLY component allowed to decide whether a payment
may proceed, and it is purely deterministic — these schemas carry structured
facts in and a structured, explainable decision out. No model output is
trusted here: the caller supplies the selected product's data and the user's
authorization, and the engine applies explicit rules.

Note: PolicySelectedProduct.price_inr is deliberately UNconstrained (it may
be None, zero, negative, or NaN). Price validity is a policy rule
(INVALID_PRODUCT_PRICE -> DENY), not an input-schema rejection, so garbage
prices produce an auditable DENY decision instead of a 422.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from .intent import Authorization, HardConstraints


class PolicyOutcome(str, Enum):
    """Final authorization decision."""

    ALLOW_AUTONOMOUS_PURCHASE = "ALLOW_AUTONOMOUS_PURCHASE"
    REQUIRE_USER_APPROVAL = "REQUIRE_USER_APPROVAL"
    DENY = "DENY"


class ReasonCode(str, Enum):
    """Stable, machine-readable reason codes (for frontend UI mapping).

    Values are frozen: the frontend will key UI off these strings, so they
    must never be renamed — only added to.
    """

    # DENY codes.
    INVALID_PRODUCT_PRICE = "INVALID_PRODUCT_PRICE"
    UNSUPPORTED_CURRENCY = "UNSUPPORTED_CURRENCY"
    NO_PURCHASE_REQUESTED = "NO_PURCHASE_REQUESTED"
    EXCEEDS_SHOPPING_MAX = "EXCEEDS_SHOPPING_MAX"
    BELOW_SHOPPING_MIN = "BELOW_SHOPPING_MIN"

    # REQUIRE_USER_APPROVAL codes.
    AUTONOMY_NOT_GRANTED = "AUTONOMY_NOT_GRANTED"
    NO_AUTONOMOUS_LIMIT = "NO_AUTONOMOUS_LIMIT"
    EXCEEDS_AUTONOMOUS_LIMIT = "EXCEEDS_AUTONOMOUS_LIMIT"

    # ALLOW codes.
    WITHIN_AUTONOMOUS_LIMIT = "WITHIN_AUTONOMOUS_LIMIT"


class PolicySelectedProduct(BaseModel):
    """The product the user/agent selected for purchase evaluation.

    Supplied by the caller (e.g. from a prior recommendation response); the
    Policy Engine itself never looks anything up.
    """

    product_id: str = Field(min_length=1, description="Catalog id, e.g. 'LAP002'.")
    price_inr: Optional[float] = Field(
        default=None,
        description="Product price. Validity is judged by the engine, not here.",
    )
    currency: str = Field(
        default="INR",
        description="Product currency. This phase accepts INR only.",
    )


class PolicyDecision(BaseModel):
    """Structured, explainable result of a policy evaluation."""

    decision: PolicyOutcome
    reason_code: ReasonCode
    reason: str = Field(description="Human-readable explanation of the decision.")

    # Echoed facts the decision was based on, for auditability.
    product_id: str
    product_price_inr: Optional[float] = None
    currency: str = "INR"
    shopping_max_inr: Optional[int] = None
    authorization_limit_inr: Optional[float] = None


class PolicyEvaluateRequest(BaseModel):
    """Request body for POST /policy/evaluate."""

    product: PolicySelectedProduct
    authorization: Authorization
    hard_constraints: Optional[HardConstraints] = Field(
        default=None,
        description="Shopping constraints from the intent, if any.",
    )
