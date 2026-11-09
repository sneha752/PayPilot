"""Policy-gated purchase execution: the ONLY place PayPal depends on policy.

    Gemini recommends.  Policy Engine authorizes.  PayPal executes.

This service is the execution gate between the deterministic Policy Engine
and the existing PayPal order-creation service:

    catalog product (authoritative price)
        -> evaluate_policy (existing engine, single source of truth)
        -> DENY                     : structured denial,   NO PayPal call
        -> REQUIRE_USER_APPROVAL    : structured escalation, NO PayPal call
        -> ALLOW_AUTONOMOUS_PURCHASE: INR -> USD via currency_service,
                                      then paypal.create_order(...)

Trust boundaries:
  - The price fed to policy and to PayPal is ALWAYS product.price_inr from
    the local catalog, loaded here by product_id. No client- or AI-supplied
    number is accepted anywhere in this path.
  - No policy rule is duplicated or re-implemented here; the gate only
    branches on the engine's decision.
  - CAPTURE IS OWNERSHIP-GATED (Phase 11F-B0). capture_purchase resolves the
    requested PayPal order id against this server's own ledgers BEFORE
    contacting PayPal, via order_ownership_service. Only an id owned by a
    `transactions` row is captured, and the id sent upstream is the stored
    one. An id owned by Phase 11E's external_purchase_orders, or by no
    ledger at all, is refused with no PayPal call and no ledger write — so
    this path can neither capture an external order nor write a catalog
    ledger row for one.
  - Fully deterministic: no Gemini/provider import, no AI call.
"""

from __future__ import annotations

import logging
import sqlite3
from decimal import Decimal, InvalidOperation

from ..paypal import (
    PayPalAuthError,
    PayPalCheckoutConfigError,
    PayPalGetOrderError,
    PayPalOrderError,
    PayPalOrderNetworkError,
    PayPalOrderNotFoundError,
    capture_order,
    classify_recovery_window,
    create_order,
    get_order,
    validate_paypal_request_id,
)
from ..policy import evaluate_policy
from ..schemas.catalog_attempt import CatalogAttemptStatus, CatalogPurchaseAttempt
from ..schemas.execution import CaptureResult, ExecutionResult
from ..schemas.intent import Authorization, HardConstraints
from ..schemas.policy import PolicyOutcome, PolicySelectedProduct
from ..schemas.transaction import TransactionRecord
from . import catalog_attempt_service
from .currency_service import DEMO_INR_PER_USD, convert_inr_to_usd
from .order_ownership_service import require_catalog_order
from .product_service import ProductNotFoundError, get_product
from .transaction_service import (
    TransactionNotFoundError,
    create_transaction,
    get_transaction_by_order,
    mark_captured,
)

logger = logging.getLogger("paypilot.execution")

# The local catalog is priced in INR. Kept as a module constant so the policy
# input always states its currency explicitly (and tests can simulate a
# non-INR catalog to prove the engine's currency rule blocks execution).
CATALOG_CURRENCY = "INR"


def _evaluate(product, authorization, hard_constraints):
    """Re-run the Policy Engine on the authoritative catalog price."""
    return evaluate_policy(
        product=PolicySelectedProduct(
            product_id=product.id,
            price_inr=product.price_inr,
            currency=CATALOG_CURRENCY,
        ),
        authorization=authorization,
        hard_constraints=hard_constraints,
    )


def _base_fields(product, decision) -> dict:
    return {
        "product_id": product.id,
        "product_name": product.name,
        "catalog_price_inr": product.price_inr,
        "policy_decision": decision,
    }


class CatalogAttemptFinalizeConflict(Exception):
    """A catalog attempt cannot be finalized against the resolved transaction.

    Phase 11F-K.20. Raised (fail closed) when the deterministic order-id
    association would link an attempt to a transaction that does not match the
    attempt's own recorded product / amount / currency, when the attempt is
    already FINALIZED to a DIFFERENT transaction, or when a FAILED attempt is
    asked to finalize. No ledger row is created, modified, or captured; nothing
    is guessed.
    """

    code = "CATALOG_ATTEMPT_FINALIZE_CONFLICT"


def _amounts_equal(a: str, b: str) -> bool:
    try:
        da, db = Decimal(a), Decimal(b)
    except (InvalidOperation, ValueError, ArithmeticError):
        return False
    return da.is_finite() and db.is_finite() and da == db


def _verify_transaction_matches_attempt(
    txn: TransactionRecord, attempt: CatalogPurchaseAttempt
) -> None:
    """Fail closed unless an existing transaction matches the attempt's intent.

    The association is deterministic: both carry the same UNIQUE PayPal order
    id. This additionally requires the product, currency and amount to agree,
    so a transaction that happens to share an order id but represents something
    else is never silently linked.
    """
    if (
        txn.product_id == attempt.product_id
        and (txn.paypal_currency or "").upper() == (attempt.paypal_currency or "").upper()
        and _amounts_equal(txn.paypal_amount, attempt.paypal_amount)
    ):
        return
    raise CatalogAttemptFinalizeConflict(
        f"Transaction {txn.transaction_id} for PayPal order "
        f"{txn.paypal_order_id} does not match attempt {attempt.attempt_id} "
        f"(product/amount/currency); refusing to link."
    )


