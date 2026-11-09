/**
 * Money formatting tests.
 *
 * The property under test is that formatting is LOSSLESS and arithmetic-free:
 * digits and decimal places survive exactly, nothing is rounded, and no
 * conversion or multiplication ever happens in the browser.
 */

import { describe, expect, it } from "vitest";

import { formatMoney, groupDecimalString } from "../money";

describe("groupDecimalString", () => {
  it("preserves the fractional part exactly", () => {
    expect(groupDecimalString("3192.84", "INR")).toBe("3,192.84");
    expect(groupDecimalString("2500.00", "INR")).toBe("2,500.00");
    expect(groupDecimalString("0.05", "USD")).toBe("0.05");
  });

  it("does not round or truncate long fractions", () => {
    // A value the backend chose to send with more places stays intact.
    expect(groupDecimalString("1824.0875", "INR")).toBe("1,824.0875");
  });

  it("groups INR in the Indian convention", () => {
    expect(groupDecimalString("100000", "INR")).toBe("1,00,000");
    expect(groupDecimalString("1234567.89", "INR")).toBe("12,34,567.89");
    expect(groupDecimalString("999", "INR")).toBe("999");
  });

  it("groups other currencies in threes", () => {
    expect(groupDecimalString("100000", "USD")).toBe("100,000");
    expect(groupDecimalString("1234567.89", "USD")).toBe("1,234,567.89");
  });

  it("handles negatives and no-currency input", () => {
    expect(groupDecimalString("-1234.50", "USD")).toBe("-1,234.50");
    expect(groupDecimalString("1234.50")).toBe("1,234.50");
  });

  it("returns an unparseable value untouched rather than reinterpreting it", () => {
    expect(groupDecimalString("not-a-price", "USD")).toBe("not-a-price");
    expect(groupDecimalString("1.2.3", "USD")).toBe("1.2.3");
    expect(groupDecimalString("1e5", "USD")).toBe("1e5");
  });
});

describe("formatMoney", () => {
  it("renders a known currency with its symbol and code", () => {
    expect(formatMoney("19.99", "USD")).toBe("$19.99 USD");
    expect(formatMoney("1824.09", "INR")).toBe("₹1,824.09 INR");
  });

  it("normalizes a lowercase currency code", () => {
    expect(formatMoney("19.99", "usd")).toBe("$19.99 USD");
  });

  it("names an unknown currency without guessing a symbol", () => {
    expect(formatMoney("29.99", "EUR")).toBe("29.99 EUR");
    expect(formatMoney("1000", "JPY")).toBe("1,000 JPY");
  });

  it("returns null — never zero — when there is no amount", () => {
    expect(formatMoney(null, "INR")).toBeNull();
    expect(formatMoney(undefined, "INR")).toBeNull();
    expect(formatMoney("", "INR")).toBeNull();
    expect(formatMoney("   ", "INR")).toBeNull();
  });

  it("renders a genuine zero amount rather than hiding it", () => {
    // A backend-reported 0.00 is data; only a MISSING price is null.
    expect(formatMoney("0.00", "USD")).toBe("$0.00 USD");
  });

  it("falls back to the bare amount when no currency is given", () => {
    expect(formatMoney("19.99", null)).toBe("19.99");
  });

  it("performs no arithmetic on the value it is given", () => {
    // Whatever string arrives, the digits that come out are the same ones.
    for (const amount of ["19.99", "1824.09", "91.25", "0.01", "99999.99"]) {
      const rendered = formatMoney(amount, "INR") ?? "";
      const digitsIn = amount.replace(/[^\d]/g, "");
      const digitsOut = rendered.replace(/[^\d]/g, "");
      expect(digitsOut).toBe(digitsIn);
    }
  });
});
