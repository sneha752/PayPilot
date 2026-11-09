/** Mirrors backend `app/schemas/plan.py`. */

import type { Product } from "./product";
import type { Authorization, ProductIntent } from "./intent";
import type { PolicyDecision } from "./policy";
import type { Recommendation } from "./recommendation";

export type PlanStatus = "planned" | "no_candidates" | "no_suitable_candidate";

/**
 * The full decision pipeline for one request. A plan is a DECISION only —
 * it never carries a payment artifact.
 */
export interface PlanResponse {
  status: PlanStatus;

  // Pipeline trace.
  intent: ProductIntent;
  candidates: Product[];
  recommendation?: Recommendation | null;

  // Product information — catalog-exact.
  selected_product?: Product | null;

  // User authorization — inert facts, echoed for transparency.
  user_authorization: Authorization;

  // Policy decision — None when no product was selected.
  policy_decision?: PolicyDecision | null;
}
