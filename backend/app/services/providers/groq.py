"""Groq provider (OpenAI-compatible REST, Phase 8F).

Talks to Groq's chat-completions endpoint directly with httpx (already a core
dependency) — no vendor SDK. Structured output uses Groq's json_schema
response_format with the same Pydantic schemas the Gemini provider uses
(ProductIntent / Recommendation / RouteResult), adapted for strict mode.

The API key (GROQ_API_KEY) is read from the environment, never logged, and
never included in an error message or response.
"""

from __future__ import annotations

import json
import logging
import os

import httpx
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

logger = logging.getLogger("paypilot.ai.groq")

_DEFAULT_MODEL = "openai/gpt-oss-20b"
_API_URL = "https://api.groq.com/openai/v1/chat/completions"
# LLM completions routinely exceed httpx's 5s default.
_TIMEOUT_SECONDS = 60.0

# Pydantic metadata that strict structured-output mode may reject. Dropping
# them loses nothing: the agents re-validate every payload with the full
# Pydantic models afterwards.
_STRIPPED_KEYWORDS = frozenset(
    {
        "title",
        "default",
        "format",
        "pattern",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minItems",
        "maxItems",
    }
)


def _strict_schema(node):
    """Adapt a Pydantic JSON schema for strict structured output.

    Strict mode requires every object to close additionalProperties and list
    every property as required (optional fields stay expressible as null via
    their anyOf). Unsupported metadata keywords are stripped. Keys inside
    "properties"/"$defs" are field/model NAMES, never schema keywords, so they
    are preserved verbatim.
    """
    if isinstance(node, list):
        return [_strict_schema(item) for item in node]
    if not isinstance(node, dict):
        return node

    out = {}
    for key, value in node.items():
        if key in _STRIPPED_KEYWORDS:
            continue
        if key in ("properties", "$defs"):
            out[key] = {name: _strict_schema(sub) for name, sub in value.items()}
        else:
            out[key] = _strict_schema(value)

    if out.get("type") == "object" and "properties" in out:
        out["additionalProperties"] = False
        out["required"] = list(out["properties"].keys())

    return out


class GroqProvider(AIProvider):
    name = "groq"

    def _post(self, payload: dict, api_key: str) -> httpx.Response:
        """Single HTTP seam — tests fake this; nothing else touches the network."""
        return httpx.post(
            _API_URL,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=_TIMEOUT_SECONDS,
        )

    def _generate_structured(
        self, system_prompt: str, contents: str, response_schema, task: str
    ) -> dict:
        """Shared structured-output call: prompt + schema -> parsed dict."""
        api_key = os.getenv("GROQ_API_KEY", "").strip()
        if not api_key:
            raise AIConfigError("GROQ_API_KEY is not set in the environment")

        model = os.getenv("GROQ_MODEL", _DEFAULT_MODEL).strip() or _DEFAULT_MODEL

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": contents},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": response_schema.__name__,
                    "strict": True,
                    "schema": _strict_schema(response_schema.model_json_schema()),
                },
            },
        }

        logger.info("Requesting %s from Groq (model=%s)", task, model)

        try:
            response = self._post(payload, api_key)
        except httpx.HTTPError as exc:
            logger.error("Groq API request failed: %s", type(exc).__name__)
            raise AIServiceError("Groq API request failed") from exc

        if response.status_code != 200:
            # Status only — the body may echo request details and is not ours
            # to surface.
            logger.error("Groq API returned HTTP %s", response.status_code)
            raise AIServiceError("Groq API request failed")

        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            logger.error("Groq returned an unexpected response shape")
            raise InvalidAIResponseError(
                "Groq returned an unexpected response shape"
            ) from exc

        if not content:
            logger.error("Groq returned an empty response")
            raise InvalidAIResponseError("Groq returned no content")

        try:
            data = json.loads(content)
        except (ValueError, TypeError) as exc:
            logger.error("Groq returned non-JSON content")
            raise InvalidAIResponseError("Groq returned non-JSON content") from exc

        if not isinstance(data, dict):
            raise InvalidAIResponseError("Groq returned a non-object JSON value")

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
        # Same key handling and structured-output path as the other tasks;
        # the RouteResult schema leaves the model nothing to return beyond a
        # route name and a short summary.
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
