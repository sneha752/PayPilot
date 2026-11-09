/**
 * Results surface for one comparison: header, ranked cards, side-by-side.
 *
 * The ranking badge states exactly what the backend reported. When the
 * backend says `deterministic` — its honest fallback when AI ranking
 * failed — this says "Deterministic ranking" and never "AI-ranked".
 *
 * An empty result set is reported as empty. No mock product, sample
 * product, or demo fallback is ever rendered in its place, and the user's
 * query is never silently rewritten to produce results.
 */

import { ComparisonProductCard } from "./ComparisonProductCard";
import { ComparisonTable } from "./ComparisonTable";
import { CONFIGURED_DEMO_RATE } from "../types";
import type { ComparisonResponse } from "../types";

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

function RankingBadge({ data }: { data: ComparisonResponse }) {
  const isAi = data.ranking_method === "ai";
  return (
    <span
      title={
        isAi
          ? `Ranked by the ${data.ranking_provider ?? "configured"} AI provider`
          : "Ranked deterministically by observed price — AI ranking was not used"
      }
      className={`inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[11px] font-medium ${
        isAi
          ? "border-brand-100 bg-brand-50 text-brand-700 dark:border-brand-700 dark:bg-brand-700/20 dark:text-brand-400"
          : "border-zinc-200 bg-zinc-100 text-zinc-600 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-300"
      }`}
    >
      <span
        aria-hidden
        className={`h-1.5 w-1.5 rounded-full ${isAi ? "bg-brand-500" : "bg-zinc-400"}`}
      />
      {isAi ? "AI-ranked" : "Deterministic ranking"}
    </span>
  );
}

export interface ComparisonResultsProps {
  data: ComparisonResponse;
  /** Re-run the same query. */
  onRetry?: () => void;
}

export function ComparisonResults({ data, onRetry }: ComparisonResultsProps) {
  const { results } = data;
  const usesDemoRate = results.some(
    (r) => r.product.conversion_source === CONFIGURED_DEMO_RATE,
  );

  return (
    <div className="flex min-w-0 flex-col gap-6">
      <header className="flex min-w-0 flex-col gap-2">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <h2 className="min-w-0 text-base font-semibold break-words text-zinc-900 dark:text-zinc-100">
            Results for{" "}
            <span className="text-zinc-500 dark:text-zinc-400">
              &ldquo;{data.query}&rdquo;
            </span>
          </h2>
          <RankingBadge data={data} />
        </div>
        <p className="text-xs text-zinc-500 dark:text-zinc-400">
          {plural(data.candidates_considered, "candidate", "candidates")}{" "}
          considered
          {" · "}
          {plural(results.length, "recommendation", "recommendations")}
          {data.discovery_source && (
            <>
              {" · "}
              via {data.discovery_source}
            </>
          )}
        </p>
      </header>

      {results.length === 0 ? (
        <div className="rounded-2xl border border-zinc-200 bg-white px-5 py-10 text-center shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
          <p className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
            No matching products were found.
          </p>
          <p className="mx-auto mt-2 max-w-md text-sm text-zinc-500 dark:text-zinc-400">
            Try removing a strict feature requirement or increasing your
            budget.
          </p>
          {data.discovered_count > 0 && (
            <p className="mt-3 text-xs text-zinc-400 dark:text-zinc-500">
              {plural(data.discovered_count, "product was", "products were")}{" "}
              found, but none met every requirement.
            </p>
          )}
          {onRetry && (
            <button
              type="button"
              onClick={onRetry}
              className="mt-5 rounded-xl border border-zinc-200 bg-white px-4 py-2 text-sm font-medium text-zinc-700 shadow-sm transition-colors hover:bg-zinc-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-200 dark:hover:bg-zinc-800"
            >
              Search again
            </button>
          )}
        </div>
      ) : (
        <>
          <div className="grid min-w-0 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {results.map((result) => (
              <ComparisonProductCard
                key={result.product.external_id}
                result={result}
              />
            ))}
          </div>

          {results.length > 1 && <ComparisonTable results={results} />}

          {usesDemoRate && (
            <p className="text-[11px] leading-relaxed text-zinc-400 dark:text-zinc-500">
              Observed prices are what each retailer was seen asking. INR
              figures are PayPilot estimates derived with a configured demo
              exchange rate, not a live market rate, and are shown for
              comparison only.
            </p>
          )}
        </>
      )}
    </div>
  );
}
