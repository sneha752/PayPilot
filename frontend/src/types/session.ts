/** Mirrors backend `app/schemas/session.py`. */

export type TurnRole = "user" | "assistant";

/** One message in a session's conversation, in order of occurrence. */
export interface ConversationTurn {
  turn_id: string;
  session_id: string;
  role: TurnRole;
  content: string;
  created_at: string;
}

export type PendingActionType = "PURCHASE" | "REFUND";

/**
 * One proposal awaiting explicit user confirmation. References only:
 * product_id for PURCHASE, transaction_id for REFUND. It authorizes
 * nothing by itself — the backend re-validates everything on confirm.
 */
export interface PendingAction {
  action_type: PendingActionType;
  product_id?: string | null;
  transaction_id?: string | null;
  decision_snapshot?: string | null;
  created_at: string;
  expires_at: string;
}

/** Full read-only view of one session (GET /sessions/{session_id}). */
export interface SessionDetail {
  session_id: string;
  created_at: string;
  updated_at: string;
  turns: ConversationTurn[];
  pending_action?: PendingAction | null;
}
