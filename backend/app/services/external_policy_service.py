"""External-product policy evaluation (Phase 11C).

Connects the adoption/revalidation flow to the EXISTING deterministic Policy
Engine. It is a gate plus an adapter, and nothing more:

    AdoptedProduct
      + latest revalidation snapshot        <- server-side state only
      + stored purchase policy settings
            |
            v
    [ eligibility gate ]  is there fresh, trusted evidence to evaluate?
            |  no  -> NOT_ELIGIBLE, with a reason. The engine is NOT asked.
            |  yes
            v
    [ adapter ]  build the engine's existing PolicySelectedProduct input
            |
            v
    evaluate_policy(...)   <- the EXISTING engine, sole authority
            |
            v
    its PolicyDecision, returned verbatim

THERE IS NO SECOND POLICY ENGINE HERE
    This module contains no price threshold, no limit comparison, no budget
    check, and no notion of "allowed". Search this file for a comparison
    against a spend limit and there is none: every such rule lives in
    policy_engine.py and is reached through evaluate_policy(). The engine
    also stays product-type agnostic — it is never told that this product
    came from an external provider, because that is not a policy input.

    Even the rules it would be tempting to duplicate are left to it. A
    non-INR normalized currency is NOT rejected here; it is passed through so
    the engine's own V2 rule issues UNSUPPORTED_CURRENCY. An invalid price
    likewise reaches V1. Re-implementing either would create exactly the
    second policy engine this phase forbids.

THE GATE: NO SUCCESSFUL REVALIDATION -> NO PURCHASE
    Only RevalidationStatus.REVALIDATED may enter policy evaluation. Every
    other status is an UPSTREAM ELIGIBILITY FAILURE reported with its own
    code — never converted into a policy DENY, because the engine never
    ruled on it.

    IDENTITY_NOT_FOUND deserves singling out: it does NOT mean the product
    was withdrawn. Phase 11B.1 measured a product whose durable id was fully
    intact disappearing from, and reappearing in, provider result sets within
    the hour. It means "current provider evidence could not confirm this
    product", which is an absence of knowledge, not a fact about the product.

THE ADOPTED PRICE IS NEVER A FALLBACK
    The only price that reaches the engine is current_normalized_price from
    the latest revalidation. If that is null, the outcome is
    NO_FRESH_NORMALIZED_PRICE — not the adopted snapshot's observed price,
    not its normalized price, not an estimate. _policy_input() reads the
    AdoptedProduct for its ID ONLY; it cannot read a price from it, because
    no such argument is passed.

LATEST APPLICABLE REVALIDATION
    The single most recent snapshot, ordered by (checked_at DESC,
    revalidation_id DESC) — the ordering revalidation_service already
    guarantees. If that one is not REVALIDATED, the answer is NOT_ELIGIBLE,
    even when an older successful snapshot exists. Preferring an older
    success would mean evaluating a product on evidence we have since
    superseded, which is precisely the stale-data path this phase exists to
    close.

NO PAYMENT, NO AI, NO MUTATION
    No PayPal call, no order, no capture, no refund, no ledger write, no
    session, no confirmation, no webhook, and no AI provider. Nothing here
    writes to the adopted record or to the revalidation history either: this
    is a read-and-decide operation. An ALLOW_AUTONOMOUS_PURCHASE result
    creates nothing — it states what the rules would permit if and when a
    later phase builds an execution path.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

from ..policy import evaluate_policy
from ..schemas.adoption import AdoptedProduct, AdoptedProductStatus
from ..schemas.external_policy import (
    EligibilityReason,
    EligibilityStatus,
    ExternalPolicyEvaluation,
)
from ..schemas.policy import PolicySelectedProduct
from ..schemas.revalidation import RevalidationSnapshot, RevalidationStatus
from . import adoption_service, purchase_policy_settings, revalidation_service

logger = logging.getLogger("paypilot.external_policy")

#: Upstream revalidation statuses mapped to their eligibility reason. Every
#: non-REVALIDATED status is listed explicitly: a status missing from this
#: table would raise rather than silently fall through to "eligible".
_UPSTREAM_REASONS = {
    RevalidationStatus.IDENTITY_NOT_FOUND: (
        EligibilityReason.LATEST_REVALIDATION_IDENTITY_NOT_FOUND
    ),
    RevalidationStatus.IDENTITY_UNVERIFIABLE: (
        EligibilityReason.LATEST_REVALIDATION_IDENTITY_UNVERIFIABLE
    ),
    RevalidationStatus.PROVIDER_UNAVAILABLE: (
        EligibilityReason.LATEST_REVALIDATION_PROVIDER_UNAVAILABLE
    ),
    RevalidationStatus.PROVIDER_ERROR: (
        EligibilityReason.LATEST_REVALIDATION_PROVIDER_ERROR
    ),
    RevalidationStatus.NOT_REVALIDATABLE: (
        EligibilityReason.LATEST_REVALIDATION_NOT_REVALIDATABLE
    ),
}


def latest_revalidation(
    adopted_product_id: str,
) -> Optional[RevalidationSnapshot]:
    """The most recent revalidation snapshot, or None if there is none.

    Deliberately the LATEST one, not the latest successful one. The caller
    must see the current state of knowledge, including a failure that
    superseded an earlier success.
    """
    history = revalidation_service.list_revalidations(adopted_product_id, limit=1)
    return history[0] if history else None


def _policy_input(
    adopted_product_id: str,
    price: Decimal,
    currency: str,
) -> PolicySelectedProduct:
    """Build the engine's EXISTING input from fresh evidence.

    Note what this signature cannot do: it receives a price and a currency as
    arguments and has no access to the AdoptedProduct, so there is no way for
    the adopted snapshot's stale price to be substituted here.

    `product_id` is PayPilot's own adopted_product_id — our stable handle for
    the product — not the provider's observation id, which Phase 11B.1
    established is not a durable name in every case.

    DECIMAL -> FLOAT BOUNDARY, stated plainly: PolicySelectedProduct.price_inr
    is typed float by the existing engine, which this phase must not change.
    The fresh price is an exact Decimal, so one conversion happens here, at
    the boundary, and the exact Decimal is what this service reports back in
    its own response. The conversion is a narrowing and is documented rather
    than hidden; it is not re-rounded, re-scaled, or re-derived.
    """
    return PolicySelectedProduct(
        product_id=adopted_product_id,
        price_inr=float(price),
        currency=currency,
    )


def evaluate_adopted_product(
    adopted_product_id: str,
) -> ExternalPolicyEvaluation:
    """Evaluate one adopted product against the existing Policy Engine.

    The ONLY input is the adopted product's id. The authorization, the
    shopping constraints, the product and the fresh price all come from
    server-side state, so no caller can influence the decision.

    Raises:
        ValueError: blank id.
        AdoptedProductNotFoundError: no such adopted product.
    """
    adopted: AdoptedProduct = adoption_service.get(adopted_product_id)
    snapshot = latest_revalidation(adopted.adopted_product_id)

    def not_eligible(
        reason: EligibilityReason,
        snap: Optional[RevalidationSnapshot] = snapshot,
    ) -> ExternalPolicyEvaluation:
        """Report an upstream failure. The Policy Engine is NOT consulted.

        policy_decision stays null and no price is echoed, so an upstream
        absence can never be read as a ruling or as a current price.
        """
        logger.info(
            "External policy: NOT_ELIGIBLE %s for %s (revalidation=%s)",
            reason.value,
            adopted.adopted_product_id,
            snap.revalidation_id if snap else None,
        )
        return ExternalPolicyEvaluation(
            adopted_product_id=adopted.adopted_product_id,
            eligibility=EligibilityStatus.NOT_ELIGIBLE,
            eligibility_reason=reason,
            revalidation_id=snap.revalidation_id if snap else None,
            revalidation_checked_at=snap.checked_at if snap else None,
            revalidation_status=(
                snap.revalidation_status.value if snap else None
            ),
            price_status=snap.price_status.value if snap else None,
            current_normalized_price=None,
            current_normalized_currency=None,
            policy_decision=None,
        )

    # Gate 0 — an operator's DISABLED decision outranks fresh evidence.
    if adopted.status is AdoptedProductStatus.DISABLED:
        return not_eligible(EligibilityReason.ADOPTED_PRODUCT_DISABLED)

    # Gate 1 — no revalidation at all means no current evidence. The adopted
    # snapshot is not a substitute for it.
    if snapshot is None:
        return not_eligible(EligibilityReason.NO_REVALIDATION_ON_RECORD, None)

    # Gate 2 — the LATEST revalidation must itself have confirmed the
    # product. An older success does not count.
    if snapshot.revalidation_status is not RevalidationStatus.REVALIDATED:
        reason = _UPSTREAM_REASONS.get(snapshot.revalidation_status)
        if reason is None:  # pragma: no cover - every status is mapped
            raise AssertionError(
                "Unmapped revalidation status reached the policy gate: "
                f"{snapshot.revalidation_status!r}"
            )
        return not_eligible(reason)

    # Gate 3 — identity was confirmed, but a price is still required. Null
    # means null: nothing is borrowed from the adopted record.
    if (
        snapshot.current_normalized_price is None
        or not snapshot.current_normalized_currency
    ):
        return not_eligible(EligibilityReason.NO_FRESH_NORMALIZED_PRICE)

    # --- Eligible. Hand the FRESH price to the existing engine. ---
    authorization = purchase_policy_settings.get_authorization()
    hard_constraints = purchase_policy_settings.get_hard_constraints()

    decision = evaluate_policy(
        product=_policy_input(
            adopted.adopted_product_id,
            snapshot.current_normalized_price,
            snapshot.current_normalized_currency,
        ),
        authorization=authorization,
        hard_constraints=hard_constraints,
    )

    logger.info(
        "External policy: EVALUATED %s for %s (decision=%s reason=%s "
        "fresh_price=%s %s revalidation=%s price_status=%s)",
        EligibilityStatus.EVALUATED.value,
        adopted.adopted_product_id,
        decision.decision.value,
        decision.reason_code.value,
        snapshot.current_normalized_currency,
        snapshot.current_normalized_price,
        snapshot.revalidation_id,
        snapshot.price_status.value,
    )

    return ExternalPolicyEvaluation(
        adopted_product_id=adopted.adopted_product_id,
        eligibility=EligibilityStatus.EVALUATED,
        eligibility_reason=EligibilityReason.FRESH_EVIDENCE_CONFIRMED,
        revalidation_id=snapshot.revalidation_id,
        revalidation_checked_at=snapshot.checked_at,
        revalidation_status=snapshot.revalidation_status.value,
        price_status=snapshot.price_status.value,
        # The exact Decimal, not the float the engine was handed.
        current_normalized_price=snapshot.current_normalized_price,
        current_normalized_currency=snapshot.current_normalized_currency,
        # The engine's own ruling, unchanged and unrenamed.
        policy_decision=decision,
    )
