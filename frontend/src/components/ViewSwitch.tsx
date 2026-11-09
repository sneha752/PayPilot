/**
 * Chat / Shop surface switch (Phase 10G).
 *
 * A single additive entry point to the comparison surface, deliberately
 * small: the existing header layout and the chat's primacy are unchanged.
 */

export type AppView = "chat" | "shop";

const VIEWS: ReadonlyArray<{ id: AppView; label: string }> = [
  { id: "chat", label: "Chat" },
  { id: "shop", label: "Shop" },
];

export interface ViewSwitchProps {
  view: AppView;
  onChange: (view: AppView) => void;
}

export function ViewSwitch({ view, onChange }: ViewSwitchProps) {
  return (
    <div
      role="tablist"
      aria-label="PayPilot surface"
      className="inline-flex shrink-0 items-center rounded-full border border-zinc-200 bg-zinc-100 p-0.5 dark:border-zinc-700 dark:bg-zinc-800"
    >
      {VIEWS.map((entry) => {
        const selected = entry.id === view;
        return (
          <button
            key={entry.id}
            type="button"
            role="tab"
            aria-selected={selected}
            onClick={() => onChange(entry.id)}
            className={`rounded-full px-3 py-1 text-xs font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500 ${
              selected
                ? "bg-white text-zinc-900 shadow-sm dark:bg-zinc-950 dark:text-zinc-100"
                : "text-zinc-500 hover:text-zinc-800 dark:text-zinc-400 dark:hover:text-zinc-100"
            }`}
          >
            {entry.label}
          </button>
        );
      })}
    </div>
  );
}
