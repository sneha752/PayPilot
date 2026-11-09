/**
 * Shopping comparison surface tests.
 *
 * The centralized API service is mocked, so no real backend, discovery
 * provider, or AI request occurs. The assertions that matter most are the
 * honesty ones: prices are rendered exactly as the backend sent them, no
 * conversion happens in the browser, an empty result set stays empty, and
 * a deterministic fallback is never labelled as AI.
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ShopPage } from "../ShopPage";
import { ApiError } from "../../services/apiError";
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

const QUERY = "wireless headphones under 3000";

beforeEach(() => {
  vi.clearAllMocks();
});

async function searchFor(query = QUERY) {
  const user = userEvent.setup();
  render(<ShopPage />);
  await user.type(screen.getByRole("searchbox"), query);
  await user.click(screen.getByRole("button", { name: /compare/i }));
  return user;
}

// ---------------------------------------------------------------------------
// 1-4. Search input, empty input, submission, loading
// ---------------------------------------------------------------------------

describe("search input", () => {
  it("renders the search input and compare button", () => {
    render(<ShopPage />);

    expect(screen.getByRole("searchbox")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /compare/i })).toBeInTheDocument();
    expect(
      screen.getByPlaceholderText(/wireless headphones under/i),
    ).toBeInTheDocument();
  });

  it("disables compare while the input is empty", () => {
    render(<ShopPage />);

    expect(screen.getByRole("button", { name: /compare/i })).toBeDisabled();
    expect(compareMock).not.toHaveBeenCalled();
  });

  it("does not submit a whitespace-only query", async () => {
    const user = userEvent.setup();
    render(<ShopPage />);

    await user.type(screen.getByRole("searchbox"), "   ");
    expect(screen.getByRole("button", { name: /compare/i })).toBeDisabled();
    await user.keyboard("{Enter}");

    expect(compareMock).not.toHaveBeenCalled();
  });

  it("submits the trimmed query through the centralized API service", async () => {
    compareMock.mockResolvedValue(makeComparisonResponse());

    const user = userEvent.setup();
    render(<ShopPage />);
    await user.type(screen.getByRole("searchbox"), `  ${QUERY}  `);
    await user.click(screen.getByRole("button", { name: /compare/i }));

    await waitFor(() => expect(compareMock).toHaveBeenCalledTimes(1));
    expect(compareMock.mock.calls[0][0]).toBe(QUERY);
  });

  it("submits on Enter from the keyboard", async () => {
    compareMock.mockResolvedValue(makeComparisonResponse());

    const user = userEvent.setup();
    render(<ShopPage />);
    await user.type(screen.getByRole("searchbox"), `${QUERY}{Enter}`);

    await waitFor(() => expect(compareMock).toHaveBeenCalledTimes(1));
  });

  it("sends no price, currency, or payment field to the backend", async () => {
    compareMock.mockResolvedValue(makeComparisonResponse());
    await searchFor();

    await waitFor(() => expect(compareMock).toHaveBeenCalled());
    const [query, options] = compareMock.mock.calls[0];
    expect(query).toBe(QUERY);
    // Only bounded counts accompany the text.
    expect(options).toEqual({});
    const serialized = JSON.stringify(compareMock.mock.calls[0]);
    for (const forbidden of [
      "paypal",
      "capture",
      "refund",
      "amount",
      "conversion_rate",
      "observed_price",
      "external_id",
    ]) {
      expect(serialized.toLowerCase()).not.toContain(forbidden);
    }
  });

  it("shows a loading state and disables input while comparing", async () => {
    compareMock.mockReturnValue(new Promise(() => {}));

    await searchFor();

    expect(await screen.findByRole("status")).toHaveTextContent(
      /searching live products/i,
    );
    expect(screen.getByRole("searchbox")).toBeDisabled();
    expect(screen.getByRole("button", { name: /comparing/i })).toBeDisabled();
  });
});

// ---------------------------------------------------------------------------
// 5-9, 13. Successful rendering
// ---------------------------------------------------------------------------

describe("successful comparison", () => {
  it("renders the query and candidate counts from the backend", async () => {
    compareMock.mockResolvedValue(makeComparisonResponse());
    await searchFor();

    expect(await screen.findByText(/4 candidates considered/i)).toBeInTheDocument();
    expect(screen.getByText(/3 recommendations/i)).toBeInTheDocument();
    expect(screen.getByText(/via serper/i)).toBeInTheDocument();
  });

  it("renders all three ranked products with their ranks", async () => {
    compareMock.mockResolvedValue(makeComparisonResponse());
    await searchFor();

    const cards = await screen.findAllByRole("article");
    expect(cards).toHaveLength(3);
    expect(within(cards[0]).getByText("1")).toBeInTheDocument();
    expect(within(cards[0]).getByText(/top recommendation/i)).toBeInTheDocument();
    expect(within(cards[1]).getByText("2")).toBeInTheDocument();
    expect(within(cards[1]).getByText(/alternative/i)).toBeInTheDocument();
    expect(within(cards[2]).getByText("3")).toBeInTheDocument();
  });

  it("renders the observed price exactly as the backend sent it", async () => {
    compareMock.mockResolvedValue(makeComparisonResponse());
    await searchFor();

    const cards = await screen.findAllByRole("article");
    // "19.99" verbatim, with its currency named.
    expect(within(cards[0]).getByText("$19.99 USD")).toBeInTheDocument();
    expect(within(cards[0]).getByText(/observed/i)).toBeInTheDocument();
  });

  it("renders the backend-derived INR price marked as an estimate", async () => {
    compareMock.mockResolvedValue(makeComparisonResponse());
    await searchFor();

    const cards = await screen.findAllByRole("article");
    // Grouped for reading, but the digits are the backend's: 1824.09.
    expect(within(cards[0]).getByText(/₹1,824\.09 INR/)).toBeInTheDocument();
    // The exact label span, distinct from the demo-rate note below it.
    expect(within(cards[0]).getByText("estimate")).toBeInTheDocument();
    expect(
      within(cards[0]).getByText(/configured demo exchange rate/i),
    ).toBeInTheDocument();
  });

  it("renders the backend reason verbatim", async () => {
    compareMock.mockResolvedValue(makeComparisonResponse());
    await searchFor();

    const reason =
      "Best match among the returned candidates while remaining within the budget.";
    const cards = await screen.findAllByRole("article");
    // Unmodified in the card...
    expect(within(cards[0]).getByText(reason)).toBeInTheDocument();
    // ...and unmodified again in the side-by-side table.
    expect(
      within(screen.getByRole("table")).getByText(reason),
    ).toBeInTheDocument();
  });

  it("renders the side-by-side comparison table", async () => {
    compareMock.mockResolvedValue(makeComparisonResponse());
    await searchFor();

    const table = await screen.findByRole("table");
    expect(within(table).getByText(/^Why$/)).toBeInTheDocument();
    expect(within(table).getByText(/^Price$/)).toBeInTheDocument();
    // One row per result.
    expect(within(table).getAllByRole("row")).toHaveLength(4); // header + 3
  });

  it("omits table columns no result has data for", async () => {
    compareMock.mockResolvedValue(
      makeComparisonResponse({
        results: [
          makeComparisonResult({
            product: makeDiscoveredProduct({ availability: null }),
          }),
          makeComparisonResult({
            rank: 2,
            product: makeDiscoveredProduct({
              external_id: "serper:999",
              availability: null,
            }),
          }),
        ],
      }),
    );
    await searchFor();

    const table = await screen.findByRole("table");
    expect(within(table).queryByText(/availability/i)).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// 10-12. Missing data handled without invention
// ---------------------------------------------------------------------------

describe("missing backend data", () => {
  it("shows only the observed price when there is no normalized price", async () => {
    compareMock.mockResolvedValue(
      makeComparisonResponse({
        results: [
          makeComparisonResult({
            price_note: null,
            product: makeDiscoveredProduct({
              normalized_price: null,
              normalized_currency: null,
              conversion_rate: null,
              conversion_source: null,
            }),
          }),
        ],
      }),
    );
    await searchFor();

    const card = (await screen.findAllByRole("article"))[0];
    expect(within(card).getByText("$19.99 USD")).toBeInTheDocument();
    expect(within(card).queryByText(/₹/)).not.toBeInTheDocument();
    expect(within(card).queryByText(/estimate/i)).not.toBeInTheDocument();
    expect(
      within(card).queryByText(/configured demo exchange rate/i),
    ).not.toBeInTheDocument();
  });

  it("shows 'Price unavailable' rather than zero when no price exists", async () => {
    compareMock.mockResolvedValue(
      makeComparisonResponse({
        results: [
          makeComparisonResult({
            price_note: null,
            product: makeDiscoveredProduct({
              observed_price: null,
              observed_currency: null,
              normalized_price: null,
              normalized_currency: null,
            }),
          }),
        ],
      }),
    );
    await searchFor();

    const card = (await screen.findAllByRole("article"))[0];
    expect(within(card).getByText(/price unavailable/i)).toBeInTheDocument();
    expect(within(card).queryByText(/₹0/)).not.toBeInTheDocument();
    expect(within(card).queryByText(/\$0/)).not.toBeInTheDocument();
  });

  it("shows a neutral placeholder when the image is missing", async () => {
    compareMock.mockResolvedValue(
      makeComparisonResponse({
        results: [
          makeComparisonResult({
            product: makeDiscoveredProduct({ image_url: null }),
          }),
        ],
      }),
    );
    await searchFor();

    const card = (await screen.findAllByRole("article"))[0];
    expect(
      within(card).getByRole("img", { name: /no product image available/i }),
    ).toBeInTheDocument();
  });

  it("disables the product action when there is no product URL", async () => {
    compareMock.mockResolvedValue(
      makeComparisonResponse({
        results: [
          makeComparisonResult({
            product: makeDiscoveredProduct({ product_url: null }),
          }),
        ],
      }),
    );
    await searchFor();

    const card = (await screen.findAllByRole("article"))[0];
    expect(within(card).queryByRole("link")).not.toBeInTheDocument();
    expect(
      within(card).getByRole("button", { name: /no product link available/i }),
    ).toBeDisabled();
  });

  it("does not infer a brand from the product title", async () => {
    compareMock.mockResolvedValue(
      makeComparisonResponse({
        results: [
          makeComparisonResult({
            product: makeDiscoveredProduct({
              brand: null,
              title: "Sony WH-CH520 Wireless Headphones",
            }),
          }),
        ],
      }),
    );
    await searchFor();

    const card = (await screen.findAllByRole("article"))[0];
    // The title mentions Sony, but no standalone brand line is rendered.
    expect(within(card).queryByText(/^Sony$/)).not.toBeInTheDocument();
  });

  // Phase 10H replaced this test's original assertion. It used to require
  // that retailer_domain be rendered verbatim, which for a Serper result
  // means displaying "www.google.com" as the seller of an Amazon product.
  // The honesty requirement is now stronger: show the merchant the backend
  // resolved, and never show the aggregator the link happens to pass
  // through. Nothing is relabelled — the backend supplies both values.
  it("names the merchant and never the aggregator the link passes through", async () => {
    compareMock.mockResolvedValue(
      makeComparisonResponse({
        results: [
          makeComparisonResult({
            product: makeDiscoveredProduct({
              source: "serper",
              // A real Google Shopping result: Amazon sells it, Google
              // hosts the link, so no merchant domain is derivable.
              merchant_name: "Amazon",
              merchant_domain: null,
              product_url: "https://www.google.com/shopping/product/1",
              retailer_domain: "www.google.com",
            }),
          }),
        ],
      }),
    );
    await searchFor();

    const card = (await screen.findAllByRole("article"))[0];
    expect(within(card).getByText("Amazon")).toBeInTheDocument();
    // Google appears nowhere as a seller — not as a host, not as a name.
    expect(within(card).queryByText("www.google.com")).not.toBeInTheDocument();
    expect(within(card).queryByText(/^Google$/)).not.toBeInTheDocument();
    // And the provider's own name is not passed off as a shop either.
    expect(within(card).queryByText(/^serper$/)).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// 14-15. Ranking labels
// ---------------------------------------------------------------------------

describe("ranking labels", () => {
  it("shows AI-ranked when the backend reports an AI ranking", async () => {
    compareMock.mockResolvedValue(
      makeComparisonResponse({ ranking_method: "ai", ranking_provider: "groq" }),
    );
    await searchFor();

    expect(await screen.findByText("AI-ranked")).toBeInTheDocument();
    expect(screen.queryByText(/deterministic ranking/i)).not.toBeInTheDocument();
  });

  it("shows Deterministic ranking and never claims AI for a fallback", async () => {
    compareMock.mockResolvedValue(
      makeComparisonResponse({
        ranking_method: "deterministic",
        ranking_provider: null,
      }),
    );
    await searchFor();

    expect(await screen.findByText("Deterministic ranking")).toBeInTheDocument();
    expect(screen.queryByText("AI-ranked")).not.toBeInTheDocument();
  });

  it("invents no superiority label of its own", async () => {
    compareMock.mockResolvedValue(makeComparisonResponse());
    await searchFor();

    await screen.findAllByRole("article");
    for (const claim of [
      /best value/i,
      /cheapest/i,
      /best battery/i,
      /highest quality/i,
      /best rated/i,
      /best on the market/i,
    ]) {
      expect(screen.queryByText(claim)).not.toBeInTheDocument();
    }
  });
});

// ---------------------------------------------------------------------------
// 16-17. Empty results
// ---------------------------------------------------------------------------

describe("empty results", () => {
  it("reports an empty result set without inventing products", async () => {
    compareMock.mockResolvedValue(
      makeComparisonResponse({
        results: [],
        candidates_considered: 0,
        discovered_count: 7,
        ranking_method: "deterministic",
        ranking_provider: null,
      }),
    );
    await searchFor();

    expect(
      await screen.findByText(/no matching products were found/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/removing a strict feature requirement/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/7 products were found/i)).toBeInTheDocument();
    // No cards, no table, no fallback demo product.
    expect(screen.queryAllByRole("article")).toHaveLength(0);
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("still shows the original query and never rewrites it", async () => {
    compareMock.mockResolvedValue(
      makeComparisonResponse({ results: [], query: QUERY }),
    );
    await searchFor();

    expect(await screen.findByText(new RegExp(QUERY, "i"))).toBeInTheDocument();
    expect(compareMock).toHaveBeenCalledTimes(1);
    expect(compareMock.mock.calls[0][0]).toBe(QUERY);
  });
});

// ---------------------------------------------------------------------------
// 17-20. Errors
// ---------------------------------------------------------------------------

describe("error states", () => {
  async function expectErrorFor(error: ApiError, pattern: RegExp) {
    compareMock.mockRejectedValue(error);
    await searchFor();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(pattern);
    return alert;
  }

  it("handles 400 with actionable guidance", async () => {
    await expectErrorFor(
      new ApiError({ message: "x", status: 400, detail: "query must be non-empty" }),
      /couldn't be understood/i,
    );
  });

  it("handles 422 the same way as 400", async () => {
    await expectErrorFor(
      new ApiError({ message: "x", status: 422 }),
      /couldn't be understood/i,
    );
  });

  it("handles 429 with a rate-limit message", async () => {
    await expectErrorFor(
      new ApiError({ message: "x", status: 429 }),
      /temporarily rate-limited/i,
    );
  });

  it("handles 502 as an upstream failure", async () => {
    await expectErrorFor(
      new ApiError({ message: "x", status: 502 }),
      /unusable response/i,
    );
  });

  it("handles 503 as discovery unavailable", async () => {
    await expectErrorFor(
      new ApiError({ message: "x", status: 503 }),
      /temporarily unavailable/i,
    );
  });

  it("handles a network failure", async () => {
    await expectErrorFor(ApiError.network(), /cannot reach paypilot/i);
  });

  it("never leaks backend detail text into the UI", async () => {
    compareMock.mockRejectedValue(
      new ApiError({
        message: "boom",
        status: 503,
        detail:
          "SERPER_API_KEY is not set in the environment; Traceback (most recent call last)",
      }),
    );
    await searchFor();

    await screen.findByRole("alert");
    expect(screen.queryByText(/SERPER_API_KEY/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Traceback/)).not.toBeInTheDocument();
  });

  it("offers a retry that re-runs the same query", async () => {
    compareMock.mockRejectedValueOnce(new ApiError({ message: "x", status: 503 }));
    const user = await searchFor();

    await screen.findByRole("alert");
    compareMock.mockResolvedValue(makeComparisonResponse());
    await user.click(screen.getByRole("button", { name: /try again/i }));

    expect(await screen.findAllByRole("article")).toHaveLength(3);
    expect(compareMock).toHaveBeenCalledTimes(2);
    expect(compareMock.mock.calls[1][0]).toBe(QUERY);
  });

  it("shows no results alongside an error", async () => {
    compareMock.mockResolvedValueOnce(makeComparisonResponse());
    const user = await searchFor();
    await screen.findAllByRole("article");

    compareMock.mockRejectedValueOnce(new ApiError({ message: "x", status: 503 }));
    await user.clear(screen.getByRole("searchbox"));
    await user.type(screen.getByRole("searchbox"), "laptops{Enter}");

    await screen.findByRole("alert");
    // Stale results must never read as the answer to the new query.
    expect(screen.queryAllByRole("article")).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// 21-25. Honesty, safety, and layout guarantees
// ---------------------------------------------------------------------------

describe("financial and layout guarantees", () => {
  it("performs no currency conversion in the browser", async () => {
    // A rate and an observed price that, if multiplied client-side, would
    // give 1824.0875 -> the UI must show only the backend's own figure.
    compareMock.mockResolvedValue(
      makeComparisonResponse({
        results: [
          makeComparisonResult({
            product: makeDiscoveredProduct({
              observed_price: "19.99",
              conversion_rate: "91.25",
              normalized_price: "1500.00",
            }),
          }),
        ],
      }),
    );
    await searchFor();

    const card = (await screen.findAllByRole("article"))[0];
    // The backend said 1500.00, so 1500.00 is what shows — even though it
    // is not 19.99 x 91.25.
    expect(within(card).getByText(/₹1,500\.00 INR/)).toBeInTheDocument();
    expect(within(card).queryByText(/1,824/)).not.toBeInTheDocument();
  });

  it("renders no product data before a search", () => {
    render(<ShopPage />);

    expect(screen.queryAllByRole("article")).toHaveLength(0);
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(compareMock).not.toHaveBeenCalled();
    // The intro explains the surface rather than showing sample products.
    expect(screen.getByText(/compare products across the live web/i)).toBeInTheDocument();
  });

  it("opens external product links safely and does not call them Buy Now", async () => {
    compareMock.mockResolvedValue(makeComparisonResponse());
    await searchFor();

    const cards = await screen.findAllByRole("article");
    const link = within(cards[0]).getByRole("link", { name: /view product/i });
    expect(link).toHaveAttribute(
      "href",
      "https://www.bestbuy.com/site/sony-ch520",
    );
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    // PayPilot does not own the retailer checkout.
    expect(screen.queryByText(/buy now/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/checkout/i)).not.toBeInTheDocument();
  });

  it("does not rewrite the backend product URL", async () => {
    const url = "https://shop.example/p?id=1";
    compareMock.mockResolvedValue(
      makeComparisonResponse({
        results: [
          makeComparisonResult({
            product: makeDiscoveredProduct({ product_url: url }),
          }),
        ],
      }),
    );
    await searchFor();

    const link = await screen.findByRole("link", { name: /view product/i });
    expect(link).toHaveAttribute("href", url);
  });

  it("keeps a very long title inside the card without widening it", async () => {
    const longTitle = `Sony ${"Ultra ".repeat(40)}Wireless Headphones`;
    compareMock.mockResolvedValue(
      makeComparisonResponse({
        results: [
          makeComparisonResult({
            product: makeDiscoveredProduct({ title: longTitle }),
          }),
        ],
      }),
    );
    await searchFor();

    const heading = await screen.findByRole("heading", { level: 3 });
    expect(heading).toHaveTextContent(/Sony/);
    // Clamped and wrapped rather than allowed to stretch the layout.
    expect(heading.className).toMatch(/line-clamp-2/);
    expect(heading.className).toMatch(/break-words/);
  });

  it("constrains long explanations and merchant names", async () => {
    compareMock.mockResolvedValue(
      makeComparisonResponse({
        results: [
          makeComparisonResult({
            reason: "Because ".repeat(60),
            product: makeDiscoveredProduct({
              merchant_name: "Very Long Retailer Name ".repeat(5),
              merchant_domain: `${"very-long-retailer-name".repeat(5)}.example`,
              retailer_domain: `${"very-long-retailer-name".repeat(5)}.example`,
            }),
          }),
        ],
      }),
    );
    await searchFor();

    const card = (await screen.findAllByRole("article"))[0];
    expect(within(card).getByText(/Because Because/)).toBeInTheDocument();
    // The card and its children opt into min-w-0/truncate so a long token
    // cannot force horizontal overflow.
    expect(card.className).toMatch(/min-w-0/);
  });

  it("renders a mobile comparison list that does not scroll horizontally", async () => {
    compareMock.mockResolvedValue(makeComparisonResponse());
    await searchFor();

    const table = await screen.findByRole("table");
    // Desktop table is hidden on small screens...
    expect(table.closest("div")?.className).toMatch(/hidden/);
    expect(table.closest("div")?.className).toMatch(/md:block/);
    // ...and the stacked mobile list is hidden from md upward.
    const mobileList = screen.getAllByRole("list").find((element) =>
      element.className.includes("md:hidden"),
    );
    expect(mobileList).toBeDefined();
    expect(mobileList?.className).toMatch(/md:hidden/);
  });

  it("exposes no payment, PayPal, or secret-looking content", async () => {
    compareMock.mockResolvedValue(makeComparisonResponse());
    await searchFor();

    await screen.findAllByRole("article");
    const text = document.body.textContent?.toLowerCase() ?? "";
    for (const forbidden of [
      "paypal",
      "capture id",
      "refund id",
      "api key",
      "client secret",
      "approve",
    ]) {
      expect(text).not.toContain(forbidden);
    }
  });
});
