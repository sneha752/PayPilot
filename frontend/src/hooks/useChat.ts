/**
 * Conversation state for the PayPilot chat UI.
 *
 * Owns messages, the backend session id, loading/error state, and (Phase 9
 * Step 3) the session's active pending action plus the explicit-confirmation
 * flow. All business decisions (routing, policy, pricing, execution) stay on
 * the backend — this hook only relays what the API returns, and the
 * confirmation request body is exactly { confirm: boolean }, nothing else.
 */

import { useCallback, useState } from "react";
import { confirmSession, getSession, sendChat } from "../services/api";
import { ApiError } from "../services/apiError";
import type {
  ChatRoute,
  ConfirmationResponse,
  PendingAction,
  PlanResponse,
} from "../types";

/** One rendered message. Assistant messages carry the structured extras. */
export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  route?: ChatRoute;
  plan?: PlanResponse | null;
  pendingAction?: PendingAction | null;
}

export interface ChatState {
  messages: ChatMessage[];
  sessionId: string | null;
  loading: boolean;
  error: string | null;
  sendMessage: (text: string) => void;

  /** The session's ACTIVE pending action after the latest turn/confirm. */
  pendingAction: PendingAction | null;
  /** The plan that accompanied the pending action's proposal, if any. */
  pendingPlan: PlanResponse | null;
  /** True while POST /sessions/{id}/confirm is in flight. */
  confirming: boolean;
  /** Result of the last confirm=true call (order created / not executed). */
  confirmation: ConfirmationResponse | null;
  /** Backend message after an explicit decline (proposal stays pending). */
  confirmNotice: string | null;
  confirmError: string | null;
  /** Explicitly confirm (true) or decline (false) the pending action. */
  confirmPending: (confirm: boolean) => void;
}

function friendlyErrorMessage(error: unknown): string {
  if (error instanceof ApiError && error.kind === "network") {
    return "PayPilot couldn't reach the server. Make sure the backend is running, then try again.";
  }
  return "That request couldn't be processed. Please try again.";
}

export function useChat(): ChatState {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [pendingAction, setPendingAction] = useState<PendingAction | null>(null);
  const [pendingPlan, setPendingPlan] = useState<PlanResponse | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [confirmation, setConfirmation] = useState<ConfirmationResponse | null>(null);
  const [confirmNotice, setConfirmNotice] = useState<string | null>(null);
  const [confirmError, setConfirmError] = useState<string | null>(null);

  const sendMessage = useCallback(
    (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || loading) return;

      setError(null);
      setMessages((prev) => [
        ...prev,
        { id: crypto.randomUUID(), role: "user", content: trimmed },
      ]);
      setLoading(true);

      sendChat(trimmed, sessionId)
        .then((response) => {
          setSessionId(response.session_id);
          setMessages((prev) => [
            ...prev,
            {
              id: crypto.randomUUID(),
              role: "assistant",
              content: response.assistant_message,
              route: response.route,
              plan: response.data.plan ?? null,
              pendingAction: response.pending_action ?? null,
            },
          ]);
          // /chat reports the session's ACTIVE pending action after this
          // turn — it is the single source of truth for the confirm card.
          if (response.pending_action) {
            setPendingAction(response.pending_action);
            setPendingPlan(response.data.plan ?? null);
            // A fresh proposal supersedes any earlier confirmation state.
            setConfirmation(null);
            setConfirmNotice(null);
            setConfirmError(null);
          } else {
            setPendingAction(null);
          }
        })
        .catch((cause: unknown) => {
          // Full detail stays in the console; the UI gets a safe summary.
          console.error("POST /chat failed:", cause);
          setError(friendlyErrorMessage(cause));
        })
        .finally(() => setLoading(false));
    },
    [loading, sessionId],
  );

  const confirmPending = useCallback(
    (confirm: boolean) => {
      if (!sessionId || confirming || !pendingAction) return;

      setConfirming(true);
      setConfirmError(null);
      setConfirmNotice(null);

      confirmSession(sessionId, confirm)
        .then(async (response) => {
          if (!confirm) {
            // Declined: the backend keeps the proposal pending until it
            // expires, so the card stays actionable with its notice.
            setConfirmNotice(response.message);
            setPendingAction(response.pending_action ?? null);
            return;
          }

          setConfirmation(response);

          // Reflect the backend's actual post-confirmation state rather
          // than relying on optimistic local state alone.
          try {
            const session = await getSession(sessionId);
            setPendingAction(session.pending_action ?? null);
          } catch {
            setPendingAction(response.pending_action ?? null);
          }
        })
        .catch((cause: unknown) => {
          console.error("POST /sessions/{id}/confirm failed:", cause);
          if (cause instanceof ApiError && cause.kind === "conflict") {
            // 409: no active pending action anymore (expired or consumed).
            setPendingAction(null);
            setConfirmError(
              "This purchase request is no longer active. Ask PayPilot to create a new purchase proposal.",
            );
          } else if (cause instanceof ApiError && cause.kind === "network") {
            setConfirmError(
              "PayPilot couldn't reach the server. Nothing was confirmed — please try again.",
            );
          } else {
            setConfirmError(
              "The confirmation couldn't be completed. Nothing was charged — you can try again.",
            );
          }
        })
        .finally(() => setConfirming(false));
    },
    [sessionId, confirming, pendingAction],
  );

  return {
    messages,
    sessionId,
    loading,
    error,
    sendMessage,
    pendingAction,
    pendingPlan,
    confirming,
    confirmation,
    confirmNotice,
    confirmError,
    confirmPending,
  };
}
