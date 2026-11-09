/**
 * Merchant identity in the UI (Phase 10H).
 *
 * The property under test is an honesty one: the surface names the seller
 * the backend resolved, says so plainly when there is none, and never
 * presents Google — which merely hosts a Serper result's link — as the shop.
 *
 * The API service is mocked throughout, so no backend, discovery provider or
 * AI request occurs.
 */

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  MERCHANT_UNAVAILABLE,
  MerchantLabel,
  merchantLabel,
} from "../MerchantLabel";
import { ComparisonTable } from "../ComparisonTable";
import { ShopPage } from "../../pages/ShopPage";
import {
  makeComparisonResponse,
  makeComparisonResult,
  makeDiscoveredProduct,
} from "../../test/fixtures";

vi.mock("../../services/api", () => ({
  compareProducts: vi.fn(),
}));

import { compareProducts } from "../../services/api";

const compareMock = vi.mocked(compareProducts);

const GOOGLE_LINK = "https://www.google.com/shopping/product/1";

beforeEach(() => {
  vi.clearAllMocks();
});

/** A Serper result as it really arrives: named shop, Google-hosted link. */
function googleHostedAmazonProduct(overrides = {}) {
  return makeDiscoveredProduct({
    source: "serper",
    merchant_name: "Amazon",
    merchant_domain: null,
    product_url: GOOGLE_LINK,
    retailer_domain: "www.google.com",
    ...overrides,
  });
}

async function searchWith(response: ReturnType<typeof makeComparisonResponse>) {
  compareMock.mockResolvedValue(response);
  const user = userEvent.setup();
  render(<ShopPage />);
  await user.type(screen.getByRole("searchbox"), "wireless headphones");
  await user.click(screen.getByRole("button", { name: /compare/i }));
  return user;
}

// ---------------------------------------------------------------------------
// merchantLabel: the single display rule
// ---------------------------------------------------------------------------

describe("merchantLabel", () => {
  it("prefers the provider-reported merchant name", () => {
    const product = makeDiscoveredProduct({
      merchant_name: "Amazon",
      merchant_domain: "bestbuy.com",
    });

    expect(merchantLabel(product)).toEqual({ label: "Amazon", known: true });
  });

  it("falls back to the backend-derived merchant domain", () => {
    const product = makeDiscoveredProduct({
      merchant_name: null,
      merchant_domain: "bestbuy.com",
    });

    expect(merchantLabel(product)).toEqual({ label: "bestbuy.com", known: true });
  });

  it("reports the neutral fallback when the backend knows neither", () => {
    const product = makeDiscoveredProduct({
      merchant_name: null,
      merchant_domain: null,
    });

    expect(merchantLabel(product)).toEqual({
      label: MERCHANT_UNAVAILABLE,
      known: false,
    });
  });

  it("treats a blank merchant name as absent", () => {
    const product = makeDiscoveredProduct({
      merchant_name: "   ",
      merchant_domain: "bestbuy.com",
    });

    expect(merchantLabel(product)).toEqual({ label: "bestbuy.com", known: true });
  });

  it("never falls back to the link host, the provider, or the brand", () => {
    const product = makeDiscoveredProduct({
      merchant_name: null,
      merchant_domain: null,
      source: "serper",
      brand: "Sony",
      retailer_domain: "www.google.com",
      product_url: GOOGLE_LINK,
    });

    const { label } = merchantLabel(product);

    expect(label).toBe(MERCHANT_UNAVAILABLE);
    expect(label).not.toContain("google");
    expect(label).not.toContain("serper");
    expect(label).not.toContain("Sony");
  });

  it("does not turn a merchant name into a domain", () => {
    const product = makeDiscoveredProduct({
      merchant_name: "Amazon",
      merchant_domain: null,
    });

    const { label } = merchantLabel(product);

    expect(label).toBe("Amazon");
    expect(label).not.toMatch(/\.(com|in|co\.uk)$/);
  });
});

// ---------------------------------------------------------------------------
// MerchantLabel component
// ---------------------------------------------------------------------------

describe("MerchantLabel", () => {
  it("marks a known merchant as known and titles it for hover", () => {
    render(<MerchantLabel product={makeDiscoveredProduct()} />);

    const label = screen.getByText("Best Buy");
    expect(label).toHaveAttribute("data-merchant-known", "true");
    expect(label).toHaveAttribute("title", "Best Buy");
  });

  it("styles the fallback as absent rather than as a seller's name", () => {
    render(
      <MerchantLabel
        product={makeDiscoveredProduct({
          merchant_name: null,
          merchant_domain: null,
        })}
      />,
    );

    const label = screen.getByText(MERCHANT_UNAVAILABLE);
    expect(label).toHaveAttribute("data-merchant-known", "false");
    expect(label).not.toHaveAttribute("title");
    expect(label.className).toMatch(/italic/);
  });
});

// ---------------------------------------------------------------------------
// Product cards
// ---------------------------------------------------------------------------

