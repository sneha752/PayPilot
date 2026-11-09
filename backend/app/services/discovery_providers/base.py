"""Provider-neutral discovery interface and exceptions (Phase 10D).

The common representation every provider must produce is the existing
Phase 10C DiscoveredProduct — external discovery EVIDENCE only. The
abstraction deliberately has no concept of PayPal, policy, the local
catalog, transactions, sessions, or AI, and no provider result can ever
become a payment amount through this layer.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from ...schemas.discovery import DiscoveredProduct, MerchantEvidence


# ---------------------------------------------------------------------------
# Provider-neutral exceptions. Provider-specific exceptions (e.g. the
# Channel3 client's) SUBCLASS these, so callers and the API layer handle
# every provider identically. `retryable` says whether the same request
# may succeed later.
# ---------------------------------------------------------------------------


class DiscoveryError(Exception):
    """Base class for all discovery-provider failures."""

    retryable: bool = False


class DiscoveryConfigError(DiscoveryError):
    """The selected provider is not configured (e.g. missing API key)."""


class DiscoveryAuthError(DiscoveryError):
    """The provider rejected our credentials."""


class DiscoveryQuotaError(DiscoveryError):
    """Provider credits/billing exhausted."""


class DiscoveryRequestError(DiscoveryError):
    """The provider rejected the request itself as invalid."""


class DiscoveryRateLimitError(DiscoveryError):
    """Provider rate limit hit."""

    retryable = True


class DiscoveryUnavailableError(DiscoveryError):
    """Provider upstream/network failure."""

    retryable = True


class DiscoveryInvalidResponseError(DiscoveryError):
    """The provider answered, but not with a usable response body."""


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------


class ProviderSearchResult(BaseModel):
    """What a provider returns: normalized products + optional pagination.

    `merchant_evidence` (Phase 10H) is a deliberately separate channel. The
    DiscoveredProduct contract is exactly the Phase 10C/10D provider-evidence
    fields and stays that way, so a merchant DISPLAY NAME - which some
    providers report and that model has no field for - rides alongside the
    products instead of widening them. It is optional: a provider that names
    no merchant simply leaves it empty, and merchant identity then resolves
    to whatever is deterministically derivable (see
    discovery_merchant_service).
    """

    products: list[DiscoveredProduct] = Field(default_factory=list)
    next_page_token: Optional[str] = None
    merchant_evidence: list[MerchantEvidence] = Field(
        default_factory=list,
        description="Provider-reported merchant names, bound to products by "
        "external_id. Never a domain, and never a payment destination.",
    )


class DiscoveryProvider(ABC):
    """One external discovery source, already normalized.

    Implementations own their HTTP contract, authentication, timeout and
    validation, and raise Discovery* exceptions (or subclasses). They must
    never fabricate results: an upstream failure is an exception, not an
    empty success.
    """

    #: Provenance value written into DiscoveredProduct.source.
    source: str

    @abstractmethod
    def search(
        self,
        query: str,
        *,
        limit: int,
        page_token: Optional[str] = None,
        observed_at: datetime,
    ) -> ProviderSearchResult:
        """Search the provider and return normalized discovery results.

        Args:
            query: Non-empty free-text query (validated by the caller).
            limit: Maximum results to return.
            page_token: Continuation token, only if the provider supports
                one — a provider without token pagination must reject a
                supplied token rather than silently ignoring it.
            observed_at: Server-side UTC timestamp to stamp on results.
        """
