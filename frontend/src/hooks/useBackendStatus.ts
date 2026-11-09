/**
 * Development connectivity check: calls the existing GET /products
 * endpoint (read-only) to confirm the backend is reachable. No dedicated
 * health endpoint is used or added.
 */

import { useEffect, useState } from "react";
import { getProducts } from "../services/api";

export type BackendStatus = "checking" | "connected" | "unavailable";

export function useBackendStatus(): BackendStatus {
  const [status, setStatus] = useState<BackendStatus>("checking");

  useEffect(() => {
    const controller = new AbortController();

    getProducts(controller.signal)
      .then(() => setStatus("connected"))
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setStatus("unavailable");
      });

    return () => controller.abort();
  }, []);

  return status;
}
