/**
 * Transaction history drawer (Phase 9 Step 5).
 *
 * A compact activity panel over the chat (which stays the primary
 * experience). Every value displayed is backend-authoritative: amounts,
 * currencies, statuses, and timestamps come verbatim from the ledger
 * endpoints — nothing is computed, converted, or inferred here.
 *
 * List:   GET /transactions?status=&limit=20 (via useTransactions)
 * Detail: GET /transactions/{transaction_id} on row click — the list
 *         response is never assumed to carry every detail field.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { getTransaction, refundTransaction } from "../services/api";
import { ApiError } from "../services/apiError";
import { formatDateTime, inrFormatter } from "../lib/format";
import { useTransactions } from "../hooks/useTransactions";
import { StatusBadge } from "./StatusBadge";
import type {
  RefundResult,
  TransactionRecord,
  TransactionStatus,
} from "../types";

export interface TransactionHistoryPanelProps {
  open: boolean;
  onClose: () => void;
  /** Bumped after a successful capture so the new record appears. */
  refreshToken: number;
}

const FILTER_OPTIONS: Array<{ value: TransactionStatus | ""; label: string }> = [
  { value: "", label: "All statuses" },
  { value: "CAPTURED", label: "Captured" },
  { value: "ORDER_CREATED", label: "Order created" },
  { value: "ORDER_APPROVED", label: "Order approved" },
  { value: "FAILED", label: "Failed" },
  { value: "REFUNDING", label: "Refund processing" },
  { value: "REFUNDED", label: "Refunded" },
];

// ---------------------------------------------------------------------------
// Detail view — fetches the authoritative record itself.
// ---------------------------------------------------------------------------

function DetailRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-0.5 py-2">
      <dt className="text-[11px] font-medium uppercase tracking-wide text-zinc-400 dark:text-zinc-500">
        {label}
      </dt>
      <dd className="break-all text-sm text-zinc-800 dark:text-zinc-200">
        {value}
      </dd>
    </div>
  );
}

/**
 * Refund flow (Phase 9 Step 6): full refunds only, explicitly confirmed,
 * executed exclusively by POST /transactions/{id}/refund — the frontend
 * never supplies an amount, currency, or any PayPal identifier.
 */
type RefundFlow =
  | { kind: "idle" }
  | { kind: "confirming" }
  | { kind: "processing" }
  | { kind: "success"; result: RefundResult }
  | { kind: "error"; message: string };

function refundErrorMessage(cause: unknown): string {
  if (cause instanceof ApiError) {
    switch (cause.kind) {
      case "conflict":
        // Already refunded or not refundable — the backend refused and no
        // (second) refund happened. The refreshed status below is the truth.
        return "This transaction couldn't be refunded — it may already be refunded or not be in a refundable state. The status shown has been refreshed from the server.";
      case "not_found":
        return "This transaction no longer exists on the server. No refund was made.";
      case "network":
        return "PayPilot couldn't reach the server, so the refund was not confirmed. Check the transaction status before trying again.";
      default:
        return "The refund could not be completed. No refund was recorded — the transaction keeps its current status.";
    }
  }
  return "The refund could not be completed. No refund was recorded.";
}

