/** Typed fixtures mirroring real backend payloads for frontend tests. */

import type {
  CaptureResult,
  ComparisonRequirements,
  ComparisonResponse,
  ComparisonResult,
  ConfirmationResponse,
  NormalizedDiscoveredProduct,
  ExecutionResult,
  PendingAction,
  PlanResponse,
  Product,
  RefundResult,
  TransactionRecord,
} from "../types";

export function makeProduct(overrides: Partial<Product> = {}): Product {
  return {
    id: "HDP006",
    name: "boAt Rockerz 450",
    category: "headphones",
    brand: "boAt",
    price_inr: 1499,
    rating: 3.9,
    description: "Wireless on-ear headphones.",
    wireless: true,
    battery_hours: 15,
    ...overrides,
  };
}

export function makePlan(overrides: Partial<PlanResponse> = {}): PlanResponse {
  const product = makeProduct();
  return {
    status: "planned",
    intent: {
      category: "headphones",
      keywords: [],
      use_case: null,
      quantity: 1,
      hard_constraints: { required_features: [] },
      preferences: { preferred_features: [] },
      excluded_features: [],
      authorization: {
        purchase_requested: true,
        autonomous_purchase_allowed: false,
        auto_approve_below_inr: null,
      },
    },
    candidates: [product],
    recommendation: {
      recommended_product_id: product.id,
      reasoning: "Best fit.",
      matched_constraints: [],
      tradeoffs: [],
    },
    selected_product: product,
    user_authorization: {
      purchase_requested: true,
      autonomous_purchase_allowed: false,
      auto_approve_below_inr: null,
    },
    policy_decision: {
      decision: "REQUIRE_USER_APPROVAL",
      reason_code: "AUTONOMY_NOT_GRANTED",
      reason:
        "A purchase was requested, but autonomous purchasing was not authorized.",
      product_id: product.id,
      product_price_inr: product.price_inr,
      currency: "INR",
      shopping_max_inr: null,
      authorization_limit_inr: null,
    },
    ...overrides,
  };
}

export function makePendingAction(
  overrides: Partial<PendingAction> = {},
): PendingAction {
  return {
    action_type: "PURCHASE",
    product_id: "HDP006",
    transaction_id: null,
    decision_snapshot: null,
    created_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 10 * 60 * 1000).toISOString(),
    ...overrides,
  };
}

export function makeExecutionResult(
  overrides: Partial<ExecutionResult> = {},
): ExecutionResult {
  return {
    status: "order_created",
    approval_used: true,
    product_id: "HDP006",
    product_name: "boAt Rockerz 450",
    catalog_price_inr: 1499,
    policy_decision: makePlan().policy_decision!,
    paypal_currency: "USD",
    paypal_amount: "18.07",
    paypal_order_id: "5O190127TN364715T",
    approve_url: "https://www.sandbox.paypal.com/checkoutnow?token=5O190127TN364715T",
    transaction_id: "txn_1",
    ...overrides,
  };
}

export function makeCaptureResult(
  overrides: Partial<CaptureResult> = {},
): CaptureResult {
  return {
    status: "captured",
    order_id: "5O190127TN364715T",
    order_status: "COMPLETED",
    capture_id: "3C679366HH908993F",
    capture_status: "COMPLETED",
    amount: "18.07",
    currency: "USD",
    transaction_id: "txn_1",
    transaction_status: "CAPTURED",
    ...overrides,
  };
}

export function makeTransactionRecord(
  overrides: Partial<TransactionRecord> = {},
): TransactionRecord {
  return {
    transaction_id: "txn_1",
    product_id: "HDP006",
    product_name: "boAt Rockerz 450",
    catalog_price_inr: 1499,
    paypal_amount: "18.07",
    paypal_currency: "USD",
    paypal_order_id: "5O190127TN364715T",
    paypal_capture_id: "3C679366HH908993F",
    status: "CAPTURED",
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    ...overrides,
  };
}

export function makeRefundResult(
  overrides: Partial<RefundResult> = {},
): RefundResult {
  return {
    status: "refunded",
    transaction_id: "txn_1",
    paypal_order_id: "5O190127TN364715T",
    paypal_capture_id: "3C679366HH908993F",
    refund_id: "1JU08902781691411",
    refund_status: "COMPLETED",
    amount: "18.07",
    currency: "USD",
    ...overrides,
  };
}

