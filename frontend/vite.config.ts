/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The FastAPI backend origin the dev server proxies same-origin API and
// webhook traffic to. Keeping these paths same-origin lets a single public
// (ngrok) HTTPS domain serve the SPA at "/" AND reach FastAPI, so PayPal's
// return/cancel redirects and the registered webhook work without a second
// domain or a public URL baked into the bundle.
const BACKEND_ORIGIN = "http://127.0.0.1:8000";

// Exact backend path prefixes the frontend (and PayPal) rely on. ONLY these
// are proxied — "/" and every Vite asset (/@vite, /src, /node_modules,
// /assets, ...) stay frontend-served. Each key is a prefix match, so e.g.
// "/transactions" also covers "/transactions/paypal/{id}".
const PROXY_PATHS = [
  "/webhooks/paypal", // PayPal -> FastAPI signature-verified webhook (public)
  "/auth", // operator login
  "/products", // catalog + dev backend-status check
  "/ai", // /ai/plan
  "/chat", // conversational turn
  "/discovery", // /discovery/compare
  "/sessions", // session read + confirm
  "/purchase", // /purchase/capture/{order_id}
  "/transactions", // history, by-order, refund
];

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    strictPort: true,
    // Allow an optional HTTPS tunnel host (e.g. ngrok) for the dev server.
    // Set VITE_ALLOWED_HOST in your shell before running `npm run dev`.
    ...(process.env.VITE_ALLOWED_HOST
      ? { allowedHosts: [process.env.VITE_ALLOWED_HOST] }
      : {}),
    // Same-origin routing: forward only the explicit backend prefixes above
    // to FastAPI. http-proxy streams the method, body, and headers (including
    // Authorization and PayPal's PAYPAL-* transmission headers) unchanged, so
    // authentication and webhook signature verification are unaffected.
    proxy: Object.fromEntries(
      PROXY_PATHS.map((path) => [
        path,
        { target: BACKEND_ORIGIN, changeOrigin: true },
      ]),
    ),
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    // Pin the API base for tests so the wire-contract URL assertions stay
    // deterministic now that the app defaults to relative same-origin paths.
    env: { VITE_API_BASE_URL: "http://localhost:8000" },
  },
});
