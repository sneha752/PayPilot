"""PayPal webhook signature verification service (Phase 8A).

Verification ONLY. This module receives a raw webhook event plus PayPal's
transmission headers and asks PayPal's official verification endpoint
(POST /v1/notifications/verify-webhook-signature) whether the delivery is
authentic. A webhook is trusted only after PayPal answers SUCCESS.

It never mutates the transaction ledger, never captures or refunds, and
never calls an AI provider — ledger updates from webhooks are Phase 8B.

Nothing client-supplied can short-circuit verification: any "verified"
field inside the payload is ignored because this module never reads one.
No secrets, tokens, signatures, or full payloads are ever logged.
"""

from __future__ import annotations

import logging
import os
from typing import Mapping

import httpx

# Reuse the existing PayPal OAuth layer (same package): the verification call
# authenticates with the same Sandbox token infrastructure as orders/captures.
from ..paypal import get_access_token, _get_config

logger = logging.getLogger("paypilot.webhooks")

# Event types this phase knows how to NAME. Recognition only — no state
# changes happen for any of them (that is Phase 8B). An unknown type on a
# correctly signed webhook is still a verified webhook.
KNOWN_EVENT_TYPES = frozenset(
    {
        "PAYMENT.CAPTURE.COMPLETED",
        "PAYMENT.CAPTURE.DENIED",
        "PAYMENT.CAPTURE.REFUNDED",
        "CHECKOUT.ORDER.APPROVED",
    }
)

# PayPal transmission headers required for verification, mapped to the field
# names the verify-webhook-signature API expects. All five are mandatory —
# without any one of them PayPal cannot verify, so the delivery is rejected.
REQUIRED_HEADER_FIELDS = {
    "paypal-transmission-id": "transmission_id",
    "paypal-transmission-time": "transmission_time",
    "paypal-transmission-sig": "transmission_sig",
    "paypal-cert-url": "cert_url",
    "paypal-auth-algo": "auth_algo",
}


class MissingWebhookHeadersError(Exception):
    """Raised when one or more required PAYPAL-* headers are absent/blank."""

    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__(
            "Missing required PayPal webhook header(s): " + ", ".join(missing)
        )


class WebhookConfigError(Exception):
    """Raised when PAYPAL_WEBHOOK_ID is not configured on the server."""


class WebhookVerificationError(Exception):
    """Raised when PayPal's verification API cannot be consulted or returns
    an unusable response. This is an upstream failure — it is NEVER treated
    as a successful verification."""


def verify_webhook(event: dict, headers: Mapping[str, str]) -> dict:
    """Verify a PayPal webhook delivery via PayPal's official API.

    Args:
        event: The webhook event exactly as PayPal posted it (parsed JSON).
        headers: The request headers (case-insensitive lookup is handled
            here, so a plain dict works too).

    Returns:
        A clean internal result:
        {
            "verified": bool,            # True only on PayPal SUCCESS
            "event_type": str | None,    # e.g. "PAYMENT.CAPTURE.COMPLETED"
            "event_id": str | None,
            "known_event_type": bool,    # recognized by KNOWN_EVENT_TYPES
        }

    Raises:
        MissingWebhookHeadersError: a required PAYPAL-* header is missing.
        WebhookConfigError: PAYPAL_WEBHOOK_ID is not set.
        PayPalAuthError: an OAuth token could not be obtained.
        WebhookVerificationError: the verification API call itself failed.
    """
    # Case-insensitive header extraction; values must be non-blank.
    lowered = {str(k).lower(): v for k, v in headers.items()}
    verify_fields: dict[str, str] = {}
    missing: list[str] = []
    for header, field in REQUIRED_HEADER_FIELDS.items():
        value = (lowered.get(header) or "").strip()
        if not value:
            missing.append(header.upper())
        else:
            verify_fields[field] = value
    if missing:
        raise MissingWebhookHeadersError(missing)

    webhook_id = os.getenv("PAYPAL_WEBHOOK_ID", "").strip()
    if not webhook_id:
        # Report that the setting is missing, never any credential value.
        raise WebhookConfigError(
            "PAYPAL_WEBHOOK_ID must be set in .env to verify webhooks"
        )

    # Reuse the existing auth layer; its errors propagate as PayPalAuthError.
    token = get_access_token()
    _, _, api_base = _get_config()

    body = {
        **verify_fields,
        "webhook_id": webhook_id,
        "webhook_event": event,
    }

    event_type = event.get("event_type")
    # Safe metadata only: the event type name. Never the payload, the
    # signature, the cert URL, or any credential.
    logger.info("Verifying PayPal webhook (event_type=%s)", event_type)

    try:
        response = httpx.post(
            f"{api_base}/v1/notifications/verify-webhook-signature",
            json=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            timeout=15.0,
        )
    except httpx.RequestError as exc:
        logger.error("Network error verifying webhook: %s", exc)
        raise WebhookVerificationError(
            "Could not reach PayPal to verify the webhook signature"
        ) from exc

    if response.status_code != 200:
        logger.error("PayPal webhook verification failed: HTTP %s", response.status_code)
        raise WebhookVerificationError(
            f"PayPal returned HTTP {response.status_code} for webhook verification"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        logger.error("PayPal webhook verification response was not valid JSON")
        raise WebhookVerificationError(
            "PayPal returned a malformed (non-JSON) verification response"
        ) from exc

    status = payload.get("verification_status") if isinstance(payload, dict) else None
    if status not in ("SUCCESS", "FAILURE"):
        # An unexpected answer is an upstream failure, never a pass.
        logger.error("PayPal verification response had no usable status")
        raise WebhookVerificationError(
            "PayPal verification response did not contain a verification_status"
        )

    verified = status == "SUCCESS"
    logger.info(
        "Webhook verification result=%s event_type=%s",
        "VERIFIED" if verified else "INVALID",
        event_type,
    )

    return {
        "verified": verified,
        "event_type": event_type,
        "event_id": event.get("id"),
        "known_event_type": event_type in KNOWN_EVENT_TYPES,
    }
