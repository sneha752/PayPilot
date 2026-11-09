import type React from "react";
import { BackendStatusPill } from "./BackendStatusPill";
import { BrandMark } from "./BrandMark";

export interface AppHeaderProps {
  /** Opens the transaction history drawer (Phase 9 Step 5). */
  onOpenActivity?: () => void;
  /** Surface switcher slot (Phase 10G). Omitted = header unchanged. */
  nav?: React.ReactNode;
  /** Logs the operator out (Phase 11F-H). Omitted = no logout button. */
  onLogout?: () => void;
}

export function AppHeader({ onOpenActivity, nav, onLogout }: AppHeaderProps) {
  return (
    <header className="border-b border-zinc-200 bg-white/80 backdrop-blur dark:border-zinc-800 dark:bg-zinc-950/80">
      <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-4 py-4 sm:px-6">
        <div className="flex items-center gap-3">
          <BrandMark />
          <div>
            <div className="flex items-baseline gap-2">
              <h1 className="text-lg font-semibold tracking-tight">PayPilot</h1>
              <span className="hidden text-sm text-zinc-500 sm:inline dark:text-zinc-400">
                Your money. Your rules.
              </span>
            </div>
          </div>
        </div>

        <div className="flex items-center gap-2">
          {nav}
          {onOpenActivity && (
            <button
              type="button"
              onClick={onOpenActivity}
              aria-label="Open transaction history"
              className="inline-flex h-8 items-center gap-1.5 rounded-full border border-zinc-200 bg-white px-3 text-xs font-medium text-zinc-600 shadow-sm transition-colors hover:bg-zinc-50 hover:text-zinc-800 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-300 dark:hover:bg-zinc-800 dark:hover:text-zinc-100"
            >
              <svg
                aria-hidden
                viewBox="0 0 16 16"
                className="h-3.5 w-3.5"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
              >
                <path d="M2 4h12M2 8h12M2 12h8" />
              </svg>
              Activity
            </button>
          )}
          <BackendStatusPill />
          <span className="inline-flex items-center gap-1.5 rounded-full border border-amber-200 bg-amber-50 px-2.5 py-1 text-xs font-medium text-amber-700 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-400">
            <span className="h-1.5 w-1.5 rounded-full bg-amber-500" />
            Sandbox
          </span>
          {onLogout && (
            <button
              type="button"
              onClick={onLogout}
              aria-label="Log out"
              className="inline-flex h-8 items-center rounded-full border border-zinc-200 bg-white px-3 text-xs font-medium text-zinc-600 shadow-sm transition-colors hover:bg-zinc-50 hover:text-zinc-800 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-300 dark:hover:bg-zinc-800 dark:hover:text-zinc-100"
            >
              Log out
            </button>
          )}
        </div>
      </div>
    </header>
  );
}
