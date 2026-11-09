"""PayPal OAuth 2.0 authentication layer (Phase 1).

All PayPal-specific logic lives here. The only thing this module does for now
is obtain an OAuth 2.0 access token from the PayPal Sandbox using the
client-credentials grant. No secrets or tokens are ever logged.
"""

from __future__ import annotations

import logging
import os

from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv

# Load variables from backend/.env into the process environment.
load_dotenv()

logger = logging.getLogger("paypilot.paypal")

# Base URLs for each PayPal environment. Phase 1 uses sandbox only.
_API_BASES = {
    "sandbox": "https://api-m.sandbox.paypal.com",
    "live": "https://api-m.paypal.com",
}


class PayPalAuthError(Exception):
    """Raised when we cannot obtain an access token from PayPal."""


def _get_config() -> tuple[str, str, str]:
    """Read and validate PayPal settings from the environment.

    Returns (client_id, client_secret, api_base).
    Raises PayPalAuthError if credentials are missing.
    """
    client_id = os.getenv("PAYPAL_CLIENT_ID", "").strip()
    client_secret = os.getenv("PAYPAL_CLIENT_SECRET", "").strip()
    environment = os.getenv("PAYPAL_ENVIRONMENT", "sandbox").strip().lower()

    if not client_id or not client_secret:
        # Note: we report *that* a credential is missing, never its value.
        raise PayPalAuthError(
            "PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set in .env"
        )

    api_base = _API_BASES.get(environment, _API_BASES["sandbox"])
    return client_id, client_secret, api_base


def get_access_token() -> str:
    """Obtain an OAuth 2.0 access token from PayPal.

    Uses the client-credentials grant (HTTP Basic auth with the client ID and
    secret). This is the reusable function the rest of the app will call.

    Returns:
        The access token string.

    Raises:
        PayPalAuthError: if credentials are missing, the network call fails,
            or PayPal returns a non-200 response.
    """
    client_id, client_secret, api_base = _get_config()

    logger.info("Requesting PayPal access token from %s", api_base)

    try:
        # httpx handles the HTTP Basic auth header from the (id, secret) tuple.
        # We never put the secret in the URL, query string, or logs.
        response = httpx.post(
            f"{api_base}/v1/oauth2/token",
            data={"grant_type": "client_credentials"},
            auth=(client_id, client_secret),
            headers={"Accept": "application/json"},
            timeout=15.0,
        )
    except httpx.RequestError as exc:
        # Network-level failure (DNS, timeout, connection refused, ...).
        logger.error("Network error contacting PayPal: %s", exc)
        raise PayPalAuthError("Could not reach PayPal to obtain a token") from exc

    if response.status_code != 200:
        # Log the status code only — the body may echo request details.
        logger.error("PayPal token request failed: HTTP %s", response.status_code)
        raise PayPalAuthError(
            f"PayPal returned HTTP {response.status_code} for the token request"
        )

    payload = response.json()
    token = payload.get("access_token")
    if not token:
        logger.error("PayPal response did not contain an access_token")
        raise PayPalAuthError("PayPal response did not contain an access_token")

    # Success: log metadata that is safe to record, never the token itself.
    logger.info(
        "Obtained PayPal access token (type=%s, expires_in=%ss)",
        payload.get("token_type", "unknown"),
        payload.get("expires_in", "unknown"),
    )
    return token


class PayPalOrderError(Exception):
    """Raised when we cannot create an order with PayPal."""


class PayPalOrderNetworkError(PayPalOrderError):
    """A create-order request could not be completed at the TRANSPORT level
    (connection error / timeout), so the outcome is AMBIGUOUS: PayPal may or
    may not have created an order.

    A subclass of PayPalOrderError, so every existing `except PayPalOrderError`
    handler still catches it and behaves exactly as before (the create path
    still treats it as a definitive non-success and never retries). Callers
    that must distinguish an ambiguous transport outcome from a non-2xx /
    malformed response — the Phase 11F-K.22b operator-gated recovery — catch
    this more specific type and DELIBERATELY do not conclude "no order
    exists". It never widens what create_order does on its own."""


# ---------------------------------------------------------------------------
# Checkout URL configuration (Phase 11F-B).
#
# The buyer's return and cancel destinations used to be literals inside
# create_order. They are now server-side CONFIGURATION, read from the
# environment and validated every time they are read.
#
# THEY ARE NEVER AN INPUT. There is no parameter, keyword argument or
# request model anywhere in this path through which a client, a provider or
# a model could supply a return or cancel URL -- the values are read from
# the process environment and nowhere else. An open-redirect through this
# surface would hand an attacker a paypal.com-hosted page that bounces the
# buyer wherever they like, immediately after a payment, which is why the
# allowed shape is deliberately narrow and fails closed.
#
# ONE PAIR SERVES BOTH FLOWS. The catalog purchase flow and the Phase 11E
# external flow use the same configured pair. No per-call override and no
# flow argument exists, because either would be a place for a caller to pass
# request data; distinguishing the flows would also require a second
# frontend origin, which this phase is not permitted to introduce. A future
# phase that genuinely needs a distinct external return path should add a
# second server-side variable here, resolved by the same validator, and
# select it with a literal keyword at the (server-side) call site -- never
# with a value that originated in a request.
# ---------------------------------------------------------------------------

