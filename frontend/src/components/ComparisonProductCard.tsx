/**
 * One ranked product from POST /discovery/compare.
 *
 * Every value shown comes from the backend response. Nothing is inferred:
 * a missing image gets a neutral placeholder rather than a guessed one, a
 * missing brand is simply absent (never read out of the title), and the
 * merchant line comes from the backend's resolved merchant identity (see
 * MerchantLabel) — never from the link's host, which for a Google Shopping
 * result is Google rather than the shop.
 *
 * The action is "View Product", not "Buy Now": this opens the retailer's
 * own page. PayPilot does not own that checkout, and the label must not
 * imply otherwise.
 */

import { useState } from "react";
import { MerchantLabel } from "./MerchantLabel";
import { ProductPrice } from "./ProductPrice";
import type { ComparisonResult } from "../types";

/** Deterministic, backend-derived position labels. Deliberately NOT
 * "Best Value" / "Cheapest" / "Best Battery" — the backend supplies no
 * such classification, so claiming one would be invention. */
function rankLabel(rank: number): string {
  return rank === 1 ? "Top recommendation" : "Alternative";
}

function ImageGlyph() {
  return (
    <svg
      aria-hidden
      viewBox="0 0 24 24"
      className="h-8 w-8 text-zinc-300 dark:text-zinc-600"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <circle cx="8.5" cy="9.5" r="1.5" />
      <path d="m4 17 5-4.5 4 3.5 2.5-2L20 17" />
    </svg>
  );
}

/** Shown when the backend supplied no image, or the one it supplied
 * failed to load. Announced, because the absence is real information. */
function ImagePlaceholder() {
  return (
    <div
      role="img"
      aria-label="No product image available"
      className="flex h-full w-full items-center justify-center bg-zinc-100 dark:bg-zinc-800"
    >
      <ImageGlyph />
    </div>
  );
}

export interface ComparisonProductCardProps {
  result: ComparisonResult;
}

export function ComparisonProductCard({ result }: ComparisonProductCardProps) {
  const { product, rank, reason, price_note } = result;
  // A broken image URL falls back to the same neutral placeholder rather
  // than leaving a torn-image icon in the card.
  const [imageFailed, setImageFailed] = useState(false);
  const showImage = Boolean(product.image_url) && !imageFailed;

  return (
    <article className="flex min-w-0 flex-col overflow-hidden rounded-2xl border border-zinc-200 bg-white shadow-sm transition-shadow hover:shadow-md dark:border-zinc-800 dark:bg-zinc-900">
      <div className="flex items-center justify-between gap-2 border-b border-zinc-100 px-4 py-2.5 dark:border-zinc-800">
        <div className="flex min-w-0 items-center gap-2">
          <span
            className={`inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-xs font-bold ${
              rank === 1
                ? "bg-brand-500 text-white"
                : "bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300"
            }`}
          >
            {rank}
          </span>
          <span className="truncate text-xs font-medium text-zinc-500 dark:text-zinc-400">
            {rankLabel(rank)}
          </span>
        </div>
        {product.availability && (
          <span className="shrink-0 truncate rounded-full bg-zinc-100 px-2 py-0.5 text-[11px] font-medium text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300">
            {product.availability}
          </span>
        )}
      </div>

      <div className="relative aspect-[4/3] w-full overflow-hidden bg-zinc-50 dark:bg-zinc-800/50">
        {showImage ? (
          <>
            {/* Decorative resting state behind the image, so a slow or
                transparent remote image never leaves an empty void. */}
            <div
              aria-hidden
              className="absolute inset-0 flex items-center justify-center"
            >
              <ImageGlyph />
            </div>
            <img
              src={product.image_url ?? undefined}
              alt={product.title}
              loading="lazy"
              onError={() => setImageFailed(true)}
              className="relative h-full w-full object-contain"
            />
          </>
        ) : (
          <ImagePlaceholder />
        )}
      </div>

      <div className="flex min-w-0 flex-1 flex-col gap-3 p-4">
        <div className="min-w-0">
          {/* Long titles wrap and clamp rather than widening the card. */}
          <h3
            title={product.title}
            className="line-clamp-2 text-sm leading-snug font-semibold break-words text-zinc-900 dark:text-zinc-100"
          >
            {product.title}
          </h3>
          <div className="mt-1 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-zinc-500 dark:text-zinc-400">
            {/* Brand only when the backend supplied one. */}
            {product.brand && (
              <span className="truncate font-medium">{product.brand}</span>
            )}
            {product.brand && <span aria-hidden>·</span>}
            {/* Always rendered: who is selling this is information the user
                needs, and "we don't know" is an honest answer worth saying
                rather than an empty space. */}
            <MerchantLabel product={product} className="truncate" />
          </div>
        </div>

        <ProductPrice product={product} priceNote={price_note} />

        {/* Backend-authored explanation, rendered verbatim. The browser
            neither edits it nor generates one of its own. */}
        <p className="rounded-lg bg-zinc-50 px-3 py-2 text-xs leading-relaxed break-words text-zinc-600 dark:bg-zinc-800/60 dark:text-zinc-300">
          {reason}
        </p>

        <div className="mt-auto pt-1">
          {product.product_url ? (
            <a
              href={product.product_url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex w-full items-center justify-center gap-1.5 rounded-xl border border-zinc-200 bg-white px-4 py-2.5 text-sm font-semibold text-zinc-800 shadow-sm transition-colors hover:bg-zinc-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100 dark:hover:bg-zinc-800"
            >
              View Product
              <svg
                aria-hidden
                viewBox="0 0 16 16"
                className="h-3.5 w-3.5"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M6 3h7v7M13 3 6.5 9.5" />
                <path d="M11 11.5V13H3V5h1.5" />
              </svg>
            </a>
          ) : (
            // No URL from the provider: the action is disabled rather than
            // constructed or guessed.
            <button
              type="button"
              disabled
              className="w-full cursor-not-allowed rounded-xl border border-zinc-200 bg-zinc-50 px-4 py-2.5 text-sm font-medium text-zinc-400 dark:border-zinc-800 dark:bg-zinc-800/50 dark:text-zinc-500"
            >
              No product link available
            </button>
          )}
        </div>
      </div>
    </article>
  );
}