describe("merchant identity on comparison cards", () => {
  it("shows the merchant name when the provider reported one", async () => {
    await searchWith(
      makeComparisonResponse({
        results: [
          makeComparisonResult({ product: googleHostedAmazonProduct() }),
        ],
      }),
    );

    const card = (await screen.findAllByRole("article"))[0];
    expect(within(card).getByText("Amazon")).toBeInTheDocument();
  });

  it("never shows Google as the merchant for a Google Shopping link", async () => {
    await searchWith(
      makeComparisonResponse({
        results: [
          makeComparisonResult({ product: googleHostedAmazonProduct() }),
        ],
      }),
    );

    const card = (await screen.findAllByRole("article"))[0];
    expect(within(card).queryByText(/google/i)).not.toBeInTheDocument();
  });

  it("shows the derived domain when no merchant name was reported", async () => {
    await searchWith(
      makeComparisonResponse({
        results: [
          makeComparisonResult({
            // The Channel3 shape: a real retailer host, no store name.
            product: makeDiscoveredProduct({
              source: "channel3",
              merchant_name: null,
              merchant_domain: "bestbuy.com",
            }),
          }),
        ],
      }),
    );

    const card = (await screen.findAllByRole("article"))[0];
    expect(within(card).getByText("bestbuy.com")).toBeInTheDocument();
  });

  it("shows the neutral fallback when merchant identity is unavailable", async () => {
    await searchWith(
      makeComparisonResponse({
        results: [
          makeComparisonResult({
            product: googleHostedAmazonProduct({ merchant_name: null }),
          }),
        ],
      }),
    );

    const card = (await screen.findAllByRole("article"))[0];
    expect(within(card).getByText(MERCHANT_UNAVAILABLE)).toBeInTheDocument();
    expect(within(card).queryByText(/google/i)).not.toBeInTheDocument();
  });

  it("keeps the product link exactly as the backend sent it", async () => {
    await searchWith(
      makeComparisonResponse({
        results: [
          makeComparisonResult({ product: googleHostedAmazonProduct() }),
        ],
      }),
    );

    const link = await screen.findByRole("link", { name: /view product/i });
    // The merchant is Amazon, but the link is NOT rewritten to Amazon.
    expect(link).toHaveAttribute("href", GOOGLE_LINK);
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("leaves existing price formatting untouched", async () => {
    await searchWith(
      makeComparisonResponse({
        results: [
          makeComparisonResult({ product: googleHostedAmazonProduct() }),
        ],
      }),
    );

    const card = (await screen.findAllByRole("article"))[0];
    // Observed stays observed; the INR figure stays a labelled estimate.
    expect(within(card).getByText(/\$19\.99/)).toBeInTheDocument();
    expect(within(card).getByText(/observed/i)).toBeInTheDocument();
    expect(within(card).getByText(/1,824\.09/)).toBeInTheDocument();
    expect(within(card).getByText("estimate")).toBeInTheDocument();
  });

  it("still renders the brand alongside the merchant", async () => {
    await searchWith(
      makeComparisonResponse({
        results: [
          makeComparisonResult({
            product: googleHostedAmazonProduct({ brand: "Sony" }),
          }),
        ],
      }),
    );

    const card = (await screen.findAllByRole("article"))[0];
    expect(within(card).getByText("Sony")).toBeInTheDocument();
    expect(within(card).getByText("Amazon")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Side-by-side table
// ---------------------------------------------------------------------------

describe("merchant identity in the comparison table", () => {
  it("labels the column Merchant and lists the resolved merchants", () => {
    render(
      <ComparisonTable
        results={[
          makeComparisonResult({ product: googleHostedAmazonProduct() }),
          makeComparisonResult({
            rank: 2,
            product: makeDiscoveredProduct({
              external_id: "serper:222222",
              merchant_name: null,
              merchant_domain: "walmart.com",
            }),
          }),
        ]}
      />,
    );

    const table = screen.getByRole("table");
    expect(
      within(table).getByRole("columnheader", { name: "Merchant" }),
    ).toBeInTheDocument();
    expect(within(table).getByText("Amazon")).toBeInTheDocument();
    expect(within(table).getByText("walmart.com")).toBeInTheDocument();
    expect(within(table).queryByText(/google/i)).not.toBeInTheDocument();
  });

  it("shows the fallback for a row with no merchant identity", () => {
    render(
      <ComparisonTable
        results={[
          makeComparisonResult({ product: googleHostedAmazonProduct() }),
          makeComparisonResult({
            rank: 2,
            product: makeDiscoveredProduct({
              external_id: "serper:222222",
              merchant_name: null,
              merchant_domain: null,
            }),
          }),
        ]}
      />,
    );

    const table = screen.getByRole("table");
    expect(within(table).getByText(MERCHANT_UNAVAILABLE)).toBeInTheDocument();
  });

  it("drops the Merchant column when no row has a known merchant", () => {
    render(
      <ComparisonTable
        results={[
          makeComparisonResult({
            product: googleHostedAmazonProduct({ merchant_name: null }),
          }),
        ]}
      />,
    );

    const table = screen.getByRole("table");
    expect(
      within(table).queryByRole("columnheader", { name: "Merchant" }),
    ).not.toBeInTheDocument();
    // And nothing else sneaks the aggregator in as a substitute.
    expect(within(table).queryByText(/google/i)).not.toBeInTheDocument();
  });
});
