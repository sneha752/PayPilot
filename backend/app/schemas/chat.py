"""Chat endpoint schemas (Phase 8E Step 3).

One conversational turn in, one typed conversational result out. The
response reuses the existing schemas verbatim (PlanResponse,
TransactionRecord, PendingAction) — no second representation of any
domain object, and no raw model output.

SECURITY: ChatRequest carries ONLY a session id and a message. There is
no field for a price, amount, currency, capture id, order id, or policy
decision, and unknown fields are dropped by Pydantic — nothing a client
sends through chat can influence catalog prices, policy results, or any
future execution.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator

from .plan import PlanResponse
from .routing import ChatRoute
from .session import PendingAction
from .transaction import TransactionRecord


class ChatRequest(BaseModel):
    """Request body for POST /chat."""

    session_id: Optional[str] = Field(
        default=None,
        description="Existing session to continue; omit to start a new one.",
    )
    message: str = Field(
        min_length=1,
        max_length=2000,
        description="The user's conversational message.",
    )

    @field_validator("message")
    @classmethod
    def _message_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value.strip()


class ChatData(BaseModel):
    """Typed per-route payload for the frontend. Exactly the fields the
    route produced are set; everything else stays None.

    - SHOP: plan (the full existing PlanResponse — intent, candidates,
      recommendation, selected product with catalog price, policy decision).
    - REFUND: transaction (the resolved refund target) or transactions
      (the refundable candidates when clarification is needed).
    - HISTORY: transactions (newest-first, from the existing ledger).
    - SMALLTALK: nothing.
    """

    plan: Optional[PlanResponse] = None
    transaction: Optional[TransactionRecord] = None
    transactions: Optional[list[TransactionRecord]] = None


class ChatResponse(BaseModel):
    """Structured result of one chat turn.

    pending_action is the session's ACTIVE pending action after this turn
    (None when the turn produced none and none survives). It contains
    references only; execution always requires the future explicit
    confirmation endpoint.
    """

    session_id: str
    route: ChatRoute
    assistant_message: str
    pending_action: Optional[PendingAction] = None
    data: ChatData = Field(default_factory=ChatData)
