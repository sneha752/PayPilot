"""External-product policy evaluation schemas (Phase 11C).

TWO DIFFERENT QUESTIONS, TWO DIFFERENT ANSWERS
    This phase adds exactly one new concept, and it is NOT a policy rule:

        ELIGIBILITY  "is there fresh, trusted evidence good enough to
                      evaluate this product at all?"
        POLICY       "given that evidence, may this purchase proceed?"

    The second question already has an owner — the existing deterministic
    Policy Engine — and this module does not duplicate, wrap, or second-guess
    it. When eligibility holds, policy_decision carries the engine's own
    PolicyDecision verbatim, with its own vocabulary
    (ALLOW_AUTONOMOUS_PURCHASE / REQUIRE_USER_APPROVAL / DENY) and its own
    reason codes. Nothing is renamed and no external-product decision value
    is invented.

AN UPSTREAM FAILURE IS NOT A POLICY DENY
    If revalidation could not confirm the product, the Policy Engine is never
    asked, and policy_decision is null. Reporting such a case as DENY would
    put words in the engine's mouth and, worse, would blur two things a
    caller must distingu: "the rules say no" versus "we do not currently
    know what this product costs". Only the first is a judgement; the second
    is an absence of evidence.

NO STALE PRICE, EVER
    The only price that reaches the engine is the fresh, Phase 10F-normalized
    price from the latest revalidation. There is deliberately no field here
    carrying the adopted snapshot's observed price, so it cannot be mistaken
    for, or fall back to, purchase authority.

NO PAYMENT FIELDS
    No PayPal id, no amount, no order, no capture, no refund, no transaction
    id. ALLOW_AUTONOMOUS_PURCHASE here means the rules WOULD permit it — it
    creates nothing and authorizes no execution path, which does not exist
    yet.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from .policy import PolicyDecision


class EligibilityStatus(str, Enum):
    """Whether the product reached the Policy Engine at all.

    EVALUATED    Fresh evidence sufficed; policy_decision is present and is
                 the engine's own ruling.
    NOT_ELIGIBLE An upstream requirement failed; the engine was NOT asked
                 and policy_decision is null. This is not a denial.
    """

    EVALUATED = "EVALUATED"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"


class EligibilityReason(str, Enum):
    """Deterministic, machine-readable reason for the eligibility outcome.

    Each value is produced by exactly one explicit code path, and none is
    AI-written. These are eligibility codes and must never be confused with
    the Policy Engine's own ReasonCode values.
    """

    #: Eligible: the latest revalidation confirmed the product's identity and
    #: carried a fresh normalized price.
    FRESH_EVIDENCE_CONFIRMED = "FRESH_EVIDENCE_CONFIRMED"

    #: No revalidation has ever been run for this adopted product, so there
    #: is no current evidence at all. The adopted snapshot is NOT a fallback.
    NO_REVALIDATION_ON_RECORD = "NO_REVALIDATION_ON_RECORD"

    #: The LATEST revalidation did not confirm the product. One code per
    #: upstream status, so the caller learns which kind of absence it was.
    #: IDENTITY_NOT_FOUND notably does NOT mean the product was withdrawn —
    #: it means current provider evidence could not confirm it.
    LATEST_REVALIDATION_IDENTITY_NOT_FOUND = (
        "LATEST_REVALIDATION_IDENTITY_NOT_FOUND"
    )
    LATEST_REVALIDATION_IDENTITY_UNVERIFIABLE = (
        "LATEST_REVALIDATION_IDENTITY_UNVERIFIABLE"
    )
    LATEST_REVALIDATION_PROVIDER_UNAVAILABLE = (
        "LATEST_REVALIDATION_PROVIDER_UNAVAILABLE"
    )
    LATEST_REVALIDATION_PROVIDER_ERROR = "LATEST_REVALIDATION_PROVIDER_ERROR"
    LATEST_REVALIDATION_NOT_REVALIDATABLE = (
        "LATEST_REVALIDATION_NOT_REVALIDATABLE"
    )

    #: Identity was confirmed, but the provider supplied no usable price, so
    #: there is nothing current to evaluate. Never filled from the adopted
    #: price or from an estimate.
    NO_FRESH_NORMALIZED_PRICE = "NO_FRESH_NORMALIZED_PRICE"

    #: The adopted record was disabled by an operator. An operator decision
    #: outranks fresh evidence, so the engine is not consulted.
    ADOPTED_PRODUCT_DISABLED = "ADOPTED_PRODUCT_DISABLED"


class ExternalPolicyEvaluation(BaseModel):
    """Response body for POST /discovery/adopted/{id}/policy-evaluate.

    Every field is server-derived. There is no request body, so nothing here
    can originate from a client.
    """

    adopted_product_id: str

    # --- the new concept: was there evidence good enough to evaluate? ---
    eligibility: EligibilityStatus
    eligibility_reason: EligibilityReason

    # --- which revalidation this verdict rests on (null when none applied) ---
    revalidation_id: Optional[str] = Field(
        default=None,
        description="The LATEST revalidation snapshot considered. Null only "
        "when no revalidation exists for this product.",
    )
    revalidation_checked_at: Optional[str] = Field(
        default=None,
        description="When that revalidation contacted the provider (ISO 8601 "
        "UTC). The age of the evidence this verdict rests on.",
    )
    revalidation_status: Optional[str] = Field(
        default=None,
        description="That revalidation's RevalidationStatus, carried over "
        "verbatim so the upstream outcome stays visible.",
    )
    price_status: Optional[str] = Field(
        default=None,
        description="That revalidation's PriceStatus, e.g. PRICE_CHANGED. "
        "Informational: a changed price is evaluated on its fresh value, "
        "never auto-denied.",
    )

    # --- the FRESH price that was evaluated. The only price here. ---
    current_normalized_price: Optional[Decimal] = Field(
        default=None,
        description="The fresh, Phase 10F-normalized price the Policy Engine "
        "evaluated. Exact Decimal. Null when nothing was evaluated.",
    )
    current_normalized_currency: Optional[str] = Field(
        default=None,
        description="Currency of current_normalized_price (INR in this "
        "phase). Currency support is the engine's own rule, not re-checked "
        "here.",
    )

    # --- the existing engine's ruling, verbatim ---
    policy_decision: Optional[PolicyDecision] = Field(
        default=None,
        description="The existing Policy Engine's PolicyDecision, unchanged "
        "and unrenamed. Null whenever eligibility is NOT_ELIGIBLE — an "
        "upstream failure is never reported as a policy DENY.",
    )
