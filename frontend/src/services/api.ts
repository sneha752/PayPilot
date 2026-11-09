/**
 * Centralized API service layer — the only module components should
 * import for backend calls. One function per frontend-facing backend
 * endpoint; every call goes through apiRequest (no scattered fetch()).
 *
 * Deliberately NOT covered here (not frontend-facing, or not needed yet):
 * raw /paypal/* endpoints, /webhooks/paypal, /policy/evaluate,
 * /purchase/* (chat + confirm are the conversational path the UI uses).
 */

import { apiRequest } from "./httpClient";
import { setToken } from "./authService";
import type {
  CaptureResult,
  ChatRequest,
  ChatResponse,
  ComparisonRequest,
  ComparisonResponse,
  ConfirmationResponse,
  PlanResponse,
  Product,
  RefundResult,
  SessionDetail,
  TransactionListResponse,
  TransactionRecord,
  TransactionStatus,
} from "../types";

// ---------------------------------------------------------------------------
// Authentication (Phase 11F-H)
// ---------------------------------------------------------------------------

interface LoginResponse {
  token: string;
  token_type: string;
  expires_in: number;
}

/**
 * POST /auth/login — exchange the operator API key for a bearer token and
 * store it in memory. The key is sent once, in the body, over the same
 * centralized client; the returned token is then attached to every
 * subsequent request by httpClient. Throws ApiError (401) on a bad key.
 */
export async function login(apiKey: string, signal?: AbortSignal): Promise<void> {
  const result = await apiRequest<LoginResponse>("/auth/login", {
    method: "POST",
    body: { api_key: apiKey },
    signal,
  });
  setToken(result.token);
}

// ---------------------------------------------------------------------------
// Products
// ---------------------------------------------------------------------------

/** GET /products — the full catalog. */
export function getProducts(signal?: AbortSignal): Promise<Product[]> {
  return apiRequest<Product[]>("/products", { signal });
}

/** GET /products/{id} — one product, 404 if unknown. */
export function getProduct(id: string, signal?: AbortSignal): Promise<Product> {
  return apiRequest<Product>(`/products/${encodeURIComponent(id)}`, { signal });
}

// ---------------------------------------------------------------------------
// AI
// ---------------------------------------------------------------------------

/** POST /ai/plan — full decision pipeline (decision only, never a payment). */
export function planPurchase(
  message: string,
  signal?: AbortSignal,
): Promise<PlanResponse> {
  return apiRequest<PlanResponse>("/ai/plan", {
    method: "POST",
    body: { message },
    signal,
  });
}

/** POST /chat — one conversational turn; omit sessionId to start fresh. */
export function sendChat(
  message: string,
  sessionId?: string | null,
  signal?: AbortSignal,
): Promise<ChatResponse> {
  const body: ChatRequest = { message, session_id: sessionId ?? null };
  return apiRequest<ChatResponse>("/chat", { method: "POST", body, signal });
}

// ---------------------------------------------------------------------------
// Product discovery comparison (Phase 10G)
// ---------------------------------------------------------------------------

/** Discovery results the backend fetches per comparison (backend max 30). */
export const COMPARISON_LIMIT = 10;
/** Ranked results to return (backend max 5). */
export const COMPARISON_TOP_K = 3;

/**
 * POST /discovery/compare — live product discovery, deterministic
 * filtering, and AI ranking, all performed by the backend.
 *
 * SECURITY: the natural-language query and two bounded counts are the ONLY
 * things the browser sends. No price, currency, exchange rate, product id,
 * PayPal/order/capture/refund id, or payment amount is ever transmitted —
 * the backend derives the requirements and owns every monetary value. This
 * is also why the frontend never talks to Channel3, Serper, or any AI
 * provider directly: they are reachable only through this endpoint.
 */
export function compareProducts(
  query: string,
  options: { limit?: number; topK?: number } = {},
  signal?: AbortSignal,
): Promise<ComparisonResponse> {
  const body: ComparisonRequest = {
    query,
    limit: options.limit ?? COMPARISON_LIMIT,
    top_k: options.topK ?? COMPARISON_TOP_K,
  };
  return apiRequest<ComparisonResponse>("/discovery/compare", {
    method: "POST",
    body,
    signal,
  });
}

// ---------------------------------------------------------------------------
// Sessions
// ---------------------------------------------------------------------------

/** GET /sessions/{id} — full read-only session state. */
export function getSession(
  sessionId: string,
  signal?: AbortSignal,
): Promise<SessionDetail> {
  return apiRequest<SessionDetail>(
    `/sessions/${encodeURIComponent(sessionId)}`,
    { signal },
  );
}

/**
 * POST /sessions/{id}/confirm — explicitly confirm (true) or decline
 * (false) the session's stored pending action.
 */
export function confirmSession(
  sessionId: string,
  confirm: boolean,
  signal?: AbortSignal,
): Promise<ConfirmationResponse> {
  return apiRequest<ConfirmationResponse>(
    `/sessions/${encodeURIComponent(sessionId)}/confirm`,
    { method: "POST", body: { confirm }, signal },
  );
}

// ---------------------------------------------------------------------------
// Purchase capture (Phase 9 Step 4)
// ---------------------------------------------------------------------------

/**
 * POST /purchase/capture/{order_id} — capture an approved PayPal order.
 *
 * SECURITY: the PayPal order id in the URL is the ONLY thing the browser
 * sends. No body, ever — the backend derives amount, currency, product,
 * and ledger state itself, with PayPal's order as the source of truth.
 */
export function capturePurchase(
  orderId: string,
  signal?: AbortSignal,
): Promise<CaptureResult> {
  return apiRequest<CaptureResult>(
    `/purchase/capture/${encodeURIComponent(orderId)}`,
    { method: "POST", signal },
  );
}

// ---------------------------------------------------------------------------
// Transactions
// ---------------------------------------------------------------------------

/** GET /transactions/paypal/{order_id} — the ledger record tracking one
 * PayPal order (authoritative local state, e.g. after "already captured"). */
export function getTransactionByOrder(
  orderId: string,
  signal?: AbortSignal,
): Promise<TransactionRecord> {
  return apiRequest<TransactionRecord>(
    `/transactions/paypal/${encodeURIComponent(orderId)}`,
    { signal },
  );
}

/** GET /transactions — history, newest-first, optionally filtered. */
export function getTransactions(
  options: { status?: TransactionStatus; limit?: number } = {},
  signal?: AbortSignal,
): Promise<TransactionListResponse> {
  return apiRequest<TransactionListResponse>("/transactions", {
    query: { status: options.status, limit: options.limit },
    signal,
  });
}

/** GET /transactions/{id} — one stored transaction, 404 if unknown. */
export function getTransaction(
  transactionId: string,
  signal?: AbortSignal,
): Promise<TransactionRecord> {
  return apiRequest<TransactionRecord>(
    `/transactions/${encodeURIComponent(transactionId)}`,
    { signal },
  );
}

/** POST /transactions/{id}/refund — full refund of a CAPTURED transaction. */
export function refundTransaction(
  transactionId: string,
  signal?: AbortSignal,
): Promise<RefundResult> {
  return apiRequest<RefundResult>(
    `/transactions/${encodeURIComponent(transactionId)}/refund`,
    { method: "POST", signal },
  );
}
