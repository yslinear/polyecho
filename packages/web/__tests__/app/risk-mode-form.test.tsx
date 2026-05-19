import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

/**
 * RiskModeForm island (PRD §5.6 旅行/保守模式). Segmented control upserting
 * the risk_mode settings row; toast + router.refresh(). DB-free.
 */

vi.mock("@/lib/db", () => ({ db: {} }));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));

const refresh = vi.fn<() => void>();
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh }) }));

const setRiskMode = vi.fn(async (mode: string) => ({
  ok: true,
  message: mode === "conservative" ? "已切換為保守/旅行模式" : "已切換為正常模式",
  value: mode,
}));
vi.mock("@/app/settings/actions", () => ({
  setRiskMode: (...a: unknown[]) => setRiskMode(...a),
}));

const toastSuccess = vi.fn();
const toastError = vi.fn();
vi.mock("sonner", () => ({
  toast: {
    success: (...a: unknown[]) => toastSuccess(...a),
    error: (...a: unknown[]) => toastError(...a),
  },
}));

import { RiskModeForm } from "@/app/settings/risk-mode-form";
import { t } from "@/lib/i18n";

// Locators derive from the i18n dictionary, not hardcoded copy: the test
// asserts the §5.6 segmented-control BEHAVIOUR (write value, no-op on the
// current mode, error toast) and stays correct if a zh-TW string changes.
const NORMAL = t("alertsSettings.settings.riskMode.normal");
const CONSERVATIVE = t("alertsSettings.settings.riskMode.conservative");
// Buttons match by accessible name; escape any regex metachars in the
// translated label so "保守 / 旅行模式" stays a literal substring match.
const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const NORMAL_RE = new RegExp(esc(NORMAL));
const CONSERVATIVE_RE = new RegExp(esc(CONSERVATIVE));

beforeEach(() => {
  refresh.mockClear();
  setRiskMode.mockClear();
  toastSuccess.mockClear();
  toastError.mockClear();
});

describe("RiskModeForm", () => {
  it("switches to conservative and writes risk_mode='conservative'", async () => {
    render(<RiskModeForm current="normal" />);
    await userEvent.click(
      screen.getByRole("button", { name: CONSERVATIVE_RE })
    );
    expect(setRiskMode).toHaveBeenCalledWith("conservative");
    expect(toastSuccess).toHaveBeenCalled();
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("status")).toHaveTextContent(CONSERVATIVE);
  });

  it("is a no-op when clicking the already-current mode", async () => {
    render(<RiskModeForm current="normal" />);
    await userEvent.click(
      screen.getByRole("button", { name: NORMAL_RE })
    );
    expect(setRiskMode).not.toHaveBeenCalled();
  });

  it("switches back to normal from conservative", async () => {
    render(<RiskModeForm current="conservative" />);
    await userEvent.click(
      screen.getByRole("button", { name: NORMAL_RE })
    );
    expect(setRiskMode).toHaveBeenCalledWith("normal");
  });

  it("toasts an error when the action throws", async () => {
    setRiskMode.mockRejectedValueOnce(new Error("db down"));
    render(<RiskModeForm current="normal" />);
    await userEvent.click(
      screen.getByRole("button", { name: CONSERVATIVE_RE })
    );
    expect(toastError).toHaveBeenCalled();
  });
});
