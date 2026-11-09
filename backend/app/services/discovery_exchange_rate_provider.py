"""Discovery exchange-rate abstraction (Phase 10F).

This is the ONLY source of exchange rates for product DISCOVERY. It exists
so a USD-priced discovery result can be compared against an INR shopping
budget deterministically.

SEPARATE FROM PAYMENT, DELIBERATELY
    currency_service.DEMO_INR_PER_USD is payment-demo logic: it converts an
    INR catalog price into the USD amount PayPal Sandbox is charged. It is
    part of the frozen payment core and is NOT imported, read, or reused
    here — not even as a default value. The two numbers are allowed to
    differ, and tests assert that they do, so that a change to one can never
    silently move the other.

    Nothing produced by this module may ever become a PayPal order amount, a
    capture amount, or a refund amount. These rates exist for shopping
    research, budget comparison, ranking and display only.

NO GUESSING
    No LLM is consulted. No website is scraped. No rate is inferred from a
    search result. A pair this module does not support deterministically
    yields UnsupportedCurrencyPairError, and the caller leaves the derived
    fields null rather than converting on a guess.

SUPPORTED PAIRS
    USD -> INR, plus the identity pair (X -> X at exactly 1), which is a
    tautology rather than a market claim. Everything else is unsupported;
    this module does not pretend to cover global FX.

EXTENSION POINT
    `get_rate_provider()` is the single seam. A live-rate implementation can
    replace ConfiguredDemoRateProvider without any change to normalization,
    filtering, ranking or comparison logic — they depend on this interface,
    never on the rate's origin.
"""

from __future__ import annotations

import logging
import os
import re
from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict

load_dotenv()

logger = logging.getLogger("paypilot.discovery_rate")

#: ISO-4217-shaped code: exactly three ASCII letters.
_CURRENCY_CODE = re.compile(r"^[A-Za-z]{3}$")

#: Environment variable holding the configured discovery rate. Named for
#: discovery specifically so it can never be confused with, or defaulted
#: from, any PayPal configuration.
RATE_ENV_VAR = "DISCOVERY_USD_INR_RATE"

#: Fallback USD->INR rate used when RATE_ENV_VAR is unset, so local demos
#: work out of the box. This is a CONFIGURED DEMONSTRATION RATE, not a live
#: market quote, and it is reported as such in conversion_source. It is
#: deliberately NOT equal to currency_service.DEMO_INR_PER_USD.
DEFAULT_DEMO_USD_INR_RATE = Decimal("91.25")

#: conversion_source values. These strings reach the API response, so they
#: must stay honest about where a rate came from.
SOURCE_CONFIGURED_DEMO = "configured_demo_rate"
SOURCE_IDENTITY = "identity"


class DiscoveryRateError(Exception):
    """Base class for all discovery exchange-rate failures."""


class InvalidCurrencyError(DiscoveryRateError, ValueError):
    """A currency code was malformed (not three letters)."""


class DiscoveryRateConfigError(DiscoveryRateError):
    """The configured rate is missing, non-numeric, or not a positive value."""


class UnsupportedCurrencyPairError(DiscoveryRateError):
    """No deterministic rate exists for this pair.

    This is an expected, non-fatal outcome: the caller preserves the observed
    price and leaves the derived fields null. It never fails a discovery
    request.
    """


class DiscoveryRateUnavailableError(DiscoveryRateError):
    """A rate source was reachable in principle but produced no usable rate."""


class ExchangeRate(BaseModel):
    """One rate plus the provenance needed to report it honestly.

    `get_rate` returns this rather than a bare Decimal so that
    conversion_source and rate_observed_at travel with the number they
    describe; `.rate` is the Decimal multiplier.

    observed_at is None for a configured rate: a constant from configuration
    was never "obtained" at a point in time, and stamping it with now() would
    imply a freshness it does not have.
    """

    model_config = ConfigDict(frozen=True)

    from_currency: str
    to_currency: str
    rate: Decimal
    source: str
    observed_at: Optional[datetime] = None


def normalize_currency_code(code: Optional[str]) -> str:
    """Validate and canonicalize a currency code to upper case.

    Raises:
        InvalidCurrencyError: empty, non-string, or not three letters.
    """
    if not isinstance(code, str) or not _CURRENCY_CODE.match(code.strip()):
        raise InvalidCurrencyError(f"Not a valid 3-letter currency code: {code!r}")
    return code.strip().upper()


def validate_rate(value: object, *, context: str) -> Decimal:
    """Coerce `value` to a finite, strictly positive Decimal.

    Floats are routed through str() so a binary artifact never becomes a
    rate. A zero or negative rate is rejected: it would silently zero out or
    invert every converted price.
    """
    try:
        rate = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise DiscoveryRateConfigError(
            f"{context} is not a valid number: {value!r}"
        ) from exc

    if not rate.is_finite() or rate <= 0:
        raise DiscoveryRateConfigError(
            f"{context} must be a finite value greater than zero, got {value!r}"
        )
    return rate


class DiscoveryExchangeRateProvider(ABC):
    """One source of discovery exchange rates."""

    #: Value written into conversion_source.
    source: str

    @abstractmethod
    def get_rate(self, from_currency: str, to_currency: str) -> ExchangeRate:
        """Return the rate to multiply a `from_currency` amount by.

        Raises:
            InvalidCurrencyError: a code was malformed.
            UnsupportedCurrencyPairError: the pair is not supported.
            DiscoveryRateUnavailableError / DiscoveryRateConfigError: the
                source could not produce a usable rate.
        """


class ConfiguredDemoRateProvider(DiscoveryExchangeRateProvider):
    """USD -> INR from configuration; identity for same-currency pairs.

    The rate comes from DISCOVERY_USD_INR_RATE, falling back to
    DEFAULT_DEMO_USD_INR_RATE. It is reported as "configured_demo_rate" and
    is never described as live or current, because it is neither.
    """

    source = SOURCE_CONFIGURED_DEMO

    def _configured_usd_inr(self) -> Decimal:
        raw = os.getenv(RATE_ENV_VAR, "").strip()
        if not raw:
            return DEFAULT_DEMO_USD_INR_RATE
        return validate_rate(raw, context=RATE_ENV_VAR)

    def get_rate(self, from_currency: str, to_currency: str) -> ExchangeRate:
        source_code = normalize_currency_code(from_currency)
        target_code = normalize_currency_code(to_currency)

        if source_code == target_code:
            # A tautology, not a market claim: 1 unit is 1 unit.
            return ExchangeRate(
                from_currency=source_code,
                to_currency=target_code,
                rate=Decimal(1),
                source=SOURCE_IDENTITY,
            )

        if (source_code, target_code) == ("USD", "INR"):
            return ExchangeRate(
                from_currency=source_code,
                to_currency=target_code,
                rate=self._configured_usd_inr(),
                source=self.source,
                # Configured, not observed — see ExchangeRate's docstring.
                observed_at=None,
            )

        raise UnsupportedCurrencyPairError(
            f"No deterministic discovery rate for {source_code}->{target_code}; "
            "the observed price is preserved and no derived price is produced"
        )


def get_rate_provider() -> DiscoveryExchangeRateProvider:
    """Return the configured discovery rate provider.

    The single extension point for this phase: swapping in a live-rate
    implementation here changes nothing in normalization, filtering, ranking,
    or comparison.
    """
    return ConfiguredDemoRateProvider()
