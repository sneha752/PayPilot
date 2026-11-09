/**
 * Core HTTP helper — the single place fetch() is called for backend
 * requests. Every API function in api.ts goes through this, so base URL
 * handling, JSON parsing, and error mapping are never duplicated.
 */

import { API_BASE_URL } from "../lib/config";
import { ApiError } from "./apiError";
import { clearToken, getToken } from "./authService";

interface RequestOptions {
  method?: "GET" | "POST";
  body?: unknown;
  /** Query parameters; null/undefined values are omitted. */
  query?: Record<string, string | number | null | undefined>;
  signal?: AbortSignal;
}

function buildUrl(
  path: string,
  query?: RequestOptions["query"],
): string {
  // API_BASE_URL is empty by default (same-origin relative paths), so resolve
  // against the current origin. An absolute VITE_API_BASE_URL override still
  // wins — the base argument is ignored when the first argument is absolute.
  const url = new URL(`${API_BASE_URL}${path}`, window.location.origin);
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value !== null && value !== undefined) {
        url.searchParams.set(key, String(value));
      }
    }
  }
  return url.toString();
}

export async function apiRequest<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const { method = "GET", body, query, signal } = options;

  // Build headers once: JSON content type when there's a body, and the
  // bearer token when the operator is logged in. This is the SINGLE place
  // the token is attached — no API function sets it, so none can forget to.
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;

  let response: Response;
  try {
    response = await fetch(buildUrl(path, query), {
      method,
      signal,
      headers: Object.keys(headers).length > 0 ? headers : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (cause) {
    // AbortError is the caller cancelling, not a backend failure.
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    throw ApiError.network(cause);
  }

  if (!response.ok) {
    // 401 means the token is missing, invalid, or expired: drop it so the
    // app falls back to the login gate instead of retrying with a dead token.
    if (response.status === 401) clearToken();
    throw await ApiError.fromResponse(response);
  }

  return (await response.json()) as T;
}
