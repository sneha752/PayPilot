/**
 * Side-by-side comparison of the ranked results.
 *
 * A real <table> on desktop (so it reads correctly to screen readers and
 * scales with content) and stacked definition rows on mobile, where a
 * seven-column table could only overflow. Both render the same
 * backend-provided fields; neither computes anything.
 *
 * Columns with no data in any row are dropped rather than filled with
 * placeholder dashes everywhere, and no specification is invented.
 *
 * The Merchant column reports the backend's resolved merchant identity, not
 * the link's host: a Serper result links through Google Shopping, so its
 * host is Google even when the seller is Amazon. See MerchantLabel.
 */

import { MerchantLabel, merchantLabel } from "./MerchantLabel";
import { ProductPrice } from "./ProductPrice";
import type { ComparisonResult } from "../types";

export interface ComparisonTableProps {
  results: ComparisonResult[];
}

export function ComparisonTable({ results }: ComparisonTableProps) {
  if (results.length === 0) return null;

  const showAvailability = results.some((r) => r.product.availability);
  // The Merchant column appears once ANY row has a known merchant. When no
  // row does, a column of "Merchant unavailable" would add nothing, so it
  // is dropped like every other empty column.
  const showMerchant = results.some((r) => merchantLabel(r.product).known);

  return (
    <section className="min-w-0">
      <h3 className="mb-3 text-sm font-semibold text-zinc-900 dark:text-zinc-100">
        Side-by-side
      </h3>

      {/* Desktop / tablet */}
      <div className="hidden overflow-hidden rounded-2xl border border-zinc-200 bg-white shadow-sm md:block dark:border-zinc-800 dark:bg-zinc-900">
        <table className="w-full table-fixed border-collapse text-left text-sm">
          <caption className="sr-only">
            Comparison of the ranked products returned by PayPilot
          </caption>
          <thead>
            <tr className="border-b border-zinc-200 bg-zinc-50 text-[11px] font-semibold tracking-wide text-zinc-500 uppercase dark:border-zinc-800 dark:bg-zinc-800/50 dark:text-zinc-400">
              <th scope="col" className="w-12 px-3 py-2.5">
                #
              </th>
              <th scope="col" className="w-[30%] px-3 py-2.5">
                Product
              </th>
              <th scope="col" className="w-[18%] px-3 py-2.5">
                Price
              </th>
              {showMerchant && (
                <th scope="col" className="w-[15%] px-3 py-2.5">
                  Merchant
                </th>
              )}
              {showAvailability && (
                <th scope="col" className="w-[12%] px-3 py-2.5">
                  Availability
                </th>
              )}
              <th scope="col" className="px-3 py-2.5">
                Why
              </th>
            </tr>
          </thead>
          <tbody>
            {results.map((result) => (
              <tr
                key={result.product.external_id}
                className="border-b border-zinc-100 align-top last:border-0 dark:border-zinc-800"
              >
                <td className="px-3 py-3 font-bold text-zinc-400 tabular-nums dark:text-zinc-500">
                  {result.rank}
                </td>
                <td className="px-3 py-3">
                  <span className="line-clamp-3 font-medium break-words text-zinc-900 dark:text-zinc-100">
                    {result.product.title}
                  </span>
                </td>
                <td className="px-3 py-3">
                  <ProductPrice
                    product={result.product}
                    priceNote={result.price_note}
                    compact
                  />
                </td>
                {showMerchant && (
                  <td className="px-3 py-3 break-words text-zinc-600 dark:text-zinc-300">
                    <MerchantLabel product={result.product} />
                  </td>
                )}
                {showAvailability && (
                  <td className="px-3 py-3 break-words text-zinc-600 dark:text-zinc-300">
                    {result.product.availability ?? "—"}
                  </td>
                )}
                <td className="px-3 py-3 text-xs leading-relaxed break-words text-zinc-600 dark:text-zinc-300">
                  {result.reason}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Mobile: stacked rows, no horizontal scroll */}
      <ul className="flex flex-col gap-3 md:hidden">
        {results.map((result) => (
          <li
            key={result.product.external_id}
            className="min-w-0 rounded-2xl border border-zinc-200 bg-white p-4 shadow-sm dark:border-zinc-800 dark:bg-zinc-900"
          >
            <div className="flex min-w-0 items-start gap-2">
              <span className="shrink-0 text-xs font-bold text-zinc-400 tabular-nums dark:text-zinc-500">
                #{result.rank}
              </span>
              <span className="min-w-0 flex-1 text-sm font-medium break-words text-zinc-900 dark:text-zinc-100">
                {result.product.title}
              </span>
            </div>
            <dl className="mt-3 flex flex-col gap-2 text-xs">
              <div className="flex min-w-0 items-start justify-between gap-3">
                <dt className="shrink-0 text-zinc-500 dark:text-zinc-400">Price</dt>
                <dd className="min-w-0 text-right">
                  <ProductPrice
                    product={result.product}
                    priceNote={result.price_note}
                    compact
                  />
                </dd>
              </div>
              {showMerchant && (
                <div className="flex min-w-0 items-start justify-between gap-3">
                  <dt className="shrink-0 text-zinc-500 dark:text-zinc-400">
                    Merchant
                  </dt>
                  <dd className="min-w-0 text-right break-words text-zinc-700 dark:text-zinc-200">
                    <MerchantLabel product={result.product} />
                  </dd>
                </div>
              )}
              {showAvailability && (
                <div className="flex min-w-0 items-start justify-between gap-3">
                  <dt className="shrink-0 text-zinc-500 dark:text-zinc-400">
                    Availability
                  </dt>
                  <dd className="min-w-0 text-right break-words text-zinc-700 dark:text-zinc-200">
                    {result.product.availability ?? "—"}
                  </dd>
                </div>
              )}
              <div className="min-w-0">
                <dt className="text-zinc-500 dark:text-zinc-400">Why</dt>
                <dd className="mt-0.5 leading-relaxed break-words text-zinc-600 dark:text-zinc-300">
                  {result.reason}
                </dd>
              </div>
            </dl>
          </li>
        ))}
      </ul>
    </section>
  );
}
