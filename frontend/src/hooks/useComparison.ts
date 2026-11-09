/**
 * Product comparison state (Phase 10G).
 *
 * A thin fetch-state wrapper around POST /discovery/compare via the
 * centralized API service. The backend is the single authority: this hook
 * stores exactly the response it received and never computes, converts,
 * filters, re-ranks, or invents anything. A failed request is surfaced as
 * an explicit error, never as an empty result list.
 *
 * The staged progress labels are presentation only — the browser is not
 * performing those steps, the backend is. They exist because one request
 * legitimately takes several seconds (live discovery plus an AI call).
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { describeComparisonError } from "../lib/comparisonError";
import { compareProducts } from "../services/api";
import type { ComparisonResponse } from "../types";

/** What the backend is doing while we wait. UI copy only. */
export const PROGRESS_STAGES = [
  "Searching live products…",
  "Filtering candidates…",
  "Comparing the best matches…",
] as const;

/** How long each label shows before advancing to the next. */
export const STAGE_INTERVAL_MS = 1800;

export interface ComparisonState {
  /** Exactly the backend response, or null before the first search. */
  data: ComparisonResponse | null;
  loading: boolean;
  /** User-facing error message; when set, `data` must not be trusted. */
  error: string | null;
  /** Whether retrying the same query could plausibly succeed. */
  retryable: boolean;
  /** The query of the in-flight or most recent request. */
  submittedQuery: string;
  /** Current progress label while loading. */
  stage: string;
  /** Run a comparison. A blank query is rejected without a request. */
  search: (query: string) => void;
  /** Re-run the last submitted query. */
  retry: () => void;
  /** Return to the pre-search state. */
  reset: () => void;
}

export function useComparison(): ComparisonState {
  const [data, setData] = useState<ComparisonResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [retryable, setRetryable] = useState(false);
  const [submittedQuery, setSubmittedQuery] = useState("");
  const [stageIndex, setStageIndex] = useState(0);

  // One in-flight request at a time: a new search supersedes the old one.
  const controllerRef = useRef<AbortController | null>(null);

  useEffect(
    () => () => {
      controllerRef.current?.abort();
    },
    [],
  );

  // Advance the progress label while a request is in flight.
  useEffect(() => {
    if (!loading) return;
    const timer = window.setInterval(() => {
      setStageIndex((index) => Math.min(index + 1, PROGRESS_STAGES.length - 1));
    }, STAGE_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [loading]);

  const run = useCallback((query: string) => {
    const trimmed = query.trim();
    // Blank input never reaches the network.
    if (!trimmed) return;

    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;

    setSubmittedQuery(trimmed);
    setLoading(true);
    setError(null);
    setRetryable(false);
    setStageIndex(0);
    // Clear stale results so an old comparison can never be read as the
    // answer to the new query.
    setData(null);

    compareProducts(trimmed, {}, controller.signal)
      .then((response) => {
        if (controller.signal.aborted) return;
        // Verbatim backend response — never merged, patched, or reordered.
        setData(response);
        setLoading(false);
      })
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === "AbortError") {
          return; // superseded request — not an error
        }
        const info = describeComparisonError(cause);
        setData(null);
        setError(info.message);
        setRetryable(info.retryable);
        setLoading(false);
      });
  }, []);

  const retry = useCallback(() => {
    if (submittedQuery) run(submittedQuery);
  }, [run, submittedQuery]);

  const reset = useCallback(() => {
    controllerRef.current?.abort();
    controllerRef.current = null;
    setData(null);
    setError(null);
    setRetryable(false);
    setSubmittedQuery("");
    setLoading(false);
    setStageIndex(0);
  }, []);

  return {
    data,
    loading,
    error,
    retryable,
    submittedQuery,
    stage: PROGRESS_STAGES[stageIndex],
    search: run,
    retry,
    reset,
  };
}
