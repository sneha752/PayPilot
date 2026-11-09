"""rank_products is a vendor-neutral provider operation (Phase 10E).

Asserts that ranking lives on the AIProvider abstraction rather than being
coupled to one vendor, that each registered provider implements it through
its OWN existing structured-output path (no second AI client), and that the
shared ranking prompt forbids inventing facts. No real AI request is made.
"""

from __future__ import annotations

import json

import pytest

from app.services.providers import (
    ClaudeProvider,
    GeminiProvider,
    GroqProvider,
    get_provider,
)
from app.services.providers import claude as claude_mod
from app.services.providers import gemini as gemini_mod
from app.services.providers import groq as groq_mod
from app.services.providers.base import (
    RANKING_SYSTEM_PROMPT,
    AIProvider,
    InvalidAIResponseError,
)

REQUIREMENTS = {"category": "headphones", "max_price": "300", "price_currency": "USD"}
CANDIDATES = [
    {"external_id": "a", "title": "Alpha", "observed_price": "100", "observed_currency": "USD"},
    {"external_id": "b", "title": "Beta"},
]


# ---------------------------------------------------------------------------
# Abstraction
# ---------------------------------------------------------------------------


def test_rank_products_is_declared_on_the_base_abstraction():
    assert hasattr(AIProvider, "rank_products")


def test_every_registered_provider_implements_ranking():
    for name in ("gemini", "claude", "groq"):
        provider = get_provider(name)
        assert provider.rank_products.__func__ is not AIProvider.rank_products


def test_a_provider_without_ranking_fails_clearly_rather_than_silently():
    class MinimalProvider(AIProvider):
        name = "minimal"

        def extract_intent(self, message: str) -> dict:
            return {}

    with pytest.raises(NotImplementedError, match="minimal"):
        MinimalProvider().rank_products(REQUIREMENTS, CANDIDATES, 3)


def test_ranking_does_not_depend_on_a_specific_vendor(monkeypatch):
    # The comparison agent accepts any AIProvider; nothing imports Groq.
    from app.agents import comparison_agent

    source = comparison_agent.__doc__ or ""
    assert "groq" not in source.lower()
    for module_attr in vars(comparison_agent).values():
        assert module_attr is not GroqProvider


# ---------------------------------------------------------------------------
# Shared prompt
# ---------------------------------------------------------------------------


def test_ranking_prompt_forbids_inventing_facts():
    lowered = RANKING_SYSTEM_PROMPT.lower()
    for forbidden in (
        "prices",
        "urls",
        "specifications",
        "ratings",
        "availability",
        "brands",
        "merchants",
        "product ids",
    ):
        assert forbidden in lowered
    assert "do not invent" in lowered


def test_ranking_prompt_restricts_the_model_to_supplied_ids():
    lowered = RANKING_SYSTEM_PROMPT.lower()
    assert "only the supplied candidate ids" in lowered
    assert "never repeat an id" in lowered


def test_ranking_prompt_forbids_unsupported_superlatives():
    lowered = RANKING_SYSTEM_PROMPT.lower()
    for banned_claim in ("best on the market", "highest quality", "cheapest online"):
        assert banned_claim in lowered
    assert "best match among the returned candidates" in lowered


def test_ranking_prompt_denies_payment_authority():
    lowered = RANKING_SYSTEM_PROMPT.lower()
    assert "never authorize spending" in lowered
    assert "not payment amounts" in lowered


# ---------------------------------------------------------------------------
# Groq (the configured provider) — reuses its existing structured-output path
# ---------------------------------------------------------------------------


