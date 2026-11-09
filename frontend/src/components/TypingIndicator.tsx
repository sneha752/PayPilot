/** Subtle three-dot indicator shown while POST /chat is in flight. */
export function TypingIndicator() {
  return (
    <div className="flex justify-start" role="status" aria-label="PayPilot is thinking">
      <div className="rounded-2xl rounded-tl-md border border-zinc-200 bg-white px-4 py-3 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
        <div className="flex items-center gap-1">
          {[0, 1, 2].map((i) => (
            <span
              key={i}
              aria-hidden
              className="h-1.5 w-1.5 animate-bounce rounded-full bg-zinc-400 dark:bg-zinc-500"
              style={{ animationDelay: `${i * 150}ms` }}
            />
          ))}
        </div>
      </div>
    </div>
  );
}