def finalize_attempt(attempt_id: str) -> TransactionRecord:
    """Idempotently resolve a CREATING attempt (with an order id) to a ledger row.

    Phase 11F-K.20. The attempt must already carry a persisted PayPal order id
    (claim_order_id). This function then, in order:
      1. LOCATES the transaction by that UNIQUE order id, or creates it if none
         exists (so a re-run after a crashed finalization links the existing
         row instead of inserting a duplicate — the UNIQUE(paypal_order_id)
         constraint and a re-locate on IntegrityError make the create safe);
      2. VERIFIES the transaction matches the attempt (fail closed otherwise);
      3. marks the attempt FINALIZED and links the transaction id, guarded so
         it is applied exactly once.

    It is safe to repeat: an already-FINALIZED attempt returns its linked
    transaction (verifying the link is consistent), and a located transaction
    is reused rather than duplicated. It calls no PayPal endpoint and captures
    nothing.

    Raises:
        CatalogAttemptPersistenceError-adjacent conditions are NOT raised here;
        instead:
        ValueError: the attempt does not exist, or carries no order id (an
            unresolved reservation is never finalized or guessed).
        CatalogAttemptFinalizeConflict: a product/amount/currency mismatch, a
            FAILED attempt, or an already-FINALIZED link to a different txn.
    """
    attempt = catalog_attempt_service.get(attempt_id)
    if attempt is None:
        raise ValueError(f"No catalog purchase attempt with id {attempt_id}")
    if attempt.status is CatalogAttemptStatus.FAILED:
        raise CatalogAttemptFinalizeConflict(
            f"Attempt {attempt_id} is FAILED (definitive no-order) and cannot "
            f"be finalized."
        )
    order_id = (attempt.paypal_order_id or "").strip()
    if not order_id:
        # An unresolved reservation has no order to finalize against. The id is
        # never guessed; recovery of such a row belongs to a later phase.
        raise ValueError(
            f"Attempt {attempt_id} has no persisted PayPal order id, so it "
            f"cannot be finalized; it remains unresolved."
        )

    # 1. Locate the transaction by its UNIQUE order id, or create it.
    try:
        txn = get_transaction_by_order(order_id)
    except TransactionNotFoundError:
        txn = None
    if txn is None:
        product = get_product(attempt.product_id)  # for the ledger's product_name
        try:
            txn = create_transaction(
                product_id=attempt.product_id,
                product_name=product.name,
                catalog_price_inr=attempt.catalog_price_inr,
                paypal_amount=attempt.paypal_amount,
                paypal_currency=attempt.paypal_currency,
                paypal_order_id=order_id,
            )
        except sqlite3.IntegrityError:
            # A concurrent/previous insert won the UNIQUE order id: re-locate it
            # and link that one rather than inserting a duplicate.
            txn = get_transaction_by_order(order_id)

    # 2. The transaction must match the attempt's recorded intent.
    _verify_transaction_matches_attempt(txn, attempt)

    # 3. Finalize + link. Idempotent: an already-FINALIZED attempt must already
    #    point at this same transaction, or it is a conflict.
    if attempt.status is CatalogAttemptStatus.FINALIZED:
        if (attempt.transaction_id or "") != txn.transaction_id:
            raise CatalogAttemptFinalizeConflict(
                f"Attempt {attempt_id} is already FINALIZED to transaction "
                f"{attempt.transaction_id}, not {txn.transaction_id}."
            )
        return txn
    catalog_attempt_service.mark_finalized(attempt_id, txn.transaction_id)
    return txn


# ===========================================================================
# Phase 11F-K.21: read-first recovery of a STUCK CREATING catalog attempt.
#
# An operator endpoint and a webhook fallback resolve a CREATING attempt that
# carries a persisted PayPal order id (K.20) but never finalized. Recovery is
# READ-FIRST: it re-reads the order with the existing read-only get_order and
# settles the attempt to FINALIZED only on conclusive, independently verified
# evidence, reusing the idempotent finalize_attempt linking. It NEVER creates,
# approves, authorizes, captures, or refunds a PayPal order, and never advances
# the ledger beyond the normal ORDER_CREATED creation state (existing capture /
# webhook paths do that later). A CREATING attempt WITHOUT an order id is left
# unresolved (manual review); an id is never guessed.
# ===========================================================================


