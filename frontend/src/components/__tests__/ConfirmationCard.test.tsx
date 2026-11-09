import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ConfirmationCard, type ConfirmationCardProps } from "../ConfirmationCard";
import {
  makeConfirmation,
  makeExecutionResult,
  makePendingAction,
  makePlan,
} from "../../test/fixtures";

function renderCard(overrides: Partial<ConfirmationCardProps> = {}) {
  const props: ConfirmationCardProps = {
    pendingAction: null,
    plan: null,
    confirming: false,
    confirmation: null,
    notice: null,
    error: null,
    onConfirm: vi.fn(),
    onCancel: vi.fn(),
    ...overrides,
  };
  return { ...render(<ConfirmationCard {...props} />), props };
}

describe("ConfirmationCard", () => {
  it("renders the confirmation card for an active PURCHASE pending action", () => {
    renderCard({ pendingAction: makePendingAction(), plan: makePlan() });

    expect(screen.getByText("Purchase ready for confirmation")).toBeInTheDocument();
    expect(screen.getByText("boAt Rockerz 450")).toBeInTheDocument();
    expect(screen.getByText("₹1,499")).toBeInTheDocument();
    expect(screen.getByText(/autonomous purchasing was not authorized/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Confirm purchase" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeEnabled();
  });

  it("renders nothing when there is no pending action, result, or error", () => {
    const { container } = renderCard();
    expect(container).toBeEmptyDOMElement();
  });

  it("does not render for a REFUND pending action", () => {
    const { container } = renderCard({
      pendingAction: makePendingAction({ action_type: "REFUND" }),
    });
    expect(container).toBeEmptyDOMElement();
  });

  it("invokes onConfirm / onCancel from the buttons", async () => {
    const user = userEvent.setup();
    const { props } = renderCard({
      pendingAction: makePendingAction(),
      plan: makePlan(),
    });

    await user.click(screen.getByRole("button", { name: "Confirm purchase" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(props.onConfirm).toHaveBeenCalledTimes(1);
    expect(props.onCancel).toHaveBeenCalledTimes(1);
  });

  it("disables both buttons while confirming", () => {
    renderCard({
      pendingAction: makePendingAction(),
      plan: makePlan(),
      confirming: true,
    });

    expect(screen.getByRole("button", { name: "Confirming…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
  });

  it("renders the order-created state after a successful confirmation", () => {
    renderCard({ confirmation: makeConfirmation() });

    expect(screen.getByText("PayPal order created")).toBeInTheDocument();
    // Appears in both the backend message and the details list.
    expect(screen.getAllByText(/boAt Rockerz 450/).length).toBeGreaterThan(0);
    expect(screen.getByText("5O190127TN364715T")).toBeInTheDocument();
    // No confirm button remains.
    expect(screen.queryByRole("button", { name: "Confirm purchase" })).toBeNull();
  });

  it("renders Continue to PayPal only with a backend-provided approval URL", () => {
    const { unmount } = renderCard({ confirmation: makeConfirmation() });
    const link = screen.getByRole("link", { name: "Continue to PayPal" });
    expect(link).toHaveAttribute(
      "href",
      "https://www.sandbox.paypal.com/checkoutnow?token=5O190127TN364715T",
    );
    unmount();

    renderCard({
      confirmation: makeConfirmation({
        purchase_result: makeExecutionResult({ approve_url: null }),
      }),
    });
    expect(screen.queryByRole("link", { name: "Continue to PayPal" })).toBeNull();
    expect(screen.getByText(/awaits PayPal approval/)).toBeInTheDocument();
  });

  it("renders a not-executed outcome without pretending success", () => {
    renderCard({
      confirmation: makeConfirmation({
        executed: false,
        message: "The purchase was denied by policy at confirmation time.",
        purchase_result: makeExecutionResult({
          status: "denied",
          paypal_order_id: null,
          approve_url: null,
        }),
      }),
    });

    expect(screen.getByText("Purchase not executed")).toBeInTheDocument();
    expect(screen.getByText(/denied by policy/)).toBeInTheDocument();
    expect(screen.queryByText("PayPal order created")).toBeNull();
  });

  it("shows confirmation errors", () => {
    renderCard({
      pendingAction: makePendingAction(),
      plan: makePlan(),
      error: "The confirmation couldn't be completed. Nothing was charged — you can try again.",
    });

    expect(screen.getByRole("alert")).toHaveTextContent("Nothing was charged");
    // Buttons stay available for a retry.
    expect(screen.getByRole("button", { name: "Confirm purchase" })).toBeEnabled();
  });

  it("shows a standalone error when the action is gone", () => {
    renderCard({
      error:
        "This purchase request is no longer active. Ask PayPilot to create a new purchase proposal.",
    });
    expect(screen.getByRole("alert")).toHaveTextContent("no longer active");
    expect(screen.queryByRole("button", { name: "Confirm purchase" })).toBeNull();
  });

  it("shows the expired state without a Confirm button", () => {
    renderCard({
      pendingAction: makePendingAction({
        expires_at: new Date(Date.now() - 60_000).toISOString(),
      }),
      plan: makePlan(),
    });

    expect(
      screen.getByText(/Purchase request expired\. Please ask PayPilot to create a new purchase proposal\./),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Confirm purchase" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
  });
});
