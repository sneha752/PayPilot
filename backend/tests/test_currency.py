"""Tests for the deterministic demo INR -> USD currency service.

Pure Decimal arithmetic against the fixed demo rate (1 USD = 90 INR):
no network, no mocking needed.
"""

from decimal import Decimal

import pytest

from app.services.currency_service import (
    DEMO_INR_PER_USD,
    CurrencyConversionError,
    convert_inr_to_usd,
)


def test_demo_rate_is_90():
    assert DEMO_INR_PER_USD == Decimal("90")


def test_90_inr_is_one_dollar():
    assert convert_inr_to_usd(90) == "1.00"


def test_180_inr_is_two_dollars():
    assert convert_inr_to_usd(180) == "2.00"


def test_catalog_price_68990_rounds_half_up():
    # 68990 / 90 = 766.5555... -> 766.56 (half-up to two places).
    assert convert_inr_to_usd(68990) == "766.56"


def test_more_rounding_cases():
    assert convert_inr_to_usd(52990) == "588.78"   # 588.777... rounds up
    assert convert_inr_to_usd(45) == "0.50"        # exact half-dollar
    assert convert_inr_to_usd(1) == "0.01"         # 0.0111... rounds down


def test_accepts_string_and_decimal_inputs():
    assert convert_inr_to_usd("68990") == "766.56"
    assert convert_inr_to_usd(Decimal("68990")) == "766.56"


def test_result_has_exactly_two_decimal_places():
    for amount in (90, 91, 123456, 68990):
        value = convert_inr_to_usd(amount)
        whole, _, frac = value.partition(".")
        assert len(frac) == 2
        # Round-trips through Decimal without binary-float artifacts.
        assert Decimal(value) == Decimal(whole) + Decimal(f"0.{frac}")


def test_conversion_is_deterministic():
    results = {convert_inr_to_usd(68990) for _ in range(100)}
    assert results == {"766.56"}


@pytest.mark.parametrize(
    "bad_amount",
    [-1, 0, -68990, "not-a-number", "", None, float("nan"), float("inf")],
    ids=["negative", "zero", "very-negative", "text", "empty", "none", "nan", "inf"],
)
def test_invalid_amounts_rejected(bad_amount):
    with pytest.raises(CurrencyConversionError):
        convert_inr_to_usd(bad_amount)
