/**
 * State-machine tests for the return-from-PayPal capture flow: when a
 * capture may run (only on a real approval return), that it runs exactly
 * once per page load, what it sends, and that every outcome reflects the
 * backend's answer — success is never invented.
 */

import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { usePayPalReturn } from "../usePayPalReturn";
import { ApiError } from "../../services/apiError";
import {
  makeCaptureResult,
  makeTransactionRecord,
} from "../../test/fixtures";

vi.mock("../../services/api", () => ({
  capturePurchase: vi.fn(),
  getTransaction: vi.fn(),
  getTransactionByOrder: vi.fn(),
}));

import {
  capturePurchase,
  getTransaction,
  getTransactionByOrder,
} from "../../services/api";

const capturePurchaseMock = vi.mocked(capturePurchase);
const getTransactionMock = vi.mocked(getTransaction);
const getTransactionByOrderMock = vi.mocked(getTransactionByOrder);

const ORDER_ID = "5O190127TN364715T";

function enterWithApprovalReturn(orderId = ORDER_ID) {
  window.history.replaceState(null, "", `/?token=${orderId}&PayerID=PAYER123`);
}

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  window.history.replaceState(null, "", "/");
});

describe("usePayPalReturn", () => {
  it("stays idle and makes NO capture request on a plain page load", () => {
    window.history.replaceState(null, "", "/");

    const { result } = renderHook(() => usePayPalReturn());

    expect(result.current.state.phase).toBe("idle");
    expect(capturePurchaseMock).not.toHaveBeenCalled();
  });

  it("does not capture on a cancel return (token without PayerID)", () => {
    window.history.replaceState(null, "", `/?token=${ORDER_ID}`);

    const { result } = renderHook(() => usePayPalReturn());

    expect(result.current.state.phase).toBe("idle");
    expect(capturePurchaseMock).not.toHaveBeenCalled();
  });

  it("shows the capturing state, then the backend-confirmed capture", async () => {
    enterWithApprovalReturn();
    let resolveCapture!: (value: ReturnType<typeof makeCaptureResult>) => void;
    capturePurchaseMock.mockReturnValue(
      new Promise((resolve) => {
        resolveCapture = resolve;
      }),
    );
    getTransactionMock.mockResolvedValue(makeTransactionRecord());

    const { result } = renderHook(() => usePayPalReturn());

    // Loading state first — no success claim yet.
    expect(result.current.state.phase).toBe("capturing");

    resolveCapture(makeCaptureResult());
    await waitFor(() => expect(result.current.state.phase).toBe("captured"));

    const state = result.current.state;
    if (state.phase !== "captured") throw new Error("expected captured");
    expect(state.result.capture_id).toBe("3C679366HH908993F");
    expect(state.transaction?.status).toBe("CAPTURED");
    // Ledger refreshed through the existing read endpoint.
    expect(getTransactionMock).toHaveBeenCalledWith("txn_1");
  });

  it("calls the capture endpoint exactly once, with only the order id", async () => {
    enterWithApprovalReturn();
    capturePurchaseMock.mockResolvedValue(makeCaptureResult());
    getTransactionMock.mockResolvedValue(makeTransactionRecord());

    const { result, rerender } = renderHook(() => usePayPalReturn());
    rerender();
    rerender();

    await waitFor(() => expect(result.current.state.phase).toBe("captured"));
    expect(capturePurchaseMock).toHaveBeenCalledTimes(1);
    expect(capturePurchaseMock).toHaveBeenCalledWith(ORDER_ID);
  });

  it("strips the return params so a refresh cannot re-capture", async () => {
    enterWithApprovalReturn();
    capturePurchaseMock.mockResolvedValue(makeCaptureResult());
    getTransactionMock.mockResolvedValue(makeTransactionRecord());

    const first = renderHook(() => usePayPalReturn());
    await waitFor(() => expect(first.result.current.state.phase).toBe("captured"));

    // Params are gone from the address bar immediately.
    expect(window.location.search).not.toContain("token=");
    expect(window.location.search).not.toContain("PayerID=");

    // Simulated refresh: a brand-new mount on the cleaned URL stays idle.
    first.unmount();
    const second = renderHook(() => usePayPalReturn());
    expect(second.result.current.state.phase).toBe("idle");
    expect(capturePurchaseMock).toHaveBeenCalledTimes(1);
  });

  it("shows the authoritative captured state when already captured (409)", async () => {
    enterWithApprovalReturn();
    capturePurchaseMock.mockRejectedValue(
      new ApiError({
        message: `Order ${ORDER_ID} has already been captured`,
        status: 409,
        detail: `Order ${ORDER_ID} has already been captured`,
      }),
    );
    getTransactionByOrderMock.mockResolvedValue(makeTransactionRecord());

    const { result } = renderHook(() => usePayPalReturn());

    await waitFor(() =>
      expect(result.current.state.phase).toBe("already_captured"),
    );
    const state = result.current.state;
    if (state.phase !== "already_captured") throw new Error("unexpected");
    expect(getTransactionByOrderMock).toHaveBeenCalledWith(ORDER_ID);
    expect(state.transaction?.status).toBe("CAPTURED");
  });

  it("reports not-approved (409) as an error, never success", async () => {
    enterWithApprovalReturn();
    capturePurchaseMock.mockRejectedValue(
      new ApiError({
        message: `Order ${ORDER_ID} has not been approved by the buyer yet`,
        status: 409,
        detail: `Order ${ORDER_ID} has not been approved by the buyer yet`,
      }),
    );

    const { result } = renderHook(() => usePayPalReturn());

    await waitFor(() => expect(result.current.state.phase).toBe("error"));
    const state = result.current.state;
    if (state.phase !== "error") throw new Error("unexpected");
    expect(state.reason).toBe("order_not_approved");
    expect(getTransactionByOrderMock).not.toHaveBeenCalled();
  });

  it("reports an unknown order (404) as an error", async () => {
    enterWithApprovalReturn();
    capturePurchaseMock.mockRejectedValue(
      new ApiError({ message: "No PayPal order found", status: 404 }),
    );

    const { result } = renderHook(() => usePayPalReturn());

    await waitFor(() => expect(result.current.state.phase).toBe("error"));
    const state = result.current.state;
    if (state.phase !== "error") throw new Error("unexpected");
    expect(state.reason).toBe("order_not_found");
  });

  it("reports a PayPal/upstream failure (502) without claiming success", async () => {
    enterWithApprovalReturn();
    capturePurchaseMock.mockRejectedValue(
      new ApiError({ message: "PayPal capture failed", status: 502 }),
    );

    const { result } = renderHook(() => usePayPalReturn());

    await waitFor(() => expect(result.current.state.phase).toBe("error"));
    const state = result.current.state;
    if (state.phase !== "error") throw new Error("unexpected");
    expect(state.reason).toBe("capture_failed");
  });

  it("reports a network failure distinctly", async () => {
    enterWithApprovalReturn();
    capturePurchaseMock.mockRejectedValue(ApiError.network());

    const { result } = renderHook(() => usePayPalReturn());

    await waitFor(() => expect(result.current.state.phase).toBe("error"));
    const state = result.current.state;
    if (state.phase !== "error") throw new Error("unexpected");
    expect(state.reason).toBe("network");
  });

  it("still shows success if the post-capture ledger refresh fails", async () => {
    enterWithApprovalReturn();
    capturePurchaseMock.mockResolvedValue(makeCaptureResult());
    getTransactionMock.mockRejectedValue(
      new ApiError({ message: "down", status: 500 }),
    );

    const { result } = renderHook(() => usePayPalReturn());

    await waitFor(() => expect(result.current.state.phase).toBe("captured"));
    const state = result.current.state;
    if (state.phase !== "captured") throw new Error("unexpected");
    expect(state.transaction).toBeNull();
    expect(state.result.order_id).toBe(ORDER_ID);
  });
});
