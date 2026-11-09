"""Adopted-product revalidation schemas (Phase 11B).

WHAT REVALIDATION ANSWERS
    "Does the provider still serve the exact product we adopted, and what
    does it say about it right now?"

    It does NOT answer "may we buy this?". No status here is a purchase
    decision, a policy outcome, or an authorization. Deciding whether a
    price change is acceptable belongs to Phase 11C; this phase only reports
    deterministically what changed.

OLD EVIDENCE vs FRESH EVIDENCE
    The two can never be confused, because they never share a field name.
    Everything the provider said just now is prefixed `current_`, and the
    adopted record's original snapshot appears only as the explicitly named
    `previous_observed_price` / `previous_observed_currency`. The adopted
    record itself is never overwritten: a revalidation produces a NEW
    snapshot row, so history accumulates instead of being rewritten.

IDENTITY IS THE GATE
    A result is accepted ONLY when its provider-native external_id equals
    the adopted product's. Title, merchant and price are never used to
    establish identity — they are exactly the fields an attacker or a
    reshuffled search ranking could change. See revalidation_service for
    the matching rule and its provider limitations.

NO PAYMENT, NO AI
    There is no PayPal field, no policy decision, no transaction id, no
    capture or refund id, and no AI-generated text anywhere in these models.
    A hallucinated or forged value has nowhere to land.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class RevalidationStatus(str, Enum):
    """Outcome of one revalidation attempt. None of these is a purchase state.

    REVALIDATED          The provider responded AND a result matched the
                         adopted product's provider identity exactly. Fresh
                         evidence in this snapshot is trustworthy. This does
                         NOT mean purchasable.
    IDENTITY_NOT_FOUND   The adopted product HAS a durable identity, the
                         provider responded normally, but nothing it
                         returned carried that identity. We refuse rather
                         than substitute a different product.
    IDENTITY_UNVERIFIABLE
                         The adopted product has NO durable identity at all
                         (the provider gave only an observation id), so no
                         amount of fresh data could confirm it. Reported
                         without contacting the provider, because a request
                         could not settle the question either way. This is
                         an honest limitation, never downgraded into a
                         match on title, merchant or price.
    PROVIDER_UNAVAILABLE A TRANSIENT provider failure (rate limit, 5xx,
                         timeout, network). Says nothing about the product;
                         the adopted record is deliberately left alone.
    PROVIDER_ERROR       A NON-TRANSIENT provider failure (auth rejected,
                         credits exhausted, request refused, unusable
                         response body). Retrying unchanged will not help.
    NOT_REVALIDATABLE    The attempt could not even be made: the original
                         provider is not configured here, or the adopted
                         record carries nothing usable to query with.
    """

    REVALIDATED = "REVALIDATED"
    IDENTITY_NOT_FOUND = "IDENTITY_NOT_FOUND"
    IDENTITY_UNVERIFIABLE = "IDENTITY_UNVERIFIABLE"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    NOT_REVALIDATABLE = "NOT_REVALIDATABLE"


class IdentityMatch(str, Enum):
    """How (or whether) the fresh result was tied to the adopted product.

    EXACT_EXTERNAL_ID  A returned result's provider-native external_id was
                       byte-equal to the adopted discovery_external_id. The
                       only accepted form of match.
    NOT_FOUND          The provider answered, but no result carried the
                       durable identity. Never downgraded to a title or
                       price match.
    UNVERIFIABLE       The adopted product has no durable identity, so
                       matching is not merely unsuccessful but impossible.
    NOT_ATTEMPTED      No matching was possible (provider failure, or the
                       attempt was never made).
    """

    EXACT_EXTERNAL_ID = "EXACT_EXTERNAL_ID"
    NOT_FOUND = "NOT_FOUND"
    UNVERIFIABLE = "UNVERIFIABLE"
    NOT_ATTEMPTED = "NOT_ATTEMPTED"


class PriceStatus(str, Enum):
    """Deterministic comparison of the adopted vs the fresh observed price.

    Exact Decimal comparison, same currency only. No float arithmetic, no
    tolerance band, and no conversion: comparing across currencies would
    require a rate, and a rate-dependent "unchanged" would be a fiction.

    PRICE_UNCHANGED      Both present, same currency, equal Decimals.
    PRICE_CHANGED        Both present, same currency, different Decimals.
    PRICE_UNAVAILABLE    No fresh observed price (the provider supplied
                         none, or there is no fresh evidence at all).
    PRICE_NOT_COMPARABLE Both sides have a price but in DIFFERENT
                         currencies, or the adopted record had no price to
                         compare against. Reported honestly rather than
                         guessed either way.
    """

    PRICE_UNCHANGED = "PRICE_UNCHANGED"
    PRICE_CHANGED = "PRICE_CHANGED"
    PRICE_UNAVAILABLE = "PRICE_UNAVAILABLE"
    PRICE_NOT_COMPARABLE = "PRICE_NOT_COMPARABLE"


class AvailabilityStatus(str, Enum):
    """Fresh availability, strictly as the provider stated it.

    AVAILABLE   The provider explicitly reported the item in stock.
    UNAVAILABLE The provider explicitly reported it out of stock.
    UNKNOWN     The provider said nothing about availability.

    UNKNOWN is NOT inferred from a missing price. Serper's shopping results
    carry no availability field at all, so Serper revalidations are always
    UNKNOWN — an honest "not stated", never a guess from other fields.
    """

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class MerchantStatus(str, Enum):
    """Whether the seller looks like the same one we adopted from.

    Informational only. A merchant change never fails revalidation: identity
    is the provider's external_id, and a retailer renaming itself or the
    provider restyling a display name is not a different product. Phase 11C
    decides whether such a change matters.

    MERCHANT_UNCHANGED Same merchant after whitespace/case normalization.
    MERCHANT_CHANGED   Both sides identify a merchant, and they differ.
    MERCHANT_UNKNOWN   Either side names no merchant, so nothing to compare.
    """

    MERCHANT_UNCHANGED = "MERCHANT_UNCHANGED"
    MERCHANT_CHANGED = "MERCHANT_CHANGED"
    MERCHANT_UNKNOWN = "MERCHANT_UNKNOWN"


class RevalidationReason(str, Enum):
    """A deterministic, machine-readable explanation. Never AI-written.

    Pairs with revalidation_status to say WHY, so a client never has to
    parse prose. Every value is produced by a single explicit code path.
    """

    IDENTITY_CONFIRMED = "IDENTITY_CONFIRMED"
    NO_RESULT_MATCHED_IDENTITY = "NO_RESULT_MATCHED_IDENTITY"
    PROVIDER_RETURNED_NO_RESULTS = "PROVIDER_RETURNED_NO_RESULTS"
    PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
    PROVIDER_UPSTREAM_FAILURE = "PROVIDER_UPSTREAM_FAILURE"
    PROVIDER_AUTH_REJECTED = "PROVIDER_AUTH_REJECTED"
    PROVIDER_QUOTA_EXHAUSTED = "PROVIDER_QUOTA_EXHAUSTED"
    PROVIDER_REJECTED_REQUEST = "PROVIDER_REJECTED_REQUEST"
    PROVIDER_RESPONSE_UNUSABLE = "PROVIDER_RESPONSE_UNUSABLE"
    PROVIDER_NOT_CONFIGURED = "PROVIDER_NOT_CONFIGURED"
    NO_USABLE_QUERY = "NO_USABLE_QUERY"
    NO_DURABLE_PRODUCT_IDENTITY = "NO_DURABLE_PRODUCT_IDENTITY"


class RevalidationSnapshot(BaseModel):
    """One persisted revalidation attempt. Append-only history.

    Self-contained on purpose: it carries the previous observed price
    alongside the fresh one, so a snapshot can be audited later without
    re-reading (and trusting) the adopted record as it stands then.
    """

    revalidation_id: str = Field(
        description="PayPilot's own id, e.g. 'reval_<hex>'. Server-generated."
    )
    adopted_product_id: str
    checked_at: str = Field(
        description="When the provider was contacted (ISO 8601 UTC). "
        "Server-generated; never supplied by a client or a provider."
    )

    # --- the identity that was revalidated (from the adopted record) ---
    discovery_source: str
    discovery_external_id: str = Field(
        description="The adopted OBSERVATION id. Durable only when "
        "identity_kind is PROVIDER_PRODUCT_ID."
    )
    identity_kind: Optional[str] = Field(
        default=None,
        description="The adopted product's ProductIdentityKind at check "
        "time (Phase 11B.1). Null on snapshots recorded before 11B.1.",
    )
    product_identity_key: Optional[str] = Field(
        default=None,
        description="The durable key matching was performed against, or "
        "null when there was none.",
    )

    # --- outcome ---
    revalidation_status: RevalidationStatus
    identity_match: IdentityMatch
    reason_code: RevalidationReason
    price_status: PriceStatus
    availability: AvailabilityStatus
    merchant_status: MerchantStatus

    # --- FRESH provider evidence. All null unless identity matched. ---
    current_title: Optional[str] = None
    current_merchant_name: Optional[str] = None
    current_merchant_domain: Optional[str] = Field(
        default=None,
        description="Merchant host the backend DERIVED from fresh evidence, "
        "null for an aggregator link. Never inferred from a merchant name.",
    )
    current_product_url: Optional[str] = Field(
        default=None, description="The provider's fresh link, verbatim."
    )
    current_observed_price: Optional[Decimal] = Field(
        default=None,
        description="Price the provider is asking NOW. Discovery evidence: "
        "not a payment amount and not an authorization to spend.",
    )
    current_observed_currency: Optional[str] = None
    current_normalized_price: Optional[Decimal] = Field(
        default=None,
        description="PayPilot-derived comparison figure for the fresh price. "
        "Arithmetic on an observation, not a retailer's price.",
    )
    current_normalized_currency: Optional[str] = None

    # --- the adopted record's ORIGINAL figures, for contrast ---
    previous_observed_price: Optional[Decimal] = Field(
        default=None,
        description="What the adopted snapshot recorded. Shown so the "
        "comparison is auditable; never overwritten by this phase.",
    )
    previous_observed_currency: Optional[str] = None


class RevalidationResponse(BaseModel):
    """Response body for POST /discovery/adopted/{id}/revalidate.

    The snapshot plus the adopted record's lifecycle status AFTER the
    attempt, so a client does not have to re-fetch to learn whether the
    adopted product was marked stale.
    """

    snapshot: RevalidationSnapshot
    adopted_product_status: str = Field(
        description="The adopted record's lifecycle status after this "
        "attempt (ACTIVE / STALE / DISABLED). A lifecycle state only — it "
        "never means purchasable."
    )
    adopted_status_changed: bool = Field(
        description="True when this revalidation transitioned the adopted "
        "record's status. A transient provider failure never does."
    )
