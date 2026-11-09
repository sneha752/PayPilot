"""Tests for the Recommendation Agent and POST /ai/recommend.

No real AI call is ever made: a FakeProvider returns canned intent and
recommendation payloads and records what it was asked, so the full
message -> intent -> search -> recommendation pipeline is exercised
deterministically with zero API credits. PayPal is never touched.
"""

from starlette.testclient import TestClient

from app.main import app
from app.agents import intent_agent, recommendation_agent
from app.services.providers.base import AIProvider, AIServiceError

client = TestClient(app, raise_server_exceptions=False)


class FakeProvider(AIProvider):
    """Canned intent + recommendation payloads; records recommend calls."""

    name = "fake"

    def __init__(self, intent_payload=None, rec_payload=None, rec_exc=None):
        self._intent_payload = intent_payload
        self._rec_payload = rec_payload
        self._rec_exc = rec_exc
        # Each entry is (intent_dict, candidates_list) as given to the AI.
        self.recommend_calls: list[tuple[dict, list[dict]]] = []

    def extract_intent(self, message: str) -> dict:
        return self._intent_payload

    def recommend_product(self, intent: dict, candidates: list[dict]) -> dict:
        self.recommend_calls.append((intent, candidates))
        if self._rec_exc is not None:
            raise self._rec_exc
        return self._rec_payload


def _install(monkeypatch, **kwargs) -> FakeProvider:
    provider = FakeProvider(**kwargs)
    monkeypatch.setattr(intent_agent, "get_provider", lambda: provider)
    monkeypatch.setattr(recommendation_agent, "get_provider", lambda: provider)
    return provider


def _post(message: str):
    return client.post("/ai/recommend", json={"message": message})


# Canned payloads -------------------------------------------------------------

LAPTOP_INTENT = {"category": "laptop", "keywords": ["laptop"]}

LAPTOP_80K_INTENT = {
    "category": "laptop",
    "hard_constraints": {"max_price_inr": 80000},
}

REC_LAP002 = {
    "recommended_product_id": "LAP002",
    "reasoning": "Best balance of specs and price within budget.",
    "matched_constraints": ["category: laptop", "price <= 80000"],
    "tradeoffs": [],
}


# --- 1. Basic laptop recommendation ------------------------------------------


def test_basic_laptop_recommendation(monkeypatch):
    _install(monkeypatch, intent_payload=LAPTOP_INTENT, rec_payload=REC_LAP002)
    resp = _post("Find me a good laptop")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "recommended"
    assert body["recommendation"]["recommended_product_id"] == "LAP002"
    assert body["recommended_product"]["id"] == "LAP002"
    # Product data comes from the catalog, exactly.
    assert body["recommended_product"]["price_inr"] == 68990
    assert body["recommendation"]["reasoning"]


# --- 2. Maximum price filtering ----------------------------------------------


def test_max_price_filters_candidates(monkeypatch):
    provider = _install(
        monkeypatch, intent_payload=LAPTOP_80K_INTENT, rec_payload=REC_LAP002
    )
    resp = _post("Find me a laptop under 80000")
    assert resp.status_code == 200

    (intent_sent, candidates_sent), = provider.recommend_calls
    sent_ids = {c["id"] for c in candidates_sent}
    assert sent_ids == {"LAP002", "LAP006"}
    assert all(c["price_inr"] <= 80000 for c in candidates_sent)
    # The response's candidate list matches what the AI saw.
    assert {c["id"] for c in resp.json()["candidates"]} == sent_ids


# --- 3. Minimum RAM filtering -------------------------------------------------


def test_min_ram_filters_candidates(monkeypatch):
    provider = _install(
        monkeypatch,
        intent_payload={
            "category": "laptop",
            "hard_constraints": {"ram_gb_min": 16},
        },
        rec_payload=REC_LAP002,
    )
    resp = _post("a laptop with at least 16GB RAM")
    assert resp.status_code == 200
    (_, candidates_sent), = provider.recommend_calls
    assert {c["id"] for c in candidates_sent} == {
        "LAP001", "LAP002", "LAP003", "LAP005",
    }
    assert all(c["ram_gb"] >= 16 for c in candidates_sent)


# --- 4. Hard constraint vs preference -----------------------------------------


def test_preference_does_not_filter_candidates(monkeypatch):
    # "prefer RTX" is soft: under 80000 no laptop has an RTX GPU, yet
    # candidates must NOT be emptied by the preference — the model handles it
    # as a tradeoff instead.
    provider = _install(
        monkeypatch,
        intent_payload={
            "category": "laptop",
            "hard_constraints": {"max_price_inr": 80000},
            "preferences": {"gpu": "RTX"},
        },
        rec_payload={
            **REC_LAP002,
            "tradeoffs": ["No RTX GPU available within the budget."],
        },
    )
    resp = _post("laptop under 80000, I prefer an RTX GPU")
    assert resp.status_code == 200
    (intent_sent, candidates_sent), = provider.recommend_calls
    # Same candidate set as without the preference.
    assert {c["id"] for c in candidates_sent} == {"LAP002", "LAP006"}
    # The preference still reaches the model for ranking.
    assert intent_sent["preferences"]["gpu"] == "RTX"
    assert resp.json()["recommendation"]["tradeoffs"]


# --- 5. No candidate satisfies constraints --------------------------------------


def test_no_candidates_reports_honestly_without_ai_call(monkeypatch):
    provider = _install(
        monkeypatch,
        intent_payload={
            "category": "laptop",
            "hard_constraints": {"max_price_inr": 10000},
        },
        rec_payload=REC_LAP002,  # must never be used
    )
    resp = _post("Find me a laptop under 10000")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "no_candidates"
    assert body["candidates"] == []
    assert body["recommendation"] is None
    assert body["recommended_product"] is None
    # No AI credits spent when there is nothing to recommend.
    assert provider.recommend_calls == []


