/**
 * The shopping comparison surface (Phase 10G).
 *
 * Search -> POST /discovery/compare -> ranked results. This is a separate
 * surface from the chat: it does not touch conversation state, sessions,
 * or any purchase flow, and nothing here can create a PayPal order.
 *
 * Research only. The "View Product" action on each card opens the
 * retailer's own page; PayPilot does not check out on the user's behalf.
 */

import { ComparisonResults } from "../components/ComparisonResults";
import { ComparisonSearch } from "../components/ComparisonSearch";
import { ComparisonSkeleton } from "../components/ComparisonSkeleton";
import { useComparison } from "../hooks/useComparison";

function Intro() {
  return (
    <div className="rounded-2xl border border-dashed border-zinc-200 bg-white/60 px-5 py-10 text-center dark:border-zinc-700 dark:bg-zinc-900/40">
      <h2 className="text-base font-semibold text-zinc-900 dark:text-zinc-100">
        Compare products across the live web
      </h2>
      <p className="mx-auto mt-2 max-w-md text-sm leading-relaxed text-zinc-500 dark:text-zinc-400">
        Describe what you want and your budget. PayPilot searches live
        retailers, filters on your requirements, and explains why each
        option made the shortlist.
      </p>
      <p className="mx-auto mt-4 max-w-md text-xs text-zinc-400 dark:text-zinc-500">
        Research only &mdash; no purchase is made and no payment is
        authorized here.
      </p>
    </div>
  );
}

export function ShopPage() {
  const { data, loading, error, retryable, submittedQuery, stage, search, retry } =
    useComparison();

  return (
    <main className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto w-full max-w-6xl px-4 py-6 sm:px-6">
        <div className="flex flex-col gap-6">
          <ComparisonSearch
            onSearch={search}
            loading={loading}
            initialQuery={submittedQuery}
          />

          {error && (
            <div
              role="alert"
              className="flex min-w-0 flex-col gap-3 rounded-2xl border border-red-200 bg-red-50 px-4 py-3.5 text-sm text-red-700 sm:flex-row sm:items-center sm:justify-between dark:border-red-900 dark:bg-red-950 dark:text-red-300"
            >
              <span className="min-w-0 break-words">{error}</span>
              {retryable && (
                <button
                  type="button"
                  onClick={retry}
                  className="shrink-0 self-start rounded-lg border border-red-300 bg-white px-3 py-1.5 text-xs font-semibold text-red-700 transition-colors hover:bg-red-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-red-500 sm:self-auto dark:border-red-800 dark:bg-red-900/40 dark:text-red-200 dark:hover:bg-red-900/70"
                >
                  Try again
                </button>
              )}
            </div>
          )}

          {loading && <ComparisonSkeleton stage={stage} />}

          {/* Results render only from a backend response. There is no
              fallback product list when data is absent. */}
          {!loading && data && (
            <ComparisonResults data={data} onRetry={retry} />
          )}

          {!loading && !data && !error && <Intro />}
        </div>
      </div>
    </main>
  );
}