#: The literals create_order used before this phase. Keeping them as the
#: defaults means an unconfigured deployment behaves exactly as before.
DEFAULT_RETURN_URL = "https://example.com/paypilot/return"
DEFAULT_CANCEL_URL = "https://example.com/paypilot/cancel"

RETURN_URL_ENV = "PAYPAL_RETURN_URL"
CANCEL_URL_ENV = "PAYPAL_CANCEL_URL"

#: The ONLY scheme accepted. Not a stylistic preference: PayPal redirects a
#: buyer here straight after approving a payment, and http:// would expose
#: the order token in the clear, while any other scheme (javascript:, data:,
#: file:, intent: ...) is a redirect primitive rather than a destination.
_REQUIRED_SCHEME = "https"


class PayPalCheckoutConfigError(Exception):
    """A configured return/cancel URL is unusable, so no order is created.

    Deliberately NOT a subclass of PayPalOrderError. A deployment-time
    configuration fault is not an upstream PayPal failure, and conflating
    them would (a) report a misconfiguration as a 502 "PayPal is down" and
    (b) make the external purchase service burn an approval's single
    execution slot as permanently FAILED over a fixable typo.

    The message names the variable and what was wrong with its SHAPE. It
    never contains credentials, and the offending value is not echoed --
    a mis-pasted secret is the most likely way a credential ends up in one
    of these variables, so the value stays out of logs and responses.
    """


def _validate_checkout_url(env_name: str, value: str) -> str:
    """Return `value` if it is an absolute https:// URL, else raise.

    Accepts only a URL that urllib can parse, whose scheme is exactly
    "https" and which carries a network location. Everything else --
    a relative path, a scheme-relative "//host/path", http://, javascript:,
    data:, a bare word, a URL with no host, or anything urlsplit refuses --
    is rejected. Fails closed: there is no "best effort" repair, because a
    repaired redirect target is still a redirect target nobody reviewed.
    """
    # Whitespace and control characters are rejected outright, before
    # parsing. urlsplit happily accepts "https:// space in host" (netloc is
    # truthy, so a host check alone passes it), and a raw CR/LF in a value
    # that ends up in an outbound request is a header-injection shape. A
    # legitimate URL has neither: a real space is %20.
    if any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise PayPalCheckoutConfigError(
            f"{env_name} contains whitespace or control characters; a URL "
            f"must be percent-encoded (a space is %20)"
        )

    try:
        parts = urlsplit(value)
    except ValueError as exc:
        # urlsplit raises on a few malformed inputs (e.g. a bad IPv6 literal).
        raise PayPalCheckoutConfigError(
            f"{env_name} is not a parsable URL"
        ) from exc

    scheme = parts.scheme.lower()
    if not scheme:
        raise PayPalCheckoutConfigError(
            f"{env_name} must be an absolute https:// URL; a relative or "
            f"scheme-less value cannot be a PayPal redirect target"
        )
    if scheme != _REQUIRED_SCHEME:
        raise PayPalCheckoutConfigError(
            f"{env_name} must use the https scheme (got {scheme!r}); PayPal "
            f"redirects the buyer to this URL after approving a payment"
        )
    if not parts.netloc:
        raise PayPalCheckoutConfigError(
            f"{env_name} must include a host, e.g. https://example.com/return"
        )
    return value


def checkout_urls() -> tuple[str, str]:
    """The validated (return_url, cancel_url) for a PayPal checkout.

    Read from PAYPAL_RETURN_URL / PAYPAL_CANCEL_URL on every call, so a
    deployment change takes effect without a code change and a BROKEN one
    is caught at the moment it would be used rather than at import. An unset
    or whitespace-only variable falls back to the pre-11F-B literal, so an
    unconfigured deployment is byte-identical to the old behavior.

    The defaults go through the same validator as configured values: no
    value reaches PayPal unvalidated, including one baked into this module.

    Raises:
        PayPalCheckoutConfigError: either variable is set to something that
            is not an absolute https:// URL. No PayPal call is made.
    """
    return_url = os.getenv(RETURN_URL_ENV, "").strip() or DEFAULT_RETURN_URL
    cancel_url = os.getenv(CANCEL_URL_ENV, "").strip() or DEFAULT_CANCEL_URL
    return (
        _validate_checkout_url(RETURN_URL_ENV, return_url),
        _validate_checkout_url(CANCEL_URL_ENV, cancel_url),
    )