def test_model_may_decline_with_null_id(monkeypatch):
    # The model found the candidates unsuitable and says so (id=null) instead
    # of silently relaxing constraints.
    _install(
        monkeypatch,
        intent_payload=LAPTOP_80K_INTENT,
        rec_payload={
            "recommended_product_id": None,
            "reasoning": "No candidate fits the stated use case.",
        },
    )
    resp = _post("laptop under 80000 for heavy 3D rendering")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "no_suitable_candidate"
    assert body["recommended_product"] is None
    assert body["recommendation"]["recommended_product_id"] is None


# --- 6. Recommended id must exist in the candidate list -------------------------


def test_nonexistent_product_id_rejected(monkeypatch):
    _install(
        monkeypatch,
        intent_payload=LAPTOP_INTENT,
        rec_payload={"recommended_product_id": "LAP999", "reasoning": "made up"},
    )
    assert _post("Find me a laptop").status_code == 502


def test_real_catalog_id_outside_candidates_rejected(monkeypatch):
    # PHN001 exists in the catalog but is not a laptop — recommending it
    # would bypass the deterministic search, so it must be rejected.
    _install(
        monkeypatch,
        intent_payload=LAPTOP_INTENT,
        rec_payload={"recommended_product_id": "PHN001", "reasoning": "nice phone"},
    )
    assert _post("Find me a laptop").status_code == 502


# --- 7. The model cannot invent prices or specifications ------------------------


def test_invented_price_and_specs_are_ignored(monkeypatch):
    # The fake model tries to attach its own price/specs; the schema has no
    # such fields, and the response product is served from the catalog.
    _install(
        monkeypatch,
        intent_payload=LAPTOP_INTENT,
        rec_payload={
            **REC_LAP002,
            "price_inr": 1,            # invented — must be discarded
            "ram_gb": 999,             # invented — must be discarded
            "recommended_product": {   # invented object — must be discarded
                "id": "LAP002", "price_inr": 1,
            },
        },
    )
    resp = _post("Find me a laptop")
    assert resp.status_code == 200
    body = resp.json()
    product = body["recommended_product"]
    assert product["price_inr"] == 68990   # catalog value, not the model's
    assert product["ram_gb"] == 16         # catalog value
    assert "price_inr" not in body["recommendation"]
    assert "ram_gb" not in body["recommendation"]


# --- 8. Authorization threshold must not affect filtering -----------------------


def test_auto_approve_threshold_does_not_cap_candidates(monkeypatch):
    # Budget 80000, autonomous threshold 50000: LAP002 (68,990) must remain
    # both a candidate and recommendable.
    provider = _install(
        monkeypatch,
        intent_payload={
            "category": "laptop",
            "hard_constraints": {"max_price_inr": 80000},
            "authorization": {
                "purchase_requested": True,
                "autonomous_purchase_allowed": True,
                "auto_approve_below_inr": 50000,
            },
        },
        rec_payload=REC_LAP002,
    )
    resp = _post("laptop under 80000, you may auto-buy below 50000")
    assert resp.status_code == 200
    body = resp.json()

    (_, candidates_sent), = provider.recommend_calls
    sent_ids = {c["id"] for c in candidates_sent}
    assert "LAP002" in sent_ids            # 50000 < 68,990 <= 80000
    assert sent_ids == {"LAP002", "LAP006"}
    assert body["recommended_product"]["price_inr"] == 68990


def test_authorization_is_not_sent_to_the_recommender(monkeypatch):
    # The recommendation model receives only shopping data; financial
    # authorization stays out of its context entirely.
    provider = _install(
        monkeypatch,
        intent_payload={
            "category": "laptop",
            "authorization": {
                "purchase_requested": True,
                "autonomous_purchase_allowed": True,
                "auto_approve_below_inr": 50000,
            },
        },
        rec_payload=REC_LAP002,
    )
    assert _post("laptop, auto-buy below 50000").status_code == 200
    (intent_sent, _), = provider.recommend_calls
    assert "authorization" not in intent_sent


def test_authorization_is_echoed_as_inert_data(monkeypatch):
    # The overall response may carry authorization through — unevaluated —
    # for the future Policy Engine.
    _install(
        monkeypatch,
        intent_payload={
            "category": "laptop",
            "hard_constraints": {"max_price_inr": 80000},
            "authorization": {
                "purchase_requested": True,
                "autonomous_purchase_allowed": True,
                "auto_approve_below_inr": 50000,
            },
        },
        rec_payload=REC_LAP002,
    )
    body = _post("laptop under 80000, auto-buy below 50000").json()
    auth = body["intent"]["authorization"]
    assert auth["auto_approve_below_inr"] == 50000
    assert body["intent"]["hard_constraints"]["max_price_inr"] == 80000
    # No field anywhere decides whether the purchase is allowed.
    assert "purchase_allowed" not in body
    assert "auto_approved" not in body


# --- Error handling -----------------------------------------------------------


def test_recommendation_provider_failure_returns_502(monkeypatch):
    _install(
        monkeypatch,
        intent_payload=LAPTOP_INTENT,
        rec_exc=AIServiceError("provider API request failed"),
    )
    assert _post("Find me a laptop").status_code == 502


def test_invalid_recommendation_payload_returns_502(monkeypatch):
    # Missing required 'reasoning' -> schema validation failure.
    _install(
        monkeypatch,
        intent_payload=LAPTOP_INTENT,
        rec_payload={"recommended_product_id": "LAP002"},
    )
    assert _post("Find me a laptop").status_code == 502


def test_empty_message_is_rejected():
    assert _post("").status_code == 422
