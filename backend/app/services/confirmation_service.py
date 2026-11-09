"""Explicit confirmation of a session's pending action (Phase 8E Step 4).

The ONLY bridge from conversational proposal to execution:

    stored pending action (references only)
        + explicit {"confirm": true}
        -> existing deterministic execution services.

Division of responsibility (nothing new is invented here):
  - The PENDING ACTION identifies WHAT is being confirmed (a product_id
    or a transaction_id — nothing else).
  - The CONFIRMATION REQUEST establishes THAT the user explicitly
    confirmed (an explicit boolean; calling the endpoint alone is not
    authorization).
  - The EXISTING SERVICES decide WHETHER the action is currently
    permitted and HOW PayPal is called: approve_purchase() re-fetches
    the catalog product and re-runs the Policy Engine from scratch
    (stored decision snapshots are never trusted), and
    refund_transaction() re-fetches the transaction, re-checks
    deterministic eligibility, and uses only the server-stored capture id.

Pending-action lifecycle:
  - Executed, or terminally not executable (policy DENY, already
    refunded, not refundable, record gone): the pending action is
    CLEARED — re-confirming can never duplicate a PayPal order/refund,
    because the action is consumed before any such retry.
  - Transient upstream failure (PayPal unreachable / 5xx BEFORE any
    order or refund exists): the pending action is RETAINED, because the
    existing services create orders/ledger records only after PayPal
    succeeds, so re-confirming is a safe retry, not a duplicate.
  - Expired or absent: nothing executes (session_service never returns
    an expired action as active), and nothing is revived.

No Gemini/provider import anywhere in this module — confirmation is
fully deterministic.
"""

from __future__ import annotations

import logging

from ..paypal import PayPalAuthError, PayPalOrderError, PayPalRefundError
from ..schemas.confirmation import ConfirmationResponse, RefundResult
from ..schemas.intent import Authorization
from ..schemas.session import PendingAction, PendingActionType, TurnRole
from .currency_service import CurrencyConversionError
from .purchase_execution_service import approve_purchase
from .refund_service import refund_transaction
from .session_service import (
    append_turn,
    claim_pending_action,
    clear_pending_action,
    get_pending_action,
    get_session,
    restore_pending_action,
)

logger = logging.getLogger("paypilot.confirmation")


class NoPendingActionError(Exception):
    """No ACTIVE pending action exists for this session (none was ever
    proposed, it was already consumed, or it has expired)."""


def confirm_pending_action(session_id: str, confirm: bool) -> ConfirmationResponse:
    """Decline or execute the session's stored pending action.

    Raises:
        SessionNotFoundError: unknown session.
        NoPendingActionError: no active (non-expired) pending action.
        Everything the existing execution services raise, propagated for
        the endpoint to map with the established conventions.
    """
    session = get_session(session_id)  # unknown session -> 404 upstream
    sid = session.session_id

    action = get_pending_action(sid)  # expired actions are never returned
    if action is None:
        raise NoPendingActionError(
            f"Session {sid} has no active pending action to confirm "
            f"(none proposed, already completed, or expired)"
        )

    if not confirm:
        # Explicitly declined: execute nothing, keep the proposal as-is so
        # the user can still confirm it before it expires.
        logger.info("Confirmation declined: session=%s", sid)
        return ConfirmationResponse(
            session_id=sid,
            action_type=action.action_type,
            confirmed=False,
            executed=False,
            message=(
                "Understood — nothing was executed. The proposal is still "
                "pending; confirm it before it expires or make a new request."
            ),
            pending_action=action,
        )

    if action.action_type == PendingActionType.PURCHASE:
        return _confirm_purchase(sid, action)
    return _confirm_refund(sid, action)


# --- PURCHASE ---------------------------------------------------------------------


