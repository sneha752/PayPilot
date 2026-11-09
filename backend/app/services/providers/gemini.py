"""Google Gemini provider (default).

Uses the official google-genai SDK with structured output: we hand Gemini our
ProductIntent Pydantic model as the response schema and ask for JSON, then
return the parsed dict for the Intent Agent to validate.

The API key (GEMINI_API_KEY) is read from the environment, never logged, and
never returned. The SDK is imported lazily so importing this module does not
require google-genai to be installed.
"""

from __future__ import annotations

import json
import logging
import os

from dotenv import load_dotenv

from ...schemas.comparison import ProductRanking
from ...schemas.intent import ProductIntent
from ...schemas.recommendation import Recommendation
from ...schemas.routing import RouteResult
from .base import (
    INTENT_SYSTEM_PROMPT,
    RANKING_SYSTEM_PROMPT,
    RECOMMENDATION_SYSTEM_PROMPT,
    ROUTING_SYSTEM_PROMPT,
    AIConfigError,
    AIProvider,
    AIServiceError,
    InvalidAIResponseError,
)

load_dotenv()

logger = logging.getLogger("paypilot.ai.gemini")

_DEFAULT_MODEL = "gemini-3.5-flash"


class GeminiProvider(AIProvider):
    name = "gemini"

    def _get_client(self):
        """Build a Gemini client, or raise AIConfigError if no key is set."""
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise AIConfigError("GEMINI_API_KEY is not set in the environment")
        from google import genai  # lazy import

        return genai.Client(api_key=api_key)

    def _generate_structured(
        self, system_prompt: str, contents: str, response_schema, task: str
    ) -> dict:
        """Shared structured-output call: prompt + schema -> parsed dict."""
        client = self._get_client()
        model = os.getenv("GEMINI_MODEL", _DEFAULT_MODEL).strip() or _DEFAULT_MODEL

        from google.genai import types  # lazy import

        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            response_schema=response_schema,
        )

        logger.info("Requesting %s from Gemini (model=%s)", task, model)

        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
        except Exception as exc:  # SDK/network/status errors
            logger.error("Gemini API request failed: %s", type(exc).__name__)
            raise AIServiceError("Gemini API request failed") from exc

        text = getattr(response, "text", None)
        if not text:
            logger.error("Gemini returned an empty response")
            raise InvalidAIResponseError("Gemini returned no content")

        try:
            data = json.loads(text)
        except (ValueError, TypeError) as exc:
            logger.error("Gemini returned non-JSON content")
            raise InvalidAIResponseError("Gemini returned non-JSON content") from exc

        if not isinstance(data, dict):
            raise InvalidAIResponseError("Gemini returned a non-object JSON value")

        return data

    def extract_intent(self, message: str) -> dict:
        return self._generate_structured(
            system_prompt=INTENT_SYSTEM_PROMPT,
            contents=message,
            response_schema=ProductIntent,
            task="intent extraction",
        )

    def recommend_product(self, intent: dict, candidates: list[dict]) -> dict:
        # Candidates and intent are serialized verbatim so the model can only
        # quote catalog values; the Recommendation schema leaves it no field
        # to assert a price or spec of its own.
        contents = (
            "INTENT:\n"
            + json.dumps(intent, ensure_ascii=False)
            + "\n\nCANDIDATES:\n"
            + json.dumps(candidates, ensure_ascii=False)
        )
        return self._generate_structured(
            system_prompt=RECOMMENDATION_SYSTEM_PROMPT,
            contents=contents,
            response_schema=Recommendation,
            task="product recommendation",
        )

    def classify_route(self, message: str) -> dict:
        # Same client, key handling, and structured-output path as the other
        # tasks; the RouteResult schema leaves the model nothing to return
        # beyond a route name and a short summary.
        return self._generate_structured(
            system_prompt=ROUTING_SYSTEM_PROMPT,
            contents=message,
            response_schema=RouteResult,
            task="message routing",
        )

    def rank_products(
        self, requirements: dict, candidates: list[dict], top_k: int
    ) -> dict:
        # Requirements and candidates are serialized verbatim so the model can
        # only quote backend-supplied values; the ProductRanking schema gives
        # it no field in which to assert a price, URL, or specification.
        contents = (
            "REQUIREMENTS:\n"
            + json.dumps(requirements, ensure_ascii=False)
            + "\n\nCANDIDATES:\n"
            + json.dumps(candidates, ensure_ascii=False)
            + f"\n\nReturn at most {top_k} ranked candidate id(s), best first."
        )
        return self._generate_structured(
            system_prompt=RANKING_SYSTEM_PROMPT,
            contents=contents,
            response_schema=ProductRanking,
            task="product ranking",
        )
