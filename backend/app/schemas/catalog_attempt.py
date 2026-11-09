"""Catalog purchase-attempt schemas (Phase 11F-K.18 Option B, phase 1).

A catalog purchase attempt is a DURABLE RESERVATION written and committed
BEFORE the catalog flow contacts PayPal's create-order endpoint. It exists so
that a crash — or a ledger-write failure — AFTER PayPal creates an order can
no longer leave the order completely untracked: the attempt row remains,
identifying the product, the authoritative price, and the exact amount this
server intended to charge.

SCOPE OF THIS PHASE (K.19). The attempt records the INTENT and the eventual
disposition only. It does NOT yet carry the returned PayPal order id (that is
Phase K.20), and there is no reconciliation or operator recovery for it yet
(Phase K.21). It is a foundation: a reservation plus a conservative failure
classification, nothing more.

IT IS NOT THE LEDGER, AND IT IS NOT AN AUTHORITY. The `transactions` ledger
remains the record of a real purchase; this table never authorizes, captures,
or refunds anything, and it is only ever written AFTER the Policy Engine has
allowed or an explicit approval has covered the purchase. Every field is
server-derived; no client or AI value reaches it.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class CatalogAttemptStatus(str, Enum):
    """Lifecycle of one catalog purchase attempt.

    CREATING   A reservation, committed BEFORE PayPal's create-order endpoint
               is contacted. It means "a create-order call is in flight, or
               its outcome was never learned" — never "an order exists". A row
               is LEFT in this state on an AMBIGUOUS failure (a transport error
               or timeout, where PayPal may or may not have created an order)
               and on a crash, precisely because the outcome is unknown and a
               blind retry could duplicate an order. It is never automatically
               retried here.
    FAILED     A DEFINITIVE pre-order failure: the current error contract
               establishes that no PayPal order was created (the failure
               occurred before the create-order POST — a checkout-config fault
               or an auth-token failure). Terminal.
    FINALIZED  The create-order call succeeded AND the `transactions` ledger
               row was written; `transaction_id` links to it. Terminal. This
               is the only success state, and it is set only after the
               existing, unchanged finalization has committed the ledger row.

    There is deliberately no "safely retryable" state: an ambiguous outcome
    stays CREATING, not a state that invites an automatic second create-order.
    """

    CREATING = "CREATING"
    FAILED = "FAILED"
    FINALIZED = "FINALIZED"


class CatalogPurchaseAttempt(BaseModel):
    """One catalog purchase attempt, as stored and as returned.

    Holds order metadata and disposition only — no credentials, tokens, or
    secrets, and (in this phase) not the PayPal order id.
    """

    attempt_id: str = Field(
        description="PayPilot's own id, e.g. 'cpa_<hex>'. Server-generated."
    )
    product_id: str = Field(description="The catalog product being purchased.")
    catalog_price_inr: int = Field(
        description="The authoritative catalog price in INR at reservation "
        "time — read from the catalog, never from a client or AI value."
    )
    paypal_amount: str = Field(
        description="The amount this server intends to send to PayPal, as the "
        "two-decimal string PayPal expects. Derived by the existing INR->USD "
        "conversion. An intent record, never an input."
    )
    paypal_currency: str = Field(
        description="The currency this server intends to charge (USD in the "
        "demo). Intent record, like paypal_amount."
    )
    paypal_order_id: Optional[str] = Field(
        default=None,
        description="The PayPal order id. ALWAYS null in Phase K.19 — "
        "persisting it on the attempt is Phase K.20. Nullable and UNIQUE so a "
        "later phase can fill it without a migration and one order id maps to "
        "at most one attempt.",
    )
    claim_key: str = Field(
        description="A unique per-attempt key. UNIQUE in the table, so the "
        "reservation INSERT is an atomic single-row claim and a future stable "
        "key can make reservation idempotent. In Phase K.19 it is a fresh "
        "server-generated token (one logical create-order invocation)."
    )
    paypal_request_id: Optional[str] = Field(
        default=None,
        description="The stable PayPal-Request-Id sent on this attempt's "
        "create_order call (Phase 11F-K.22a). Derived from the unique "
        "attempt_id, persisted at reservation time BEFORE PayPal is contacted, "
        "and never overwritten. Null only for a row that predates the column. "
        "An output/audit value; no request can set it.",
    )
    recovery_claimed_at: Optional[str] = Field(
        default=None,
        description="ISO 8601 UTC instant at which an operator-gated "
        "create-order REPLAY recovery claimed this CREATING attempt (Phase "
        "11F-K.22b), or null when no replay is in flight. A durable "
        "compare-and-set claim so at most one worker replays at a time; it is "
        "cleared when the replay completes and never affects eligibility "
        "(which is measured from created_at). An internal audit value; no "
        "request can set it.",
    )
    recovery_claim_token: Optional[str] = Field(
        default=None,
        description="A unique token (UUID hex) generated fresh for each "
        "successful recovery claim (Phase 11F-K.22b ownership guard). The "
        "worker that wins the claim receives this token and must present it "
        "to release the claim; a stale-reclaim overwrites it, so an old "
        "worker's release becomes a no-op. Null when no claim is held.",
    )
    status: CatalogAttemptStatus
    transaction_id: Optional[str] = Field(
        default=None,
        description="Link to the finalized `transactions` row, set only when "
        "the attempt reaches FINALIZED. Null while CREATING or FAILED.",
    )
    created_at: str = Field(description="ISO 8601 UTC. Server-generated.")
    updated_at: str = Field(description="ISO 8601 UTC. Server-generated.")


class CatalogAttemptTriage(BaseModel):
    """Operator-triage projection of one catalog attempt (Phase 11F-K.21).

    A deliberately narrow view for the recovery endpoints. The attempt row
    holds no credential, access token, approval URL, or raw payload, so this
    exposes only what an operator needs to triage a stuck catalog purchase.
    has_paypal_order_id makes explicit the one distinction that decides how a
    stuck CREATING attempt is recovered: True means a PayPal order id is
    persisted (a read-first reconciliation can look the order up), False means
    none is recorded (the create-order outcome is ambiguous and only manual
    review is safe).
    """

    attempt_id: str
    product_id: str
    catalog_price_inr: int
    paypal_amount: str
    paypal_currency: str
    paypal_order_id: Optional[str] = None
    has_paypal_order_id: bool = False
    status: CatalogAttemptStatus
    transaction_id: Optional[str] = None
    created_at: str
    updated_at: str

    @classmethod
    def from_record(cls, record: "CatalogPurchaseAttempt") -> "CatalogAttemptTriage":
        return cls(
            attempt_id=record.attempt_id,
            product_id=record.product_id,
            catalog_price_inr=record.catalog_price_inr,
            paypal_amount=record.paypal_amount,
            paypal_currency=record.paypal_currency,
            paypal_order_id=record.paypal_order_id,
            has_paypal_order_id=bool((record.paypal_order_id or "").strip()),
            status=record.status,
            transaction_id=record.transaction_id,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class UnresolvedCatalogAttemptsResponse(BaseModel):
    """Response for GET /purchase/attempts/unresolved (Phase 11F-K.21).

    A read-only triage list of STALE, non-finalized catalog attempts (CREATING
    only, older than the staleness grace period — FAILED is terminal and is not
    an actionable orphan). It exposes no way to mutate a status and returns no
    arbitrary rows, only the bounded projection above.
    """

    attempts: list[CatalogAttemptTriage]
    count: int = Field(description="Number of attempts returned (<= limit).")


class CatalogAttemptReconcileReport(BaseModel):
    """Response for POST /purchase/attempts/{attempt_id}/reconcile (K.21).

    An operator recovery REPORT, not a payment result. Reconciliation is
    read-first: it re-reads PayPal by the server-stored order id and settles a
    CREATING attempt to FINALIZED only on conclusive, independently verified
    evidence. It NEVER creates, approves, authorizes, captures, or refunds.
    """

    attempt: CatalogAttemptTriage = Field(
        description="The attempt AFTER reconciliation, as stored."
    )
    attempt_id: str
    outcome: str = Field(
        description="recovered_finalized | already_finalized | "
        "manual_review_required | inconclusive | review_required | "
        "not_applicable."
    )
    status: str = Field(description="The resulting stored CatalogAttemptStatus.")
    reconciled: bool = Field(
        description="True ONLY when THIS call settled the attempt to FINALIZED "
        "on verified evidence. False for an idempotent replay and for every "
        "non-finalizing outcome."
    )
    review_required: bool = Field(
        default=False,
        description="True when a human must inspect the attempt (a "
        "definitive-looking discrepancy, a removed product, or a no-id "
        "attempt that cannot be safely auto-resolved).",
    )
    retryable: bool = Field(
        default=False,
        description="True when the outcome may change on a later retry (e.g. "
        "PayPal was unreadable).",
    )
    transaction_id: Optional[str] = Field(
        default=None,
        description="The linked `transactions` id when the attempt is "
        "FINALIZED; null otherwise.",
    )
    message: str = Field(description="Human-readable explanation of the outcome.")
    detail: Optional[dict] = Field(
        default=None,
        description="Safe diagnostic codes from reconciliation. Never a "
        "credential or raw payload.",
    )
