"""Webhook → ledger reconciliation (Phase 8B).

Takes a PayPal webhook event that has ALREADY passed official signature
verification (Phase 8A) and reconciles the local SQLite transaction ledger
with what PayPal reports. Reconciliation is bookkeeping only: it records
state PayPal has already reached — it never captures, never refunds, never
calls PayPal, and never calls an AI provider.

Deterministic transition table (anything else is ignored, never guessed):

    CHECKOUT.ORDER.APPROVED      ORDER_CREATED                 -> ORDER_APPROVED
    PAYMENT.CAPTURE.COMPLETED    ORDER_CREATED/ORDER_APPROVED  -> CAPTURED
    PAYMENT.CAPTURE.DENIED       ORDER_CREATED/ORDER_APPROVED  -> FAILED
    PAYMENT.CAPTURE.REFUNDED     ORDER_CREATED/APPROVED/CAPTURED -> REFUNDED

Webhook delivery is at-least-once and unordered, so reconciliation is
idempotent (a repeat of the current state is a no-op) and a stale event
(e.g. CHECKOUT.ORDER.APPROVED arriving after CAPTURED) is ignored rather
than allowed to regress the record.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..schemas.transaction import TransactionRecord, TransactionStatus
from .transaction_service import (
    TransactionNotFoundError,
    get_transaction_by_capture,
    get_transaction_by_order,
    update_status,
)

logger = logging.getLogger("paypilot.webhook_reconciliation")

# event_type -> (target status, statuses the transition may start from).
_TRANSITIONS: dict[str, tuple[TransactionStatus, frozenset[TransactionStatus]]] = {
    "CHECKOUT.ORDER.APPROVED": (
        TransactionStatus.ORDER_APPROVED,
        frozenset({TransactionStatus.ORDER_CREATED}),
    ),
    "PAYMENT.CAPTURE.COMPLETED": (
        TransactionStatus.CAPTURED,
        frozenset({TransactionStatus.ORDER_CREATED, TransactionStatus.ORDER_APPROVED}),
    ),
    "PAYMENT.CAPTURE.DENIED": (
        TransactionStatus.FAILED,
        frozenset({TransactionStatus.ORDER_CREATED, TransactionStatus.ORDER_APPROVED}),
    ),
    # A refund implies a capture happened at PayPal, so it is accepted even
    # if the CAPTURED webhook was missed (out-of-order delivery). REFUNDING is
    # included (Phase 11F-K.13) so a verified PAYMENT.CAPTURE.REFUNDED event
    # reconciles a row left REFUNDING by an AMBIGUOUS refund outcome to its
    # true REFUNDED state.
    "PAYMENT.CAPTURE.REFUNDED": (
        TransactionStatus.REFUNDED,
        frozenset(
            {
                TransactionStatus.ORDER_CREATED,
                TransactionStatus.ORDER_APPROVED,
                TransactionStatus.CAPTURED,
                TransactionStatus.REFUNDING,
            }
        ),
    ),
}


def _identify(event: dict) -> tuple[Optional[str], Optional[str]]:
    """Extract (paypal_order_id, paypal_capture_id) from a webhook event.

    For CHECKOUT.ORDER.APPROVED the resource IS the order. For capture
    events the resource is the capture (its id is the capture id) and the
    order id sits in resource.supplementary_data.related_ids. For REFUNDED
    the resource is the *refund* — its id is a refund id, never stored as
    a capture id; the capture id comes from the refund's "up" link.
    """
    resource = event.get("resource")
    if not isinstance(resource, dict):
        return None, None
    event_type = event.get("event_type")

    related = {}
    supplementary = resource.get("supplementary_data")
    if isinstance(supplementary, dict) and isinstance(
        supplementary.get("related_ids"), dict
    ):
        related = supplementary["related_ids"]
    order_id = related.get("order_id")

    if event_type == "CHECKOUT.ORDER.APPROVED":
        return resource.get("id") or order_id, None

    if event_type == "PAYMENT.CAPTURE.REFUNDED":
        capture_id = related.get("capture_id")
        if not capture_id:
            # Fall back to the refund's "up" link: .../v2/payments/captures/{id}
            for link in resource.get("links") or []:
                href = link.get("href", "") if isinstance(link, dict) else ""
                if link.get("rel") == "up" and "/v2/payments/captures/" in href:
                    capture_id = href.rstrip("/").rsplit("/", 1)[-1]
                    break
        return order_id, capture_id

    # PAYMENT.CAPTURE.COMPLETED / PAYMENT.CAPTURE.DENIED: resource is the capture.
    return order_id, resource.get("id")


def _find_transaction(
    order_id: Optional[str], capture_id: Optional[str]
) -> Optional[TransactionRecord]:
    if order_id:
        try:
            return get_transaction_by_order(order_id)
        except TransactionNotFoundError:
            pass
    if capture_id:
        try:
            return get_transaction_by_capture(capture_id)
        except TransactionNotFoundError:
            pass
    return None


def reconcile_event(event: dict) -> dict:
    """Reconcile one VERIFIED webhook event with the transaction ledger.

    Never raises for an event that simply doesn't apply — unknown event
    types, unmatched orders, and stale deliveries are reported in the
    result so the endpoint can still acknowledge the webhook (a non-2xx
    would only make PayPal redeliver an event we cannot use).

    Returns a dict with "action" (one of: updated, already_applied,
    out_of_order_ignored, ignored_event_type, unidentifiable,
    transaction_not_found) plus identifying/status fields when available.
    """
    event_type = event.get("event_type")

    transition = _TRANSITIONS.get(event_type or "")
    if transition is None:
        # Verified but not an event this ledger tracks — acknowledge only.
        return {"action": "ignored_event_type", "event_type": event_type}
    target, allowed_from = transition

    order_id, capture_id = _identify(event)
    if not order_id and not capture_id:
        logger.info(
            "Webhook event carried no usable order/capture id (event_type=%s)",
            event_type,
        )
        return {"action": "unidentifiable", "event_type": event_type}

    txn = _find_transaction(order_id, capture_id)
    if txn is None:
        # Not an error: e.g. an order created outside this app's ledger.
        logger.info(
            "No local transaction for webhook (event_type=%s order=%s)",
            event_type,
            order_id,
        )
        return {
            "action": "transaction_not_found",
            "event_type": event_type,
            "paypal_order_id": order_id,
        }

    base = {
        "event_type": event_type,
        "transaction_id": txn.transaction_id,
        "paypal_order_id": txn.paypal_order_id,
    }

    if txn.status == target:
        # At-least-once delivery: a repeat of the current state is a no-op.
        return {**base, "action": "already_applied", "status": txn.status.value}

    if txn.status not in allowed_from:
        # Stale or conflicting delivery — never regress the record.
        logger.info(
            "Ignoring out-of-order webhook: %s cannot move %s from %s",
            event_type,
            txn.transaction_id,
            txn.status.value,
        )
        return {
            **base,
            "action": "out_of_order_ignored",
            "status": txn.status.value,
        }

    updated = update_status(txn.transaction_id, target, capture_id=capture_id)
    logger.info(
        "Reconciled %s: %s %s -> %s",
        event_type,
        txn.transaction_id,
        txn.status.value,
        updated.status.value,
    )
    return {
        **base,
        "action": "updated",
        "old_status": txn.status.value,
        "status": updated.status.value,
    }