#: PayPal order statuses from which a CREATING attempt may be finalized: the
#: order EXISTS and is still PRE-CAPTURE. APPROVED is included (the buyer
#: approved, but the ledger row is created in its normal ORDER_CREATED state).
#: COMPLETED (captured), VOIDED, PAYER_ACTION_REQUIRED and anything else are
#: excluded and routed to review — finalizing a captured order to ORDER_CREATED
#: would understate it, and a captured orphan needs a human + the capture path.
_RECOVERABLE_ORDER_STATUSES = frozenset({"CREATED", "APPROVED"})


def reconcile_creating_attempt(
    attempt: CatalogPurchaseAttempt,
) -> tuple[str, CatalogPurchaseAttempt, dict]:
    """Reconcile a stuck CREATING catalog attempt from an INDEPENDENT read.

    Returns (outcome, refreshed_attempt, detail). Never calls create_order,
    capture, authorize or refund. Settles ONLY CREATING -> FINALIZED, and only
    when PayPal's own order read proves the server's recorded order exists, is
    still pre-capture, answers about the stored order id, and carries exactly
    the amount and currency the attempt durably recorded.

    outcomes:
      already_finalized      attempt is FINALIZED; PayPal is NOT contacted.
      not_applicable         attempt is FAILED (definitive no-order); terminal.
      manual_review_required CREATING with NO persisted order id: the
                             create-order outcome is ambiguous (e.g. a
                             timeout). The id is never guessed, create_order is
                             never retried.
      recovered_finalized    settled CREATING -> FINALIZED on verified evidence.
      inconclusive           PayPal unreadable (retryable); state preserved.
      review_required        a definitive-looking discrepancy (wrong id, a
                             non-pre-capture status such as COMPLETED, an
                             amount/currency mismatch, a finalize conflict, a
                             removed product with no existing transaction, or
                             the attempt moved). State preserved for a human.
    """
    if attempt.status is CatalogAttemptStatus.FINALIZED:
        return "already_finalized", attempt, {"status": attempt.status.value}
    if attempt.status is not CatalogAttemptStatus.CREATING:  # FAILED
        return "not_applicable", attempt, {"status": attempt.status.value}

    order_id = (attempt.paypal_order_id or "").strip()
    if not order_id:
        return (
            "manual_review_required",
            attempt,
            {"reason": "NO_PERSISTED_ORDER_ID", "retryable": False},
        )

    try:
        order = get_order(order_id)
    except (PayPalAuthError, PayPalOrderNotFoundError, PayPalGetOrderError):
        # Unknown is never treated as a confirmed order: keep CREATING, retry.
        return "inconclusive", attempt, {"reason": "ORDER_UNREADABLE", "retryable": True}

    returned_id = order.get("order_id")
    if not isinstance(returned_id, str) or returned_id.strip() != order_id:
        return (
            "review_required",
            attempt,
            {
                "reason": "ORDER_ID_MISMATCH",
                "retryable": False,
                "observed_order_id": returned_id if isinstance(returned_id, str) else None,
            },
        )

    status = order.get("order_status")
    status_text = status.strip() if isinstance(status, str) else ""
    if status_text.upper() not in _RECOVERABLE_ORDER_STATUSES:
        # COMPLETED (captured) / VOIDED / anything not pre-capture.
        return (
            "review_required",
            attempt,
            {
                "reason": "ORDER_NOT_RECOVERABLE_STATUS",
                "retryable": False,
                "observed_order_status": status_text or None,
            },
        )

    # Exactly one purchase unit, matching the attempt's recorded amount/currency.
    units = order.get("purchase_units")
    if not isinstance(units, list) or not units:
        return (
            "review_required",
            attempt,
            {"reason": "PURCHASE_UNIT_MISSING", "retryable": False},
        )
    if len(units) > 1:
        return (
            "review_required",
            attempt,
            {"reason": "PURCHASE_UNIT_AMBIGUOUS", "retryable": False},
        )
    unit = units[0] if isinstance(units[0], dict) else {}
    amount = unit.get("amount") if isinstance(unit.get("amount"), dict) else {}
    observed_value = amount.get("value")
    observed_currency = amount.get("currency_code")
    if not isinstance(observed_value, str) or not isinstance(observed_currency, str):
        return (
            "review_required",
            attempt,
            {"reason": "PURCHASE_UNIT_MISSING", "retryable": False},
        )
    if observed_currency.strip().upper() != (attempt.paypal_currency or "").upper():
        return (
            "review_required",
            attempt,
            {
                "reason": "CURRENCY_MISMATCH",
                "retryable": False,
                "observed_currency": observed_currency,
            },
        )
    if not _amounts_equal(observed_value.strip(), attempt.paypal_amount):
        return (
            "review_required",
            attempt,
            {
                "reason": "AMOUNT_MISMATCH",
                "retryable": False,
                "observed_amount": observed_value,
            },
        )

    # Conclusive: finalize via the idempotent K.20 linking. It locates the
    # transaction by the UNIQUE order id (or creates it), verifies it matches,
    # and guard-transitions the attempt to FINALIZED. Handle the safe failure
    # outcomes without ever fabricating a product or forcing a bad link.
    try:
        txn = finalize_attempt(attempt.attempt_id)
    except ProductNotFoundError:
        return (
            "review_required",
            catalog_attempt_service.get(attempt.attempt_id) or attempt,
            {"reason": "PRODUCT_UNAVAILABLE", "retryable": False},
        )
    except CatalogAttemptFinalizeConflict as exc:
        return (
            "review_required",
            catalog_attempt_service.get(attempt.attempt_id) or attempt,
            {"reason": CatalogAttemptFinalizeConflict.code, "retryable": False, "message": str(exc)},
        )
    except ValueError:
        # The attempt lost its order id or vanished between the read and the
        # finalize (an unexpected concurrent change). Do not force it.
        return (
            "review_required",
            catalog_attempt_service.get(attempt.attempt_id) or attempt,
            {"reason": "STATE_CHANGED", "retryable": True},
        )

    refreshed = catalog_attempt_service.get(attempt.attempt_id) or attempt
    if refreshed.status is CatalogAttemptStatus.FINALIZED:
        logger.info(
            "Recovered stuck CREATING catalog attempt %s to FINALIZED from an "
            "independent PayPal read (order status %s, txn %s). No order was "
            "created, nothing was captured, approved or refunded.",
            refreshed.attempt_id,
            status_text,
            txn.transaction_id,
        )
        return (
            "recovered_finalized",
            refreshed,
            {"observed_order_status": status_text, "transaction_id": txn.transaction_id},
        )
    # finalize returned a transaction but the attempt is not FINALIZED: it moved
    # unexpectedly. Preserve it for review rather than forcing a transition.
    return (
        "review_required",
        refreshed,
        {"reason": "STATE_CHANGED", "retryable": True, "status": refreshed.status.value},
    )


