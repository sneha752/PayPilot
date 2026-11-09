"""Webhook-triggered reconciliation for EXTERNAL purchase orders (Phase 11F-F).

A VERIFIED PayPal webhook (Phase 8A signature verification) is only a
TRIGGER here. This service never trusts the webhook payload as payment
evidence. It resolves the event to one of THIS server's own
external_purchase_orders rows by a SERVER-STORED PayPal order id or capture
id, then performs an INDEPENDENT read-only `get_order` and applies the
existing strict verification (external_purchase_service) before moving the
record forward with a database compare-and-set.

IT MOVES NO MONEY. `capture_order` and `refund_capture` are not imported and
are never called; no order is created, authorized, captured or refunded.
Every transition it can make is justified by PayPal's own order read, not by
the event body.

SEPARATION IS PRESERVED. This service only ever touches the external table,
through external_purchase_service. The catalog `transactions` ledger is
neither read nor written here, and the webhook endpoint only routes an event
to external reconciliation when catalog reconciliation did not own it, so a
single event can mutate at most one ledger.

WHAT IT CANNOT DO. It never regresses CAPTURED, never rewrites a stored
capture id, never releases a stuck CAPTURING reservation, and never promotes
CAPTURE_FAILED (the stored row cannot safely distinguish an ambiguous
timeout from a verified mismatch, so that case is left for manual review).
"""

from __future__ import annotations

import logging
from typing import Optional

from ..schemas.external_purchase import ExternalPurchaseOrder
from . import external_purchase_service

# Reuse the catalog reconciler's event-id extraction verbatim: parsing a
# PayPal event into (order_id, capture_id) is a shared concern, and keeping a
# second copy here would let the two drift. It reads only the event shape; it
# performs no lookup, no PayPal call and no write.
from .webhook_reconciliation_service import _identify

logger = logging.getLogger("paypilot.external_reconciliation")

#: event_type -> which reconciliation this phase runs. The webhook is only a
#: trigger; the transition is justified by an INDEPENDENT get_order read,
#: never by the payload. CHECKOUT.ORDER.APPROVED can establish buyer
#: approval; the two capture events are a trigger to re-check for a verified
#: completed capture (a DENIED event never promotes anything on its own — it
#: only causes an independent read, which promotes to CAPTURED solely when
#: PayPal itself shows one matching COMPLETED capture).
_APPROVAL_EVENTS = frozenset({"CHECKOUT.ORDER.APPROVED"})
_CAPTURE_EVENTS = frozenset(
    {"PAYMENT.CAPTURE.COMPLETED", "PAYMENT.CAPTURE.DENIED"}
)


def _resolve(
    order_id: Optional[str], capture_id: Optional[str]
) -> Optional[ExternalPurchaseOrder]:
    """The external execution owning a SERVER-STORED order or capture id.

    Ownership is never inferred from the payload: get_by_paypal_order /
    get_by_paypal_capture match only an id this server itself stored, so an
    id the webhook made up resolves to nothing and mutates nothing.
    """
    if order_id:
        record = external_purchase_service.get_by_paypal_order(order_id)
        if record is not None:
            return record
    if capture_id:
        record = external_purchase_service.get_by_paypal_capture(capture_id)
        if record is not None:
            return record
    return None


def reconcile_external_event(event: dict) -> dict:
    """Reconcile one VERIFIED webhook event against the external ledger.

    Never raises for an event that simply does not apply: unknown types,
    unidentifiable events and events for no known external execution are
    reported in the result so the endpoint can still acknowledge the webhook
    (a non-2xx would only make PayPal redeliver an event we cannot use).

    Returns a dict with "action" (ignored_event_type, unidentifiable,
    external_execution_not_found, or one of the reconciliation outcomes:
    approved, captured, already_applied, not_applicable, order_not_approved,
    unresolved_no_capture, inconclusive, review_required) plus identifying
    fields and, when present, a "detail" describing the evidence.
    """
    event_type = event.get("event_type")

    if event_type not in _APPROVAL_EVENTS and event_type not in _CAPTURE_EVENTS:
        # Verified but not an event external reconciliation acts on.
        return {"action": "ignored_event_type", "event_type": event_type}

    order_id, capture_id = _identify(event)
    if not order_id and not capture_id:
        return {"action": "unidentifiable", "event_type": event_type}

    execution = _resolve(order_id, capture_id)
    if execution is None:
        # Not an error: the id belongs to no external execution of this
        # server (e.g. a catalog order, or an order created elsewhere).
        logger.info(
            "No external execution for webhook (event_type=%s order=%s)",
            event_type,
            order_id,
        )
        return {
            "action": "external_execution_not_found",
            "event_type": event_type,
            "paypal_order_id": order_id,
        }

    if event_type in _APPROVAL_EVENTS:
        outcome, updated, detail = external_purchase_service.reconcile_buyer_approval(
            execution
        )
    else:
        outcome, updated, detail = external_purchase_service.reconcile_capture(
            execution
        )

    result = {
        "action": outcome,
        "event_type": event_type,
        "execution_id": updated.execution_id,
        "paypal_order_id": updated.paypal_order_id,
        "status": updated.status.value,
    }
    if detail:
        result["detail"] = detail

    logger.info(
        "External reconciliation: event=%s execution=%s action=%s status=%s",
        event_type,
        updated.execution_id,
        outcome,
        updated.status.value,
    )
    return result
