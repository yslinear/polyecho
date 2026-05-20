import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

/**
 * RiskModeForm island (PRD §5.6 旅行/保守模式).
 *
 * P7d / R24 asymmetry:
 *   - "conservative" stays single-click (safe-direction).
 *   - "normal" (LEAVE conservative) renders the InlineConfirm primitive
 *     and forwards `{ confirmText }` to setRiskMode.
 */

vi.mock("@/lib/db", () => ({ db: {} }));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));

const refresh = vi.fn<() => void>();
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh }) }));

const setRiskMode = vi.fn(
  async (mode: string, _confirm?: { confirmText?: string }) => ({
    ok: true,
    message:
      mode === "conservative" ? "已切換為保守/旅行模式" : "已切換為正常模式",
    value: mode,
  })
);
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

const CONFIRM_PHRASE = "確認執行";
const NORMAL = t("alertsSettings.settings.riskMode.normal");
const CONSERVATIVE = t("alertsSettings.settings.riskMode.conservative");
const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const NORMAL_RE = new RegExp(esc(NORMAL));
const CONSERVATIVE_RE = new RegExp(esc(CONSERVATIVE));
const RESUME_SUBMIT = t("inlineConfirm.submitButton");
const CONFIRM_LABEL = t("inlineConfirm.confirmPhraseLabel", {
  phrase: CONFIRM_PHRASE,
});

beforeEach(() => {
  refresh.mockClear();
  setRiskMode.mockClear();
  toastSuccess.mockClear();
  toastError.mockClear();
});

describe("RiskModeForm", () => {
  it("switches to conservative without an inline confirm (safe-direction)", async () => {
    render(<RiskModeForm current="normal" />);
    await userEvent.click(
      screen.getByRole("button", { name: CONSERVATIVE_RE })
    );
    expect(setRiskMode).toHaveBeenCalledWith("conservative");
    expect(toastSuccess).toHaveBeenCalled();
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("status")).toHaveTextContent(CONSERVATIVE);
  });

  it("requires inline confirm to leave conservative for normal", async () => {
    render(<RiskModeForm current="conservative" />);
    // Trigger (collapsed) for the risk-increasing direction.
    const trigger = screen.getByRole("button", { name: NORMAL_RE });
    await userEvent.click(trigger);
    // Inline confirm expanded; fill the phrase + submit.
    await userEvent.type(screen.getByLabelText(CONFIRM_LABEL), CONFIRM_PHRASE);
    await userEvent.click(screen.getByRole("button", { name: RESUME_SUBMIT }));
    await waitFor(() =>
      expect(setRiskMode).toHaveBeenCalledWith("normal", {
        confirmText: CONFIRM_PHRASE,
      })
    );
    expect(toastSuccess).toHaveBeenCalled();
  });

  it("disables the inline confirm submit until the phrase matches", async () => {
    render(<RiskModeForm current="conservative" />);
    await userEvent.click(screen.getByRole("button", { name: NORMAL_RE }));
    const submit = screen.getByRole("button", { name: RESUME_SUBMIT });
    expect(submit).toBeDisabled();
    await userEvent.type(screen.getByLabelText(CONFIRM_LABEL), "nope");
    expect(submit).toBeDisabled();
    expect(setRiskMode).not.toHaveBeenCalled();
  });
});
