"""Server-side ownership resolution for PayPal order and capture ids (11F-B0).

    A client-supplied PayPal identifier is NOT an authorization.

Every money-moving route that takes a PayPal identifier from the URL --
capture an order, refund a capture -- used to hand that identifier straight
to PayPal. PayPal then answered for whatever order or capture that id names,
including one this installation never created, as long as the id happened to
be capturable or refundable under our own merchant credentials.

This module is the gate those routes now pass through FIRST. It answers one
deterministic question against the server's own storage:

    which of this server's ledgers, if any, owns this identifier?

    CATALOG   a row in the `transactions` ledger (local-catalog purchases,
              created by purchase_execution_service). The legitimate owner
              of the existing capture and refund flows.
    EXTERNAL  a row in `external_purchase_orders` (Phase 11E discovery
              purchases). Those orders are created and left awaiting the
              buyer's approval; this phase does NOT implement external
              capture, so they are refused here rather than captured
              through a path typed to the catalog.
    UNKNOWN   neither. Refused: with nothing server-side to point at, there
              is no evidence this installation is entitled to move money for
              that id.

WHAT THE GATE IS, AND WHAT IT IS NOT
    It is an ownership check: does our own state show this identifier was
    created by us, and by which flow. It is NOT authentication -- this
    service has no user, no session and no caller identity to check against,
    because none exists in this system yet. Any client that can reach the
    API can still act on an id this server created. What it can no longer do
    is make this server act on an arbitrary id the client supplied itself.

    It is also not a state machine. Whether an owned order is approved,
    already captured or already refunded stays PayPal's answer (for capture)
    and refund_service's deterministic rules (for refunds). Ownership only
    decides whether the request is OURS to forward at all.

THE IDENTIFIER SENT UPSTREAM IS THE STORED ONE
    require_catalog_order / require_catalog_capture return the ledger
    RECORD, and callers forward record.paypal_order_id /
    record.paypal_capture_id to PayPal -- never the raw path string. So a
    lookup that matched after whitespace normalization cannot forward an
    un-normalized variant, and the value that reaches PayPal is always one
    this server stored itself.

NO PAYPAL CALL HAPPENS HERE
    This module imports no PayPal client and makes no network call. Its
    refusals are therefore provably reached before any upstream contact:
    there is no code path from a refusal to a capture or a refund.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Optional, Union

from ..schemas.external_purchase import ExternalPurchaseOrder
from ..schemas.transaction import TransactionRecord
from . import external_purchase_service
from .transaction_service import (
    TransactionNotFoundError,
    get_transaction_by_capture,
    get_transaction_by_order,
)

logger = logging.getLogger("paypilot.ownership")


class OrderOwner(str, Enum):
    """Which ledger owns a PayPal identifier. Deterministic, never AI-set."""

    CATALOG = "CATALOG"
    EXTERNAL = "EXTERNAL"
    UNKNOWN = "UNKNOWN"


#: An external PayPal order exists, but capture for external purchases is
#: not implemented in this phase and the catalog capture path cannot stand
#: in for it: that path writes the `transactions` ledger, whose schema is
#: typed to the local catalog (a NOT NULL catalog product id and an INTEGER
#: catalog_price_inr). Capturing an external order there would either
#: fabricate catalog fields or silently skip the ledger write.
EXTERNAL_ORDER_NOT_CAPTURABLE_HERE = "EXTERNAL_ORDER_NOT_CAPTURABLE_HERE"

#: An external capture exists for this id (Phase 11F-D made external
#: captures possible), but refunding it here is not. The catalog refund path
#: is keyed on a `transactions` row and performs a CAPTURED -> REFUNDED
#: transition that does not exist for an external execution; external
#: refunds are not implemented in any phase so far. Refused explicitly
#: rather than as "unknown", so the answer stays truthful now that the id IS
#: something this server knows about.
EXTERNAL_CAPTURE_NOT_REFUNDABLE_HERE = "EXTERNAL_CAPTURE_NOT_REFUNDABLE_HERE"

#: No ledger of this server knows the identifier. Fail closed.
UNKNOWN_ORDER = "UNKNOWN_ORDER"
UNKNOWN_CAPTURE = "UNKNOWN_CAPTURE"


class OwnershipRefusedError(Exception):
    """A money-moving request was refused before PayPal was contacted.

    Carries a machine-readable `code` so a client can tell WHY without
    parsing prose, the `owner` the identifier resolved to, and the
    identifier itself (echoed back exactly as supplied, so a caller can
    correlate; it is never used as an authority for anything).

    Raising this means, in every case: no PayPal call was made, no money
    moved, no ledger row was created, modified or repaired.
    """

    def __init__(
        self,
        code: str,
        message: str,
        owner: OrderOwner,
        identifier: str,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.owner = owner
        self.identifier = identifier

    def as_detail(self) -> dict:
        """Structured HTTPException detail — same shape as the 11E conflicts."""
        return {
            "code": self.code,
            "message": self.message,
            "owner": self.owner.value,
            "identifier": self.identifier,
        }


def _normalize(identifier: object, field: str) -> str:
    """Reject anything that is not a usable identifier string.

    A non-string (a caller passing a list or an object where a path segment
    is expected), an empty string or whitespace raises ValueError, which the
    endpoints already map to 422 — the same refusal the PayPal client itself
    makes for a blank id, just made earlier and without a network call.
    """
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return identifier.strip()


def resolve_order_owner(
    order_id: str,
) -> tuple[OrderOwner, Optional[Union[TransactionRecord, ExternalPurchaseOrder]]]:
    """Resolve a PayPal order id against this server's own ledgers.

    Read-only, and the catalog ledger is consulted first so a legitimate
    catalog capture never depends on the external table being readable.

    Returns:
        (owner, record). record is the TransactionRecord for CATALOG, the
        ExternalPurchaseOrder for EXTERNAL, and None for UNKNOWN.

    Raises:
        ValueError: blank/non-string order id (no lookup performed).
    """
    identifier = _normalize(order_id, "order_id")

    try:
        return OrderOwner.CATALOG, get_transaction_by_order(identifier)
    except TransactionNotFoundError:
        pass

    external = external_purchase_service.get_by_paypal_order(identifier)
    if external is not None:
        return OrderOwner.EXTERNAL, external

    return OrderOwner.UNKNOWN, None


def require_catalog_order(order_id: str) -> TransactionRecord:
    """Return the catalog transaction owning `order_id`, or refuse.

    This is the gate every catalog capture route passes through before
    PayPal is contacted. The returned record's `paypal_order_id` is the
    value callers must forward upstream.

    Raises:
        ValueError: blank/non-string order id (no lookup, no PayPal call).
        OwnershipRefusedError:
            EXTERNAL_ORDER_NOT_CAPTURABLE_HERE - the id belongs to
                external_purchase_orders. External capture is not
                implemented, and this catalog path will not stand in for
                it, nor write a catalog ledger row for it.
            UNKNOWN_ORDER - no ledger of this server knows the id.
    """
    owner, record = resolve_order_owner(order_id)

    if owner is OrderOwner.CATALOG and isinstance(record, TransactionRecord):
        return record

    if owner is OrderOwner.EXTERNAL and isinstance(record, ExternalPurchaseOrder):
        logger.warning(
            "Capture refused (%s): PayPal order belongs to external "
            "execution %s; no PayPal call was made and no catalog "
            "transaction was created",
            EXTERNAL_ORDER_NOT_CAPTURABLE_HERE,
            record.execution_id,
        )
        raise OwnershipRefusedError(
            code=EXTERNAL_ORDER_NOT_CAPTURABLE_HERE,
            message=(
                f"PayPal order {record.paypal_order_id} belongs to an "
                f"external purchase execution, not to a local-catalog "
                f"transaction. External capture is not implemented, and the "
                f"catalog capture path is not used as a substitute for it: "
                f"no PayPal capture was attempted and no catalog "
                f"transaction was created."
            ),
            owner=owner,
            identifier=order_id,
        )

    logger.warning(
        "Capture refused (%s): no local ledger owns the requested PayPal "
        "order id; no PayPal call was made",
        UNKNOWN_ORDER,
    )
    raise OwnershipRefusedError(
        code=UNKNOWN_ORDER,
        message=(
            "No transaction of this server corresponds to the requested "
            "PayPal order id, so there is no server-side record authorizing "
            "a capture for it. No PayPal capture was attempted."
        ),
        owner=OrderOwner.UNKNOWN,
        identifier=order_id,
    )


def require_catalog_capture(capture_id: str) -> TransactionRecord:
    """Return the catalog transaction owning `capture_id`, or refuse.

    The gate for the capture-id-addressed refund route. A capture id the
    server never stored is refused outright: a client-supplied capture id is
    not authorization to refund, and resolving it here is what lets the
    existing deterministic refund rules (refund_service) apply at all —
    those rules are keyed on a transaction, not on a bare capture id.

    An external capture cannot be laundered through here. Until Phase
    11F-D no external capture id existed at all; now that external captures
    are possible, such an id is recognised and refused EXPLICITLY, with
    EXTERNAL_CAPTURE_NOT_REFUNDABLE_HERE, rather than being reported as
    unknown. The reason is unchanged: this path performs a CAPTURED ->
    REFUNDED transition on a `transactions` row, which does not exist for an
    external execution, and external refunds are not implemented anywhere.
    An external refund path, when one exists, must be its own.

    Raises:
        ValueError: blank/non-string capture id (no lookup, no PayPal call).
        OwnershipRefusedError:
            EXTERNAL_CAPTURE_NOT_REFUNDABLE_HERE - an external execution
                holds this capture id.
            UNKNOWN_CAPTURE - no ledger of this server stores it.
    """
    identifier = _normalize(capture_id, "capture_id")

    try:
        return get_transaction_by_capture(identifier)
    except TransactionNotFoundError:
        pass

    external = external_purchase_service.get_by_paypal_capture(identifier)
    if external is not None:
        logger.warning(
            "Refund refused (%s): capture belongs to external execution %s; "
            "no PayPal call was made",
            EXTERNAL_CAPTURE_NOT_REFUNDABLE_HERE,
            external.execution_id,
        )
        raise OwnershipRefusedError(
            code=EXTERNAL_CAPTURE_NOT_REFUNDABLE_HERE,
            message=(
                f"PayPal capture {external.paypal_capture_id} belongs to an "
                f"external purchase execution, not to a local-catalog "
                f"transaction. External refunds are not implemented, and the "
                f"catalog refund path is not used as a substitute for one: "
                f"no PayPal refund was attempted and no catalog transaction "
                f"was created or modified."
            ),
            owner=OrderOwner.EXTERNAL,
            identifier=capture_id,
        ) from None

    logger.warning(
        "Refund refused (%s): no local transaction stores the requested "
        "capture id; no PayPal call was made",
        UNKNOWN_CAPTURE,
    )
    raise OwnershipRefusedError(
        code=UNKNOWN_CAPTURE,
        message=(
            "No transaction of this server stores the requested PayPal "
            "capture id, so there is no server-side record authorizing a "
            "refund for it. A client-supplied capture id is not "
            "authorization. No PayPal refund was attempted."
        ),
        owner=OrderOwner.UNKNOWN,
        identifier=capture_id,
    ) from None
