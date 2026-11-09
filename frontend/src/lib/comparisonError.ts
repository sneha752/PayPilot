/**
 * User-facing messages for comparison failures.
 *
 * The backend's `detail` string is deliberately NOT shown. It can name an
 * internal provider, a configuration variable, or an exception type, none
 * of which belongs in front of a shopper. Each status gets a message that
 * says what happened and what to do, and nothing about our internals.
 *
 * Stack traces, API keys, provider credentials, and raw exception text can
 * never reach the UI through this function.
 */

import { ApiError } from "../services/apiError";

export interface ComparisonErrorInfo {
  message: string;
  /** Whether trying the same query again could plausibly work. */
  retryable: boolean;
}

export function describeComparisonError(cause: unknown): ComparisonErrorInfo {
  if (cause instanceof ApiError) {
    switch (cause.status) {
      case 400:
      case 422:
        return {
          message:
            "That search couldn't be understood. Try describing the product and your budget, for example “wireless headphones under ₹3,000”.",
          retryable: false,
        };
      case 429:
        return {
          message:
            "Live shopping search is temporarily rate-limited. Please try again shortly.",
          retryable: true,
        };
      case 502:
        return {
          message:
            "Live product discovery returned an unusable response. Please try again.",
          retryable: true,
        };
      case 503:
        return {
          message: "Live product discovery is temporarily unavailable.",
          retryable: true,
        };
      default:
        break;
    }

    if (cause.kind === "network") {
      return {
        message:
          "Cannot reach PayPilot. Check your connection and try again.",
        retryable: true,
      };
    }

    return {
      message: "Product comparison failed. Please try again.",
      retryable: true,
    };
  }

  return {
    message: "Something went wrong while comparing products. Please try again.",
    retryable: true,
  };
}