def _approval_url(links: object) -> str | None:
    """The buyer's approval URL from a PayPal `links` array, or None.

    Defensive by construction, because this parses a remote payload: a
    `links` value that is not a list, an entry that is not a dict, a missing
    or non-string `href`, a missing `rel` -- each is skipped, never raised
    on. The pre-11F-B code indexed link["href"] directly and would raise
    KeyError on a link object without one.

    Only "approve" (standard checkout) and "payer-action" (the rel PayPal
    returns for some flows) are treated as approval links, and the href must
    be an absolute https:// URL to be returned at all. A URL is NEVER
    constructed from an order id: if PayPal did not send a usable link, the
    answer is None and the caller must say so rather than guess a checkout
    address that may not exist.
    """
    if not isinstance(links, list):
        return None
    for link in links:
        if not isinstance(link, dict):
            continue
        rel = link.get("rel")
        if not isinstance(rel, str) or rel.lower() not in ("approve", "payer-action"):
            continue
        href = link.get("href")
        if not isinstance(href, str) or not href.strip():
            continue
        try:
            parts = urlsplit(href.strip())
        except ValueError:
            continue
        if parts.scheme.lower() == _REQUIRED_SCHEME and parts.netloc:
            return href.strip()
    return None


def _safe_links(links: object) -> list[dict]:
    """PayPal's `links` array, filtered to entries safe to hand onward.

    Keeps only dict entries whose href is an absolute https:// URL, and
    keeps only the three fields that describe a link. Any other key PayPal
    may add is dropped rather than forwarded, so this cannot become an
    accidental passthrough for provider payload we have not reviewed.
    """
    safe: list[dict] = []
    if not isinstance(links, list):
        return safe
    for link in links:
        if not isinstance(link, dict):
            continue
        href = link.get("href")
        rel = link.get("rel")
        if not isinstance(href, str) or not isinstance(rel, str):
            continue
        try:
            parts = urlsplit(href.strip())
        except ValueError:
            continue
        if parts.scheme.lower() != _REQUIRED_SCHEME or not parts.netloc:
            continue
        method = link.get("method")
        safe.append(
            {
                "rel": rel,
                "href": href.strip(),
                "method": method if isinstance(method, str) else None,
            }
        )
    return safe


#: Maximum length PayPal documents for the PayPal-Request-Id header on the
#: Orders v2 / Payments v2 APIs. A conservative, documented ceiling used to
#: validate server-generated idempotency keys before they are sent. (The
#: Orders v2 six-hour idempotency retention window is a SEPARATE concern,
#: relevant only to future retry/recovery planning, not to this phase.)
PAYPAL_REQUEST_ID_MAX_LENGTH = 108


def validate_paypal_request_id(value: str) -> str:
    """Return a header-safe PayPal-Request-Id, or raise ValueError.

    Enforces the documented constraints on a server-generated idempotency key:
    non-empty after stripping, at most PAYPAL_REQUEST_ID_MAX_LENGTH characters,
    and only printable ASCII with no whitespace or control characters (so it is
    safe to place verbatim in an HTTP header). Deterministic and side-effect
    free; it makes no network call. Callers derive the value from a unique
    per-operation identifier and validate it before persisting/sending.
    """
    candidate = (value or "").strip()
    if not candidate:
        raise ValueError("PayPal-Request-Id must be a non-empty string")
    if len(candidate) > PAYPAL_REQUEST_ID_MAX_LENGTH:
        raise ValueError(
            f"PayPal-Request-Id must be at most {PAYPAL_REQUEST_ID_MAX_LENGTH} "
            f"characters (got {len(candidate)})"
        )
    if any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in candidate):
        raise ValueError(
            "PayPal-Request-Id must contain only printable ASCII with no "
            "whitespace"
        )
    return candidate


# ---------------------------------------------------------------------------
# Create-order idempotency recovery window (Phase 11F-K.22b).
#
# PayPal's Orders v2 API deduplicates a POST that carries a PayPal-Request-Id
# it has already seen WITHIN a documented retention period: a replay of the
# SAME request id returns the SAME order (if one was created) rather than
# creating a second. The DOCUMENTED default retention is six hours. We DO NOT
# assume any negotiated extension.
#
# Operator-gated recovery (K.22b) replays a stuck create-order ONLY while the
# attempt is demonstrably still inside a CONSERVATIVE window strictly below
# that six hours, with a meaningful safety margin, so even with clock skew and
# the delay between the original call and the replay we stay inside PayPal's
# retention. The reservation's created_at is used as a CONSERVATIVE lower bound
# on the age: the original create-order POST happened at or after created_at,
# so the true age at PayPal is never greater than (now - created_at). If that
# computed age is inside the window, the true age is too.
# ---------------------------------------------------------------------------

