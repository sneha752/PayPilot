"""Intent Agent: natural language -> validated ProductIntent.

Pipeline:
    message -> AIProvider.extract_intent -> Pydantic validation.

The agent depends on the provider *interface* (app.services.providers), not on
any vendor SDK. It must never call PayPal, search the product catalog, or
trigger any purchase. `purchase_requested` on the returned intent is
descriptive only.
"""

from __future__ import annotations

import logging
from typing import Optional

from pydantic import ValidationError

from ..schemas.intent import ProductIntent
from ..services.providers import AIProvider, get_provider

logger = logging.getLogger("paypilot.intent_agent")


class IntentValidationError(Exception):
    """Raised when the provider's payload fails ProductIntent validation."""


def interpret(message: str, provider: Optional[AIProvider] = None) -> ProductIntent:
    """Interpret a natural-language purchasing request into a ProductIntent.

    Args:
        message: The user's request.
        provider: Optional explicit provider (for testing/DI). Defaults to the
            configured provider from get_provider().

    Raises:
        AIConfigError: provider not configured (e.g. missing API key).
        AIServiceError: the provider's API call failed.
        InvalidAIResponseError: provider returned no usable structured output.
        IntentValidationError: the returned payload failed schema validation.
    """
    provider = provider or get_provider()
    payload = provider.extract_intent(message)

    try:
        intent = ProductIntent(**payload)
    except ValidationError as exc:
        logger.error("Intent payload failed validation: %s", exc.error_count())
        raise IntentValidationError(
            "AI provider returned an intent that failed validation"
        ) from exc

    logger.info(
        "Parsed intent via %s: category=%s purchase_requested=%s autonomous=%s",
        provider.name,
        intent.category,
        intent.authorization.purchase_requested,
        intent.authorization.autonomous_purchase_allowed,
    )
    return intent