# ===========================================================================
# Phase 11F-K.22b: operator-gated create-order REPLAY recovery (catalog).
#
# The AMBIGUOUS case the read-first K.21 path cannot settle: a CREATING attempt
# with NO persisted order id, whose create-order outcome was never learned.
# K.22a persisted a stable PayPal-Request-Id, so an operator can REPLAY the
# original create_order with that EXACT id. PayPal's idempotency means a replay
# inside its retention window returns the SAME order if one was created, or
# creates it now if none was — at most one order ever exists. Permitted ONLY
# when the attempt is demonstrably inside a CONSERVATIVE window below that
# retention.
#
# NEVER automatic, never from a webhook or the ordinary confirmation flow, and
# never a second request id. The replay response is NOT trusted as proof the
# order is still CREATED: the returned id is persisted via the guarded K.20
# claim and then INDEPENDENTLY re-read and verified through the existing K.21
# read-first path before the attempt is finalized. A replay that fails leaves
# the attempt CREATING and is NEVER marked FAILED.
# ===========================================================================


def _catalog_replay_eligibility(
    attempt: CatalogPurchaseAttempt,
    *,
    now,
    window_seconds,
) -> tuple[bool, dict]:
    """Decide whether this ambiguous CREATING attempt admits a replay.

    Returns (eligible, detail). Every non-eligible detail fails closed and
    leaves the attempt UNCHANGED. Checks the persisted request id is present,
    valid, and the ORIGINAL value (derived from the attempt id — recovery never
    generates a new one), then the conservative recovery window.
    """
    rid = (attempt.paypal_request_id or "").strip()
    if not rid:
        return False, {"reason": "NO_PERSISTED_REQUEST_ID", "retryable": False}
    try:
        validated = validate_paypal_request_id(rid)
    except ValueError:
        return False, {"reason": "REQUEST_ID_INVALID", "retryable": False}
    if validated != attempt.attempt_id:
        # The stored id is not the one this attempt was created with. Recovery
        # replays ONLY the original id and never substitutes another.
        return False, {"reason": "REQUEST_ID_NOT_ORIGINAL", "retryable": False}
    eligible, reason, age = classify_recovery_window(
        attempt.created_at, now=now, window_seconds=window_seconds
    )
    if not eligible:
        return False, {"reason": reason, "retryable": False}
    return True, {"reason": "ELIGIBLE", "age_seconds": age}


