"""Tests for the decision-pipeline orchestrator and POST /ai/plan.

No real AI call is made (FakeProvider returns canned payloads) and PayPal is
proven untouched (its functions are patched to fail loudly if invoked). The
deterministic parts — product search and the Policy Engine — run for real.
"""

import json

from starlette.testclient import TestClient

from app import paypal
from app.main import app
from app.agents import intent_agent, recommendation_agent
from app.services import purchase_orchestrator
from app.services.providers.base import AIProvider

client = TestClient(app, raise_server_exceptions=False)


class FakeProvider(AIProvider):
    """Canned intent + recommendation payloads; records recommend calls."""

    name = "fake"

    def __init__(self, intent_payload=None, rec_payload=None):
        self._intent_payload = intent_payload
        self._rec_payload = rec_payload
        self.recommend_calls: list[tuple[dict, list[dict]]] = []

    def extract_intent(self, message: str) -> dict:
        return self._intent_payload

    def recommend_product(self, intent: dict, candidates: list[dict]) -> dict:
        self.recommend_calls.append((intent, candidates))
        return self._rec_payload


def _install(monkeypatch, **kwargs) -> FakeProvider:
    provider = FakeProvider(**kwargs)
    monkeypatch.setattr(intent_agent, "get_provider", lambda: provider)
    monkeypatch.setattr(recommendation_agent, "get_provider", lambda: provider)
    return provider


def _post(message: str):
    return client.post("/ai/plan", json={"message": message})


def _rec(product_id="LAP002", **extra) -> dict:
    return {
        "recommended_product_id": product_id,
        "reasoning": "Best fit among the candidates.",
        "matched_constraints": [],
        "tradeoffs": [],
        **extra,
    }


def _laptop_intent(max_price=80000, authorization=None, **extra) -> dict:
    payload = {"category": "laptop", **extra}
    if max_price is not None:
        payload.setdefault("hard_constraints", {})["max_price_inr"] = max_price
    if authorization is not None:
        payload["authorization"] = authorization
    return payload


AUTONOMOUS_50K = {
    "purchase_requested": True,
    "autonomous_purchase_allowed": True,
    "auto_approve_below_inr": 50000,
}


# --- Normal flow --------------------------------------------------------------


