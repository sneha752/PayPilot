/**
 * Return-from-PayPal capture flow (Phase 9 Step 4).
 *
 * On mount, this hook checks whether the current URL is a PayPal APPROVAL
 * return (see lib/paypalReturn.ts for the observed contract). If so it:
 *
 *   1. strips the return parameters from the address bar immediately, so a
 *      refresh can never re-trigger the capture, then
 *   2. calls POST /purchase/capture/{order_id} through the centralized API
 *      service — the order id is the ONLY thing sent, and
 *   3. reflects the backend's authoritative answer (captured / already
 *      captured / each failure) without ever inventing a success.
 *
 * No PayPal API is ever called from here; the backend owns the entire
 * capture path. A plain page load without return parameters stays "idle"
 * and makes no request at all.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  capturePurchase,
  getTransaction,
  getTransactionByOrder,
} from "../services/api";
import { ApiError } from "../services/apiError";
import {
  parsePayPalReturn,
  stripPayPalReturnParams,
} from "../lib/paypalReturn";
import type { CaptureResult, TransactionRecord } from "../types";

/** Why a capture attempt did not end in a (new) capture. */
export type CaptureErrorReason =
  | "order_not_found"
  | "order_not_approved"
  | "capture_failed"
  | "network"
  | "invalid_order";

export type PayPalReturnState =
  | { phase: "idle" }
  /** Returned from PayPal approval; the backend capture call is in flight. */
  | { phase: "capturing"; orderId: string }
  /** Backend confirmed the capture. `transaction` is the refreshed ledger
   * record when one exists (authoritative product name / INR price). */
  | {
      phase: "captured";
      orderId: string;
      result: CaptureResult;
      transaction: TransactionRecord | null;
    }
  /** Backend reported the order was captured earlier — shown as the
   * authoritative captured state, never as a new purchase. */
  | {
      phase: "already_captured";
      orderId: string;
      transaction: TransactionRecord | null;
    }
  | {
      phase: "error";
      orderId: string;
      reason: CaptureErrorReason;
      message: string;
    };

export interface PayPalReturnFlow {
  state: PayPalReturnState;
  /** Clear a finished (success or error) capture card. */
  dismiss: () => void;
}

function errorState(orderId: string, cause: unknown): PayPalReturnState {
  if (cause instanceof ApiError) {
    switch (cause.kind) {
      case "not_found":
        return {
          phase: "error",
          orderId,
          reason: "order_not_found",
          message:
            "PayPal has no order with this id. No payment was captured.",
        };
      case "conflict":
        // 409 that is NOT "already captured" (handled by the caller) means
        // the buyer has not approved the order on PayPal yet.
        return {
          phase: "error",
          orderId,
          reason: "order_not_approved",
          message:
            "This order has not been approved on PayPal yet, so nothing was captured. Approve it on PayPal first, then return here.",
        };
      case "network":
        return {
          phase: "error",
          orderId,
          reason: "network",
          message:
            "PayPilot couldn't reach the server, so the capture could not be confirmed. Check that the backend is running and reload this page from PayPal's return link.",
        };
      case "validation":
        return {
          phase: "error",
          orderId,
          reason: "invalid_order",
          message:
            "The PayPal order id in the return link is not valid. No payment was captured.",
        };
      default:
        // 502 upstream PayPal failure, 503, other 5xx.
        return {
          phase: "error",
          orderId,
          reason: "capture_failed",
          message:
            "The payment could not be captured. No success was recorded — please try again or check the transaction later.",
        };
    }
  }
  return {
    phase: "error",
    orderId,
    reason: "capture_failed",
    message:
      "The payment could not be captured. No success was recorded — please try again.",
  };
}

export function usePayPalReturn(): PayPalReturnFlow {
  const [state, setState] = useState<PayPalReturnState>({ phase: "idle" });

  // Guards against StrictMode double-mount and any re-render: the capture
  // request is issued at most once per page load.
  const startedRef = useRef(false);

  useEffect(() => {
    if (startedRef.current) return;
    startedRef.current = true;

    const paypalReturn = parsePayPalReturn(window.location.search);
    if (!paypalReturn) return; // normal page load — no capture, no request

    const { orderId } = paypalReturn;

    // Refresh safety: drop token/PayerID from the URL before anything else,
    // so reloading the page cannot start a second capture attempt.
    stripPayPalReturnParams();

    setState({ phase: "capturing", orderId });

    capturePurchase(orderId)
      .then(async (result) => {
        // Refresh the ledger record through the existing read endpoint so
        // the success card can show authoritative product name/INR price.
        let transaction: TransactionRecord | null = null;
        if (result.transaction_id) {
          try {
            transaction = await getTransaction(result.transaction_id);
          } catch {
            // Display still works from the CaptureResult alone.
          }
        }
        setState({ phase: "captured", orderId, result, transaction });
      })
      .catch(async (cause: unknown) => {
        console.error("POST /purchase/capture failed:", cause);

        // 409 "already captured": show the authoritative captured state
        // from the ledger instead of treating it as a failure or a new
        // purchase (covers re-entering the PayPal return link).
        if (
          cause instanceof ApiError &&
          cause.kind === "conflict" &&
          /already been captured/i.test(cause.detail ?? cause.message)
        ) {
          let transaction: TransactionRecord | null = null;
          try {
            transaction = await getTransactionByOrder(orderId);
          } catch {
            // Ledger record unavailable — still report the captured fact.
          }
          setState({ phase: "already_captured", orderId, transaction });
          return;
        }

        setState(errorState(orderId, cause));
      });
  }, []);

  const dismiss = useCallback(() => {
    setState((current) =>
      current.phase === "capturing" ? current : { phase: "idle" },
    );
  }, []);

  return { state, dismiss };
}