def _catalog_replay_and_reconcile(
    attempt: CatalogPurchaseAttempt,
) -> tuple[str, CatalogPurchaseAttempt, dict]:
    """Replay create_order with the persisted id, then verify independently."""
    # A best-effort description; PayPal ignores it when it returns the order
    # already created for this request id. The amount and currency are the
    # ones durably recorded on the attempt at reservation time — exactly what
    # the original create sent and what the K.21 verification expects.
    try:
        product = get_product(attempt.product_id)
        description = f"{product.name} ({product.id})"
    except ProductNotFoundError:
        description = attempt.product_id

    try:
        order = create_order(
            amount=attempt.paypal_amount,
            currency=attempt.paypal_currency,
            description=description,
            # The EXACT persisted request id — never a new one.
            request_id=attempt.paypal_request_id,
        )
    except PayPalOrderNetworkError:
        # AMBIGUOUS transport outcome: an order may exist. Keep CREATING.
        return (
            "unresolved",
            catalog_attempt_service.get(attempt.attempt_id) or attempt,
            {"reason": "REPLAY_TRANSPORT_AMBIGUOUS", "retryable": True},
        )
    except PayPalOrderError:
        # Non-2xx / malformed / missing id. An HTTP error does not prove no
        # order exists. Keep CREATING; never FAILED.
        return (
            "unresolved",
            catalog_attempt_service.get(attempt.attempt_id) or attempt,
            {"reason": "REPLAY_UNRESOLVED", "retryable": True},
        )
    except (PayPalAuthError, PayPalCheckoutConfigError):
        # A precondition failing NOW says nothing about whether the ORIGINAL
        # call created an order; never conclude FAILED from a replay failure.
        return (
            "unresolved",
            catalog_attempt_service.get(attempt.attempt_id) or attempt,
            {"reason": "REPLAY_PRECONDITION_UNVERIFIED", "retryable": True},
        )

    order_id = (order.get("order_id") or "").strip()
    if not order_id:
        return (
            "unresolved",
            catalog_attempt_service.get(attempt.attempt_id) or attempt,
            {"reason": "REPLAY_NO_ORDER_ID", "retryable": True},
        )

    # Persist the returned id on the still-CREATING attempt via the GUARDED
    # K.20 claim. Never overwrite a different id; a conflict is review_required.
    claimed, record = catalog_attempt_service.claim_order_id(
        attempt.attempt_id, order_id
    )
    if not claimed:
        benign_reentry = (
            record is not None
            and record.status is CatalogAttemptStatus.CREATING
            and (record.paypal_order_id or "") == order_id
        )
        if not benign_reentry:
            return (
                "review_required",
                record or catalog_attempt_service.get(attempt.attempt_id) or attempt,
                {
                    "reason": "ORDER_ID_CONFLICT",
                    "retryable": False,
                    "observed_order_id": order_id,
                },
            )

    # The replay response is NOT proof the order is still CREATED. Re-read and
    # verify INDEPENDENTLY, then finalize, via the K.21 read-first path.
    refreshed = catalog_attempt_service.get(attempt.attempt_id) or attempt
    return reconcile_creating_attempt(refreshed)


def recover_creating_attempt(
    attempt: CatalogPurchaseAttempt,
    *,
    now=None,
    window_seconds=None,
) -> tuple[str, CatalogPurchaseAttempt, dict]:
    """Operator-gated recovery of a stuck CREATING catalog attempt (K.22b).

    Returns (outcome, refreshed_attempt, detail). Behaviour by case:

      - NOT CREATING, or CREATING WITH a persisted order id: no replay is
        needed or allowed; delegates to the read-first K.21 recovery
        (reconcile_creating_attempt), which contacts PayPal only to READ.
      - CREATING with NO order id and INELIGIBLE (no/invalid/non-original
        request id, malformed/ambiguous/future timestamp, or outside the
        conservative window): manual_review_required, attempt UNCHANGED, no
        provider call.
      - CREATING with NO order id, eligible, but a replay already claimed by
        another worker: replay_in_progress, no provider call.
      - CREATING with NO order id and eligible: replays create_order with the
        EXACT persisted request id under a durable single-flight claim, then
        independently verifies and finalizes via the K.21 read-first path.

    outcomes add replay_in_progress and unresolved to the K.21 set. It NEVER
    captures, authorizes, refunds, marks the attempt FAILED, overwrites an
    order id, or replaces the request id.
    """
    if attempt.status is not CatalogAttemptStatus.CREATING:
        return reconcile_creating_attempt(attempt)
    order_id = (attempt.paypal_order_id or "").strip()
    if order_id:
        # An order already exists; the read-first path is correct and safe.
        return reconcile_creating_attempt(attempt)

    eligible, detail = _catalog_replay_eligibility(
        attempt, now=now, window_seconds=window_seconds
    )
    if not eligible:
        return "manual_review_required", attempt, detail

    claimed, current, claim_token = catalog_attempt_service.claim_recovery(
        attempt.attempt_id, now=now
    )
    if not claimed:
        return (
            "replay_in_progress",
            current or attempt,
            {"reason": "REPLAY_IN_PROGRESS", "retryable": True},
        )

    try:
        return _catalog_replay_and_reconcile(current or attempt)
    finally:
        # Free the claim so a later in-window operator retry can re-claim.
        # Owner-guarded: only this worker's token matches; a stale-reclaim
        # by another worker wrote a new token, so this release is a no-op.
        catalog_attempt_service.release_recovery_claim(
            attempt.attempt_id, claim_token
        )


