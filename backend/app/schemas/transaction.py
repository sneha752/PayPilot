"""Transaction ledger schemas.

The local transaction record is a TRACKING layer, never the payment
authority: PayPal remains the source of truth for payment state, and a
record only transitions to CAPTURED after a successful PayPal capture
response. Records hold order metadata only — no credentials, tokens, or
secrets ever enter this schema.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel


class TransactionStatus(str, Enum):
    """Deterministic transaction lifecycle states.

    ORDER_CREATED and CAPTURED are set by the purchase flow (Phase 7C).
    ORDER_APPROVED, FAILED, and REFUNDED are reconciled from VERIFIED
    PayPal webhook events (Phase 8B) — webhooks report what happened at
    PayPal; they never trigger a capture or refund themselves.

    REFUNDING (Phase 11F-K.13) is a short-lived INTERMEDIATE claim: the
    refund service atomically moves CAPTURED -> REFUNDING so only one
    concurrent request can initiate a PayPal refund. It normally resolves to
    REFUNDED on success, or back to CAPTURED on a definitive no-funds failure;
    it is left REFUNDING only when the PayPal outcome is ambiguous (e.g. a
    timeout) and is then resolved by operator review or a verified
    PAYMENT.CAPTURE.REFUNDED webhook. A REFUNDING row is NOT refundable again.
    """

    ORDER_CREATED = "ORDER_CREATED"
    ORDER_APPROVED = "ORDER_APPROVED"
    CAPTURED = "CAPTURED"
    FAILED = "FAILED"
    REFUNDING = "REFUNDING"
    REFUNDED = "REFUNDED"


class TransactionRecord(BaseModel):
    """One purchase's lifecycle: product -> PayPal order -> PayPal capture."""

    transaction_id: str
    product_id: str
    product_name: str
    catalog_price_inr: int
    paypal_amount: str
    paypal_currency: str
    paypal_order_id: str
    paypal_capture_id: Optional[str] = None
    status: TransactionStatus
    created_at: str
    updated_at: str


class TransactionListResponse(BaseModel):
    """Response for GET /transactions: history page plus its size.

    Reuses TransactionRecord verbatim — one representation of a
    transaction everywhere, never a second incompatible shape.
    """

    transactions: list[TransactionRecord]
    count: int
