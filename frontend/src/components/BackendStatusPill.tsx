/**
 * Development-only indicator showing whether the backend answered the
 * GET /products connectivity check. Hidden entirely in production builds.
 */

import { useBackendStatus } from "../hooks/useBackendStatus";

const STYLES = {
  checking: {
    dot: "bg-zinc-400",
    text: "text-zinc-500 dark:text-zinc-400",
    label: "Checking backend…",
  },
  connected: {
    dot: "bg-emerald-500",
    text: "text-emerald-700 dark:text-emerald-400",
    label: "Backend connected",
  },
  unavailable: {
    dot: "bg-red-500",
    text: "text-red-700 dark:text-red-400",
    label: "Backend unavailable",
  },
} as const;

export function BackendStatusPill() {
  const status = useBackendStatus();

  if (!import.meta.env.DEV) return null;

  const style = STYLES[status];
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border border-zinc-200 bg-white px-2.5 py-1 text-xs font-medium dark:border-zinc-800 dark:bg-zinc-900 ${style.text}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${style.dot}`} />
      {style.label}
    </span>
  );
}
