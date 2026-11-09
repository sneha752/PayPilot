import { useEffect } from "react";
import { CaptureCard } from "../components/CaptureCard";
import { ChatInput } from "../components/ChatInput";
import { ChatWindow } from "../components/ChatWindow";
import { ConfirmationCard } from "../components/ConfirmationCard";
import { useChat } from "../hooks/useChat";
import { usePayPalReturn } from "../hooks/usePayPalReturn";

export interface HomePageProps {
  /** Notified when a capture completes, so transaction history can
   * refresh from the backend (Phase 9 Step 5). */
  onCaptureComplete?: () => void;
}

/** The conversational commerce page: one chat, one input, one session. */
export function HomePage({ onCaptureComplete }: HomePageProps = {}) {
  const {
    messages,
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
  } = useChat();

  // Return-from-PayPal capture flow. A PayPal approval return is a fresh
  // page load (the chat is empty), so the card renders above the chat
  // window rather than in its footer slot.
  const paypalReturn = usePayPalReturn();

  // A finished capture (new or re-encountered) means the ledger changed /
  // is authoritative — let the app refresh transaction history from it.
  const capturePhase = paypalReturn.state.phase;
  useEffect(() => {
    if (capturePhase === "captured" || capturePhase === "already_captured") {
      onCaptureComplete?.();
    }
  }, [capturePhase, onCaptureComplete]);

  return (
    <main className="flex min-h-0 flex-1 flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto h-full w-full max-w-3xl">
          {paypalReturn.state.phase !== "idle" && (
            <div className="px-4 pt-6 sm:px-6">
              <CaptureCard
                state={paypalReturn.state}
                onDismiss={paypalReturn.dismiss}
              />
            </div>
          )}
          <ChatWindow
            messages={messages}
            loading={loading}
            onSelectPrompt={sendMessage}
            footer={
              <ConfirmationCard
                pendingAction={pendingAction}
                plan={pendingPlan}
                confirming={confirming}
                confirmation={confirmation}
                notice={confirmNotice}
                error={confirmError}
                onConfirm={() => confirmPending(true)}
                onCancel={() => confirmPending(false)}
              />
            }
          />
        </div>
      </div>

      <div className="border-t border-zinc-200 bg-white/80 backdrop-blur dark:border-zinc-800 dark:bg-zinc-950/80">
        <div className="mx-auto w-full max-w-3xl px-4 py-3 sm:px-6">
          {error && (
            <div
              role="alert"
              className="mb-2 rounded-xl border border-red-200 bg-red-50 px-4 py-2.5 text-sm text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-400"
            >
              {error}
            </div>
          )}
          <ChatInput onSend={sendMessage} loading={loading} />
          <p className="mt-2 text-center text-[11px] text-zinc-400 dark:text-zinc-500">
            PayPal Sandbox demo · No real money moves here · Purchases always
            need your confirmation
          </p>
        </div>
      </div>
    </main>
  );
}
