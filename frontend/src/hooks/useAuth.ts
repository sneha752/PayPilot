/**
 * React binding for the in-memory auth store (Phase 11F-H).
 *
 * useSyncExternalStore subscribes the component tree to login/logout/expiry
 * changes so the login gate re-renders the moment the token appears or is
 * cleared (including when httpClient drops it on a 401).
 */

import { useSyncExternalStore } from "react";
import { isAuthenticated, subscribe } from "../services/authService";

export function useIsAuthenticated(): boolean {
  return useSyncExternalStore(subscribe, isAuthenticated, isAuthenticated);
}
