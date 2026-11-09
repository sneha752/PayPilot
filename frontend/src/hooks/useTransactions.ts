/**
 * Transaction history state (Phase 9 Step 5).
 *
 * Thin fetch-state wrapper around GET /transactions via the centralized
 * API service. The backend ledger is the single financial authority:
 * this hook never computes, converts, infers, or invents anything — it
 * stores exactly the records the backend returned, or an explicit error
 * (a failed request is never shown as an empty list).
 */

import { useCallback, useEffect, useState } from "react";
import { getTransactions } from "../services/api";
import type { TransactionRecord, TransactionStatus } from "../types";

/** Default page size for the history panel. */
export const HISTORY_LIMIT = 20;

export interface TransactionsState {
  /** Exactly the records the backend returned, newest-first. */
  transactions: TransactionRecord[];
  loading: boolean;
  /** Localized history error; when set, the list must not be trusted. */
  error: string | null;
  /** Backend-side status filter (null = all). Changing it refetches. */
  statusFilter: TransactionStatus | null;
  setStatusFilter: (status: TransactionStatus | null) => void;
  /** Retry/refresh — refetches from GET /transactions. */
  reload: () => void;
}

export interface UseTransactionsOptions {
  /** Fetch only while true (e.g. while the history panel is open). */
  enabled: boolean;
  /** Bump to force a refetch (e.g. after a successful capture). */
  refreshToken?: number;
}

export function useTransactions({
  enabled,
  refreshToken = 0,
}: UseTransactionsOptions): TransactionsState {
  const [transactions, setTransactions] = useState<TransactionRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<TransactionStatus | null>(
    null,
  );
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    if (!enabled) return;

    const controller = new AbortController();
    setLoading(true);
    setError(null);

    getTransactions(
      { status: statusFilter ?? undefined, limit: HISTORY_LIMIT },
      controller.signal,
    )
      .then((response) => {
        // Verbatim backend records — never merged, patched, or reordered.
        setTransactions(response.transactions);
        setLoading(false);
      })
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === "AbortError") {
          return; // superseded request — not an error
        }
        console.error("GET /transactions failed:", cause);
        setTransactions([]);
        setError("Transaction history couldn't be loaded.");
        setLoading(false);
      });

    return () => controller.abort();
  }, [enabled, statusFilter, refreshToken, reloadToken]);

  const reload = useCallback(() => setReloadToken((n) => n + 1), []);

  return { transactions, loading, error, statusFilter, setStatusFilter, reload };
}
