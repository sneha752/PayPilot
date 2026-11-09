/**
 * Who is actually selling this product.
 *
 * One rule, in one place, used by both the cards and the table so the two
 * can never disagree:
 *
 *   merchant_name    ->  "Amazon"                (what the provider called
 *                                                 the seller)
 *   merchant_domain  ->  "bestbuy.com"           (the backend's derived host,
 *                                                 when no name was reported)
 *   neither          ->  "Merchant unavailable"  (said plainly)
 *
 * WHY NOT retailer_domain. retailer_domain is the host of the link the
 * provider gave us, and for a Serper (Google Shopping) result that host is
 * www.google.com even when Amazon is the seller. Showing it as the merchant
 * would tell the user Google is selling the product. merchant_domain is the
 * backend's derived answer to the same question and is deliberately null
 * for an aggregator host, so Google can never land here.
 *
 * NOTHING IS DERIVED HERE. This file does not parse a URL, read a host out
 * of product_url, fall back to `source` (that is the discovery provider, not
 * a shop), borrow the brand, or map a name to a domain. When the backend
 * says it does not know, so does the UI.
 */

import type { NormalizedDiscoveredProduct } from "../types";

/** Shown when the backend reported no merchant name and could derive no
 * merchant domain. Neutral and honest: never a provider name, a brand, a
 * guessed retailer, or a bare dash that reads as "none". */
export const MERCHANT_UNAVAILABLE = "Merchant unavailable";

/**
 * The merchant string to display, and whether it is real information.
 *
 * `known` is false for the fallback, so callers can style it as absent
 * rather than as a seller's name.
 */
export function merchantLabel(product: NormalizedDiscoveredProduct): {
  label: string;
  known: boolean;
} {
  const name = product.merchant_name?.trim();
  if (name) return { label: name, known: true };

  const domain = product.merchant_domain?.trim();
  if (domain) return { label: domain, known: true };

  return { label: MERCHANT_UNAVAILABLE, known: false };
}

export interface MerchantLabelProps {
  product: NormalizedDiscoveredProduct;
  /** Extra classes from the surrounding layout (truncation, colour). */
  className?: string;
}

export function MerchantLabel({ product, className = "" }: MerchantLabelProps) {
  const { label, known } = merchantLabel(product);

  return (
    <span
      // The full value on hover, since the layout truncates long hosts.
      title={known ? label : undefined}
      data-merchant-known={known}
      className={`${known ? "" : "italic text-zinc-400 dark:text-zinc-500"} ${className}`.trim()}
    >
      {label}
    </span>
  );
}
