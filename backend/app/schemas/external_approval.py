"""External-product approval-gate schemas (Phase 11D).

WHAT THIS PHASE ADDS
    A server-side, explicit HUMAN APPROVAL for one externally discovered
    product, bound to the exact server state that was approved:

        policy decision
      + fresh revalidated evidence
      + explicit user approval
      = an approved external purchase INTENT

    An intent, and nothing else. Phase 11D creates no PayPal order, no
    capture, no refund, no transaction and no session, so APPROVED is the
    last state that exists. Execution is a later phase's problem, and this
    record is the security boundary it must pass through.

EXTERNAL PRODUCTS ALWAYS NEED A HUMAN
    The existing Policy Engine stays the sole policy authority and its
    semantics are untouched. But for EXTERNAL products, its most permissive
    ruling is deliberately not sufficient on its own:

        ALLOW_AUTONOMOUS_PURCHASE -> still requires explicit user approval
        REQUIRE_USER_APPROVAL     -> still requires explicit user approval
        DENY                      -> no approval can be created at all

    That is an ADDITIONAL gate layered on top of the engine's answer, not a
    reinterpretation of it: policy_decision below carries the engine's own
    PolicyOutcome verbatim, unrenamed, and no code here re-derives it.

THE BINDING IS THE POINT
    An approval names a revalidation_id, and revalidation history is
    append-only and immutable. So pinning that one id pins everything the
    provider said at that moment -- the title, the merchant, the URL, the
    observed price and the normalized price -- because none of it can be
    rewritten afterwards. The approved price and currency are ALSO stored
    explicitly, as defence in depth and so the record is auditable without
    re-reading another table.

    Substitution is therefore structurally impossible rather than filtered:
    the resolve request carries one boolean, and there is no field anywhere
    in it for a product, a revalidation, a price, a currency, a merchant, a
    URL, or a policy decision.

WHAT IS DELIBERATELY ABSENT
    No paypal_order_id, no paypal_capture_id, no refund id, no transaction
    id, no amount-to-charge, no approval URL, no session id, and no
    AI-generated text. None of those things exist in this phase, so there is
    nowhere for a forged or hallucinated one to land. approved_normalized_price
    is named for what it is: the normalized price of the approved
    revalidation, evidence that was approved -- not a payment amount.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from .external_policy import EligibilityReason, EligibilityStatus
from .policy import PolicyOutcome, ReasonCode


class ExternalApprovalStatus(str, Enum):
    """Lifecycle of one approval record. No payment states exist here.

    PENDING  Created from a valid fresh policy evaluation, awaiting the
             user's explicit decision. Not an authorization by itself.
    APPROVED The user explicitly approved AND every binding condition was
             re-verified at that moment. The terminal success state of this
             phase -- it executes nothing.
    REJECTED The user explicitly declined. Terminal.
    EXPIRED  The approval outlived the freshness of the evidence it was
             bound to before anyone resolved it. Terminal.

    The lifecycle is strictly forward: PENDING may become any of the other
    three, and nothing ever leaves a terminal state. There is intentionally
    no CAPTURED, PAID, ORDERED or REFUNDED value to graduate into.
    """

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class ApprovalBlockedReason(str, Enum):
    """Why an approval could NOT be prepared. Deterministic, never AI-written.

    This is a real answer, not an error: POST .../approval returns 200 with
    approvable=false and one of these codes, exactly as Phase 11C returns
    NOT_ELIGIBLE rather than inventing a policy DENY.
    """

    #: The product never reached the Policy Engine. eligibility_reason
    #: carries Phase 11C's own code saying which upstream requirement
    #: failed (no revalidation on record, the latest revalidation was
    #: IDENTITY_NOT_FOUND / IDENTITY_UNVERIFIABLE / PROVIDER_UNAVAILABLE /
    #: PROVIDER_ERROR / NOT_REVALIDATABLE, no fresh price, or the adopted
    #: record is disabled).
    NOT_POLICY_ELIGIBLE = "NOT_POLICY_ELIGIBLE"

    #: The latest revalidation DID confirm the product, but it was checked
    #: too long ago to approve a purchase against. Phase 11D's new rule.
    REVALIDATION_STALE = "REVALIDATION_STALE"

    #: That revalidation's server-side checked_at could not be read as an
    #: unambiguous UTC instant, so its age is unknown. Unknown age is not
    #: treated as fresh.
    REVALIDATION_TIMESTAMP_UNUSABLE = "REVALIDATION_TIMESTAMP_UNUSABLE"

    #: The Policy Engine ruled DENY. No approval record is created, so there
    #: is nothing for a user to approve.
    POLICY_DENIED = "POLICY_DENIED"


class ApprovalConflict(str, Enum):
    """Why resolving an approval was refused. Returned with HTTP 409.

    Every value means "nothing changed, and nothing executed". None of them
    silently repairs the approval: when the underlying state moved, the user
    must be shown the new state and approve again, which requires a NEW
    approval record they explicitly ask for.
    """

    #: Terminal-state refusals. The lifecycle never runs backwards.
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    APPROVAL_ALREADY_APPROVED = "APPROVAL_ALREADY_APPROVED"
    APPROVAL_ALREADY_REJECTED = "APPROVAL_ALREADY_REJECTED"

    #: Binding re-verification failures, each naming exactly what moved.
    ADOPTED_PRODUCT_MISSING = "ADOPTED_PRODUCT_MISSING"
    REVALIDATION_SUPERSEDED = "REVALIDATION_SUPERSEDED"
    REVALIDATION_MISSING = "REVALIDATION_MISSING"
    REVALIDATION_NOT_CONFIRMED = "REVALIDATION_NOT_CONFIRMED"
    REVALIDATION_STALE = "REVALIDATION_STALE"
    REVALIDATION_TIMESTAMP_UNUSABLE = "REVALIDATION_TIMESTAMP_UNUSABLE"
    NOT_POLICY_ELIGIBLE = "NOT_POLICY_ELIGIBLE"
    POLICY_CHANGED = "POLICY_CHANGED"
    PRICE_CHANGED = "PRICE_CHANGED"
    CURRENCY_CHANGED = "CURRENCY_CHANGED"


class ExternalApprovalResolveRequest(BaseModel):
    """Request body for POST /discovery/approvals/{approval_id}/resolve.

    ONE boolean. That is the complete set of things a user may say, and
    approve has no default because merely calling the endpoint is not an
    approval.

    extra="forbid" turns any additional key into a 422 instead of a silently
    ignored field, so an attempt to smuggle a price, currency, merchant,
    URL, revalidation id, adopted product id, policy decision or PayPal
    field is rejected loudly rather than dropped quietly.
    """

    model_config = ConfigDict(extra="forbid")

    approve: bool = Field(
        description="true to approve THIS bound external product state; "
        "false to reject it. Nothing is executed either way."
    )


class ExternalApproval(BaseModel):
    """One server-side approval record, as stored and as returned.

    Every field is server-derived. Nothing here originates from a client:
    the creation endpoint has no body and the resolve endpoint accepts only
    a boolean.
    """

    approval_id: str = Field(
        description="PayPilot's own id, e.g. 'xapv_<hex>'. Server-generated."
    )
    adopted_product_id: str = Field(
        description="The adopted external product this approval commits to. "
        "Fixed at creation; a resolve request cannot name a different one."
    )

    # --- the exact evidence the approval is bound to ---
    revalidation_id: str = Field(
        description="The ONE revalidation snapshot that was approved. "
        "Revalidation history is append-only, so this id pins the provider's "
        "entire statement -- title, merchant, URL and prices included."
    )
    revalidation_checked_at: str = Field(
        description="When that revalidation contacted the provider (ISO 8601 "
        "UTC, server-generated). The sole basis for every freshness "
        "calculation; no client timestamp is ever consulted."
    )

    # --- the existing engine's ruling, carried verbatim ---
    policy_decision: PolicyOutcome = Field(
        description="The Policy Engine's own PolicyOutcome at creation time, "
        "unchanged and unrenamed. Never DENY: a denied product cannot reach "
        "an approval record. ALLOW_AUTONOMOUS_PURCHASE here still required "
        "this explicit approval."
    )
    policy_reason_code: ReasonCode = Field(
        description="The engine's own ReasonCode at creation time. Bound "
        "alongside the decision, so a same-decision/different-reason change "
        "is still detected as a policy change."
    )

    # --- the approved price. Evidence that was approved, not an amount. ---
    approved_normalized_price: Decimal = Field(
        description="The normalized price of the approved revalidation, "
        "exact Decimal. What the user was shown and agreed to. NOT a payment "
        "amount and not an authorization to charge -- no payment exists."
    )
    approved_normalized_currency: str = Field(
        description="Currency of approved_normalized_price."
    )

    # --- merchant identity, for display and audit ---
    merchant_name: Optional[str] = Field(
        default=None,
        description="Seller as the provider named it in the approved "
        "revalidation. Display and audit only: merchant identity is pinned "
        "by revalidation_id, not re-compared on resolve.",
    )
    merchant_domain: Optional[str] = Field(
        default=None,
        description="Merchant host the backend derived from that same fresh "
        "evidence. Null for an aggregator link.",
    )

    # --- lifecycle ---
    status: ExternalApprovalStatus
    created_at: str = Field(description="ISO 8601 UTC. Server-generated.")
    expires_at: str = Field(
        description="ISO 8601 UTC. Deliberately revalidation_checked_at + "
        "EXTERNAL_REVALIDATION_TTL_SECONDS, so an approval can never outlive "
        "the freshness of the evidence it is bound to. Enforced server-side."
    )
    approved_at: Optional[str] = Field(
        default=None,
        description="When the user explicitly approved. ISO 8601 UTC.",
    )
    rejected_at: Optional[str] = Field(
        default=None,
        description="When the user explicitly rejected. ISO 8601 UTC.",
    )


class ExternalApprovalRequestResult(BaseModel):
    """Response body for POST /discovery/adopted/{id}/approval.

    Returns 200 whether or not an approval could be prepared: "there is no
    fresh confirmed evidence to approve" is an answer about the product, not
    a malformed request. approvable says which case this is, and exactly one
    of approval / blocked_reason is populated.
    """

    adopted_product_id: str

    approvable: bool = Field(
        description="True when a PENDING approval exists for the user to "
        "resolve. False means no approval record was created at all."
    )
    blocked_reason: Optional[ApprovalBlockedReason] = Field(
        default=None,
        description="Why no approval was created. Null when approvable.",
    )
    created: bool = Field(
        default=False,
        description="False when an existing PENDING approval for this exact "
        "binding was returned instead of inserting a second one -- preparing "
        "an approval twice is idempotent and never forks the decision.",
    )

    # --- Phase 11C's verdict, carried over so the caller sees the chain ---
    eligibility: EligibilityStatus
    eligibility_reason: EligibilityReason

    revalidation_id: Optional[str] = None
    revalidation_checked_at: Optional[str] = None
    revalidation_status: Optional[str] = None

    # --- the freshness rule, made inspectable ---
    revalidation_ttl_seconds: int = Field(
        description="The TTL enforced for this request "
        "(EXTERNAL_REVALIDATION_TTL_SECONDS)."
    )
    revalidation_age_seconds: Optional[float] = Field(
        default=None,
        description="now - revalidation_checked_at, in seconds, computed "
        "server-side. Null when there was no revalidation or its timestamp "
        "was unusable.",
    )

    # --- the engine's ruling, verbatim; null when it was never asked ---
    policy_decision: Optional[PolicyOutcome] = None
    policy_reason_code: Optional[ReasonCode] = None

    current_normalized_price: Optional[Decimal] = Field(
        default=None,
        description="The fresh normalized price the engine evaluated. Exact "
        "Decimal. The adopted snapshot's price is never substituted here.",
    )
    current_normalized_currency: Optional[str] = None

    approval: Optional[ExternalApproval] = Field(
        default=None, description="The PENDING approval. Null when blocked."
    )


class ExternalApprovalConflictDetail(BaseModel):
    """HTTP 409 body (as `detail`) when an approval could not be resolved.

    Structured on purpose: the caller must be able to tell WHICH binding
    moved without parsing prose, and must be shown the approval's current
    state so a human can decide whether to request a new approval. Nothing
    was executed, and nothing was repaired.
    """

    code: ApprovalConflict
    message: str = Field(
        description="Human-readable explanation. Never AI-written."
    )
    approval: ExternalApproval = Field(
        description="The approval's state as it stands now, after any "
        "deterministic expiry transition."
    )
