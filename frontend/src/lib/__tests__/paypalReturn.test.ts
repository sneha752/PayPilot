/**
 * PayPal return-URL contract tests: `token` carries the PayPal order id,
 * and only the presence of BOTH token and PayerID marks an approval
 * return. A cancel return (token alone) must never be detected.
 */

import { afterEach, describe, expect, it } from "vitest";

import { parsePayPalReturn, stripPayPalReturnParams } from "../paypalReturn";

describe("parsePayPalReturn", () => {
  it("extracts the order id from an approval return (token + PayerID)", () => {
    const result = parsePayPalReturn("?token=3A032920JC2100350&PayerID=ABCDEF12345");
    expect(result).toEqual({ orderId: "3A032920JC2100350" });
  });

  it("returns null for a plain page load with no query string", () => {
    expect(parsePayPalReturn("")).toBeNull();
    expect(parsePayPalReturn("?")).toBeNull();
  });

  it("returns null for a cancel return (token without PayerID)", () => {
    expect(parsePayPalReturn("?token=3A032920JC2100350")).toBeNull();
  });

  it("returns null when PayerID is present without a token", () => {
    expect(parsePayPalReturn("?PayerID=ABCDEF12345")).toBeNull();
  });

  it("returns null for blank parameter values", () => {
    expect(parsePayPalReturn("?token=&PayerID=ABCDEF12345")).toBeNull();
    expect(parsePayPalReturn("?token=3A032920JC2100350&PayerID=%20")).toBeNull();
  });

  it("ignores unrelated query parameters", () => {
    const result = parsePayPalReturn(
      "?utm_source=x&token=3A032920JC2100350&PayerID=P1",
    );
    expect(result).toEqual({ orderId: "3A032920JC2100350" });
  });
});

describe("stripPayPalReturnParams", () => {
  afterEach(() => {
    window.history.replaceState(null, "", "/");
  });

  it("removes token and PayerID but preserves other params", () => {
    window.history.replaceState(
      null,
      "",
      "/?token=3A032920JC2100350&PayerID=P1&other=keep",
    );

    stripPayPalReturnParams();

    const params = new URLSearchParams(window.location.search);
    expect(params.get("token")).toBeNull();
    expect(params.get("PayerID")).toBeNull();
    expect(params.get("other")).toBe("keep");
  });

  it("makes a subsequent parse detect no return (refresh safety)", () => {
    window.history.replaceState(null, "", "/?token=X1&PayerID=P1");

    stripPayPalReturnParams();

    expect(parsePayPalReturn(window.location.search)).toBeNull();
  });
});