export function makeConfirmation(
  overrides: Partial<ConfirmationResponse> = {},
): ConfirmationResponse {
  return {
    session_id: "sess_1",
    action_type: "PURCHASE",
    confirmed: true,
    executed: true,
    message:
      "Purchase confirmed: a PayPal order for boAt Rockerz 450 was created.",
    purchase_result: makeExecutionResult(),
    refund_result: null,
    pending_action: null,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Discovery comparison (Phase 10G)
//
// These mirror real POST /discovery/compare payloads, including the fact
// that every monetary value arrives as an exact decimal STRING. They exist
// for tests only: the production UI renders nothing but live backend data.
// ---------------------------------------------------------------------------

export function makeDiscoveredProduct(
  overrides: Partial<NormalizedDiscoveredProduct> = {},
): NormalizedDiscoveredProduct {
  return {
    external_id: "serper:111111",
    source: "serper",
    title: "Sony WH-CH520 Wireless Headphones",
    description: "On-ear wireless headphones.",
    brand: null,
    category: null,
    image_url: "https://img.example/ch520.jpg",
    product_url: "https://www.bestbuy.com/site/sony-ch520",
    retailer_domain: "www.bestbuy.com",
    availability: "InStock",
    observed_price: "19.99",
    observed_currency: "USD",
    observed_at: "2026-01-01T12:00:00Z",
    normalized_price: "1824.09",
    normalized_currency: "INR",
    conversion_rate: "91.25",
    conversion_source: "configured_demo_rate",
    rate_observed_at: null,
    // Phase 10H. The default mirrors a direct-merchant-link result: the
    // provider named the shop AND the backend could derive its host. Tests
    // override one or both to cover the Google-Shopping case (a name with
    // no derivable domain) and the unknown-merchant case.
    merchant_name: "Best Buy",
    merchant_domain: "bestbuy.com",
    ...overrides,
  };
}

export function makeComparisonResult(
  overrides: Partial<ComparisonResult> = {},
): ComparisonResult {
  return {
    rank: 1,
    product: makeDiscoveredProduct(),
    reason:
      "Best match among the returned candidates while remaining within the budget.",
    price_note:
      "Observed USD 19.99 converts to a derived INR 1,824.09 at a configured demonstration rate of 91.25 INR per USD.",
    ...overrides,
  };
}

export function makeComparisonRequirements(
  overrides: Partial<ComparisonRequirements> = {},
): ComparisonRequirements {
  return {
    category: null,
    brand: null,
    max_price: "3000",
    min_price: null,
    price_currency: "INR",
    required_keywords: [],
    excluded_keywords: [],
    preferred_brand: null,
    preferred_keywords: [],
    use_case: null,
    ...overrides,
  };
}

export function makeComparisonResponse(
  overrides: Partial<ComparisonResponse> = {},
): ComparisonResponse {
  const results = overrides.results ?? [
    makeComparisonResult(),
    makeComparisonResult({
      rank: 2,
      reason: "Lower observed price among the returned candidates.",
      product: makeDiscoveredProduct({
        external_id: "serper:222222",
        title: "JBL Tune 510BT Wireless Headphones",
        observed_price: "24.50",
        normalized_price: "2235.63",
        product_url: "https://www.walmart.com/ip/jbl-510bt",
        retailer_domain: "www.walmart.com",
        merchant_name: "Walmart",
        merchant_domain: "walmart.com",
      }),
    }),
    makeComparisonResult({
      rank: 3,
      reason: "Matches the requested category among the returned candidates.",
      product: makeDiscoveredProduct({
        external_id: "serper:333333",
        title: "boAt Rockerz 450 Wireless Headphones",
        observed_price: "29.00",
        normalized_price: "2646.25",
        product_url: "https://www.flipkart.com/boat-rockerz-450",
        retailer_domain: "www.flipkart.com",
        merchant_name: "Flipkart",
        merchant_domain: "flipkart.com",
      }),
    }),
  ];
  return {
    query: "wireless headphones under ₹3000",
    requirements: makeComparisonRequirements(),
    ranking_method: "ai",
    ranking_provider: "groq",
    discovery_source: "serper",
    normalization_currency: "INR",
    discovered_count: 10,
    candidates_considered: 4,
    ...overrides,
    results,
  };
}