def _confirm_purchase(sid: str, action: PendingAction) -> ConfirmationResponse:
    """Execute a confirmed purchase through the EXISTING approval path.

    CONCURRENCY GATE (Phase 11F-K.12). The pending purchase action is consumed
    by an ATOMIC claim BEFORE any PayPal call, so only one of several
    concurrent confirmations proceeds; the rest observe an already-consumed
    action and get a controlled NoPendingActionError (409) without ever
    reaching create_order. This is what prevents two concurrent confirmations
    from creating two PayPal orders.

    approve_purchase() then re-fetches the product from the catalog (current
    authoritative price) and re-runs the Policy Engine from scratch. The
    confirmation call is the explicit approval event, so the server-side
    authorization states exactly that and nothing more: the user requested
    this purchase. No autonomy, no limits, no client input.
    """
    # Atomic claim-and-consume: the sole winner receives the action; a
    # concurrent loser (or an action that expired between the routing peek and
    # this claim) receives None and becomes a 409 that never calls PayPal.
    claimed = claim_pending_action(sid, PendingActionType.PURCHASE)
    if claimed is None:
        raise NoPendingActionError(
            f"Session {sid} has no active pending action to confirm "
            f"(already confirmed, consumed, or expired)"
        )

    try:
        result = approve_purchase(
            product_id=claimed.product_id,
            authorization=Authorization(purchase_requested=True),
        )
    except (PayPalAuthError, CurrencyConversionError):
        # DEFINITIVE pre-order failure: the auth-token fetch precedes the
        # create-order POST (PayPalAuthError), and currency conversion precedes
        # the reservation entirely (CurrencyConversionError), so NO PayPal order
        # was created. RESTORE the claimed action (original expiry, TTL not
        # extended) so an explicit re-confirm is a safe retry, then re-raise.
        restore_pending_action(sid, claimed)
        logger.error("Purchase confirmation failed upstream; action restored for retry")
        raise
    except PayPalOrderError:
        # AMBIGUOUS outcome (Phase 11F-K.21). PayPalOrderError conflates a
        # transport error / timeout — where PayPal MAY have created an order —
        # with non-2xx and 2xx-without-id, and the error contract carries no
        # structured discriminator. The claimed action is therefore NOT
        # restored: re-confirming could issue a SECOND create_order for an
        # order that may already exist. The action stays consumed, and the
        # durable CREATING attempt (K.19/K.20) is left for read-first operator
        # or webhook reconciliation instead of a blind retry.
        logger.error(
            "Purchase confirmation hit an AMBIGUOUS PayPal outcome; action "
            "consumed (NOT restored) so no second order can be issued. The "
            "unresolved attempt is left for reconciliation."
        )
        raise
    except Exception:
        # Terminal (e.g. product no longer in catalog): the claim already
        # consumed the action and it is NOT restored — re-confirming can never
        # succeed — and the existing error is surfaced.
        logger.error("Purchase confirmation terminally failed; action stays consumed")
        raise

    if result.status == "denied":
        # The CURRENT policy run says DENY: no PayPal call happened inside the
        # service. The claim already consumed the action, so the dead proposal
        # cannot be re-confirmed.
        message = (
            f"The purchase of {result.product_name} cannot proceed: "
            f"{result.policy_decision.reason} No order was created."
        )
        append_turn(sid, TurnRole.ASSISTANT, message)
        logger.info(
            "Confirmed purchase denied by current policy: session=%s (%s)",
            sid,
            result.policy_decision.reason_code.value,
        )
        return ConfirmationResponse(
            session_id=sid,
            action_type=PendingActionType.PURCHASE,
            confirmed=True,
            executed=False,
            message=message,
            purchase_result=result,
        )

    # order_created: the one shared PayPal path ran. The action was already
    # consumed by the atomic claim above, so a repeated confirmation (e.g.
    # after a lost response) cannot double-order.
    message = (
        f"Purchase confirmed: a PayPal order for {result.product_name} "
        f"({result.paypal_amount} {result.paypal_currency}) was created. "
        f"Complete the payment approval on PayPal to finish; nothing is "
        f"charged until then."
    )
    append_turn(sid, TurnRole.ASSISTANT, message)
    logger.info(
        "Confirmed purchase executed: session=%s order=%s txn=%s",
        sid,
        result.paypal_order_id,
        result.transaction_id,
    )
    return ConfirmationResponse(
        session_id=sid,
        action_type=PendingActionType.PURCHASE,
        confirmed=True,
        executed=True,
        message=message,
        purchase_result=result,
    )


# --- REFUND ------------------------------------------------------------------------


def _confirm_refund(sid: str, action: PendingAction) -> ConfirmationResponse:
    """Execute a confirmed refund through the EXISTING refund service.

    refund_transaction() re-fetches the transaction, re-checks the
    deterministic eligibility rules (the stored status snapshot is never
    trusted), and refunds only the server-stored capture id.
    """
    try:
        refund = refund_transaction(action.transaction_id)
    except (PayPalAuthError, PayPalRefundError):
        # Transient/upstream failure: the ledger is still CAPTURED and no
        # refund exists, so keep the action for a safe explicit retry.
        logger.error("Refund confirmation failed upstream; action retained")
        raise
    except Exception:
        # Terminal per the existing service (already refunded, not
        # refundable, unknown transaction, missing/unknown capture id):
        # consume the action — it can never execute — and let the existing
        # error behavior surface unchanged. PayPal is not called again.
        clear_pending_action(sid)
        raise

    # Refund succeeded and the ledger is REFUNDED: consume the action so a
    # repeated confirmation cannot attempt a second refund.
    clear_pending_action(sid)
    message = (
        f"Refund confirmed: {refund['amount']} {refund['currency']} was "
        f"refunded via PayPal (refund id {refund['refund_id']}). The "
        f"transaction is now marked REFUNDED."
    )
    append_turn(sid, TurnRole.ASSISTANT, message)
    logger.info(
        "Confirmed refund executed: session=%s txn=%s refund=%s",
        sid,
        refund["transaction_id"],
        refund["refund_id"],
    )
    return ConfirmationResponse(
        session_id=sid,
        action_type=PendingActionType.REFUND,
        confirmed=True,
        executed=True,
        message=message,
        refund_result=RefundResult(**refund),
    )
