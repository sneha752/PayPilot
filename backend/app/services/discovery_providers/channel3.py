"""Channel3 discovery provider adapter (Phase 10D).

Wraps the FROZEN Phase 10B Channel3Client behind the provider-neutral
DiscoveryProvider interface. The client keeps owning authentication,
timeouts, retries and Channel3 response validation; this adapter ONLY
translates validated Channel3 models into DiscoveredProduct, using the
exact deterministic normalization rules introduced in Phase 10C
(representative-offer selection, brand/image/category picks).

MERCHANT IDENTITY (Phase 10H)
    Behavior here is UNCHANGED. A Channel3Offer carries url, domain, price,
    availability and condition - there is no store/merchant display-name
    field anywhere in the verified /v1/search contract - so this adapter
    reports NO MerchantEvidence. merchant_name stays null rather than being
    filled from the offer domain, the brand, or the title, any of which
    would be invention.

    Channel3 does, however, link straight to the retailer, so the offer's
    own domain is the real merchant host. That needs nothing new: it is
    already in retailer_domain, and discovery_merchant_service derives
    merchant_domain from it (the aggregator rule that nulls Google Shopping
    hosts never fires for a Channel3 offer).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from ...schemas.discovery import DiscoveredProduct
from ..channel3_client import (
    Channel3Client,
    Channel3Offer,
    Channel3Product,
)
from .base import DiscoveryProvider, ProviderSearchResult

logger = logging.getLogger("paypilot.discovery")

SOURCE_CHANNEL3 = "channel3"


def _availability_rank(offer: Channel3Offer) -> int:
    """Preference order: explicitly in stock < unknown/other < out of stock."""
    if offer.availability == "InStock":
        return 0
    if offer.availability == "OutOfStock":
        return 2
    return 1


def _select_offer(offers: list[Channel3Offer]) -> Optional[Channel3Offer]:
    """Pick ONE representative offer, deterministically.

    Rule (documented, no AI):
      1. Only offers with a non-blank URL and a finite price > 0 qualify.
      2. Prefer explicitly available offers (InStock) over unknown, and
         unknown over explicit OutOfStock.
      3. Within the same availability rank, choose the LOWEST observed
         price — lowest among what the provider returned, not a claim
         about the whole market.
      4. Ties break by retailer domain, then URL (lexicographic).
      5. No qualifying offer -> None (the product is still returned, with
         null price/retailer fields — nothing is invented).
    """
    candidates = []
    for offer in offers:
        if not offer.url or not offer.url.strip():
            continue  # malformed: no merchant link
        if offer.price is None:
            continue
        price = offer.price.price
        if not price.is_finite() or price <= 0:
            continue  # malformed/invalid price
        candidates.append(offer)

    if not candidates:
        return None

    return min(
        candidates,
        key=lambda o: (
            _availability_rank(o),
            o.price.price,
            o.domain or "",
            o.url,
        ),
    )


def _normalize_brand(product: Channel3Product) -> Optional[str]:
    """First non-empty brand name, whitespace-trimmed. No canonicalization."""
    for brand in product.brands:
        if brand.name and brand.name.strip():
            return brand.name.strip()
    return None


def _normalize_image(product: Channel3Product) -> Optional[str]:
    """Main image if flagged, else the first image with a usable URL."""
    main = None
    first = None
    for image in product.images:
        if not image.url or not image.url.strip():
            continue
        if first is None:
            first = image.url
        if image.is_main_image and main is None:
            main = image.url
    return main or first


def _normalize_category(product: Channel3Product) -> Optional[str]:
    """Category slug preferred (stable identifier), else its title."""
    if product.category is None:
        return None
    if product.category.slug and product.category.slug.strip():
        return product.category.slug.strip()
    if product.category.title and product.category.title.strip():
        return product.category.title.strip()
    return None


def normalize_product(
    product: Channel3Product, observed_at: datetime
) -> DiscoveredProduct:
    """Convert one Channel3 product into the provider-neutral model.

    Decimal prices are carried over EXACTLY (no float round-trip, no
    conversion); currency is preserved verbatim, whatever it is.
    """
    offer = _select_offer(product.offers)

    return DiscoveredProduct(
        external_id=product.id,
        source=SOURCE_CHANNEL3,
        title=product.title,
        description=product.description,
        brand=_normalize_brand(product),
        category=_normalize_category(product),
        image_url=_normalize_image(product),
        product_url=offer.url if offer else None,
        retailer_domain=offer.domain if offer else None,
        availability=offer.availability if offer else None,
        observed_price=offer.price.price if offer else None,
        observed_currency=offer.price.currency if offer else None,
        observed_at=observed_at,
    )


class Channel3Provider(DiscoveryProvider):
    """DiscoveryProvider adapter over the existing Channel3Client."""

    source = SOURCE_CHANNEL3

    def __init__(self, client: Optional[Channel3Client] = None) -> None:
        # Channel3ConfigError (a DiscoveryConfigError) propagates if the
        # key is missing — no translation needed.
        self._client = client if client is not None else Channel3Client()

    def search(
        self,
        query: str,
        *,
        limit: int,
        page_token: Optional[str] = None,
        observed_at: datetime,
    ) -> ProviderSearchResult:
        # Channel3 exceptions are Discovery* subclasses: they propagate
        # unchanged and remain provider-neutral to every caller.
        result = self._client.search(query, limit=limit, page_token=page_token)
        return ProviderSearchResult(
            products=[normalize_product(p, observed_at) for p in result.products],
            next_page_token=result.next_page_token,
            # No merchant display name exists in the Channel3 contract (see
            # the module docstring), so none is reported. Left to default
            # rather than filled with placeholders.
        )
