/**
 * Consistent error abstraction for every backend call.
 *
 * Preserves the HTTP status and the backend's `detail` message (FastAPI's
 * error convention) so the UI can eventually distinguish validation
 * errors, policy denials, PayPal failures, not-found, conflicts, and
 * server errors. Never carries a stack trace into the UI.
 */

export type ApiErrorKind =
  | "network" // backend unreachable (no HTTP response at all)
  | "validation" // 400 / 422 — the request was malformed
  | "not_found" // 404
  | "conflict" // 409 — state conflict (already captured/refunded, no pending action)
  | "rate_limited" // 429 — upstream discovery/AI provider rate limit
  | "upstream" // 502 — PayPal / AI provider failure reported by the backend
  | "unavailable" // 503 — backend dependency not configured/available
  | "server" // other 5xx
  | "unknown"; // anything else

function kindFromStatus(status: number): ApiErrorKind {
  if (status === 400 || status === 422) return "validation";
  if (status === 404) return "not_found";
  if (status === 409) return "conflict";
  if (status === 429) return "rate_limited";
  if (status === 502) return "upstream";
  if (status === 503) return "unavailable";
  if (status >= 500) return "server";
  return "unknown";
}

export class ApiError extends Error {
  /** HTTP status code; 0 when the request never got a response. */
  readonly status: number;
  readonly kind: ApiErrorKind;
  /** The backend's `detail` field when available. */
  readonly detail: string | null;

  constructor(options: {
    message: string;
    status?: number;
    kind?: ApiErrorKind;
    detail?: string | null;
  }) {
    super(options.message);
    this.name = "ApiError";
    this.status = options.status ?? 0;
    this.kind = options.kind ?? (options.status ? kindFromStatus(options.status) : "unknown");
    this.detail = options.detail ?? null;
  }

  /** The backend crashed/was unreachable before sending any response. */
  static network(cause?: unknown): ApiError {
    const error = new ApiError({
      message: "Cannot reach the PayPilot backend.",
      status: 0,
      kind: "network",
    });
    if (cause instanceof Error) error.cause = cause;
    return error;
  }

  /** Build from a non-2xx HTTP response, extracting FastAPI's detail. */
  static async fromResponse(response: Response): Promise<ApiError> {
    let detail: string | null = null;
    try {
      const body: unknown = await response.json();
      if (body && typeof body === "object" && "detail" in body) {
        const raw = (body as { detail: unknown }).detail;
        // FastAPI validation errors send a structured array; everything
        // else sends a string. Keep both readable.
        detail = typeof raw === "string" ? raw : JSON.stringify(raw);
      }
    } catch {
      // Non-JSON error body — status alone still tells the story.
    }
    return new ApiError({
      message: detail ?? `Request failed with status ${response.status}`,
      status: response.status,
      detail,
    });
  }
}