#: PayPal's DOCUMENTED Orders v2 idempotency retention period. A constant for
#: documentation and for bounding the configurable window; never itself used
#: as the recovery window.
PAYPAL_IDEMPOTENCY_RETENTION_SECONDS = 6 * 60 * 60  # 21600s, documented

#: The CONSERVATIVE default recovery window: three hours. That is half of the
#: documented six-hour retention, i.e. a three-hour safety margin below it.
DEFAULT_CREATE_ORDER_RECOVERY_WINDOW_SECONDS = 3 * 60 * 60  # 10800s

#: The absolute ceiling an operator may configure the window to. Kept a full
#: hour below the documented retention so a configured value can never erase
#: the margin entirely; anything above it falls back to the conservative
#: default rather than being honored.
MAX_CREATE_ORDER_RECOVERY_WINDOW_SECONDS = (
    PAYPAL_IDEMPOTENCY_RETENTION_SECONDS - 60 * 60  # 18000s (5h)
)

CREATE_ORDER_RECOVERY_WINDOW_ENV_VAR = "PAYPAL_CREATE_ORDER_RECOVERY_WINDOW_SECONDS"

#: Benign clock-skew tolerance when deciding a timestamp is "in the future".
#: A created_at a few seconds ahead of our clock is skew, not corruption; a
#: timestamp further ahead is rejected (never silently trusted).
RECOVERY_FUTURE_SKEW_TOLERANCE_SECONDS = 5


def create_order_recovery_window_seconds() -> int:
    """The configured conservative create-order recovery window, in seconds.

    Read from PAYPAL_CREATE_ORDER_RECOVERY_WINDOW_SECONDS at call time (so a
    deployment change needs no code change and no restart), bounded to
    (0, MAX_CREATE_ORDER_RECOVERY_WINDOW_SECONDS]. A missing, non-integer,
    non-positive, or over-ceiling value falls back to the conservative default
    with a warning — the window is a SAFETY bound, so a misconfiguration never
    silently WIDENS it past the margin; it fails safe to the default.
    """
    raw = os.getenv(CREATE_ORDER_RECOVERY_WINDOW_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_CREATE_ORDER_RECOVERY_WINDOW_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "%s is not an integer (%r); using the conservative default %ss",
            CREATE_ORDER_RECOVERY_WINDOW_ENV_VAR,
            raw,
            DEFAULT_CREATE_ORDER_RECOVERY_WINDOW_SECONDS,
        )
        return DEFAULT_CREATE_ORDER_RECOVERY_WINDOW_SECONDS
    if value <= 0:
        logger.warning(
            "%s must be positive (got %s); using the conservative default %ss",
            CREATE_ORDER_RECOVERY_WINDOW_ENV_VAR,
            value,
            DEFAULT_CREATE_ORDER_RECOVERY_WINDOW_SECONDS,
        )
        return DEFAULT_CREATE_ORDER_RECOVERY_WINDOW_SECONDS
    if value > MAX_CREATE_ORDER_RECOVERY_WINDOW_SECONDS:
        logger.warning(
            "%s=%s exceeds the conservative ceiling %ss (kept below PayPal's "
            "documented %ss retention); using the default %ss instead",
            CREATE_ORDER_RECOVERY_WINDOW_ENV_VAR,
            value,
            MAX_CREATE_ORDER_RECOVERY_WINDOW_SECONDS,
            PAYPAL_IDEMPOTENCY_RETENTION_SECONDS,
            DEFAULT_CREATE_ORDER_RECOVERY_WINDOW_SECONDS,
        )
        return DEFAULT_CREATE_ORDER_RECOVERY_WINDOW_SECONDS
    return value


def classify_recovery_window(
    created_at: str,
    *,
    now: Optional[datetime] = None,
    window_seconds: Optional[int] = None,
) -> tuple[bool, str, Optional[float]]:
    """Decide whether a reservation's age admits a create-order replay.

    Pure and side-effect free: parses an ISO-8601 reservation timestamp and
    compares its age to the conservative recovery window. It makes NO network
    call and reads no configuration beyond the window.

    Returns (eligible, reason, age_seconds). reason is one of:
      ELIGIBLE                the timestamp is valid, not in the future, and
                              its age is strictly inside the window.
      TIMESTAMP_MALFORMED     missing, or not a parseable ISO-8601 timestamp.
      TIMESTAMP_NOT_TZ_AWARE  parseable but carries no timezone/offset, so its
                              instant is ambiguous and is never guessed.
      TIMESTAMP_IN_FUTURE     dated after now (beyond a small skew tolerance).
      WINDOW_EXPIRED          older than the window; a replay could fall
                              outside PayPal's idempotency retention.

    A non-ELIGIBLE reason always fails closed (no replay).
    """
    window = (
        window_seconds
        if window_seconds is not None
        else create_order_recovery_window_seconds()
    )
    if not isinstance(created_at, str) or not created_at.strip():
        return False, "TIMESTAMP_MALFORMED", None
    try:
        parsed = datetime.fromisoformat(created_at.strip())
    except ValueError:
        return False, "TIMESTAMP_MALFORMED", None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        # A naive timestamp's real instant depends on an assumed zone; we never
        # assume one, so recovery cannot establish the age safely.
        return False, "TIMESTAMP_NOT_TZ_AWARE", None
    current = now if now is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age = (current - parsed).total_seconds()
    if age < -RECOVERY_FUTURE_SKEW_TOLERANCE_SECONDS:
        return False, "TIMESTAMP_IN_FUTURE", age
    if age > window:
        return False, "WINDOW_EXPIRED", age
    return True, "ELIGIBLE", age


