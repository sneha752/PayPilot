/**
 * Operator login gate (Phase 11F-H).
 *
 * The whole app sits behind this until a valid operator key is exchanged for
 * a bearer token. The key is never stored anywhere by this component; it is
 * passed straight to api.login, which keeps the returned token in memory
 * only. On failure the field is preserved and a message is shown.
 */

import type React from "react";
import { useState } from "react";
import { login } from "../services/api";
import { ApiError } from "../services/apiError";
import { BrandMark } from "./BrandMark";

export function LoginScreen() {
  const [apiKey, setApiKey] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (submitting || apiKey.trim() === "") return;
    setSubmitting(true);
    setError(null);
    try {
      await login(apiKey);
      // Success flips isAuthenticated() -> the gate in App unmounts this.
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 401) {
        setError("That operator key was not accepted.");
      } else if (cause instanceof ApiError && cause.status === 503) {
        setError("Authentication is not configured on the server.");
      } else {
        setError("Could not reach the PayPilot backend.");
      }
      setSubmitting(false);
    }
  }

  return (
    <div className="flex min-h-dvh items-center justify-center bg-zinc-50 px-4 dark:bg-zinc-950">
      <form
        onSubmit={handleSubmit}
        className="w-full max-w-sm space-y-5 rounded-2xl border border-zinc-200 bg-white p-6 shadow-sm dark:border-zinc-800 dark:bg-zinc-900"
      >
        <div className="flex items-center gap-3">
          <BrandMark />
          <div>
            <h1 className="text-lg font-semibold tracking-tight">PayPilot</h1>
            <p className="text-sm text-zinc-500 dark:text-zinc-400">
              Operator sign-in
            </p>
          </div>
        </div>

        <div className="space-y-1.5">
          <label
            htmlFor="operator-key"
            className="block text-sm font-medium text-zinc-700 dark:text-zinc-300"
          >
            Operator key
          </label>
          <input
            id="operator-key"
            name="operator-key"
            type="password"
            autoComplete="off"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            disabled={submitting}
            className="w-full rounded-lg border border-zinc-300 bg-white px-3 py-2 text-sm text-zinc-900 shadow-sm focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500 disabled:opacity-60 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-100"
            aria-invalid={error !== null}
            aria-describedby={error ? "login-error" : undefined}
          />
        </div>

        {error && (
          <p id="login-error" role="alert" className="text-sm text-red-600 dark:text-red-400">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={submitting || apiKey.trim() === ""}
          className="inline-flex h-10 w-full items-center justify-center rounded-lg bg-brand-600 px-4 text-sm font-medium text-white shadow-sm transition-colors hover:bg-brand-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {submitting ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
