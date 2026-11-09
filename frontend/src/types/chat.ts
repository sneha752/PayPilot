/** Mirrors backend `app/schemas/chat.py` and `app/schemas/routing.py`. */

import type { PlanResponse } from "./plan";
import type { PendingAction } from "./session";
import type { TransactionRecord } from "./transaction";

/** The only four flows a conversational message can be routed to. */
export type ChatRoute = "SHOP" | "REFUND" | "HISTORY" | "SMALLTALK";

export interface ChatRequest {
  session_id?: string | null;
  message: string;
}

/**
 * Typed per-route payload. Exactly the fields the route produced are set:
 * SHOP -> plan; REFUND -> transaction or transactions; HISTORY ->
 * transactions; SMALLTALK -> nothing.
 */
export interface ChatData {
  plan?: PlanResponse | null;
  transaction?: TransactionRecord | null;
  transactions?: TransactionRecord[] | null;
}

/**
 * Structured result of one chat turn. pending_action is the session's
 * ACTIVE pending action after this turn; execution always requires the
 * explicit confirmation endpoint.
 */
export interface ChatResponse {
  session_id: string;
  route: ChatRoute;
  assistant_message: string;
  pending_action?: PendingAction | null;
  data: ChatData;
}
