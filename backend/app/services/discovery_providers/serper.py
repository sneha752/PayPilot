"""Serper (Google Shopping) discovery provider (Phase 10D).

Contract verified against official Serper material (serper.dev):

    POST https://google.serper.dev/shopping
    headers: X-API-KEY: <key>, JSON body
    body:    {"q": "...", "gl": "us", "hl": "en", "num": <n>}
    result:  {"shopping": [{"title", "source", "link", "price",
              "imageUrl", "productId", "position", ...}]}

This provider is discovery infrastructure only: observed prices stay
Decimal discovery evidence (never payment amounts), nothing is invented
for missing fields, and an upstream failure raises a Discovery* error —
never a fake empty success.

Deterministic choices, documented:
  - external_id: "serper:<productId>" when Serper supplies productId,
    else "serper:url:<sha256(link)[:16]>" (content-derived, stable for
    the same upstream result; never random, time-based, or LLM-made).
    A result with neither productId nor link is skipped.
  - price: Serper returns a DISPLAY string (e.g. "$1,299.99"). Because
    every request pins gl=us / hl=en, a "$NNN[.NN]"-shaped value is
    parsed as Decimal with currency "USD". Any other shape yields
    null price/currency — nothing is guessed.
  - retailer_domain: the hostname of the result's link; `source` (a
    retailer display name) is not a domain and is not used for it.
  - merchant name (Phase 10H): Serper's `source` IS the seller's display
    name ("Amazon", "Best Buy"), while `link` is a Google Shopping URL, so
    the link's host is google.com. The name is therefore reported as
    MerchantEvidence rather than discarded, and it is never converted into
    a domain - "Amazon" does not license writing amazon.in. Our own
    provenance value stays `source="serper"` on every product, so
    discovery_source in the comparison response keeps naming the provider,
    not a shop.
  - brand/description/category/availability: Serper's shopping results
    do not carry them, so they are null — never inferred from titles.
  - pagination: Serper uses numeric pages, not tokens. A supplied
    page_token is rejected (ValueError) and next_page_token is always
    null; faking token state would be invention.
  - no retries: one attempt per request (the API is fast and cheap to
    re-issue); transient failures surface as retryable Discovery errors.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv

from ...schemas.discovery import DiscoveredProduct, MerchantEvidence
from .base import (
    DiscoveryAuthError,
    DiscoveryConfigError,
    DiscoveryError,
    DiscoveryInvalidResponseError,
    DiscoveryProvider,
    DiscoveryQuotaError,
    DiscoveryRateLimitError,
    DiscoveryRequestError,
    DiscoveryUnavailableError,
    ProviderSearchResult,
)

load_dotenv()

logger = logging.getLogger("paypilot.discovery.serper")

SOURCE_SERPER = "serper"

_SHOPPING_URL = "https://google.serper.dev/shopping"
_DEFAULT_TIMEOUT_SECONDS = 15.0

# "$1,299.99", "$5", "$12.5" — pinned to gl=us, so "$" means USD here.
_USD_PRICE_RE = re.compile(r"^\$\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)$")


def _parse_price(raw: Any) -> tuple[Optional[Decimal], Optional[str]]:
    """Parse Serper's display price deterministically, or return nulls."""
    if not isinstance(raw, str):
        return None, None
    match = _USD_PRICE_RE.match(raw.strip())
    if not match:
        return None, None
    try:
        value = Decimal(match.group(1).replace(",", ""))
    except InvalidOperation:  # pragma: no cover - regex precludes this
        return None, None
    if not value.is_finite() or value <= 0:
        return None, None
    return value, "USD"


def _domain_of(link: str) -> Optional[str]:
    try:
        netloc = urlparse(link).netloc
    except ValueError:
        return None
    return netloc or None


def _merchant_name(item: dict) -> Optional[str]:
    """Serper's `source` - the seller's display name - trimmed, or None.

    Carried verbatim. It is not normalized against a retailer list, not
    turned into a domain, and not substituted when absent.
    """
    raw = item.get("source")
    if not isinstance(raw, str):
        return None
    return raw.strip() or None


def _external_id(item: dict) -> Optional[str]:
    """Stable provider-scoped id (see module docstring)."""
    product_id = item.get("productId")
    if isinstance(product_id, (str, int)) and str(product_id).strip():
        return f"serper:{str(product_id).strip()}"
    link = item.get("link")
    if isinstance(link, str) and link.strip():
        digest = hashlib.sha256(link.strip().encode("utf-8")).hexdigest()[:16]
        return f"serper:url:{digest}"
    return None


