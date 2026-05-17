import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

/**
 * CreateDangerousAction + ConfirmPendingRow islands (PRD §5.7 危险 tier).
 *
 * - Create form builds params and calls createPendingAction.
 * - ConfirmPendingRow enforces a HARD 5-second countdown gate before the
 *   passphrase input/confirm button enable, then calls confirmPendingAction.
 * - It is NOT a modal (inline disclosure) per the DESIGN.md ban.
 */

vi.mock("@/lib/db", () => ({ db: {} }));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));

const refresh = vi.fn<() => void>();
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh }) }));

const createPendingAction = vi.fn(async () => ({
  ok: true,
  message: "已建立",
  id: "pa-1",
}));
const confirmPendingAction = vi.fn(async () => ({
  ok: true,
  message: "已確認",
}));
vi.mock("@/app/alerts/actions", () => ({
  createPendingAction: (...a: unknown[]) => createPendingAction(...a),
  confirmPendingAction: (...a: unknown[]) => confirmPendingAction(...a),
}));

const toastSuccess = vi.fn();
const toastError = vi.fn();
vi.mock("sonner", () => ({
  toast: {
    success: (...a: unknown[]) => toastSuccess(...a),
    error: (...a: unknown[]) => toastError(...a),
  },
}));

import {
  CreateDangerousAction,
  ConfirmPendingRow,
} from "@/app/alerts/dangerous-action";

beforeEach(() => {
  refresh.mockClear();
  createPendingAction.mockClear();
  confirmPendingAction.mockClear();
  toastSuccess.mockClear();
  toastError.mockClear();
});

describe("CreateDangerousAction", () => {
  it("is collapsed by default (no modal-first) and expands inline", async () => {
    render(<CreateDangerousAction />);
    const open = screen.getByRole("button", {
      name: "建立危險操作請求",
    });
    await userEvent.click(open);
    expect(
      screen.getByRole("form", { name: "建立危險操作請求" })
    ).toBeInTheDocument();
  });

  it("creates a manual_sell request with target + size", async () => {
    render(<CreateDangerousAction />);
    await userEvent.click(
      screen.getByRole("button", { name: "建立危險操作請求" })
    );
    await userEvent.type(screen.getByLabelText("目標"), "market-1");
    await userEvent.type(screen.getByLabelText("賣出數量"), "100");
    await userEvent.click(
      screen.getByRole("button", { name: "建立待確認請求" })
    );
    await waitFor(() =>
      expect(createPendingAction).toHaveBeenCalledWith("manual_sell", {
        target: "market-1",
        size: "100",
      })
    );
    expect(toastSuccess).toHaveBeenCalled();
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("blocks submit without a target", async () => {
    render(<CreateDangerousAction />);
    await userEvent.click(
      screen.getByRole("button", { name: "建立危險操作請求" })
    );
    await userEvent.click(
      screen.getByRole("button", { name: "建立待確認請求" })
    );
    expect(createPendingAction).not.toHaveBeenCalled();
    expect(toastError).toHaveBeenCalled();
  });
});

describe("ConfirmPendingRow — countdown gate (PRD §5.7)", () => {
  it("starts disabled with a countdown when the 5s gate is closed", () => {
    const future = new Date(Date.now() + 120_000).toISOString();
    // Default gateSeconds is 5 — at first render the gate is closed.
    render(<ConfirmPendingRow id="pa-1" expiresAtIso={future} />);
    const btn = screen.getByRole("button", {
      name: "確認執行此危險操作",
    });
    expect(btn).toBeDisabled();
    expect(btn).toHaveTextContent(/倒數 [1-5] 秒/);
    // Passphrase input is present but disabled behind the gate.
    expect(screen.getByLabelText("確認通行碼")).toBeDisabled();
  });

  it("once the gate elapses, accepts the passphrase and confirms", async () => {
    const future = new Date(Date.now() + 120_000).toISOString();
    // gateSeconds=0 opens the gate immediately (real timers, no flake);
    // production always uses the default 5 (asserted above).
    render(
      <ConfirmPendingRow id="pa-1" expiresAtIso={future} gateSeconds={0} />
    );
    const enabled = await screen.findByRole("button", {
      name: "確認執行此危險操作",
    });
    expect(enabled).toBeEnabled();
    expect(enabled).toHaveTextContent("確認執行");

    await userEvent.type(screen.getByLabelText("確認通行碼"), "secret");
    await userEvent.click(enabled);
    await waitFor(() =>
      expect(confirmPendingAction).toHaveBeenCalledWith("pa-1", "secret")
    );
  });

  it("blocks confirm with an empty passphrase even after the gate opens", async () => {
    const future = new Date(Date.now() + 120_000).toISOString();
    render(
      <ConfirmPendingRow id="pa-1" expiresAtIso={future} gateSeconds={0} />
    );
    const btn = await screen.findByRole("button", {
      name: "確認執行此危險操作",
    });
    await userEvent.click(btn);
    expect(toastError).toHaveBeenCalled();
    expect(confirmPendingAction).not.toHaveBeenCalled();
  });

  it("shows the expired message and no confirm UI when already expired", () => {
    const past = new Date(Date.now() - 1000).toISOString();
    render(<ConfirmPendingRow id="pa-1" expiresAtIso={past} />);
    expect(screen.getByText(/已逾時/)).toBeInTheDocument();
    expect(
      screen.queryByLabelText("確認通行碼")
    ).not.toBeInTheDocument();
  });
});