def _create_order_result(product, decision, approval_used: bool) -> ExecutionResult:
    """The ONE safe PayPal execution path, shared by both endpoints:
    authoritative catalog INR -> demo USD -> existing create_order().

    Phase 11F-K.19: a DURABLE reservation is committed BEFORE create_order, so
    a crash or a ledger-write failure after PayPal creates an order leaves an
    identifiable attempt row rather than nothing. This function is reached only
    AFTER the Policy Engine allowed (execute_purchase) or an explicit approval
    covered (approve_purchase) the purchase, so a reservation never represents
    a denied, unapproved, or invalid-product request.
    """
    usd_amount = convert_inr_to_usd(product.price_inr)

    # Reservation first, committed in its own short transaction (no write
    # transaction is held open across the PayPal network call that follows).
    attempt = catalog_attempt_service.reserve_attempt(
        product_id=product.id,
        catalog_price_inr=product.price_inr,
        paypal_amount=usd_amount,
        paypal_currency="USD",
    )

    try:
        order = create_order(
            amount=usd_amount,
            currency="USD",
            description=f"{product.name} ({product.id})",
            # Phase 11F-K.22a: the stable request id persisted on THIS attempt,
            # so the single create_order call is idempotent-tagged at the
            # provider. No retry is performed in this phase.
            request_id=attempt.paypal_request_id,
        )
    except (PayPalAuthError, PayPalCheckoutConfigError):
        # DEFINITIVE no-order: checkout-config validation and the auth-token
        # fetch both run BEFORE the create-order POST, so no PayPal order was
        # created. Mark the attempt FAILED (terminal) and re-raise unchanged.
        catalog_attempt_service.mark_failed(attempt.attempt_id)
        raise
    except PayPalOrderError:
        # AMBIGUOUS: PayPalOrderError conflates a transport error / timeout
        # (the request may have reached PayPal and created an order), a non-2xx
        # response, and a 2xx-without-id, with NO structured discriminator in
        # the current error contract. We refuse to claim "no order was
        # created": the attempt is LEFT CREATING (unresolved, not marked FAILED
        # and never auto-retried here) and the error is re-raised unchanged.
        raise

    order_id = (order.get("order_id") or "").strip()
    if not order_id:  # pragma: no cover - create_order raises on a missing id
        # 2xx without an id is ambiguous (an order may exist); leave CREATING.
        raise PayPalOrderError("PayPal response did not contain an order id")

    # Phase 11F-K.20: persist the PayPal order id on the still-CREATING attempt
    # in its OWN committed write, BEFORE the ledger row is created. A crash
    # after this point leaves a CREATING attempt that NAMES its order, so the
    # orphan is correlatable. The claim is guarded (CREATING + NULL id only),
    # UNIQUE-protected, and fails closed rather than overwriting a different id.
    claimed, claimed_attempt = catalog_attempt_service.claim_order_id(
        attempt.attempt_id, order_id
    )
    if not claimed:
        benign_reentry = (
            claimed_attempt is not None
            and claimed_attempt.status is CatalogAttemptStatus.CREATING
            and (claimed_attempt.paypal_order_id or "") == order_id
        )
        if not benign_reentry:
            logger.error(
                "Catalog order id NOT recorded for attempt %s; leaving it for "
                "recovery. No ledger row created, no different id overwritten.",
                attempt.attempt_id,
            )
            raise catalog_attempt_service.CatalogAttemptPersistenceError(
                attempt.attempt_id, order_id, claimed_attempt
            )

    # Finalize: locate-or-create the ledger row by the UNIQUE order id, verify
    # it matches the attempt, then mark the attempt FINALIZED. Idempotent and
    # recovery-safe — a re-run links the existing transaction instead of
    # inserting a duplicate. If create_transaction inside here fails (the orphan
    # window), the attempt stays CREATING WITH its order id, fully recoverable.
    transaction = finalize_attempt(attempt.attempt_id)

    logger.info(
        "Order created (%s): product=%s inr=%s usd=%s order_id=%s txn=%s",
        "explicit approval" if approval_used else "autonomous",
        product.id,
        product.price_inr,
        usd_amount,
        order_id,
        transaction.transaction_id,
    )
    return ExecutionResult(
        status="order_created",
        approval_used=approval_used,
        **_base_fields(product, decision),
        paypal_currency="USD",
        paypal_amount=usd_amount,
        demo_fx_rate=str(DEMO_INR_PER_USD),
        paypal_order_id=order_id,
        approve_url=order.get("approve_url"),
        transaction_id=transaction.transaction_id,
    )


