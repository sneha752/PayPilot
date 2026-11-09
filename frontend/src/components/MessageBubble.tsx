import type { ChatMessage } from "../hooks/useChat";
import type { ChatRoute } from "../types";
import { PlanCard } from "./PlanCard";

/** Routes worth surfacing as a context label (SMALLTALK stays unlabeled). */
const ROUTE_LABELS: Partial<Record<ChatRoute, string>> = {
  SHOP: "Shop",
  REFUND: "Refund",
  HISTORY: "History",
};

export function MessageBubble({ message }: { message: ChatMessage }) {
  if (message.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] rounded-2xl rounded-br-md bg-brand-600 px-4 py-2.5 text-sm leading-relaxed text-white shadow-sm sm:max-w-[75%]">
          <p className="whitespace-pre-wrap">{message.content}</p>
        </div>
      </div>
    );
  }

  const routeLabel = message.route ? ROUTE_LABELS[message.route] : undefined;

  return (
    <div className="flex justify-start">
      <div className="max-w-[92%] sm:max-w-[85%]">
        <div className="mb-1 flex items-center gap-2 pl-1">
          <span className="text-xs font-semibold text-zinc-500 dark:text-zinc-400">
            PayPilot
          </span>
          {routeLabel && (
            <span className="inline-flex rounded-full border border-brand-100 bg-brand-50 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-brand-600 dark:border-brand-700 dark:bg-zinc-900 dark:text-brand-400">
              {routeLabel}
            </span>
          )}
        </div>
        <div className="rounded-2xl rounded-tl-md border border-zinc-200 bg-white px-4 py-2.5 text-sm leading-relaxed text-zinc-800 shadow-sm dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-200">
          <p className="whitespace-pre-wrap">{message.content}</p>
        </div>

        {message.plan && <PlanCard plan={message.plan} />}

        {message.pendingAction && (
          <p className="mt-2 flex items-center gap-1.5 pl-1 text-xs font-medium text-amber-700 dark:text-amber-400">
            <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-amber-500" />
            {message.pendingAction.action_type === "REFUND"
              ? "Refund ready for confirmation"
              : "Purchase ready for confirmation"}
          </p>
        )}
      </div>
    </div>
  );
}
