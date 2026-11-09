/**
 * Post-PayPal-approval capture card (Phase 9 Step 4).
 *
 * Rendered when the page was entered via a PayPal approval return. Every
 * value shown comes from the backend (CaptureResult / ledger record) —
 * nothing is computed or invented here, and "captured" language appears
 * ONLY after the backend confirms it.
 */

import { inrFormatter } from "../lib/format";
import type { PayPalReturnState } from "../hooks/usePayPalReturn";
import type { TransactionRecord } from "../types";

export interface CaptureCardProps {
  state: PayPalReturnState;
  onDismiss: () => void;
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-1.5">
      <dt className="font-medium">{label}:</dt>
      <dd className="break-all">{value}</dd>
    </div>
  );
}

function LedgerRows({ transaction }: { transaction: TransactionRecord }) {
  return (
    <>
      <Row label="Product" value={transaction.product_name} />
      <Row
        label="Catalog price"
        value={inrFormatter.format(transaction.catalog_price_inr)}
      />
    </>
  );
}

function DismissButton({ onDismiss }: { onDismiss: () => void }) {
  return (
    <button
      type="button"
      onClick={onDismiss}
      className="mt-4 inline-flex h-10 items-center rounded-xl border border-zinc-300 bg-white px-5 text-sm font-medium text-zinc-700 shadow-sm transition-colors hover:bg-zinc-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-300 dark:hover:bg-zinc-800"
    >
      Continue chatting
    </button>
  );
}

export function CaptureCard({ state, onDismiss }: CaptureCardProps) {
  if (state.phase === "idle") return null;

  // --- Capture request in flight: approved, but NOT yet captured ----------
  if (state.phase === "capturing") {
    return (
      <div
        role="status"
        className="rounded-2xl border border-amber-300 bg-white p-4 shadow-sm dark:border-amber-700 dark:bg-zinc-900"
      >
        <div className="flex items-center gap-2">
          <span
            aria-hidden
            className="h-2 w-2 animate-pulse rounded-full bg-amber-500"
          />
          <h3 className="text-sm font-semibold text-amber-800 dark:text-amber-400">
            Payment approved — completing your purchase
          </h3>
        </div>
        <p className="mt-2 text-sm leading-relaxed text-zinc-700 dark:text-zinc-300">
          You approved the payment on PayPal. PayPilot is now asking the
          server to capture it — this is not complete yet.
        </p>
        <p className="mt-2 text-xs text-zinc-500 dark:text-zinc-400">
          PayPal order: <span className="break-all">{state.orderId}</span>
        </p>
      </div>
    );
  }

  // --- Backend confirmed the capture --------------------------------------
  if (state.phase === "captured") {
    const { result, transaction } = state;
    return (
      <div className="rounded-2xl border border-emerald-200 bg-emerald-50/60 p-4 shadow-sm dark:border-emerald-900 dark:bg-emerald-950/40">
        <div className="flex items-center gap-2">
          <span aria-hidden className="h-2 w-2 rounded-full bg-emerald-500" />
          <h3 className="text-sm font-semibold text-emerald-800 dark:text-emerald-300">
            Payment captured successfully
          </h3>
        </div>
        <dl className="mt-3 grid gap-x-6 gap-y-1 text-xs text-zinc-600 sm:grid-cols-2 dark:text-zinc-400">
          {transaction && <LedgerRows transaction={transaction} />}
          {result.amount && result.currency && (
            <Row
              label="PayPal amount"
              value={`${result.amount} ${result.currency}`}
            />
          )}
          {(result.transaction_id ?? transaction?.transaction_id) && (
            <Row
              label="Transaction ID"
              value={(result.transaction_id ?? transaction?.transaction_id)!}
            />
          )}
          <Row label="Order ID" value={result.order_id} />
          {result.capture_id && (
            <Row label="Capture ID" value={result.capture_id} />
          )}
          <Row
            label="Status"
            value={transaction?.status ?? result.transaction_status ?? "CAPTURED"}
          />
        </dl>
        <DismissButton onDismiss={onDismiss} />
      </div>
    );
  }

  // --- Order was captured earlier: authoritative state, not a new purchase -
  if (state.phase === "already_captured") {
    const { transaction } = state;
    return (
      <div className="rounded-2xl border border-emerald-200 bg-emerald-50/60 p-4 shadow-sm dark:border-emerald-900 dark:bg-emerald-950/40">
        <div className="flex items-center gap-2">
          <span aria-hidden className="h-2 w-2 rounded-full bg-emerald-500" />
          <h3 className="text-sm font-semibold text-emerald-800 dark:text-emerald-300">
            Payment already captured
          </h3>
        </div>
        <p className="mt-2 text-sm leading-relaxed text-zinc-700 dark:text-zinc-300">
          This order's payment was captured earlier. No new charge was made.
        </p>
        <dl className="mt-3 grid gap-x-6 gap-y-1 text-xs text-zinc-600 sm:grid-cols-2 dark:text-zinc-400">
          {transaction && (
            <>
              <LedgerRows transaction={transaction} />
              <Row
                label="PayPal amount"
                value={`${transaction.paypal_amount} ${transaction.paypal_currency}`}
              />
              <Row label="Transaction ID" value={transaction.transaction_id} />
              {transaction.paypal_capture_id && (
                <Row label="Capture ID" value={transaction.paypal_capture_id} />
              )}
              <Row label="Status" value={transaction.status} />
            </>
          )}
          <Row label="Order ID" value={state.orderId} />
        </dl>
        <DismissButton onDismiss={onDismiss} />
      </div>
    );
  }

  // --- Capture failed: never a success claim ------------------------------
  return (
    <div
      role="alert"
      className="rounded-2xl border border-red-200 bg-red-50/60 p-4 shadow-sm dark:border-red-900 dark:bg-red-950/40"
    >
      <h3 className="text-sm font-semibold text-red-800 dark:text-red-300">
        Payment capture failed
      </h3>
      <p className="mt-2 text-sm leading-relaxed text-zinc-700 dark:text-zinc-300">
        {state.message}
      </p>
      <p className="mt-2 text-xs text-zinc-500 dark:text-zinc-400">
        PayPal order: <span className="break-all">{state.orderId}</span>
      </p>
      <DismissButton onDismiss={onDismiss} />
    </div>
  );
}
