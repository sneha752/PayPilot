/**
 * Operator authentication wiring (Phase 11F-H).
 *
 * Proves the token is attached CENTRALLY (in httpClient, not per-call) and
 * cleared on logout and on a 401 — without storing it anywhere persistent.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { getProducts, login } from "../api";
import { ApiError } from "../apiError";
import { getToken, isAuthenticated, logout, setToken } from "../authService";

afterEach(() => {
  vi.unstubAllGlobals();
  logout(); // reset the shared in-memory store between tests
});

describe("operator auth wiring", () => {
  it("login stores the token and later requests carry it centrally", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ token: "TKN", token_type: "bearer", expires_in: 1800 }),
      })
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => [] });
    vi.stubGlobal("fetch", fetchMock);

    await login("the-operator-key");
    expect(getToken()).toBe("TKN");
    expect(isAuthenticated()).toBe(true);

    await getProducts();
    const [, init] = fetchMock.mock.calls[1] as [string, RequestInit];
    const headers = init.headers as Record<string, string>;
    expect(headers.Authorization).toBe("Bearer TKN");
  });

  it("login POSTs the api key to /auth/login", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ token: "T", token_type: "bearer", expires_in: 1800 }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await login("secret-key");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/auth/login");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ api_key: "secret-key" });
  });

  it("a 401 response clears the in-memory token", async () => {
    setToken("TKN");
    expect(isAuthenticated()).toBe(true);

    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 401,
      json: async () => ({ detail: "Invalid or expired bearer token" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(getProducts()).rejects.toBeInstanceOf(ApiError);
    expect(isAuthenticated()).toBe(false);
    expect(getToken()).toBeNull();
  });

  it("logout clears the token", () => {
    setToken("X");
    logout();
    expect(getToken()).toBeNull();
    expect(isAuthenticated()).toBe(false);
  });

  it("requests without a token carry no Authorization header", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => [] });
    vi.stubGlobal("fetch", fetchMock);

    await getProducts();
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = (init.headers ?? {}) as Record<string, string>;
    expect(headers.Authorization).toBeUndefined();
  });
});
