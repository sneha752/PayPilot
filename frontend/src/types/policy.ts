/** Mirrors backend `app/schemas/policy.py`. */

export type PolicyOutcome =
  | "ALLOW_AUTONOMOUS_PURCHASE"
  | "REQUIRE_USER_APPROVAL"
  | "DENY";

/**
 * Stable, machine-readable reason codes. Frozen on the backend — UI maps
 * off these strings. New codes may be added over time, so consumers should
 * handle unknown values gracefully.
 */
export type ReasonCode =
  // DENY codes.
  | "INVALID_PRODUCT_PRICE"
  | "UNSUPPORTED_CURRENCY"
  | "NO_PURCHASE_REQUESTED"
  | "EXCEEDS_SHOPPING_MAX"
  | "BELOW_SHOPPING_MIN"
  // REQUIRE_USER_APPROVAL codes.
  | "AUTONOMY_NOT_GRANTED"
  | "NO_AUTONOMOUS_LIMIT"
  | "EXCEEDS_AUTONOMOUS_LIMIT"
  // ALLOW codes.
  | "WITHIN_AUTONOMOUS_LIMIT";

export interface PolicyDecision {
  decision: PolicyOutcome;
  reason_code: ReasonCode;
  reason: string;

  // Echoed facts the decision was based on, for auditability.
  product_id: string;
  product_price_inr?: number | null;
  currency: string;
  shopping_max_inr?: number | null;
  authorization_limit_inr?: number | null;
}
