"""Explicit-confirmation schemas (Phase 8E Step 4).

The confirmation request is deliberately minimal: an explicit boolean and
nothing else. There is NO field for a product id, transaction id, price,
amount, currency, PayPal order/capture/refund id, policy decision, or
authorization limit — the server acts exclusively on the stored pending
action, and unknown JSON fields are dropped by Pydantic.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from .execution import ExecutionResult
from .session import PendingAction, PendingActionType


class ConfirmRequest(BaseModel):
    """Request body for POST /sessions/{session_id}/confirm.

    confirm is REQUIRED (no default): merely calling the endpoint is not
    an authorization — only an explicit {"confirm": true} is.
    """

    confirm: bool = Field(
        description="true to execute the stored pending action; false to decline."
    )


class RefundResult(BaseModel):
    """Typed mirror of the existing refund service's success result."""

    status: Literal["refunded"] = "refunded"
    transaction_id: str
    paypal_order_id: str
    paypal_capture_id: Optional[str] = None
    refund_id: Optional[str] = None
    refund_status: Optional[str] = None
    amount: Optional[str] = None
    currency: Optional[str] = None


class ConfirmationResponse(BaseModel):
    """Outcome of one confirmation call.

    executed is True only when a PayPal action actually happened
    (order created / refund completed). A policy DENY or a declined
    confirmation returns executed=False with the structured reason.
    purchase_result reuses the existing ExecutionResult verbatim;
    pending_action is whatever remains active on the session afterwards
    (the declined action when confirm=false, otherwise normally None).
    """

    session_id: str
    action_type: PendingActionType
    confirmed: bool
    executed: bool
    message: str
    purchase_result: Optional[ExecutionResult] = None
    refund_result: Optional[RefundResult] = None
    pending_action: Optional[PendingAction] = None
