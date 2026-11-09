import type { ReactNode } from "react";

interface PlaceholderPanelProps {
  title: string;
  description: string;
  icon: ReactNode;
}

/** Card standing in for a feature that arrives in a later Phase 9 step. */
export function PlaceholderPanel({ title, description, icon }: PlaceholderPanelProps) {
  return (
    <section className="flex h-full flex-col rounded-2xl border border-zinc-200 bg-white p-6 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
      <div className="flex items-center gap-3">
        <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-zinc-100 text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400">
          {icon}
        </div>
        <h2 className="text-base font-semibold">{title}</h2>
      </div>
      <p className="mt-3 text-sm leading-relaxed text-zinc-500 dark:text-zinc-400">
        {description}
      </p>
      <div className="mt-auto pt-6">
        <span className="inline-flex rounded-full bg-zinc-100 px-2.5 py-1 text-xs font-medium text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400">
          Coming in a later step
        </span>
      </div>
    </section>
  );
}
