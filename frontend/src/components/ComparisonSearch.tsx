/**
 * Shopping search input for the comparison surface.
 *
 * Sends only free text. The placeholder is an example of the SHAPE of a
 * query, not a product we stock or recommend — any shopping request is
 * valid, and nothing here is hardcoded into the results.
 */

import { useState } from "react";

export interface ComparisonSearchProps {
  onSearch: (query: string) => void;
  loading: boolean;
  /** Prefill (e.g. the previous query after an error). */
  initialQuery?: string;
}

const PLACEHOLDER = "Try: wireless headphones under ₹3,000";

export function ComparisonSearch({
  onSearch,
  loading,
  initialQuery = "",
}: ComparisonSearchProps) {
  const [query, setQuery] = useState(initialQuery);
  const trimmed = query.trim();
  const canSubmit = trimmed.length > 0 && !loading;

  return (
    <form
      role="search"
      onSubmit={(event) => {
        event.preventDefault();
        // Blank or whitespace-only input is a no-op, not a request.
        if (!canSubmit) return;
        onSearch(trimmed);
      }}
      className="flex flex-col gap-2 sm:flex-row sm:items-center"
    >
      <label htmlFor="comparison-query" className="sr-only">
        What are you shopping for?
      </label>
      <div className="relative min-w-0 flex-1">
        <svg
          aria-hidden
          viewBox="0 0 16 16"
          className="pointer-events-none absolute top-1/2 left-3.5 h-4 w-4 -translate-y-1/2 text-zinc-400"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.6"
          strokeLinecap="round"
        >
          <circle cx="7" cy="7" r="4.5" />
          <path d="M10.5 10.5 14 14" />
        </svg>
        <input
          id="comparison-query"
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={PLACEHOLDER}
          disabled={loading}
          autoComplete="off"
          enterKeyHint="search"
          className="w-full rounded-xl border border-zinc-200 bg-white py-3 pr-4 pl-10 text-[15px] text-zinc-900 shadow-sm transition-colors placeholder:text-zinc-400 focus-visible:border-brand-400 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-brand-500 disabled:cursor-not-allowed disabled:opacity-60 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100 dark:placeholder:text-zinc-500"
        />
      </div>
      <button
        type="submit"
        disabled={!canSubmit}
        className="inline-flex shrink-0 items-center justify-center gap-2 rounded-xl bg-brand-500 px-5 py-3 text-sm font-semibold text-white shadow-sm transition-colors hover:bg-brand-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500 disabled:cursor-not-allowed disabled:bg-zinc-300 dark:disabled:bg-zinc-700"
      >
        {loading ? (
          <>
            <span
              aria-hidden
              className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white/40 border-t-white"
            />
            Comparing
          </>
        ) : (
          "Compare"
        )}
      </button>
    </form>
  );
}
