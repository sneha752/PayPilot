/**
 * Wire-contract tests for the confirmation call: the browser must send
 * EXACTLY { confirm: boolean } — never a price, amount, currency, or any
 * PayPal identifier.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { confirmSession } from "../api";
import { makeConfirmation } from "../../test/fixtures";

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

describe("confirmSession wire contract", () => {
  it("POSTs to /sessions/{id}/confirm with exactly { confirm: true }", async () => {
    const fetchMock = stubFetch(makeConfirmation());

    await confirmSession("sess_abc", true);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/sessions/sess_abc/confirm");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ confirm: true });
  });

  it("sends { confirm: false } for an explicit decline", async () => {
    const fetchMock = stubFetch(
      makeConfirmation({ confirmed: false, executed: false }),
    );

    await confirmSession("sess_abc", false);

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ confirm: false });
  });

  it("never includes client-controlled financial fields in the body", async () => {
    const fetchMock = stubFetch(makeConfirmation());

    await confirmSession("sess_abc", true);

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(Object.keys(body)).toEqual(["confirm"]);
    for (const forbidden of [
      "amount",
      "currency",
      "price",
      "price_inr",
      "catalog_price_inr",
      "paypal_order_id",
      "paypal_capture_id",
      "capture_id",
      "refund_id",
      "product_id",
    ]) {
      expect(body).not.toHaveProperty(forbidden);
    }
  });
});