def execute_purchase(
    product_id: str,
    authorization: Authorization,
    hard_constraints: HardConstraints | None = None,
) -> ExecutionResult:
    """Evaluate policy for a catalog product and execute only on ALLOW.

    Raises:
        ProductNotFoundError: unknown product_id (no policy run, no PayPal).
        CurrencyConversionError: catalog price not convertible (unexpected).
        PayPalAuthError / PayPalOrderError: the (allowed) order call failed.
    """
    # Authoritative product data — the catalog, nothing else — then the
    # deterministic Policy Engine. Its ruling is the only authorization.
    product = get_product(product_id)
    decision = _evaluate(product, authorization, hard_constraints)

    if decision.decision == PolicyOutcome.DENY:
        logger.info("Execution blocked (DENY/%s): %s", decision.reason_code.value, product.id)
        return ExecutionResult(status="denied", **_base_fields(product, decision))

    if decision.decision == PolicyOutcome.REQUIRE_USER_APPROVAL:
        logger.info(
            "Execution escalated (REQUIRE_USER_APPROVAL/%s): %s",
            decision.reason_code.value,
            product.id,
        )
        return ExecutionResult(
            status="approval_required", **_base_fields(product, decision)
        )

    # ALLOW_AUTONOMOUS_PURCHASE — and only now — touch money paths.
    return _create_order_result(product, decision, approval_used=False)


def approve_purchase(
    product_id: str,
    authorization: Authorization,
    hard_constraints: HardConstraints | None = None,
) -> ExecutionResult:
    """Execute after the user EXPLICITLY approved proceeding.

    The approval is an additional authorization event, not a policy bypass:
    the product is re-fetched from the catalog (its price may have changed)
    and the Policy Engine is re-run from scratch. Semantics per outcome:

      - DENY: stays denied. Explicit approval never overrides a DENY
        condition (budget violation, invalid price, bad currency, no
        purchase requested, ...).
      - REQUIRE_USER_APPROVAL: satisfied by this explicit approval ->
        proceed to the shared PayPal execution path.
      - ALLOW_AUTONOMOUS_PURCHASE: approval was not required, but an
        explicit approval is a strictly stronger authorization -> proceed.

    Raises: same as execute_purchase.
    """
    product = get_product(product_id)  # re-fetch: authoritative, current
    decision = _evaluate(product, authorization, hard_constraints)  # re-run

    if decision.decision == PolicyOutcome.DENY:
        logger.info(
            "Approval cannot override DENY (%s): %s",
            decision.reason_code.value,
            product.id,
        )
        return ExecutionResult(
            status="denied", approval_used=True, **_base_fields(product, decision)
        )

    # REQUIRE_USER_APPROVAL or ALLOW: the explicit approval covers both.
    return _create_order_result(product, decision, approval_used=True)


class CaptureLedgerDesyncError(Exception):
    """PayPal captured, but the catalog ledger row is gone. NOT a rollback.

    Reachable only in a narrow race: ownership resolution found the catalog
    transaction, PayPal then captured the order, and the row disappeared
    (or had its order id rewritten) before the CAPTURED write landed.

    This is raised instead of being logged and swallowed. The capture DID
    succeed at PayPal and nothing here undoes it — there is no compensating
    void or refund in this phase, and claiming otherwise would be a lie. So
    the error carries PayPal's own capture facts verbatim, to be surfaced to
    the caller and the logs as an operator-actionable inconsistency: real
    money moved and this server has no ledger row tracking it.
    """

    code = "CAPTURE_LEDGER_DESYNC"

    def __init__(self, result: dict) -> None:
        self.paypal_result = dict(result)
        self.order_id = result.get("order_id")
        self.capture_id = result.get("capture_id")
        super().__init__(
            f"PayPal order {self.order_id} WAS CAPTURED (capture "
            f"{self.capture_id}), but no catalog transaction could be "
            f"updated for it: the ledger row present at the ownership check "
            f"no longer exists. The PayPal capture is NOT rolled back and no "
            f"refund was issued; this server now has no ledger record "
            f"tracking a completed capture and needs manual reconciliation."
        )

    def as_detail(self) -> dict:
        """Structured detail: names the condition, states money moved."""
        return {
            "code": self.code,
            "message": str(self),
            "paypal_captured": True,
            "rolled_back": False,
            "order_id": self.order_id,
            "capture_id": self.capture_id,
            "capture_status": self.paypal_result.get("capture_status"),
            "amount": self.paypal_result.get("amount"),
            "currency": self.paypal_result.get("currency"),
        }


