/**
 * Frontend configuration. The ONLY place the backend base URL is read —
 * nothing else in the app touches import.meta.env for it.
 *
 * Secrets never belong here: the frontend must not hold PayPal or Gemini
 * credentials. Anything in a VITE_ variable ships to the browser.
 *
 * The default is EMPTY — i.e. same-origin, relative API paths. This lets a
 * single public HTTPS domain (e.g. the ngrok tunnel) serve the SPA at "/"
 * and reach FastAPI through the Vite dev-server proxy, with no hardcoded
 * public URL in the bundle. A deployment that genuinely needs a different
 * backend origin can still set VITE_API_BASE_URL to an absolute URL.
 */

const DEFAULT_API_BASE_URL = "";

function normalizeBaseUrl(url: string): string {
  return url.replace(/\/+$/, "");
}

export const API_BASE_URL = normalizeBaseUrl(
  import.meta.env.VITE_API_BASE_URL ?? DEFAULT_API_BASE_URL,
);
