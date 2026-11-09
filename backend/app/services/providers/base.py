"""AIProvider interface, shared prompt, and provider-neutral exceptions.

A provider's only job is: natural-language message -> raw intent payload (dict).
Validation into ProductIntent happens in the Intent Agent, so the error types
here are vendor-neutral and map cleanly to HTTP status codes upstream.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

# Shared across providers so every backend extracts the same structure and
# applies the same hard-vs-soft rule. Kept vendor-neutral (no tool/function
# mechanics here — each provider adapts it to its own structured-output API).
INTENT_SYSTEM_PROMPT = (
    "You are PayPilot's intent parser. Convert the user's shopping request into "
    "a single structured intent object matching the provided schema.\n\n"
    "SHOPPING CONSTRAINTS vs soft PREFERENCES:\n"
    "- HARD constraints are mandatory: budget caps ('under 80000', 'below'), "
    "minimums ('at least 16GB RAM', 'minimum 4 stars'), and features the user "
    "says they need or must have. Put these in hard_constraints.\n"
    "- PREFERENCES are desires: 'prefer', 'ideally', 'would like', 'nice to "
    "have'. Put these in preferences (e.g. a preferred RTX GPU -> preferences.gpu).\n\n"
    "TWO DIFFERENT KINDS OF NUMBER — keep them separate:\n"
    "A. hard_constraints.max_price_inr is the PRODUCT SHOPPING BUDGET (how much "
    "the item may cost).\n"
    "B. authorization.auto_approve_below_inr is a FINANCIAL AUTHORIZATION "
    "threshold (how much the user lets PayPilot spend automatically without "
    "asking). These are NOT the same. NEVER copy an authorization threshold "
    "into max_price_inr, and never copy the shopping budget into "
    "auto_approve_below_inr.\n\n"
    "AUTHORIZATION rules:\n"
    "- authorization.purchase_requested = true if the user expresses ANY "
    "intention to eventually purchase, INCLUDING a conditional one ('buy it', "
    "'find and buy', 'you may buy it if...'). false for pure 'find/search/"
    "show/compare' with no buy intent.\n"
    "- authorization.autonomous_purchase_allowed = true ONLY if the user "
    "explicitly grants automatic/autonomous buying ('you may buy it "
    "automatically if...', 'buy it but ask me before spending more than...'). "
    "A plain 'buy me X' is a purchase request but NOT autonomous authority.\n"
    "- authorization.auto_approve_below_inr = the spend ceiling under which "
    "autonomous purchase is allowed, when the user gives one.\n"
    "A conditional purchase authorization is NOT a product preference — record "
    "it in authorization, never in preferences.\n\n"
    "EXAMPLES:\n"
    "A. 'Find me a laptop under 80000.' -> max_price_inr=80000; "
    "purchase_requested=false; autonomous_purchase_allowed=false; "
    "auto_approve_below_inr=null.\n"
    "B. 'Find and buy me a laptop under 80000.' -> max_price_inr=80000; "
    "purchase_requested=true; autonomous_purchase_allowed=false; "
    "auto_approve_below_inr=null.\n"
    "C. 'Find me a laptop under 80000. You may buy it automatically if it costs "
    "below 50000.' -> max_price_inr=80000; purchase_requested=true; "
    "autonomous_purchase_allowed=true; auto_approve_below_inr=50000.\n"
    "D. 'Buy it, but ask me before spending more than 50000.' -> "
    "purchase_requested=true; autonomous_purchase_allowed=true; "
    "auto_approve_below_inr=50000.\n\n"
    "Leave fields null/empty when the user did not mention them. Do not invent "
    "constraints. You only record intent as data; you never decide to spend."
)


# Shared recommendation prompt. The model picks ONE candidate by catalog id;
# it is grounded hard against hallucination and explicitly scoped to
# recommendation only (no purchasing, no authorization decisions).
RECOMMENDATION_SYSTEM_PROMPT = (
    "You are PayPilot's product recommender. You receive a structured shopping "
    "intent and a CANDIDATES list of catalog products that already satisfy "
    "every hard constraint. Pick the single most suitable candidate.\n\n"
    "GROUNDING RULES (absolute):\n"
    "- Only recommend a product that appears in the CANDIDATES list.\n"
    "- Never invent products, prices, or specifications. Every attribute you "
    "mention must come verbatim from the candidate data — use catalog values "
    "exactly.\n"
    "- recommended_product_id MUST be the 'id' field of one candidate, copied "
    "exactly.\n"
    "- If no candidate is genuinely suitable, set recommended_product_id to "
    "null and explain why in reasoning. NEVER relax or ignore constraints "
    "silently.\n\n"
    "HOW TO CHOOSE:\n"
    "- All candidates already satisfy the hard constraints; use the intent's "
    "soft preferences, use case, and the products' attributes (rating, value "
    "for money, relevant specs) to pick the best fit.\n"
    "- matched_constraints: which constraints/preferences the pick satisfies.\n"
    "- tradeoffs: honest compromises versus the user's stated preferences "
    "(e.g. a preferred GPU brand not available within budget).\n\n"
    "You only recommend. You never purchase, never authorize spending, and "
    "never evaluate spending thresholds."
)


# Shared routing prompt. Classification ONLY: the model names one of four
# routes and may summarize the request — it executes nothing and the schema
# gives it no field to carry a price, id, or decision.
ROUTING_SYSTEM_PROMPT = (
    "You are PayPilot's conversation router. Classify the user's message "
    "into exactly ONE route:\n\n"
    "- SHOP: the user wants to find, compare, or buy a product (e.g. 'find "
    "me a laptop under 80000', 'I want cheap headphones', 'buy me the best "
    "monitor for coding').\n"
    "- REFUND: the user wants money back, to return an item, or mentions a "
    "refund for a purchase (e.g. 'I want a refund', 'the laptop arrived "
    "damaged, I want my money back', 'can I return that purchase?').\n"
    "- HISTORY: the user asks about past purchases, transactions, or order "
    "status (e.g. 'show me my purchases', 'what have I bought?', 'what's "
    "the status of my order?').\n"
    "- SMALLTALK: greetings, thanks, chit-chat, or questions about the "
    "assistant itself (e.g. 'hi', 'thanks', 'what can you do?').\n\n"
    "Rules:\n"
    "- route MUST be exactly one of: SHOP, REFUND, HISTORY, SMALLTALK. "
    "There is no other route; never invent one.\n"
    "- If a message mixes concerns, pick the dominant actionable one "
    "(a refund mention wins over pleasantries around it).\n"
    "- summary: one short sentence restating what the user wants, nothing "
    "else.\n"
    "- You ONLY classify. You never select products, quote prices, decide "
    "eligibility, authorize spending, or execute anything."
)


# Shared product-ranking prompt (Phase 10E). The model orders candidate IDs
# it was handed and explains the order. It is told, in the strongest terms,
# that it may not create facts — and the ProductRanking schema gives it no
# field in which to do so even if it tried.
RANKING_SYSTEM_PROMPT = (
    "You are a product comparison ranking component. You receive the user's "
    "REQUIREMENTS and a CANDIDATES list of products that were already "
    "discovered and validated by the backend. Rank the candidates from best "
    "to worst fit.\n\n"
    "ABSOLUTE RULES:\n"
    "- You may rank ONLY the supplied candidate ids. Copy each id exactly as "
    "given.\n"
    "- Never output an id that is not in CANDIDATES, and never repeat an id.\n"
    "- Do NOT invent products, prices, currencies, URLs, images, product "
    "ids, specifications, ratings, availability, brands, or merchants. Do "
    "not invent any product attribute whatsoever.\n"
    "- Every factual claim in a reason must come from the supplied candidate "
    "data. If the data does not state something, you may not state it.\n"
    "- Some candidates may have missing fields (no description, no price, no "
    "brand). Say the information is not available; never fill the gap.\n\n"
    "CURRENCY:\n"
    "- Do not perform currency conversion. Use normalized_price when "
    "supplied. Do not invent or recompute prices.\n"
    "- normalized_price is a value the backend already DERIVED in "
    "normalized_currency; quote it as given or not at all. Never multiply, "
    "divide, or estimate an exchange rate, and never convert between "
    "currencies yourself.\n"
    "- observed_price is in observed_currency. Never compare an amount in "
    "one currency against an amount in another.\n\n"
    "WRITING REASONS:\n"
    "- Scope every claim to the candidate set you were given.\n"
    "- GOOD: 'Lower observed price among the candidates while matching the "
    "requested category.'\n"
    "- GOOD: 'Best match among the returned candidates for the stated use "
    "case.'\n"
    "- BAD: 'Has excellent 40-hour battery life.' (unless the candidate data "
    "actually says so)\n"
    "- BAD: 'Best on the market', 'highest quality', 'best rated', "
    "'cheapest online' — you cannot see the market, only these candidates.\n"
    "- You may explain tradeoffs between candidates, but only using supplied "
    "fields.\n\n"
    "Return only the requested structured JSON: the ranked candidate ids and "
    "one short reason per ranked id.\n\n"
    "You ONLY rank and explain. You never select a payment amount, never "
    "decide eligibility to purchase, never authorize spending, and never "
    "execute anything. Observed prices in the candidate data are retailer "
    "observations, not payment amounts."
)


class AIProviderError(Exception):
    """Base class for all provider errors."""


class AIConfigError(AIProviderError):
    """Provider is not configured (e.g. missing API key) — maps to HTTP 503."""


class AIServiceError(AIProviderError):
    """The provider's API call failed (network/status) — maps to HTTP 502."""


