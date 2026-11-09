"""Discovery exchange rate and price normalization tests (Phase 10F).

Pure unit tests: no HTTP, no AI, no external exchange-rate request. The rate
provider is either the configured demo provider (a constant) or a fake.

The central properties asserted here are that the observed price is
immutable, that every derived value is exact Decimal arithmetic with
ROUND_HALF_UP, that an unconvertible candidate yields nulls rather than a
guess, and that the PayPal demo rate is never involved.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.schemas.discovery import DiscoveredProduct, NormalizedDiscoveredProduct
from app.services import discovery_currency_service as dcs
from app.services import discovery_exchange_rate_provider as drp
from app.services.currency_service import DEMO_INR_PER_USD
from app.services.discovery_exchange_rate_provider import (
    DEFAULT_DEMO_USD_INR_RATE,
    SOURCE_CONFIGURED_DEMO,
    SOURCE_IDENTITY,
    ConfiguredDemoRateProvider,
    DiscoveryExchangeRateProvider,
    DiscoveryRateConfigError,
    DiscoveryRateUnavailableError,
    ExchangeRate,
    InvalidCurrencyError,
    UnsupportedCurrencyPairError,
    get_rate_provider,
)

OBSERVED_AT = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clear_rate_env(monkeypatch):
    """Each test starts from the documented default, not the dev .env."""
    monkeypatch.delenv(drp.RATE_ENV_VAR, raising=False)


def product(
    external_id="p1",
    *,
    price=None,
    currency=None,
    title="Wireless Headphones",
) -> DiscoveredProduct:
    return DiscoveredProduct(
        external_id=external_id,
        source="serper",
        title=title,
        observed_price=Decimal(price) if price is not None else None,
        observed_currency=currency,
        observed_at=OBSERVED_AT,
    )


class FakeRateProvider(DiscoveryExchangeRateProvider):
    """Returns a scripted rate, or raises, without any network call."""

    source = "fake_rate_source"

    def __init__(self, rate=None, error=None, observed_at=None):
        self.rate = Decimal(rate) if rate is not None else None
        self.error = error
        self.observed_at = observed_at
        self.calls = []

    def get_rate(self, from_currency, to_currency):
        self.calls.append((from_currency, to_currency))
        if self.error:
            raise self.error
        return ExchangeRate(
            from_currency=from_currency.upper(),
            to_currency=to_currency.upper(),
            rate=self.rate,
            source=self.source,
            observed_at=self.observed_at,
        )


# ---------------------------------------------------------------------------
# 1-2. USD -> INR rate and multiplication
# ---------------------------------------------------------------------------


def test_usd_to_inr_returns_a_decimal_rate():
    rate = get_rate_provider().get_rate("USD", "INR")

    assert isinstance(rate.rate, Decimal)
    assert rate.rate == DEFAULT_DEMO_USD_INR_RATE
    assert rate.from_currency == "USD"
    assert rate.to_currency == "INR"


def test_configured_rate_overrides_the_default(monkeypatch):
    monkeypatch.setenv(drp.RATE_ENV_VAR, "88.40")

    assert get_rate_provider().get_rate("USD", "INR").rate == Decimal("88.40")


def test_currency_codes_are_case_and_whitespace_insensitive():
    rate = get_rate_provider().get_rate(" usd ", "inr")

    assert rate.from_currency == "USD"
    assert rate.to_currency == "INR"
    assert rate.rate == DEFAULT_DEMO_USD_INR_RATE


def test_conversion_multiplies_exactly():
    # The worked example: 34.99 * 91.25 = 3192.8375 -> 3192.84
    assert dcs.convert_amount(Decimal("34.99"), Decimal("91.25")) == Decimal("3192.84")


def test_conversion_uses_decimal_not_float():
    # 0.1 * 3 in binary floating point is 0.30000000000000004.
    assert dcs.convert_amount(Decimal("0.1"), Decimal("3")) == Decimal("0.30")


def test_float_inputs_are_routed_through_str_to_avoid_binary_artifacts():
    from_float = dcs.convert_amount(34.99, 91.25)
    from_decimal = dcs.convert_amount(Decimal("34.99"), Decimal("91.25"))

    assert from_float == from_decimal == Decimal("3192.84")


def test_result_always_has_exactly_two_decimal_places():
    assert str(dcs.convert_amount(Decimal("1"), Decimal("2"))) == "2.00"


# ---------------------------------------------------------------------------
# 3. ROUND_HALF_UP
# ---------------------------------------------------------------------------


def test_rounding_is_half_up_not_half_even():
    # 0.10 * 1.05 = 0.1050 exactly. HALF_UP -> 0.11; HALF_EVEN -> 0.10.
    assert dcs.convert_amount(Decimal("0.10"), Decimal("1.05")) == Decimal("0.11")


def test_rounding_half_up_on_a_second_tie():
    # 1 * 1.005 = 1.005. HALF_UP -> 1.01; HALF_EVEN -> 1.00.
    assert dcs.convert_amount(Decimal("1"), Decimal("1.005")) == Decimal("1.01")


def test_rounding_down_below_the_tie():
    assert dcs.convert_amount(Decimal("1"), Decimal("1.00499")) == Decimal("1.00")


# ---------------------------------------------------------------------------
# 4-5. Invalid rates and currencies
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_rate", ["0", "-1", "-0.01", "0.0"])
def test_zero_or_negative_rate_is_rejected(bad_rate):
    with pytest.raises(DiscoveryRateConfigError):
        dcs.convert_amount(Decimal("10"), Decimal(bad_rate))


@pytest.mark.parametrize("bad_rate", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_rate_is_rejected(bad_rate):
    with pytest.raises(DiscoveryRateConfigError):
        dcs.convert_amount(Decimal("10"), Decimal(bad_rate))


@pytest.mark.parametrize("bad_rate", ["abc", "", None, "9,1.25"])
def test_non_numeric_rate_is_rejected(bad_rate):
    with pytest.raises(DiscoveryRateConfigError):
        dcs.convert_amount(Decimal("10"), bad_rate)


@pytest.mark.parametrize("bad_env", ["0", "-5", "not-a-number", "abc"])
def test_invalid_configured_rate_is_rejected(monkeypatch, bad_env):
    monkeypatch.setenv(drp.RATE_ENV_VAR, bad_env)

    with pytest.raises(DiscoveryRateConfigError):
        get_rate_provider().get_rate("USD", "INR")


@pytest.mark.parametrize("bad_code", ["US", "USDD", "", "1SD", None, 123, "US$"])
def test_invalid_currency_code_is_rejected(bad_code):
    with pytest.raises(InvalidCurrencyError):
        get_rate_provider().get_rate(bad_code, "INR")


def test_invalid_target_currency_is_rejected():
    with pytest.raises(InvalidCurrencyError):
        get_rate_provider().get_rate("USD", "RUPEES")


@pytest.mark.parametrize("bad_amount", ["-1", "-0.01", "NaN", "Infinity"])
def test_invalid_amount_is_rejected(bad_amount):
    with pytest.raises(dcs.DiscoveryConversionError):
        dcs.convert_amount(Decimal(bad_amount), Decimal("91.25"))


def test_non_numeric_amount_is_rejected():
    with pytest.raises(dcs.DiscoveryConversionError):
        dcs.convert_amount("not-money", Decimal("91.25"))


def test_zero_amount_converts_to_zero_rather_than_erroring():
    assert dcs.convert_amount(Decimal("0"), Decimal("91.25")) == Decimal("0.00")


# ---------------------------------------------------------------------------
# 6. Supported / unsupported pairs
# ---------------------------------------------------------------------------


def test_identity_pair_is_exactly_one_and_marked_as_identity():
    rate = get_rate_provider().get_rate("INR", "INR")

    assert rate.rate == Decimal(1)
    assert rate.source == SOURCE_IDENTITY


@pytest.mark.parametrize(
    ("source_code", "target"),
    [("EUR", "INR"), ("GBP", "INR"), ("JPY", "INR"), ("INR", "USD"), ("EUR", "USD")],
)
def test_unsupported_pair_raises_rather_than_guessing(source_code, target):
    with pytest.raises(UnsupportedCurrencyPairError):
        get_rate_provider().get_rate(source_code, target)


def test_inr_to_usd_is_not_supported_here():
    # That direction is PayPal's demo conversion, which lives in the frozen
    # payment core and is deliberately not reachable from discovery.
    with pytest.raises(UnsupportedCurrencyPairError):
        get_rate_provider().get_rate("INR", "USD")


# ---------------------------------------------------------------------------
# 7. Rate provider failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        UnsupportedCurrencyPairError("no pair"),
        DiscoveryRateUnavailableError("source down"),
        DiscoveryRateConfigError("bad config"),
        InvalidCurrencyError("bad code"),
    ],
)
def test_rate_provider_failure_leaves_derived_fields_null(error):
    fake = FakeRateProvider(error=error)

    normalized = dcs.normalize_product(product(price="34.99", currency="USD"), "INR", fake)

    assert normalized.normalized_price is None
    assert normalized.normalized_currency is None
    assert normalized.conversion_rate is None
    assert normalized.conversion_source is None
    # The observation survives the failure intact.
    assert normalized.observed_price == Decimal("34.99")
    assert normalized.observed_currency == "USD"


def test_rate_provider_failure_does_not_raise_out_of_normalization():
    fake = FakeRateProvider(error=DiscoveryRateUnavailableError("down"))

    # One bad candidate must never fail a whole discovery request.
    results = dcs.normalize_products(
        [product("a", price="1", currency="USD"), product("b")], "INR", fake
    )

    assert len(results) == 2
    assert all(r.normalized_price is None for r in results)


def test_a_bad_rate_from_a_provider_is_not_silently_accepted():
    fake = FakeRateProvider(rate="0")

    normalized = dcs.normalize_product(product(price="10", currency="USD"), "INR", fake)

    assert normalized.normalized_price is None


# ---------------------------------------------------------------------------
# 8. Separation from the PayPal payment core
# ---------------------------------------------------------------------------


def test_discovery_rate_is_not_the_paypal_demo_rate():
    assert DEFAULT_DEMO_USD_INR_RATE != DEMO_INR_PER_USD
    assert get_rate_provider().get_rate("USD", "INR").rate != DEMO_INR_PER_USD


def test_paypal_demo_rate_is_unchanged():
    # The frozen payment core's constant, asserted here as a tripwire.
    assert DEMO_INR_PER_USD == Decimal("90")


def test_discovery_currency_modules_do_not_import_the_paypal_converter():
    for module in (dcs, drp):
        names = vars(module)
        assert "DEMO_INR_PER_USD" not in names
        assert "convert_inr_to_usd" not in names
        assert "currency_service" not in names


def test_paypal_converter_is_never_called_during_normalization(monkeypatch):
    from app.services import currency_service

    def bomb(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("discovery must not use the PayPal converter")

    monkeypatch.setattr(currency_service, "convert_inr_to_usd", bomb)

    normalized = dcs.normalize_product(product(price="34.99", currency="USD"), "INR")

    assert normalized.normalized_price == Decimal("3192.84")


def test_the_rate_env_var_is_discovery_specific():
    assert drp.RATE_ENV_VAR == "DISCOVERY_USD_INR_RATE"
    assert "PAYPAL" not in drp.RATE_ENV_VAR
    assert "DEMO_INR_PER_USD" not in drp.RATE_ENV_VAR


# ---------------------------------------------------------------------------
# 9-13. Normalization happy path
# ---------------------------------------------------------------------------


def test_usd_observed_price_gets_a_normalized_inr_price():
    normalized = dcs.normalize_product(product(price="34.99", currency="USD"), "INR")

    assert normalized.normalized_price == Decimal("3192.84")
    assert normalized.normalized_currency == "INR"


def test_observed_price_remains_unchanged_after_normalization():
    original = product(price="34.99", currency="USD")
    snapshot = original.model_dump()

    normalized = dcs.normalize_product(original, "INR")

    # The derived value never overwrites the observation...
    assert normalized.observed_price == Decimal("34.99")
    assert normalized.observed_currency == "USD"
    assert normalized.observed_price != normalized.normalized_price
    # ...and the input object is not mutated at all.
    assert original.model_dump() == snapshot


def test_normalization_returns_a_new_object():
    original = product(price="10", currency="USD")

    assert dcs.normalize_product(original, "INR") is not original


def test_conversion_rate_is_exposed_as_a_decimal():
    normalized = dcs.normalize_product(product(price="10", currency="USD"), "INR")

    assert normalized.conversion_rate == DEFAULT_DEMO_USD_INR_RATE
    assert isinstance(normalized.conversion_rate, Decimal)


def test_conversion_source_is_exposed_and_marked_as_configured():
    normalized = dcs.normalize_product(product(price="10", currency="USD"), "INR")

    assert normalized.conversion_source == SOURCE_CONFIGURED_DEMO
    # Never described as a live or current market rate.
    assert "live" not in normalized.conversion_source


def test_configured_rate_carries_no_observation_time():
    # A constant from configuration was never observed at a point in time;
    # stamping it would imply a freshness it does not have.
    normalized = dcs.normalize_product(product(price="10", currency="USD"), "INR")

    assert normalized.rate_observed_at is None


def test_a_live_rate_source_can_report_its_observation_time():
    fake = FakeRateProvider(rate="90.5", observed_at=OBSERVED_AT)

    normalized = dcs.normalize_product(product(price="10", currency="USD"), "INR", fake)

    assert normalized.rate_observed_at == OBSERVED_AT
    assert normalized.conversion_source == "fake_rate_source"


def test_same_currency_normalizes_at_the_identity_rate():
    normalized = dcs.normalize_product(product(price="2500", currency="INR"), "INR")

    assert normalized.normalized_price == Decimal("2500.00")
    assert normalized.normalized_currency == "INR"
    assert normalized.conversion_rate == Decimal(1)
    assert normalized.conversion_source == SOURCE_IDENTITY


def test_normalization_target_can_be_something_other_than_inr():
    normalized = dcs.normalize_product(product(price="25", currency="USD"), "USD")

    assert normalized.normalized_price == Decimal("25.00")
    assert normalized.normalized_currency == "USD"


# ---------------------------------------------------------------------------
# 14-15. Unsupported currency and missing price
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("currency", ["EUR", "GBP", "JPY", "AUD"])
def test_unsupported_currency_produces_null_derived_fields(currency):
    normalized = dcs.normalize_product(product(price="30", currency=currency), "INR")

    assert normalized.normalized_price is None
    assert normalized.normalized_currency is None
    assert normalized.conversion_rate is None
    assert normalized.conversion_source is None
    # Observed data is still fully available.
    assert normalized.observed_price == Decimal("30")
    assert normalized.observed_currency == currency


def test_missing_observed_price_stays_null():
    normalized = dcs.normalize_product(product(), "INR")

    assert normalized.observed_price is None
    assert normalized.normalized_price is None
    assert normalized.normalized_currency is None
    assert normalized.conversion_rate is None


def test_price_without_a_currency_stays_null():
    normalized = dcs.normalize_product(product(price="10", currency=None), "INR")

    assert normalized.observed_price == Decimal("10")
    assert normalized.normalized_price is None


def test_derived_fields_are_all_null_or_all_present():
    for candidate in (
        product("none"),
        product("eur", price="10", currency="EUR"),
        product("usd", price="10", currency="USD"),
    ):
        normalized = dcs.normalize_product(candidate, "INR")
        derived = [
            normalized.normalized_price,
            normalized.normalized_currency,
            normalized.conversion_rate,
            normalized.conversion_source,
        ]
        assert all(v is None for v in derived) or all(v is not None for v in derived)


def test_a_batch_mixes_convertible_and_unconvertible_candidates_safely():
    results = dcs.normalize_products(
        [
            product("usd", price="34.99", currency="USD"),
            product("eur", price="29.99", currency="EUR"),
            product("inr", price="2500", currency="INR"),
            product("nothing"),
        ],
        "INR",
    )

    by_id = {r.external_id: r for r in results}
    assert by_id["usd"].normalized_price == Decimal("3192.84")
    assert by_id["eur"].normalized_price is None
    assert by_id["inr"].normalized_price == Decimal("2500.00")
    assert by_id["nothing"].normalized_price is None
    # Every observed price is preserved regardless.
    assert by_id["eur"].observed_price == Decimal("29.99")


def test_a_caller_cannot_inject_a_derived_price():
    # Only this module may set derived fields; a pre-populated input is
    # stripped and recomputed.
    forged = NormalizedDiscoveredProduct(
        external_id="forged",
        source="serper",
        title="Forged",
        observed_price=Decimal("34.99"),
        observed_currency="USD",
        observed_at=OBSERVED_AT,
        normalized_price=Decimal("1.00"),
        normalized_currency="INR",
        conversion_rate=Decimal("0.0001"),
        conversion_source="attacker_supplied",
    )

    normalized = dcs.normalize_product(forged, "INR")

    assert normalized.normalized_price == Decimal("3192.84")
    assert normalized.conversion_rate == DEFAULT_DEMO_USD_INR_RATE
    assert normalized.conversion_source == SOURCE_CONFIGURED_DEMO


def test_forged_derived_fields_are_dropped_when_unconvertible():
    forged = NormalizedDiscoveredProduct(
        external_id="forged",
        source="serper",
        title="Forged",
        observed_price=Decimal("29.99"),
        observed_currency="EUR",
        observed_at=OBSERVED_AT,
        normalized_price=Decimal("1.00"),
        normalized_currency="INR",
        conversion_rate=Decimal("0.0001"),
        conversion_source="attacker_supplied",
    )

    normalized = dcs.normalize_product(forged, "INR")

    assert normalized.normalized_price is None
    assert normalized.conversion_source is None


# ---------------------------------------------------------------------------
# comparable_amount
# ---------------------------------------------------------------------------


def test_comparable_amount_prefers_an_exact_observation():
    candidate = dcs.normalize_product(product(price="2500", currency="INR"), "INR")

    assert dcs.comparable_amount(candidate, "INR") == Decimal("2500")


def test_comparable_amount_uses_the_derived_value_across_currencies():
    candidate = dcs.normalize_product(product(price="34.99", currency="USD"), "INR")

    assert dcs.comparable_amount(candidate, "INR") == Decimal("3192.84")


def test_comparable_amount_is_none_for_an_unconvertible_currency():
    candidate = dcs.normalize_product(product(price="29.99", currency="EUR"), "INR")

    assert dcs.comparable_amount(candidate, "INR") is None


def test_comparable_amount_is_none_without_a_price():
    assert dcs.comparable_amount(dcs.normalize_product(product(), "INR"), "INR") is None


def test_comparable_amount_works_on_a_plain_discovered_product():
    # A record with no derived fields at all must not raise.
    plain = product(price="34.99", currency="USD")

    assert dcs.comparable_amount(plain, "USD") == Decimal("34.99")
    assert dcs.comparable_amount(plain, "INR") is None


def test_comparable_amount_without_a_target_returns_the_observation():
    plain = product(price="34.99", currency="USD")

    assert dcs.comparable_amount(plain, None) == Decimal("34.99")


# ---------------------------------------------------------------------------
# describe_conversion — deterministic, backend-assembled prose
# ---------------------------------------------------------------------------


def test_describe_conversion_states_the_derivation_and_its_provenance():
    candidate = dcs.normalize_product(product(price="34.99", currency="USD"), "INR")

    note = dcs.describe_conversion(candidate)

    assert "USD 34.99" in note
    assert "3,192.84" in note
    assert "derived" in note
    assert "configured demonstration rate" in note
    assert "91.25" in note
    # Never presented as a live/current market quote.
    assert "live" not in note.lower()


def test_describe_conversion_is_none_without_a_derived_price():
    candidate = dcs.normalize_product(product(price="29.99", currency="EUR"), "INR")

    assert dcs.describe_conversion(candidate) is None


def test_describe_conversion_is_none_for_the_identity_rate():
    # No arithmetic happened, so there is nothing to disclose.
    candidate = dcs.normalize_product(product(price="2500", currency="INR"), "INR")

    assert dcs.describe_conversion(candidate) is None


def test_describe_conversion_names_a_non_demo_source_honestly():
    fake = FakeRateProvider(rate="90.5", observed_at=OBSERVED_AT)
    candidate = dcs.normalize_product(product(price="10", currency="USD"), "INR", fake)

    note = dcs.describe_conversion(candidate)

    assert "fake_rate_source" in note
    assert "configured demonstration rate" not in note


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------


def test_the_configured_provider_satisfies_the_interface():
    provider = get_rate_provider()

    assert isinstance(provider, DiscoveryExchangeRateProvider)
    assert isinstance(provider, ConfiguredDemoRateProvider)
    assert provider.source == SOURCE_CONFIGURED_DEMO


def test_the_rate_provider_can_be_replaced_without_touching_filtering():
    # The extension point: normalization accepts any provider, and nothing
    # in filtering/ranking knows where a rate came from.
    fake = FakeRateProvider(rate="100")

    normalized = dcs.normalize_product(product(price="10", currency="USD"), "INR", fake)

    assert normalized.normalized_price == Decimal("1000.00")
    assert fake.calls == [("USD", "INR")]


def test_exchange_rate_value_object_is_immutable():
    rate = get_rate_provider().get_rate("USD", "INR")

    with pytest.raises(Exception):
        rate.rate = Decimal("1")


def test_no_ai_provider_is_reachable_from_the_rate_layer():
    for module in (dcs, drp):
        names = vars(module)
        for forbidden in ("get_provider", "AIProvider", "rank_products"):
            assert forbidden not in names
