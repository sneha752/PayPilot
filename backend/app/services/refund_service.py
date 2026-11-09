"""Transaction-aware refund service (Phase 8C — deterministic, FULL refunds).

Connects the local transaction ledger to the existing, tested PayPal refund
operation (paypal.refund_capture). This service decides WHICH capture to
refund — always the server-stored transaction.paypal_capture_id, never
anything client-supplied — while PayPal remains the authority on whether
the refund actually succeeds. The ledger moves to REFUNDED only after
PayPal confirms a successful refund.

Deterministic eligibility (no new policy system, no AI):
    CAPTURED + stored capture id  -> refundable
    ORDER_CREATED / ORDER_APPROVED / FAILED -> not refundable
    REFUNDED -> already refunded; PayPal is NOT called again.

Full refunds only: refund_capture posts an empty body, which PayPal treats
as a full refund of the capture, so no amount/currency ever enters here.
No Gemini call. No Policy Engine call. No secrets logged or returned.
"""

from __future__ import annotations

import logging

from ..paypal import (
    PayPalAlreadyRefundedError,
    PayPalAuthError,
    PayPalCaptureNotFoundError,
    PayPalRefundError,
    PayPalRefundNetworkError,
    refund_capture,
)
from ..schemas.transaction import TransactionRecord, TransactionStatus
from .transaction_service import (
    TransactionNotFoundError,
    claim_for_refund,
    get_transaction,
    release_refund_claim,
    update_status,
)

logger = logging.getLogger("paypilot.refunds")


class RefundNotAllowedError(Exception):
    """The transaction's state does not permit a refund (not CAPTURED)."""

    def __init__(self, transaction_id: str, status: TransactionStatus):
        self.status = status
        super().__init__(
            f"Transaction {transaction_id} cannot be refunded: "
            f"status is {status.value}, only CAPTURED transactions are refundable"
        )


class AlreadyRefundedError(Exception):
    """The transaction is already REFUNDED — no second PayPal refund is made."""

    def __init__(self, transaction_id: str):
        super().__init__(f"Transaction {transaction_id} has already been refunded")


class MissingCaptureIdError(Exception):
    """CAPTURED transaction with no stored capture id — inconsistent record.

    PayPal is NOT called in this case: without the server-stored capture id
    there is no authoritative identifier to refund.
    """

    def __init__(self, transaction_id: str):
        super().__init__(
            f"Transaction {transaction_id} is CAPTURED but has no stored "
            f"PayPal capture id; refund cannot proceed"
        )


class RefundInProgressError(Exception):
    """A concurrent refund has already CLAIMED this transaction (Phase
    11F-K.13): its status is REFUNDING, so another request is either mid
    PayPal-call or the row is awaiting reconciliation after an ambiguous
    outcome. The second request must NOT start its own refund; it fails closed
    with a conflict and makes no PayPal call.
    """

    def __init__(self, transaction_id: str):
        super().__init__(
            f"Transaction {transaction_id} already has a refund in progress"
        )


class RefundLedgerDesyncError(Exception):
    """PayPal refunded the capture, but the local REFUNDING -> REFUNDED write
    then failed. NOT a rollback: the money WAS returned and nothing here
    reverses it. Surfaced (never a false success) so an operator can
    reconcile; the stuck REFUNDING row can also be reconciled by a verified
    PAYMENT.CAPTURE.REFUNDED webhook.
    """

    code = "REFUND_LEDGER_DESYNC"

    def __init__(self, refund: dict, transaction_id: str):
        self.refund = dict(refund)
        self.transaction_id = transaction_id
        super().__init__(
            f"Transaction {transaction_id} WAS REFUNDED at PayPal (refund "
            f"{refund.get('refund_id')}), but the ledger could not be updated "
            f"to REFUNDED; the row is left REFUNDING for reconciliation."
        )

    def as_detail(self) -> dict:
        return {
            "code": self.code,
            "message": str(self),
            "paypal_refunded": True,
            "rolled_back": False,
            "transaction_id": self.transaction_id,
            "refund_id": self.refund.get("refund_id"),
            "refund_status": self.refund.get("refund_status"),
        }


