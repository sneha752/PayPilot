"""Deterministic discovery price normalization (Phase 10F).

Turns a DiscoveredProduct into a NormalizedDiscoveredProduct by DERIVING a
price in a target currency, so an INR shopping budget can be compared
against a USD-priced discovery result without ever comparing the two
numbers directly.

THE OBSERVED PRICE IS IMMUTABLE
    observed_price and observed_currency are copied through byte for byte.
    Normalization only ADDS normalized_price / normalized_currency /
    conversion_rate / conversion_source / rate_observed_at. Nothing in this
    module writes to an observed field, and a caller-supplied derived field
    is stripped before normalization so neither a provider nor a client can
    inject one.

ARITHMETIC
    normalized_price = observed_price * conversion_rate

    Decimal end to end — never float. The result is quantized to exactly two
    decimal places with ROUND_HALF_UP, matching the project's existing
    monetary rounding convention (currency_service uses the same policy for
    PayPal amounts).

    Worked example, with the configured demonstration rate 91.25 INR/USD:
        observed      USD 34.99
        rate          91.25
        product       34.99 * 91.25 = 3192.8375
        quantized     INR 3192.84

FAILURE IS NULL, NEVER A GUESS
    No observed price, no observed currency, an unsupported pair, or a rate
    source failure all produce the same honest outcome: every derived field
    stays null and the observed price is preserved. One bad candidate never
    fails a whole discovery request.

SEPARATION FROM PAYMENT
    currency_service / DEMO_INR_PER_USD are NOT imported here. Derived
    discovery prices are for research, budget comparison, ranking and
    display only, and may never become a PayPal, capture, or refund amount.
"""

from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Iterable, Optional

from ..schemas.discovery import DiscoveredProduct, NormalizedDiscoveredProduct
from .discovery_exchange_rate_provider import (
    SOURCE_IDENTITY,
    DiscoveryExchangeRateProvider,
    DiscoveryRateError,
    get_rate_provider,
    normalize_currency_code,
    validate_rate,
)

logger = logging.getLogger("paypilot.discovery_currency")

#: The project's display/shopping currency. The local catalog is priced in
#: INR (products.json uses price_inr), and intent budgets are stated in INR,
#: so INR is the default normalization target.
DEFAULT_NORMALIZATION_CURRENCY = "INR"

#: Monetary presentation: exactly two decimal places, ROUND_HALF_UP — the
#: same policy currency_service applies to PayPal amounts.
_TWO_PLACES = Decimal("0.01")

#: Derived fields. Stripped from any input before normalization so they can
#: only ever be produced by this module.
DERIVED_FIELDS = (
    "normalized_price",
    "normalized_currency",
    "conversion_rate",
    "conversion_source",
    "rate_observed_at",
)


class DiscoveryConversionError(ValueError):
    """An amount could not be converted (non-numeric, negative, NaN...)."""


def convert_amount(amount: object, rate: object) -> Decimal:
    """Multiply a monetary amount by a rate, Decimal end to end.

    Returns the product quantized to two places with ROUND_HALF_UP.

    Raises:
        DiscoveryConversionError: the amount is not a finite, non-negative
            number.
        DiscoveryRateConfigError: the rate is not finite and positive.
    """
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise DiscoveryConversionError(
            f"Observed amount is not a valid number: {amount!r}"
        ) from exc

    if not value.is_finite() or value < 0:
        raise DiscoveryConversionError(
            f"Observed amount must be a finite, non-negative value, got {amount!r}"
        )

    multiplier = validate_rate(rate, context="Discovery exchange rate")
    return (value * multiplier).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


