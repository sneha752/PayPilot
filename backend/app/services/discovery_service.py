"""Normalized discovery service (Phase 10C, provider-neutral since 10D).

Orchestrates ONE deterministic discovery search:

    1. validate the query
    2. select the configured DiscoveryProvider (DISCOVERY_PROVIDER env:
       "channel3" — the default, preserving Phase 10C behavior — or
       "serper"; anything else is a clear configuration error)
    3. call the provider (which returns already-normalized products)
    4. stamp a server-side UTC observed_at and assemble the existing
       DiscoverySearchResponse

Two entry points, one implementation:

  - search_discovery() returns the DiscoverySearchResponse exactly as Phase
    10C/10D defined it. GET /discovery/search is unchanged, down to the
    product key set.
  - search_discovery_with_evidence() additionally hands back the provider's
    Phase 10H MerchantEvidence, for callers that resolve merchant identity
    (the comparison service). It is the same search - no extra request.

This service is discovery-only. It must never:
  - rank with AI, or call any AI provider
  - convert currency or compute a PayPal amount
  - invoke the Policy Engine
  - create catalog Product records or transactions
  - touch sessions, chat, or purchase execution

Provider failures propagate as provider-neutral Discovery* exceptions —
an outage is surfaced honestly, never turned into fake/empty results.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from ..schemas.discovery import DiscoverySearchResponse, MerchantEvidence
from .channel3_client import Channel3Client
from .discovery_providers.base import DiscoveryConfigError, DiscoveryProvider
from .discovery_providers.channel3 import (  # re-exported: Phase 10C public
    SOURCE_CHANNEL3,  # noqa: F401  (stable import path for callers/tests)
    Channel3Provider,
    _select_offer,  # noqa: F401
    normalize_product,  # noqa: F401
)
from .discovery_providers.serper import SerperProvider

logger = logging.getLogger("paypilot.discovery")

DEFAULT_DISCOVERY_LIMIT = 10

#: Providers selectable via the DISCOVERY_PROVIDER environment variable.
SUPPORTED_PROVIDERS = ("channel3", "serper")


def _make_client() -> Channel3Client:
    """Channel3 client factory — kept separate so tests can substitute."""
    return Channel3Client()


def _select_provider(
    client: Optional[Channel3Client] = None,
) -> DiscoveryProvider:
    """Deterministically pick ONE provider for this request.

    An explicitly supplied Channel3 client always selects the Channel3
    adapter (test/injection path, unchanged from Phase 10C). Otherwise the
    DISCOVERY_PROVIDER environment variable decides, defaulting to
    "channel3" for backward compatibility. Providers are never mixed or
    silently swapped within a request, and selecting one never requires
    the other to be configured.
    """
    if client is not None:
        return Channel3Provider(client)

    name = os.getenv("DISCOVERY_PROVIDER", "").strip().lower() or "channel3"
    if name == "channel3":
        return Channel3Provider(_make_client())
    if name == "serper":
        return SerperProvider()
    raise DiscoveryConfigError(
        f"Unsupported DISCOVERY_PROVIDER '{name}' — expected one of: "
        + ", ".join(SUPPORTED_PROVIDERS)
    )


@dataclass(frozen=True)
class DiscoveryOutcome:
    """One discovery search: the public response plus merchant evidence.

    `merchant_evidence` is the provider's Phase 10H merchant display names.
    It is NOT part of DiscoverySearchResponse and never reaches
    GET /discovery/search: that endpoint's contract is pure Phase 10C/10D
    provider evidence. Empty when the provider named no merchant.
    """

    response: DiscoverySearchResponse
    merchant_evidence: tuple[MerchantEvidence, ...] = ()


def provider_for_source(
    source: str,
    client: Optional[Channel3Client] = None,
) -> DiscoveryProvider:
    """Build the provider named by `source`, ignoring DISCOVERY_PROVIDER.

    Revalidation (Phase 11B) must re-query the provider a product was
    ORIGINALLY discovered from, which is a property of the adopted record —
    not of whatever DISCOVERY_PROVIDER happens to say now. Selecting by env
    here would silently revalidate a Channel3 product against Serper, and
    the identity check would then fail for an entirely spurious reason.

    `source` comes from server-side storage, never from a client. An
    unconfigured provider raises DiscoveryConfigError, which the caller
    reports as "cannot revalidate" rather than treating as a product
    verdict.

    Raises:
        ValueError: blank source.
        DiscoveryConfigError: unsupported name, or the provider's own
            configuration (e.g. its API key) is missing.
    """
    name = (source or "").strip().lower()
    if not name:
        raise ValueError("source must be a non-empty string")
    if name == "channel3":
        return Channel3Provider(client if client is not None else _make_client())
    if name == "serper":
        return SerperProvider()
    raise DiscoveryConfigError(
        f"Unsupported discovery source '{name}' — expected one of: "
        + ", ".join(SUPPORTED_PROVIDERS)
    )


def search_discovery_with_evidence(
    query: str,
    limit: int = DEFAULT_DISCOVERY_LIMIT,
    page_token: Optional[str] = None,
    client: Optional[Channel3Client] = None,
) -> DiscoveryOutcome:
    """Search the configured provider, keeping the provider's merchant names.

    Identical to search_discovery() in every observable way - same
    validation, same single upstream call, same response - except that the
    provider's MerchantEvidence survives instead of being dropped.

    Args:
        query: Free-text search query (must be non-empty after trimming).
        limit: Results per page.
        page_token: Opaque continuation token, if the provider supports one.
        client: Optional pre-built Channel3 client (tests); forces the
            Channel3 adapter.

    Raises:
        ValueError: blank query, out-of-range limit, or a page_token the
            selected provider does not support (no upstream call is made
            for the first two).
        DiscoveryError subclasses: propagated for the API layer to map
            onto HTTP statuses.
    """
    trimmed = (query or "").strip()
    if not trimmed:
        raise ValueError("query must be a non-empty string")

    provider = _select_provider(client)

    # One server-side timestamp per page: every product in this response
    # was observed in the same upstream call.
    observed_at = datetime.now(timezone.utc)

    result = provider.search(
        trimmed, limit=limit, page_token=page_token, observed_at=observed_at
    )

    logger.info(
        "Discovery search normalized %d product(s) (provider=%s, query=%r, "
        "next_page=%s, merchant_names=%d)",
        len(result.products),
        provider.source,
        trimmed[:60],
        "yes" if result.next_page_token else "no",
        len(result.merchant_evidence),
    )

    return DiscoveryOutcome(
        response=DiscoverySearchResponse(
            query=trimmed,
            products=result.products,
            next_page_token=result.next_page_token,
        ),
        merchant_evidence=tuple(result.merchant_evidence),
    )


def search_discovery(
    query: str,
    limit: int = DEFAULT_DISCOVERY_LIMIT,
    page_token: Optional[str] = None,
    client: Optional[Channel3Client] = None,
) -> DiscoverySearchResponse:
    """Search the configured provider and return normalized results.

    The Phase 10C/10D entry point, unchanged: provider evidence only. Use
    search_discovery_with_evidence() when merchant identity is needed.

    Args:
        query: Free-text search query (must be non-empty after trimming).
        limit: Results per page.
        page_token: Opaque continuation token, if the provider supports one.
        client: Optional pre-built Channel3 client (tests); forces the
            Channel3 adapter.

    Raises:
        ValueError: blank query, out-of-range limit, or a page_token the
            selected provider does not support (no upstream call is made
            for the first two).
        DiscoveryError subclasses: propagated for the API layer to map
            onto HTTP statuses.
    """
    return search_discovery_with_evidence(
        query=query, limit=limit, page_token=page_token, client=client
    ).response
