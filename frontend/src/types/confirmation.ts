/** Mirrors backend `app/schemas/confirmation.py`. */

import type { ExecutionResult } from "./execution";
import type { PendingAction, PendingActionType } from "./session";
import type { RefundResult } from "./transaction";

export interface ConfirmRequest {
  /** true to execute the stored pending action; false to decline. */
  confirm: boolean;
}

/**
 * Outcome of one confirmation call. executed is true only when a PayPal
 * action actually happened (order created / refund completed).
 */
export interface ConfirmationResponse {
  session_id: string;
  action_type: PendingActionType;
  confirmed: boolean;
  executed: boolean;
  message: string;
  purchase_result?: ExecutionResult | null;
  refund_result?: RefundResult | null;
  pending_action?: PendingAction | null;
}
