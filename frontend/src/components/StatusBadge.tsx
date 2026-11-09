/**
 * Compact ledger-status badge. The label always names the exact backend
 * status — "Order created" never reads as paid, "Order approved" never
 * reads as captured — and status is conveyed by text, not color alone.
 */

import type { TransactionStatus } from "../types";

const STATUS_PRESENTATION: Record<
  TransactionStatus,
  { label: string; badge: string; dot: string }
> = {
  CAPTURED: {
    label: "Captured",
    badge:
      "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-900 dark:bg-emerald-950 dark:text-emerald-400",
    dot: "bg-emerald-500",
  },
  ORDER_CREATED: {
    label: "Order created",
    badge:
      "border-blue-200 bg-blue-50 text-blue-700 dark:border-blue-900 dark:bg-blue-950 dark:text-blue-400",
    dot: "bg-blue-500",
  },
  ORDER_APPROVED: {
    label: "Order approved",
    badge:
      "border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-400",
    dot: "bg-amber-500",
  },
  FAILED: {
    label: "Failed",
    badge:
      "border-red-200 bg-red-50 text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-400",
    dot: "bg-red-500",
  },
  REFUNDING: {
    // Neutral wording: a refund is being processed / awaiting confirmation.
    // Deliberately NOT "Refunded" (not yet confirmed) and NOT "Captured".
    // The status alone does not say whether this is a normal in-flight refund
    // or an unresolved (ambiguous) one, and the UI does not guess.
    label: "Refund processing",
    badge:
      "border-violet-200 bg-violet-50 text-violet-700 dark:border-violet-900 dark:bg-violet-950 dark:text-violet-400",
    dot: "bg-violet-500",
  },
  REFUNDED: {
    label: "Refunded",
    badge:
      "border-zinc-200 bg-zinc-100 text-zinc-600 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-300",
    dot: "bg-zinc-400",
  },
};

export function StatusBadge({ status }: { status: TransactionStatus }) {
  // An unexpected status from the backend is still shown verbatim rather
  // than guessed at or hidden.
  const presentation = STATUS_PRESENTATION[status] ?? {
    label: status,
    badge:
      "border-zinc-200 bg-zinc-100 text-zinc-600 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-300",
    dot: "bg-zinc-400",
  };

  return (
    <span
      className={`inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[11px] font-medium ${presentation.badge}`}
    >
      <span aria-hidden className={`h-1.5 w-1.5 rounded-full ${presentation.dot}`} />
      {presentation.label}
    </span>
  );
}
