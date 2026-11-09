"""PayPilot FastAPI application (Phase 1).

Only routing lives here. The single endpoint verifies that PayPal OAuth 2.0
authentication works by requesting a token via paypal.py. The token itself is
NEVER returned to the client — we only report success plus safe metadata.
"""

from __future__ import annotations

import json
import logging
import os

from decimal import Decimal, InvalidOperation

from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from .services.product_service import (
    Product,
    ProductNotFoundError,
    get_product,
    list_products,
    search_products,
)
from .services.providers import (
    AIConfigError,
    AIServiceError,
    InvalidAIResponseError,
)
from .services.product_search import find_candidates
from .schemas.discovery import DiscoverySearchResponse
from .schemas.comparison import ComparisonRequest, ComparisonResponse
from .schemas.adoption import AdoptedProduct, AdoptionRequest
from .schemas.revalidation import RevalidationResponse
from .schemas.external_policy import ExternalPolicyEvaluation
from .schemas.external_approval import (
    ExternalApproval,
    ExternalApprovalRequestResult,
    ExternalApprovalResolveRequest,
)
from .services.external_policy_service import (
    evaluate_adopted_product as evaluate_adopted_product_policy,
)
from .services.external_approval_service import (
    ApprovalConfigError,
    ApprovalConflictError,
    ApprovalNotFoundError,
    request_approval as request_external_approval,
    resolve_approval as resolve_external_approval,
)
from .schemas.external_purchase import (
    ExternalApprovalVerification,
    ExternalCaptureAuthorization,
    ExternalCaptureResult,
    ExternalExecutionTriage,
    ExternalOrderResult,
    ExternalOrderStatus,
    ExternalOrderView,
    ExternalReconcileReport,
    UnresolvedExecutionsResponse,
)
from .services.external_purchase_service import (
    ExternalApprovalVerificationError,
    ExternalApprovalVerificationFailure,
    ExternalCaptureConfigError,
    ExternalCaptureFailure,
    ExternalCaptureRefusedError,
    ExternalExecutionNotFoundError,
    ExternalOrderConflictError,
    UNRESOLVED_STATUSES,
    authorize_capture as authorize_external_capture,
    capture_external_order,
    create_order_for_approval as create_external_order,
    get_by_approval as get_external_order_by_approval,
    list_by_statuses as list_external_executions_by_statuses,
    list_stale_creating as list_stale_creating_external_orders,
    reconcile_capture as reconcile_external_capture,
    reconcile_creating_order as reconcile_external_creating_order,
    recover_creating_order as recover_external_creating_order,
    verify_buyer_approval as verify_external_buyer_approval,
)
from .services.discovery_service import search_discovery
from .services.discovery_comparison_service import compare_discovery
from .services.adoption_service import adopt as adopt_discovered_product
from .services.adoption_service import AdoptedProductNotFoundError
from .services.revalidation_service import revalidate as revalidate_adopted_product
from .services.discovery_evidence_store import (
    DiscoveryEvidenceNotFoundError,
    MalformedDiscoveryEvidenceError,
)
from .agents.comparison_agent import RankingValidationError
from .services.discovery_providers.base import (
    DiscoveryAuthError,
    DiscoveryConfigError,
    DiscoveryInvalidResponseError,
    DiscoveryQuotaError,
    DiscoveryRateLimitError,
    DiscoveryRequestError,
    DiscoveryUnavailableError,
)
from .agents.intent_agent import IntentValidationError, interpret as interpret_intent
from .agents.recommendation_agent import (
    RecommendationValidationError,
    recommend as recommend_from_candidates,
)
from .schemas.intent import IntentRequest, ProductIntent
from .schemas.recommendation import RecommendRequest, RecommendResponse
from .schemas.policy import PolicyDecision, PolicyEvaluateRequest
from .schemas.plan import PlanRequest, PlanResponse
from .policy import evaluate_policy
from .services.purchase_orchestrator import plan as build_plan
from .schemas.execution import CaptureResult, ExecuteRequest, ExecutionResult
from .services.currency_service import CurrencyConversionError
from .services.purchase_execution_service import (
    CaptureLedgerDesyncError,
    CaptureNotCompletedError,
    approve_purchase,
    capture_purchase,
    execute_purchase,
    reconcile_creating_attempt as reconcile_catalog_attempt,
    recover_creating_attempt as recover_catalog_attempt,
)
from .services import catalog_attempt_service
from .schemas.catalog_attempt import (
    CatalogAttemptReconcileReport,
    CatalogAttemptTriage,
    UnresolvedCatalogAttemptsResponse,
)
from .services.order_ownership_service import (
    EXTERNAL_CAPTURE_NOT_REFUNDABLE_HERE,
    EXTERNAL_ORDER_NOT_CAPTURABLE_HERE,
    OwnershipRefusedError,
    UNKNOWN_CAPTURE,
    UNKNOWN_ORDER,
    require_catalog_capture,
)
from .services.webhook_service import (
    MissingWebhookHeadersError,
    WebhookConfigError,
    WebhookVerificationError,
    verify_webhook,
)
from .services.webhook_reconciliation_service import reconcile_event
from .services.external_reconciliation_service import reconcile_external_event
from .services.catalog_reconciliation_service import reconcile_catalog_attempt_event
from .services import auth_service
from .services.refund_service import (
    AlreadyRefundedError,
    MissingCaptureIdError,
    RefundInProgressError,
    RefundLedgerDesyncError,
    RefundNotAllowedError,
    refund_transaction,
)
from .agents.router_agent import RouteValidationError
from .schemas.chat import ChatRequest, ChatResponse
from .schemas.confirmation import ConfirmationResponse, ConfirmRequest
from .services.chat_service import handle_chat
from .services.confirmation_service import (
    NoPendingActionError,
    confirm_pending_action,
)
from .schemas.session import SessionDetail
from .services.session_service import (
    SessionNotFoundError,
    get_pending_action,
    get_session,
    get_turns,
)
from .schemas.transaction import (
    TransactionListResponse,
    TransactionRecord,
    TransactionStatus,
)
from .services.transaction_service import (
    TransactionNotFoundError,
    get_transaction,
    get_transaction_by_order,
    list_transactions,
)
from .paypal import (
    PayPalAlreadyCapturedError,
    PayPalAlreadyRefundedError,
    PayPalAuthError,
    PayPalCaptureError,
    PayPalCheckoutConfigError,
    PayPalCaptureNotFoundError,
    PayPalOrderNotApprovedError,
    PayPalGetOrderError,
    PayPalOrderError,
    PayPalOrderNotFoundError,
    PayPalRefundError,
    create_order,
    get_access_token,
    get_order,
)

# Basic logging config so our INFO/ERROR messages are visible in the console.
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("paypilot.main")

# ---------------------------------------------------------------------------
# Single-operator authentication gate (Phase 11F-H).
#
# A global dependency protects EVERY route by default; only the explicit
# public allowlist below is exempt, so a route added later is protected
# unless deliberately exempted here (fail closed). The webhook stays public
# HERE and keeps its OWN PayPal signature verification as its gate -- it must
# not require an operator token, because PayPal cannot present one. This gate
# establishes caller identity for a SINGLE operator; it is not, and must not
# be described as, multi-user isolation.
# ---------------------------------------------------------------------------

#: (method, exact path) pairs that need no operator token. GET
#: /products/{product_id} varies, so it is matched by prefix in _is_public.
_PUBLIC_EXACT = {
    ("GET", "/"),
    ("GET", "/products"),
    ("GET", "/products/search"),
    ("POST", "/auth/login"),
    ("POST", "/webhooks/paypal"),
}


def _is_public(method: str, path: str) -> bool:
    """True only for the explicit public allowlist."""
    normalized = path.rstrip("/") or "/"
    if (method, normalized) in _PUBLIC_EXACT:
        return True
    # GET /products/{product_id}: a single product id, never a deeper path
    # and never /products/search (already matched above).
    if method == "GET" and normalized.startswith("/products/"):
        remainder = normalized[len("/products/") :]
        if remainder and "/" not in remainder:
            return True
    return False


