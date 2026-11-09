/** Mirrors backend `app/schemas/intent.py`. */

export interface HardConstraints {
  max_price_inr?: number | null;
  min_price_inr?: number | null;
  min_rating?: number | null;
  ram_gb_min?: number | null;
  storage_gb_min?: number | null;
  required_features: string[];
}

export interface Preferences {
  brand?: string | null;
  gpu?: string | null;
  preferred_features: string[];
}

/**
 * Financial authorization the user expressed — kept separate from shopping
 * constraints. Inert data: only the backend Policy Engine acts on it.
 */
export interface Authorization {
  purchase_requested: boolean;
  autonomous_purchase_allowed: boolean;
  auto_approve_below_inr?: number | null;
}

export interface ProductIntent {
  category?: string | null;
  keywords: string[];
  use_case?: string | null;
  quantity: number;
  hard_constraints: HardConstraints;
  preferences: Preferences;
  excluded_features: string[];
  authorization: Authorization;
}
