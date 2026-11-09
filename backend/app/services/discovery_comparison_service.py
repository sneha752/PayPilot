"""Discovery comparison orchestration (Phase 10E).

Runs ONE research-only comparison:

    1. validate the query
    2. resolve structured requirements (client-supplied, else derived from AI
       intent extraction on the query)
    3. live discovery through the configured provider (Phase 10D abstraction)
    4. deterministic currency normalization (Phase 10F) — derive a price in
       the target currency so an INR budget can admit a USD candidate
    5. deterministic merchant identity (Phase 10H) — carry the provider's
       merchant name over and derive a merchant domain where one is
       deterministic
   5b. record the normalized products as server-side discovery EVIDENCE
       (Phase 11A), so a later adoption needs only an identifier from the
       client — best-effort, and never a payment authority
    6. deterministic candidate filtering
    7. deterministic reduction to MAX_AI_CANDIDATES
    8. AI ranking of the surviving candidate ids
    9. validation of the AI output against the candidate set
   10. re-join of ranked ids to the backend-owned discovery records
   11. structured comparison response

CURRENCY NORMALIZATION
    Target currency = requirements.price_currency when the user stated one,
    else INR (the project's display currency). Observed prices are never
    altered; a derived amount is ADDED alongside them, and the conversion is
    restated deterministically in each result's price_note. The AI performs
    no conversion and the PayPal demo rate is never consulted. A candidate
    whose currency cannot be converted keeps null derived fields and is
    excluded by a strict monetary bound — it never fails the request.

MERCHANT IDENTITY
    A Serper result links through Google Shopping, so its link host is
    google.com even when the seller is Amazon. Merchant identity is
    therefore resolved separately: the provider's merchant NAME is carried
    over as evidence, and a merchant DOMAIN is derived only when the
    product's own retailer_domain/product_url deterministically yields a
    non-aggregator host. A name is never turned into a domain, no extra
    external request is made to resolve one, and when neither is available
    both stay null. Merchant identity is display/provenance data only: it
    is not a payee and carries no payment authority.

THE AUTHORITY BOUNDARY
    The AI contributes exactly two things: an ordering of ids it was given,
    and a reason string per id. Every product fact in the response —
    external_id, title, observed price, currency, URL, image, source,
    retailer, availability, timestamp — is the DiscoveredProduct instance
    the discovery layer produced, carried through by reference. There is no
    code path by which AI output reaches a product field.

WHAT THIS SERVICE NEVER DOES
    No PayPal call of any kind. No order creation, capture, or refund. No
    Policy Engine evaluation. No transaction-ledger read or write. No
    catalog/products.json read or mutation. No session or chat state. No
    currency conversion. No payment amount is computed and no discovered
    product becomes purchasable through this path.

    The Phase 11A evidence write is the only persistence here, and it writes
    exclusively to the discovery_evidence table. Recording what was observed
    does not make it purchasable, and nothing downstream may read that table
    as an amount to charge.

AI FAILURE
    By default an AI failure is an error: a ranking is never fabricated.
    When the caller opts in with allow_deterministic_fallback, the response
    falls back to the documented deterministic ordering and is labeled
    ranking_method="deterministic" with ranking_provider=null, so fallback
    output is never presented as AI-ranked. Requirement DERIVATION failures
    always propagate — silently discarding the user's stated constraints
    would be worse than an honest error.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..agents.comparison_agent import RankingValidationError
from ..agents.comparison_agent import rank as rank_candidates
from ..agents.intent_agent import interpret as interpret_intent
from ..schemas.comparison import (
    DEFAULT_TOP_K,
    ComparisonRequirements,
    ComparisonResponse,
    ComparisonResult,
    requirements_from_intent,
)
from ..schemas.discovery import NormalizedDiscoveredProduct
from .channel3_client import Channel3Client
from .discovery_currency_service import (
    DEFAULT_NORMALIZATION_CURRENCY,
    describe_conversion,
    normalize_products,
)
from .discovery_exchange_rate_provider import DiscoveryExchangeRateProvider
from .discovery_filter_service import (
    MAX_AI_CANDIDATES,
    order_candidates,
    select_candidates,
)
from .discovery_evidence_store import record_products
from .discovery_merchant_service import attach_merchant_identity
from .discovery_service import search_discovery_with_evidence
from .providers import (
    AIConfigError,
    AIProvider,
    AIServiceError,
    InvalidAIResponseError,
    get_provider,
)

logger = logging.getLogger("paypilot.discovery_comparison")

#: Provider-level and validation failures that a deterministic fallback may
#: absorb: misconfiguration, network/status errors, rate limits and timeouts
#: (AIServiceError), malformed JSON (InvalidAIResponseError), and unusable
#: output such as unknown or duplicated ids (RankingValidationError).
_RANKING_FAILURES = (
    AIConfigError,
    AIServiceError,
    InvalidAIResponseError,
    RankingValidationError,
)


def _deterministic_reason(
    product: NormalizedDiscoveredProduct, requirements: ComparisonRequirements
) -> str:
    """A reason that states only what the deterministic ordering actually did.

    It never claims AI involvement and makes no quality claim about the
    product or the market.
    """
    if product.observed_price is not None and product.observed_currency:
        return (
            "Deterministic fallback ordering (not AI-ranked): placed by lower "
            f"observed price among the returned candidates priced in "
            f"{product.observed_currency}."
        )
    if requirements.has_price_constraint:  # pragma: no cover - filtered out
        return (
            "Deterministic fallback ordering (not AI-ranked): no comparable "
            "observed price available."
        )
    return (
        "Deterministic fallback ordering (not AI-ranked): no observed price "
        "available for this candidate, placed by stable candidate id."
    )


def _deterministic_results(
    candidates: list[NormalizedDiscoveredProduct],
    requirements: ComparisonRequirements,
    top_k: int,
) -> list[ComparisonResult]:
    """Build a clearly-labeled deterministic ranking (no AI involved)."""
    ordered = order_candidates(candidates, requirements)[:top_k]
    return [
        ComparisonResult(
            rank=position,
            product=product,
            reason=_deterministic_reason(product, requirements),
            price_note=describe_conversion(product),
        )
        for position, product in enumerate(ordered, start=1)
    ]


def compare_discovery(
    query: str,
    requirements: Optional[ComparisonRequirements] = None,
    limit: int = 10,
    top_k: int = DEFAULT_TOP_K,
    allow_deterministic_fallback: bool = False,
    max_ai_candidates: int = MAX_AI_CANDIDATES,
    client: Optional[Channel3Client] = None,
    provider: Optional[AIProvider] = None,
    rate_provider: Optional[DiscoveryExchangeRateProvider] = None,
) -> ComparisonResponse:
    """Discover, filter, rank and compare external products. Research only.

    Args:
        query: Free-text shopping query (must be non-empty after trimming).
        requirements: Explicit structured requirements. When None they are
            derived from AI intent extraction on `query`.
        limit: Discovery results to fetch from the provider.
        top_k: Ranked results to return.
        allow_deterministic_fallback: Fall back to the deterministic ordering
            (clearly labeled) instead of raising when AI ranking fails.
        max_ai_candidates: Deterministic cap on candidates sent to the AI.
        client: Optional pre-built Channel3 client (tests/DI).
        provider: Optional explicit AI provider (tests/DI).
        rate_provider: Optional explicit discovery exchange-rate provider
            (tests/DI). Never a PayPal rate.

    Raises:
        ValueError: blank query, or top_k / limit rejected downstream.
        Discovery* errors: propagated from the discovery provider.
        AI provider errors / RankingValidationError: propagated when no
            deterministic fallback was requested.
    """
    trimmed = (query or "").strip()
    if not trimmed:
        # Checked before any AI or discovery call so a bad request costs
        # nothing upstream.
        raise ValueError("query must be a non-empty string")
    if top_k < 1:
        raise ValueError("top_k must be at least 1")

    # Step 2 — requirements. A derivation failure propagates: proceeding with
    # the user's constraints silently dropped is not an acceptable fallback.
    if requirements is None:
        intent = interpret_intent(trimmed)
        requirements = requirements_from_intent(intent)
        logger.info(
            "Derived comparison requirements from intent "
            "(category=%s price_constraint=%s required_kw=%d)",
            requirements.category,
            requirements.has_price_constraint,
            len(requirements.required_keywords),
        )

    # Step 3 — live discovery through the provider-neutral abstraction. The
    # evidence-carrying entry point is used so the provider's merchant names
    # survive; it is the same single upstream search.
    outcome = search_discovery_with_evidence(
        query=trimmed, limit=limit, client=client
    )
    discovered = outcome.response.products
    source = discovered[0].source if discovered else None

    # Step 4 — derive a price in the target currency. Observed prices are
    # copied through untouched; an unconvertible candidate simply keeps null
    # derived fields.
    target_currency = requirements.price_currency or DEFAULT_NORMALIZATION_CURRENCY
    normalized = normalize_products(discovered, target_currency, rate_provider)

    # Step 5 — resolve merchant identity. Writes only merchant_name /
    # merchant_domain; every observed and derived-price field is carried
    # through untouched, and no external request is made.
    normalized = attach_merchant_identity(normalized, outcome.merchant_evidence)

    # Step 5b (Phase 11A) — remember what we served, so a later adoption can
    # be identified by (source, external_id) alone instead of the client
    # sending product details back. Recorded BEFORE filtering and ranking, so
    # the stored evidence is identical whether the AI ranked or the
    # deterministic fallback ran. This is evidence, never a payment authority.
    #
    # A cache failure must never fail a comparison: the user asked to
    # compare products, not to populate a cache. The consequence of a miss is
    # simply that adopting that product later returns "no evidence stored".
    try:
        record_products(normalized)
    except Exception:  # noqa: BLE001 - cache writes are strictly best-effort
        logger.warning(
            "Could not record discovery evidence; comparison continues "
            "(adoption of these products will report no stored evidence)",
            exc_info=True,
        )

    # Steps 6-7 — deterministic filtering, then the deterministic cap.
    # Filtering is unaffected by merchant identity: Phase 10E semantics are
    # unchanged and no requirement is matched against a merchant.
    candidates = select_candidates(normalized, requirements, max_ai_candidates)

    def _respond(
        method: str,
        ranking_provider: Optional[str],
        results: list[ComparisonResult],
    ) -> ComparisonResponse:
        return ComparisonResponse(
            query=trimmed,
            requirements=requirements,
            ranking_method=method,
            ranking_provider=ranking_provider,
            discovery_source=source,
            normalization_currency=target_currency,
            discovered_count=len(discovered),
            candidates_considered=len(candidates),
            results=results,
        )

    if not candidates:
        # Nothing survived filtering (or nothing was discovered). Reported
        # honestly, with no AI call spent and no constraint relaxed.
        logger.info(
            "Comparison found no eligible candidates from %d discovered result(s)",
            len(discovered),
        )
        return _respond("deterministic", None, [])

    # Steps 8-10 — AI ranking, validation, and re-join to backend records.
    # The provider is resolved here so the response can name it, and so a
    # bad AI_PROVIDER value is absorbed by the same fallback path.
    try:
        ai_provider = provider or get_provider()
        ranked = rank_candidates(requirements, candidates, top_k, provider=ai_provider)
    except _RANKING_FAILURES as exc:
        if not allow_deterministic_fallback:
            raise
        logger.warning(
            "AI ranking failed (%s); returning a deterministic ordering "
            "explicitly labeled as not AI-ranked",
            type(exc).__name__,
        )
        return _respond(
            "deterministic",
            None,
            _deterministic_results(candidates, requirements, top_k),
        )

    results = [
        # entry.product is the normalized discovery instance the backend
        # built; the AI supplied only the position and the reason string.
        # price_note is assembled from backend data, never from the model, so
        # any converted figure here is the backend's own arithmetic.
        ComparisonResult(
            rank=entry.rank,
            product=entry.product,
            reason=entry.reason,
            price_note=describe_conversion(entry.product),
        )
        for entry in ranked
    ]
    return _respond("ai", ai_provider.name, results)
