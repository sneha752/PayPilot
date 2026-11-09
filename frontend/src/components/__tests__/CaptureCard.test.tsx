/**
 * Rendering tests for the capture card: the in-flight state never claims
 * success, the success/already-captured states show backend-authoritative
 * values, and failures are explicit.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { CaptureCard } from "../CaptureCard";
import type { PayPalReturnState } from "../../hooks/usePayPalReturn";
import {
  makeCaptureResult,
  makeTransactionRecord,
} from "../../test/fixtures";

const ORDER_ID = "5O190127TN364715T";

function renderCard(state: PayPalReturnState) {
  return render(<CaptureCard state={state} onDismiss={vi.fn()} />);
}

describe("CaptureCard", () => {
  it("renders nothing when idle", () => {
    const { container } = renderCard({ phase: "idle" });
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the approved/completing state without claiming success", () => {
    renderCard({ phase: "capturing", orderId: ORDER_ID });

    expect(
      screen.getByText(/Payment approved — completing your purchase/),
    ).toBeInTheDocument();
    expect(screen.getByText(ORDER_ID)).toBeInTheDocument();
    expect(screen.queryByText(/captured successfully/i)).not.toBeInTheDocument();
  });

  it("shows the backend-confirmed capture with authoritative fields", () => {
    renderCard({
      phase: "captured",
      orderId: ORDER_ID,
      result: makeCaptureResult(),
      transaction: makeTransactionRecord(),
    });

    expect(
      screen.getByText("Payment captured successfully"),
    ).toBeInTheDocument();
    expect(screen.getByText("boAt Rockerz 450")).toBeInTheDocument(); // product
    expect(screen.getByText(/1,499/)).toBeInTheDocument(); // catalog INR
    expect(screen.getByText("18.07 USD")).toBeInTheDocument(); // PayPal amount
    expect(screen.getByText("txn_1")).toBeInTheDocument(); // transaction id
    expect(screen.getByText(ORDER_ID)).toBeInTheDocument(); // order id
    expect(screen.getByText("3C679366HH908993F")).toBeInTheDocument(); // capture
    expect(screen.getByText("CAPTURED")).toBeInTheDocument(); // status
  });

  it("shows success from the capture result alone when no ledger record", () => {
    renderCard({
      phase: "captured",
      orderId: ORDER_ID,
      result: makeCaptureResult({ transaction_id: null, transaction_status: null }),
      transaction: null,
    });

    expect(
      screen.getByText("Payment captured successfully"),
    ).toBeInTheDocument();
    expect(screen.getByText(ORDER_ID)).toBeInTheDocument();
    // Status falls back to the only honest claim: the capture itself.
    expect(screen.getByText("CAPTURED")).toBeInTheDocument();
  });

  it("shows the authoritative state for an already-captured order", () => {
    renderCard({
      phase: "already_captured",
      orderId: ORDER_ID,
      transaction: makeTransactionRecord(),
    });

    expect(screen.getByText("Payment already captured")).toBeInTheDocument();
    expect(screen.getByText(/No new charge was made/)).toBeInTheDocument();
    expect(screen.getByText("CAPTURED")).toBeInTheDocument();
    expect(
      screen.queryByText("Payment captured successfully"),
    ).not.toBeInTheDocument();
  });

  it("shows a failure without any success language", () => {
    renderCard({
      phase: "error",
      orderId: ORDER_ID,
      reason: "capture_failed",
      message: "The payment could not be captured.",
    });

    expect(screen.getByRole("alert")).toHaveTextContent(
      /Payment capture failed/,
    );
    expect(
      screen.getByText("The payment could not be captured."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/successfully/i)).not.toBeInTheDocument();
  });
});
