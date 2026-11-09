/**
 * Mirrors backend `app/schemas/comparison.py` and
 * `app/schemas/discovery.py::NormalizedDiscoveredProduct`.
 *
 * MONEY IS A STRING, DELIBERATELY.
 * The backend models prices as Python Decimal and serializes them as exact
 * strings ("34.99", "3192.84", "91.25"). They are kept as `string` here —
 * never `number` — for two reasons:
 *
 *   1. A string cannot be silently arithmetic'd. There is no way to add,
 *      multiply, or convert these values by accident, which is exactly the
 *      guarantee this feature needs: the frontend performs NO currency
 *      conversion and no price math. It renders what the backend computed.
 *   2. Parsing to a JS number would introduce binary float error into
 *      values the backend was careful to keep exact.
 *
 * OBSERVED vs DERIVED.
 *   observed_price / observed_currency are what a retailer was seen asking.
 *   normalized_price / normalized_currency are a value PayPilot DERIVED
 *   using conversion_rate. They are not a retailer's price and must never
 *   be presented as one. All derived fields are null together when no
 *   deterministic conversion was possible.
 *
 * MERCHANT IDENTITY (Phase 10H).
 *   merchant_name is the seller the discovery provider named. merchant_domain
 *   is a host the BACKEND derived, and it is null whenever the product's link
 *   is a search-aggregator URL — a Google Shopping link does not make Google
 *   the merchant. The browser does not derive either one: it must never read
 *   a merchant out of product_url, retailer_domain, brand, or the title, and
 *   it must never turn a name into a domain.
 *
 * Never invent a field here. If the backend does not send it, the UI does
 * not show it.
 */

/** How the ordering was produced. `ai` only when a validated model ranking
 * was used; a deterministic fallback is always `deterministic`. */
export type RankingMethod = "ai" | "deterministic";

/** Backend value of `conversion_source` for the configured demo rate.
 * Used to decide whether to show the demo-rate transparency note. */
export const CONFIGURED_DEMO_RATE = "configured_demo_rate";

/** One discovered product plus any PayPilot-derived price. */
export interface NormalizedDiscoveredProduct {
  // --- provider evidence (Phase 10C/10D) ---
  external_id: string;
  source: string;
  title: string;
  description: string | null;
  brand: string | null;
  category: string | null;
  image_url: string | null;
  product_url: string | null;
  retailer_domain: string | null;
  availability: string | null;
  /** Exact decimal string, e.g. "34.99". Never a number. */
  observed_price: string | null;
  observed_currency: string | null;
  /** Server-side ISO timestamp for when the offer was observed. */
  observed_at: string;

  // --- PayPilot-derived (Phase 10F); all null together ---
  /** Exact decimal string the BACKEND computed. Not a retailer price. */
  normalized_price: string | null;
  normalized_currency: string | null;
  conversion_rate: string | null;
  /** e.g. "configured_demo_rate" (a demonstration constant, not a live
   * market quote) or "identity". */
  conversion_source: string | null;
  /** When a live rate was obtained; null for a configured rate. */
  rate_observed_at: string | null;

  // --- PayPilot-derived merchant identity (Phase 10H) ---
  /** The seller as the provider named it, e.g. "Amazon". Null when the
   * provider named no merchant. Independent of merchant_domain. */
  merchant_name: string | null;
  /** Backend-derived merchant host, e.g. "bestbuy.com". Null when the
   * product's link only resolves to a search aggregator (Google Shopping),
   * or when there is no usable link at all. Never derived from a name. */
  merchant_domain: string | null;
}

/** One ranked entry. `reason` and `price_note` are backend-authored text. */
export interface ComparisonResult {
  rank: number;
  product: NormalizedDiscoveredProduct;
  /** The ranking component's explanation. Rendered verbatim. */
  reason: string;
  /** A backend-ASSEMBLED sentence describing the conversion, when one
   * happened. Never produced in the browser. */
  price_note: string | null;
}

/** The structured requirements the backend filtered on. Echoed for
 * transparency; the UI never filters or compares with these itself. */
export interface ComparisonRequirements {
  category: string | null;
  brand: string | null;
  /** Exact decimal strings. */
  max_price: string | null;
  min_price: string | null;
  price_currency: string | null;
  required_keywords: string[];
  excluded_keywords: string[];
  preferred_brand: string | null;
  preferred_keywords: string[];
  use_case: string | null;
}

/** Response body of POST /discovery/compare. */
export interface ComparisonResponse {
  query: string;
  requirements: ComparisonRequirements;
  ranking_method: RankingMethod;
  /** AI provider name when ranking_method is "ai"; null otherwise. */
  ranking_provider: string | null;
  /** Discovery provider the candidates came from. */
  discovery_source: string | null;
  /** Currency the backend derived normalized prices in. */
  normalization_currency: string | null;
  discovered_count: number;
  candidates_considered: number;
  results: ComparisonResult[];
}

/**
 * Request body for POST /discovery/compare.
 *
 * The browser sends a natural-language query and two bounded counts —
 * nothing else. There is deliberately no field for a price, a currency, an
 * exchange rate, a product id, a PayPal/order/capture/refund id, or a
 * payment amount. The backend performs its own discovery, derives its own
 * requirements from the query, and owns every monetary value.
 */
export interface ComparisonRequest {
  query: string;
  /** Discovery results for the backend to fetch (backend bound: 1-30). */
  limit?: number;
  /** Ranked results to return (backend bound: 1-5). */
  top_k?: number;
}
