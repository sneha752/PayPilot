"""Schemas for the policy-gated purchase execution endpoint.

SECURITY: ExecuteRequest deliberately contains NO price, NO currency, NO
PayPal amount, and NO policy decision. The server derives every one of those
itself: the price comes from the local catalog, the decision from re-running
the deterministic Policy Engine, and the PayPal amount from the currency
service. Unknown fields a client sends (e.g. "price_inr": 1 or
"decision": "ALLOW_AUTONOMOUS_PURCHASE") are silently dropped by Pydantic
and can never influence execution.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from .intent import Authorization, HardConstraints
from .policy import PolicyDecision


class CaptureResult(BaseModel):
    """Purchase-level view of a successful PayPal capture.

    Built verbatim from the PayPal capture response — PayPal is the source
    of truth for the captured amount/currency; nothing here is recomputed
    or client-supplied.
    """

    status: Literal["captured"] = "captured"
    order_id: str
    order_status: Optional[str] = None
    capture_id: Optional[str] = None
    capture_status: Optional[str] = None
    amount: Optional[str] = None
    currency: Optional[str] = None

    # Local ledger tracking (None when no local transaction exists for this
    # order — the ledger never blocks or overrides a PayPal capture).
    transaction_id: Optional[str] = None
    transaction_status: Optional[str] = None


class ExecuteRequest(BaseModel):
    """Request body for POST /purchase/execute.

    The client names the product and supplies the authorization facts to
    evaluate (e.g. taken from a prior /ai/plan response). Nothing else is
    trusted from the client.
    """

    product_id: str = Field(min_length=1, description="Catalog id, e.g. 'LAP002'.")
    authorization: Authorization = Field(
        description="The user's financial authorization to evaluate."
    )
    hard_constraints: Optional[HardConstraints] = Field(
        default=None,
        description="Shopping constraints from the intent, if any.",
    )


class ExecutionResult(BaseModel):
    """Outcome of a policy-gated execution attempt.

    The PayPal fields are populated ONLY when status == "order_created";
    for "denied" and "approval_required" they are all None — no order
    exists, because PayPal was never called.
    """

    status: Literal["denied", "approval_required", "order_created"]

    # True when this result came from the explicit-approval path
    # (POST /purchase/approve); False for the autonomous path.
    approval_used: bool = False

    # PRODUCT INFORMATION — authoritative, from the local catalog.
    product_id: str
    product_name: str
    catalog_price_inr: int

    # POLICY DECISION — the deterministic engine's ruling, re-evaluated
    # server-side for this exact execution attempt.
    policy_decision: PolicyDecision

    # PayPal order facts (order_created only). Never credentials or tokens.
    paypal_currency: Optional[str] = None
    paypal_amount: Optional[str] = None
    demo_fx_rate: Optional[str] = None
    paypal_order_id: Optional[str] = None
    approve_url: Optional[str] = None

    # Local ledger record id (order_created only).
    transaction_id: Optional[str] = None
