"""Webhook-triggered reconciliation for CATALOG purchase ATTEMPTS (Phase 11F-K.21).

This is the THIRD and last webhook fallback, reached only when a verified event
matched neither a catalog `transactions` row (webhook_reconciliation_service)
nor an external purchase order (external_reconciliation_service). It exists for
exactly one case: a catalog purchase ATTEMPT that carries a persisted PayPal
order id (K.20) but never finalized into a `transactions` row — the catalog
orphan.

A VERIFIED webhook is only a TRIGGER. Ownership is never inferred from the
payload: the event's order id is resolved against a SERVER-STORED order id on a
CREATING attempt, and the transition is then justified by an INDEPENDENT
read-only get_order (purchase_execution_service.reconcile_creating_attempt),
never by the event body.

IT MOVES NO MONEY. No order is created, approved, authorized, captured, or
refunded. It only ever settles a CREATING attempt to FINALIZED (creating the
ledger row in its normal ORDER_CREATED state), and only on conclusive evidence;
a captured/mismatched/unreadable order is left untouched for review. It never
regresses a finalized transaction and never marks a transaction captured from
an event alone.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..schemas.catalog_attempt import CatalogAttemptStatus, CatalogPurchaseAttempt
from . import catalog_attempt_service, purchase_execution_service

# Reuse the catalog reconciler's event-id extraction verbatim (shared concern;
# keeping a second copy would let the two drift). It only reads the event shape.
from .webhook_reconciliation_service import _identify

logger = logging.getLogger("paypilot.catalog_attempt_reconciliation")

#: The event types that can trigger catalog-attempt recovery. The webhook is
#: only a trigger; the transition is justified by the independent get_order.
#: CHECKOUT.ORDER.APPROVED is the natural one; the capture events are accepted
#: as a trigger to re-check, but the independent read will only finalize a
#: pre-capture order (a COMPLETED/captured order is left for review).
_RECONCILABLE_EVENTS = frozenset(
    {
        "CHECKOUT.ORDER.APPROVED",
        "PAYMENT.CAPTURE.COMPLETED",
        "PAYMENT.CAPTURE.DENIED",
    }
)


def _resolve(order_id: Optional[str]) -> Optional[CatalogPurchaseAttempt]:
    """The CREATING catalog attempt owning a SERVER-STORED order id, or None.

    Ownership is never inferred from the payload: get_by_order matches only an
    id this server itself persisted onto an attempt. A FINALIZED attempt is
    deliberately NOT returned — its order is already owned by a `transactions`
    row (handled by the primary catalog reconciler), so there is nothing to
    recover here.
    """
    if not order_id:
        return None
    attempt = catalog_attempt_service.get_by_order(order_id)
    if attempt is None or attempt.status is not CatalogAttemptStatus.CREATING:
        return None
    return attempt


def reconcile_catalog_attempt_event(event: dict) -> dict:
    """Reconcile one VERIFIED webhook event against the catalog ATTEMPT table.

    Never raises for an event that simply does not apply: unknown types,
    unidentifiable events, and events for no CREATING attempt are reported in
    the result so the endpoint can still acknowledge the webhook.

    Returns a dict with "action" (ignored_event_type, unidentifiable,
    catalog_attempt_not_found, or one of the reconcile_creating_attempt
    outcomes: recovered_finalized, already_finalized, not_applicable,
    manual_review_required, inconclusive, review_required) plus identifying
    fields and, when present, a "detail".
    """
    event_type = event.get("event_type")
    if event_type not in _RECONCILABLE_EVENTS:
        return {"action": "ignored_event_type", "event_type": event_type}

    order_id, _capture_id = _identify(event)
    if not order_id:
        return {"action": "unidentifiable", "event_type": event_type}

    attempt = _resolve(order_id)
    if attempt is None:
        logger.info(
            "No CREATING catalog attempt for webhook (event_type=%s order=%s)",
            event_type,
            order_id,
        )
        return {
            "action": "catalog_attempt_not_found",
            "event_type": event_type,
            "paypal_order_id": order_id,
        }

    outcome, updated, detail = purchase_execution_service.reconcile_creating_attempt(
        attempt
    )

    result = {
        "action": outcome,
        "event_type": event_type,
        "attempt_id": updated.attempt_id,
        "paypal_order_id": updated.paypal_order_id,
        "status": updated.status.value,
    }
    if detail:
        result["detail"] = detail

    logger.info(
        "Catalog-attempt reconciliation: event=%s attempt=%s action=%s status=%s",
        event_type,
        updated.attempt_id,
        outcome,
        updated.status.value,
    )
    return result
