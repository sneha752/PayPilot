/** Mirrors backend `app/schemas/recommendation.py`. */

import type { Product } from "./product";
import type { ProductIntent } from "./intent";

export interface Recommendation {
  recommended_product_id?: string | null;
  reasoning: string;
  matched_constraints: string[];
  tradeoffs: string[];
}

export type RecommendStatus =
  | "recommended"
  | "no_candidates"
  | "no_suitable_candidate";

export interface RecommendResponse {
  status: RecommendStatus;
  intent: ProductIntent;
  candidates: Product[];
  recommendation?: Recommendation | null;
  recommended_product?: Product | null;
}
