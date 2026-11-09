/**
 * Wire-contract tests for the capture call: the browser must send ONLY the
 * PayPal order id, in the URL path, to the PayPilot backend — never a body,
 * never a financial field, and never a request to PayPal itself.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { capturePurchase, getTransactionByOrder } from "../api";
import { makeCaptureResult, makeTransactionRecord } from "../../test/fixtures";

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

describe("capturePurchase wire contract", () => {
  it("POSTs to the backend /purchase/capture/{order_id}", async () => {
    const fetchMock = stubFetch(makeCaptureResult());

    await capturePurchase("3A032920JC2100350");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(
      "http://localhost:8000/purchase/capture/3A032920JC2100350",
    );
    expect(init.method).toBe("POST");
  });

  it("sends NO request body — the order id is the only data", async () => {
    const fetchMock = stubFetch(makeCaptureResult());

    await capturePurchase("3A032920JC2100350");

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.body).toBeUndefined();
    expect(init.headers).toBeUndefined(); // no JSON content-type: no payload
  });

  it("never sends amount/currency/capture/transaction/product fields", async () => {
    const fetchMock = stubFetch(makeCaptureResult());

    await capturePurchase("3A032920JC2100350");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.body).toBeUndefined();
    for (const forbidden of [
      "amount",
      "currency",
      "price",
      "price_inr",
      "capture_id",
      "transaction_id",
      "product_id",
      "client_id",
      "client_secret",
      "access_token",
    ]) {
      expect(url).not.toContain(forbidden);
    }
  });

  it("URL-encodes the order id", async () => {
    const fetchMock = stubFetch(makeCaptureResult());

    await capturePurchase("abc/../def");

    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toBe(
      "http://localhost:8000/purchase/capture/abc%2F..%2Fdef",
    );
  });

  it("talks ONLY to the PayPilot backend, never to PayPal", async () => {
    const fetchMock = stubFetch(makeCaptureResult());

    await capturePurchase("3A032920JC2100350");

    for (const call of fetchMock.mock.calls) {
      const url = String(call[0]);
      expect(url.startsWith("http://localhost:8000/")).toBe(true);
      expect(url).not.toMatch(/paypal\.com/i);
    }
  });
});

describe("getTransactionByOrder wire contract", () => {
  it("GETs the backend ledger record for one PayPal order", async () => {
    const fetchMock = stubFetch(makeTransactionRecord());

    await getTransactionByOrder("3A032920JC2100350");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(
      "http://localhost:8000/transactions/paypal/3A032920JC2100350",
    );
    expect(init.method ?? "GET").toBe("GET");
    expect(init.body).toBeUndefined();
  });
});
