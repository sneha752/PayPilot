"""Deterministic merchant identity for discovered products (Phase 10H).

WHY THIS MODULE EXISTS
    A Serper (Google Shopping) result names the real seller in its `source`
    field ("Amazon", "Best Buy") but links through a Google Shopping URL, so
    the link's host is www.google.com. Reading that host as the retailer
    tells the user Google is selling the product, which is false. Equally,
    Serper's merchant name must not be turned into a domain by guesswork:
    "Amazon" does not entitle anyone to write amazon.in.

    So merchant identity is split into exactly two independent values, each
    with its own provenance:

      merchant_name    Provider EVIDENCE. Whatever the provider called the
                       seller, carried over verbatim from MerchantEvidence.
      merchant_domain  PayPilot-DERIVED, from evidence the product already
                       carries (retailer_domain, else product_url).

    Neither is ever computed from the other. There is no name-to-domain
    table, no domain-to-name table, and no lookup of either anywhere in
    this module.

HOW merchant_domain IS DERIVED
    1. Take retailer_domain; if it yields no usable host, take product_url.
    2. Normalize the host: lowercase, drop userinfo, drop the port, drop a
       trailing root dot, drop one leading "www.". A host with no dot is not
       a usable public merchant identity and yields null.
    3. If the normalized host belongs to a search aggregator or redirector
       (any google.* host, or serper.dev), it is NOT the merchant: yield
       null. A link that merely passes through Google says nothing about
       who the seller is, and inventing one would be worse than admitting
       we do not know.
    4. Otherwise that host IS the merchant domain.

    Channel3 supplies the real retailer host on the offer it selected, so
    step 1 already yields the right answer there; the aggregator check
    simply never fires.

NO EXTRA REQUESTS
    Pure functions over data already in hand. No HTTP, no redirect
    following, no scraping, no DNS, no AI call, and no external lookup of
    any kind happens here. Resolving a Google Shopping redirect to its
    merchant would require a network request, so it is deliberately not
    done: the domain stays null and the name still identifies the seller.

OBSERVED DATA IS UNTOUCHED
    This module writes merchant_name and merchant_domain and nothing else.
    observed_price, observed_currency, product_url, retailer_domain,
    observed_at and every other evidence field are carried through exactly
    as received, and attach_merchant_identity returns new instances rather
    than mutating its input.

NO PAYMENT AUTHORITY
    Merchant identity is display and provenance information for research
    output. It is not a payee, not a payment destination, not a Policy
    Engine input, and it can never become a PayPal, capture, or refund
    amount. Nothing in this module imports paypal, policy, the ledger, or
    the catalog.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable, Optional, Sequence
from urllib.parse import urlparse

from ..schemas.discovery import (
    DiscoveredProduct,
    MerchantEvidence,
    NormalizedDiscoveredProduct,
)

logger = logging.getLogger("paypilot.discovery_merchant")

#: Fields this module is permitted to write. Everything else on a product is
#: evidence or Phase 10F price derivation and is carried through untouched.
MERCHANT_FIELDS = ("merchant_name", "merchant_domain")

#: Hosts (and their subdomains) that aggregate or redirect to OTHER sellers.
#: A product link on one of these does not identify a merchant.
AGGREGATOR_SUFFIXES = frozenset({"serper.dev"})

#: Google's country domains, which are the aggregator in practice for the
#: Serper provider: google.com, www.google.com, shopping.google.com,
#: google.co.in, google.com.au. Matched structurally rather than by a list
#: of literals, because the ccTLD set is open-ended. "mygoogle.com" and
#: "googleadservices.com" deliberately do NOT match: the label boundary is
#: required.
_GOOGLE_HOST_RE = re.compile(r"^(?:[a-z0-9-]+\.)*google(?:\.[a-z]{2,3}){1,2}$")


def is_aggregator_host(host: Optional[str]) -> bool:
    """True when `host` is a search aggregator/redirector, not a merchant.

    Expects an already-normalized host (see normalize_host). Returns False
    for None so an absent host is simply "unknown", not "aggregator".
    """
    if not host:
        return False
    if _GOOGLE_HOST_RE.match(host):
        return True
    return any(
        host == suffix or host.endswith("." + suffix)
        for suffix in AGGREGATOR_SUFFIXES
    )


def normalize_host(raw: Optional[str]) -> Optional[str]:
    """Reduce a domain or URL to a bare lowercase host, or None.

    Accepts either form because retailer_domain is already a host while
    product_url is a full URL. Returns None for anything that is not a
    usable public domain (blank, no dot, an IPv6 literal, a bare label).
    This is normalization of data we were given, never enrichment.
    """
    if raw is None or not str(raw).strip():
        return None

    host = str(raw).strip().lower()
    if "//" in host:
        try:
            host = urlparse(host).netloc or ""
        except ValueError:
            return None
    if "/" in host:  # a path was supplied without a scheme
        host = host.split("/", 1)[0]
    if "@" in host:  # strip userinfo
        host = host.rsplit("@", 1)[1]
    if host.startswith("["):  # IPv6 literal: not a merchant identity
        return None
    host = host.split(":", 1)[0]  # strip port
    host = host.strip().rstrip(".")  # trailing root dot
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host:
        return None
    return host


def derive_merchant_domain(product: DiscoveredProduct) -> Optional[str]:
    """The merchant's domain, or None when it is not deterministic.

    Tries retailer_domain first (the provider's own statement of the offer's
    host), then product_url. An aggregator host is rejected rather than
    returned, and nothing is ever derived from merchant_name or brand.
    """
    for candidate in (product.retailer_domain, product.product_url):
        host = normalize_host(candidate)
        if host is None:
            continue
        if is_aggregator_host(host):
            # A pass-through link. Keep looking, but never fall back to a
            # guess: if nothing else qualifies, the answer is None.
            continue
        return host
    return None


def clean_merchant_name(raw: Optional[str]) -> Optional[str]:
    """Trim a provider-supplied merchant name; blank becomes None.

    Whitespace trimming is the ONLY transformation. The name is not
    title-cased, expanded, abbreviated, matched against a known-retailer
    list, or replaced by a domain.
    """
    if not isinstance(raw, str):
        return None
    return raw.strip() or None


def merchant_names_by_external_id(
    evidence: Iterable[MerchantEvidence],
) -> dict[str, str]:
    """Index provider merchant evidence by the product id it names.

    A duplicated external_id keeps the FIRST non-blank name, so the result
    is deterministic for a given provider response rather than dependent on
    iteration luck. Records with no usable name contribute nothing.
    """
    names: dict[str, str] = {}
    for record in evidence:
        name = clean_merchant_name(record.merchant_name)
        if name is None:
            continue
        names.setdefault(record.external_id, name)
    return names


def attach_merchant_identity(
    products: Sequence[NormalizedDiscoveredProduct],
    evidence: Iterable[MerchantEvidence] = (),
) -> list[NormalizedDiscoveredProduct]:
    """Return copies carrying resolved merchant identity.

    Both merchant fields are set EXPLICITLY on every product, so a value
    that arrived on the input (from a provider, a client payload, or an
    earlier pass) is always replaced by this module's own determination and
    can never survive unexamined.

    The input products are not mutated; observed and Phase 10F price fields
    are copied through unchanged.
    """
    names = merchant_names_by_external_id(evidence)

    resolved: list[NormalizedDiscoveredProduct] = []
    named = 0
    domained = 0
    for product in products:
        merchant_name = names.get(product.external_id)
        merchant_domain = derive_merchant_domain(product)
        named += merchant_name is not None
        domained += merchant_domain is not None
        resolved.append(
            product.model_copy(
                update={
                    "merchant_name": merchant_name,
                    "merchant_domain": merchant_domain,
                }
            )
        )

    if products:
        logger.info(
            "Merchant identity resolved for %d product(s): %d named, "
            "%d with a deterministic domain",
            len(resolved),
            named,
            domained,
        )
    return resolved
