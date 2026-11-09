"""Conversational message-routing schemas (Phase 8E Step 2).

A route is a CLASSIFICATION of what flow a user's message belongs to —
nothing more. The schema is deliberately minimal: it carries no price,
amount, currency, PayPal id, authorization, policy decision, or anything
executable, so a routing result can never influence money movement. The
four routes are a closed enum; a model cannot invent a fifth.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ChatRoute(str, Enum):
    """The only four flows a conversational message can be routed to."""

    SHOP = "SHOP"
    REFUND = "REFUND"
    HISTORY = "HISTORY"
    SMALLTALK = "SMALLTALK"


class RouteResult(BaseModel):
    """Validated routing decision for one user message.

    summary is an optional short restatement of what the user wants
    (useful for logging/UI); it is inert data and never an instruction.
    Unknown fields in a model payload are dropped by Pydantic and can
    never surface here.
    """

    route: ChatRoute
    summary: Optional[str] = Field(
        default=None,
        max_length=200,
        description="Short normalized restatement of the user's request.",
    )
