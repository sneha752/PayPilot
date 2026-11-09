"""Channel3 product-discovery client (Phase 10B).

A small, deterministic HTTP client for Channel3's product search API
(POST https://api.trychannel3.com/v1/search). This module is PURELY a
commerce-data client: it knows nothing about PayPal, the Policy Engine,
sessions, purchase execution, chat, or the frontend, and it never calls
an AI provider.

PRICE AUTHORITY (critical): every price in a Channel3 response is an
OBSERVED retailer price — discovery data only. It is preserved verbatim
(as Decimal) and is never converted, never fed to the currency service,
never passed to the Policy Engine, and never written into the catalog or
the transaction ledger. Nothing in this module can create a payment.

The API key is read from CHANNEL3_API_KEY and is never logged, never
embedded in exception messages, and never returned to a caller.
"""

from __future__ import annotations

import logging
import os
import time
from decimal import Decimal
from typing import Any, Optional

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

from .discovery_providers.base import (
    DiscoveryAuthError,
    DiscoveryConfigError,
    DiscoveryError,
    DiscoveryInvalidResponseError,
    DiscoveryQuotaError,
    DiscoveryRateLimitError,
    DiscoveryRequestError,
    DiscoveryUnavailableError,
)

# Match the existing backend convention (paypal.py): backend/.env feeds the
# process environment at import time.
load_dotenv()

logger = logging.getLogger("paypilot.channel3")

_API_BASE = "https://api.trychannel3.com"
_SEARCH_PATH = "/v1/search"

# Verified contract limits: limit is 1-30 (default 20).
_LIMIT_MIN = 1
_LIMIT_MAX = 30
DEFAULT_LIMIT = 20

# Explicit timeout so a slow upstream can never hang a chat turn
# indefinitely. Overridable via CHANNEL3_TIMEOUT_SECONDS (a plain float —
# no configuration framework).
_DEFAULT_TIMEOUT_SECONDS = 15.0

# Conservative bounded retries for transient failures only: one initial
# attempt plus two retries, with short increasing backoff. Safe for
# concurrent users — no shared state, no aggressive loops.
_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = (0.5, 1.0)

# Module-level sleep indirection so tests can neutralize the backoff.
_sleep = time.sleep


# ---------------------------------------------------------------------------
# Typed error structure. Each Channel3 exception SUBCLASSES the matching
# provider-neutral Discovery* exception (Phase 10D), so callers can handle
# every discovery provider uniformly while Channel3-specific behavior stays
# intact. `retryable` tells the caller whether the same request MAY succeed
# later (rate limit, upstream outage) or is pointless to repeat.
# ---------------------------------------------------------------------------


class Channel3Error(DiscoveryError):
    """Base class for all Channel3 client failures."""

    retryable: bool = False


class Channel3ConfigError(Channel3Error, DiscoveryConfigError):
    """CHANNEL3_API_KEY is not configured."""


class Channel3AuthError(Channel3Error, DiscoveryAuthError):
    """Channel3 rejected the API key (HTTP 401/403)."""


class Channel3QuotaError(Channel3Error, DiscoveryQuotaError):
    """Channel3 credits are exhausted (HTTP 402)."""


class Channel3RequestError(Channel3Error, DiscoveryRequestError):
    """Channel3 rejected the request itself (HTTP 400/404/422)."""


class Channel3RateLimitError(Channel3Error, DiscoveryRateLimitError):
    """Rate limited (HTTP 429) and retries were exhausted."""

    retryable = True


class Channel3UnavailableError(Channel3Error, DiscoveryUnavailableError):
    """Upstream/network failure (5xx, timeout) after bounded retries."""

    retryable = True


class Channel3InvalidResponseError(Channel3Error, DiscoveryInvalidResponseError):
    """Channel3 answered 200 but the body is not a valid search response."""


# ---------------------------------------------------------------------------
# Response models — validated mirrors of the documented /v1/search response.
# Only `id` and `title` are required on a product (and `url` on an offer);
# everything else is optional with safe defaults, because a missing optional
# field must never crash a whole search. Unknown fields are ignored.
# ---------------------------------------------------------------------------


class Channel3Brand(BaseModel):
    id: Optional[str] = None
    name: Optional[str] = None


class Channel3Image(BaseModel):
    url: str
    is_main_image: bool = False
    alt_text: Optional[str] = None


class Channel3Category(BaseModel):
    slug: Optional[str] = None
    title: Optional[str] = None


