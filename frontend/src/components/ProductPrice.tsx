/**
 * Price presentation for a discovered product.
 *
 * THE FRONTEND NEVER CONVERTS CURRENCY. There is no arithmetic in this
 * file. The observed price is what a retailer was seen asking; the INR
 * figure is a value the BACKEND derived and sent. Both are rendered from
 * their own backend fields, and the derived one is always labelled as an
 * estimate so it cannot be read as a retailer's price.
 *
 * Fallback order, with no invented values:
 *   observed + derived -> show both, derived marked "est."
 *   observed only      -> show the observed price alone
 *   neither            -> "Price unavailable" (never 0, never ₹0)
 */

import { formatMoney } from "../lib/money";
import { CONFIGURED_DEMO_RATE } from "../types";
import type { NormalizedDiscoveredProduct } from "../types";

/** Shown when a derived figure used the configured demonstration rate. It
 * is not a live market rate and must not be described as one. */
export const DEMO_RATE_NOTE =
  "INR estimate uses PayPilot's configured demo exchange rate.";

export interface ProductPriceProps {
  product: NormalizedDiscoveredProduct;
  /** Backend-assembled conversion sentence, surfaced on hover. */
  priceNote?: string | null;
  /** Compact single-line rendering for the comparison table. */
  compact?: boolean;
}

export function ProductPrice({
  product,
  priceNote,
  compact = false,
}: ProductPriceProps) {
  const observed = formatMoney(product.observed_price, product.observed_currency);
  const derived = formatMoney(
    product.normalized_price,
    product.normalized_currency,
  );

  // The derived figure is only worth showing when it is a different
  // currency from the observation; an identity "conversion" would just
  // repeat the same number.
  const showDerived =
    derived !== null &&
    observed !== null &&
    product.normalized_currency?.toUpperCase() !==
      product.observed_currency?.toUpperCase();

  const usesDemoRate = product.conversion_source === CONFIGURED_DEMO_RATE;

  if (observed === null && derived === null) {
    return (
      <p className="text-sm text-zinc-500 dark:text-zinc-400">
        Price unavailable
      </p>
    );
  }

  if (compact) {
    return (
      <div className="flex flex-col gap-0.5">
        <span className="font-semibold text-zinc-900 tabular-nums dark:text-zinc-100">
          {observed ?? derived}
        </span>
        {showDerived && (
          <span
            title={priceNote ?? undefined}
            className="text-xs text-zinc-500 tabular-nums dark:text-zinc-400"
          >
            {"≈ "}
            {derived} est.
          </span>
        )}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="text-xl font-semibold text-zinc-900 tabular-nums dark:text-zinc-100">
          {observed ?? derived}
        </span>
        {observed !== null && (
          <span className="text-[11px] font-medium tracking-wide text-zinc-400 uppercase dark:text-zinc-500">
            observed
          </span>
        )}
      </div>

      {showDerived && (
        <div className="flex flex-wrap items-baseline gap-x-2">
          <span
            title={priceNote ?? undefined}
            className="text-sm font-medium text-zinc-600 tabular-nums dark:text-zinc-300"
          >
            {"≈ "}
            {derived}
          </span>
          <span className="text-[11px] font-medium tracking-wide text-zinc-400 uppercase dark:text-zinc-500">
            estimate
          </span>
        </div>
      )}

      {showDerived && usesDemoRate && (
        <p className="text-[11px] leading-snug text-zinc-400 dark:text-zinc-500">
          {DEMO_RATE_NOTE}
        </p>
      )}
    </div>
  );
}