function TransactionDetail({
  transactionId,
  onBack,
  onLedgerChanged,
}: {
  transactionId: string;
  onBack: () => void;
  /** Called when the ledger (may have) changed, so the list refetches. */
  onLedgerChanged?: () => void;
}) {
  const [record, setRecord] = useState<TransactionRecord | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [refundFlow, setRefundFlow] = useState<RefundFlow>({ kind: "idle" });

  // Duplicate protection: at most one in-flight refund request, guarded by
  // a ref so not even a re-render or double-invoked handler can repeat it.
  const refundInFlightRef = useRef(false);

  const confirmRefund = useCallback(() => {
    if (refundInFlightRef.current) return;
    refundInFlightRef.current = true;
    setRefundFlow({ kind: "processing" });

    refundTransaction(transactionId)
      .then((result) => {
        // Backend (and PayPal) confirmed. Re-read the authoritative
        // record and let the history list refetch — never a local
        // CAPTURED→REFUNDED mutation.
        setRefundFlow({ kind: "success", result });
        setAttempt((n) => n + 1);
        onLedgerChanged?.();
      })
      .catch((cause: unknown) => {
        console.error("POST /transactions/{id}/refund failed:", cause);
        setRefundFlow({ kind: "error", message: refundErrorMessage(cause) });
        if (cause instanceof ApiError && cause.status > 0) {
          // The server answered: re-read the authoritative state (e.g.
          // still CAPTURED after a PayPal failure, or already REFUNDED).
          setAttempt((n) => n + 1);
          onLedgerChanged?.();
        }
      })
      .finally(() => {
        refundInFlightRef.current = false;
      });
  }, [transactionId, onLedgerChanged]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);

    getTransaction(transactionId, controller.signal)
      .then((transaction) => {
        setRecord(transaction);
        setLoading(false);
      })
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        console.error("GET /transactions/{id} failed:", cause);
        setError("Transaction details couldn't be loaded.");
        setLoading(false);
      });

    return () => controller.abort();
  }, [transactionId, attempt]);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <button
        type="button"
        onClick={onBack}
        className="inline-flex items-center gap-1.5 self-start rounded-lg px-2 py-1 text-xs font-medium text-zinc-500 transition-colors hover:bg-zinc-100 hover:text-zinc-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-200"
      >
        <span aria-hidden>←</span> Back to history
      </button>

      {loading && !record && (
        <div role="status" aria-label="Loading transaction details" className="mt-4 space-y-3">
          {[0, 1, 2, 3].map((i) => (
            <div
              key={i}
              className="h-10 animate-pulse rounded-xl bg-zinc-100 dark:bg-zinc-800"
            />
          ))}
        </div>
      )}

      {!loading && error && (
        <div className="mt-4">
          <p
            role="alert"
            className="rounded-xl border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-400"
          >
            {error}
          </p>
          <button
            type="button"
            onClick={() => setAttempt((n) => n + 1)}
            className="mt-3 inline-flex h-9 items-center rounded-xl border border-zinc-300 bg-white px-4 text-xs font-medium text-zinc-700 shadow-sm transition-colors hover:bg-zinc-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-300 dark:hover:bg-zinc-800"
          >
            Retry
          </button>
        </div>
      )}

      {!error && record && (
        <div className="mt-3 min-h-0 flex-1 overflow-y-auto">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
              {record.product_name}
            </h3>
            <StatusBadge status={record.status} />
          </div>
          <dl className="mt-2 divide-y divide-zinc-100 dark:divide-zinc-800">
            <DetailRow
              label="Catalog price"
              value={inrFormatter.format(record.catalog_price_inr)}
            />
            <DetailRow
              label="PayPal amount"
              value={`${record.paypal_amount} ${record.paypal_currency}`}
            />
            <DetailRow label="Transaction ID" value={record.transaction_id} />
            <DetailRow label="PayPal order ID" value={record.paypal_order_id} />
            {record.paypal_capture_id && (
              <DetailRow
                label="PayPal capture ID"
                value={record.paypal_capture_id}
              />
            )}
            <DetailRow label="Status" value={record.status} />
            <DetailRow label="Created" value={formatDateTime(record.created_at)} />
            {record.updated_at && (
              <DetailRow
                label="Updated"
                value={formatDateTime(record.updated_at)}
              />
            )}
          </dl>

          {/* --- Refund (Phase 9 Step 6) --------------------------------- */}

          {record.status === "CAPTURED" && refundFlow.kind === "idle" && (
            <button
              type="button"
              onClick={() => setRefundFlow({ kind: "confirming" })}
              className="mt-4 inline-flex h-10 items-center rounded-xl border border-red-300 bg-white px-5 text-sm font-semibold text-red-700 shadow-sm transition-colors hover:bg-red-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-red-500 dark:border-red-900 dark:bg-zinc-900 dark:text-red-400 dark:hover:bg-red-950/40"
            >
              Refund transaction
            </button>
          )}

          {/* REFUNDING: a refund is claimed but not yet confirmed REFUNDED.
              No refund action is offered (it is unavailable in this state),
              and the wording neither claims success nor guesses whether the
              outcome is a normal in-flight refund or an unresolved one — the
              status alone does not say, and the UI does not infer it. */}
          {record.status === "REFUNDING" && (
            <p
              role="status"
              className="mt-4 rounded-xl border border-violet-200 bg-violet-50/60 px-3 py-2 text-xs text-violet-800 dark:border-violet-900 dark:bg-violet-950/40 dark:text-violet-300"
            >
              A refund for this transaction is being processed and is not yet
              confirmed. It is not refundable again from here while in this
              state; its final status will update once PayPal confirms or the
              record is reconciled.
            </p>
          )}

          {(refundFlow.kind === "confirming" ||
            refundFlow.kind === "processing") && (
            <div className="mt-4 rounded-2xl border border-red-200 bg-red-50/60 p-4 dark:border-red-900 dark:bg-red-950/30">
              <h4 className="text-sm font-semibold text-red-800 dark:text-red-300">
                Refund this transaction?
              </h4>
              <p className="mt-1.5 text-sm leading-relaxed text-zinc-700 dark:text-zinc-300">
                This will issue a full refund for this captured payment.
              </p>
              <p className="mt-2 text-xs text-zinc-600 dark:text-zinc-400">
                {record.product_name} · {record.paypal_amount}{" "}
                {record.paypal_currency} ·{" "}
                {inrFormatter.format(record.catalog_price_inr)}
              </p>
              <p className="mt-1 break-all font-mono text-[10px] text-zinc-500 dark:text-zinc-500">
                {record.transaction_id}
              </p>
              {refundFlow.kind === "processing" && (
                <p
                  role="status"
                  className="mt-3 text-xs font-medium text-zinc-600 dark:text-zinc-300"
                >
                  Processing refund…
                </p>
              )}
              <div className="mt-3 flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={confirmRefund}
                  disabled={refundFlow.kind === "processing"}
                  className="inline-flex h-10 items-center rounded-xl bg-red-600 px-5 text-sm font-semibold text-white shadow-sm transition-colors hover:bg-red-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-red-500 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  Confirm full refund
                </button>
                <button
                  type="button"
                  onClick={() => setRefundFlow({ kind: "idle" })}
                  disabled={refundFlow.kind === "processing"}
                  className="inline-flex h-10 items-center rounded-xl border border-zinc-300 bg-white px-5 text-sm font-medium text-zinc-700 shadow-sm transition-colors hover:bg-zinc-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500 disabled:cursor-not-allowed disabled:opacity-40 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-300 dark:hover:bg-zinc-800"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}

          {refundFlow.kind === "success" && (
            <div className="mt-4 rounded-2xl border border-emerald-200 bg-emerald-50/60 p-4 dark:border-emerald-900 dark:bg-emerald-950/40">
              <div className="flex items-center gap-2">
                <span
                  aria-hidden
                  className="h-2 w-2 rounded-full bg-emerald-500"
                />
                <h4 className="text-sm font-semibold text-emerald-800 dark:text-emerald-300">
                  Refund completed
                </h4>
              </div>
              <dl className="mt-3 grid gap-x-6 gap-y-1 text-xs text-zinc-600 sm:grid-cols-2 dark:text-zinc-400">
                <div className="flex gap-1.5">
                  <dt className="font-medium">Product:</dt>
                  <dd>{record.product_name}</dd>
                </div>
                {refundFlow.result.amount && refundFlow.result.currency && (
                  <div className="flex gap-1.5">
                    <dt className="font-medium">Refunded:</dt>
                    <dd>
                      {refundFlow.result.amount} {refundFlow.result.currency}
                    </dd>
                  </div>
                )}
                {refundFlow.result.refund_id && (
                  <div className="flex gap-1.5">
                    <dt className="font-medium">Refund ID:</dt>
                    <dd className="break-all">{refundFlow.result.refund_id}</dd>
                  </div>
                )}
                <div className="flex gap-1.5">
                  <dt className="font-medium">Transaction:</dt>
                  <dd className="break-all">
                    {refundFlow.result.transaction_id}
                  </dd>
                </div>
              </dl>
            </div>
          )}

          {refundFlow.kind === "error" && (
            <p
              role="alert"
              className="mt-4 rounded-xl border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-400"
            >
              {refundFlow.message}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// List row
// ---------------------------------------------------------------------------

function TransactionRow({
  transaction,
  onSelect,
}: {
  transaction: TransactionRecord;
  onSelect: () => void;
}) {
  return (
    <li>
      <button
        type="button"
        onClick={onSelect}
        aria-label={`View details for ${transaction.product_name}`}
        className="w-full rounded-xl border border-zinc-200 bg-white p-3 text-left shadow-sm transition-colors hover:border-zinc-300 hover:bg-zinc-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500 dark:border-zinc-800 dark:bg-zinc-900 dark:hover:border-zinc-700 dark:hover:bg-zinc-800/60"
      >
        <div className="flex items-start justify-between gap-3">
          <p className="min-w-0 truncate text-sm font-semibold text-zinc-900 dark:text-zinc-100">
            {transaction.product_name}
          </p>
          <StatusBadge status={transaction.status} />
        </div>
        <div className="mt-1.5 flex flex-wrap items-baseline gap-x-3 gap-y-0.5 text-xs text-zinc-500 dark:text-zinc-400">
          <span className="font-medium tabular-nums text-zinc-700 dark:text-zinc-300">
            {inrFormatter.format(transaction.catalog_price_inr)}
          </span>
          <span className="tabular-nums">
            {transaction.paypal_amount} {transaction.paypal_currency}
          </span>
          <span>{formatDateTime(transaction.created_at)}</span>
        </div>
        <p className="mt-1 truncate font-mono text-[10px] text-zinc-400 dark:text-zinc-500">
          {transaction.transaction_id}
        </p>
      </button>
    </li>
  );
}

// ---------------------------------------------------------------------------
// Drawer shell
// ---------------------------------------------------------------------------

export function TransactionHistoryPanel({
  open,
  onClose,
  refreshToken,
}: TransactionHistoryPanelProps) {
  const { transactions, loading, error, statusFilter, setStatusFilter, reload } =
    useTransactions({ enabled: open, refreshToken });

  const [selectedId, setSelectedId] = useState<string | null>(null);

  // Reopening always starts at the list.
  useEffect(() => {
    if (!open) setSelectedId(null);
  }, [open]);

  // Esc closes the drawer (accessible close alongside the labeled button).
  const handleKeyDown = useCallback(
    (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    },
    [onClose],
  );
  useEffect(() => {
    if (!open) return;
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [open, handleKeyDown]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-40">
      <div
        aria-hidden
        onClick={onClose}
        className="absolute inset-0 bg-zinc-950/40 backdrop-blur-[2px]"
      />
      <aside
        role="dialog"
        aria-modal="true"
        aria-label="Transaction history"
        className="absolute inset-y-0 right-0 flex w-full max-w-md flex-col border-l border-zinc-200 bg-white shadow-2xl dark:border-zinc-800 dark:bg-zinc-950"
      >
        <header className="flex items-center justify-between gap-3 border-b border-zinc-200 px-4 py-3 dark:border-zinc-800">
          <div>
            <h2 className="text-base font-semibold text-zinc-900 dark:text-zinc-100">
              Transaction history
            </h2>
            <p className="text-xs text-zinc-500 dark:text-zinc-400">
              From PayPilot's ledger — not chat history.
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close transaction history"
            className="flex h-9 w-9 items-center justify-center rounded-xl border border-zinc-200 text-zinc-500 transition-colors hover:bg-zinc-100 hover:text-zinc-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500 dark:border-zinc-700 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-200"
          >
            <span aria-hidden>✕</span>
          </button>
        </header>

        <div className="flex min-h-0 flex-1 flex-col px-4 py-3">
          {selectedId ? (
            <TransactionDetail
              key={selectedId}
              transactionId={selectedId}
              onBack={() => setSelectedId(null)}
              onLedgerChanged={reload}
            />
          ) : (
            <>
              <label className="flex items-center gap-2 text-xs text-zinc-500 dark:text-zinc-400">
                <span>Filter</span>
                <select
                  value={statusFilter ?? ""}
                  onChange={(event) =>
                    setStatusFilter(
                      (event.target.value || null) as TransactionStatus | null,
                    )
                  }
                  className="h-8 rounded-lg border border-zinc-200 bg-white px-2 text-xs text-zinc-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-300"
                >
                  {FILTER_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </label>

              <div className="mt-3 min-h-0 flex-1 overflow-y-auto">
                {loading && (
                  <div
                    role="status"
                    aria-label="Loading transaction history"
                    className="space-y-2"
                  >
                    {[0, 1, 2].map((i) => (
                      <div
                        key={i}
                        className="h-20 animate-pulse rounded-xl bg-zinc-100 dark:bg-zinc-800"
                      />
                    ))}
                  </div>
                )}

                {!loading && error && (
                  <div>
                    <p
                      role="alert"
                      className="rounded-xl border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-400"
                    >
                      {error}
                    </p>
                    <button
                      type="button"
                      onClick={reload}
                      className="mt-3 inline-flex h-9 items-center rounded-xl border border-zinc-300 bg-white px-4 text-xs font-medium text-zinc-700 shadow-sm transition-colors hover:bg-zinc-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-300 dark:hover:bg-zinc-800"
                    >
                      Retry
                    </button>
                  </div>
                )}

                {!loading && !error && transactions.length === 0 && (
                  <div className="rounded-xl border border-dashed border-zinc-200 p-6 text-center dark:border-zinc-800">
                    <p className="text-sm font-medium text-zinc-600 dark:text-zinc-300">
                      No transactions yet
                    </p>
                    <p className="mt-1 text-xs text-zinc-400 dark:text-zinc-500">
                      Completed purchases will appear here.
                    </p>
                  </div>
                )}

                {!loading && !error && transactions.length > 0 && (
                  <ul className="space-y-2">
                    {transactions.map((transaction) => (
                      <TransactionRow
                        key={transaction.transaction_id}
                        transaction={transaction}
                        onSelect={() => setSelectedId(transaction.transaction_id)}
                      />
                    ))}
                  </ul>
                )}
              </div>
            </>
          )}
        </div>
      </aside>
    </div>
  );
}