class CaptureNotCompletedError(Exception):
    """PayPal accepted the capture call but the NESTED capture status was not
    COMPLETED (e.g. PENDING). Funds are NOT known to be collected.

    This is NOT a desync and NOT a rollback: no ledger row was written, so the
    catalog transaction keeps its prior state (ORDER_CREATED / ORDER_APPROVED).
    A later PAYMENT.CAPTURE.COMPLETED webhook can still reconcile it to
    CAPTURED. Raised so the endpoint reports a non-success instead of a false
    collection.
    """

    code = "CAPTURE_NOT_COMPLETED"

    def __init__(self, result: dict) -> None:
        self.paypal_result = dict(result)
        self.order_id = result.get("order_id")
        self.capture_id = result.get("capture_id")
        self.capture_status = result.get("capture_status")
        super().__init__(
            f"PayPal order {self.order_id} returned capture status "
            f"{self.capture_status!r}, not COMPLETED; the catalog ledger was "
            f"not marked CAPTURED and no collection is recorded."
        )

    def as_detail(self) -> dict:
        return {
            "code": self.code,
            "message": str(self),
            "captured": False,
            "order_id": self.order_id,
            "capture_id": self.capture_id,
            "capture_status": self.capture_status,
            "amount": self.paypal_result.get("amount"),
            "currency": self.paypal_result.get("currency"),
        }


def capture_purchase(order_id: str) -> CaptureResult:
    """Capture a catalog-owned approved PayPal order.

    OWNERSHIP IS CHECKED BEFORE PAYPAL IS CONTACTED (Phase 11F-B0). The
    order id from the URL is resolved against this server's own ledgers
    first; only an id owned by a `transactions` row reaches PayPal, and the
    id forwarded upstream is the STORED one, never the raw path string. An
    id belonging to Phase 11E's external_purchase_orders, or to no ledger at
    all, is refused with no PayPal call and no ledger write. A client-
    supplied order id is not an authorization to move money.

    Beyond that gate the flow is unchanged: a thin layer over the existing,
    tested paypal.capture_order that accepts ONLY the order id (no amount,
    currency, product, or decision — PayPal is the source of truth for what
    the order is worth). Buyer approval remains PayPal's to enforce:
    capturing a CREATED order raises PayPalOrderNotApprovedError, which
    propagates for the endpoint to map; nothing is retried or fabricated.

    Raises:
        ValueError: blank/invalid order id (no lookup, no PayPal call).
        OwnershipRefusedError: EXTERNAL_ORDER_NOT_CAPTURABLE_HERE or
            UNKNOWN_ORDER — refused before any PayPal call.
        CaptureLedgerDesyncError: PayPal captured but the ledger row vanished.
        PayPalAuthError, PayPalOrderNotFoundError,
        PayPalOrderNotApprovedError, PayPalAlreadyCapturedError,
        PayPalCaptureError: propagated from the PayPal layer.
    """
    # THE GATE. Raises before any PayPal contact for an external or unknown
    # order id; returns the catalog ledger row that owns this order.
    transaction = require_catalog_order(order_id)

    # Forward the SERVER-STORED order id, not the client's path string.
    result = capture_order(transaction.paypal_order_id)

    # D-5 FIX (Phase 11F-J): PayPal accepting the capture call (HTTP 2xx) does
    # NOT by itself mean funds were collected. Only a NESTED capture status of
    # exactly COMPLETED is a collection. Any other outcome (e.g. PENDING) must
    # never be recorded as CAPTURED: leave the ledger in its prior state so a
    # later PAYMENT.CAPTURE.COMPLETED webhook can reconcile it, and report that
    # the capture did not complete rather than a false success. This restricts
    # payment authority; it never expands it.
    capture_status = (result.get("capture_status") or "").strip().upper()
    if capture_status != "COMPLETED":
        not_completed = CaptureNotCompletedError(result)
        logger.warning("%s", not_completed)
        raise not_completed

    # Capture succeeded on PayPal — only now may the ledger transition to
    # CAPTURED (a failed capture raises above, so the record keeps its
    # previous state). The row existed a moment ago at the ownership check,
    # so a miss here is a real inconsistency, not a tolerated absence: it is
    # raised, never swallowed, and the error states plainly that the PayPal
    # capture stands.
    # Keyed on the STORED order id — the row ownership resolved — so a
    # PayPal response echoing some other order id cannot redirect the write.
    try:
        updated = mark_captured(
            transaction.paypal_order_id, result.get("capture_id")
        )
    except TransactionNotFoundError as exc:
        desync = CaptureLedgerDesyncError(result)
        logger.error("%s", desync)
        raise desync from exc

    transaction_id = updated.transaction_id
    transaction_status = updated.status.value

    logger.info(
        "Purchase capture: order=%s capture=%s status=%s txn=%s",
        result.get("order_id"),
        result.get("capture_id"),
        result.get("capture_status"),
        transaction_id,
    )
    return CaptureResult(
        order_id=result["order_id"],
        order_status=result.get("status"),
        capture_id=result.get("capture_id"),
        capture_status=result.get("capture_status"),
        amount=result.get("amount"),
        currency=result.get("currency"),
        transaction_id=transaction_id,
        transaction_status=transaction_status,
    )
