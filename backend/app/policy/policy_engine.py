"""PayPilot Policy Engine: pure deterministic purchase-authorization rules.

Input -> rules -> output. Nothing else:
  - NO model calls (Gemini/Claude never decide whether a payment is allowed)
  - NO PayPal calls
  - NO network, NO catalog/search access, NO side effects

The engine receives structured facts (selected product data, the user's
Authorization, optional shopping HardConstraints) and returns a PolicyDecision
with a stable reason_code plus a human-readable reason.

Rule order (first match wins):
  V1. Invalid price (missing/zero/negative/NaN/inf)         -> DENY
  V2. Currency other than INR (no conversion in this phase) -> DENY
  R1. purchase_requested is false                           -> DENY
  R2. Price violates shopping constraints (max/min)         -> DENY
  R3. Autonomous purchasing not granted                     -> REQUIRE_USER_APPROVAL
  R6. Autonomy granted but no spend limit set               -> REQUIRE_USER_APPROVAL
      (an absent limit is NEVER unlimited authority)
  R4. price <= auto_approve_below_inr (inclusive)           -> ALLOW_AUTONOMOUS_PURCHASE
  R5. price >  auto_approve_below_inr                       -> REQUIRE_USER_APPROVAL

The shopping budget (hard_constraints.max_price_inr) and the autonomous spend
limit (authorization.auto_approve_below_inr) are evaluated by SEPARATE rules
(R2 vs R4/R5) and remain fully independent: a product may pass the budget yet
still need manual approval.
"""

from __future__ import annotations

import logging
import math
from typing import Optional

from ..schemas.intent import Authorization, HardConstraints
from ..schemas.policy import (
    PolicyDecision,
    PolicyOutcome,
    PolicySelectedProduct,
    ReasonCode,
)

logger = logging.getLogger("paypilot.policy")

SUPPORTED_CURRENCY = "INR"


def _inr(amount: float) -> str:
    """Format an amount for human-readable reasons, e.g. 42000.0 -> '₹42,000'."""
    return f"₹{amount:,.0f}"


def evaluate_policy(
    product: PolicySelectedProduct,
    authorization: Authorization,
    hard_constraints: Optional[HardConstraints] = None,
) -> PolicyDecision:
    """Apply the purchase-authorization rules to one selected product.

    Pure function: same inputs always yield the same PolicyDecision, and
    nothing outside the arguments is read or written (logging aside).
    """
    price = product.price_inr
    limit = authorization.auto_approve_below_inr
    shopping_max = hard_constraints.max_price_inr if hard_constraints else None
    shopping_min = hard_constraints.min_price_inr if hard_constraints else None

    def _decision(
        outcome: PolicyOutcome, code: ReasonCode, reason: str
    ) -> PolicyDecision:
        result = PolicyDecision(
            decision=outcome,
            reason_code=code,
            reason=reason,
            product_id=product.product_id,
            product_price_inr=price,
            currency=product.currency,
            shopping_max_inr=shopping_max,
            authorization_limit_inr=limit,
        )
        logger.info(
            "Policy: %s (%s) product=%s price=%s limit=%s",
            outcome.value,
            code.value,
            product.product_id,
            price,
            limit,
        )
        return result

    # V1 — the price must be a usable positive finite number before any other
    # reasoning; a corrupt price can never be approved or escalated.
    if price is None or not math.isfinite(price) or price <= 0:
        return _decision(
            PolicyOutcome.DENY,
            ReasonCode.INVALID_PRODUCT_PRICE,
            f"Product price is missing or invalid ({price!r}); "
            "a purchase cannot be evaluated.",
        )

    # V2 — INR only in this phase; no currency conversion.
    if product.currency.strip().upper() != SUPPORTED_CURRENCY:
        return _decision(
            PolicyOutcome.DENY,
            ReasonCode.UNSUPPORTED_CURRENCY,
            f"Product currency '{product.currency}' is not supported; "
            f"only {SUPPORTED_CURRENCY} is accepted in this phase.",
        )

    # R1 — a recommendation request is NOT permission to purchase.
    if not authorization.purchase_requested:
        return _decision(
            PolicyOutcome.DENY,
            ReasonCode.NO_PURCHASE_REQUESTED,
            "No purchase was requested.",
        )

    # R2 — shopping constraints are hard requirements on the product itself.
    if shopping_max is not None and price > shopping_max:
        return _decision(
            PolicyOutcome.DENY,
            ReasonCode.EXCEEDS_SHOPPING_MAX,
            f"Product price {_inr(price)} exceeds the shopping budget "
            f"of {_inr(shopping_max)}.",
        )
    if shopping_min is not None and price < shopping_min:
        return _decision(
            PolicyOutcome.DENY,
            ReasonCode.BELOW_SHOPPING_MIN,
            f"Product price {_inr(price)} is below the required minimum "
            f"of {_inr(shopping_min)}.",
        )

    # R3 — the user wants it bought, but did not grant autonomous authority.
    if not authorization.autonomous_purchase_allowed:
        return _decision(
            PolicyOutcome.REQUIRE_USER_APPROVAL,
            ReasonCode.AUTONOMY_NOT_GRANTED,
            "A purchase was requested, but autonomous purchasing was not "
            "authorized; explicit user approval is required.",
        )

    # R6 — autonomy without a stated ceiling is never unlimited authority.
    if limit is None:
        return _decision(
            PolicyOutcome.REQUIRE_USER_APPROVAL,
            ReasonCode.NO_AUTONOMOUS_LIMIT,
            "Autonomous purchasing was authorized but no spending limit was "
            "set; unlimited authority is never assumed, so user approval is "
            "required.",
        )

    # R4 — within the autonomous spend limit (inclusive boundary).
    if price <= limit:
        return _decision(
            PolicyOutcome.ALLOW_AUTONOMOUS_PURCHASE,
            ReasonCode.WITHIN_AUTONOMOUS_LIMIT,
            f"Product price {_inr(price)} is within the user's autonomous "
            f"purchase limit of {_inr(limit)}.",
        )

    # R5 — passes the shopping rules, but exceeds the autonomy ceiling.
    return _decision(
        PolicyOutcome.REQUIRE_USER_APPROVAL,
        ReasonCode.EXCEEDS_AUTONOMOUS_LIMIT,
        f"The product satisfies the shopping requirements but its price "
        f"{_inr(price)} exceeds the user's autonomous purchase limit of "
        f"{_inr(limit)}; user approval is required.",
    )
