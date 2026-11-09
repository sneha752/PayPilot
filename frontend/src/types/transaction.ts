/** Mirrors backend `app/schemas/transaction.py`. */

export type TransactionStatus =
  | "ORDER_CREATED"
  | "ORDER_APPROVED"
  | "CAPTURED"
  | "FAILED"
  // Short-lived intermediate claim (backend Phase 11F-K.13): a refund has been
  // claimed but is not yet confirmed REFUNDED. It is NOT a successful refund
  // and NOT still cleanly CAPTURED — the refund action is unavailable while in
  // this state.
  | "REFUNDING"
  | "REFUNDED";

/** One purchase's lifecycle: product -> PayPal order -> PayPal capture. */
export interface TransactionRecord {
  transaction_id: string;
  product_id: string;
  product_name: string;
  catalog_price_inr: number;
  paypal_amount: string;
  paypal_currency: string;
  paypal_order_id: string;
  paypal_capture_id?: string | null;
  status: TransactionStatus;
  created_at: string;
  updated_at: string;
}

export interface TransactionListResponse {
  transactions: TransactionRecord[];
  count: number;
}

/**
 * Success payload of POST /transactions/{id}/refund (mirrors the backend
 * refund service's result dict / `RefundResult` shape).
 */
export interface RefundResult {
  status: "refunded";
  transaction_id: string;
  paypal_order_id: string;
  paypal_capture_id?: string | null;
  refund_id?: string | null;
  refund_status?: string | null;
  amount?: string | null;
  currency?: string | null;
}
