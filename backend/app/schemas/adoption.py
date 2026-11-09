"""External-product adoption schemas (Phase 11A).

THE ADOPTION BOUNDARY
    An adopted product is PayPilot's OWN record of an external product a
    user selected. It is the third of four distinct trust levels, and the
    boundaries between them are the entire point of this module:

      1. DiscoveredProduct          Provider evidence. What Serper or
                                    Channel3 reported.
      2. NormalizedDiscoveredProduct  PayPilot-derived discovery data:
                                    normalized price (10F), merchant
                                    identity (10H).
      3. AdoptedProduct  (HERE)     A PayPilot-owned, persisted record,
                                    built ONLY from (2) as the server
                                    stored it.
      4. Purchase authority         Does not exist yet. A later phase must
                                    re-validate against a live source
                                    before any amount is authorized.

OBSERVED PRICE IS NOT A PURCHASE PRICE
    observed_price / observed_currency are what a retailer was SEEN asking
    at observed_at. They are retained as discovery evidence and nothing
    more. They are not a quote, not an offer, not a payment amount, and not
    a PayPal amount. An adopted record going stale is expected and normal;
    that is precisely why a purchase phase must re-validate rather than
    trust this number. The field names say "observed" everywhere, and there
    is deliberately no `price`, `purchase_price`, `amount`, or
    `paypal_amount` field for one to be mistaken for.

WHAT IS DELIBERATELY ABSENT
    No paypal_order_id, no paypal_capture_id, no refund id, no payment
    amount, no policy decision, no transaction id, no authorization state,
    and no AI-generated text. A hallucinated or client-forged value has
    nowhere to land, because no such field exists on these models.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Bounds on the identifiers a client may send. Generous enough for every
#: real provider id (Serper's are URL hashes or numeric product ids), tight
#: enough that an unbounded blob is rejected before it reaches a query.
MAX_SOURCE_LENGTH = 64
MAX_EXTERNAL_ID_LENGTH = 512


class ProductIdentityKind(str, Enum):
    """Whether a provider's id is durable product identity (Phase 11B.1).

    PROVIDER_PRODUCT_ID
        The provider's id is a product-level identifier that has been shown
        to survive separate requests, so a later revalidation may rely on
        it. Carries a product_identity_key.

    PROVIDER_OBSERVATION_ONLY
        The provider's id names the result we saw, not the product. It is
        still recorded (an observation is real), but it is NOT a basis for
        re-identifying the product later, so revalidation reports
        IDENTITY_UNVERIFIABLE instead of matching on something weaker.

    The classification is per provider and backed by measured evidence —
    see services/product_identity.py. Adoption itself never upgrades an
    observation into an identity: picking a product proves nothing about
    whether its id will still mean the same thing tomorrow.
    """

    PROVIDER_PRODUCT_ID = "PROVIDER_PRODUCT_ID"
    PROVIDER_OBSERVATION_ONLY = "PROVIDER_OBSERVATION_ONLY"


class AdoptedProductStatus(str, Enum):
    """Lifecycle of an adopted record. No purchase states exist here.

    ACTIVE   The adopted record was built from valid stored discovery
             evidence. This is the ONLY status Phase 11A produces.
    STALE    Reserved: the evidence is too old to be presented as current.
             Nothing sets this yet; revalidation belongs to a later phase.
    DISABLED Reserved: the record must not be used (withdrawn offer,
             operator action). Nothing sets this yet.

    STALE and DISABLED are declared rather than invented later so that the
    lifecycle is visible now, and so no caller can read ACTIVE as meaning
    "purchasable" — being ACTIVE means the adoption succeeded, nothing more.
    """

    ACTIVE = "ACTIVE"
    STALE = "STALE"
    DISABLED = "DISABLED"


class AdoptionRequest(BaseModel):
    """Request body for POST /discovery/adopt — an IDENTIFIER, nothing else.

    These two fields are the complete set of things a client may say. There
    is no field for a title, price, currency, merchant, URL, image, PayPal
    amount, order id, capture id, or policy override, and extra="forbid"
    makes an attempt to add one a 422 rather than a silently ignored key.

    Every product fact in the adopted record is read from the server's own
    stored discovery evidence, which only the discovery layer ever writes.
    So there is no code path — not even a mistaken one — by which a value in
    this request becomes authoritative product data.
    """

    model_config = ConfigDict(extra="forbid")

    source: str = Field(
        min_length=1,
        max_length=MAX_SOURCE_LENGTH,
        description="Discovery provider that produced the result, e.g. "
        "'serper'. Matched against stored evidence; never trusted as a fact.",
    )
    external_id: str = Field(
        min_length=1,
        max_length=MAX_EXTERNAL_ID_LENGTH,
        description="The provider's product id as the discovery layer "
        "recorded it, e.g. 'serper:111111'.",
    )

    @field_validator("source", "external_id")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        # min_length=1 admits "   "; an all-whitespace identifier identifies
        # nothing and must not reach a lookup.
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed


class AdoptedProduct(BaseModel):
    """A PayPilot-owned record of one adopted external product.

    Persisted in the `adopted_products` table. Every field below was copied
    from server-stored discovery evidence at adoption time — this is a
    SNAPSHOT, not a live view, and it carries no payment authority.
    """

    adopted_product_id: str = Field(
        description="PayPilot's own id, e.g. 'adopt_<hex>'. Server-generated."
    )

    # --- provider identity (the deterministic uniqueness key) ---
    discovery_source: str = Field(
        description="Discovery provider the evidence came from."
    )
    discovery_external_id: str = Field(
        description="The provider's id for the OBSERVATION we adopted. "
        "Together with discovery_source it uniquely identifies this adopted "
        "record — never the title, the merchant, or the price. It is NOT by "
        "itself a promise that the id still means this product later: see "
        "product_identity_kind."
    )

    # --- durable identity (Phase 11B.1) ---
    product_identity_kind: ProductIdentityKind = Field(
        default=ProductIdentityKind.PROVIDER_OBSERVATION_ONLY,
        description="Whether discovery_external_id is durable product "
        "identity or only an observation id. Defaults to the safe value, so "
        "a record whose provenance is unknown is never assumed verifiable.",
    )
    product_identity_key: Optional[str] = Field(
        default=None,
        description="Provider-namespaced key a later revalidation may "
        "re-identify this product by, e.g. 'serper:8522572424499656344'. "
        "Null when the provider gave only an observation id — revalidation "
        "then reports IDENTITY_UNVERIFIABLE rather than matching on a "
        "title, merchant or price.",
    )

    # --- product identity, copied from stored evidence ---
    title: str
    description: Optional[str] = None
    brand: Optional[str] = None
    category: Optional[str] = None

    # --- merchant identity (Phase 10H), copied from stored evidence ---
    merchant_name: Optional[str] = Field(
        default=None, description="Seller as the provider named it."
    )
    merchant_domain: Optional[str] = Field(
        default=None,
        description="Merchant host the backend derived. Null when the "
        "product's link only resolves to a search aggregator.",
    )

    product_url: Optional[str] = Field(
        default=None,
        description="The retailer link the provider supplied, verbatim. A "
        "page to visit — not a checkout PayPilot controls.",
    )

    # --- OBSERVED discovery evidence. NOT a purchase price. ---
    observed_price: Optional[Decimal] = Field(
        default=None,
        description="Price a retailer was SEEN asking at observed_at. "
        "Discovery evidence only: never a payment amount, never a PayPal "
        "amount, and never an authorization to spend. A purchase phase must "
        "re-validate before any money moves.",
    )
    observed_currency: Optional[str] = Field(
        default=None, description="Currency observed_price was expressed in."
    )

    # --- DERIVED by PayPilot (Phase 10F). Also not a purchase price. ---
    normalized_price: Optional[Decimal] = Field(
        default=None,
        description="Price PayPilot derived from observed_price for "
        "comparison. Arithmetic on an observation — not a retailer's price "
        "and not a payment amount.",
    )
    normalized_currency: Optional[str] = Field(
        default=None, description="Currency normalized_price is expressed in."
    )

    # --- timestamps (ISO 8601 UTC strings, matching the ledger convention) ---
    observed_at: str = Field(
        description="When the discovery layer observed the offer. Carried "
        "over from the evidence; it is NOT the adoption time."
    )
    adopted_at: str = Field(
        description="When this record was created. Server-generated."
    )
    updated_at: str = Field(
        description="When this record last changed (status transitions)."
    )

    status: AdoptedProductStatus = Field(
        description="Lifecycle state. ACTIVE means adoption succeeded from "
        "valid evidence — it does NOT mean purchasable."
    )
