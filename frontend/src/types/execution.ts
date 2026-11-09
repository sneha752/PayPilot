/** Mirrors backend `app/schemas/execution.py`. */

import type { PolicyDecision } from "./policy";

export type ExecutionStatus = "denied" | "approval_required" | "order_created";

/**
 * Success payload of POST /purchase/capture/{order_id}. Built by the
 * backend verbatim from PayPal's capture response — nothing here is
 * recomputed or client-supplied.
 */
export interface CaptureResult {
  status: "captured";
  order_id: string;
  order_status?: string | null;
  capture_id?: string | null;
  capture_status?: string | null;
  amount?: string | null;
  currency?: string | null;

  // Local ledger tracking (null when no local transaction exists).
  transaction_id?: string | null;
  transaction_status?: string | null;
}

/**
 * Outcome of a policy-gated execution attempt. The PayPal fields are
 * populated ONLY when status === "order_created".
 */
export interface ExecutionResult {
  status: ExecutionStatus;
  approval_used: boolean;

  // Product information — authoritative, from the backend catalog.
  product_id: string;
  product_name: string;
  catalog_price_inr: number;

  // Policy decision — re-evaluated server-side for this exact attempt.
  policy_decision: PolicyDecision;

  // PayPal order facts (order_created only).
  paypal_currency?: string | null;
  paypal_amount?: string | null;
  demo_fx_rate?: string | null;
  paypal_order_id?: string | null;
  approve_url?: string | null;

  // Local ledger record id (order_created only).
  transaction_id?: string | null;
}
