import { BrandMark } from "./BrandMark";

const EXAMPLE_PROMPTS = [
  "Find headphones under ₹3,000",
  "Find me a laptop under ₹80,000",
  "Show my recent purchases",
];

interface EmptyChatProps {
  /** Sends the clicked example through the normal chat flow. */
  onSelectPrompt: (prompt: string) => void;
  disabled?: boolean;
}

/** Welcome state shown before the first message. */
export function EmptyChat({ onSelectPrompt, disabled }: EmptyChatProps) {
  return (
    <div className="flex h-full flex-col items-center justify-center px-4 py-12 text-center">
      <BrandMark />
      <h2 className="mt-4 text-xl font-semibold tracking-tight">PayPilot</h2>
      <p className="mt-1 text-sm font-medium text-zinc-500 dark:text-zinc-400">
        Your money. Your rules.
      </p>
      <p className="mt-4 max-w-sm text-sm leading-relaxed text-zinc-500 dark:text-zinc-400">
        Tell me what you're looking for, and I'll help you shop within your
        rules.
      </p>

      <div className="mt-8 flex w-full max-w-md flex-col gap-2">
        {EXAMPLE_PROMPTS.map((prompt) => (
          <button
            key={prompt}
            type="button"
            disabled={disabled}
            onClick={() => onSelectPrompt(prompt)}
            className="rounded-xl border border-zinc-200 bg-white px-4 py-3 text-left text-sm text-zinc-700 shadow-sm transition-colors hover:border-brand-400 hover:text-brand-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500 disabled:opacity-50 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-300 dark:hover:border-brand-500 dark:hover:text-brand-400"
          >
            {prompt}
          </button>
        ))}
      </div>
    </div>
  );
}
