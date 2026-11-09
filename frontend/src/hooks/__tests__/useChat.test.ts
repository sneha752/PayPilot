import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useChat } from "../useChat";
import { makeConfirmation, makePendingAction, makePlan } from "../../test/fixtures";
import type { ChatResponse, SessionDetail } from "../../types";

vi.mock("../../services/api", () => ({
  sendChat: vi.fn(),
  confirmSession: vi.fn(),
  getSession: vi.fn(),
}));

import { confirmSession, getSession, sendChat } from "../../services/api";

const sendChatMock = vi.mocked(sendChat);
const confirmSessionMock = vi.mocked(confirmSession);
const getSessionMock = vi.mocked(getSession);

function chatResponseWithPending(): ChatResponse {
  return {
    session_id: "sess_1",
    route: "SHOP",
    assistant_message: "Prepared and waiting for your confirmation.",
    pending_action: makePendingAction(),
    data: { plan: makePlan(), transaction: null, transactions: null },
  };
}

function sessionDetail(pending: ReturnType<typeof makePendingAction> | null): SessionDetail {
  return {
    session_id: "sess_1",
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    turns: [],
    pending_action: pending,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

async function setupWithPendingAction() {
  sendChatMock.mockResolvedValue(chatResponseWithPending());
  const hook = renderHook(() => useChat());
  act(() => hook.result.current.sendMessage("buy those headphones"));
  await waitFor(() =>
    expect(hook.result.current.pendingAction?.action_type).toBe("PURCHASE"),
  );
  return hook;
}

describe("useChat confirmation flow", () => {
  it("tracks the pending action and plan from the chat response", async () => {
    const { result } = await setupWithPendingAction();
    expect(result.current.sessionId).toBe("sess_1");
    expect(result.current.pendingPlan?.selected_product?.id).toBe("HDP006");
  });

  it("prevents duplicate confirmation calls while one is in flight", async () => {
    const { result } = await setupWithPendingAction();

    let resolveConfirm!: (value: ReturnType<typeof makeConfirmation>) => void;
    confirmSessionMock.mockReturnValue(
      new Promise((resolve) => {
        resolveConfirm = resolve;
      }),
    );
    getSessionMock.mockResolvedValue(sessionDetail(null));

    act(() => {
      result.current.confirmPending(true);
    });
    await waitFor(() => expect(result.current.confirming).toBe(true));
    act(() => {
      // Second click while in flight must be ignored.
      result.current.confirmPending(true);
    });

    expect(confirmSessionMock).toHaveBeenCalledTimes(1);
    expect(confirmSessionMock).toHaveBeenCalledWith("sess_1", true);

    await act(async () => {
      resolveConfirm(makeConfirmation());
    });
    await waitFor(() => expect(result.current.confirming).toBe(false));
  });

  it("refreshes session state from the backend after confirmation", async () => {
    const { result } = await setupWithPendingAction();
    confirmSessionMock.mockResolvedValue(makeConfirmation());
    getSessionMock.mockResolvedValue(sessionDetail(null));

    act(() => {
      result.current.confirmPending(true);
    });

    await waitFor(() => expect(result.current.confirmation).not.toBeNull());
    expect(getSessionMock).toHaveBeenCalledWith("sess_1");
    // Backend says no pending action remains — the card's confirm state goes.
    expect(result.current.pendingAction).toBeNull();
    expect(result.current.confirmation?.purchase_result?.status).toBe("order_created");
  });

  it("keeps the proposal pending after an explicit decline", async () => {
    const { result } = await setupWithPendingAction();
    const declined = makeConfirmation({
      confirmed: false,
      executed: false,
      message: "Nothing was executed. The proposed action is still pending.",
      purchase_result: null,
      pending_action: makePendingAction(),
    });
    confirmSessionMock.mockResolvedValue(declined);

    act(() => {
      result.current.confirmPending(false);
    });

    await waitFor(() =>
      expect(result.current.confirmNotice).toMatch(/still pending/),
    );
    expect(confirmSessionMock).toHaveBeenCalledWith("sess_1", false);
    expect(result.current.pendingAction).not.toBeNull();
    expect(result.current.confirmation).toBeNull();
  });

  it("surfaces a clear error and clears the card on 409 (no active action)", async () => {
    const { ApiError } = await import("../../services/apiError");
    const { result } = await setupWithPendingAction();
    confirmSessionMock.mockRejectedValue(
      new ApiError({ message: "no active pending action", status: 409 }),
    );

    act(() => {
      result.current.confirmPending(true);
    });

    await waitFor(() =>
      expect(result.current.confirmError).toMatch(/no longer active/),
    );
    expect(result.current.pendingAction).toBeNull();
    expect(result.current.confirmation).toBeNull();
  });

  it("shows an error without pretending success on server failure", async () => {
    const { ApiError } = await import("../../services/apiError");
    const { result } = await setupWithPendingAction();
    confirmSessionMock.mockRejectedValue(
      new ApiError({ message: "PayPal order failed", status: 502 }),
    );

    act(() => {
      result.current.confirmPending(true);
    });

    await waitFor(() =>
      expect(result.current.confirmError).toMatch(/Nothing was charged/),
    );
    // The action is retained server-side on upstream failure; the card
    // stays so the user can retry.
    expect(result.current.pendingAction).not.toBeNull();
    expect(result.current.confirmation).toBeNull();
  });
});
