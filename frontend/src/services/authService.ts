/**
 * In-memory operator authentication state (Phase 11F-H).
 *
 * The bearer token lives ONLY in this module's memory. It is deliberately
 * never written to localStorage, sessionStorage, a cookie, or a URL: those
 * are readable by other scripts or persist across sessions, which is exactly
 * what we do not want for a payment operator token. A page reload therefore
 * clears it and the operator logs in again.
 *
 * This module imports nothing (in particular not httpClient), so it can be
 * imported by httpClient without a cycle. The login network call lives in
 * api.ts, which owns backend requests; this module only holds the token and
 * notifies subscribers when it changes.
 */

type Listener = () => void;

let token: string | null = null;
const listeners = new Set<Listener>();

function notify(): void {
  for (const listener of listeners) listener();
}

/** The current bearer token, or null when logged out. */
export function getToken(): string | null {
  return token;
}

/** True while a token is held. Not a security check — the backend decides. */
export function isAuthenticated(): boolean {
  return token !== null;
}

/** Replace the in-memory token and notify subscribers. */
export function setToken(next: string | null): void {
  if (token === next) return;
  token = next;
  notify();
}

/** Clear the token (logout, or after an auth failure). */
export function clearToken(): void {
  setToken(null);
}

/** Explicit logout — same as clearing, named for intent at call sites. */
export function logout(): void {
  clearToken();
}

/**
 * Subscribe to token changes (login/logout/expiry-clear). Returns an
 * unsubscribe function. Used by the login gate to re-render.
 */
export function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