class Channel3Price(BaseModel):
    """An OBSERVED retailer price — discovery data, never a payment amount.

    Decimal (pydantic converts JSON numbers via their string form) so the
    observed value is preserved exactly, with no binary-float drift.
    """

    price: Decimal
    compare_at_price: Optional[Decimal] = None
    currency: str


class Channel3Offer(BaseModel):
    """One retailer's offer for a product (merchant link + observed price)."""

    url: str
    domain: Optional[str] = None
    price: Optional[Channel3Price] = None
    # Kept as plain strings (not Literals) so a new upstream enum value
    # degrades gracefully instead of invalidating the product.
    availability: Optional[str] = None
    condition: Optional[str] = None


class Channel3Product(BaseModel):
    """One discovered product. Everything here is external discovery data."""

    id: str
    title: str
    description: Optional[str] = None
    brands: list[Channel3Brand] = Field(default_factory=list)
    images: list[Channel3Image] = Field(default_factory=list)
    category: Optional[Channel3Category] = None
    offers: list[Channel3Offer] = Field(default_factory=list)
    key_features: Optional[list[str]] = None
    materials: Optional[list[str]] = None


class Channel3SearchResult(BaseModel):
    """A validated page of search results."""

    products: list[Channel3Product] = Field(default_factory=list)
    next_page_token: Optional[str] = None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class Channel3Client:
    """Thin, stateless client for Channel3 product search.

    Usage:
        client = Channel3Client()           # reads CHANNEL3_API_KEY
        result = client.search("wireless headphones", limit=5)
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ) -> None:
        key = (api_key or os.getenv("CHANNEL3_API_KEY", "")).strip()
        if not key:
            # Report THAT the key is missing, never any value.
            raise Channel3ConfigError(
                "CHANNEL3_API_KEY must be set in the environment (.env) "
                "to use Channel3 product discovery"
            )
        self._api_key = key

        if timeout_seconds is None:
            raw = os.getenv("CHANNEL3_TIMEOUT_SECONDS", "").strip()
            try:
                timeout_seconds = float(raw) if raw else _DEFAULT_TIMEOUT_SECONDS
            except ValueError:
                timeout_seconds = _DEFAULT_TIMEOUT_SECONDS
        self._timeout = timeout_seconds

    def search(
        self,
        query: Optional[str] = None,
        *,
        limit: int = DEFAULT_LIMIT,
        page_token: Optional[str] = None,
        filters: Optional[dict[str, Any]] = None,
        config: Optional[dict[str, Any]] = None,
    ) -> Channel3SearchResult:
        """Search Channel3 for products.

        Args:
            query: Free-text search query. Required unless page_token is
                given (the API needs at least one of them).
            limit: Results per page, 1-30 (API constraint; rejected here,
                never silently clamped).
            page_token: Opaque token from a previous result's
                next_page_token to fetch the next page.
            filters: Optional raw Channel3 `filters` object (e.g. price
                range, availability) passed through verbatim — kept generic
                so later phases can add deterministic filters without a
                client rewrite.
            config: Optional raw Channel3 `config` object (language,
                country, currency, mode), passed through verbatim.

        Raises:
            ValueError: invalid arguments (no query/page_token, bad limit) —
                rejected before any HTTP call.
            Channel3AuthError / Channel3QuotaError / Channel3RequestError /
            Channel3RateLimitError / Channel3UnavailableError /
            Channel3InvalidResponseError: see each class; `.retryable`
                says whether trying again later could help.
        """
        if not (query and query.strip()) and not page_token:
            raise ValueError("search requires a non-empty query or a page_token")
        if not (_LIMIT_MIN <= limit <= _LIMIT_MAX):
            raise ValueError(
                f"limit must be between {_LIMIT_MIN} and {_LIMIT_MAX}, got {limit}"
            )

        payload: dict[str, Any] = {"limit": limit}
        if query and query.strip():
            payload["query"] = query.strip()
        if page_token:
            payload["page_token"] = page_token
        if filters:
            payload["filters"] = filters
        if config:
            payload["config"] = config

        logger.info(
            "Channel3 search initiated (query=%r, limit=%d, page=%s)",
            (query or "")[:60],
            limit,
            "yes" if page_token else "no",
        )

        response = self._post_with_retries(payload)
        return self._parse_response(response)

    # -- HTTP ---------------------------------------------------------------

    def _post_with_retries(self, payload: dict[str, Any]) -> httpx.Response:
        """POST the search with bounded retries for transient failures only.

        Retried: HTTP 429, 5xx, and network-level errors (timeouts included).
        Never retried: 400/401/402/403/404/422 — repeating those cannot help.
        """
        last_transient: Optional[str] = None

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = httpx.post(
                    f"{_API_BASE}{_SEARCH_PATH}",
                    json=payload,
                    headers={
                        # The key goes in this header ONLY — never in the
                        # URL, logs, or error messages.
                        "x-api-key": self._api_key,
                        "Accept": "application/json",
                    },
                    timeout=self._timeout,
                )
            except httpx.RequestError as exc:
                # Network-level failure (DNS, refused, timeout...) — log the
                # exception type, not the repr, to keep headers out of logs.
                last_transient = f"network error ({type(exc).__name__})"
                logger.warning(
                    "Channel3 %s (attempt %d/%d)",
                    last_transient,
                    attempt,
                    _MAX_ATTEMPTS,
                )
                if attempt < _MAX_ATTEMPTS:
                    _sleep(_BACKOFF_SECONDS[attempt - 1])
                    continue
                raise Channel3UnavailableError(
                    "Could not reach Channel3 after "
                    f"{_MAX_ATTEMPTS} attempts ({last_transient})"
                ) from exc

            status = response.status_code

            if status in (200, 201):
                return response

            if status in (401, 403):
                logger.error("Channel3 authentication failed: HTTP %s", status)
                raise Channel3AuthError(
                    f"Channel3 rejected the API key (HTTP {status})"
                )
            if status == 402:
                logger.error("Channel3 credits exhausted: HTTP 402")
                raise Channel3QuotaError(
                    "Channel3 API credits are exhausted (HTTP 402)"
                )
            if status in (400, 404, 422):
                logger.error("Channel3 rejected the request: HTTP %s", status)
                raise Channel3RequestError(
                    f"Channel3 rejected the search request (HTTP {status})"
                )

            if status == 429 or 500 <= status <= 599:
                last_transient = f"HTTP {status}"
                logger.warning(
                    "Channel3 transient failure %s (attempt %d/%d)",
                    last_transient,
                    attempt,
                    _MAX_ATTEMPTS,
                )
                if attempt < _MAX_ATTEMPTS:
                    _sleep(_BACKOFF_SECONDS[attempt - 1])
                    continue
                if status == 429:
                    raise Channel3RateLimitError(
                        f"Channel3 rate limit exceeded after {_MAX_ATTEMPTS} "
                        "attempts (HTTP 429)"
                    )
                raise Channel3UnavailableError(
                    f"Channel3 unavailable after {_MAX_ATTEMPTS} attempts "
                    f"(HTTP {status})"
                )

            logger.error("Channel3 unexpected response: HTTP %s", status)
            raise Channel3Error(f"Unexpected Channel3 response (HTTP {status})")

        # Unreachable: every loop path returns or raises.
        raise Channel3UnavailableError("Channel3 request could not be completed")

    # -- Validation ----------------------------------------------------------

    def _parse_response(self, response: httpx.Response) -> Channel3SearchResult:
        """Validate the response body into Channel3SearchResult.

        A product that fails validation (e.g. missing id/title) is skipped
        with a warning rather than failing the whole page; a body that is
        not a search response at all raises Channel3InvalidResponseError.
        An upstream outage is NEVER turned into fake/empty-but-OK results —
        this method only runs on an HTTP 2xx body.
        """
        try:
            body = response.json()
        except ValueError as exc:
            logger.error("Channel3 returned a non-JSON body")
            raise Channel3InvalidResponseError(
                "Channel3 response body is not valid JSON"
            ) from exc

        if not isinstance(body, dict) or not isinstance(body.get("products"), list):
            logger.error("Channel3 response is not a search result object")
            raise Channel3InvalidResponseError(
                "Channel3 response does not match the search response schema"
            )

        products: list[Channel3Product] = []
        skipped = 0
        for item in body["products"]:
            try:
                products.append(Channel3Product.model_validate(item))
            except ValidationError:
                skipped += 1

        if skipped:
            logger.warning(
                "Channel3 search: skipped %d invalid product record(s)", skipped
            )

        next_token = body.get("next_page_token")
        result = Channel3SearchResult(
            products=products,
            next_page_token=next_token if isinstance(next_token, str) else None,
        )
        logger.info(
            "Channel3 search ok: HTTP %s, %d product(s), next_page=%s",
            response.status_code,
            len(result.products),
            "yes" if result.next_page_token else "no",
        )
        return result
