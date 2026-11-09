/**
 * POST /discovery/compare request-shape tests.
 *
 * fetch is stubbed, so no backend, discovery provider, or AI provider is
 * contacted. The point is to pin down exactly what the browser sends: a
 * query and two bounded counts, and nothing financial.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

import { COMPARISON_LIMIT, COMPARISON_TOP_K, compareProducts } from "../api";
import { ApiError } from "../apiError";
import { makeComparisonResponse } from "../../test/fixtures";

function stubFetch(body: unknown, init: { status?: number } = {}) {
  const status = init.status ?? 200;
  // Params are declared so mock.calls is typed as [url, init] rather
  // than an empty tuple.
  const fetchMock = vi.fn(
    async (_url: string | URL | Request, _init?: RequestInit) =>
      new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      }),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function sentBody(fetchMock: ReturnType<typeof stubFetch>): unknown {
  const init = fetchMock.mock.calls[0][1];
  return JSON.parse(String(init?.body));
}

beforeEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("compareProducts", () => {
  it("POSTs to /discovery/compare on the configured backend", async () => {
    const fetchMock = stubFetch(makeComparisonResponse());

    await compareProducts("wireless headphones under 3000");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://localhost:8000/discovery/compare");
    expect(init?.method).toBe("POST");
  });

  it("sends the query and the default bounded counts", async () => {
    const fetchMock = stubFetch(makeComparisonResponse());

    await compareProducts("laptop under 70000");

    expect(sentBody(fetchMock)).toEqual({
      query: "laptop under 70000",
      limit: COMPARISON_LIMIT,
      top_k: COMPARISON_TOP_K,
    });
    expect(COMPARISON_LIMIT).toBe(10);
    expect(COMPARISON_TOP_K).toBe(3);
  });

  it("sends no price, currency, rate, product id, or payment field", async () => {
    const fetchMock = stubFetch(makeComparisonResponse());

    await compareProducts("headphones");

    const body = sentBody(fetchMock) as Record<string, unknown>;
    expect(Object.keys(body).sort()).toEqual(["limit", "query", "top_k"]);
    for (const forbidden of [
      "price",
      "max_price",
      "currency",
      "conversion_rate",
      "product_id",
      "external_id",
      "paypal_order_id",
      "capture_id",
      "refund_id",
      "amount",
      "requirements",
    ]) {
      expect(body).not.toHaveProperty(forbidden);
    }
  });

  it("returns the backend response unchanged", async () => {
    const response = makeComparisonResponse();
    stubFetch(response);

    await expect(compareProducts("headphones")).resolves.toEqual(response);
  });

  it("maps 429 to a rate-limited ApiError", async () => {
    stubFetch({ detail: "rate limited" }, { status: 429 });

    await expect(compareProducts("headphones")).rejects.toMatchObject({
      status: 429,
      kind: "rate_limited",
    });
  });

  it("maps 503 to an unavailable ApiError", async () => {
    stubFetch({ detail: "not configured" }, { status: 503 });

    await expect(compareProducts("headphones")).rejects.toMatchObject({
      status: 503,
      kind: "unavailable",
    });
  });

  it("surfaces a transport failure as a network ApiError", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string | URL | Request, _init?: RequestInit) => {
        throw new TypeError("Failed to fetch");
      }),
    );

    await expect(compareProducts("headphones")).rejects.toBeInstanceOf(ApiError);
    await expect(compareProducts("headphones")).rejects.toMatchObject({
      kind: "network",
    });
  });
});
