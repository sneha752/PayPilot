/**
 * PayPal approval-return detection (Phase 9 Step 4).
 *
 * Contract, taken from the backend's existing Orders v2 integration: the
 * order is created with application_context.return_url / cancel_url, and
 * PayPal's hosted checkout redirects the buyer back with:
 *
 *   approval return:  ?token=<PAYPAL_ORDER_ID>&PayerID=<payer id>
 *   cancel return:    ?token=<PAYPAL_ORDER_ID>          (no PayerID)
 *
 * "token" IS the PayPal order id — PayPal reuses the order id as the
 * checkout token. We treat a URL as an approval return ONLY when both
 * parameters are present: a cancel (token alone) must never trigger a
 * capture attempt. The PayerID value itself is never stored or sent to
 * the backend — its presence only distinguishes approval from cancel.
 */

export interface PayPalReturn {
  /** The PayPal order id, from the `token` query parameter. */
  orderId: string;
}

/** Parse a query string; null unless it is a PayPal APPROVAL return. */
export function parsePayPalReturn(search: string): PayPalReturn | null {
  const params = new URLSearchParams(search);
  const token = params.get("token")?.trim() ?? "";
  const payerId = params.get("PayerID")?.trim() ?? "";
  if (!token || !payerId) return null;
  return { orderId: token };
}

/**
 * Remove the PayPal return parameters from the address bar (no reload,
 * no navigation). Called as soon as the return is detected so a page
 * refresh cannot re-trigger a capture attempt. Other query parameters
 * are preserved untouched.
 */
export function stripPayPalReturnParams(): void {
  const url = new URL(window.location.href);
  url.searchParams.delete("token");
  url.searchParams.delete("PayerID");
  window.history.replaceState(null, "", url.pathname + url.search + url.hash);
}
