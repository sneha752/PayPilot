"""Normalized product-discovery schemas (Phase 10C).

A DiscoveredProduct is PayPilot's provider-neutral view of ONE externally
discovered product offer. It is discovery EVIDENCE, nothing more:

  - observed_price / observed_currency are what a retailer was seen asking
    at observed_at — never an authoritative payment price, PayPal amount,
    catalog price, or Policy Engine input.
  - There are deliberately NO payment fields here: no PayPal ids, no
    transaction ids, no policy decisions, no authorization state, and no
    link to the local catalog Product model. Discovery stays inert until a
    later phase designs an explicit, server-controlled adoption step.

Prices are Decimal end to end; serialization renders them as exact strings.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class DiscoveredProduct(BaseModel):
    """One normalized external product with its representative offer.

    When the provider returns multiple offers for a product, the
    normalization layer selects ONE representative offer deterministically
    (see discovery_service._select_offer); the retailer/price fields here
    describe that selected offer only. "Lowest observed price" means lowest
    among the offers that provider returned — never a claim about the whole
    internet.
    """

    external_id: str = Field(description="The provider's product id.")
    source: str = Field(description="Discovery provider, e.g. 'channel3'.")
    title: str
    description: Optional[str] = None
    brand: Optional[str] = None
    category: Optional[str] = None
    image_url: Optional[str] = None

    # Representative offer (null when no valid priced offer existed).
    product_url: Optional[str] = None
    retailer_domain: Optional[str] = None
    availability: Optional[str] = None
    observed_price: Optional[Decimal] = Field(
        default=None,
        description="OBSERVED retailer price — discovery data only, never "
        "a payment amount.",
    )
    observed_currency: Optional[str] = None

    # Server-generated (UTC, timezone-aware) at normalization time; external
    # timestamps are not trusted.
    observed_at: datetime


class MerchantEvidence(BaseModel):
    """Provider-reported MERCHANT DISPLAY NAME for one discovery result.

    This exists because a merchant display name is provider evidence that
    the 13-field DiscoveredProduct contract has no home for, and that
    contract is deliberately frozen (GET /discovery/search is asserted to
    return exactly those 13 keys). Rather than widen or reinterpret it,
    providers emit this alongside their products on ProviderSearchResult,
    bound to a product by `external_id`.

    It carries nothing but a name. A merchant DOMAIN is never transported
    here, because it is derivable from evidence the product already has
    (retailer_domain / product_url) — see discovery_merchant_service.

    `merchant_name` is verbatim provider evidence: whatever the provider
    called the seller, trimmed only of surrounding whitespace. It is never
    assembled from a domain, a brand, or a title, and it never participates
    in payment authority.
    """

    external_id: str = Field(
        description="The external_id of the DiscoveredProduct this names."
    )
    merchant_name: Optional[str] = Field(
        default=None,
        description="Merchant display name as the provider reported it, e.g. "
        "'Amazon'. Null when the provider named no merchant.",
    )


class NormalizedDiscoveredProduct(DiscoveredProduct):
    """A DiscoveredProduct plus PayPilot-DERIVED fields (Phase 10F/10H).

    Two groups of derived data live here: price (Phase 10F) and merchant
    identity (Phase 10H). Both follow the same rule — a derivation is
    either complete and honest, or null. Neither may become a PayPal,
    capture, or refund amount, and neither carries payment authority.

    The derived fields are deliberately a SUBCLASS rather than additions to
    DiscoveredProduct, for two reasons:

      1. GET /discovery/search keeps its exact Phase 10C/10D contract — pure
         provider evidence, no PayPilot-computed values mixed in.
      2. The observed/derived boundary stays visible in the type system: a
         DiscoveredProduct is only what a provider reported; derived money
         only exists on this richer type.

    OBSERVED vs DERIVED
        observed_price / observed_currency are inherited UNCHANGED — they
        remain exactly what the retailer was seen asking. They are never
        overwritten, re-denominated, or replaced by a converted value.

        normalized_* is arithmetic PayPilot performed on that observation
        using conversion_rate. It is NOT a price any retailer displayed, and
        a client must not present it as one.

    All four derived fields are null together: a candidate either has a
    complete, honest derivation or none at all. They are null when there is
    no observed price, no observed currency, or no deterministic rate for
    the pair — never filled by a guess, an estimate, or an LLM.

    Nothing here may become a PayPal order, capture, or refund amount.
    """

    normalized_price: Optional[Decimal] = Field(
        default=None,
        description="DERIVED price in normalized_currency, computed by "
        "PayPilot as observed_price * conversion_rate. Not a retailer price.",
    )
    normalized_currency: Optional[str] = Field(
        default=None, description="Currency normalized_price is expressed in."
    )
    conversion_rate: Optional[Decimal] = Field(
        default=None,
        description="Exact Decimal multiplier applied to observed_price.",
    )
    conversion_source: Optional[str] = Field(
        default=None,
        description="Where the rate came from, e.g. 'configured_demo_rate' "
        "(a demonstration constant, NOT a live market quote) or 'identity' "
        "(observed currency already equals the target).",
    )
    rate_observed_at: Optional[datetime] = Field(
        default=None,
        description="When the rate was obtained from a live source. Null for "
        "a configured rate, which was never observed at a point in time.",
    )

    # --- Derived merchant identity (Phase 10H) -----------------------------
    # Independent of the price fields above: a candidate can carry merchant
    # identity with no derived price, and vice versa.

    merchant_name: Optional[str] = Field(
        default=None,
        description="Merchant display name carried over from the provider's "
        "MerchantEvidence, e.g. 'Amazon'. Null when the provider named no "
        "merchant. NEVER constructed from merchant_domain, brand, or title.",
    )
    merchant_domain: Optional[str] = Field(
        default=None,
        description="DERIVED merchant domain, e.g. 'bestbuy.com'. Normalized "
        "from retailer_domain / product_url and null whenever that host is a "
        "search aggregator or redirector (a Google Shopping link does not "
        "make Google the merchant). NEVER inferred from merchant_name.",
    )

    @property
    def has_derived_price(self) -> bool:
        return self.normalized_price is not None and self.normalized_currency is not None


class DiscoverySearchResponse(BaseModel):
    """Response body for GET /discovery/search."""

    query: str
    products: list[DiscoveredProduct] = Field(default_factory=list)
    next_page_token: Optional[str] = None
