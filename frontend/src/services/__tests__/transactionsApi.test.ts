/**
 * Wire-contract tests for the history calls: GET requests to the PayPilot
 * backend only, the backend's exact query parameters, and the response
 * shape consumed verbatim — amounts are passed through untouched.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { getTransaction, getTransactions } from "../api";
import { makeTransactionRecord } from "../../test/fixtures";

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

describe("getTransactions wire contract", () => {
  it("GETs /transactions with the status and limit query parameters", async () => {
    const fetchMock = stubFetch({ transactions: [], count: 0 });

    await getTransactions({ status: "CAPTURED", limit: 20 });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(
      "http://localhost:8000/transactions?status=CAPTURED&limit=20",
    );
    expect(init.method ?? "GET").toBe("GET");
    expect(init.body).toBeUndefined();
  });

  it("omits unset filters rather than sending empty values", async () => {
    const fetchMock = stubFetch({ transactions: [], count: 0 });

    await getTransactions({ limit: 20 });

    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toBe("http://localhost:8000/transactions?limit=20");
  });

  it("returns the backend response shape verbatim — amounts untouched", async () => {
    const record = makeTransactionRecord({ paypal_amount: "16.66" });
    stubFetch({ transactions: [record], count: 1 });

    const response = await getTransactions({ limit: 20 });

    expect(response.count).toBe(1);
    expect(response.transactions[0]).toEqual(record);
    // The financial strings are the backend's own, character for character.
    expect(response.transactions[0].paypal_amount).toBe("16.66");
    expect(response.transactions[0].paypal_currency).toBe("USD");
    expect(response.transactions[0].catalog_price_inr).toBe(1499);
  });

  it("talks ONLY to the PayPilot backend, never to PayPal", async () => {
    const fetchMock = stubFetch({ transactions: [], count: 0 });

    await getTransactions({ status: "CAPTURED", limit: 20 });
    await getTransaction("txn_1");

    for (const call of fetchMock.mock.calls) {
      const url = String(call[0]);
      expect(url.startsWith("http://localhost:8000/")).toBe(true);
      expect(url).not.toMatch(/paypal\.com/i);
    }
  });
});
