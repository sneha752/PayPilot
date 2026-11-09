/**
 * Transaction history state tests: fetching through the centralized API
 * only, backend-verbatim records, explicit error state (never a silent
 * empty list), retry, backend-side filtering, and refresh-after-capture.
 */

import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { HISTORY_LIMIT, useTransactions } from "../useTransactions";
import { ApiError } from "../../services/apiError";
import { makeTransactionRecord } from "../../test/fixtures";
import type { TransactionListResponse } from "../../types";

vi.mock("../../services/api", () => ({
  getTransactions: vi.fn(),
}));

import { getTransactions } from "../../services/api";

const getTransactionsMock = vi.mocked(getTransactions);

function listResponse(
  transactions = [makeTransactionRecord()],
): TransactionListResponse {
  return { transactions, count: transactions.length };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("useTransactions", () => {
  it("makes no request while disabled (panel closed)", () => {
    renderHook(() => useTransactions({ enabled: false }));
    expect(getTransactionsMock).not.toHaveBeenCalled();
  });

  it("loads with the default limit and stores backend records verbatim", async () => {
    const record = makeTransactionRecord();
    getTransactionsMock.mockResolvedValue(listResponse([record]));

    const { result } = renderHook(() => useTransactions({ enabled: true }));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(getTransactionsMock).toHaveBeenCalledWith(
      { status: undefined, limit: HISTORY_LIMIT },
      expect.any(AbortSignal),
    );
    // Exactly the backend's records — nothing invented, altered, reordered.
    expect(result.current.transactions).toEqual([record]);
    expect(result.current.transactions[0].paypal_amount).toBe(
      record.paypal_amount,
    );
    expect(result.current.error).toBeNull();
  });

  it("shows an explicit error (not an empty list) when the API fails", async () => {
    getTransactionsMock.mockRejectedValue(
      new ApiError({ message: "boom", status: 500 }),
    );

    const { result } = renderHook(() => useTransactions({ enabled: true }));

    await waitFor(() =>
      expect(result.current.error).toBe(
        "Transaction history couldn't be loaded.",
      ),
    );
    expect(result.current.loading).toBe(false);
  });

  it("retries via reload() after a failure", async () => {
    getTransactionsMock
      .mockRejectedValueOnce(new ApiError({ message: "boom", status: 502 }))
      .mockResolvedValueOnce(listResponse());

    const { result } = renderHook(() => useTransactions({ enabled: true }));
    await waitFor(() => expect(result.current.error).not.toBeNull());

    act(() => result.current.reload());

    await waitFor(() => expect(result.current.transactions).toHaveLength(1));
    expect(result.current.error).toBeNull();
    expect(getTransactionsMock).toHaveBeenCalledTimes(2);
  });

  it("passes the status filter to the backend (no client-side filtering)", async () => {
    getTransactionsMock.mockResolvedValue(listResponse());
    const { result } = renderHook(() => useTransactions({ enabled: true }));
    await waitFor(() => expect(result.current.loading).toBe(false));

    act(() => result.current.setStatusFilter("CAPTURED"));

    await waitFor(() =>
      expect(getTransactionsMock).toHaveBeenLastCalledWith(
        { status: "CAPTURED", limit: HISTORY_LIMIT },
        expect.any(AbortSignal),
      ),
    );
  });

  it("refetches when refreshToken bumps, surfacing a newly captured record", async () => {
    const before = makeTransactionRecord({
      transaction_id: "txn_old",
      status: "ORDER_CREATED",
      paypal_capture_id: null,
    });
    const after = makeTransactionRecord({
      transaction_id: "txn_old",
      status: "CAPTURED",
    });
    getTransactionsMock
      .mockResolvedValueOnce(listResponse([before]))
      .mockResolvedValueOnce(listResponse([after]));

    const { result, rerender } = renderHook(
      ({ token }: { token: number }) =>
        useTransactions({ enabled: true, refreshToken: token }),
      { initialProps: { token: 0 } },
    );
    await waitFor(() =>
      expect(result.current.transactions[0]?.status).toBe("ORDER_CREATED"),
    );

    rerender({ token: 1 }); // capture completed → app bumps the token

    await waitFor(() =>
      expect(result.current.transactions[0]?.status).toBe("CAPTURED"),
    );
    // The CAPTURED record came from GET /transactions, not local invention.
    expect(getTransactionsMock).toHaveBeenCalledTimes(2);
  });
});
