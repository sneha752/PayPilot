/**
 * Explicit purchase-confirmation card (Phase 9 Step 3).
 *
 * Shown when the session has an ACTIVE PURCHASE pending action, and after a
 * confirmation to show the backend's authoritative outcome. Every value
 * displayed (product, price, policy, order, approval URL) comes from the
 * backend; the only thing this card ever sends is { confirm: boolean }.
 */

import { useEffect, useRef, useState } from "react";
import { formatTime, inrFormatter } from "../lib/format";
import type {
  ConfirmationResponse,
  PendingAction,
  PlanResponse,
} from "../types";

export interface ConfirmationCardProps {
  pendingAction: PendingAction | null;
  /** The plan that accompanied the proposal (authoritative product info). */
  plan: PlanResponse | null;
  confirming: boolean;
  confirmation: ConfirmationResponse | null;
  notice: string | null;
  error: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}

function isExpired(action: PendingAction, now: number): boolean {
  const expires = Date.parse(action.expires_at);
  return !Number.isNaN(expires) && expires <= now;
}

export function ConfirmationCard({
  pendingAction,
  plan,
  confirming,
  confirmation,
  notice,
  error,
  onConfirm,
  onCancel,
}: ConfirmationCardProps) {
  // Re-evaluate expiry periodically so an idle card flips to the expired
  // state without a page interaction.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 15000);
    return () => clearInterval(timer);
  }, []);

  // Keep the card in view when it appears or its outcome changes.
  const cardRef = useRef<HTMLDivElement>(null);
  const stateKey = confirmation
    ? "result"
    : pendingAction
      ? "pending"
      : error
        ? "error"
        : "none";
  useEffect(() => {
    if (stateKey !== "none") {
      cardRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }, [stateKey]);

  // --- 1. Outcome of an executed confirmation --------------------------------
  if (confirmation) {
    const result = confirmation.purchase_result;
    if (confirmation.executed && result?.status === "order_created") {
      return (
        <div
          ref={cardRef}
          className="rounded-2xl border border-emerald-200 bg-emerald-50/60 p-4 shadow-sm dark:border-emerald-900 dark:bg-emerald-950/40"
        >
          <div className="flex items-center gap-2">
            <span aria-hidden className="h-2 w-2 rounded-full bg-emerald-500" />
            <h3 className="text-sm font-semibold text-emerald-800 dark:text-emerald-300">
              PayPal order created
            </h3>
          </div>
          <p className="mt-2 text-sm leading-relaxed text-zinc-700 dark:text-zinc-300">
            {confirmation.message}
          </p>
          <dl className="mt-3 grid gap-x-6 gap-y-1 text-xs text-zinc-600 sm:grid-cols-2 dark:text-zinc-400">
            <div className="flex gap-1.5">
              <dt className="font-medium">Product:</dt>
              <dd>{result.product_name}</dd>
            </div>
            <div className="flex gap-1.5">
              <dt className="font-medium">Catalog price:</dt>
              <dd>{inrFormatter.format(result.catalog_price_inr)}</dd>
            </div>
            {result.paypal_amount && result.paypal_currency && (
              <div className="flex gap-1.5">
                <dt className="font-medium">PayPal amount:</dt>
                <dd>
                  {result.paypal_amount} {result.paypal_currency}
                </dd>
              </div>
            )}
            {result.paypal_order_id && (
              <div className="flex gap-1.5">
                <dt className="font-medium">Order ID:</dt>
                <dd className="break-all">{result.paypal_order_id}</dd>
              </div>
            )}
          </dl>
          {result.approve_url ? (
            <div className="mt-4">
              <a
                href={result.approve_url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex h-10 items-center rounded-xl bg-brand-600 px-5 text-sm font-semibold text-white shadow-sm transition-colors hover:bg-brand-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500"
              >
                Continue to PayPal
              </a>
              <p className="mt-2 text-xs text-zinc-500 dark:text-zinc-400">
                Approve the payment on PayPal to continue. Nothing is captured
                until the later capture step.
              </p>
            </div>
          ) : (
            <p className="mt-3 text-xs text-zinc-500 dark:text-zinc-400">
              The order is created and awaits PayPal approval. No approval
              link was provided for this order.
            </p>
          )}
        </div>
      );
    }

    // Confirmed but not executed (e.g. policy denied the purchase at
    // confirmation time). The backend message is authoritative.
    return (
      <div
        ref={cardRef}
        className="rounded-2xl border border-red-200 bg-red-50/60 p-4 shadow-sm dark:border-red-900 dark:bg-red-950/40"
      >
        <h3 className="text-sm font-semibold text-red-800 dark:text-red-300">
          Purchase not executed
        </h3>
        <p className="mt-2 text-sm leading-relaxed text-zinc-700 dark:text-zinc-300">
          {confirmation.message}
        </p>
        {result?.policy_decision && (
          <p className="mt-2 text-xs text-zinc-500 dark:text-zinc-400">
            Policy: {result.policy_decision.reason}
          </p>
        )}
      </div>
    );
  }

  // --- 2. Active PURCHASE pending action -------------------------------------
  if (pendingAction && pendingAction.action_type === "PURCHASE") {
    if (isExpired(pendingAction, now)) {
      return (
        <div
          ref={cardRef}
          className="rounded-2xl border border-zinc-200 bg-zinc-50 p-4 text-sm text-zinc-600 shadow-sm dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-400"
        >
          Purchase request expired. Please ask PayPilot to create a new
          purchase proposal.
        </div>
      );
    }

    const product = plan?.selected_product ?? null;
    const policy = plan?.policy_decision ?? null;

    return (
      <div
        ref={cardRef}
        className="rounded-2xl border border-amber-300 bg-white p-4 shadow-sm dark:border-amber-700 dark:bg-zinc-900"
      >
        <div className="flex items-center gap-2">
          <span aria-hidden className="h-2 w-2 rounded-full bg-amber-500" />
          <h3 className="text-sm font-semibold text-amber-800 dark:text-amber-400">
            Purchase ready for confirmation
          </h3>
        </div>

        <div className="mt-3 flex flex-wrap items-start justify-between gap-x-4 gap-y-1">
          <div className="min-w-0">
            <p className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
              {product ? product.name : `Product ${pendingAction.product_id ?? ""}`}
            </p>
            {product && (
              <p className="text-xs text-zinc-500 dark:text-zinc-400">
                {product.brand} · {product.category} · ★ {product.rating}
              </p>
            )}
          </div>
          {product && (
            <p className="text-base font-semibold tabular-nums text-zinc-900 dark:text-zinc-100">
              {inrFormatter.format(product.price_inr)}
            </p>
          )}
        </div>

        {policy && (
          <p className="mt-2 text-xs leading-relaxed text-zinc-500 dark:text-zinc-400">
            Policy: {policy.reason}
          </p>
        )}

        <p className="mt-2 text-xs text-zinc-400 dark:text-zinc-500">
          Expires at {formatTime(pendingAction.expires_at)}. Nothing is
          ordered or charged until you confirm.
        </p>

        {notice && (
          <p
            role="status"
            className="mt-3 rounded-xl border border-zinc-200 bg-zinc-50 px-3 py-2 text-xs text-zinc-600 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-300"
          >
            {notice}
          </p>
        )}
        {error && (
          <p
            role="alert"
            className="mt-3 rounded-xl border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-400"
          >
            {error}
          </p>
        )}

        <div className="mt-4 flex flex-wrap gap-2">
          <button
            type="button"
            onClick={onConfirm}
            disabled={confirming}
            className="inline-flex h-10 items-center rounded-xl bg-brand-600 px-5 text-sm font-semibold text-white shadow-sm transition-colors hover:bg-brand-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {confirming ? "Confirming…" : "Confirm purchase"}
          </button>
          <button
            type="button"
            onClick={onCancel}
            disabled={confirming}
            className="inline-flex h-10 items-center rounded-xl border border-zinc-300 bg-white px-5 text-sm font-medium text-zinc-700 shadow-sm transition-colors hover:bg-zinc-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500 disabled:cursor-not-allowed disabled:opacity-40 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-300 dark:hover:bg-zinc-800"
          >
            Cancel
          </button>
        </div>
      </div>
    );
  }

  // --- 3. A confirm attempt failed after the action disappeared --------------
  if (error) {
    return (
      <div
        ref={cardRef}
        role="alert"
        className="rounded-2xl border border-red-200 bg-red-50/60 p-4 text-sm text-red-700 shadow-sm dark:border-red-900 dark:bg-red-950/40 dark:text-red-400"
      >
        {error}
      </div>
    );
  }

  return null;
}
