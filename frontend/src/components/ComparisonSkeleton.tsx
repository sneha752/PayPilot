/**
 * Loading state for a comparison.
 *
 * The stage label names what the BACKEND is doing (live discovery,
 * deterministic filtering, then AI ranking) during one request. The
 * browser performs none of those steps; this is progress copy for a call
 * that legitimately takes a few seconds.
 *
 * Animation is limited to a single pulse on the placeholder blocks.
 */

export interface ComparisonSkeletonProps {
  /** Current progress label from the comparison hook. */
  stage: string;
  /** How many placeholder cards to show. */
  count?: number;
}

export function ComparisonSkeleton({ stage, count = 3 }: ComparisonSkeletonProps) {
  return (
    <div aria-busy="true" className="flex flex-col gap-5">
      <p
        role="status"
        aria-live="polite"
        className="flex items-center justify-center gap-2 text-sm text-zinc-500 dark:text-zinc-400"
      >
        <span
          aria-hidden
          className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-zinc-300 border-t-brand-500 dark:border-zinc-600"
        />
        {stage}
      </p>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {Array.from({ length: count }, (_, index) => (
          <div
            key={index}
            aria-hidden
            className="animate-pulse overflow-hidden rounded-2xl border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900"
          >
            <div className="h-9 border-b border-zinc-100 bg-zinc-50 dark:border-zinc-800 dark:bg-zinc-800/50" />
            <div className="aspect-[4/3] w-full bg-zinc-100 dark:bg-zinc-800" />
            <div className="flex flex-col gap-3 p-4">
              <div className="h-3.5 w-5/6 rounded bg-zinc-100 dark:bg-zinc-800" />
              <div className="h-3 w-1/3 rounded bg-zinc-100 dark:bg-zinc-800" />
              <div className="h-6 w-2/5 rounded bg-zinc-100 dark:bg-zinc-800" />
              <div className="h-12 w-full rounded-lg bg-zinc-100 dark:bg-zinc-800" />
              <div className="h-10 w-full rounded-xl bg-zinc-100 dark:bg-zinc-800" />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