def create_order(
    amount: str,
    currency: str = "USD",
    description: str | None = None,
    request_id: str | None = None,
) -> dict:
    """Create a PayPal order via the Orders v2 API.

    Flow: obtain an OAuth token, then POST to /v2/checkout/orders with a single
    purchase unit for the given amount and currency. Returns the trimmed order
    object (id, status, and links) so the frontend can drive the next step.

    Args:
        amount: The order total as a string, e.g. "50.00".
        currency: ISO-4217 currency code, e.g. "USD".
        description: Optional purchase-unit description shown at checkout
            (PayPal caps it at 127 characters; longer values are truncated).
        request_id: Optional stable idempotency key sent as PayPal-Request-Id
            (Phase 11F-K.22a). When omitted, no such header is sent and
            behavior is unchanged. Validated before use.

    Returns:
        A dict with the PayPal "order_id", "status", and "approve_url"
        (the sandbox/live checkout link the buyer must visit to approve).

    Raises:
        PayPalAuthError: if a token cannot be obtained.
        PayPalOrderError: if the create-order call fails.
    """
    # Checkout configuration FIRST: a misconfigured return/cancel URL must
    # refuse before a token is fetched and long before an order exists, so a
    # deployment typo can never produce an order the buyer cannot return
    # from. PayPalCheckoutConfigError is not a PayPalOrderError, so callers
    # that treat upstream failures as terminal do not burn state over it.
    return_url, cancel_url = checkout_urls()

    # Reuse the existing auth layer; its errors propagate as PayPalAuthError.
    token = get_access_token()
    _, _, api_base = _get_config()

    purchase_unit: dict = {"amount": {"currency_code": currency, "value": amount}}
    if description:
        purchase_unit["description"] = description[:127]

    body = {
        "intent": "CAPTURE",
        "purchase_units": [purchase_unit],
        # Checkout configuration. Without this, PayPal has no return_url to send
        # the buyer back to after they approve, so the hosted page ends on a
        # generic error screen. We also skip shipping (this is a digital demo
        # product) and ask for a "Pay Now" button so approval completes cleanly.
        # The two URLs are server-side configuration, validated above; they
        # default to the pre-11F-B literals and are never client-supplied.
        "application_context": {
            "brand_name": "PayPilot",
            "shipping_preference": "NO_SHIPPING",
            "user_action": "PAY_NOW",
            "return_url": return_url,
            "cancel_url": cancel_url,
        },
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    # Phase 11F-K.22a: when the caller supplies a stable idempotency key, send
    # it as PayPal-Request-Id so a replay of the SAME logical create-order is
    # idempotent at the provider (within PayPal's retention window). Validated
    # here too, so an unusable key fails closed before the call rather than
    # sending a malformed header. When omitted, behavior is exactly as before.
    if request_id is not None:
        headers["PayPal-Request-Id"] = validate_paypal_request_id(request_id)

    logger.info("Creating PayPal order (currency=%s)", currency)

    try:
        response = httpx.post(
            f"{api_base}/v2/checkout/orders",
            json=body,
            headers=headers,
            timeout=15.0,
        )
    except httpx.RequestError as exc:
        logger.error("Network error creating PayPal order: %s", exc)
        # Transport-level failure: the request may or may not have reached
        # PayPal and created an order, so this is AMBIGUOUS. The more specific
        # subclass lets operator-gated recovery avoid concluding "no order".
        raise PayPalOrderNetworkError(
            "Could not reach PayPal to create an order"
        ) from exc

    # PayPal returns 201 Created on success (200 is also acceptable).
    if response.status_code not in (200, 201):
        logger.error("PayPal create-order failed: HTTP %s", response.status_code)
        raise PayPalOrderError(
            f"PayPal returned HTTP {response.status_code} for create-order"
        )

    # A 2xx with a non-JSON / malformed body must never leak a raw
    # JSON-decoding exception to the caller: it is a create-order failure like
    # any other, and recovery must be able to classify it conservatively
    # rather than crash. (A 2xx could still mean an order was created, which is
    # exactly why this is PayPalOrderError, never a "no order" signal.)
    try:
        payload = response.json()
    except ValueError as exc:
        logger.error("PayPal create-order response was not valid JSON")
        raise PayPalOrderError(
            "PayPal returned a malformed (non-JSON) create-order response"
        ) from exc
    if not isinstance(payload, dict):
        raise PayPalOrderError(
            "PayPal returned an unexpected create-order response shape"
        )
    order_id = payload.get("id")
    if not order_id:
        logger.error("PayPal response did not contain an order id")
        raise PayPalOrderError("PayPal response did not contain an order id")

    # Pull out the approval link — the checkout URL the buyer visits to pay.
    # Parsed defensively (see _approval_url): a links array that is missing,
    # not a list, or holds an entry without an href yields None rather than
    # raising, and a non-https href is not returned. No URL is ever built
    # from the order id.
    approve_url = _approval_url(payload.get("links"))

    logger.info("Created PayPal order id=%s status=%s", order_id, payload.get("status"))
    return {
        "order_id": order_id,
        "status": payload.get("status"),
        "approve_url": approve_url,
    }


class PayPalCaptureError(Exception):
    """Raised when we cannot capture an approved order."""


class PayPalOrderNotApprovedError(Exception):
    """Raised when capture is attempted before the buyer approved (HTTP 422)."""


class PayPalAlreadyCapturedError(Exception):
    """Raised when the order has already been captured (HTTP 422).

    Capture is NOT idempotent from our side: a second capture attempt for the
    same order surfaces PayPal's ORDER_ALREADY_CAPTURED as this error — we
    never fabricate a local "success" for it.
    """


def capture_order(order_id: str) -> dict:
    """Capture payment for a previously approved PayPal order.

    Flow: obtain an OAuth token, then POST to
    /v2/checkout/orders/{order_id}/capture. This is the step where money
    actually moves — it only succeeds once the buyer has APPROVED the order
    on PayPal's hosted checkout page.

    Note the two-level status (see the order lifecycle): the *order* becomes
    COMPLETED, while the nested *capture* carries its own status that can be
    COMPLETED, PENDING, or DECLINED. We return both so callers never mistake a
    COMPLETED order for guaranteed-collected funds.

    Args:
        order_id: The PayPal order id returned by create_order.

    Returns:
        A dict with "order_id", the order "status" (e.g. "COMPLETED"),
        "capture_id", "capture_status", and the captured "amount"/"currency".

    Raises:
        ValueError: if order_id is empty/blank (no PayPal call is made).
        PayPalAuthError: if a token cannot be obtained.
        PayPalOrderNotFoundError: if PayPal has no such order (HTTP 404).
        PayPalOrderNotApprovedError: if the buyer has not approved it yet.
        PayPalAlreadyCapturedError: if the order was already captured.
        PayPalCaptureError: for any other capture failure.
    """
    if not order_id or not order_id.strip():
        raise ValueError("order_id must be a non-empty string")
    order_id = order_id.strip()

    # Reuse the existing auth layer; its errors propagate as PayPalAuthError.
    token = get_access_token()
    _, _, api_base = _get_config()

    logger.info("Capturing PayPal order id=%s", order_id)

    try:
        response = httpx.post(
            f"{api_base}/v2/checkout/orders/{order_id}/capture",
            # An empty JSON body is the documented request for a plain capture.
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            timeout=15.0,
        )
    except httpx.RequestError as exc:
        logger.error("Network error capturing PayPal order: %s", exc)
        raise PayPalCaptureError("Could not reach PayPal to capture the order") from exc

    # An unknown order id is a distinct, expected case → not-found.
    if response.status_code == 404:
        logger.info("PayPal order not found for capture: id=%s", order_id)
        raise PayPalOrderNotFoundError(f"No PayPal order found with id {order_id}")

    if response.status_code not in (200, 201):
        # The capture error body carries no secrets — just PayPal's own
        # diagnostic name (e.g. ORDER_NOT_APPROVED). Surfacing it makes the
        # CREATED-vs-APPROVED distinction obvious while developing.
        detail = _paypal_error_detail(response)

        # Two expected 422 states get their own exceptions so the API can
        # answer with a meaningful status instead of a generic upstream error.
        if response.status_code == 422 and "ORDER_ALREADY_CAPTURED" in detail:
            logger.info("Order already captured: id=%s", order_id)
            raise PayPalAlreadyCapturedError(
                f"Order {order_id} has already been captured"
            )
        if response.status_code == 422 and "ORDER_NOT_APPROVED" in detail:
            logger.info("Order not approved yet: id=%s", order_id)
            raise PayPalOrderNotApprovedError(
                f"Order {order_id} has not been approved by the buyer yet"
            )

        logger.error("PayPal capture failed: HTTP %s", response.status_code)
        raise PayPalCaptureError(
            f"PayPal returned HTTP {response.status_code} for capture{detail}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        logger.error("PayPal capture response was not valid JSON")
        raise PayPalCaptureError(
            "PayPal returned a malformed (non-JSON) capture response"
        ) from exc
    if not isinstance(payload, dict):
        raise PayPalCaptureError("PayPal returned an unexpected capture response shape")

    order_status = payload.get("status")

    # The capture itself is nested under the first purchase unit's payments.
    # Tolerate missing/empty lists so a malformed body degrades to None
    # fields instead of an unhandled IndexError.
    units = payload.get("purchase_units") or [{}]
    captures_list = (units[0].get("payments") or {}).get("captures") or [{}]
    capture = captures_list[0] if isinstance(captures_list[0], dict) else {}
    capture_id = capture.get("id")
    capture_status = capture.get("status")
    amount = capture.get("amount") or {}

    logger.info(
        "Captured order id=%s order_status=%s capture_status=%s",
        order_id,
        order_status,
        capture_status,
    )
    return {
        "order_id": order_id,
        "status": order_status,
        "capture_id": capture_id,
        "capture_status": capture_status,
        "amount": amount.get("value"),
        "currency": amount.get("currency_code"),
    }


def _paypal_error_detail(response: httpx.Response) -> str:
    """Extract PayPal's diagnostic error name/message for an error response.

    Returns a short "(NAME: message)" suffix, or "" if the body isn't the
    expected JSON shape. Never includes credentials — PayPal error bodies
    contain only an error name, a human message, and a debug id.
    """
    try:
        body = response.json()
    except ValueError:
        return ""
    name = body.get("name")
    message = body.get("message")
    # The actionable reason (e.g. ORDER_NOT_APPROVED) is often only in details[].
    issues = [d.get("issue") for d in body.get("details", []) if d.get("issue")]
    issue_suffix = f" [{', '.join(issues)}]" if issues else ""

    if name and message:
        return f" ({name}: {message}){issue_suffix}"
    if name:
        return f" ({name}){issue_suffix}"
    if issue_suffix:
        return issue_suffix
    return ""


class PayPalOrderNotFoundError(Exception):
    """Raised when PayPal has no order with the given id (HTTP 404)."""


class PayPalGetOrderError(Exception):
    """Raised when we cannot retrieve an order from PayPal."""


def get_order(order_id: str) -> dict:
    """Retrieve a PayPal order's current state via the Orders v2 API.

    Read-only: issues GET /v2/checkout/orders/{order_id} using the existing
    OAuth layer and returns a trimmed view of the order plus any capture
    information. Nothing is mutated — safe to call repeatedly.

    Args:
        order_id: The PayPal order id (from create_order).

    Returns:
        A dict with "order_id", "order_status", "intent", "purchase_units"
        (as PayPal returns them), and "captures" — a list of
        {id, status, amount} summarising any captures (empty if none yet).

        Phase 11F-B adds two ADDITIVE keys; every key above is unchanged, so
        existing consumers are unaffected:
          "links"       PayPal's own links array, filtered to entries with
                        an absolute https href and reduced to
                        {rel, href, method}. [] when there are none.
          "approve_url" The buyer's approval/payer-action URL, or None. This
                        is PayPal's own href — never a URL assembled from
                        the order id — and it is the value the external
                        purchase service recovers a lost checkout link from.

    Raises:
        PayPalAuthError: if a token cannot be obtained.
        PayPalOrderNotFoundError: if PayPal has no such order (HTTP 404).
        PayPalGetOrderError: for any other failure retrieving the order.
    """
    # Reuse the existing auth layer; its errors propagate as PayPalAuthError.
    token = get_access_token()
    _, _, api_base = _get_config()

    logger.info("Fetching PayPal order id=%s", order_id)

    try:
        response = httpx.get(
            f"{api_base}/v2/checkout/orders/{order_id}",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            timeout=15.0,
        )
    except httpx.RequestError as exc:
        logger.error("Network error retrieving PayPal order: %s", exc)
        raise PayPalGetOrderError(
            "Could not reach PayPal to retrieve the order"
        ) from exc

    # A missing order is a distinct, expected case → surface it as not-found
    # so the route can answer 404 rather than a generic upstream error.
    if response.status_code == 404:
        logger.info("PayPal order not found: id=%s", order_id)
        raise PayPalOrderNotFoundError(f"No PayPal order found with id {order_id}")

    if response.status_code != 200:
        logger.error("PayPal get-order failed: HTTP %s", response.status_code)
        detail = _paypal_error_detail(response)
        raise PayPalGetOrderError(
            f"PayPal returned HTTP {response.status_code} for get-order{detail}"
        )

    payload = response.json()
    purchase_units = payload.get("purchase_units", [])

    # Flatten any captures nested under each purchase unit's payments block.
    captures = [
        {
            "id": cap.get("id"),
            "status": cap.get("status"),
            "amount": cap.get("amount"),
        }
        for unit in purchase_units
        for cap in unit.get("payments", {}).get("captures", [])
    ]

    # ADDITIVE (Phase 11F-B): PayPal's own links, filtered to https entries,
    # plus the approval URL if one is present. Both are parsed defensively,
    # so a malformed or absent links array degrades to [] / None instead of
    # raising. Every pre-existing key below is unchanged, so a caller that
    # does not know about links is unaffected.
    links = _safe_links(payload.get("links"))
    approve_url = _approval_url(payload.get("links"))

    logger.info(
        "Fetched order id=%s status=%s captures=%d links=%d approval=%s",
        payload.get("id"),
        payload.get("status"),
        len(captures),
        len(links),
        "yes" if approve_url else "no",
    )
    return {
        "order_id": payload.get("id"),
        "order_status": payload.get("status"),
        "intent": payload.get("intent"),
        "purchase_units": purchase_units,
        "captures": captures,
        "links": links,
        "approve_url": approve_url,
    }


class PayPalCaptureNotFoundError(Exception):
    """Raised when PayPal has no capture with the given id (HTTP 404)."""


class PayPalAlreadyRefundedError(Exception):
    """Raised when the capture has already been fully refunded (HTTP 422)."""


class PayPalRefundError(Exception):
    """Raised for any other failure refunding a capture."""


class PayPalRefundNetworkError(PayPalRefundError):
    """Raised when the refund request could not be completed at the transport
    level (connection error / timeout), so the outcome is AMBIGUOUS: the
    refund may or may not have been applied at PayPal.

    A subclass of PayPalRefundError, so existing `except PayPalRefundError`
    handlers still catch it; callers that must distinguish an ambiguous
    outcome from a definitive rejection (e.g. the atomic refund claim) catch
    this more specific type first and DELIBERATELY do not auto-retry."""


def refund_capture(capture_id: str) -> dict:
    """Issue a FULL refund for a captured payment via the Payments v2 API.

    POSTs an empty JSON body to /v2/payments/captures/{capture_id}/refund,
    which PayPal treats as a full refund of the capture. Partial refunds
    (sending an amount object) are intentionally not implemented yet.

    Args:
        capture_id: The capture id returned by capture_order / get_order.

    Returns:
        A dict with "refund_id", "refund_status", "refunded_amount", and
        "currency".

    Raises:
        PayPalAuthError: if a token cannot be obtained.
        PayPalCaptureNotFoundError: if no such capture exists (HTTP 404).
        PayPalAlreadyRefundedError: if the capture is already fully refunded.
        PayPalRefundError: for any other failure.
    """
    # Reuse the existing auth layer; its errors propagate as PayPalAuthError.
    token = get_access_token()
    _, _, api_base = _get_config()

    logger.info("Refunding capture id=%s (full refund)", capture_id)

    try:
        response = httpx.post(
            f"{api_base}/v2/payments/captures/{capture_id}/refund",
            # Empty body => full refund (per PayPal Payments v2 docs).
            json={},
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                # Ask PayPal for the full refund object (incl. amount) rather
                # than the minimal id/status/links representation it returns
                # by default for an empty-body full refund.
                "Prefer": "return=representation",
            },
            timeout=15.0,
        )
    except httpx.RequestError as exc:
        logger.error("Network error refunding capture: %s", exc)
        # Transport-level failure: the refund may or may not have been applied,
        # so this is AMBIGUOUS, not a definitive rejection. The more specific
        # type lets the atomic refund claim avoid an unsafe automatic retry.
        raise PayPalRefundNetworkError(
            "Could not reach PayPal to refund the capture"
        ) from exc

    # Unknown capture id is a distinct, expected case → 404.
    if response.status_code == 404:
        logger.info("PayPal capture not found: id=%s", capture_id)
        raise PayPalCaptureNotFoundError(
            f"No PayPal capture found with id {capture_id}"
        )

    detail = _paypal_error_detail(response)

    # A fully-refunded capture comes back as 422 with this specific issue.
    if response.status_code == 422 and "CAPTURE_FULLY_REFUNDED" in detail:
        logger.info("Capture already fully refunded: id=%s", capture_id)
        raise PayPalAlreadyRefundedError(
            f"Capture {capture_id} has already been fully refunded"
        )

    # PayPal returns 201 Created on a successful refund.
    if response.status_code not in (200, 201):
        logger.error("PayPal refund failed: HTTP %s", response.status_code)
        raise PayPalRefundError(
            f"PayPal returned HTTP {response.status_code} for refund{detail}"
        )

    payload = response.json()
    amount = payload.get("amount", {})

    logger.info(
        "Refunded capture id=%s refund_id=%s status=%s",
        capture_id,
        payload.get("id"),
        payload.get("status"),
    )
    return {
        "refund_id": payload.get("id"),
        "refund_status": payload.get("status"),
        "refunded_amount": amount.get("value"),
        "currency": amount.get("currency_code"),
    }