def refund_transaction(transaction_id: str) -> dict:
    """Issue a FULL refund for one CAPTURED transaction.

    Flow: load the transaction -> check deterministic eligibility -> ATOMIC
    claim CAPTURED -> REFUNDING (Phase 11F-K.13) so only one concurrent caller
    proceeds -> refund the SERVER-STORED capture id via the existing PayPal
    operation (no DB transaction held across the call) -> only on
    PayPal-confirmed success mark the ledger REFUNDED.

    Concurrency & failure semantics:
      - AT MOST ONE concurrent request wins the claim; the loser raises
        RefundInProgressError and never calls PayPal. This is at-most-one
        concurrent refund INITIATION, not exactly-once external execution.
      - Definitive failure, no funds moved (auth error, capture not found,
        PayPal error response): the claim is released back to CAPTURED so an
        explicit retry is safe.
      - Ambiguous failure (transport error / timeout): the row is LEFT
        REFUNDING — never auto-retried — for an operator or a verified
        PAYMENT.CAPTURE.REFUNDED webhook to reconcile.
      - PayPal success but the final ledger write fails: RefundLedgerDesyncError
        is raised (money moved, row left REFUNDING) — never a false success.

    Args:
        transaction_id: The LOCAL transaction id (application-facing).

    Returns:
        A safe summary built from PayPal's actual refund response:
        {"status": "refunded", "transaction_id", "paypal_order_id",
         "paypal_capture_id", "refund_id", "refund_status", "amount",
         "currency"}.

    Raises:
        TransactionNotFoundError: no such transaction.
        AlreadyRefundedError: already REFUNDED (PayPal not called).
        RefundInProgressError: a concurrent claim holds it REFUNDING (no call).
        RefundNotAllowedError: status is not CAPTURED (PayPal not called).
        MissingCaptureIdError: CAPTURED without a capture id (PayPal not called).
        PayPalAuthError / PayPalCaptureNotFoundError / PayPalRefundError:
            definitive failure — the claim is released back to CAPTURED.
        PayPalRefundNetworkError: ambiguous — the row is left REFUNDING.
        PayPalAlreadyRefundedError: PayPal already fully refunded — the ledger
            is set REFUNDED and the conflict is re-raised.
        RefundLedgerDesyncError: refund succeeded but the ledger write failed.
    """
    txn: TransactionRecord = get_transaction(transaction_id)

    # Deterministic eligibility, checked BEFORE the atomic claim so each
    # non-refundable state keeps its own specific error.
    if txn.status == TransactionStatus.REFUNDED:
        logger.info("Refund rejected (already refunded): %s", txn.transaction_id)
        raise AlreadyRefundedError(txn.transaction_id)

    if txn.status == TransactionStatus.REFUNDING:
        # A concurrent refund already claimed it, or it is stuck awaiting
        # reconciliation after an ambiguous outcome — never start a second one.
        logger.info("Refund rejected (already in progress): %s", txn.transaction_id)
        raise RefundInProgressError(txn.transaction_id)

    if txn.status != TransactionStatus.CAPTURED:
        logger.info(
            "Refund rejected (not refundable): %s status=%s",
            txn.transaction_id,
            txn.status.value,
        )
        raise RefundNotAllowedError(txn.transaction_id, txn.status)

    if not txn.paypal_capture_id or not txn.paypal_capture_id.strip():
        logger.error(
            "Refund blocked: transaction %s is CAPTURED without a capture id",
            txn.transaction_id,
        )
        raise MissingCaptureIdError(txn.transaction_id)

    # ATOMIC CLAIM: CAPTURED -> REFUNDING. Exactly one concurrent caller wins;
    # the loser never reaches PayPal. The claim is a short, committed write —
    # no database transaction is held open across the PayPal call below.
    if not claim_for_refund(txn.transaction_id):
        # Lost the race between the read above and the claim. Re-read to return
        # the precise conflict rather than guessing.
        current = get_transaction(txn.transaction_id)
        if current.status == TransactionStatus.REFUNDED:
            raise AlreadyRefundedError(txn.transaction_id)
        if current.status == TransactionStatus.REFUNDING:
            raise RefundInProgressError(txn.transaction_id)
        raise RefundNotAllowedError(txn.transaction_id, current.status)

    capture_id = txn.paypal_capture_id  # server-stored, validated above

    # The ONLY refund identifier ever used is the server-stored capture id.
    # This network call is made with NO database transaction held open.
    logger.info("Refunding transaction %s via stored capture id", txn.transaction_id)
    try:
        refund = refund_capture(capture_id)
    except PayPalRefundNetworkError:
        # AMBIGUOUS outcome: the refund may have been applied. DO NOT release
        # the claim (that would permit an unsafe second refund) and do NOT mark
        # REFUNDED (we have no confirmation). Leave the row REFUNDING for an
        # operator or a verified PAYMENT.CAPTURE.REFUNDED webhook to reconcile,
        # and surface the failure. A retry meanwhile hits REFUNDING -> 409.
        logger.error(
            "Ambiguous refund outcome for %s; left REFUNDING for reconciliation",
            txn.transaction_id,
        )
        raise
    except PayPalAlreadyRefundedError:
        # PayPal reports the capture is already fully refunded: the funds are
        # returned, so REFUNDING -> REFUNDED is the truthful terminal state.
        # The conflict is still surfaced to the caller.
        update_status(txn.transaction_id, TransactionStatus.REFUNDED)
        logger.info(
            "Capture already fully refunded at PayPal; ledger set REFUNDED: %s",
            txn.transaction_id,
        )
        raise
    except (PayPalCaptureNotFoundError, PayPalAuthError, PayPalRefundError):
        # DEFINITIVE failure with NO funds moved (auth failed before the call,
        # the capture does not exist, or PayPal returned an error response):
        # release the claim back to CAPTURED so an explicit retry is safe.
        release_refund_claim(txn.transaction_id)
        logger.info(
            "Definitive refund failure; claim released to CAPTURED: %s",
            txn.transaction_id,
        )
        raise

    # PayPal confirmed the refund — only now does the ledger move to REFUNDED.
    try:
        updated = update_status(txn.transaction_id, TransactionStatus.REFUNDED)
    except TransactionNotFoundError as exc:
        # The row vanished between the claim and the final write: the refund
        # succeeded at PayPal but cannot be recorded. Never a false success.
        desync = RefundLedgerDesyncError(refund, txn.transaction_id)
        logger.error("%s", desync)
        raise desync from exc

    logger.info(
        "Transaction refunded: %s refund_id=%s status %s -> %s",
        txn.transaction_id,
        refund.get("refund_id"),
        txn.status.value,
        updated.status.value,
    )

    return {
        "status": "refunded",
        "transaction_id": updated.transaction_id,
        "paypal_order_id": updated.paypal_order_id,
        "paypal_capture_id": updated.paypal_capture_id,
        "refund_id": refund.get("refund_id"),
        "refund_status": refund.get("refund_status"),
        "amount": refund.get("refunded_amount"),
        "currency": refund.get("currency"),
    }
