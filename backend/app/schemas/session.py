"""Conversation-session schemas (Phase 8E Step 1).

A session remembers WHO is talking (identity + timestamps), WHAT was said
(turns), and WHAT the assistant last proposed (one pending action awaiting
explicit confirmation). It is pure state: nothing here executes a payment,
evaluates policy, or calls an AI provider.

SECURITY: a PendingAction stores only REFERENCES (a catalog product id or
a local transaction id) plus an inert snapshot of the deterministic
decision that produced it. It never stores a client-supplied price,
currency, amount, or PayPal capture id as authoritative — the future
confirmation step re-fetches the product/transaction and re-runs the
deterministic checks from scratch. No credential or secret can enter
these schemas.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, model_validator


class TurnRole(str, Enum):
    """Who produced a conversation turn."""

    USER = "user"
    ASSISTANT = "assistant"


class ConversationTurn(BaseModel):
    """One message in a session's conversation, in order of occurrence."""

    turn_id: str
    session_id: str
    role: TurnRole
    content: str = Field(min_length=1, max_length=4000)
    created_at: str


class PendingActionType(str, Enum):
    """The kinds of action a chat turn may propose for later confirmation.

    PURCHASE references a catalog product; REFUND references a local
    transaction. Confirmation (a later phase) re-validates everything —
    the pending action itself authorizes nothing.
    """

    PURCHASE = "PURCHASE"
    REFUND = "REFUND"


class PendingAction(BaseModel):
    """One proposal awaiting explicit user confirmation.

    References only: product_id for PURCHASE, transaction_id for REFUND.
    decision_snapshot is an inert, human-auditable copy of the
    deterministic decision/state at proposal time (e.g. the Policy
    Engine's "REQUIRE_USER_APPROVAL" or the transaction's "CAPTURED");
    it is never trusted at execution time.
    """

    action_type: PendingActionType
    product_id: Optional[str] = None
    transaction_id: Optional[str] = None
    decision_snapshot: Optional[str] = None
    created_at: str
    expires_at: str

    @model_validator(mode="after")
    def _reference_matches_type(self) -> "PendingAction":
        if self.action_type == PendingActionType.PURCHASE:
            if not self.product_id:
                raise ValueError("a PURCHASE pending action requires product_id")
        if self.action_type == PendingActionType.REFUND:
            if not self.transaction_id:
                raise ValueError("a REFUND pending action requires transaction_id")
        return self


class SessionRecord(BaseModel):
    """A conversation session's identity and lifecycle timestamps."""

    session_id: str
    created_at: str
    updated_at: str


class SessionDetail(BaseModel):
    """Full read-only view of one session (GET /sessions/{session_id}).

    Composes the existing schemas verbatim — no second representation.
    pending_action is the session's ACTIVE pending action (None when
    there is none or it has expired, per session_service semantics).
    """

    session_id: str
    created_at: str
    updated_at: str
    turns: list[ConversationTurn]
    pending_action: Optional[PendingAction] = None
