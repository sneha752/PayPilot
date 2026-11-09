"""External-product PayPal order-creation schemas (Phase 11E).

WHAT THIS PHASE ADDS
    The first — and only — path from an APPROVED external product to a real
    PayPal order:

        APPROVED external approval
            -> server re-reads everything it was bound to
            -> server derives the amount from that state alone
            -> paypal.create_order(...)
            -> ExternalPurchaseOrder(status=ORDER_CREATED)

    And it STOPS there. The order sits at PayPal awaiting the buyer's own
    approval on the hosted checkout page. Nothing in this phase captures it,
    so no money moves, and there is no refund path because there is nothing
    to refund.

LLM NEVER CONTROLS MONEY. NEITHER DOES THE CLIENT.
    The request carries one thing: an approval_id, in the URL. There is
    deliberately no request model in this module, because a request model
    would be a place to put a price. The endpoint declares no body
    parameter at all, so an amount, a currency, a merchant, a product URL,
    a title, a product id, a revalidation id, a policy decision, a PayPal
    order id or a capture id sent by a client is not merely rejected — it
    is never parsed, never bound to a name, and has no variable to land in.

    Every monetary field below is therefore an OUTPUT. paypal_amount and
    paypal_currency record what the server sent to PayPal; they are an
    execution audit trail, never an input and never an authority that could
    compete with the approval.

WHERE THE AMOUNT COMES FROM
    The approval binds a revalidation_id, and revalidation history is
    append-only. So the exact row the user approved still holds the
    provider's own OBSERVED price and currency from that moment, and that
    observation — re-read by id, after every binding is re-verified — is
    the amount. No conversion happens: Phase 10F's USD -> INR normalization
    is for policy comparison, and reversing it would invent a rate, so a
    non-USD observed currency fails closed instead (see
    external_purchase_service.SUPPORTED_PAYPAL_CURRENCIES).

WHAT IS DELIBERATELY ABSENT
    No capture id. No refund id. No CAPTURED or REFUNDED status. No
    transaction-ledger id, because the existing ledger is typed to the
    local catalog (an integer catalog_price_inr and a catalog product id)
    and cannot represent an external purchase without losing exactly the
    trust distinction this phase exists to draw. No AI-generated text
    anywhere. None of those things exist here, so a forged or hallucinated
    one has nowhere to land.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .external_approval import ExternalApproval


class ExternalOrderStatus(str, Enum):
    """Lifecycle of one external purchase execution. No capture states.

    CREATING       A reservation. This row is the unique, server-side claim
                   on its approval_id, written and committed BEFORE PayPal
                   is contacted, so a second concurrent request finds it and
                   refuses rather than creating a second PayPal order. It
                   means "a PayPal create-order call is in flight or its
                   outcome was never learned" — never "an order exists".
    ORDER_CREATED  PayPal returned an order id. The order awaits the buyer's
                   approval and is NOT captured.
    BUYER_APPROVED The SERVER read the order back from PayPal (Phase 11F-C)
                   and PayPal itself reported status APPROVED for that exact
                   order id, at the exact amount and currency this server
                   recorded sending. Reached only through
                   external_purchase_service.verify_buyer_approval, only
                   from ORDER_CREATED, and only by a compare-and-set.

                   IT IS NOT A CAPTURE AND NOT A PAYMENT. No money has
                   moved, nothing has been collected, and nothing in this
                   phase can collect it: the external order remains
                   uncapturable through every route (the Phase 11F-B0
                   ownership gate refuses it as EXTERNAL regardless of this
                   status). It records one fact — the buyer approved — so a
                   later phase does not have to infer it from a browser
                   redirect.
    FAILED         PayPal did not return an order id (auth, 4xx, 5xx,
                   timeout, network, malformed body). Terminal and never
                   retried, because a retry could produce a second PayPal
                   order for a failure that actually succeeded upstream.
                   The user requests a new approval instead.

    --- capture states, added by Phase 11F-D ---

    CAPTURING      A RESERVATION, written and committed BEFORE PayPal's
                   capture endpoint is contacted, exactly as CREATING is for
                   order creation. It means "a capture call is in flight or
                   its outcome was never learned" — never "money was
                   collected". A concurrent request that finds this status
                   refuses rather than capturing again, and a process that
                   crashes here leaves the row visibly stuck in CAPTURING
                   for an explicit later reconciliation phase to resolve.
                   It is never auto-retried: a capture that timed out may
                   have succeeded upstream.
    CAPTURED       PayPal's nested capture status was exactly COMPLETED and
                   the captured amount and currency equalled the ones this
                   server recorded. The only state in which money is known
                   to have been collected.
    CAPTURE_PENDING PayPal accepted the capture but reported the nested
                   capture status as PENDING. NOT a success: funds are not
                   collected, and this phase does not poll or settle it.
                   Terminal here; a later reconciliation phase resolves it.
    CAPTURE_DECLINED PayPal definitively refused the capture (nested status
                   DECLINED). Terminal and safe: no money moved.
    CAPTURE_FAILED The outcome is UNKNOWN or unacceptable — an auth failure,
                   a 5xx, a timeout, a network error, a malformed response,
                   or a response whose capture id, amount or currency did
                   not match what this server recorded. Terminal and NEVER
                   auto-retried. Whatever PayPal did report is still
                   persisted on the row, because a capture that actually
                   happened must stay visible even when we refuse to call it
                   a success; nothing here is rolled back.

    These are EXTERNAL states on the external table. They are not, and never
    become, a catalog transaction: nothing in this module writes the
    `transactions` ledger, and the Phase 11F-B0 ownership gate still refuses
    an external order on every catalog capture route whatever its status
    here. There is intentionally still no REFUNDED value — external refunds
    do not exist.
    """

    CREATING = "CREATING"
    ORDER_CREATED = "ORDER_CREATED"
    BUYER_APPROVED = "BUYER_APPROVED"
    CAPTURING = "CAPTURING"
    CAPTURED = "CAPTURED"
    CAPTURE_PENDING = "CAPTURE_PENDING"
    CAPTURE_DECLINED = "CAPTURE_DECLINED"
    CAPTURE_FAILED = "CAPTURE_FAILED"
    FAILED = "FAILED"


class ExternalOrderConflict(str, Enum):
    """Why an order was NOT created. Deterministic, never AI-written.

    Every value means the same two things: no PayPal order was created by
    this request, and nothing was repaired or re-bound. When the underlying
    state moved, the user must be shown the new state and approve again,
    which requires a new approval record they explicitly ask for.
    """

    # --- the approval itself is not an authorization to charge ---
    #: Still PENDING. Nobody has approved anything yet.
    APPROVAL_NOT_APPROVED = "APPROVAL_NOT_APPROVED"
    #: The user explicitly declined this approval.
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    #: The approval outlived the freshness of the evidence it is bound to.
    #: Enforced for an APPROVED record too: approving fresh evidence does
    #: not freeze it, so an order may only be created while it is still
    #: fresh.
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"

    # --- Phase 11D's binding re-verification, re-run from scratch here ---
    ADOPTED_PRODUCT_MISSING = "ADOPTED_PRODUCT_MISSING"
    REVALIDATION_MISSING = "REVALIDATION_MISSING"
    REVALIDATION_SUPERSEDED = "REVALIDATION_SUPERSEDED"
    REVALIDATION_NOT_CONFIRMED = "REVALIDATION_NOT_CONFIRMED"
    REVALIDATION_STALE = "REVALIDATION_STALE"
    REVALIDATION_TIMESTAMP_UNUSABLE = "REVALIDATION_TIMESTAMP_UNUSABLE"
    NOT_POLICY_ELIGIBLE = "NOT_POLICY_ELIGIBLE"
    POLICY_CHANGED = "POLICY_CHANGED"
    PRICE_CHANGED = "PRICE_CHANGED"
    CURRENCY_CHANGED = "CURRENCY_CHANGED"
    #: A binding condition failed that this phase does not have its own code
    #: for. Unreachable today — present so an unforeseen conflict fails
    #: closed as a 409 instead of falling through to an order.
    APPROVAL_BINDING_CHANGED = "APPROVAL_BINDING_CHANGED"

    # --- identity and lifecycle re-checks this phase adds ---
    #: The adopted product's provider identity is no longer durable, or the
    #: bound snapshot did not confirm it by exact external id.
    PRODUCT_IDENTITY_INVALID = "PRODUCT_IDENTITY_INVALID"
    #: The adopted record is not ACTIVE (STALE or DISABLED).
    PRODUCT_NOT_PURCHASABLE = "PRODUCT_NOT_PURCHASABLE"

    # --- money representation, all failing closed ---
    #: The bound snapshot carries no provider-observed price/currency, so
    #: there is no amount to charge. The normalized comparison figure is
    #: NOT substituted, and neither is the adopted snapshot's old price.
    OBSERVED_PRICE_UNAVAILABLE = "OBSERVED_PRICE_UNAVAILABLE"
    #: The observed currency is not one the existing PayPal integration
    #: charges. No rate is invented to bridge the gap.
    UNSUPPORTED_PAYPAL_CURRENCY = "UNSUPPORTED_PAYPAL_CURRENCY"
    #: The observed price cannot be stated exactly as a PayPal amount (not
    #: finite, not positive, or more precise than two decimal places).
    #: Silently rounding it would charge a number nobody approved.
    AMOUNT_NOT_REPRESENTABLE = "AMOUNT_NOT_REPRESENTABLE"

    # --- single-execution guarantees ---
    #: Another request holds the reservation for this approval and its
    #: PayPal outcome is not yet known. Refusing is the only answer that
    #: cannot produce a second PayPal order.
    EXECUTION_IN_PROGRESS = "EXECUTION_IN_PROGRESS"
    #: This approval's one execution attempt already failed. Terminal: a
    #: retry could duplicate an order that succeeded at PayPal without us
    #: learning of it. Request a new approval.
    EXECUTION_ALREADY_FAILED = "EXECUTION_ALREADY_FAILED"

    # --- checkout link recovery (Phase 11F-B) ---
    #: The order EXISTS at PayPal, but this server cannot produce a usable
    #: checkout URL for it: none was stored, and a read-only get_order did
    #: not yield a valid https approval link either (PayPal returned no such
    #: link, or could not be reached). Nothing was created and no second
    #: create-order was attempted — the alternative would be fabricating a
    #: checkout address, which is exactly what must not happen. The
    #: execution record is returned alongside this code, so the order id and
    #: the recorded amount remain visible.
    APPROVAL_URL_UNAVAILABLE = "APPROVAL_URL_UNAVAILABLE"


class ExternalPurchaseOrder(BaseModel):
    """One external purchase execution record, as stored and as returned.

    Every field is server-derived. The client supplies only the approval_id
    in the URL; everything else is read from server-side state or returned
    by PayPal.
    """

    execution_id: str = Field(
        description="PayPilot's own id, e.g. 'xord_<hex>'. Server-generated."
    )
    approval_id: str = Field(
        description="The APPROVED external approval this execution serves. "
        "UNIQUE in the table: one approval can own at most one execution "
        "record, which is what makes a duplicate request idempotent rather "
        "than a second order."
    )
    adopted_product_id: str = Field(
        description="Bound from the approval, never from a request."
    )
    revalidation_id: str = Field(
        description="The exact revalidation snapshot whose observed price "
        "became the PayPal amount. Append-only, so this stays auditable."
    )

    paypal_order_id: Optional[str] = Field(
        default=None,
        description="PayPal's order id, as PayPal returned it. Null while "
        "CREATING and on FAILED. The order is NOT captured.",
    )
    paypal_amount: Optional[str] = Field(
        default=None,
        description="Exactly what was sent to PayPal, as the two-decimal "
        "string PayPal's amount.value expects. An audit value: it is an "
        "output of the approved state, never an input, and never consulted "
        "as an alternative authority over the approval.",
    )
    paypal_currency: Optional[str] = Field(
        default=None,
        description="Currency sent to PayPal — the provider's own observed "
        "currency from the bound snapshot. Audit value, like paypal_amount.",
    )
    paypal_approve_url: Optional[str] = Field(
        default=None,
        description="PayPal's hosted-checkout link for this order, exactly "
        "as PayPal returned it (Phase 11F-B). Null while CREATING, on "
        "FAILED, and for an ORDER_CREATED row written before this column "
        "existed — such a row recovers the real link from a read-only "
        "get_order on replay. It is NEVER reconstructed from the order id, "
        "and it is an OUTPUT: no request can set it.",
    )

    capture_authorized_at: Optional[str] = Field(
        default=None,
        description="ISO 8601 UTC instant at which a PayPilot operator "
        "explicitly authorized capturing this order (Phase 11F-D). Null "
        "means NOT authorized, and capture refuses. It is both the "
        "authorization record and the start of the capture deadline. "
        "Server-generated from the server's own clock; no request can set "
        "it, and it is a workflow safeguard, NOT proof of caller identity — "
        "this API is unauthenticated.",
    )
    paypal_capture_id: Optional[str] = Field(
        default=None,
        description="PayPal's capture id, as PayPal reported it (Phase "
        "11F-D). Recorded for CAPTURED, and also for a PENDING, DECLINED or "
        "rejected outcome when PayPal reported one, so a capture that "
        "actually happened stays visible even when this server refuses to "
        "call it a success. Never constructed from the order id. This is an "
        "EXTERNAL capture id: it is not, and never becomes, a catalog "
        "transaction's paypal_capture_id, and the catalog refund path "
        "refuses it.",
    )
    paypal_capture_status: Optional[str] = Field(
        default=None,
        description="PayPal's nested capture status verbatim (COMPLETED, "
        "PENDING, DECLINED, or whatever else PayPal sent). Reported as "
        "observed; only COMPLETED is treated as collected.",
    )
    captured_amount: Optional[str] = Field(
        default=None,
        description="The amount PayPal reported capturing. For a CAPTURED "
        "row it equals paypal_amount — a mismatch is why a row would be "
        "CAPTURE_FAILED instead. Audit value, never an input.",
    )
    captured_currency: Optional[str] = Field(
        default=None,
        description="The currency PayPal reported capturing. Audit value.",
    )
    paypal_request_id: Optional[str] = Field(
        default=None,
        description="The stable PayPal-Request-Id sent on this execution's "
        "create_order call (Phase 11F-K.22a). Derived from the unique "
        "execution id, persisted at reservation time BEFORE PayPal is "
        "contacted, and never overwritten. Null only for a row that predates "
        "the column. An output/audit value; no request can set it.",
    )
    recovery_claimed_at: Optional[str] = Field(
        default=None,
        description="ISO 8601 UTC instant at which an operator-gated "
        "create-order REPLAY recovery claimed this CREATING reservation "
        "(Phase 11F-K.22b), or null when no replay is in flight. A durable "
        "compare-and-set claim so at most one worker replays at a time; it is "
        "cleared when the replay completes and never affects eligibility "
        "(measured from created_at). An internal audit value; no request can "
        "set it.",
    )
    recovery_claim_token: Optional[str] = Field(
        default=None,
        description="A unique token (UUID hex) generated fresh for each "
        "successful recovery claim (Phase 11F-K.22b ownership guard). The "
        "worker that wins the claim receives this token and must present it "
        "to release the claim; a stale-reclaim overwrites it, so an old "
        "worker's release becomes a no-op. Null when no claim is held.",
    )

    status: ExternalOrderStatus
    created_at: str = Field(description="ISO 8601 UTC. Server-generated.")
    updated_at: str = Field(description="ISO 8601 UTC. Server-generated.")


class ExternalOrderResult(BaseModel):
    """Response body for POST /discovery/approvals/{id}/create-order.

    Carries the execution record plus the trust chain that produced the
    amount, so an auditor can see the derivation without re-reading four
    tables — and can check that the amount sent to PayPal is the observed
    price of the approved revalidation, not the normalized comparison
    figure and not the adopted snapshot's price.
    """

    execution: ExternalPurchaseOrder

    created: bool = Field(
        description="True when THIS request created the PayPal order. False "
        "on an idempotent replay: an execution record already existed for "
        "this approval and is returned unchanged, with no second PayPal "
        "call."
    )
    approve_url: Optional[str] = Field(
        default=None,
        description="PayPal's hosted-checkout link, where the BUYER must "
        "approve the order. Returned only by the request that created the "
        "order: approval links are not stored, and PayPal's URL is never "
        "reconstructed locally. Visiting it does not capture anything.",
    )
    captured: bool = Field(
        default=False,
        description="Always false. Stated explicitly so a client can assert "
        "it: this phase never calls capture_order, so the order remains "
        "awaiting buyer approval.",
    )

    # --- the approved state the amount was derived from ---
    approval: ExternalApproval = Field(
        description="The approval as it stands, re-read server-side. Its "
        "approved_normalized_price/currency are the POLICY figures (INR), "
        "shown alongside the PayPal amount so the two are never confused."
    )
    observed_price_source: str = Field(
        description="Deterministic provenance for the amount, naming the "
        "revalidation id it was read from. Never AI-written."
    )


class ExternalApprovalVerificationFailure(str, Enum):
    """Why buyer approval was NOT verified (Phase 11F-C). Fails closed.

    Every value means the same three things: PayPal's own answer did not
    establish that the buyer approved THIS order at THIS amount, the
    execution did NOT become BUYER_APPROVED, and nothing was captured,
    refunded, created, replayed or repaired. A verification failure never
    degrades into a weaker claim — there is no "probably approved".

    Kept deliberately separate from ExternalOrderConflict: that enum answers
    "why was an order not created", and conflating the two would let a
    reader mistake a failed amount check for a creation problem.
    """

    #: The execution is CREATING (no order exists yet, or its outcome was
    #: never learned) or FAILED (terminal). Neither is approvable: there is
    #: no confirmed order id to ask PayPal about.
    EXECUTION_NOT_VERIFIABLE = "EXECUTION_NOT_VERIFIABLE"

    #: The stored row carries no PayPal order id, amount or currency, so
    #: there is nothing authoritative to verify PayPal's answer AGAINST.
    #: Verifying against a missing expectation would check nothing.
    RECORDED_ORDER_INCOMPLETE = "RECORDED_ORDER_INCOMPLETE"

    #: PayPal answered about a DIFFERENT order id than the one stored. The
    #: stored id is the only authority; PayPal's echo never overrides it.
    ORDER_ID_MISMATCH = "ORDER_ID_MISMATCH"

    #: PayPal's status for the order is not APPROVED. The observed status is
    #: reported verbatim (CREATED, VOIDED, COMPLETED, PAYER_ACTION_REQUIRED,
    #: anything else) WITHOUT claiming approval — including COMPLETED, which
    #: this system never produces for an external order and must not treat
    #: as a success it can act on.
    ORDER_NOT_APPROVED = "ORDER_NOT_APPROVED"

    #: PayPal returned no purchase unit, or one with no usable amount, so
    #: there is no figure to compare against the recorded one.
    PURCHASE_UNIT_MISSING = "PURCHASE_UNIT_MISSING"

    #: PayPal returned more than one purchase unit. Which one the recorded
    #: amount refers to is ambiguous, and guessing is how an approval for a
    #: different sum gets accepted. Refused rather than resolved.
    PURCHASE_UNIT_AMBIGUOUS = "PURCHASE_UNIT_AMBIGUOUS"

    #: The amount PayPal reported is not a finite decimal number.
    AMOUNT_MALFORMED = "AMOUNT_MALFORMED"

    #: PayPal's amount differs from the amount this server recorded sending.
    #: The buyer approved a different sum than the one the user approved.
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"

    #: PayPal's currency differs from the recorded one.
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"

    #: PayPal could not be read: auth failure, the order is gone, an
    #: upstream 5xx, a timeout or a network error. Unknown is not approved.
    ORDER_UNREADABLE = "ORDER_UNREADABLE"


class ExternalApprovalVerification(BaseModel):
    """Response body for POST /discovery/approvals/{id}/verify-approval.

    Returned ONLY when the buyer's approval is established: either this
    request verified it against PayPal, or a previous request already did
    and the execution is BUYER_APPROVED. Every failure is an error response
    carrying an ExternalApprovalVerificationFailure code instead.

    NOTHING WAS CAPTURED. Verification is a read of PayPal's order plus one
    guarded status write in this server's own table. No capture, no refund,
    no catalog ledger row, no PayPal order created or replayed.
    """

    execution: ExternalPurchaseOrder = Field(
        description="The execution record as it stands AFTER verification — "
        "status BUYER_APPROVED. Every field is server-derived."
    )
    verified: bool = Field(
        description="Always true in a success response. Present so a client "
        "asserts on a field rather than on the absence of an error."
    )
    transitioned: bool = Field(
        description="True when THIS request performed the ORDER_CREATED -> "
        "BUYER_APPROVED compare-and-set. False on an idempotent replay and "
        "when a concurrent request won the race — in both cases the state "
        "was already BUYER_APPROVED and no second transition occurred."
    )
    verification_source: Literal["paypal", "stored"] = Field(
        description="'paypal' when this request read the order from PayPal "
        "and checked it. 'stored' when the execution was already "
        "BUYER_APPROVED, so the answer comes from the earlier verification "
        "and PayPal was NOT contacted. Stated explicitly so a replay is "
        "never mistaken for a fresh observation."
    )
    observed_order_status: Optional[str] = Field(
        default=None,
        description="PayPal's own order status, as PayPal reported it on "
        "THIS request. Null when verification_source is 'stored' — nothing "
        "was observed, and inventing a value would be a lie.",
    )
    observed_amount: Optional[str] = Field(
        default=None,
        description="The purchase-unit amount PayPal reported on this "
        "request. It matched the recorded paypal_amount, or this would be "
        "an AMOUNT_MISMATCH failure. Null on a stored replay.",
    )
    observed_currency: Optional[str] = Field(
        default=None,
        description="The purchase-unit currency PayPal reported on this "
        "request. Null on a stored replay.",
    )
    captured: bool = Field(
        default=False,
        description="Always false. Buyer approval is not payment: no "
        "capture exists for an external order in any phase so far, and "
        "verifying approval creates none.",
    )


class ExternalCaptureFailure(str, Enum):
    """Why an external capture did NOT complete (Phase 11F-D). Fails closed.

    Split into two groups by a single question: WAS PAYPAL'S CAPTURE
    ENDPOINT CALLED? Everything in the first group is refused before any
    capture call, so no money can have moved. Everything in the second
    group is reported after a call whose outcome was not an acceptable,
    verified COMPLETED — and for those the execution record keeps whatever
    PayPal did report, because this phase never pretends a call was rolled
    back. Nothing in either group is ever retried automatically.
    """

    # --- refused BEFORE PayPal's capture endpoint is called ---

    #: The execution is not BUYER_APPROVED, so server-side verification of
    #: the buyer's approval has not happened. CREATING, ORDER_CREATED and
    #: FAILED all land here.
    CAPTURE_NOT_PERMITTED_IN_STATE = "CAPTURE_NOT_PERMITTED_IN_STATE"
    #: Buyer approval is verified, but no PayPilot operator has explicitly
    #: authorized capturing. Verified approval alone is NOT authorization.
    CAPTURE_NOT_AUTHORIZED = "CAPTURE_NOT_AUTHORIZED"
    #: The explicit authorization is older than the capture window. It is
    #: not extended silently; re-authorize to start a fresh window.
    CAPTURE_AUTHORIZATION_EXPIRED = "CAPTURE_AUTHORIZATION_EXPIRED"
    #: The row records no complete order id, amount and currency, so
    #: PayPal's answer could not be checked against anything authoritative.
    RECORDED_ORDER_INCOMPLETE = "RECORDED_ORDER_INCOMPLETE"
    #: The pre-capture re-read of the order failed (auth, missing order,
    #: 5xx, timeout, network). Unknown is not capturable.
    ORDER_UNREADABLE = "ORDER_UNREADABLE"
    #: The pre-capture re-read answered about a different order id.
    ORDER_ID_MISMATCH = "ORDER_ID_MISMATCH"
    #: The order's current status is not APPROVED. Reported verbatim.
    ORDER_NOT_APPROVED = "ORDER_NOT_APPROVED"
    #: No usable purchase unit / amount in the pre-capture re-read.
    PURCHASE_UNIT_MISSING = "PURCHASE_UNIT_MISSING"
    #: More than one purchase unit: which amount the buyer approved is
    #: ambiguous, and guessing is how the wrong sum gets captured.
    PURCHASE_UNIT_AMBIGUOUS = "PURCHASE_UNIT_AMBIGUOUS"
    #: The order's amount is not a finite decimal number.
    AMOUNT_MALFORMED = "AMOUNT_MALFORMED"
    #: The order's amount differs from the recorded one.
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    #: The order's currency differs from the recorded one.
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    #: Another request holds this execution's capture reservation and its
    #: outcome is not yet known. Refusing is the only answer that cannot
    #: produce a second capture call.
    CAPTURE_IN_PROGRESS = "CAPTURE_IN_PROGRESS"
    #: This execution's one capture attempt already reached a terminal
    #: non-captured state (PENDING, DECLINED or FAILED). It is NOT retried,
    #: because a retry could double-charge a capture that succeeded upstream
    #: without this server learning of it.
    CAPTURE_ALREADY_ATTEMPTED = "CAPTURE_ALREADY_ATTEMPTED"

    # --- reported AFTER a capture call; money may or may not have moved ---

    #: PayPal reported the nested capture status DECLINED. Definitive: no
    #: money moved. Terminal.
    CAPTURE_DECLINED = "CAPTURE_DECLINED"
    #: The capture call failed or its outcome is unknown: auth, 404, 5xx,
    #: timeout, network, a malformed body, or a nested status that is
    #: neither COMPLETED, PENDING nor DECLINED. The capture MAY have
    #: succeeded upstream. Never auto-retried; needs reconciliation.
    CAPTURE_UNCONFIRMED = "CAPTURE_UNCONFIRMED"
    #: PayPal reported a COMPLETED capture whose amount, currency or capture
    #: id did not match what this server recorded, or omitted the capture id
    #: entirely. MONEY MAY HAVE MOVED AND IS NOT ROLLED BACK — this server
    #: simply refuses to record it as a clean success. The observed values
    #: are persisted so the discrepancy is visible and reconcilable.
    CAPTURED_RESULT_MISMATCH = "CAPTURED_RESULT_MISMATCH"
    #: PayPal says the order was already captured, and this server could not
    #: independently retrieve and verify that existing capture. It is NOT
    #: reported as a new successful capture.
    EXISTING_CAPTURE_UNVERIFIABLE = "EXISTING_CAPTURE_UNVERIFIABLE"


class ExternalCaptureAuthorization(BaseModel):
    """Response body for POST /discovery/approvals/{id}/authorize-capture.

    Records that a PayPilot operator explicitly authorized capturing this
    execution, and opens the capture window. It performs NO PayPal call and
    captures nothing: it only writes capture_authorized_at.

    THIS IS A WORKFLOW SAFEGUARD, NOT AUTHENTICATION. This API has no
    caller identity, so the stored authorization records that the step was
    taken, never WHO took it. It exists so that a verified buyer approval
    alone cannot cause money to move — capture needs a second, separate,
    deliberate act — and so that act has a deadline.
    """

    execution: ExternalPurchaseOrder
    authorized: bool = Field(
        description="Always true in a success response: capture is now "
        "authorized until capture_expires_at."
    )
    capture_authorized_at: str = Field(
        description="When the authorization was recorded, from the server's "
        "own clock. ISO 8601 UTC."
    )
    capture_expires_at: str = Field(
        description="capture_authorized_at plus the configured capture "
        "window. After this instant capture refuses with "
        "CAPTURE_AUTHORIZATION_EXPIRED until it is authorized again."
    )
    capture_window_seconds: int = Field(
        description="The configured window, in seconds. A checkout deadline "
        "in its own right — deliberately NOT the discovery revalidation TTL, "
        "which answers a different question about different evidence."
    )
    captured: bool = Field(
        default=False,
        description="Always false. Authorizing captures nothing.",
    )


class ExternalCaptureResult(BaseModel):
    """Response body for POST /discovery/approvals/{id}/capture.

    Returned for the two outcomes that are not refusals: a verified
    COMPLETED capture, and a PENDING one. `captured` is the only field that
    asserts money was collected, and it is true for exactly one of them.
    """

    execution: ExternalPurchaseOrder
    outcome: Literal["CAPTURED", "PENDING"] = Field(
        description="CAPTURED: PayPal's nested capture status was exactly "
        "COMPLETED and the captured amount and currency matched the "
        "recorded ones. PENDING: PayPal accepted the capture but has not "
        "completed it — funds are NOT collected and this phase does not "
        "poll for settlement."
    )
    captured: bool = Field(
        description="True ONLY for outcome CAPTURED. False for PENDING, so "
        "a client that checks this one field can never mistake an "
        "unsettled capture for a collected payment."
    )
    replay: bool = Field(
        default=False,
        description="True when the execution was ALREADY CAPTURED and this "
        "request returned the stored result without calling PayPal again."
    )
    paypal_capture_id: Optional[str] = Field(
        default=None, description="PayPal's capture id, as PayPal reported it."
    )
    paypal_capture_status: Optional[str] = Field(
        default=None, description="PayPal's nested capture status verbatim."
    )
    captured_amount: Optional[str] = Field(
        default=None, description="The amount PayPal reported capturing."
    )
    captured_currency: Optional[str] = Field(
        default=None, description="The currency PayPal reported capturing."
    )
    refundable: bool = Field(
        default=False,
        description="Always false. External refunds do not exist in any "
        "phase so far: the catalog refund path refuses an external capture "
        "id, and this module has no refund capability.",
    )


class ExternalOrderView(BaseModel):
    """Response body for GET /discovery/approvals/{id}/order (Phase 11F-B).

    A READ of one execution record. The GET creates nothing, mutates
    nothing, contacts PayPal for nothing, and has no side effect of any
    kind — it answers from the `external_purchase_orders` row alone.

    NOT AUTHENTICATED. This endpoint establishes no caller identity, and
    nothing in this system does yet: anyone who can reach the API and knows
    (or guesses) an approval id can read that approval's execution record,
    including its checkout URL. The record deliberately holds no credential,
    no access token, no authorization header and no raw provider payload, so
    what a reader learns is the order id, the amount this server recorded,
    and PayPal's own hosted-checkout link. Visiting that link does not
    capture anything; the buyer still has to approve at PayPal.

    `approve_url` is the stored value and may be null — for a CREATING or
    FAILED execution (no order exists) or for an ORDER_CREATED row written
    before the column existed. Recovery of a missing link is deliberately
    NOT performed here, because it would mean a GET writing to the database
    and calling PayPal; it happens on the create-order replay instead.
    """

    execution: ExternalPurchaseOrder = Field(
        description="The stored record, read by approval id. Every field is "
        "server-derived; no request can set any of them."
    )
    approve_url: Optional[str] = Field(
        default=None,
        description="The stored PayPal hosted-checkout link, or null when "
        "none is recorded. Never reconstructed from the order id.",
    )
    captured: bool = Field(
        default=False,
        description="Always false. Stated explicitly so a client can assert "
        "it: no capture exists for an external order in any phase so far, "
        "and this read introduces none.",
    )


class ExternalExecutionTriage(BaseModel):
    """Operator-triage projection of one execution (Phase 11F-I).

    A DELIBERATELY NARROW view for the recovery endpoints: only the fields an
    operator needs to triage an unresolved external payment. It omits the
    hosted-checkout URL (not needed for triage) and holds no credential,
    access token, authorization header, or raw provider payload — the stored
    row never contains any of those. Every field is server-derived.

    has_paypal_order_id (Phase 11F-K.17) makes the one distinction that
    decides how a stuck CREATING row is recovered explicit: True means a
    PayPal order id is persisted (a read-first reconciliation can look the
    order up), False means none is recorded (the create-order outcome is
    ambiguous and only manual review is safe).
    """

    execution_id: str
    approval_id: str
    adopted_product_id: str
    revalidation_id: str
    paypal_order_id: Optional[str] = None
    has_paypal_order_id: bool = False
    paypal_amount: Optional[str] = None
    paypal_currency: Optional[str] = None
    paypal_capture_id: Optional[str] = None
    paypal_capture_status: Optional[str] = None
    captured_amount: Optional[str] = None
    captured_currency: Optional[str] = None
    capture_authorized_at: Optional[str] = None
    status: ExternalOrderStatus
    created_at: str
    updated_at: str

    @classmethod
    def from_record(cls, record: "ExternalPurchaseOrder") -> "ExternalExecutionTriage":
        return cls(
            execution_id=record.execution_id,
            approval_id=record.approval_id,
            adopted_product_id=record.adopted_product_id,
            revalidation_id=record.revalidation_id,
            paypal_order_id=record.paypal_order_id,
            has_paypal_order_id=bool((record.paypal_order_id or "").strip()),
            paypal_amount=record.paypal_amount,
            paypal_currency=record.paypal_currency,
            paypal_capture_id=record.paypal_capture_id,
            paypal_capture_status=record.paypal_capture_status,
            captured_amount=record.captured_amount,
            captured_currency=record.captured_currency,
            capture_authorized_at=record.capture_authorized_at,
            status=record.status,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class UnresolvedExecutionsResponse(BaseModel):
    """Response for GET /discovery/external-orders/unresolved (Phase 11F-I).

    A read-only triage list of external executions needing attention
    (CAPTURING / CAPTURE_PENDING / CAPTURE_FAILED, plus — Phase 11F-K.17 —
    CREATING reservations older than the staleness grace period). It exposes
    no way to mutate a status and returns no arbitrary rows — only the
    bounded, status-filtered projection above.
    """

    executions: list[ExternalExecutionTriage]
    count: int = Field(description="Number of executions returned (<= limit).")


class ExternalReconcileReport(BaseModel):
    """Response for POST /discovery/approvals/{id}/reconcile (Phase 11F-I).

    An operator recovery REPORT, not a payment result. Reconciliation is
    read-first: it re-reads PayPal by the server-stored order id and promotes
    CAPTURING/CAPTURE_PENDING to CAPTURED only on conclusive, independently
    verified evidence, via the existing compare-and-set. It NEVER calls
    capture, refund, create-order or capture-authorization.
    """

    execution: ExternalExecutionTriage = Field(
        description="The execution AFTER reconciliation, as stored."
    )
    approval_id: str
    outcome: str = Field(
        description="reconciled_to_captured | already_captured | "
        "unresolved_no_capture | inconclusive | review_required | "
        "manual_review_required | not_applicable."
    )
    status: str = Field(description="The resulting stored ExternalOrderStatus.")
    reconciled: bool = Field(
        description="True ONLY when THIS call advanced the execution to "
        "CAPTURED on verified evidence. False for an idempotent replay and "
        "for every non-promoting outcome."
    )
    review_required: bool = Field(
        default=False,
        description="True when a human must inspect the execution (a "
        "definitive-looking discrepancy, or a terminal CAPTURE_FAILED that "
        "cannot be safely auto-resolved).",
    )
    retryable: bool = Field(
        default=False,
        description="True when the outcome may change on a later retry (e.g. "
        "PayPal was unreadable, or a capture is not yet visible).",
    )
    captured: bool = Field(
        default=False,
        description="True only when the stored status is CAPTURED. Mirrors "
        "the ledger, never asserts a fresh collection on PayPal's word alone.",
    )
    message: str = Field(description="Human-readable explanation of the outcome.")
    detail: Optional[dict] = Field(
        default=None,
        description="Safe diagnostic codes from reconciliation (e.g. a reason "
        "and observed capture fields). Never a credential or raw payload.",
    )
