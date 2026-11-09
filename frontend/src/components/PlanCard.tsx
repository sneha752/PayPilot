/**
 * Compact recommendation card for a SHOP plan. Every value shown —
 * product, price, specs, policy outcome — is the backend's authoritative
 * data; nothing is computed or decided here.
 */

import { inrFormatter } from "../lib/format";
import type { PlanResponse, PolicyDecision, Product } from "../types";

/** Human-readable spec lines from whichever catalog fields are present. */
function specLines(product: Product): string[] {
  const specs: string[] = [];
  if (product.ram_gb != null) specs.push(`${product.ram_gb} GB RAM`);
  if (product.storage_gb != null) specs.push(`${product.storage_gb} GB storage`);
  if (product.processor) specs.push(product.processor);
  if (product.gpu) specs.push(product.gpu);
  if (product.screen_size_in != null) specs.push(`${product.screen_size_in}" display`);
  if (product.camera_mp != null) specs.push(`${product.camera_mp} MP camera`);
  if (product.battery_mah != null) specs.push(`${product.battery_mah} mAh battery`);
  if (product.wireless != null) specs.push(product.wireless ? "Wireless" : "Wired");
  if (product.noise_cancelling) specs.push("Noise cancelling");
  if (product.battery_hours != null) specs.push(`${product.battery_hours} h battery`);
  if (product.form_factor) specs.push(product.form_factor);
  if (product.resolution) specs.push(product.resolution);
  if (product.refresh_rate_hz != null) specs.push(`${product.refresh_rate_hz} Hz`);
  if (product.panel_type) specs.push(`${product.panel_type} panel`);
  return specs.slice(0, 6);
}

const POLICY_BADGE: Record<
  PolicyDecision["decision"],
  { label: string; className: string }
> = {
  ALLOW_AUTONOMOUS_PURCHASE: {
    label: "Allowed",
    className:
      "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-900 dark:bg-emerald-950 dark:text-emerald-400",
  },
  REQUIRE_USER_APPROVAL: {
    label: "Approval required",
    className:
      "border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-400",
  },
  DENY: {
    label: "Denied",
    className:
      "border-red-200 bg-red-50 text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-400",
  },
};

export function PlanCard({ plan }: { plan: PlanResponse }) {
  const product = plan.selected_product;
  if (!product) return null;

  const specs = specLines(product);
  const policy = plan.policy_decision;
  const badge = policy ? POLICY_BADGE[policy.decision] : null;

  return (
    <div className="mt-3 overflow-hidden rounded-xl border border-zinc-200 bg-white shadow-sm dark:border-zinc-700 dark:bg-zinc-900">
      <div className="p-4">
        <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-1">
          <div className="min-w-0">
            <p className="text-xs font-medium uppercase tracking-wide text-zinc-400 dark:text-zinc-500">
              {product.brand} · {product.category}
            </p>
            <h3 className="mt-0.5 text-sm font-semibold text-zinc-900 dark:text-zinc-100">
              {product.name}
            </h3>
          </div>
          <div className="text-right">
            <p className="text-base font-semibold tabular-nums text-zinc-900 dark:text-zinc-100">
              {inrFormatter.format(product.price_inr)}
            </p>
            <p className="text-xs text-zinc-400 dark:text-zinc-500">
              ★ {product.rating}
            </p>
          </div>
        </div>

        {specs.length > 0 && (
          <ul className="mt-3 grid gap-x-4 gap-y-1 sm:grid-cols-2">
            {specs.map((spec) => (
              <li
                key={spec}
                className="flex items-center gap-1.5 text-xs text-zinc-600 dark:text-zinc-300"
              >
                <span aria-hidden className="text-emerald-500">
                  ✓
                </span>
                {spec}
              </li>
            ))}
          </ul>
        )}
      </div>

      {policy && badge && (
        <div className="border-t border-zinc-100 bg-zinc-50 px-4 py-3 dark:border-zinc-800 dark:bg-zinc-950/40">
          <div className="flex items-center gap-2">
            <span className="text-xs font-medium uppercase tracking-wide text-zinc-400 dark:text-zinc-500">
              Policy
            </span>
            <span
              className={`inline-flex rounded-full border px-2 py-0.5 text-xs font-medium ${badge.className}`}
            >
              {badge.label}
            </span>
          </div>
          <p className="mt-1.5 text-xs leading-relaxed text-zinc-500 dark:text-zinc-400">
            {policy.reason}
          </p>
        </div>
      )}
    </div>
  );
}
