"""Router Agent: natural language -> validated RouteResult (Phase 8E Step 2).

Pipeline:
    message -> AIProvider.classify_route -> Pydantic validation.

Classification ONLY. This agent depends on the provider *interface*
(app.services.providers), never a vendor SDK, and it must never call
PayPal, execute a refund, create a purchase, evaluate policy, or touch
the ledger. Its output names a flow; a later layer decides what (if
anything) to do with that, and money movement always requires the
deterministic, explicitly-confirmed paths.

FAIL SAFELY: any model failure — provider error, malformed payload,
unsupported route — raises a controlled exception. A route is NEVER
guessed and NEVER defaulted (especially not to SHOP or REFUND); the
future /chat endpoint maps these errors to proper HTTP responses.
"""

from __future__ import annotations

import logging
from typing import Optional

from pydantic import ValidationError

from ..schemas.routing import RouteResult
from ..services.providers import AIProvider, get_provider

logger = logging.getLogger("paypilot.router_agent")


class RouteValidationError(Exception):
    """Raised when the provider's payload fails RouteResult validation
    (including any route outside the four supported ones)."""


def route_message(message: str, provider: Optional[AIProvider] = None) -> RouteResult:
    """Classify one conversational message into a supported route.

    Args:
        message: The user's message.
        provider: Optional explicit provider (testing/DI). Defaults to the
            configured provider from get_provider().

    Raises:
        AIConfigError: provider not configured (e.g. missing API key).
        AIServiceError: the provider's API call failed (incl. timeouts).
        InvalidAIResponseError: provider returned no usable structured output.
        RouteValidationError: the payload failed schema validation or named
            an unsupported route. No fallback route is ever substituted.
    """
    provider = provider or get_provider()
    payload = provider.classify_route(message)

    try:
        result = RouteResult(**payload)
    except ValidationError as exc:
        logger.error("Routing payload failed validation: %s", exc.error_count())
        raise RouteValidationError(
            "AI provider returned a routing result that failed validation"
        ) from exc

    logger.info("Routed message via %s: route=%s", provider.name, result.route.value)
    return result
