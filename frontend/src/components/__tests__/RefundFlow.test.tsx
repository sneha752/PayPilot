/**
 * Refund-flow tests (Phase 9 Step 6): eligibility strictly from the
 * backend status, explicit confirmation before any request, duplicate
 * protection, authoritative refresh after the backend answers, and no
 * success claims the backend didn't make.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { TransactionHistoryPanel } from "../TransactionHistoryPanel";
import { ApiError } from "../../services/apiError";
import {
  makeRefundResult,
  makeTransactionRecord,
} from "../../test/fixtures";
import type {
  TransactionListResponse,
  TransactionRecord,
  TransactionStatus,
} from "../../types";

vi.mock("../../services/api", () => ({
  getTransactions: vi.fn(),
  getTransaction: vi.fn(),
  refundTransaction: vi.fn(),
}));

import {
  getTransaction,
  getTransactions,
  refundTransaction,
} from "../../services/api";

const getTransactionsMock = vi.mocked(getTransactions);
const getTransactionMock = vi.mocked(getTransaction);
const refundTransactionMock = vi.mocked(refundTransaction);

function listResponse(transactions: TransactionRecord[]): TransactionListResponse {
  return { transactions, count: transactions.length };
}

/** Render the panel and open the detail view for one record. */
async function openDetail(record: TransactionRecord) {
  getTransactionsMock.mockResolvedValue(listResponse([record]));
  getTransactionMock.mockResolvedValue(record);

  render(<TransactionHistoryPanel open onClose={vi.fn()} refreshToken={0} />);

  await userEvent.click(
    await screen.findByRole("button", {
      name: `View details for ${record.product_name}`,
    }),
  );
  await screen.findByText("PayPal order ID");
}

const refundButton = () =>
  screen.queryByRole("button", { name: "Refund transaction" });

beforeEach(() => {
  vi.clearAllMocks();
});

describe("refund eligibility", () => {
  it("shows the refund action only for CAPTURED", async () => {
    await openDetail(makeTransactionRecord({ status: "CAPTURED" }));
    expect(refundButton()).toBeInTheDocument();
  });

  it.each<TransactionStatus>(["REFUNDED", "ORDER_CREATED", "ORDER_APPROVED", "FAILED"])(
    "shows no refund action for %s",
    async (status) => {
      await openDetail(
        makeTransactionRecord({
          status,
          paypal_capture_id: status === "REFUNDED" ? "3C679366HH908993F" : null,
        }),
      );
      expect(refundButton()).not.toBeInTheDocument();
      // Detail remains viewable with its explicit status.
      expect(screen.getByText(status)).toBeInTheDocument();
    },
  );
});

describe("refund confirmation", () => {
  it("clicking refund opens confirmation without calling the backend", async () => {
    await openDetail(makeTransactionRecord({ status: "CAPTURED" }));

    await userEvent.click(refundButton()!);

    expect(screen.getByText("Refund this transaction?")).toBeInTheDocument();
    expect(
      screen.getByText("This will issue a full refund for this captured payment."),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Confirm full refund" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
    // No amount input of any kind.
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("spinbutton")).not.toBeInTheDocument();
    expect(refundTransactionMock).not.toHaveBeenCalled();
  });

  it("cancelling closes the confirmation and never calls the backend", async () => {
    await openDetail(makeTransactionRecord({ status: "CAPTURED" }));
    await userEvent.click(refundButton()!);

    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByText("Refund this transaction?")).not.toBeInTheDocument();
    expect(refundButton()).toBeInTheDocument();
    expect(refundTransactionMock).not.toHaveBeenCalled();
  });
});