def normalize_product(
    product: DiscoveredProduct,
    target_currency: str = DEFAULT_NORMALIZATION_CURRENCY,
    provider: Optional[DiscoveryExchangeRateProvider] = None,
) -> NormalizedDiscoveredProduct:
    """Return a NormalizedDiscoveredProduct carrying derived price fields.

    The input is never mutated. Observed fields are copied through
    unchanged; only the derived fields are added, and they are all null
    together when no honest derivation is possible.

    Args:
        product: The provider's normalized discovery record.
        target_currency: Currency to derive a price in (default INR).
        provider: Rate source; defaults to the configured provider.
    """
    carried = product.model_dump()
    # A provider or client cannot smuggle in a derived price: only this
    # module may ever set these.
    for field in DERIVED_FIELDS:
        carried.pop(field, None)

    normalized = NormalizedDiscoveredProduct(**carried)

    if product.observed_price is None or not product.observed_currency:
        # Nothing observed to convert. Not zero, not an estimate — null.
        return normalized

    try:
        target = normalize_currency_code(target_currency)
        rate = (provider or get_rate_provider()).get_rate(
            product.observed_currency, target
        )
        derived = convert_amount(product.observed_price, rate.rate)
    except (DiscoveryRateError, DiscoveryConversionError) as exc:
        # Unsupported pair, bad configuration, or an unusable amount. The
        # observed price survives; the derived fields stay null.
        logger.info(
            "No derived price for %s (%s->%s): %s",
            product.external_id,
            product.observed_currency,
            target_currency,
            type(exc).__name__,
        )
        return normalized

    normalized.normalized_price = derived
    normalized.normalized_currency = rate.to_currency
    normalized.conversion_rate = rate.rate
    normalized.conversion_source = rate.source
    normalized.rate_observed_at = rate.observed_at
    return normalized


def normalize_products(
    products: Iterable[DiscoveredProduct],
    target_currency: str = DEFAULT_NORMALIZATION_CURRENCY,
    provider: Optional[DiscoveryExchangeRateProvider] = None,
) -> list[NormalizedDiscoveredProduct]:
    """Normalize a batch, resolving the rate provider exactly once.

    A candidate whose currency cannot be converted simply keeps null derived
    fields; it never aborts the batch.
    """
    resolved = provider or get_rate_provider()
    return [normalize_product(p, target_currency, resolved) for p in products]


def comparable_amount(
    product: DiscoveredProduct, currency: Optional[str]
) -> Optional[Decimal]:
    """The amount of `product` that may be compared against `currency`.

    Resolution order, and why:

      1. observed_price, when observed_currency already IS `currency` — an
         exact observation beats a derived value whenever one exists.
      2. normalized_price, when normalized_currency is `currency` — the
         derived value, which is what makes cross-currency comparison
         possible at all.
      3. None — not comparable. The caller must not compare it to anything.

    (1) and (2) agree by construction for a same-currency candidate, since
    that pair normalizes at the identity rate of exactly 1.

    With `currency` None there is no target, so only a priced-and-denominated
    observation is returned; this preserves Phase 10E semantics for callers
    with no monetary constraint.
    """
    if product.observed_price is None or not product.observed_currency:
        observed = None
    else:
        observed = product.observed_price

    if currency is None:
        return observed

    target = currency.strip().upper()

    if observed is not None and product.observed_currency.strip().upper() == target:
        return observed

    # getattr keeps this usable for a plain DiscoveredProduct, which has no
    # derived fields at all.
    derived = getattr(product, "normalized_price", None)
    derived_currency = getattr(product, "normalized_currency", None)
    if derived is not None and derived_currency:
        if derived_currency.strip().upper() == target:
            return derived

    return None


def describe_conversion(product: DiscoveredProduct) -> Optional[str]:
    """A deterministic, backend-assembled sentence about a derived price.

    Returned so the conversion can be stated in the API response WITHOUT an
    LLM asserting a converted number. None when nothing was derived, or when
    the "conversion" was the identity (no arithmetic worth reporting).

    The wording names the rate's provenance and the word "derived", so a
    configured demonstration rate is never read as a live market quote.
    """
    price = getattr(product, "normalized_price", None)
    currency = getattr(product, "normalized_currency", None)
    rate = getattr(product, "conversion_rate", None)
    source = getattr(product, "conversion_source", None)

    if price is None or not currency or rate is None:
        return None
    if source == SOURCE_IDENTITY:
        return None

    provenance = (
        "a configured demonstration rate"
        if source == "configured_demo_rate"
        else f"rate source '{source}'"
    )
    return (
        f"Observed {product.observed_currency} {product.observed_price} converts to "
        f"a derived {currency} {price:,.2f} at {provenance} of {rate} "
        f"{currency} per {product.observed_currency}."
    )
