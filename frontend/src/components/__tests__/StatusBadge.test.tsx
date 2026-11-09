/**
 * StatusBadge tests: every backend status renders a distinct, accurate label.
 * Focus of Phase 11F-K.14 — the new REFUNDING status must read as a
 * not-yet-confirmed refund, never as "Captured" or "Refunded".
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StatusBadge } from "../StatusBadge";
import type { TransactionStatus } from "../../types";

describe("StatusBadge", () => {
  it("renders REFUNDING as 'Refund processing' — not Captured, not Refunded", () => {
    render(<StatusBadge status="REFUNDING" />);
    expect(screen.getByText("Refund processing")).toBeInTheDocument();
    expect(screen.queryByText("Captured")).not.toBeInTheDocument();
    expect(screen.queryByText("Refunded")).not.toBeInTheDocument();
  });

  it("gives each known status its own distinct label", () => {
    const cases: Array<[TransactionStatus, string]> = [
      ["ORDER_CREATED", "Order created"],
      ["ORDER_APPROVED", "Order approved"],
      ["CAPTURED", "Captured"],
      ["FAILED", "Failed"],
      ["REFUNDING", "Refund processing"],
      ["REFUNDED", "Refunded"],
    ];
    for (const [status, label] of cases) {
      const { unmount } = render(<StatusBadge status={status} />);
      expect(screen.getByText(label)).toBeInTheDocument();
      unmount();
    }
  });
});
