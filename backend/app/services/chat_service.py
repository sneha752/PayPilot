"""Conversational chat orchestration (Phase 8E Step 3).

One chat turn: session -> persist user turn -> route -> run the matching
READ/PLANNING flow -> persist assistant turn -> typed response.

CRITICAL SAFETY RULE: this module NEVER executes a payment or refund and
never calls PayPal — even when the Policy Engine's answer (obtained only
through the existing planning pipeline) is ALLOW_AUTONOMOUS_PURCHASE. In
the conversational flow, every money action becomes a reference-only
pending action in the session store, awaiting the future explicit
confirmation endpoint, which re-fetches and re-validates everything.

Per route:
    SHOP      -> purchase_orchestrator.plan() (existing pipeline, used
                 directly — never via HTTP). ALLOW/REQUIRE => pending
                 PURCHASE action; DENY/no candidates => no action.
    REFUND    -> deterministic transaction identification from the
                 EXISTING ledger. Unambiguous CAPTURED match => pending
                 REFUND action; anything unclear => ask, never guess.
    HISTORY   -> existing list_transactions(); read-only; no action.
    SMALLTALK -> fixed deterministic text; no extra AI call; no action.

Assistant messages are deterministic templates built from catalog/ledger
data — raw model output is never exposed.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from ..agents.router_agent import route_message
from ..schemas.chat import ChatData, ChatResponse
from ..schemas.plan import PlanResponse
from ..schemas.policy import PolicyOutcome
from ..schemas.routing import ChatRoute
from ..schemas.session import PendingActionType, TurnRole
from ..schemas.transaction import TransactionRecord, TransactionStatus
from .purchase_orchestrator import plan as build_plan
from .session_service import (
    append_turn,
    create_session,
    get_pending_action,
    get_session,
    set_pending_action,
)
from .transaction_service import list_transactions

logger = logging.getLogger("paypilot.chat")

_TXN_ID_PATTERN = re.compile(r"txn_[0-9a-f]{8,}", re.IGNORECASE)

_SMALLTALK_MESSAGE = (
    "Hi, I'm PayPilot! I can find products in the catalog, evaluate a "
    "purchase against your own spending rules, prepare PayPal purchases "
    "for your explicit approval, show your transaction history, and help "
    "you start a refund. Nothing is ever charged or refunded without your "
    "confirmation. What would you like to do?"
)


def handle_chat(message: str, session_id: Optional[str] = None) -> ChatResponse:
    """Process one conversational turn. Read/plan only — never executes.

    Raises:
        SessionNotFoundError: the supplied session_id does not exist.
        AIConfigError / AIServiceError / InvalidAIResponseError /
        RouteValidationError: routing/planning model failures (the turn
            fails cleanly; no pending action is ever created on the way).
        IntentValidationError / RecommendationValidationError: planning
            pipeline validation failures (SHOP route only).
    """
    # 1. Obtain or create the session (unknown ids raise -> 404 upstream).
    session = get_session(session_id) if session_id else create_session()
    sid = session.session_id

    # 2. Persist the user's turn.
    append_turn(sid, TurnRole.USER, message)

    # 3. Classify. Any failure raises BEFORE any flow runs or any pending
    #    action could be written — a failed model can never cause an action.
    routing = route_message(message)

    # 4. Run the appropriate read/planning flow.
    if routing.route == ChatRoute.SHOP:
        assistant_message, data = _handle_shop(sid, message)
    elif routing.route == ChatRoute.REFUND:
        assistant_message, data = _handle_refund(sid, message)
    elif routing.route == ChatRoute.HISTORY:
        assistant_message, data = _handle_history()
    else:  # SMALLTALK — deterministic, no further AI call.
        assistant_message, data = _SMALLTALK_MESSAGE, ChatData()

    # 5. Persist the assistant's turn.
    append_turn(sid, TurnRole.ASSISTANT, assistant_message)

    # 6. Typed response with the session's currently ACTIVE pending action.
    return ChatResponse(
        session_id=sid,
        route=routing.route,
        assistant_message=assistant_message,
        pending_action=get_pending_action(sid),
        data=data,
    )


# --- SHOP -----------------------------------------------------------------------


def _handle_shop(sid: str, message: str) -> tuple[str, ChatData]:
    """Existing planning pipeline; proposals only, never execution."""
    plan_result: PlanResponse = build_plan(message)
    data = ChatData(plan=plan_result)

    if plan_result.status == "no_candidates":
        return (
            "I couldn't find any catalog product that satisfies all your "
            "requirements. Try relaxing a constraint (like the budget) and "
            "ask again.",
            data,
        )
    if plan_result.status == "no_suitable_candidate":
        return (
            "I found products matching your constraints, but none seemed "
            "genuinely suitable for what you described, so I'm not "
            "recommending one. You can adjust your requirements and ask again.",
            data,
        )

    # status == "planned": a product was selected and policy has ruled.
    product = plan_result.selected_product
    decision = plan_result.policy_decision

    if decision.decision == PolicyOutcome.DENY:
        # No pending action for a denied purchase — nothing to confirm.
        logger.info("Chat SHOP denied by policy: %s", decision.reason_code.value)
        return (
            f"I found {product.name} at ₹{product.price_inr}, but this "
            f"purchase cannot proceed under your purchase policy: "
            f"{decision.reason} No purchase was prepared.",
            data,
        )

    # ALLOW_AUTONOMOUS_PURCHASE or REQUIRE_USER_APPROVAL: in conversation,
    # BOTH require the future explicit confirmation step — references only.
    set_pending_action(
        sid,
        PendingActionType.PURCHASE,
        product_id=product.id,
        decision_snapshot=decision.decision.value,
    )
    logger.info(
        "Chat SHOP pending purchase: session=%s product=%s decision=%s",
        sid,
        product.id,
        decision.decision.value,
    )
    return (
        f"I recommend {product.name} at ₹{product.price_inr} (catalog "
        f"price). Policy decision: {decision.decision.value} — "
        f"{decision.reason} Nothing has been ordered or charged; this "
        f"purchase is prepared and waiting for your explicit confirmation.",
        data,
    )


# --- REFUND ----------------------------------------------------------------------


def _handle_refund(sid: str, message: str) -> tuple[str, ChatData]:
    """Identify the refund target deterministically; never execute, never guess."""
    refundable = list_transactions(status=TransactionStatus.CAPTURED, limit=100)

    target = _resolve_refund_target(message, refundable)

    if target is None:
        if not refundable:
            return (
                "I couldn't find any refundable purchase: only captured "
                "(paid) transactions can be refunded, and there are none "
                "right now. You can say 'show my purchases' to review your "
                "history.",
                ChatData(transactions=[]),
            )
        # Ambiguous: list candidates, ask, and create NO pending action.
        listing = "; ".join(
            f"{t.product_name} ({t.transaction_id})" for t in refundable[:5]
        )
        return (
            f"I found {len(refundable)} refundable purchase(s): {listing}. "
            f"Which one would you like to refund? Please mention the product "
            f"name or transaction id.",
            ChatData(transactions=refundable),
        )

    # Unambiguous CAPTURED transaction: reference-only pending action. The
    # confirmation endpoint will re-fetch it and run the existing refund
    # service — no capture id, amount, or currency is stored here.
    set_pending_action(
        sid,
        PendingActionType.REFUND,
        transaction_id=target.transaction_id,
        decision_snapshot=target.status.value,
    )
    logger.info(
        "Chat REFUND pending: session=%s transaction=%s", sid, target.transaction_id
    )
    return (
        f"I found your purchase: {target.product_name} "
        f"({target.transaction_id}), paid {target.paypal_amount} "
        f"{target.paypal_currency}, status {target.status.value}. A full "
        f"refund is prepared and waiting for your explicit confirmation — "
        f"nothing has been refunded yet.",
        ChatData(transaction=target),
    )


def _resolve_refund_target(
    message: str, refundable: list[TransactionRecord]
) -> Optional[TransactionRecord]:
    """Deterministically resolve which CAPTURED transaction the user means.

    In order: an explicit transaction id in the message; a product-name
    match that hits exactly ONE refundable transaction; the only
    refundable transaction when there is exactly one. Anything else is
    ambiguous -> None (never guess).
    """
    by_id = {t.transaction_id.lower(): t for t in refundable}

    id_match = _TXN_ID_PATTERN.search(message)
    if id_match:
        # An explicit id either resolves among refundable ones or not at all.
        return by_id.get(id_match.group(0).lower())

    lowered = message.lower()
    name_hits = [
        t
        for t in refundable
        if t.product_name.lower() in lowered
        or any(
            len(word) >= 4 and word in lowered
            for word in t.product_name.lower().split()
        )
    ]
    if len(name_hits) == 1:
        return name_hits[0]
    if len(name_hits) > 1:
        return None  # several products matched — ask, don't guess

    if len(refundable) == 1:
        return refundable[0]
    return None


# --- HISTORY ----------------------------------------------------------------------


def _handle_history() -> tuple[str, ChatData]:
    """Existing ledger history; read-only, no AI, no PayPal, no action."""
    transactions = list_transactions(limit=20)
    if not transactions:
        return ("You don't have any transactions yet.", ChatData(transactions=[]))
    return (
        f"You have {len(transactions)} transaction(s), newest first. "
        f"The most recent is {transactions[0].product_name} "
        f"({transactions[0].status.value}).",
        ChatData(transactions=transactions),
    )
