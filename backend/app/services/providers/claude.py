"""Anthropic Claude provider (optional, swappable).

Preserved so PayPilot stays provider-agnostic — selectable via AI_PROVIDER=claude.
The Anthropic SDK is imported lazily, so neither the package nor a CLAUDE_API_KEY
is required for the default Gemini path.
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

import json

from .base import (
    INTENT_SYSTEM_PROMPT,
    RANKING_SYSTEM_PROMPT,
    RECOMMENDATION_SYSTEM_PROMPT,
    AIConfigError,
    AIProvider,
    AIServiceError,
    InvalidAIResponseError,
)

load_dotenv()

logger = logging.getLogger("paypilot.ai.claude")

_DEFAULT_MODEL = "claude-opus-5-5"

# Claude expresses structured output via a tool call. tool_choice stays "auto"
# because current models reject a forced tool_choice; the prompt + single tool
# make the call reliable, and the Intent Agent validates the result.
_INTENT_TOOL = {
    "name": "record_intent",
    "description": (
        "Record the user's structured purchasing intent. Always call this tool "
        "exactly once with your best interpretation of the user's request."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "category": {"type": ["string", "null"]},
            "keywords": {"type": "array", "items": {"type": "string"}},
            "use_case": {"type": ["string", "null"]},
            "quantity": {"type": "integer", "minimum": 1},
            "hard_constraints": {
                "type": "object",
                "properties": {
                    "max_price_inr": {"type": ["integer", "null"]},
                    "min_price_inr": {"type": ["integer", "null"]},
                    "min_rating": {"type": ["number", "null"]},
                    "ram_gb_min": {"type": ["integer", "null"]},
                    "storage_gb_min": {"type": ["integer", "null"]},
                    "required_features": {"type": "array", "items": {"type": "string"}},
                },
            },
            "preferences": {
                "type": "object",
                "properties": {
                    "brand": {"type": ["string", "null"]},
                    "gpu": {"type": ["string", "null"]},
                    "preferred_features": {"type": "array", "items": {"type": "string"}},
                },
            },
            "excluded_features": {"type": "array", "items": {"type": "string"}},
            "authorization": {
                "type": "object",
                "properties": {
                    "purchase_requested": {"type": "boolean"},
                    "autonomous_purchase_allowed": {"type": "boolean"},
                    "auto_approve_below_inr": {"type": ["number", "null"]},
                },
            },
        },
    },
}


# Structured recommendation output, mirroring schemas.recommendation.
# Deliberately has NO price/spec fields — the model can only point at a
# candidate by id.
_RECOMMEND_TOOL = {
    "name": "record_recommendation",
    "description": (
        "Record the recommendation. Always call this tool exactly once. "
        "recommended_product_id must be the id of one supplied candidate, or "
        "null if no candidate is suitable."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "recommended_product_id": {"type": ["string", "null"]},
            "reasoning": {"type": "string"},
            "matched_constraints": {"type": "array", "items": {"type": "string"}},
            "tradeoffs": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["reasoning"],
    },
}


# Structured ranking output, mirroring schemas.comparison.ProductRanking.
# Like the recommendation tool it has NO price/URL/spec fields — the model
# can only order ids it was given and attach prose to them.
_RANK_TOOL = {
    "name": "record_ranking",
    "description": (
        "Record the product ranking. Always call this tool exactly once. "
        "ranked_ids must contain only ids from the supplied CANDIDATES list, "
        "each at most once, best first."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "ranked_ids": {"type": "array", "items": {"type": "string"}},
            "reasons": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "product_id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["product_id", "reason"],
                },
            },
        },
        "required": ["ranked_ids"],
    },
}


class ClaudeProvider(AIProvider):
    name = "claude"

    def _get_client(self):
        """Build an Anthropic client, or raise AIConfigError if no key is set."""
        api_key = os.getenv("CLAUDE_API_KEY", "").strip()
        if not api_key:
            raise AIConfigError("CLAUDE_API_KEY is not set in the environment")
        import anthropic  # lazy import

        return anthropic.Anthropic(api_key=api_key)

    def extract_intent(self, message: str) -> dict:
        import anthropic  # lazy import

        client = self._get_client()
        model = os.getenv("CLAUDE_MODEL", _DEFAULT_MODEL).strip() or _DEFAULT_MODEL

        logger.info("Requesting intent extraction from Claude (model=%s)", model)

        try:
            response = client.messages.create(
                model=model,
                max_tokens=1024,
                system=INTENT_SYSTEM_PROMPT,
                tools=[_INTENT_TOOL],
                tool_choice={"type": "auto"},
                messages=[{"role": "user", "content": message}],
            )
        except anthropic.APIError as exc:
            logger.error("Claude API request failed: %s", type(exc).__name__)
            raise AIServiceError("Claude API request failed") from exc

        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "record_intent":
                return dict(block.input)

        logger.error("Claude response contained no record_intent tool call")
        raise InvalidAIResponseError("Claude did not return a structured intent")

    def recommend_product(self, intent: dict, candidates: list[dict]) -> dict:
        import anthropic  # lazy import

        client = self._get_client()
        model = os.getenv("CLAUDE_MODEL", _DEFAULT_MODEL).strip() or _DEFAULT_MODEL

        logger.info("Requesting product recommendation from Claude (model=%s)", model)

        content = (
            "INTENT:\n"
            + json.dumps(intent, ensure_ascii=False)
            + "\n\nCANDIDATES:\n"
            + json.dumps(candidates, ensure_ascii=False)
        )

        try:
            response = client.messages.create(
                model=model,
                max_tokens=1024,
                system=RECOMMENDATION_SYSTEM_PROMPT,
                tools=[_RECOMMEND_TOOL],
                tool_choice={"type": "auto"},
                messages=[{"role": "user", "content": content}],
            )
        except anthropic.APIError as exc:
            logger.error("Claude API request failed: %s", type(exc).__name__)
            raise AIServiceError("Claude API request failed") from exc

        for block in response.content:
            if (
                getattr(block, "type", None) == "tool_use"
                and block.name == "record_recommendation"
            ):
                return dict(block.input)

        logger.error("Claude response contained no record_recommendation tool call")
        raise InvalidAIResponseError("Claude did not return a structured recommendation")

    def rank_products(
        self, requirements: dict, candidates: list[dict], top_k: int
    ) -> dict:
        import anthropic  # lazy import

        client = self._get_client()
        model = os.getenv("CLAUDE_MODEL", _DEFAULT_MODEL).strip() or _DEFAULT_MODEL

        logger.info("Requesting product ranking from Claude (model=%s)", model)

        # Serialized verbatim so the model can only quote backend-supplied
        # values; the tool schema gives it no field for a price or spec.
        content = (
            "REQUIREMENTS:\n"
            + json.dumps(requirements, ensure_ascii=False)
            + "\n\nCANDIDATES:\n"
            + json.dumps(candidates, ensure_ascii=False)
            + f"\n\nReturn at most {top_k} ranked candidate id(s), best first."
        )

        try:
            response = client.messages.create(
                model=model,
                max_tokens=1024,
                system=RANKING_SYSTEM_PROMPT,
                tools=[_RANK_TOOL],
                tool_choice={"type": "auto"},
                messages=[{"role": "user", "content": content}],
            )
        except anthropic.APIError as exc:
            logger.error("Claude API request failed: %s", type(exc).__name__)
            raise AIServiceError("Claude API request failed") from exc

        for block in response.content:
            if (
                getattr(block, "type", None) == "tool_use"
                and block.name == "record_ranking"
            ):
                return dict(block.input)

        logger.error("Claude response contained no record_ranking tool call")
        raise InvalidAIResponseError("Claude did not return a structured ranking")