class GroqFakeResponse:
    status_code = 200

    def __init__(self, content):
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def test_groq_ranking_uses_json_schema_structured_output(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    captured = {}

    def fake_post(payload, api_key):
        captured["payload"] = payload
        captured["api_key"] = api_key
        return GroqFakeResponse(json.dumps({"ranked_ids": ["a"], "reasons": []}))

    monkeypatch.setattr(GroqProvider, "_post", lambda self, p, k: fake_post(p, k))

    result = GroqProvider().rank_products(REQUIREMENTS, CANDIDATES, 3)

    assert result == {"ranked_ids": ["a"], "reasons": []}
    payload = captured["payload"]
    assert payload["response_format"]["json_schema"]["name"] == "ProductRanking"
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert payload["messages"][0]["content"] == RANKING_SYSTEM_PROMPT


def test_groq_ranking_schema_has_no_price_or_url_properties(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    captured = {}

    monkeypatch.setattr(
        GroqProvider,
        "_post",
        lambda self, p, k: (
            captured.setdefault("payload", p),
            GroqFakeResponse(json.dumps({"ranked_ids": ["a"]})),
        )[1],
    )

    GroqProvider().rank_products(REQUIREMENTS, CANDIDATES, 3)

    schema = captured["payload"]["response_format"]["json_schema"]["schema"]

    def property_names(node):
        """Every property NAME anywhere in the schema (not description prose)."""
        names = set()
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "properties" and isinstance(value, dict):
                    names |= set(value)
                names |= property_names(value)
        elif isinstance(node, list):
            for item in node:
                names |= property_names(item)
        return names

    names = property_names(schema)

    assert names == {"ranked_ids", "reasons", "product_id", "reason"}
    for forbidden in (
        "observed_price",
        "observed_currency",
        "product_url",
        "image_url",
        "rating",
        "availability",
        "brand",
        "retailer_domain",
    ):
        assert forbidden not in names


def test_groq_ranking_sends_requirements_and_candidates_verbatim(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    captured = {}

    monkeypatch.setattr(
        GroqProvider,
        "_post",
        lambda self, p, k: (
            captured.setdefault("payload", p),
            GroqFakeResponse(json.dumps({"ranked_ids": ["a"]})),
        )[1],
    )

    GroqProvider().rank_products(REQUIREMENTS, CANDIDATES, 2)

    user_content = captured["payload"]["messages"][1]["content"]
    assert "REQUIREMENTS:" in user_content
    assert "CANDIDATES:" in user_content
    assert json.dumps(CANDIDATES, ensure_ascii=False) in user_content
    assert "at most 2 ranked candidate id(s)" in user_content


def test_groq_ranking_rejects_non_json_content(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(
        GroqProvider, "_post", lambda self, p, k: GroqFakeResponse("not json at all")
    )

    with pytest.raises(InvalidAIResponseError):
        GroqProvider().rank_products(REQUIREMENTS, CANDIDATES, 3)


def test_groq_ranking_never_leaks_the_api_key(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "super-secret-key")
    monkeypatch.setattr(
        GroqProvider, "_post", lambda self, p, k: GroqFakeResponse("}{")
    )

    with pytest.raises(InvalidAIResponseError) as exc:
        GroqProvider().rank_products(REQUIREMENTS, CANDIDATES, 3)

    assert "super-secret-key" not in str(exc.value)


# ---------------------------------------------------------------------------
# Gemini / Claude — same shape, no second AI client
# ---------------------------------------------------------------------------


def test_gemini_ranking_reuses_its_structured_output_helper(monkeypatch):
    captured = {}

    def fake_generate(self, system_prompt, contents, response_schema, task):
        captured.update(
            {"prompt": system_prompt, "schema": response_schema, "task": task}
        )
        return {"ranked_ids": ["a"]}

    monkeypatch.setattr(GeminiProvider, "_generate_structured", fake_generate)

    assert GeminiProvider().rank_products(REQUIREMENTS, CANDIDATES, 3) == {
        "ranked_ids": ["a"]
    }
    assert captured["prompt"] == RANKING_SYSTEM_PROMPT
    assert captured["schema"].__name__ == "ProductRanking"
    assert captured["task"] == "product ranking"


def test_claude_ranking_tool_schema_has_no_product_fact_fields():
    properties = claude_mod._RANK_TOOL["input_schema"]["properties"]

    assert set(properties) == {"ranked_ids", "reasons"}
    reason_properties = properties["reasons"]["items"]["properties"]
    assert set(reason_properties) == {"product_id", "reason"}


def test_no_provider_module_creates_a_second_ai_client():
    # Ranking goes through each provider's existing client/helper; no new
    # vendor SDK or endpoint was introduced for this phase.
    assert groq_mod._API_URL == "https://api.groq.com/openai/v1/chat/completions"
    assert hasattr(GeminiProvider, "_get_client")
    assert hasattr(ClaudeProvider, "_get_client")


# ---------------------------------------------------------------------------
# Existing provider behavior is intact
# ---------------------------------------------------------------------------


def test_existing_provider_operations_still_exist():
    for name in ("gemini", "claude", "groq"):
        provider = get_provider(name)
        assert callable(provider.extract_intent)
        assert callable(provider.recommend_product)
