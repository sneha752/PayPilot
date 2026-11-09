"""Provider-aware product identity (Phase 11B.1).

THE DISTINCTION THIS MODULE EXISTS TO ENFORCE
    A provider's id for a search result is not automatically a permanent
    name for the product. Two different things were being conflated:

      provider_observation_id   "the provider called this result X in the
                                 response it gave us at that moment"
      product_identity_key      "X will still mean this same product the
                                 next time we ask"

    The first is always available. The second is a CLAIM, and it must be
    backed by evidence about the specific provider's contract. Treating the
    first as the second is how a revalidation would silently confirm the
    wrong product later on.

    So: an adopted product always keeps its provider_observation_id, and
    gets a product_identity_key ONLY when the provider's id is demonstrably
    durable. When it is not, revalidation reports IDENTITY_UNVERIFIABLE
    rather than guessing.

WHAT IS NOT IDENTITY, EVER
    Title, merchant name, merchant domain, price, brand, category,
    availability, and result position are NEVER identity, alone or in any
    combination. They all drift, and they are exactly what an attacker or a
    reshuffled ranking would influence. There is no code path in this
    module that reads any of them.

EVIDENCE PER PROVIDER (measured in this project, not assumed)

    channel3 -> DURABLE
        external_id is Channel3Product.id, returned by a product API whose
        verified contract (see channel3_client) makes `id` the product's
        required primary identifier. It is a catalog key, not a per-response
        artifact.

    serper, id from productId -> DURABLE
        external_id is "serper:<productId>", Google Shopping's catalogid.
        Measured on 2026-10-07 against the live API through this project's
        own provider:
          - 3 repeats of one query: 19/19 products kept the same
            external_id (product_url and retailer_domain also identical).
          - 3 DIFFERENT queries ("sony wh-ch520 wireless headphones",
            "WH-CH520", "sony wireless headphones on-ear bluetooth"): of the
            8 products appearing under all three, 8/8 had the SAME
            external_id. Zero disagreements.
          - A product adopted ~35 minutes earlier under an unrelated query
            was still retrievable under its original external_id.
        Across 180 observations in that experiment, every result carried a
        productId.

    serper, id from the link hash -> NOT DURABLE
        When Serper omits productId, the provider falls back to
        "serper:url:<sha256(link)[:16]>". In the same experiment, 95/95
        Google-hosted links embedded the SEARCH QUERY in the URL, e.g.
            https://www.google.com/search?ibp=oshop&q=sony+wh-ch520...&prds=...
        A hash of that URL is therefore an artifact of one particular
        search, not a name for the product: the same product reached by a
        different query hashes differently. These ids identify an
        observation only.

    anything else -> NOT DURABLE
        An unrecognized provider has no contract evidence here, so no
        durability is claimed. Silence is not a guarantee.

    Re-measuring is the way to change any of this. A field does not become
    durable because it looks like an id.
"""

from __future__ import annotations

import logging
from typing import Optional

from pydantic import BaseModel, Field

from ..schemas.adoption import ProductIdentityKind

logger = logging.getLogger("paypilot.product_identity")

#: Discovery sources whose external_id is a catalog/product-API primary key.
#: Channel3's contract requires `id` on every product.
DURABLE_ID_SOURCES = frozenset({"channel3"})

#: Prefix the Serper adapter uses when Serper supplied no productId and the
#: id had to be derived from the (query-bearing) result link.
SERPER_LINK_HASH_PREFIX = "serper:url:"

SOURCE_SERPER = "serper"


class ProductIdentity(BaseModel):
    """How a product can (or cannot) be re-identified at a later time."""

    provider_observation_id: str = Field(
        description="What the provider called this result when we saw it. "
        "Always present. NOT a promise about the future."
    )
    kind: ProductIdentityKind = Field(
        description="Whether this provider's id is durable evidence of "
        "product identity, or only of one observation."
    )
    product_identity_key: Optional[str] = Field(
        default=None,
        description="A key that may be used to re-identify this product "
        "later, namespaced by provider. Null when no durable identity "
        "exists — in that case revalidation must report "
        "IDENTITY_UNVERIFIABLE rather than match on anything else.",
    )
    evidence: str = Field(
        description="Why this classification holds, for audit. Deterministic "
        "text, never AI-written."
    )

    @property
    def is_durable(self) -> bool:
        """True when a later revalidation may rely on this identity."""
        return (
            self.kind is ProductIdentityKind.PROVIDER_PRODUCT_ID
            and self.product_identity_key is not None
        )


def classify(source: str, external_id: str) -> ProductIdentity:
    """Classify a provider identity as durable or observation-only.

    Reads ONLY the provider name and its id. No title, merchant, price or
    brand is accepted as an argument, so none can influence the result.

    Raises:
        ValueError: blank source or external_id.
    """
    cleaned_source = (source or "").strip()
    cleaned_id = (external_id or "").strip()
    if not cleaned_source:
        raise ValueError("source must be a non-empty string")
    if not cleaned_id:
        raise ValueError("external_id must be a non-empty string")

    normalized_source = cleaned_source.lower()

    if normalized_source in DURABLE_ID_SOURCES:
        return ProductIdentity(
            provider_observation_id=cleaned_id,
            kind=ProductIdentityKind.PROVIDER_PRODUCT_ID,
            product_identity_key=f"{normalized_source}:{cleaned_id}",
            evidence=(
                "Channel3's verified /v1/search contract requires `id` on "
                "every product; it is a catalog primary key, not a "
                "per-response artifact."
            ),
        )

    if normalized_source == SOURCE_SERPER:
        if cleaned_id.startswith(SERPER_LINK_HASH_PREFIX):
            # The hashed link carries the search query, so this id names a
            # search result, not a product.
            return ProductIdentity(
                provider_observation_id=cleaned_id,
                kind=ProductIdentityKind.PROVIDER_OBSERVATION_ONLY,
                product_identity_key=None,
                evidence=(
                    "Serper supplied no productId, so the id is a hash of "
                    "the result link. Google Shopping links embed the search "
                    "query (95/95 measured), making the hash specific to one "
                    "search rather than to the product."
                ),
            )
        return ProductIdentity(
            provider_observation_id=cleaned_id,
            kind=ProductIdentityKind.PROVIDER_PRODUCT_ID,
            product_identity_key=f"{normalized_source}:{cleaned_id}",
            evidence=(
                "Serper productId (Google Shopping catalogid), measured "
                "stable across 3 repeats of one query (19/19) and across 3 "
                "different queries (8/8) on 2026-10-07."
            ),
        )

    return ProductIdentity(
        provider_observation_id=cleaned_id,
        kind=ProductIdentityKind.PROVIDER_OBSERVATION_ONLY,
        product_identity_key=None,
        evidence=(
            f"No durability evidence exists in this project for discovery "
            f"source '{cleaned_source}', so none is claimed."
        ),
    )


def identity_key_for(source: str, external_id: str) -> Optional[str]:
    """The durable key for a provider identity, or None. Convenience wrapper."""
    return classify(source, external_id).product_identity_key
