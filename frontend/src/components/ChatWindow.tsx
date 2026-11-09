import { useEffect, useRef, type ReactNode } from "react";
import type { ChatMessage } from "../hooks/useChat";
import { EmptyChat } from "./EmptyChat";
import { MessageBubble } from "./MessageBubble";
import { TypingIndicator } from "./TypingIndicator";

interface ChatWindowProps {
  messages: ChatMessage[];
  loading: boolean;
  onSelectPrompt: (prompt: string) => void;
  /** Rendered after the messages, inside the scroll area (e.g. the
   * confirmation card). */
  footer?: ReactNode;
}

/** Scrollable conversation area; keeps the latest message in view. */
export function ChatWindow({
  messages,
  loading,
  onSelectPrompt,
  footer,
}: ChatWindowProps) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, loading]);

  if (messages.length === 0) {
    return <EmptyChat onSelectPrompt={onSelectPrompt} disabled={loading} />;
  }

  return (
    <div
      className="flex flex-col gap-4 px-4 py-6 sm:px-6"
      role="log"
      aria-label="Conversation with PayPilot"
      aria-live="polite"
    >
      {messages.map((message) => (
        <MessageBubble key={message.id} message={message} />
      ))}
      {loading && <TypingIndicator />}
      {footer}
      <div ref={bottomRef} />
    </div>
  );
}
