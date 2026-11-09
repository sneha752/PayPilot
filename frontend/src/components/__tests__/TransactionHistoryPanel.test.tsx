/**
 * Transaction history drawer tests: list rendering from backend data,
 * status badges, empty/loading/error+retry states, detail view fetched
 * through GET /transactions/{id}, and amounts displayed exactly as the
 * backend returned them (never recomputed or converted).
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { TransactionHistoryPanel } from "../TransactionHistoryPanel";
import { ApiError } from "../../services/apiError";
import { makeTransactionRecord } from "../../test/fixtures";
import type { TransactionListResponse, TransactionRecord } from "../../types";

vi.mock("../../services/api", () => ({
  getTransactions: vi.fn(),
  getTransaction: vi.fn(),
}));

import { getTransaction, getTransactions } from "../../services/api";

const getTransactionsMock = vi.mocked(getTransactions);
const getTransactionMock = vi.mocked(getTransaction);

function listResponse(transactions: TransactionRecord[]): TransactionListResponse {
  return { transactions, count: transactions.length };
}

function renderPanel(props: Partial<Parameters<typeof TransactionHistoryPanel>[0]> = {}) {
  return render(
    <TransactionHistoryPanel
      open
      onClose={vi.fn()}
      refreshToken={0}
      {...props}
    />,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("TransactionHistoryPanel — list", () => {
  it("loads and renders backend transaction data verbatim", async () => {
    getTransactionsMock.mockResolvedValue(listResponse([makeTransactionRecord()]));

    renderPanel();

    expect(
      await screen.findByText("boAt Rockerz 450"),
    ).toBeInTheDocument();
    // PayPal amount shown exactly as the backend string — no conversion,
    // no arithmetic, no reformatting of the value itself.
    expect(screen.getByText("18.07 USD")).toBeInTheDocument();
    // Catalog INR comes from the backend integer, display-formatted only.
    expect(screen.getByText(/1,499/)).toBeInTheDocument();
    expect(screen.getByText("txn_1")).toBeInTheDocument();
    expect(getTransactionsMock).toHaveBeenCalledTimes(1);
  });

  it("renders the correct status badge per backend status", async () => {
    getTransactionsMock.mockResolvedValue(
      listResponse([
        makeTransactionRecord({ transaction_id: "t1", status: "CAPTURED" }),
        makeTransactionRecord({ transaction_id: "t2", status: "ORDER_CREATED" }),
        makeTransactionRecord({ transaction_id: "t3", status: "ORDER_APPROVED" }),
        makeTransactionRecord({ transaction_id: "t4", status: "FAILED" }),
        makeTransactionRecord({ transaction_id: "t5", status: "REFUNDED" }),
      ]),
    );

    renderPanel();

    // Scope to the list so the filter <option> labels don't match too.
    const list = await screen.findByRole("list");
    expect(within(list).getByText("Captured")).toBeInTheDocument();
    expect(within(list).getByText("Order created")).toBeInTheDocument();
    expect(within(list).getByText("Order approved")).toBeInTheDocument();
    expect(within(list).getByText("Failed")).toBeInTheDocument();
    expect(within(list).getByText("Refunded")).toBeInTheDocument();
  });

  it("shows only backend records — an empty backend list means an empty UI", async () => {
    getTransactionsMock.mockResolvedValue(listResponse([]));

    renderPanel();

    expect(await screen.findByText("No transactions yet")).toBeInTheDocument();
    expect(
      screen.getByText("Completed purchases will appear here."),
    ).toBeInTheDocument();
    // No invented/demo rows.
    expect(screen.queryByText(/txn_/)).not.toBeInTheDocument();
  });

  it("shows a loading skeleton while the request is in flight", () => {
    getTransactionsMock.mockReturnValue(new Promise(() => {}));

    renderPanel();

    expect(
      screen.getByRole("status", { name: "Loading transaction history" }),
    ).toBeInTheDocument();
  });

  it("shows a localized error with Retry on failure, then recovers", async () => {
    getTransactionsMock
      .mockRejectedValueOnce(new ApiError({ message: "down", status: 502 }))
      .mockResolvedValueOnce(listResponse([makeTransactionRecord()]));

    renderPanel();

    expect(
      await screen.findByText("Transaction history couldn't be loaded."),
    ).toBeInTheDocument();
    // A failed API call is never shown as an empty list.
    expect(screen.queryByText("No transactions yet")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByText("boAt Rockerz 450")).toBeInTheDocument();
  });

  it("refetches when refreshToken bumps so a new capture appears", async () => {
    getTransactionsMock
      .mockResolvedValueOnce(listResponse([]))
      .mockResolvedValueOnce(
        listResponse([makeTransactionRecord({ status: "CAPTURED" })]),
      );

    const { rerender } = renderPanel({ refreshToken: 0 });
    expect(await screen.findByText("No transactions yet")).toBeInTheDocument();

    rerender(
      <TransactionHistoryPanel open onClose={vi.fn()} refreshToken={1} />,
    );

    const list = await screen.findByRole("list");
    expect(within(list).getByText("Captured")).toBeInTheDocument();
    expect(getTransactionsMock).toHaveBeenCalledTimes(2);
  });

  it("requests the backend filter when a status is selected", async () => {
    getTransactionsMock.mockResolvedValue(listResponse([]));

    renderPanel();
    await screen.findByText("No transactions yet");

    await userEvent.selectOptions(screen.getByRole("combobox"), "CAPTURED");

    await waitFor(() =>
      expect(getTransactionsMock).toHaveBeenLastCalledWith(
        expect.objectContaining({ status: "CAPTURED" }),
        expect.any(AbortSignal),
      ),
    );
  });
});

describe("TransactionHistoryPanel — detail", () => {
  it("clicking a transaction fetches the authoritative record and renders it", async () => {
    getTransactionsMock.mockResolvedValue(listResponse([makeTransactionRecord()]));
    getTransactionMock.mockResolvedValue(makeTransactionRecord());

    renderPanel();

    await userEvent.click(
      await screen.findByRole("button", {
        name: "View details for boAt Rockerz 450",
      }),
    );

    // Detail comes from GET /transactions/{id}, never from the list alone.
    expect(getTransactionMock).toHaveBeenCalledWith(
      "txn_1",
      expect.any(AbortSignal),
    );
    expect(await screen.findByText("PayPal order ID")).toBeInTheDocument();
    expect(screen.getByText("5O190127TN364715T")).toBeInTheDocument();
    expect(screen.getByText("3C679366HH908993F")).toBeInTheDocument(); // capture id
    expect(screen.getByText("CAPTURED")).toBeInTheDocument(); // explicit status
    expect(screen.getByText("18.07 USD")).toBeInTheDocument(); // verbatim amount
    expect(screen.getByText("Created")).toBeInTheDocument();
    expect(screen.getByText("Updated")).toBeInTheDocument();
  });

  it("returns to the list via the back button", async () => {
    getTransactionsMock.mockResolvedValue(listResponse([makeTransactionRecord()]));
    getTransactionMock.mockResolvedValue(makeTransactionRecord());

    renderPanel();
    await userEvent.click(
      await screen.findByRole("button", {
        name: "View details for boAt Rockerz 450",
      }),
    );
    await screen.findByText("PayPal order ID");

    await userEvent.click(screen.getByRole("button", { name: /Back to history/ }));

    expect(await screen.findByRole("combobox")).toBeInTheDocument();
  });

  it("a REFUNDING transaction shows a processing note and offers no refund action", async () => {
    getTransactionsMock.mockResolvedValue(
      listResponse([makeTransactionRecord({ status: "REFUNDING" })]),
    );
    getTransactionMock.mockResolvedValue(
      makeTransactionRecord({ status: "REFUNDING" }),
    );

    renderPanel();
    await userEvent.click(
      await screen.findByRole("button", {
        name: "View details for boAt Rockerz 450",
      }),
    );
    await screen.findByText("PayPal order ID");

    // Badge reads "Refund processing"; the explicit Status row shows the raw
    // backend status — never "Captured"/"CAPTURED" or "Refunded"/"REFUNDED".
    expect(screen.getByText("Refund processing")).toBeInTheDocument();
    expect(screen.getByText("REFUNDING")).toBeInTheDocument();
    expect(screen.queryByText("Captured")).not.toBeInTheDocument();
    expect(screen.queryByText("Refunded")).not.toBeInTheDocument();

    // Informational note — not a success claim, no cause inferred.
    expect(
      screen.getByText(/being processed and is not yet confirmed/i),
    ).toBeInTheDocument();

    // The refund action is unavailable while REFUNDING.
    expect(
      screen.queryByRole("button", { name: "Refund transaction" }),
    ).not.toBeInTheDocument();
  });

  it("offers the refund action for a CAPTURED transaction but not a REFUNDING one", async () => {
    getTransactionsMock.mockResolvedValue(
      listResponse([makeTransactionRecord({ status: "CAPTURED" })]),
    );
    getTransactionMock.mockResolvedValue(
      makeTransactionRecord({ status: "CAPTURED" }),
    );

    renderPanel();
    await userEvent.click(
      await screen.findByRole("button", {
        name: "View details for boAt Rockerz 450",
      }),
    );
    await screen.findByText("PayPal order ID");

    expect(
      screen.getByRole("button", { name: "Refund transaction" }),
    ).toBeInTheDocument();
  });

  it("shows a detail error with Retry instead of partial data", async () => {
    getTransactionsMock.mockResolvedValue(listResponse([makeTransactionRecord()]));
    getTransactionMock
      .mockRejectedValueOnce(new ApiError({ message: "down", status: 500 }))
      .mockResolvedValueOnce(makeTransactionRecord());

    renderPanel();
    await userEvent.click(
      await screen.findByRole("button", {
        name: "View details for boAt Rockerz 450",
      }),
    );

    expect(
      await screen.findByText("Transaction details couldn't be loaded."),
    ).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByText("PayPal order ID")).toBeInTheDocument();
  });
});

describe("TransactionHistoryPanel — isolation", () => {
  it("talks only to the API service — never fetches PayPal directly", async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    getTransactionsMock.mockResolvedValue(listResponse([makeTransactionRecord()]));
    getTransactionMock.mockResolvedValue(makeTransactionRecord());

    renderPanel();
    await userEvent.click(
      await screen.findByRole("button", {
        name: "View details for boAt Rockerz 450",
      }),
    );
    await screen.findByText("PayPal order ID");

    // With the API service mocked, no raw fetch (to PayPal or anywhere
    // else) may originate from the history UI.
    expect(fetchSpy).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("closes via the labeled close button", async () => {
    getTransactionsMock.mockResolvedValue(listResponse([]));
    const onClose = vi.fn();

    renderPanel({ onClose });
    await userEvent.click(
      screen.getByRole("button", { name: "Close transaction history" }),
    );

    expect(onClose).toHaveBeenCalled();
  });
});