class SerperProvider(DiscoveryProvider):
    """Google Shopping results via Serper, normalized."""

    source = SOURCE_SERPER

    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ) -> None:
        key = (api_key or os.getenv("SERPER_API_KEY", "")).strip()
        if not key:
            # Fail clearly BEFORE any network request; name the variable,
            # never a value.
            raise DiscoveryConfigError(
                "SERPER_API_KEY must be set in the environment (.env) "
                "to use the serper discovery provider"
            )
        self._api_key = key

        if timeout_seconds is None:
            raw = os.getenv("SERPER_TIMEOUT_SECONDS", "").strip()
            try:
                timeout_seconds = float(raw) if raw else _DEFAULT_TIMEOUT_SECONDS
            except ValueError:
                timeout_seconds = _DEFAULT_TIMEOUT_SECONDS
        self._timeout = timeout_seconds

    def search(
        self,
        query: str,
        *,
        limit: int,
        page_token: Optional[str] = None,
        observed_at: datetime,
    ) -> ProviderSearchResult:
        if page_token:
            # Serper pages are numeric, not tokens; faking compatibility
            # would invent pagination state.
            raise ValueError(
                "page_token is not supported by the serper discovery provider"
            )

        logger.info(
            "Serper shopping search initiated (query=%r, limit=%d)",
            query[:60],
            limit,
        )

        try:
            response = httpx.post(
                _SHOPPING_URL,
                json={"q": query, "gl": "us", "hl": "en", "num": limit},
                headers={
                    # The key travels in this header ONLY — never in the
                    # URL, logs, or error messages.
                    "X-API-KEY": self._api_key,
                    "Accept": "application/json",
                },
                timeout=self._timeout,
            )
        except httpx.RequestError as exc:
            logger.warning(
                "Serper network error (%s)", type(exc).__name__
            )
            raise DiscoveryUnavailableError(
                f"Could not reach Serper (network error: {type(exc).__name__})"
            ) from exc

        status = response.status_code
        if status in (401, 403):
            logger.error("Serper authentication failed: HTTP %s", status)
            raise DiscoveryAuthError(f"Serper rejected the API key (HTTP {status})")
        if status == 402:
            logger.error("Serper credits exhausted: HTTP 402")
            raise DiscoveryQuotaError("Serper API credits are exhausted (HTTP 402)")
        if status == 429:
            logger.warning("Serper rate limited: HTTP 429")
            raise DiscoveryRateLimitError("Serper rate limit exceeded (HTTP 429)")
        if status in (400, 404, 422):
            logger.error("Serper rejected the request: HTTP %s", status)
            raise DiscoveryRequestError(
                f"Serper rejected the search request (HTTP {status})"
            )
        if 500 <= status <= 599:
            logger.error("Serper upstream failure: HTTP %s", status)
            raise DiscoveryUnavailableError(f"Serper unavailable (HTTP {status})")
        if status not in (200, 201):
            logger.error("Serper unexpected response: HTTP %s", status)
            raise DiscoveryError(f"Unexpected Serper response (HTTP {status})")

        return self._parse_response(response, limit, observed_at)

    # -- Validation ----------------------------------------------------------

    def _parse_response(
        self, response: httpx.Response, limit: int, observed_at: datetime
    ) -> ProviderSearchResult:
        try:
            body = response.json()
        except ValueError as exc:
            logger.error("Serper returned a non-JSON body")
            raise DiscoveryInvalidResponseError(
                "Serper response body is not valid JSON"
            ) from exc

        if not isinstance(body, dict):
            logger.error("Serper response is not an object")
            raise DiscoveryInvalidResponseError(
                "Serper response does not match the shopping response schema"
            )

        shopping = body.get("shopping", [])
        if not isinstance(shopping, list):
            logger.error("Serper 'shopping' field is not a list")
            raise DiscoveryInvalidResponseError(
                "Serper response does not match the shopping response schema"
            )

        products: list[DiscoveredProduct] = []
        merchants: list[MerchantEvidence] = []
        skipped = 0
        for item in shopping[:limit]:
            if not isinstance(item, dict):
                skipped += 1
                continue
            title = item.get("title")
            external_id = _external_id(item)
            if not (isinstance(title, str) and title.strip()) or not external_id:
                skipped += 1  # unusable: no title or no stable identity
                continue

            link = item.get("link")
            link = link if isinstance(link, str) and link.strip() else None
            image = item.get("imageUrl")
            image = image if isinstance(image, str) and image.strip() else None
            price, currency = _parse_price(item.get("price"))

            products.append(
                DiscoveredProduct(
                    external_id=external_id,
                    source=SOURCE_SERPER,
                    title=title.strip(),
                    description=None,  # not provided by Serper shopping
                    brand=None,  # not provided; never inferred from title
                    category=None,
                    image_url=image,
                    product_url=link,
                    retailer_domain=_domain_of(link) if link else None,
                    availability=None,  # not provided
                    observed_price=price,
                    observed_currency=currency,
                    observed_at=observed_at,
                )
            )

            # Phase 10H: the seller's display name, which has no field in
            # the frozen 13-field evidence contract, travels alongside.
            # Omitted entirely when Serper named no source, so "absent"
            # stays distinguishable from "named".
            merchant_name = _merchant_name(item)
            if merchant_name is not None:
                merchants.append(
                    MerchantEvidence(
                        external_id=external_id, merchant_name=merchant_name
                    )
                )

        if skipped:
            logger.warning(
                "Serper search: skipped %d unusable result(s)", skipped
            )

        logger.info(
            "Serper search ok: HTTP %s, %d product(s)",
            response.status_code,
            len(products),
        )
        # Serper has no token pagination for this endpoint (documented).
        return ProviderSearchResult(
            products=products,
            next_page_token=None,
            merchant_evidence=merchants,
        )