class InvalidAIResponseError(AIProviderError):
    """The provider returned no usable structured output — maps to HTTP 502."""


class AIProvider(ABC):
    """Interface every AI backend implements."""

    #: Short provider name, e.g. "gemini" or "claude".
    name: str = "base"

    @abstractmethod
    def extract_intent(self, message: str) -> dict:
        """Return a raw intent payload dict for `message`.

        Implementations must raise AIConfigError / AIServiceError /
        InvalidAIResponseError as appropriate. They must NOT validate against
        ProductIntent — that is the Intent Agent's responsibility.
        """
        raise NotImplementedError

    def recommend_product(self, intent: dict, candidates: list[dict]) -> dict:
        """Return a raw recommendation payload dict for the given candidates.

        `intent` is the shopping portion of a ProductIntent (the caller strips
        authorization — financial data is not the recommender's business) and
        `candidates` are catalog products that already satisfy every hard
        constraint. Implementations must only choose among `candidates` and
        must raise the same provider-neutral errors as extract_intent.
        Validation (including that the id exists in the candidate list) is the
        Recommendation Agent's responsibility.

        Concrete (not abstract) so providers that only support intent
        extraction remain valid subclasses.
        """
        raise NotImplementedError(
            f"The '{self.name}' provider does not implement recommend_product"
        )

    def classify_route(self, message: str) -> dict:
        """Return a raw routing payload dict for `message`.

        Classification only — implementations must raise the same
        provider-neutral errors as extract_intent and must NOT validate
        against RouteResult; that is the Router Agent's responsibility.

        Concrete (not abstract) so existing providers remain valid
        subclasses even if they don't support routing.
        """
        raise NotImplementedError(
            f"The '{self.name}' provider does not implement classify_route"
        )

    def rank_products(
        self, requirements: dict, candidates: list[dict], top_k: int
    ) -> dict:
        """Return a raw ranking payload dict for already-validated candidates.

        Args:
            requirements: The user's shopping requirements as plain data. The
                caller is responsible for excluding anything financial — no
                authorization block, no payment amount, no PayPal/transaction/
                capture/refund identifier, no session state ever belongs here.
            candidates: Deterministically filtered discovery records, reduced
                to a small set by the caller. Each carries only the
                descriptive fields needed for ranking.
            top_k: How many ranked ids the caller wants, best first.

        Implementations must raise the same provider-neutral errors as
        extract_intent. Validation — that every returned id was actually
        supplied, that none repeats, and that nothing else became
        application state — is the Comparison Agent's responsibility, never
        the provider's.

        Concrete (not abstract) so providers that only support intent
        extraction remain valid subclasses.
        """
        raise NotImplementedError(
            f"The '{self.name}' provider does not implement rank_products"
        )
