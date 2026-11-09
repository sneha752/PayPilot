"""Deterministic demo currency conversion: INR -> USD.

The catalog is priced in INR while the PayPal Sandbox checkout runs in USD,
so this service bridges the two with a FIXED demo rate. It is intentionally
NOT a real FX integration: no live rate API, no scraping, no AI — the rate is
a single constant below, which makes every conversion reproducible in tests
and demos.

All arithmetic uses Decimal (never binary floats) and rounds HALF-UP to
exactly two places, the format PayPal's Orders API expects for an amount
value, e.g. Decimal("68990") / 90 -> "766.56".
"""

from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Union

logger = logging.getLogger("paypilot.currency")

# THE demo FX rate — the one and only place it is defined: 1 USD = 90 INR.
DEMO_INR_PER_USD = Decimal("90")

# PayPal amount values carry exactly two decimal places.
_TWO_PLACES = Decimal("0.01")


class CurrencyConversionError(ValueError):
    """Raised when an amount cannot be converted (non-numeric, <= 0, NaN...)."""


def convert_inr_to_usd(amount_inr: Union[int, float, str, Decimal]) -> str:
    """Convert an INR amount to a USD string suitable for PayPal.

    Args:
        amount_inr: The INR amount (int/str/Decimal preferred; floats are
            accepted and routed through str() to avoid binary artifacts).

    Returns:
        The USD value rounded half-up to exactly two decimal places, as a
        string like "766.56" — the shape PayPal's amount.value field expects.

    Raises:
        CurrencyConversionError: if the amount is not a number, or is not a
            finite value greater than zero.
    """
    try:
        inr = Decimal(str(amount_inr))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise CurrencyConversionError(
            f"INR amount is not a valid number: {amount_inr!r}"
        ) from exc

    if not inr.is_finite() or inr <= 0:
        raise CurrencyConversionError(
            f"INR amount must be a finite value greater than zero, got {amount_inr!r}"
        )

    usd = (inr / DEMO_INR_PER_USD).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    logger.info("Converted INR %s -> USD %s (demo rate %s INR/USD)",
                inr, usd, DEMO_INR_PER_USD)
    return f"{usd:.2f}"
