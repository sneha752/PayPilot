"""Single-operator authentication (Phase 11F-H).

A minimal, dependency-free authentication layer: one operator, a shared
login credential, and short-lived HMAC-SHA256 bearer tokens. It establishes
CALLER IDENTITY for the API; it is NOT multi-user isolation and must never
be described as such — there is exactly one principal, "operator".

WHAT IT IS
    - PAYPILOT_API_KEY     the operator's login credential, checked at
                           /auth/login with a constant-time comparison.
    - PAYPILOT_AUTH_SECRET a high-entropy secret that signs tokens. Never
                           sent to a client, never logged, never returned.
    A successful login mints a compact, JWT-shaped token
    (base64url(header).base64url(payload).base64url(hmac)) carrying an
    issuer, subject, issued-at and explicit expiry, signed with HMAC-SHA256
    over "header.payload". Every protected request presents it as
    `Authorization: Bearer <token>`; require_operator (in app.main) verifies
    the signature, issuer, subject and expiry before the route runs.

FAIL CLOSED
    Both environment values are REQUIRED. There is deliberately no universal
    default key and no default signing secret: if either is unset, login is
    a 503 and every protected route is a 401. A missing secret can never be
    silently substituted, because a predictable signing secret would let
    anyone forge a token.

NO SECRET EVER LEAVES THE SERVER
    The token is signed with the secret but never contains it; verification
    recomputes the HMAC. Neither the key nor the secret is placed in a
    response body, a log line, or a token payload.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger("paypilot.auth")

#: Token lifetime: ~30 minutes. No refresh tokens in this phase — when a
#: token expires the operator logs in again.
DEFAULT_TOKEN_TTL_SECONDS = 30 * 60

#: Fixed claims. The verifier requires both exactly, so a token minted for a
#: different purpose (or with a different subject) is rejected.
_ISSUER = "paypilot"
_SUBJECT = "operator"
_ALG = "HS256"

API_KEY_ENV = "PAYPILOT_API_KEY"
AUTH_SECRET_ENV = "PAYPILOT_AUTH_SECRET"


class AuthConfigError(Exception):
    """Required auth configuration is missing. Authentication fails closed."""


class InvalidCredentialError(Exception):
    """The supplied operator credential did not match."""


class InvalidTokenError(Exception):
    """A bearer token was missing, malformed, unsigned-correctly, issued for
    the wrong purpose, or expired. Every case is a 401 — the distinction is
    for logs only and is never leaked to the caller."""


# ---------------------------------------------------------------------------
# Configuration (read at call time, never cached, so tests and deployments
# can set it via the environment without import-order surprises).
# ---------------------------------------------------------------------------


def _api_key() -> str:
    value = os.getenv(API_KEY_ENV, "").strip()
    if not value:
        raise AuthConfigError(
            f"{API_KEY_ENV} is not set; operator login is disabled until it is"
        )
    return value


def _secret() -> bytes:
    value = os.getenv(AUTH_SECRET_ENV, "").strip()
    if not value:
        raise AuthConfigError(
            f"{AUTH_SECRET_ENV} is not set; token signing/verification is "
            f"disabled until it is"
        )
    return value.encode("utf-8")


def is_configured() -> bool:
    """True only when BOTH the operator key and the signing secret are set."""
    return bool(os.getenv(API_KEY_ENV, "").strip()) and bool(
        os.getenv(AUTH_SECRET_ENV, "").strip()
    )


# ---------------------------------------------------------------------------
# base64url without padding (stdlib only)
# ---------------------------------------------------------------------------


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(segment: str) -> bytes:
    pad = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + pad)


def _sign(signing_input: str, secret: bytes) -> bytes:
    return hmac.new(secret, signing_input.encode("ascii"), hashlib.sha256).digest()


# ---------------------------------------------------------------------------
# Issue / verify
# ---------------------------------------------------------------------------


def issue_token(
    ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
    *,
    now: Optional[int] = None,
) -> str:
    """Mint a signed operator token. Raises AuthConfigError if unconfigured.

    `now` is an override for deterministic tests only; production always uses
    the real clock.
    """
    secret = _secret()
    issued = int(now if now is not None else time.time())
    header = {"alg": _ALG, "typ": "PPT"}
    payload = {
        "iss": _ISSUER,
        "sub": _SUBJECT,
        "iat": issued,
        "exp": issued + int(ttl_seconds),
    }
    h = _b64e(json.dumps(header, separators=(",", ":"), sort_keys=True).encode())
    p = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signing_input = f"{h}.{p}"
    sig = _b64e(_sign(signing_input, secret))
    return f"{signing_input}.{sig}"


def login(api_key: str) -> str:
    """Verify the operator credential and mint a token.

    Raises:
        AuthConfigError: the server is not configured for auth.
        InvalidCredentialError: the credential did not match.
    """
    expected = _api_key()  # AuthConfigError if unset
    # Constant-time comparison so a wrong key cannot be found byte-by-byte.
    if not hmac.compare_digest((api_key or "").encode("utf-8"), expected.encode("utf-8")):
        raise InvalidCredentialError("Invalid operator credential")
    logger.info("Operator authenticated; issuing a bearer token")
    return issue_token()


def verify_token(token: str, *, now: Optional[int] = None) -> dict:
    """Validate a bearer token strictly. Returns its claims, or raises.

    Checks, in order: three dot-separated segments; a signature that matches
    an HMAC recomputed over "header.payload" (constant-time); a decodable
    JSON payload; the exact issuer and subject; and an integer expiry still
    in the future. Any failure raises InvalidTokenError — the caller maps
    every case to 401 without revealing which check failed.
    """
    secret = _secret()  # AuthConfigError if unset -> fail closed upstream
    if not isinstance(token, str) or token.count(".") != 2:
        raise InvalidTokenError("Token is malformed")
    header_seg, payload_seg, sig_seg = token.split(".")
    if not header_seg or not payload_seg or not sig_seg:
        raise InvalidTokenError("Token is malformed")

    expected_sig = _sign(f"{header_seg}.{payload_seg}", secret)
    try:
        provided_sig = _b64d(sig_seg)
    except (ValueError, binascii.Error) as exc:
        raise InvalidTokenError("Token signature is malformed") from exc
    if not hmac.compare_digest(expected_sig, provided_sig):
        raise InvalidTokenError("Token signature does not match")

    try:
        payload = json.loads(_b64d(payload_seg))
    except (ValueError, binascii.Error) as exc:
        raise InvalidTokenError("Token payload is malformed") from exc
    if not isinstance(payload, dict):
        raise InvalidTokenError("Token payload is not an object")

    if payload.get("iss") != _ISSUER or payload.get("sub") != _SUBJECT:
        raise InvalidTokenError("Token issuer/subject is not recognized")

    exp = payload.get("exp")
    if not isinstance(exp, int):
        raise InvalidTokenError("Token has no usable expiry")
    current = int(now if now is not None else time.time())
    if current >= exp:
        raise InvalidTokenError("Token has expired")

    return payload