def test_normal_flow_full_pipeline(monkeypatch):
    # The phase's canonical message: budget 80000, 16GB RAM required, RTX
    # preferred, autonomy below 50000. Only LAP002 (68,990, 16GB) qualifies.
    _install(
        monkeypatch,
        intent_payload={
            "category": "laptop",
            "use_case": "machine learning",
            "hard_constraints": {"max_price_inr": 80000, "ram_gb_min": 16},
            "preferences": {"gpu": "RTX"},
            "authorization": AUTONOMOUS_50K,
        },
        rec_payload=_rec("LAP002", tradeoffs=["No RTX GPU within budget."]),
    )
    resp = _post(
        "Find me the best laptop for ML under 80000 with 16GB RAM. Prefer RTX. "
        "You may buy automatically only below 50000."
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["status"] == "planned"
    # Pipeline trace present and consistent.
    assert body["intent"]["hard_constraints"]["max_price_inr"] == 80000
    assert {c["id"] for c in body["candidates"]} == {"LAP002"}
    assert body["recommendation"]["recommended_product_id"] == "LAP002"
    # The three sections are distinct and populated.
    assert body["selected_product"]["id"] == "LAP002"          # product info
    assert body["user_authorization"]["auto_approve_below_inr"] == 50000
    decision = body["policy_decision"]                          # policy ruling
    assert decision["decision"] == "REQUIRE_USER_APPROVAL"      # 68,990 > 50,000
    assert decision["reason_code"] == "EXCEEDS_AUTONOMOUS_LIMIT"


# --- Case A: no purchase requested ---------------------------------------------


def test_no_purchase_requested_still_recommends_but_denies(monkeypatch):
    _install(
        monkeypatch,
        intent_payload=_laptop_intent(),  # authorization defaults: no purchase
        rec_payload=_rec("LAP002"),
    )
    body = _post("Find me a laptop under 80000").json()
    assert body["status"] == "planned"
    assert body["recommendation"]["recommended_product_id"] == "LAP002"
    assert body["selected_product"]["id"] == "LAP002"
    assert body["policy_decision"]["decision"] == "DENY"
    assert body["policy_decision"]["reason_code"] == "NO_PURCHASE_REQUESTED"


# --- Case B: no candidates ------------------------------------------------------


def test_no_candidates_skips_recommendation_and_policy(monkeypatch):
    provider = _install(
        monkeypatch,
        intent_payload=_laptop_intent(max_price=10000, authorization=AUTONOMOUS_50K),
        rec_payload=_rec(),
    )
    body = _post("laptop under 10000, auto-buy below 50000").json()
    assert body["status"] == "no_candidates"
    assert body["candidates"] == []
    assert body["recommendation"] is None
    assert body["selected_product"] is None
    assert body["policy_decision"] is None
    assert provider.recommend_calls == []  # no AI credits wasted
    # Authorization is still echoed as inert data.
    assert body["user_authorization"]["auto_approve_below_inr"] == 50000


def test_model_declining_yields_no_policy_decision(monkeypatch):
    _install(
        monkeypatch,
        intent_payload=_laptop_intent(authorization=AUTONOMOUS_50K),
        rec_payload={"recommended_product_id": None, "reasoning": "None fit."},
    )
    body = _post("laptop under 80000 for heavy rendering").json()
    assert body["status"] == "no_suitable_candidate"
    assert body["selected_product"] is None
    assert body["policy_decision"] is None


# --- Case C: within autonomy limit -> ALLOW --------------------------------------


def test_autonomous_purchase_allowed(monkeypatch):
    # LAP006 costs 52,990; limit 60,000 -> ALLOW.
    _install(
        monkeypatch,
        intent_payload=_laptop_intent(
            authorization={
                "purchase_requested": True,
                "autonomous_purchase_allowed": True,
                "auto_approve_below_inr": 60000,
            }
        ),
        rec_payload=_rec("LAP006"),
    )
    body = _post("laptop under 80000, auto-buy below 60000").json()
    decision = body["policy_decision"]
    assert decision["decision"] == "ALLOW_AUTONOMOUS_PURCHASE"
    assert decision["reason_code"] == "WITHIN_AUTONOMOUS_LIMIT"
    assert decision["product_price_inr"] == 52990
    assert decision["authorization_limit_inr"] == 60000


# --- Case D: above autonomy limit -> REQUIRE_USER_APPROVAL -----------------------


def test_approval_required_when_autonomy_limit_exceeded(monkeypatch):
    # LAP002 costs 68,990; limit 50,000 -> REQUIRE_USER_APPROVAL.
    _install(
        monkeypatch,
        intent_payload=_laptop_intent(authorization=AUTONOMOUS_50K),
        rec_payload=_rec("LAP002"),
    )
    decision = _post("laptop under 80000, auto-buy below 50000").json()[
        "policy_decision"
    ]
    assert decision["decision"] == "REQUIRE_USER_APPROVAL"
    assert decision["reason_code"] == "EXCEEDS_AUTONOMOUS_LIMIT"


# --- Case E: shopping budget exceeded -> DENY (policy as backstop) ---------------


def test_policy_denies_over_budget_product_even_if_search_misbehaves(monkeypatch):
    # In the honest pipeline the search already filters by budget, so this
    # simulates a search-layer bug: candidates ignore the 80,000 cap and the
    # model picks LAP001 (124,990). The Policy Engine must still DENY — it is
    # the single source of truth, not the filter.
    from app.services.product_service import get_product

    _install(
        monkeypatch,
        intent_payload=_laptop_intent(authorization=AUTONOMOUS_50K),
        rec_payload=_rec("LAP001"),
    )
    monkeypatch.setattr(
        purchase_orchestrator, "find_candidates", lambda intent: [get_product("LAP001")]
    )
    body = _post("laptop under 80000, auto-buy below 50000").json()
    decision = body["policy_decision"]
    assert decision["decision"] == "DENY"
    assert decision["reason_code"] == "EXCEEDS_SHOPPING_MAX"
    assert decision["product_price_inr"] == 124990


# --- Grounding ------------------------------------------------------------------


def test_recommendation_outside_candidate_set_fails(monkeypatch):
    _install(
        monkeypatch,
        intent_payload=_laptop_intent(authorization=AUTONOMOUS_50K),
        rec_payload=_rec("PHN001"),  # real product, not a laptop candidate
    )
    assert _post("laptop under 80000").status_code == 502


def test_policy_price_comes_from_catalog_not_gemini(monkeypatch):
    # The fake model tries to smuggle a 1-rupee price alongside its pick; the
    # Policy Engine must be fed the catalog price (68,990 -> above the 50,000
    # limit -> REQUIRE_USER_APPROVAL, not ALLOW).
    _install(
        monkeypatch,
        intent_payload=_laptop_intent(authorization=AUTONOMOUS_50K),
        rec_payload=_rec("LAP002", price_inr=1, product_price_inr=1),
    )
    body = _post("laptop under 80000, auto-buy below 50000").json()
    decision = body["policy_decision"]
    assert decision["product_price_inr"] == 68990
    assert decision["decision"] == "REQUIRE_USER_APPROVAL"
    assert body["selected_product"]["price_inr"] == 68990


def test_authorization_limit_is_not_a_product_filter(monkeypatch):
    # Budget 80,000 with autonomy limit 50,000: LAP002 (68,990) must still be
    # a candidate and reach the model.
    provider = _install(
        monkeypatch,
        intent_payload=_laptop_intent(authorization=AUTONOMOUS_50K),
        rec_payload=_rec("LAP002"),
    )
    assert _post("laptop under 80000, auto-buy below 50000").status_code == 200
    (_, candidates_sent), = provider.recommend_calls
    assert {c["id"] for c in candidates_sent} == {"LAP002", "LAP006"}


# --- PayPal isolation -------------------------------------------------------------


def test_plan_makes_no_paypal_calls(monkeypatch):
    def _forbidden(*args, **kwargs):
        raise AssertionError("PayPal must never be called by the plan pipeline")

    for name in (
        "get_access_token",
        "create_order",
        "capture_order",
        "get_order",
        "refund_capture",
    ):
        monkeypatch.setattr(paypal, name, _forbidden)

    _install(
        monkeypatch,
        intent_payload=_laptop_intent(authorization=AUTONOMOUS_50K),
        rec_payload=_rec("LAP006"),
    )
    resp = _post("laptop under 80000, auto-buy below 50000")
    assert resp.status_code == 200  # would be a 500 if PayPal had been touched


def test_response_contains_no_payment_artifacts(monkeypatch):
    # Even an ALLOW decision must not come with any payment handle.
    _install(
        monkeypatch,
        intent_payload=_laptop_intent(
            authorization={
                "purchase_requested": True,
                "autonomous_purchase_allowed": True,
                "auto_approve_below_inr": 60000,
            }
        ),
        rec_payload=_rec("LAP006"),
    )
    body = _post("laptop under 80000, auto-buy below 60000").json()
    assert body["policy_decision"]["decision"] == "ALLOW_AUTONOMOUS_PURCHASE"
    text = json.dumps(body)
    for artifact in ("order_id", "capture_id", "approve_url", "refund_id", "payer"):
        assert artifact not in text


# --- Input validation -------------------------------------------------------------


def test_empty_message_is_rejected():
    assert _post("").status_code == 422