def require_operator(request: Request) -> None:
    """Global gate: reject a protected request lacking a valid operator token.

    Runs BEFORE every route function, so an unauthenticated call to a
    protected route returns 401 without the protected operation executing.
    Public routes and the signature-verified webhook are exempt. Fails
    closed: when auth is not configured, every protected route is 401. This
    does NOT replace any existing safeguard (ownership gates, policy,
    buyer-approval verification, capture authorization) -- it is an
    additional gate in front of them.
    """
    if _is_public(request.method, request.url.path):
        return
    if not auth_service.is_configured():
        raise HTTPException(
            status_code=401,
            detail="Authentication is not configured on the server",
            headers={"WWW-Authenticate": "Bearer"},
        )
    header = request.headers.get("authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=401,
            detail="Missing or malformed bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        auth_service.verify_token(token.strip())
    except auth_service.AuthConfigError as exc:
        raise HTTPException(
            status_code=401,
            detail="Authentication is not configured on the server",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except auth_service.InvalidTokenError as exc:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


app = FastAPI(
    title="PayPilot",
    version="0.1.0",
    dependencies=[Depends(require_operator)],
)


def _cors_origins() -> list[str]:
    """Allowed browser origins for the frontend (Phase 8E Step 6).

    Comma-separated PAYPILOT_CORS_ORIGINS overrides the default, which is
    the local Vite dev server. Explicit origins only — never "*" — so
    enabling credentials later cannot silently open the API to any site.
    """
    env = os.getenv("PAYPILOT_CORS_ORIGINS", "").strip()
    if env:
        return [origin.strip() for origin in env.split(",") if origin.strip()]
    return ["http://localhost:5173"]


# Transport-level only: CORS decides which browser origins may read
# responses; it changes no business logic, route, or validation.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=False,  # no auth/cookies yet (flip only with auth)
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root() -> dict:
    """Liveness check — confirms the server is up."""
    return {"service": "PayPilot", "status": "running"}


@app.get("/auth/health")
def auth_health() -> dict:
    """Verify PayPal authentication works.

    Calls get_access_token(); if it succeeds we know credentials and the
    Sandbox connection are valid. We deliberately do NOT return the token or
    the client secret — only a boolean plus the token length as proof.
    """
    try:
        token = get_access_token()
    except PayPalAuthError as exc:
        # Surface a clean error without leaking any secret material.
        logger.error("PayPal authentication failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "paypal_authenticated": True,
        "token_length": len(token),  # proof a token exists, without exposing it
    }


class LoginRequest(BaseModel):
    """Operator login body: the shared API key, and nothing else."""

    api_key: str


@app.post("/auth/login")
def auth_login(req: LoginRequest) -> dict:
    """Exchange the operator API key for a short-lived bearer token (public).

    The key is checked with a constant-time comparison; neither the key nor
    the signing secret is ever returned or logged. A wrong key is 401; an
    unconfigured server is 503 (fail closed -- there is no universal default
    key or signing secret). The token must be sent as
    `Authorization: Bearer <token>` on every protected route.
    """
    try:
        token = auth_service.login(req.api_key)
    except auth_service.AuthConfigError as exc:
        logger.error("Operator login attempted but auth is not configured: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Authentication is not configured on the server",
        ) from exc
    except auth_service.InvalidCredentialError as exc:
        # Deliberately generic: never reveal whether the key was close.
        raise HTTPException(status_code=401, detail="Invalid operator credential") from exc

    return {
        "token": token,
        "token_type": "bearer",
        "expires_in": auth_service.DEFAULT_TOKEN_TTL_SECONDS,
    }


class CreateOrderRequest(BaseModel):
    """Request body for creating an order, e.g. {"amount": "50.00"}."""

    amount: str
    currency: str = "USD"
    description: Optional[str] = None  # shown at checkout; PayPal caps at 127 chars

    @field_validator("amount")
    @classmethod
    def _validate_amount(cls, value: str) -> str:
        # Must be a positive number with at most two decimal places.
        try:
            parsed = Decimal(value)
        except (InvalidOperation, TypeError) as exc:
            raise ValueError("amount must be a numeric string like '50.00'") from exc
        if parsed <= 0:
            raise ValueError("amount must be greater than zero")
        if parsed.as_tuple().exponent < -2:
            raise ValueError("amount can have at most two decimal places")
        # Normalise to exactly two decimals, which is what PayPal expects.
        return f"{parsed:.2f}"

    @field_validator("currency")
    @classmethod
    def _validate_currency(cls, value: str) -> str:
        code = value.strip().upper()
        if len(code) != 3 or not code.isalpha():
            raise ValueError("currency must be a 3-letter ISO code like 'USD'")
        return code


@app.post("/paypal/create-order")
def paypal_create_order(req: CreateOrderRequest) -> dict:
    """Create a PayPal order for the given amount and currency.

    Obtains an OAuth token, calls the Orders v2 API, and returns the order id,
    status, and links so the client can continue the checkout flow.
    """
    try:
        order = create_order(req.amount, req.currency, description=req.description)
    except PayPalCheckoutConfigError as exc:
        # Deployment fault: PAYPAL_RETURN_URL / PAYPAL_CANCEL_URL is not an
        # absolute https URL. Not an upstream PayPal failure, so not a 502 —
        # and no order was created, because the check runs first.
        logger.error("Checkout URL configuration is invalid: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except PayPalAuthError as exc:
        logger.error("Auth failed while creating order: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except PayPalOrderError as exc:
        logger.error("Create-order failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return order


#: HTTP status for each deterministic ownership refusal (Phase 11F-B0).
#: EXTERNAL_ORDER_NOT_CAPTURABLE_HERE is a conflict with the order's real
#: owner: the order exists, but not in the ledger this route serves.
#: UNKNOWN_* is a not-found: this server holds no record of the identifier,
#: so there is nothing here to act on.
_OWNERSHIP_REFUSAL_STATUS = {
    EXTERNAL_ORDER_NOT_CAPTURABLE_HERE: 409,
    EXTERNAL_CAPTURE_NOT_REFUNDABLE_HERE: 409,
    UNKNOWN_ORDER: 404,
    UNKNOWN_CAPTURE: 404,
}


def _ownership_refusal(exc: OwnershipRefusedError) -> HTTPException:
    """Map an ownership refusal to a deterministic, structured HTTP error.

    Structured like the Phase 11D/11E conflicts: the client must be able to
    read WHICH condition refused the request from a `code`, without parsing
    prose. An unrecognised code would fail closed as a 409 rather than
    falling through to a success — but every code this module defines is
    mapped above.
    """
    return HTTPException(
        status_code=_OWNERSHIP_REFUSAL_STATUS.get(exc.code, 409),
        detail=exc.as_detail(),
    )


@app.post(
    "/paypal/capture-order/{order_id}",
    responses={
        404: {
            "description": "No transaction of this server owns the order id "
            "(UNKNOWN_ORDER). No PayPal capture was attempted."
        },
        409: {
            "description": "The order belongs to an external purchase "
            "execution, not to a local-catalog transaction "
            "(EXTERNAL_ORDER_NOT_CAPTURABLE_HERE), or PayPal reports a state "
            "conflict (not approved / already captured)."
        },
    },
)
def paypal_capture_order(order_id: str) -> dict:
    """Capture payment for an approved, CATALOG-OWNED PayPal order.

    OWNERSHIP IS VALIDATED SERVER-SIDE FIRST (Phase 11F-B0). This route is
    deliberately NOT an independent path to PayPal any more: it delegates to
    the same capture_purchase() service the purchase flow uses, so it passes
    the same ownership gate and performs the same ledger transition. The
    order id must be owned by a local-catalog `transactions` row; an id
    owned by Phase 11E's external_purchase_orders is refused with 409
    EXTERNAL_ORDER_NOT_CAPTURABLE_HERE and an id owned by neither with 404
    UNKNOWN_ORDER, in both cases without any PayPal call.

    The response shape is unchanged for successful captures (order id, order
    status, capture id, capture status, amount, currency), so existing
    callers of this route are unaffected for the one case that was ever
    legitimate: capturing an order this server created.

    The order must still be APPROVED by the buyer (via the approve_url from
    create-order); PayPal remains the authority on that. Both statuses are
    returned so a caller never mistakes a COMPLETED order for guaranteed-
    collected funds.
    """
    try:
        result = capture_purchase(order_id)
    except ValueError as exc:
        # Empty/blank order id — rejected before any lookup or PayPal call.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OwnershipRefusedError as exc:
        # External or unknown order: refused with no PayPal call made.
        raise _ownership_refusal(exc) from exc
    except PayPalAuthError as exc:
        logger.error("Auth failed while capturing order: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except PayPalOrderNotFoundError as exc:
        # Owned locally but gone at PayPal → 404, not a generic upstream error.
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (PayPalAlreadyCapturedError, PayPalOrderNotApprovedError) as exc:
        # Conflict with the order's current state (already captured, or the
        # buyer has not approved yet) → 409.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PayPalCaptureError as exc:
        logger.error("Capture-order failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except CaptureLedgerDesyncError as exc:
        # PayPal DID capture and nothing rolled it back — see the handler on
        # /purchase/capture for why this is a 500 and not a fake success.
        logger.error("Capture ledger desync on /paypal/capture-order: %s", exc)
        raise HTTPException(status_code=500, detail=exc.as_detail()) from exc
    except CaptureNotCompletedError as exc:
        # PayPal accepted the capture but the nested status was not COMPLETED
        # (e.g. PENDING). Funds are NOT known collected and the ledger was left
        # unchanged; report a non-success so no false capture is shown.
        logger.warning("Catalog capture not completed on /paypal/capture-order: %s", exc)
        raise HTTPException(status_code=502, detail=exc.as_detail()) from exc

    # Wire shape preserved exactly as this route returned it before the gate:
    # PayPal's own capture facts, with the ORDER status under "status".
    return {
        "order_id": result.order_id,
        "status": result.order_status,
        "capture_id": result.capture_id,
        "capture_status": result.capture_status,
        "amount": result.amount,
        "currency": result.currency,
    }


@app.get("/paypal/order/{order_id}")
def paypal_get_order(order_id: str) -> dict:
    """Retrieve the current status of a PayPal order (read-only).

    Returns the order id, status, intent, purchase units, and any capture
    information. Does not mutate anything on PayPal's side.
    """
    try:
        result = get_order(order_id)
    except PayPalAuthError as exc:
        logger.error("Auth failed while retrieving order: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except PayPalOrderNotFoundError as exc:
        # Distinct, expected case: the id doesn't exist → 404, not 502.
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PayPalGetOrderError as exc:
        logger.error("Get-order failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return result


@app.post(
    "/paypal/refund/{capture_id}",
    responses={
        404: {
            "description": "No transaction of this server stores the capture "
            "id (UNKNOWN_CAPTURE), or PayPal has no such capture. No PayPal "
            "refund was attempted in the former case."
        },
        409: {
            "description": "The owning transaction is not refundable: it is "
            "not CAPTURED, or it is already REFUNDED. PayPal is not called."
        },
    },
)
def paypal_refund(capture_id: str) -> dict:
    """Issue a full refund for a captured payment this server owns.

    ELIGIBILITY IS ENFORCED SERVER-SIDE (Phase 11F-B0). A client-supplied
    capture id is not authorization to refund, so this route no longer hands
    the path value to PayPal. It resolves the capture id to the local
    transaction that stores it and then delegates to the SAME deterministic
    refund service as POST /transactions/{transaction_id}/refund: only a
    CAPTURED transaction with a stored capture id is refundable, a REFUNDED
    one is refused without a second PayPal call, and the capture id sent
    upstream is the server-stored one.

    A capture id no transaction stores is refused with 404 UNKNOWN_CAPTURE
    and no PayPal call. That necessarily includes any external capture:
    Phase 11E's external_purchase_orders has no capture-id column, so an
    external capture cannot be refunded through this catalog-only path.

    Full refunds only, as before. The response is the refund service's
    summary — PayPal's refund id, status, amount and currency plus the
    ledger ids involved; note that the pre-11F-B0 `refunded_amount` key is
    now `amount` (see the compatibility note in the closeout report).
    """
    try:
        txn = require_catalog_capture(capture_id)
    except ValueError as exc:
        # Blank/invalid capture id — rejected before any lookup or PayPal call.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OwnershipRefusedError as exc:
        raise _ownership_refusal(exc) from exc

    # Owned locally: the existing deterministic rules decide from here, and
    # they refund the STORED capture id, never the path value.
    try:
        return refund_transaction(txn.transaction_id)
    except TransactionNotFoundError as exc:
        # The row vanished between resolution and refund — fail closed.
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AlreadyRefundedError as exc:
        # Idempotency guard: already REFUNDED locally → PayPal is not called.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RefundInProgressError as exc:
        # A concurrent refund already claimed the transaction → PayPal was not
        # called by this request; fail closed with a conflict.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RefundNotAllowedError as exc:
        # Deterministic eligibility: only CAPTURED is refundable.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MissingCaptureIdError as exc:
        logger.error("Refund blocked by inconsistent record: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except RefundLedgerDesyncError as exc:
        # Refund succeeded at PayPal but the ledger write failed; surfaced as an
        # operator-actionable inconsistency, never a false success.
        logger.error("Refund ledger desync on /paypal/refund: %s", exc)
        raise HTTPException(status_code=500, detail=exc.as_detail()) from exc
    except PayPalAuthError as exc:
        logger.error("Auth failed while refunding capture: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except PayPalCaptureNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PayPalAlreadyRefundedError as exc:
        # Conflict with current state: the capture is already fully refunded.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PayPalRefundError as exc:
        # Includes PayPalRefundNetworkError (ambiguous): the row is left
        # REFUNDING for reconciliation; reported as an upstream failure.
        logger.error("Refund failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Product catalog (local, read-only, independent of PayPal).
# ---------------------------------------------------------------------------


@app.get("/products", response_model=list[Product])
def products_list() -> list[Product]:
    """Return the full local product catalog."""
    return list_products()


@app.get("/products/search", response_model=list[Product])
def products_search(
    category: Optional[str] = Query(default=None, description="Exact category, e.g. 'laptops'"),
    max_price: Optional[int] = Query(default=None, ge=0, description="Max price in INR (inclusive)"),
    min_rating: Optional[float] = Query(default=None, ge=0, le=5, description="Min rating (inclusive)"),
    keyword: Optional[str] = Query(default=None, description="Substring of name/brand/description"),
) -> list[Product]:
    """Filter the catalog by category, max price, min rating, and/or keyword.

    Declared before /products/{product_id} so "search" is not treated as an id.
    All filters are optional and combined with AND.
    """
    return search_products(
        category=category,
        max_price=max_price,
        min_rating=min_rating,
        keyword=keyword,
    )


@app.get("/products/{product_id}", response_model=Product)
def products_get(product_id: str) -> Product:
    """Return a single product by id, or 404 if it does not exist."""
    try:
        return get_product(product_id)
    except ProductNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# External product discovery (Phase 10C — read-only, discovery evidence only).
# Normalized Channel3 search results. No AI, no PayPal, no Policy Engine, no
# catalog/session/transaction mutation; observed prices are NEVER payment
# amounts, and there is deliberately no bridge to purchase execution.
# ---------------------------------------------------------------------------


@app.get("/discovery/search", response_model=DiscoverySearchResponse)
def discovery_search(
    q: str = Query(min_length=1, description="Free-text product search query."),
    limit: int = Query(
        default=10, ge=1, le=30, description="Results per page (Channel3: 1-30)."
    ),
    page_token: Optional[str] = Query(
        default=None, description="Continuation token from a previous response."
    ),
) -> DiscoverySearchResponse:
    """Search external products via Channel3 and return normalized results.

    The raw provider response is never exposed — only the provider-neutral
    DiscoveredProduct shape. Typed client failures map to honest statuses;
    an upstream outage is an error, never an empty 200.
    """
    try:
        return search_discovery(query=q, limit=limit, page_token=page_token)
    except ValueError as exc:
        # Blank-after-trim query, or a page_token the selected provider
        # does not support (FastAPI already rejects missing q / bad limit).
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DiscoveryConfigError as exc:
        # Server-side misconfiguration (missing key / bad provider name).
        logger.error("Discovery not configured: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except DiscoveryQuotaError as exc:
        logger.error("Discovery quota exhausted: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except DiscoveryRateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except DiscoveryUnavailableError as exc:
        logger.error("Discovery upstream unavailable: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (
        DiscoveryAuthError,
        DiscoveryRequestError,
        DiscoveryInvalidResponseError,
    ) as exc:
        # Upstream rejected the call or answered with an unusable body.
        logger.error("Discovery upstream failure: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Discovery comparison (Phase 10E — read-only research).
#
# Flow: query -> requirements -> live discovery -> deterministic filtering ->
# deterministic candidate cap -> AI ranking -> AI output validation -> re-join
# to backend-owned discovery records.
#
# The AI contributes ONLY an ordering of ids it was handed plus a reason
# string. Every product fact served here comes from the discovery layer. The
# request schema has no field for a product record, a price, a PayPal amount,
# or an order/capture/refund id (extra keys are a 422), and this endpoint
# makes no PayPal call, no Policy Engine call, no ledger write, and no catalog
# mutation. Discovered products remain non-purchasable.
# ---------------------------------------------------------------------------


@app.post("/discovery/compare", response_model=ComparisonResponse)
def discovery_compare(req: ComparisonRequest) -> ComparisonResponse:
    """Rank and compare externally discovered products for a query.

    Returns the top `top_k` candidates with a reason each. `ranking_method`
    says whether a validated AI ranking or a deterministic fallback ordering
    produced the result — a fallback is never labeled as AI-ranked.
    """
    try:
        return compare_discovery(
            query=req.query,
            requirements=req.requirements,
            limit=req.limit,
            top_k=req.top_k,
            allow_deterministic_fallback=req.allow_deterministic_fallback,
        )
    except ValueError as exc:
        # Blank-after-trim query, or a bad argument rejected before any
        # upstream call (FastAPI already rejects out-of-range limit/top_k).
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DiscoveryConfigError as exc:
        logger.error("Discovery not configured: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except DiscoveryQuotaError as exc:
        logger.error("Discovery quota exhausted: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except DiscoveryRateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except DiscoveryUnavailableError as exc:
        logger.error("Discovery upstream unavailable: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (
        DiscoveryAuthError,
        DiscoveryRequestError,
        DiscoveryInvalidResponseError,
    ) as exc:
        logger.error("Discovery upstream failure: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except AIConfigError as exc:
        logger.error("AI provider not configured: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AIServiceError as exc:
        logger.error("AI provider service error: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (
        InvalidAIResponseError,
        IntentValidationError,
        RankingValidationError,
    ) as exc:
        # The provider replied, but not with usable/valid structured output
        # (includes a ranking naming a non-candidate or duplicated id). No
        # ranking is fabricated to cover for it.
        logger.error("Invalid AI output during comparison: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# External-product adoption (Phase 11A — the adoption boundary).
#
# Converts a discovery result the SERVER produced into a PayPilot-owned
# record. The request carries an identifier and nothing else: AdoptionRequest
# has only `source` and `external_id`, and extra="forbid" makes any attempt
# to send a title, price, currency, merchant, URL, PayPal amount, or
# order/capture id a 422. Every product fact in the response is read from the
# server's own stored discovery evidence.
#
# ADOPTION IS NOT PURCHASE. This endpoint makes no PayPal call, no Policy
# Engine call, no purchase-execution call, no capture, no refund, no
# transaction-ledger write, and no AI call. An adopted product is NOT
# purchasable: a status of ACTIVE means the adoption succeeded, and the
# observed price it carries is discovery evidence that a future purchase
# phase must re-validate — never an amount to charge.
# ---------------------------------------------------------------------------


@app.post(
    "/discovery/adopt",
    response_model=AdoptedProduct,
    status_code=201,
    responses={
        200: {"description": "This product was already adopted; existing record."},
        201: {"description": "A new adopted product record was created."},
    },
)
def discovery_adopt(req: AdoptionRequest, response: Response) -> AdoptedProduct:
    """Adopt a previously discovered product by its provider identity.

    Returns 201 with a new record, or 200 with the existing one when this
    provider identity was already adopted — adoption is idempotent and never
    creates duplicates or overwrites an earlier snapshot.

    404 when the server holds no discovery evidence for that identity: the
    client's own product details are never accepted as a substitute.
    """
    try:
        record, created = adopt_discovered_product(
            source=req.source, external_id=req.external_id
        )
    except DiscoveryEvidenceNotFoundError as exc:
        # The server never served this product, so it has nothing it can
        # honestly adopt. Refusing is the whole security model.
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MalformedDiscoveryEvidenceError as exc:
        # Our own stored evidence is unusable: a server-side data integrity
        # fault, not a client error. Fail rather than adopt a partial record.
        logger.error("Adoption blocked by malformed stored evidence: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ValueError as exc:
        # Blank-after-trim identifier (the schema already rejects most).
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    response.status_code = 201 if created else 200
    return record


# ---------------------------------------------------------------------------
# Adopted-product revalidation (Phase 11B — deterministic, no payment).
#
# Re-queries the provider a product was ORIGINALLY adopted from and reports
# what it says now. There is NO request body: the provider, the external id
# and the query text all come from the server-side adopted record, so a
# client cannot redirect the lookup or supply a price, currency, merchant,
# URL, title, provider, external id, or any PayPal field.
#
# A result is accepted ONLY when its provider-native external_id matches the
# adopted one exactly. Title/merchant/price are never used for identity, so
# "product A" can never silently become "product B".
#
# REVALIDATION IS NOT A PURCHASE DECISION. This endpoint makes no PayPal
# call, no Policy Engine call, no purchase-execution call, no capture, no
# refund, no ledger write, and no AI call. REVALIDATED means "the provider
# still serves this exact product" — it does NOT mean purchasable, and
# whether a price change is acceptable is Phase 11C's question.
# ---------------------------------------------------------------------------


@app.post(
    "/discovery/adopted/{adopted_product_id}/revalidate",
    response_model=RevalidationResponse,
)
def discovery_revalidate(adopted_product_id: str) -> RevalidationResponse:
    """Re-check one adopted product against its original discovery provider.

    Returns 200 whenever the attempt was made and a snapshot recorded — the
    outcome lives in `snapshot.revalidation_status`, which distinguishes a
    confirmed identity from an unmatched one, a transient provider outage,
    a permanent provider error, and a product that cannot be revalidated
    here at all. A provider outage is not an API error: it is a recorded,
    explicit result.

    404 only when no such adopted product exists.
    """
    try:
        return revalidate_adopted_product(adopted_product_id)
    except AdoptedProductNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# External-product policy evaluation (Phase 11C).
#
# Connects the adoption/revalidation flow to the EXISTING deterministic
# Policy Engine. There is NO request body: the adopted product, the latest
# revalidation, the user's authorization and the shopping constraints all
# come from server-side state, so a client cannot supply a price, a currency,
# a merchant, a product identity, a spend limit, or a decision.
#
# TWO DISTINCT ANSWERS. `eligibility` says whether fresh, trusted evidence
# existed to evaluate at all; `policy_decision` is the engine's own ruling,
# returned verbatim with its existing vocabulary. An upstream revalidation
# failure yields NOT_ELIGIBLE with policy_decision=null — never a fabricated
# policy DENY, because the engine never ruled on it.
#
# NO SUCCESSFUL REVALIDATION -> NO PURCHASE. Only the LATEST revalidation
# being REVALIDATED admits a product, and only its fresh normalized price is
# evaluated; the adopted snapshot's price is never a fallback.
#
# THIS CREATES NO PAYMENT. No PayPal call, no order, no capture, no refund,
# no ledger write, no AI call. ALLOW_AUTONOMOUS_PURCHASE states what the
# rules would permit if a later phase builds an execution path.
# ---------------------------------------------------------------------------


@app.post(
    "/discovery/adopted/{adopted_product_id}/policy-evaluate",
    response_model=ExternalPolicyEvaluation,
)
def discovery_adopted_policy_evaluate(
    adopted_product_id: str,
) -> ExternalPolicyEvaluation:
    """Evaluate one adopted external product against the Policy Engine.

    Returns 200 whenever a verdict was reached — including NOT_ELIGIBLE,
    which is a real answer ("no current evidence"), not an error.

    404 only when no such adopted product exists.
    """
    try:
        return evaluate_adopted_product_policy(adopted_product_id)
    except AdoptedProductNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# External-product EXPLICIT APPROVAL GATE (Phase 11D).
#
# Turns a policy decision plus FRESH revalidated evidence plus an explicit
# human decision into an approved external purchase INTENT. An intent only:
# these two endpoints create no PayPal order, capture nothing, refund
# nothing, write no transaction, touch no session and call no AI. APPROVED
# is the last state that exists in this phase.
#
# AN EXTERNAL PRODUCT ALWAYS NEEDS A HUMAN. The existing Policy Engine stays
# the sole policy authority and its semantics are unchanged, but for
# externally discovered products its most permissive ruling is deliberately
# not sufficient on its own:
#
#   ALLOW_AUTONOMOUS_PURCHASE -> still requires explicit user approval
#   REQUIRE_USER_APPROVAL     -> still requires explicit user approval
#   DENY                      -> no approval record can be created at all
#
# NEITHER ENDPOINT ACCEPTS A PRODUCT FACT. Creation has no request body at
# all; resolution accepts exactly one boolean and rejects any extra key with
# a 422. There is nowhere for a client to put a price, a currency, a
# merchant, a URL, a revalidation id, another adopted product id, a policy
# decision, or a PayPal field.
#
# FRESHNESS IS ENFORCED SERVER-SIDE from the revalidation snapshot's own
# checked_at, against EXTERNAL_REVALIDATION_TTL_SECONDS (default 300). The
# same bound sets the approval's expires_at, so an approval can never
# outlive the evidence it commits to.
# ---------------------------------------------------------------------------


@app.post(
    "/discovery/adopted/{adopted_product_id}/approval",
    response_model=ExternalApprovalRequestResult,
)
def discovery_adopted_request_approval(
    adopted_product_id: str,
) -> ExternalApprovalRequestResult:
    """Prepare a PENDING approval for one adopted external product.

    Returns 200 in every case where the product exists. `approvable` says
    whether an approval record was created: false with a `blocked_reason` is
    a real answer about the product (no fresh confirmed evidence, stale
    evidence, or a policy DENY), not a malformed request, so it is not an
    HTTP error. No record is created when blocked.

    Preparing an approval twice for the same bound state is idempotent:
    `created` is false and the existing PENDING approval is returned, so the
    decision is never forked across two records.

    404 only when no such adopted product exists.
    """
    try:
        return request_external_approval(adopted_product_id)
    except AdoptedProductNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ApprovalConfigError as exc:
        # A deployment fault (bad EXTERNAL_REVALIDATION_TTL_SECONDS), not a
        # client error. Failing closed is the safe direction.
        logger.error("External approval blocked by configuration: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/discovery/approvals/{approval_id}/resolve",
    response_model=ExternalApproval,
    responses={
        409: {
            "description": "The approval is in a terminal state, has "
            "expired, or a binding it committed to no longer holds. Nothing "
            "changed and nothing executed; the structured detail names the "
            "conflict and carries the approval's current state."
        }
    },
)
def discovery_resolve_approval(
    approval_id: str, req: ExternalApprovalResolveRequest
) -> ExternalApproval:
    """Record the user's explicit decision on one pending approval.

    The request body is one boolean, and it is REQUIRED: merely calling this
    endpoint is not an approval.

    approve=true re-verifies EVERY binding against current server state --
    the adopted product, that exact revalidation snapshot, its freshness,
    the normalized price, the currency and the Policy Engine's current
    ruling -- and only then marks the approval APPROVED. Nothing downstream
    runs, because nothing downstream exists: no PayPal order, no capture, no
    refund, no transaction, no session.

    approve=false marks it REJECTED, with equally no side effects.

    404 when no such approval exists. 409 for a terminal status, an expired
    approval, or a binding that moved -- never a silent re-bind, because the
    user approved the state they were shown and has not seen the new one.
    """
    try:
        return resolve_external_approval(approval_id, req.approve)
    except ApprovalNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ApprovalConflictError as exc:
        # Structured on purpose: the client must be able to tell WHICH
        # binding moved without parsing prose, and must be shown the current
        # state so a human can decide whether to request a new approval.
        raise HTTPException(
            status_code=409,
            detail={
                "code": exc.code.value,
                "message": exc.message,
                "approval": jsonable_encoder(exc.approval),
            },
        ) from exc
    except ApprovalConfigError as exc:
        logger.error("External approval blocked by configuration: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# External-product PAYPAL ORDER CREATION (Phase 11E).
#
# The first and only path from an APPROVED external product to a real
# PayPal order. It creates the order and STOPS: capture_order is never
# called, so the order sits awaiting the BUYER's own approval on PayPal's
# hosted checkout and no money moves. No refund path exists here, and the
# catalog transaction ledger is neither read nor written.
#
# THE CLIENT NEVER SUPPLIES MONEY. This route declares NO request body
# parameter, exactly like the approval-creation route above, so a price, an
# amount, a currency, a merchant, a product URL, a title, a product id, a
# revalidation id, a policy decision, a PayPal order id or a capture id in
# the request is never parsed and has no variable to bind to. The amount,
# the currency, the description, the return URL and the cancel URL are all
# server-derived.
#
# THE AMOUNT IS THE APPROVED PROVIDER-OBSERVED PRICE, re-read from the exact
# append-only revalidation snapshot the approval is bound to, after every
# Phase 11D binding is re-verified from scratch. Phase 10F's normalized INR
# figure is the POLICY price and is not converted back into a charge; a
# currency outside the integration's explicit allowlist fails closed rather
# than being converted at an invented rate.
#
# AT MOST ONE ORDER PER APPROVAL. approval_id is UNIQUE in
# external_purchase_orders and the row is committed as a CREATING
# reservation before PayPal is contacted, so a concurrent second request
# refuses instead of creating a second order, and a later one replays the
# existing order id without calling PayPal again.
# ---------------------------------------------------------------------------


@app.post(
    "/discovery/approvals/{approval_id}/create-order",
    response_model=ExternalOrderResult,
    responses={
        409: {
            "description": "No order was created: the approval is not "
            "APPROVED, it expired, a binding it committed to moved, the "
            "product's identity or lifecycle no longer admits a purchase, "
            "the approved amount is not payable exactly in a supported "
            "currency, or another execution already holds this approval's "
            "single slot. The structured detail names the conflict. Also "
            "APPROVAL_URL_UNAVAILABLE (Phase 11F-B): the order EXISTS but "
            "no checkout link is stored and none could be read back from "
            "PayPal — nothing is fabricated and no second order is created."
        }
    },
)
def discovery_create_external_order(approval_id: str) -> ExternalOrderResult:
    """Create a PayPal order for one APPROVED external product.

    Takes ONLY the approval id from the URL; there is no request body, so a
    client can never supply an amount, a currency, a product, a merchant, a
    URL or a PayPal identifier. Everything is re-read from server-side
    state and every Phase 11D binding is re-verified before PayPal is
    contacted.

    The returned order is CREATED, never captured: the buyer still has to
    approve it at `approve_url`. `captured` is always false.

    Calling this twice is idempotent — the second call returns the same
    PayPal order with `created` false and makes no PayPal call.

    404 when no such approval exists. 409 for every deterministic refusal.
    502 when PayPal itself fails: the execution record is marked FAILED and
    never retried, so a transient failure can never become a false success.
    """
    try:
        return create_external_order(approval_id)
    except ApprovalNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ExternalOrderConflictError as exc:
        # Structured on purpose: the client must be able to tell WHICH
        # condition blocked the order without parsing prose, and nothing was
        # created, charged or repaired.
        raise HTTPException(
            status_code=409,
            detail={
                "code": exc.code.value,
                "message": exc.message,
                "execution": jsonable_encoder(exc.execution)
                if exc.execution
                else None,
            },
        ) from exc
    except ApprovalConfigError as exc:
        logger.error("External order blocked by configuration: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except PayPalCheckoutConfigError as exc:
        # Deployment fault: PAYPAL_RETURN_URL / PAYPAL_CANCEL_URL is not an
        # absolute https URL. Not an upstream PayPal failure, so not a 502 —
        # and no order was created, because the check runs first.
        logger.error("Checkout URL configuration is invalid: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except PayPalAuthError as exc:
        logger.error("Auth failed while creating external order: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except PayPalOrderError as exc:
        logger.error("Create-order failed for external product: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


#: Upstream could not be read, so approval is UNKNOWN — reported as a 502
#: because the failure is PayPal's availability, not the request's state.
#: Every other verification failure is a 409: a real, observed condition
#: that conflicts with "the buyer approved this order at this amount".
_VERIFICATION_FAILURE_STATUS = {
    ExternalApprovalVerificationFailure.ORDER_UNREADABLE: 502,
}


@app.post(
    "/discovery/approvals/{approval_id}/verify-approval",
    response_model=ExternalApprovalVerification,
    responses={
        404: {
            "description": "No execution exists for this approval — nothing "
            "has been created for it, so there is no order to verify."
        },
        409: {
            "description": "Buyer approval was NOT verified. The structured "
            "detail names the condition (EXECUTION_NOT_VERIFIABLE, "
            "ORDER_ID_MISMATCH, ORDER_NOT_APPROVED, PURCHASE_UNIT_MISSING, "
            "PURCHASE_UNIT_AMBIGUOUS, AMOUNT_MALFORMED, AMOUNT_MISMATCH, "
            "CURRENCY_MISMATCH, RECORDED_ORDER_INCOMPLETE) and carries what "
            "PayPal actually reported. The execution keeps its status; "
            "nothing was captured, refunded, created or repaired."
        },
        502: {
            "description": "ORDER_UNREADABLE: PayPal could not be read "
            "(auth, missing order, upstream error, timeout, network), so "
            "whether the buyer approved is unknown. Unknown is not approved."
        },
    },
)
def discovery_verify_buyer_approval(approval_id: str) -> ExternalApprovalVerification:
    """Verify SERVER-SIDE that the buyer approved this approval's order.

    Takes ONLY the approval id from the URL. This route declares no request
    body and no query parameter, so a PayPal order id, a `token`, a
    `PayerID`, an amount, a currency, a product id, a merchant, a URL, a
    policy decision or a capture instruction sent by a client is never
    parsed and has no variable to bind to. In particular, the `token` and
    `PayerID` a browser carries back from PayPal are NOT proof of anything
    and cannot override the stored order id: the order asked about is the
    one this server recorded creating.

    The answer is PayPal's own. The server reads the order back with the
    existing read-only get_order and requires, all of them: PayPal answers
    about the stored order id, its status is exactly APPROVED, it carries
    exactly one purchase unit, and that unit's currency and amount equal the
    amount this server recorded sending. Anything else — including a
    different sum, two purchase units, a VOIDED or CREATED order, or PayPal
    being unreachable — is a deterministic refusal that leaves the execution
    exactly as it was.

    THIS MOVES NO MONEY. One read of PayPal and one guarded status write.
    No capture, no refund, no catalog ledger row, no PayPal order created or
    replayed. BUYER_APPROVED is not a payment and unlocks nothing: the
    Phase 11F-B0 ownership gate still refuses the order on every capture
    route, because external capture does not exist.

    Idempotent: an execution that is already BUYER_APPROVED is returned from
    storage with `transitioned` false, `verification_source` "stored" and no
    PayPal call. Concurrent requests are arbitrated by a compare-and-set, so
    exactly one can report `transitioned` true and no state can regress.

    NOT AUTHENTICATED. This endpoint establishes no caller identity, and no
    part of this system does yet. It asserts nothing about WHO asked — only
    what PayPal reports about the order.
    """
    try:
        return verify_external_buyer_approval(approval_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ExternalExecutionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ExternalApprovalVerificationError as exc:
        # Structured on purpose: the client must be able to tell WHICH check
        # refused without parsing prose, and must see what PayPal actually
        # reported rather than a claim of approval.
        raise HTTPException(
            status_code=_VERIFICATION_FAILURE_STATUS.get(exc.code, 409),
            detail={
                "code": exc.code.value,
                "message": exc.message,
                "verified": False,
                "captured": False,
                "execution": jsonable_encoder(exc.execution)
                if exc.execution
                else None,
                "observed": exc.observed,
            },
        ) from exc


#: Capture refusals that are an UPSTREAM problem rather than a state
#: conflict get a 502; everything else is a deterministic 409. Both carry
#: the same structured detail, including whether PayPal's capture endpoint
#: was actually called.
_CAPTURE_FAILURE_STATUS = {
    ExternalCaptureFailure.ORDER_UNREADABLE: 502,
    ExternalCaptureFailure.CAPTURE_UNCONFIRMED: 502,
}


def _capture_refusal(exc: ExternalCaptureRefusedError) -> HTTPException:
    """Map a capture refusal to a structured, truthful HTTP error."""
    return HTTPException(
        status_code=_CAPTURE_FAILURE_STATUS.get(exc.code, 409),
        detail={
            "code": exc.code.value,
            "message": exc.message,
            "captured": False,
            # The field a reader needs first: False means PayPal's capture
            # endpoint was never called, so money cannot have moved.
            "paypal_capture_attempted": exc.paypal_called,
            "rolled_back": False,
            "retried": False,
            "execution": jsonable_encoder(exc.execution) if exc.execution else None,
            "observed": exc.observed,
        },
    )


@app.post(
    "/discovery/approvals/{approval_id}/authorize-capture",
    response_model=ExternalCaptureAuthorization,
    responses={
        404: {"description": "No execution exists for this approval."},
        409: {
            "description": "CAPTURE_NOT_PERMITTED_IN_STATE: the execution is "
            "not BUYER_APPROVED — its buyer approval has not been verified "
            "server-side, or a capture attempt has already started."
        },
        500: {
            "description": "EXTERNAL_CAPTURE_WINDOW_SECONDS is misconfigured."
        },
    },
)
def discovery_authorize_external_capture(
    approval_id: str,
) -> ExternalCaptureAuthorization:
    """Explicitly authorize capturing this approval's PayPal order (11F-D).

    THE SECOND DELIBERATE ACT. A verified buyer approval is a fact about
    the buyer; it is not this system's decision to collect money. Capture
    refuses with CAPTURE_NOT_AUTHORIZED until this endpoint has been called,
    and refuses again once the window it opens has passed.

    This route declares no request body and no query parameter, so an LLM
    response, a forged field, a PayPal return URL, a `token` or a `PayerID`
    cannot be this act — there is nowhere for any of them to land. The only
    input is the approval id in the path.

    NO PAYPAL CALL IS MADE and nothing is captured: the endpoint writes one
    timestamp, `capture_authorized_at`, under a compare-and-set guarded on
    BUYER_APPROVED. Calling it again while the execution is still
    BUYER_APPROVED refreshes the window, because each call is a fresh
    explicit decision; once a capture attempt has started the guard refuses.

    THIS IS A WORKFLOW SAFEGUARD, NOT AUTHENTICATION. This API establishes
    no caller identity, so the stored authorization records that the step
    was taken — never who took it, and it proves nothing about who asked.
    """
    try:
        return authorize_external_capture(approval_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ExternalExecutionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ExternalCaptureConfigError as exc:
        logger.error("Capture window misconfigured: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ExternalCaptureRefusedError as exc:
        raise _capture_refusal(exc) from exc


@app.post(
    "/discovery/approvals/{approval_id}/capture",
    response_model=ExternalCaptureResult,
    responses={
        202: {
            "description": "PENDING: PayPal accepted the capture but has not "
            "completed it. Funds are NOT collected, `captured` is false, and "
            "this phase does not poll for settlement."
        },
        404: {"description": "No execution exists for this approval."},
        409: {
            "description": "The capture was refused. The structured detail "
            "names the condition and states whether PayPal's capture "
            "endpoint was called at all."
        },
        502: {
            "description": "ORDER_UNREADABLE (the pre-capture re-read failed, "
            "no capture attempted) or CAPTURE_UNCONFIRMED (a capture call "
            "was made and its outcome is UNKNOWN). Nothing is rolled back "
            "and nothing is retried automatically."
        },
    },
)
def discovery_capture_external_order(approval_id: str) -> ExternalCaptureResult:
    """Capture one verified, explicitly authorized external PayPal order.

    THE ONLY PATH IN THIS SYSTEM THAT MOVES MONEY FOR AN EXTERNAL PRODUCT.
    It takes ONLY the approval id from the URL: this route declares no
    request body and no query parameter, so an amount, a currency, a PayPal
    order id, a capture id, a product id, a merchant, a URL, a `token`, a
    `PayerID` or an AI-generated instruction is never parsed and has no
    variable to bind to. The order captured is the one stored in
    `external_purchase_orders`; a client-supplied identifier is never the
    operation's target.

    Three independent things must all be true before PayPal is called: the
    buyer's approval has been VERIFIED server-side against PayPal's own
    answer (BUYER_APPROVED); a PayPilot operator has EXPLICITLY authorized
    capture; and that authorization is still inside its window. Then the
    order is re-read once more and must still be that exact order, still
    APPROVED, with exactly one purchase unit at exactly the recorded amount
    and currency. Only then does a compare-and-set claim the capture slot
    and PayPal's capture endpoint get called — once.

    The outcome is read strictly. 200 with `captured: true` requires a
    nested capture status of exactly COMPLETED, a capture id, and an amount
    and currency equal to the recorded ones. PENDING returns 202 with
    `captured: false`. DECLINED and every unknown or mismatched outcome are
    refusals, and the execution records what PayPal actually reported —
    nothing is rolled back, and nothing is ever retried automatically.

    Idempotent for a CAPTURED execution: the stored result is returned with
    `replay: true` and no PayPal call. Concurrent requests are arbitrated by
    the compare-and-set, so only one can reach PayPal; the loser is refused
    with CAPTURE_IN_PROGRESS.

    NOTHING TOUCHES THE CATALOG. No catalog transaction is created, read or
    modified, the catalog capture service is not called, and the Phase
    11F-B0 ownership gate still refuses this order on every catalog capture
    route. `refundable` is always false: external refunds do not exist, and
    the catalog refund route refuses an external capture id.

    NOT AUTHENTICATED. The stored capture authorization is a workflow
    safeguard, not proof of caller identity.
    """
    try:
        result = capture_external_order(approval_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ExternalExecutionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ExternalCaptureConfigError as exc:
        logger.error("Capture window misconfigured: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ExternalCaptureRefusedError as exc:
        raise _capture_refusal(exc) from exc

    if result.outcome == "PENDING":
        # 202 Accepted, not 200: PayPal took the capture but has not
        # completed it, so a client that only reads the status code still
        # cannot mistake it for collected funds.
        return JSONResponse(
            status_code=202, content=jsonable_encoder(result)
        )
    return result


@app.get(
    "/discovery/approvals/{approval_id}/order",
    response_model=ExternalOrderView,
    responses={
        404: {
            "description": "No execution record exists for this approval — "
            "either the approval is unknown, or no create-order has been "
            "attempted for it. Nothing is created by asking."
        }
    },
)
def discovery_get_external_order(approval_id: str) -> ExternalOrderView:
    """Read one approval's external execution record (Phase 11F-B).

    STRICTLY READ-ONLY. This GET creates nothing, mutates nothing and
    contacts PayPal for nothing: it answers from the stored
    `external_purchase_orders` row alone. In particular it never creates a
    PayPal order, never captures, never refunds, never writes the catalog
    ledger, and never advances an execution's status — so it cannot be used
    as an indirect way to trigger any of those.

    It returns the execution's status, its PayPal order id when one exists,
    the exact amount and currency this server recorded sending to PayPal,
    and the stored hosted-checkout URL when one is recorded. `approve_url`
    is null for a CREATING or FAILED execution (no order exists) and for an
    ORDER_CREATED row stored before the URL column existed; recovering a
    missing link requires both a PayPal call and a database write, so it
    happens on POST .../create-order instead, never here.

    NOT AUTHENTICATED. This endpoint does not establish caller identity, and
    no part of this system does yet — anyone who can reach the API and knows
    an approval id can read its execution record. What that discloses is
    bounded deliberately: the record holds no PayPal credential, no access
    token, no authorization header and no raw provider payload, and visiting
    the checkout URL captures nothing (the buyer still approves at PayPal).

    404 when no execution exists for the approval.
    """
    try:
        execution = get_external_order_by_approval(approval_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if execution is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No external purchase execution exists for approval "
                f"{approval_id}"
            ),
        )

    return ExternalOrderView(
        execution=execution,
        approve_url=execution.paypal_approve_url,
        captured=False,
    )


# ---------------------------------------------------------------------------
# Operator recovery & unresolved-payment monitoring (Phase 11F-I).
#
# Two AUTHENTICATED, operator-only endpoints behind the global
# require_operator gate: a read-only triage list of unresolved external
# executions, and a read-first manual reconciliation of ONE execution.
#
# Neither moves money. Reconciliation delegates to the existing
# external_purchase_service.reconcile_capture, which re-reads PayPal by the
# SERVER-STORED order id and promotes CAPTURING/CAPTURE_PENDING to CAPTURED
# only on conclusive, independently verified evidence, via a compare-and-set.
# capture_order, refund_capture, create-order and authorize-capture are never
# called. A client-supplied order/capture id, amount, currency or status is
# never trusted: the only input is the approval id in the URL, resolved
# against this server's own storage. Catalog transactions are a separate
# ledger and are untouchable here.
# ---------------------------------------------------------------------------


@app.get(
    "/discovery/external-orders/unresolved",
    response_model=UnresolvedExecutionsResponse,
    responses={
        422: {"description": "An unsupported status filter or out-of-range limit."},
    },
)
def discovery_list_unresolved_external_orders(
    status: list[ExternalOrderStatus] | None = Query(
        default=None,
        description="Restrict to these statuses. The listable set is the "
        "unresolved capture states (CAPTURING, CAPTURE_PENDING, "
        "CAPTURE_FAILED) plus CREATING (Phase 11F-K.17); anything else is "
        "422. Omitted = all of them. CREATING is additionally filtered to "
        "rows older than the staleness grace period, so an in-flight "
        "create-order call is never shown as stuck.",
    ),
    limit: int = Query(default=50, ge=1, le=100),
) -> UnresolvedExecutionsResponse:
    """List external executions needing operator attention (read-only).

    Operator-only (global auth gate). Returns a bounded, status-filtered,
    stably-ordered triage projection of external_purchase_orders rows. It
    exposes no way to mutate a status, returns no arbitrary rows, and makes
    no PayPal call. Catalog transactions are never included.

    CREATING rows are included only when they are STALE (older than the grace
    period), so a healthy, momentarily-creating reservation is not flagged.
    Each row carries has_paypal_order_id so an operator can tell a recoverable
    orphan (an id is persisted) from an ambiguous one (none is).
    """
    listable = tuple(UNRESOLVED_STATUSES) + (ExternalOrderStatus.CREATING,)
    if status:
        invalid = [s.value for s in status if s not in listable]
        if invalid:
            allowed = ", ".join(sorted(s.value for s in listable))
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Unsupported status filter(s): {', '.join(invalid)}. "
                    f"Only these statuses are listable here ({allowed})."
                ),
            )
        requested = list(status)
    else:
        requested = list(listable)

    # CREATING is the only status with a staleness gate; the capture states
    # are surfaced as soon as they occur. Query them separately, then merge
    # into one stably-ordered, bounded list.
    capture_statuses = [s for s in requested if s is not ExternalOrderStatus.CREATING]
    records = []
    if capture_statuses:
        records.extend(list_external_executions_by_statuses(capture_statuses, limit))
    if ExternalOrderStatus.CREATING in requested:
        records.extend(list_stale_creating_external_orders(limit))

    # Oldest-first across the merged set, deterministic tiebreak, bounded.
    records.sort(key=lambda r: (r.created_at, r.execution_id))
    records = records[:limit]

    return UnresolvedExecutionsResponse(
        executions=[ExternalExecutionTriage.from_record(r) for r in records],
        count=len(records),
    )


#: reconcile_capture outcome -> (report outcome, reconciled, review, retryable, message)
_RECONCILE_OUTCOMES = {
    "captured": (
        "reconciled_to_captured",
        True,
        False,
        False,
        "PayPal independently confirmed one matching completed capture; the "
        "execution was advanced to CAPTURED.",
    ),
    "already_applied": (
        "already_captured",
        False,
        False,
        False,
        "The execution is already CAPTURED; no PayPal call was made.",
    ),
    "unresolved_no_capture": (
        "unresolved_no_capture",
        False,
        False,
        True,
        "PayPal shows no capture yet. The execution is left unresolved; its "
        "reservation is not released and it is not made recapturable.",
    ),
    "inconclusive": (
        "inconclusive",
        False,
        False,
        True,
        "The evidence was inconclusive (PayPal unreadable or the record "
        "incomplete). The state is preserved; retry later.",
    ),
    "review_required": (
        "review_required",
        False,
        True,
        False,
        "A definitive-looking discrepancy was found (e.g. multiple captures, "
        "a mismatch, or a changed identifier). The state is preserved for "
        "manual review.",
    ),
}


#: reconcile_creating_order outcome -> (report outcome, reconciled, review,
#: retryable, message). Phase 11F-K.17. `reconciled` is always False here: this
#: path settles a stuck CREATING reservation to the non-money ORDER_CREATED
#: state, it never advances to CAPTURED, so it never asserts a collection.
_CREATING_RECONCILE_OUTCOMES = {
    "recovered_order_created": (
        "recovered_order_created",
        False,
        False,
        False,
        "PayPal independently confirmed the server's own order exists and is "
        "still pre-capture at the expected amount and currency; the stuck "
        "CREATING reservation was settled to ORDER_CREATED. No order was "
        "created and nothing was captured, approved or refunded.",
    ),
    "already_applied": (
        "already_recovered",
        False,
        False,
        False,
        "The execution is no longer CREATING (already settled); no PayPal "
        "call was made and no state was changed.",
    ),
    "manual_review_required": (
        "manual_review_required",
        False,
        True,
        False,
        "The reservation is CREATING with no persisted PayPal order id, so "
        "the create-order outcome is ambiguous (e.g. a timeout). The order id "
        "is never guessed and the request is never retried; a human must "
        "determine whether a PayPal order exists.",
    ),
    "inconclusive": (
        "inconclusive",
        False,
        False,
        True,
        "The evidence was inconclusive (PayPal unreadable, or the bound "
        "revalidation evidence is missing). The CREATING state is preserved; "
        "retry later.",
    ),
    "review_required": (
        "review_required",
        False,
        True,
        False,
        "A definitive-looking discrepancy was found (a different order id, a "
        "non-pre-capture status such as COMPLETED, or an amount/currency "
        "mismatch). The CREATING state is preserved for manual review.",
    ),
}


@app.post(
    "/discovery/approvals/{approval_id}/reconcile",
    response_model=ExternalReconcileReport,
    responses={
        404: {"description": "No external execution exists for this approval."},
    },
)
def discovery_reconcile_external_order(approval_id: str) -> ExternalReconcileReport:
    """Operator-only, read-first reconciliation of ONE external execution.

    Resolves the execution from the SERVER-STORED approval id, then delegates
    to external_purchase_service.reconcile_capture, which re-reads PayPal by
    the stored order id and, ONLY on conclusive independently verified
    evidence, advances CAPTURING/CAPTURE_PENDING to CAPTURED via the existing
    compare-and-set. It never calls capture, refund, create-order or
    capture-authorization, and never trusts a client-supplied identifier,
    amount, currency or status.

    CAPTURE_FAILED is never auto-promoted: it returns a
    manual_review_required outcome (no PayPal call). CAPTURED replays
    idempotently with no PayPal call. Any other state is not_applicable to
    capture recovery. Unknown approval id -> 404.

    A CREATING execution (Phase 11F-K.17) is instead routed to the read-first
    CREATING recovery: with a persisted order id it may be settled to
    ORDER_CREATED on conclusive, independently verified evidence; without one
    it is manual_review_required. That path moves no money and never advances
    buyer/capture state either.
    """
    try:
        execution = get_external_order_by_approval(approval_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if execution is None:
        raise HTTPException(
            status_code=404,
            detail=f"No external purchase execution exists for approval {approval_id}",
        )

    # A stuck CREATING reservation has its own read-first recovery; every
    # other status stays on the existing capture-reconciliation path.
    if execution.status is ExternalOrderStatus.CREATING:
        outcome, updated, detail = reconcile_external_creating_order(execution)
        report_outcome, reconciled, review, retryable, message = (
            _CREATING_RECONCILE_OUTCOMES.get(
                outcome,
                (
                    outcome,
                    False,
                    True,
                    False,
                    "Unrecognized reconciliation outcome; left for manual review.",
                ),
            )
        )
        return ExternalReconcileReport(
            execution=ExternalExecutionTriage.from_record(updated),
            approval_id=approval_id,
            outcome=report_outcome,
            status=updated.status.value,
            reconciled=reconciled,
            review_required=review,
            retryable=retryable,
            captured=updated.status is ExternalOrderStatus.CAPTURED,
            message=message,
            detail=detail or None,
        )

    outcome, updated, detail = reconcile_external_capture(execution)

    if outcome in _RECONCILE_OUTCOMES:
        report_outcome, reconciled, review, retryable, message = _RECONCILE_OUTCOMES[
            outcome
        ]
    elif outcome == "not_applicable":
        if updated.status is ExternalOrderStatus.CAPTURE_FAILED:
            report_outcome, reconciled, review, retryable = (
                "manual_review_required",
                False,
                True,
                False,
            )
            message = (
                "The execution is CAPTURE_FAILED. A successful order lookup "
                "alone does not prove funds were collected, and the stored "
                "row cannot distinguish an ambiguous timeout from a "
                "definitive failure, so it is left for manual review and is "
                "never auto-promoted or retried."
            )
        else:
            report_outcome, reconciled, review, retryable = (
                "not_applicable",
                False,
                False,
                False,
            )
            message = (
                f"The execution is {updated.status.value}, which capture "
                f"recovery does not act on. No state was changed."
            )
    else:  # pragma: no cover - reconcile_capture returns only the above
        report_outcome, reconciled, review, retryable, message = (
            outcome,
            False,
            True,
            False,
            "Unrecognized reconciliation outcome; left for manual review.",
        )

    return ExternalReconcileReport(
        execution=ExternalExecutionTriage.from_record(updated),
        approval_id=approval_id,
        outcome=report_outcome,
        status=updated.status.value,
        reconciled=reconciled,
        review_required=review,
        retryable=retryable,
        captured=updated.status is ExternalOrderStatus.CAPTURED,
        message=message,
        detail=detail or None,
    )


# ---------------------------------------------------------------------------
# Catalog orphan visibility & read-first recovery (Phase 11F-K.21).
#
# Two operator-only endpoints behind the global require_operator gate: a
# read-only triage list of STALE CREATING catalog attempts, and a read-first
# reconciliation of ONE attempt. Neither moves money: reconciliation delegates
# to purchase_execution_service.reconcile_creating_attempt, which re-reads
# PayPal by the SERVER-STORED order id and, only on conclusive evidence,
# settles CREATING -> FINALIZED (creating the ledger row in its normal
# ORDER_CREATED state) via the idempotent K.20 linking. create_order,
# capture_order, authorize-capture and refund are never called; a captured /
# mismatched / unreadable order is left untouched for review.
# ---------------------------------------------------------------------------


#: reconcile_creating_attempt outcome -> (report outcome, reconciled, review,
#: retryable, message). Phase 11F-K.21.
_CATALOG_ATTEMPT_OUTCOMES = {
    "recovered_finalized": (
        "recovered_finalized",
        True,
        False,
        False,
        "PayPal independently confirmed the server's own order exists and is "
        "still pre-capture at the recorded amount and currency; the stuck "
        "CREATING attempt was settled to FINALIZED and linked to its ledger "
        "row. No order was created and nothing was captured, approved or "
        "refunded.",
    ),
    "already_finalized": (
        "already_finalized",
        False,
        False,
        False,
        "The attempt is already FINALIZED; no PayPal call was made and no "
        "state was changed.",
    ),
    "not_applicable": (
        "not_applicable",
        False,
        False,
        False,
        "The attempt is FAILED (definitive no-order); catalog recovery does "
        "not act on it. No state was changed.",
    ),
    "manual_review_required": (
        "manual_review_required",
        False,
        True,
        False,
        "The attempt is CREATING with no persisted PayPal order id, so the "
        "create-order outcome is ambiguous (e.g. a timeout). The order id is "
        "never guessed and create_order is never retried; a human must "
        "determine whether a PayPal order exists.",
    ),
    "inconclusive": (
        "inconclusive",
        False,
        False,
        True,
        "The evidence was inconclusive (PayPal unreadable). The CREATING "
        "state is preserved; retry later.",
    ),
    "review_required": (
        "review_required",
        False,
        True,
        False,
        "A definitive-looking discrepancy was found (a different order id, a "
        "non-pre-capture status such as COMPLETED, an amount/currency "
        "mismatch, a removed product, or a finalize conflict). The CREATING "
        "state is preserved for manual review.",
    ),
}


@app.get(
    "/purchase/attempts/unresolved",
    response_model=UnresolvedCatalogAttemptsResponse,
    responses={
        422: {"description": "An out-of-range limit."},
    },
)
def purchase_list_unresolved_attempts(
    limit: int = Query(default=50, ge=1, le=100),
) -> UnresolvedCatalogAttemptsResponse:
    """List STALE catalog purchase attempts needing operator attention (read-only).

    Operator-only (global auth gate). Returns a bounded, oldest-first triage
    projection of CREATING attempts older than the staleness grace period
    (FAILED is terminal and is not an actionable orphan, so it is excluded). A
    recent, in-flight attempt is never flagged. Each row carries
    has_paypal_order_id so an operator can tell a recoverable orphan (an id is
    persisted) from an ambiguous one (none is). It makes no PayPal call, never
    mutates a status, and exposes no credential or checkout URL.
    """
    records = catalog_attempt_service.list_stale_creating(limit)
    return UnresolvedCatalogAttemptsResponse(
        attempts=[CatalogAttemptTriage.from_record(r) for r in records],
        count=len(records),
    )


@app.post(
    "/purchase/attempts/{attempt_id}/reconcile",
    response_model=CatalogAttemptReconcileReport,
    responses={
        404: {"description": "No catalog purchase attempt exists for this id."},
    },
)
def purchase_reconcile_attempt(attempt_id: str) -> CatalogAttemptReconcileReport:
    """Operator-only, read-first reconciliation of ONE catalog attempt.

    Resolves the attempt by its id, then delegates to
    purchase_execution_service.reconcile_creating_attempt, which re-reads PayPal
    by the SERVER-STORED order id and, ONLY on conclusive independently verified
    evidence (the order exists, is still pre-capture, answers about the stored
    id, and matches the recorded amount and currency), settles CREATING ->
    FINALIZED via the idempotent K.20 linking. It never calls create_order,
    capture, authorize-capture or refund, and never trusts a client-supplied
    identifier, amount, currency or status.

    A CREATING attempt with no order id is manual_review_required; a FAILED
    attempt is not_applicable; a FINALIZED attempt replays idempotently with no
    PayPal call. Unknown attempt id -> 404.
    """
    attempt = catalog_attempt_service.get(attempt_id)
    if attempt is None:
        raise HTTPException(
            status_code=404,
            detail=f"No catalog purchase attempt exists for id {attempt_id}",
        )

    outcome, updated, detail = reconcile_catalog_attempt(attempt)
    report_outcome, reconciled, review, retryable, message = (
        _CATALOG_ATTEMPT_OUTCOMES.get(
            outcome,
            (
                outcome,
                False,
                True,
                False,
                "Unrecognized reconciliation outcome; left for manual review.",
            ),
        )
    )
    return CatalogAttemptReconcileReport(
        attempt=CatalogAttemptTriage.from_record(updated),
        attempt_id=attempt_id,
        outcome=report_outcome,
        status=updated.status.value,
        reconciled=reconciled,
        review_required=review,
        retryable=retryable,
        transaction_id=updated.transaction_id,
        message=message,
        detail=detail or None,
    )


# ---------------------------------------------------------------------------
# Operator-gated create-order REPLAY recovery (Phase 11F-K.22b).
#
# The AMBIGUOUS orphan the read-first K.21/K.17 paths cannot settle: a CREATING
# reservation with NO persisted order id, whose create-order outcome was never
# learned. These AUTHENTICATED, operator-only endpoints REPLAY create_order
# with the EXACT persisted PayPal-Request-Id — only while the reservation is
# demonstrably inside a conservative window below PayPal's documented
# idempotency retention, and under a durable single-flight claim — then
# INDEPENDENTLY re-read and verify the order before settling it. A replay is
# NEVER automatic, never triggered by a webhook or the ordinary confirmation
# flow, never generates a new request id, and never marks a reservation FAILED.
# ---------------------------------------------------------------------------


#: The two outcomes the REPLAY recovery adds to the read-first set, shared by
#: both the catalog and external recover endpoints.
_REPLAY_EXTRA_OUTCOMES = {
    "replay_in_progress": (
        "replay_in_progress",
        False,
        False,
        True,
        "A create-order replay for this reservation is already in flight "
        "under a durable single-flight claim, so no second provider call was "
        "made. Retry once the in-flight replay completes.",
    ),
    "unresolved": (
        "unresolved",
        False,
        False,
        True,
        "The create-order replay did not yield a usable, independently "
        "verified order (a transport timeout, a non-2xx, or a malformed or "
        "absent id). The reservation is left CREATING and is NOT marked "
        "FAILED — a replay failure does not prove no order exists — and a "
        "later retry with the same request id is permitted while the "
        "reservation remains inside the conservative recovery window.",
    ),
}

#: recover-order outcome -> report tuple, catalog. The read-first K.21 outcomes
#: plus the two replay outcomes above.
_CATALOG_RECOVER_OUTCOMES = {**_CATALOG_ATTEMPT_OUTCOMES, **_REPLAY_EXTRA_OUTCOMES}


@app.post(
    "/purchase/attempts/{attempt_id}/recover-order",
    response_model=CatalogAttemptReconcileReport,
    responses={
        404: {"description": "No catalog purchase attempt exists for this id."},
    },
)
def purchase_recover_attempt(attempt_id: str) -> CatalogAttemptReconcileReport:
    """Operator-only create-order REPLAY recovery of ONE catalog attempt (K.22b).

    For a CREATING attempt that carries NO PayPal order id — the ambiguous
    orphan whose create-order outcome was never learned — this REPLAYS
    create_order with the EXACT persisted PayPal-Request-Id, but only when the
    attempt is still inside the conservative recovery window and no other
    worker holds the single-flight claim. PayPal's idempotency returns the
    original order if one was created (or creates it now if none was), so at
    most one order ever exists. The returned id is persisted via the guarded
    K.20 claim and then INDEPENDENTLY re-read and verified before the attempt
    is finalized — the replay response is never trusted as proof on its own.

    An attempt that already carries an order id (or is no longer CREATING) is
    routed to the read-first K.21 recovery instead; no replay happens. An
    ineligible attempt (no/invalid/non-original request id, a
    malformed/ambiguous/future timestamp, or an expired window) is
    manual_review_required and is left UNCHANGED with no provider call. This
    route NEVER captures, authorizes, refunds, marks the attempt FAILED,
    overwrites an order id, or generates a new request id. Unknown id -> 404.
    """
    attempt = catalog_attempt_service.get(attempt_id)
    if attempt is None:
        raise HTTPException(
            status_code=404,
            detail=f"No catalog purchase attempt exists for id {attempt_id}",
        )

    outcome, updated, detail = recover_catalog_attempt(attempt)
    report_outcome, reconciled, review, retryable, message = (
        _CATALOG_RECOVER_OUTCOMES.get(
            outcome,
            (
                outcome,
                False,
                True,
                False,
                "Unrecognized recovery outcome; left for manual review.",
            ),
        )
    )
    return CatalogAttemptReconcileReport(
        attempt=CatalogAttemptTriage.from_record(updated),
        attempt_id=attempt_id,
        outcome=report_outcome,
        status=updated.status.value,
        reconciled=reconciled,
        review_required=review,
        retryable=retryable,
        transaction_id=updated.transaction_id,
        message=message,
        detail=detail or None,
    )


#: recover-order outcome -> report tuple, external. The read-first K.17
#: outcomes plus the two replay outcomes.
_CREATING_RECOVER_OUTCOMES = {
    **_CREATING_RECONCILE_OUTCOMES,
    **_REPLAY_EXTRA_OUTCOMES,
}


@app.post(
    "/discovery/approvals/{approval_id}/recover-order",
    response_model=ExternalReconcileReport,
    responses={
        404: {"description": "No external execution exists for this approval."},
    },
)
def discovery_recover_external_order(approval_id: str) -> ExternalReconcileReport:
    """Operator-only create-order REPLAY recovery of ONE external execution (K.22b).

    For a CREATING reservation that carries NO PayPal order id — the ambiguous
    orphan whose create-order outcome was never learned — this REPLAYS
    create_order with the EXACT persisted PayPal-Request-Id, but only when the
    reservation is still inside the conservative recovery window and no other
    worker holds the single-flight claim. PayPal's idempotency returns the
    original order if one was created (or creates it now if none was), so at
    most one order ever exists. The returned id is persisted via the guarded
    K.16 claim and then INDEPENDENTLY re-read and verified before the
    reservation is settled to ORDER_CREATED — the replay response is never
    trusted as proof on its own, and the amount sent for a NEW order is the
    bound snapshot's observed price, never a client value.

    A reservation that already carries an order id (or is no longer CREATING)
    is routed to the read-first K.17 recovery instead; no replay happens. An
    ineligible reservation (no/invalid/non-original request id, a
    malformed/ambiguous/future timestamp, or an expired window) is
    manual_review_required and left UNCHANGED with no provider call. This route
    NEVER captures, authorizes, refunds, marks the reservation FAILED,
    overwrites an order id, or generates a new request id. Unknown approval
    id -> 404.
    """
    try:
        execution = get_external_order_by_approval(approval_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if execution is None:
        raise HTTPException(
            status_code=404,
            detail=f"No external purchase execution exists for approval {approval_id}",
        )

    outcome, updated, detail = recover_external_creating_order(execution)
    report_outcome, reconciled, review, retryable, message = (
        _CREATING_RECOVER_OUTCOMES.get(
            outcome,
            (
                outcome,
                False,
                True,
                False,
                "Unrecognized recovery outcome; left for manual review.",
            ),
        )
    )
    return ExternalReconcileReport(
        execution=ExternalExecutionTriage.from_record(updated),
        approval_id=approval_id,
        outcome=report_outcome,
        status=updated.status.value,
        reconciled=reconciled,
        review_required=review,
        retryable=retryable,
        captured=updated.status is ExternalOrderStatus.CAPTURED,
        message=message,
        detail=detail or None,
    )


# ---------------------------------------------------------------------------
# AI Intent Agent (natural language -> validated structured intent).
# This endpoint ONLY interprets text. It never calls PayPal or purchases.
# ---------------------------------------------------------------------------


@app.post("/ai/intent", response_model=ProductIntent)
def ai_intent(req: IntentRequest) -> ProductIntent:
    """Convert a natural-language purchasing request into a structured intent.

    Returns the validated ProductIntent. This does not search the catalog or
    trigger any payment — it is interpretation only.
    """
    try:
        return interpret_intent(req.message)
    except AIConfigError as exc:
        # Provider is misconfigured (no API key) — not the client's fault.
        logger.error("AI provider not configured: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AIServiceError as exc:
        logger.error("AI provider service error: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (InvalidAIResponseError, IntentValidationError) as exc:
        # The provider replied, but not with a usable/valid structured intent.
        logger.error("Invalid intent from AI provider: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# AI Recommendation (intent -> deterministic catalog search -> AI pick).
# Recommendation ONLY: this endpoint never creates, captures, refunds, or
# modifies a PayPal order, and never evaluates authorization thresholds —
# authorization data is passed through untouched for the future Policy Engine.
# ---------------------------------------------------------------------------


@app.post("/ai/recommend", response_model=RecommendResponse)
def ai_recommend(req: RecommendRequest) -> RecommendResponse:
    """Recommend one catalog product for a natural-language request.

    Flow: message -> Intent Agent -> hard-constraint product search ->
    Recommendation Agent -> structured response. If no product satisfies the
    hard constraints, that is reported honestly (status "no_candidates")
    without an AI call and without relaxing any constraint.
    """
    try:
        intent = interpret_intent(req.message)
        candidates = find_candidates(intent)

        if not candidates:
            return RecommendResponse(
                status="no_candidates", intent=intent, candidates=[]
            )

        recommendation = recommend_from_candidates(intent, candidates)
    except AIConfigError as exc:
        logger.error("AI provider not configured: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AIServiceError as exc:
        logger.error("AI provider service error: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (
        InvalidAIResponseError,
        IntentValidationError,
        RecommendationValidationError,
    ) as exc:
        # The provider replied, but not with usable/valid structured output
        # (includes a recommendation naming a non-candidate product).
        logger.error("Invalid AI output during recommendation: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if recommendation.recommended_product_id is None:
        # The model judged no candidate suitable and said so explicitly.
        return RecommendResponse(
            status="no_suitable_candidate",
            intent=intent,
            candidates=candidates,
            recommendation=recommendation,
        )

    # The agent guarantees the id is in the candidate list; serve the product
    # record from the catalog so every price/spec in the response is exact.
    product = next(
        p for p in candidates if p.id == recommendation.recommended_product_id
    )
    return RecommendResponse(
        status="recommended",
        intent=intent,
        candidates=candidates,
        recommendation=recommendation,
        recommended_product=product,
    )


# ---------------------------------------------------------------------------
# Policy Engine (deterministic — the ONLY purchase-authorization authority).
# Evaluation only: this endpoint never creates, captures, or refunds a PayPal
# order, and makes no AI or network call of any kind.
# ---------------------------------------------------------------------------


@app.post("/policy/evaluate", response_model=PolicyDecision)
def policy_evaluate(req: PolicyEvaluateRequest) -> PolicyDecision:
    """Evaluate whether a selected product may be purchased, and how.

    Applies the deterministic policy rules to the supplied product data,
    authorization, and optional shopping constraints. Returns a decision
    (ALLOW_AUTONOMOUS_PURCHASE / REQUIRE_USER_APPROVAL / DENY) with a stable
    reason_code and human-readable reason. It decides; it never executes.
    """
    return evaluate_policy(
        product=req.product,
        authorization=req.authorization,
        hard_constraints=req.hard_constraints,
    )


# ---------------------------------------------------------------------------
# Decision-pipeline orchestration (intent -> search -> recommendation ->
# policy). A plan is a DECISION, never an executed payment: this endpoint
# makes no PayPal call and its response carries no payment artifact.
# ---------------------------------------------------------------------------


@app.post("/ai/plan", response_model=PlanResponse)
def ai_plan(req: PlanRequest) -> PlanResponse:
    """Run the full decision pipeline for a natural-language request.

    Returns the pipeline trace (intent, candidates, recommendation), the
    catalog-backed selected product, the inert user authorization, and the
    Policy Engine's ruling. Executing any purchase is out of scope.
    """
    try:
        return build_plan(req.message)
    except AIConfigError as exc:
        logger.error("AI provider not configured: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AIServiceError as exc:
        logger.error("AI provider service error: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (
        InvalidAIResponseError,
        IntentValidationError,
        RecommendationValidationError,
    ) as exc:
        logger.error("Invalid AI output while building plan: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Policy-gated purchase execution (Phase 6B).
# Gemini recommends. Policy Engine authorizes. PayPal executes.
# The server derives price (catalog), decision (Policy Engine re-run), and
# PayPal amount (currency service) itself — client-sent price/decision/amount
# fields are not part of the schema and are dropped unread.
# ---------------------------------------------------------------------------


@app.post("/purchase/execute", response_model=ExecutionResult)
def purchase_execute(req: ExecuteRequest) -> ExecutionResult:
    """Evaluate policy for a catalog product and create a PayPal order on ALLOW.

    DENY and REQUIRE_USER_APPROVAL return structured results with no PayPal
    interaction of any kind. Only ALLOW_AUTONOMOUS_PURCHASE reaches PayPal,
    using the catalog price converted at the fixed demo FX rate. No capture
    happens here — the order awaits buyer approval.
    """
    try:
        return execute_purchase(
            product_id=req.product_id,
            authorization=req.authorization,
            hard_constraints=req.hard_constraints,
        )
    except ProductNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CurrencyConversionError as exc:
        # Catalog prices are validated data, so this is unexpected — surface
        # it as an upstream-data problem, never as a created order.
        logger.error("Currency conversion failed during execution: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except PayPalCheckoutConfigError as exc:
        # Deployment fault: PAYPAL_RETURN_URL / PAYPAL_CANCEL_URL is not an
        # absolute https URL. Not an upstream PayPal failure, so not a 502 —
        # and no order was created, because the check runs first.
        logger.error("Checkout URL configuration is invalid: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except PayPalAuthError as exc:
        logger.error("Auth failed while executing purchase: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except PayPalOrderError as exc:
        logger.error("Create-order failed while executing purchase: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/purchase/approve", response_model=ExecutionResult)
def purchase_approve(req: ExecuteRequest) -> ExecutionResult:
    """Execute a purchase the user has EXPLICITLY approved.

    Calling this endpoint means "the user approved proceeding". It is an
    additional authorization event, never a policy bypass: the product is
    re-fetched from the catalog and the Policy Engine re-runs from scratch.
    A DENY stays denied; REQUIRE_USER_APPROVAL is satisfied by the explicit
    approval and proceeds to the same safe PayPal path as /purchase/execute.
    The order is only CREATED — the buyer still approves payment on PayPal's
    hosted checkout; nothing is captured here.
    """
    try:
        return approve_purchase(
            product_id=req.product_id,
            authorization=req.authorization,
            hard_constraints=req.hard_constraints,
        )
    except ProductNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CurrencyConversionError as exc:
        logger.error("Currency conversion failed during approval: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except PayPalCheckoutConfigError as exc:
        # Deployment fault: PAYPAL_RETURN_URL / PAYPAL_CANCEL_URL is not an
        # absolute https URL. Not an upstream PayPal failure, so not a 502 —
        # and no order was created, because the check runs first.
        logger.error("Checkout URL configuration is invalid: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except PayPalAuthError as exc:
        logger.error("Auth failed while executing approved purchase: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except PayPalOrderError as exc:
        logger.error("Create-order failed while executing approved purchase: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post(
    "/purchase/capture/{order_id}",
    response_model=CaptureResult,
    responses={
        404: {
            "description": "No transaction of this server owns the order id "
            "(UNKNOWN_ORDER), or PayPal has no such order. In the former "
            "case no PayPal capture was attempted."
        },
        409: {
            "description": "The order belongs to an external purchase "
            "execution, not to a local-catalog transaction "
            "(EXTERNAL_ORDER_NOT_CAPTURABLE_HERE) — no PayPal call was made "
            "and no catalog transaction was created — or PayPal reports a "
            "state conflict (ORDER_NOT_APPROVED / already captured)."
        },
        500: {
            "description": "CAPTURE_LEDGER_DESYNC: PayPal captured the order "
            "but the owning ledger row disappeared before it could be "
            "recorded. The capture is NOT rolled back; the detail says so "
            "explicitly and carries PayPal's capture facts."
        },
    },
)
def purchase_capture(order_id: str) -> CaptureResult:
    """Capture an approved, catalog-owned PayPal order — the flow's last step.

    Takes ONLY the order id from the URL; any request body is ignored, so a
    client can never supply a capture amount, currency, product, or policy
    decision — PayPal's order is the source of truth for what was charged.

    OWNERSHIP IS VALIDATED BEFORE PAYPAL IS CONTACTED (Phase 11F-B0). The
    order id is resolved against this server's own ledgers first: only an id
    owned by a local-catalog `transactions` row is forwarded to PayPal (and
    the stored id is what is forwarded). An id owned by Phase 11E's
    external_purchase_orders gets 409 EXTERNAL_ORDER_NOT_CAPTURABLE_HERE and
    an id owned by neither gets 404 UNKNOWN_ORDER — both without any PayPal
    call, and neither creating or modifying a catalog transaction.

    Buyer approval is still PayPal's to enforce: a still-CREATED order
    returns 409 (ORDER_NOT_APPROVED), as does a double capture.
    """
    try:
        return capture_purchase(order_id)
    except ValueError as exc:
        # Blank/invalid order id — rejected before any lookup or PayPal call.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OwnershipRefusedError as exc:
        # External or unknown order: deterministic refusal, no PayPal call,
        # no catalog ledger row written.
        raise _ownership_refusal(exc) from exc
    except PayPalAuthError as exc:
        logger.error("Auth failed while capturing purchase: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except PayPalOrderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (PayPalAlreadyCapturedError, PayPalOrderNotApprovedError) as exc:
        # Conflict with the order's state: buyer has not approved yet, or
        # the order was already captured. Never a fabricated success.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PayPalCaptureError as exc:
        logger.error("Purchase capture failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except CaptureLedgerDesyncError as exc:
        # Replaces the old log-and-swallow. PayPal DID capture and nothing
        # here rolled it back, so this is NOT reported as a success and NOT
        # reported as a rollback: a 500 naming the inconsistency, with the
        # capture facts, is the only honest answer available in this phase.
        logger.error("Capture ledger desync on /purchase/capture: %s", exc)
        raise HTTPException(status_code=500, detail=exc.as_detail()) from exc
    except CaptureNotCompletedError as exc:
        # D-5 FIX (Phase 11F-J): a non-COMPLETED nested capture status (e.g.
        # PENDING) is not a collection. The ledger kept its prior state; this
        # is reported as a non-success, never a captured transaction.
        logger.warning("Catalog capture not completed on /purchase/capture: %s", exc)
        raise HTTPException(status_code=502, detail=exc.as_detail()) from exc


# ---------------------------------------------------------------------------
# PayPal webhook receiver (Phase 8A verify + Phase 8B reconcile).
# Every delivery is verified with PayPal's official verify-webhook-signature
# API before being trusted. A VERIFIED event is then reconciled with the
# local transaction ledger (bookkeeping only — recording what PayPal already
# did). Webhooks never trigger a capture, refund, or AI call.
# ---------------------------------------------------------------------------


@app.post("/webhooks/paypal")
async def paypal_webhook(request: Request) -> dict:
    """Receive a PayPal webhook event and verify its signature with PayPal.

    The raw body is parsed here (not via a Pydantic model) so the event is
    forwarded to PayPal's verification API exactly as delivered, and so no
    client-supplied field — e.g. {"verified": true} — can ever influence the
    outcome: authenticity is decided solely by PayPal's answer.

    Responses: 200 {"status": "verified", "event_type": ..., "reconciliation":
    {...}} on SUCCESS; 400 for malformed JSON, missing PAYPAL-* headers, or a
    FAILURE verdict; 502 when the verification API itself cannot be consulted;
    503 when the webhook id is not configured. Verification failure is never
    treated as success, and ONLY verified events reach reconciliation.

    Reconciliation (Phase 8B) updates the local ledger to match what PayPal
    reports. Events that don't apply (unknown type, no matching transaction,
    stale/out-of-order) are still acknowledged with 200 — a 4xx/5xx would
    only make PayPal redeliver an event we cannot use.
    """
    raw = await request.body()
    try:
        event = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="Request body is not valid JSON"
        ) from exc
    if not isinstance(event, dict):
        raise HTTPException(
            status_code=400, detail="Webhook payload must be a JSON object"
        )

    try:
        result = verify_webhook(event=event, headers=request.headers)
    except MissingWebhookHeadersError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except WebhookConfigError as exc:
        # Server-side misconfiguration — not the sender's fault.
        logger.error("Webhook verification not configured: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except PayPalAuthError as exc:
        logger.error("Auth failed while verifying webhook: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except WebhookVerificationError as exc:
        logger.error("Webhook verification API failure: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if not result["verified"]:
        # PayPal said FAILURE: the event is untrusted and goes no further.
        raise HTTPException(
            status_code=400, detail="PayPal webhook signature verification failed"
        )

    # Verified: reconcile the CATALOG ledger with what PayPal reports (8B).
    # This is bookkeeping only — no capture, refund, or PayPal call happens.
    reconciliation = reconcile_event(event)

    # Phase 11F-F: if no catalog transaction owned this event, it may belong
    # to an EXTERNAL purchase order. Reconcile that ledger INDEPENDENTLY — a
    # read-only get_order verifies the real state before any transition, and
    # no capture or refund is ever called. Separation holds in both
    # directions: a catalog-owned event never reaches here, and external
    # reconciliation never touches the catalog ledger, so a single event
    # mutates at most one ledger.
    external_reconciliation = None
    if reconciliation.get("action") in ("transaction_not_found", "unidentifiable"):
        external_reconciliation = reconcile_external_event(event)

    # Phase 11F-K.21: if NEITHER the catalog `transactions` ledger NOR an
    # external order owned this event, it may belong to a catalog purchase
    # ATTEMPT that carries a persisted order id (K.20) but never finalized.
    # Reconcile that INDEPENDENTLY — ownership is resolved by the SERVER-STORED
    # order id on a CREATING attempt, and a read-only get_order verifies the
    # real state before any transition. It never captures, refunds, creates an
    # order, or marks a transaction captured from the event alone; it only ever
    # settles a CREATING attempt to FINALIZED on conclusive evidence. Keeping
    # it last preserves single-ledger ownership: a finalized attempt's order is
    # owned by a `transactions` row and is handled by the primary reconciler.
    catalog_attempt_reconciliation = None
    if reconciliation.get("action") in ("transaction_not_found", "unidentifiable") and (
        external_reconciliation is None
        or external_reconciliation.get("action") == "external_execution_not_found"
    ):
        catalog_attempt_reconciliation = reconcile_catalog_attempt_event(event)

    return {
        "status": "verified",
        "event_type": result["event_type"],
        "reconciliation": reconciliation,
        "external_reconciliation": external_reconciliation,
        "catalog_attempt_reconciliation": catalog_attempt_reconciliation,
    }


# ---------------------------------------------------------------------------
# Conversational chat (Phase 8E Step 3 — route, plan, propose; NEVER execute).
# A chat turn may produce a reference-only pending action in the session
# store, but no PayPal order, capture, or refund ever happens here — even on
# ALLOW_AUTONOMOUS_PURCHASE. Execution requires the future explicit
# confirmation endpoint.
# ---------------------------------------------------------------------------


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    """Process one conversational turn.

    Routes the message (SHOP / REFUND / HISTORY / SMALLTALK), runs only the
    matching read/planning flow, persists both conversation turns, and
    returns a typed response. Model failures fail the whole turn cleanly —
    they can never fall through into a purchase or refund action.
    """
    try:
        return handle_chat(message=req.message, session_id=req.session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AIConfigError as exc:
        logger.error("AI provider not configured for chat: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AIServiceError as exc:
        logger.error("AI provider service error during chat: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (
        InvalidAIResponseError,
        RouteValidationError,
        IntentValidationError,
        RecommendationValidationError,
    ) as exc:
        # The model replied, but not with usable/valid structured output.
        logger.error("Invalid AI output during chat: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/sessions/{session_id}", response_model=SessionDetail)
def sessions_get(session_id: str) -> SessionDetail:
    """Return one session's full state for the frontend (strictly read-only).

    Identity, timestamps, turns in the deterministic chronological order
    established in the session store, and the ACTIVE pending action (an
    expired one appears as null and is neither revived nor extended). No
    Gemini, PayPal, or Policy Engine call; nothing is created or modified.
    Unknown session -> 404, never auto-created.
    """
    try:
        session = get_session(session_id)
        return SessionDetail(
            session_id=session.session_id,
            created_at=session.created_at,
            updated_at=session.updated_at,
            turns=get_turns(session.session_id),
            pending_action=get_pending_action(session.session_id),
        )
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/sessions/{session_id}/confirm", response_model=ConfirmationResponse)
def sessions_confirm(session_id: str, req: ConfirmRequest) -> ConfirmationResponse:
    """Explicitly confirm (or decline) the session's stored pending action.

    The body carries ONLY {"confirm": bool} — the action reference comes
    exclusively from the server-side session store, and execution runs
    through the existing deterministic services (approve_purchase /
    refund_transaction), which re-fetch and re-validate everything. No
    Gemini call is involved. Error mapping follows the existing purchase
    and refund endpoint conventions.
    """
    try:
        return confirm_pending_action(session_id, req.confirm)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except NoPendingActionError as exc:
        # Nothing to confirm (never proposed, already consumed, or expired).
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # --- purchase path (same mapping as /purchase/approve) ---
    except ProductNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CurrencyConversionError as exc:
        logger.error("Currency conversion failed during confirmation: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except PayPalOrderError as exc:
        logger.error("Create-order failed during confirmation: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    # --- refund path (same mapping as /transactions/{id}/refund) ---
    except TransactionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AlreadyRefundedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RefundInProgressError as exc:
        # A concurrent refund already claimed the transaction (REFUNDING).
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RefundNotAllowedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MissingCaptureIdError as exc:
        logger.error("Refund blocked by inconsistent record: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except RefundLedgerDesyncError as exc:
        logger.error("Refund ledger desync during confirmation: %s", exc)
        raise HTTPException(status_code=500, detail=exc.as_detail()) from exc
    except PayPalCaptureNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PayPalAlreadyRefundedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PayPalRefundError as exc:
        # Includes PayPalRefundNetworkError (ambiguous → left REFUNDING).
        logger.error("Refund failed during confirmation: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    # --- shared upstream ---
    except PayPalAuthError as exc:
        logger.error("Auth failed during confirmation: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Transaction ledger (local SQLite tracking layer — read-only endpoints).
# PayPal remains the payment authority; these records only track lifecycle.
# ---------------------------------------------------------------------------


@app.get("/transactions", response_model=TransactionListResponse)
def transactions_list(
    status: Optional[TransactionStatus] = Query(
        default=None, description="Filter by ledger status, e.g. CAPTURED."
    ),
    limit: int = Query(
        default=20, ge=1, le=100, description="Max rows returned (1-100)."
    ),
) -> TransactionListResponse:
    """Return transaction history, newest-first (Phase 8D — read-only).

    Deterministic commerce-state query over the existing SQLite ledger:
    no PayPal call, no AI, no policy evaluation, and no mutation of any
    kind. An invalid status or an out-of-range limit is rejected with a
    422 by validation (never silently clamped); zero matches is a normal
    200 with an empty list.
    """
    records = list_transactions(status=status, limit=limit)
    return TransactionListResponse(transactions=records, count=len(records))


@app.post("/transactions/{transaction_id}/refund")
def transactions_refund(transaction_id: str) -> dict:
    """Issue a FULL refund for one CAPTURED transaction (Phase 8C).

    Takes ONLY the local transaction id from the URL; any request body is
    ignored, so a client can never supply a capture id, amount, currency,
    order id, or policy decision — the server refunds exactly the capture id
    stored on the transaction, and PayPal's refund API is the authority on
    success. The ledger moves CAPTURED -> REFUNDED only after PayPal
    confirms; on any PayPal failure the transaction remains CAPTURED.
    Deterministic eligibility only — no AI, no Policy Engine involvement.
    """
    try:
        return refund_transaction(transaction_id)
    except TransactionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AlreadyRefundedError as exc:
        # Idempotency guard: already REFUNDED locally → PayPal is not called.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RefundInProgressError as exc:
        # Phase 11F-K.13: a concurrent refund already claimed this transaction
        # (status REFUNDING); this request made no PayPal call. Fail closed.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RefundNotAllowedError as exc:
        # Deterministic eligibility: only CAPTURED is refundable.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MissingCaptureIdError as exc:
        # Inconsistent record (CAPTURED without capture id): a server-side
        # data problem — PayPal was never called.
        logger.error("Refund blocked by inconsistent record: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except RefundLedgerDesyncError as exc:
        # PayPal refunded but the ledger write failed; surfaced (never a false
        # success) and left REFUNDING for reconciliation.
        logger.error("Refund ledger desync on /transactions refund: %s", exc)
        raise HTTPException(status_code=500, detail=exc.as_detail()) from exc
    except PayPalAuthError as exc:
        logger.error("Auth failed while refunding transaction: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except PayPalCaptureNotFoundError as exc:
        # Same mapping as /paypal/refund: PayPal has no such capture.
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PayPalAlreadyRefundedError as exc:
        # PayPal says the capture is already fully refunded — surfaced as a
        # conflict; the ledger has been reconciled to REFUNDED by the service.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PayPalRefundError as exc:
        # Includes PayPalRefundNetworkError (ambiguous → left REFUNDING).
        logger.error("Refund failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/transactions/paypal/{order_id}", response_model=TransactionRecord)
def transactions_get_by_order(order_id: str) -> TransactionRecord:
    """Return the transaction tracking a given PayPal order id, or 404."""
    try:
        return get_transaction_by_order(order_id)
    except TransactionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/transactions/{transaction_id}", response_model=TransactionRecord)
def transactions_get(transaction_id: str) -> TransactionRecord:
    """Return one stored transaction by its id, or 404."""
    try:
        return get_transaction(transaction_id)
    except TransactionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