describe("refund execution", () => {
  it("shows a processing state and prevents duplicate requests", async () => {
    await openDetail(makeTransactionRecord({ status: "CAPTURED" }));
    refundTransactionMock.mockReturnValue(new Promise(() => {}));
    await userEvent.click(refundButton()!);

    const confirmButton = screen.getByRole("button", {
      name: "Confirm full refund",
    });
    await userEvent.click(confirmButton);

    expect(screen.getByText("Processing refund…")).toBeInTheDocument();
    expect(confirmButton).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    // No premature success, no optimistic REFUNDED.
    expect(screen.queryByText("Refund completed")).not.toBeInTheDocument();

    await userEvent.click(confirmButton); // ignored while disabled
    expect(refundTransactionMock).toHaveBeenCalledTimes(1);
    expect(refundTransactionMock).toHaveBeenCalledWith("txn_1");
  });

  it("on success shows backend refund data and the refreshed REFUNDED state", async () => {
    const captured = makeTransactionRecord({ status: "CAPTURED" });
    const refunded = makeTransactionRecord({ status: "REFUNDED" });
    await openDetail(captured);
    // The post-refund refetch returns the authoritative REFUNDED record.
    getTransactionMock.mockResolvedValue(refunded);
    refundTransactionMock.mockResolvedValue(makeRefundResult());

    await userEvent.click(refundButton()!);
    await userEvent.click(screen.getByRole("button", { name: "Confirm full refund" }));

    expect(await screen.findByText("Refund completed")).toBeInTheDocument();
    // Backend amount, verbatim, in both the detail row and the refund box.
    expect(screen.getAllByText("18.07 USD")).toHaveLength(2);
    expect(screen.getByText("1JU08902781691411")).toBeInTheDocument(); // refund id
    // Status comes from the re-fetched record, not a local mutation.
    await waitFor(() => expect(screen.getByText("REFUNDED")).toBeInTheDocument());
    expect(getTransactionMock).toHaveBeenCalledTimes(2);
    expect(refundButton()).not.toBeInTheDocument();
  });

  it("refreshes the transaction history list after a successful refund", async () => {
    await openDetail(makeTransactionRecord({ status: "CAPTURED" }));
    getTransactionMock.mockResolvedValue(
      makeTransactionRecord({ status: "REFUNDED" }),
    );
    refundTransactionMock.mockResolvedValue(makeRefundResult());
    expect(getTransactionsMock).toHaveBeenCalledTimes(1);

    await userEvent.click(refundButton()!);
    await userEvent.click(screen.getByRole("button", { name: "Confirm full refund" }));
    await screen.findByText("Refund completed");

    // The list refetches from GET /transactions — no row is invented.
    await waitFor(() => expect(getTransactionsMock).toHaveBeenCalledTimes(2));
  });
});

describe("refund failures", () => {
  async function attemptRefund(cause: unknown, refreshed?: TransactionRecord) {
    await openDetail(makeTransactionRecord({ status: "CAPTURED" }));
    if (refreshed) getTransactionMock.mockResolvedValue(refreshed);
    refundTransactionMock.mockRejectedValue(cause);

    await userEvent.click(refundButton()!);
    await userEvent.click(screen.getByRole("button", { name: "Confirm full refund" }));
  }

  it("handles 409 (already refunded / not refundable) with a refresh", async () => {
    await attemptRefund(
      new ApiError({
        message: "Transaction txn_1 has already been refunded",
        status: 409,
      }),
      makeTransactionRecord({ status: "REFUNDED" }),
    );

    expect(
      await screen.findByText(/couldn't be refunded/),
    ).toBeInTheDocument();
    expect(screen.queryByText("Refund completed")).not.toBeInTheDocument();
    // Authoritative state was re-read and shows REFUNDED.
    await waitFor(() => expect(screen.getByText("REFUNDED")).toBeInTheDocument());
    expect(getTransactionMock).toHaveBeenCalledTimes(2);
  });

  it("handles a 5xx/PayPal failure — still CAPTURED after refresh", async () => {
    await attemptRefund(
      new ApiError({ message: "PayPal refund failed", status: 502 }),
      makeTransactionRecord({ status: "CAPTURED" }),
    );

    expect(
      await screen.findByText(/refund could not be completed/i),
    ).toBeInTheDocument();
    expect(screen.queryByText("Refund completed")).not.toBeInTheDocument();
    // Backend left the ledger CAPTURED; the refreshed detail shows exactly that.
    await waitFor(() => expect(screen.getByText("CAPTURED")).toBeInTheDocument());
    expect(getTransactionMock).toHaveBeenCalledTimes(2);
  });

  it("handles a network failure without claiming any outcome", async () => {
    await attemptRefund(ApiError.network());

    expect(
      await screen.findByText(/couldn't reach the server/),
    ).toBeInTheDocument();
    expect(screen.queryByText("Refund completed")).not.toBeInTheDocument();
    // No server answer → no extra refetch pretending to know the state.
    expect(getTransactionMock).toHaveBeenCalledTimes(1);
  });

  it("handles 404 (transaction not found)", async () => {
    await attemptRefund(
      new ApiError({ message: "No transaction found", status: 404 }),
      makeTransactionRecord({ status: "CAPTURED" }),
    );

    expect(
      await screen.findByText(/no longer exists on the server/),
    ).toBeInTheDocument();
    expect(screen.queryByText("Refund completed")).not.toBeInTheDocument();
  });
});

describe("isolation", () => {
  it("makes no direct PayPal (or any raw fetch) request during a refund", async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);

    await openDetail(makeTransactionRecord({ status: "CAPTURED" }));
    getTransactionMock.mockResolvedValue(
      makeTransactionRecord({ status: "REFUNDED" }),
    );
    refundTransactionMock.mockResolvedValue(makeRefundResult());

    await userEvent.click(refundButton()!);
    await userEvent.click(screen.getByRole("button", { name: "Confirm full refund" }));
    await screen.findByText("Refund completed");

    expect(fetchSpy).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});
