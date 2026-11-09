/**
 * Wire-contract tests for the refund call: the browser sends ONLY the
 * local transaction id, in the URL path, to the PayPilot backend —
 * never a body, an amount, a PayPal identifier, or a PayPal request.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { refundTransaction } from "../api";
import { makeRefundResult } from "../../test/fixtures";

function stubFetch(body: unknown, status = 200) {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("refundTransaction wire contract", () => {
  it("POSTs to the backend /transactions/{id}/refund", async () => {
    const fetchMock = stubFetch(makeRefundResult());

    await refundTransaction("txn_1");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/transactions/txn_1/refund");
    expect(init.method).toBe("POST");
  });

  it("sends NO request body — the transaction id is the only data", async () => {
    const fetchMock = stubFetch(makeRefundResult());

    await refundTransaction("txn_1");

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.body).toBeUndefined();
    expect(init.headers).toBeUndefined(); // no JSON content-type: no payload
  });

  it("never sends amount/currency/order/capture/refund/product fields", async () => {
    const fetchMock = stubFetch(makeRefundResult());

    await refundTransaction("txn_1");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.body).toBeUndefined();
    for (const forbidden of [
      "amount",
      "currency",
      "price",
      "price_inr",
      "paypal_order_id",
      "order_id",
      "capture_id",
      "refund_id",
      "product_id",
      "client_id",
      "client_secret",
      "access_token",
    ]) {
      expect(url).not.toContain(forbidden);
    }
  });

  it("URL-encodes the transaction id", async () => {
    const fetchMock = stubFetch(makeRefundResult());

    await refundTransaction("abc/../def");

    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toBe("http://localhost:8000/transactions/abc%2F..%2Fdef/refund");
  });

  it("talks ONLY to the PayPilot backend, never to PayPal", async () => {
    const fetchMock = stubFetch(makeRefundResult());

    await refundTransaction("txn_1");

    for (const call of fetchMock.mock.calls) {
      const url = String(call[0]);
      expect(url.startsWith("http://localhost:8000/")).toBe(true);
      expect(url).not.toMatch(/paypal\.com/i);
    }
  });
});
