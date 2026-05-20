import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

/**
 * GlobalPauseToggle island (PRD §5.8 top-bar 全域暫停/恢復).
 *
 * P7d / R24 asymmetry:
 *   - PAUSE  (paused=false → true): single click.
 *   - RESUME (paused=true  → false): InlineConfirm with BOTH CONFIRM_PHRASE
 *     AND a danger passphrase before the action fires (the highest tier).
 *
 * Reflects the layout-supplied `paused` state. DB-free (action mocked).
 */

vi.mock("@/lib/db", () => ({ db: {} }));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));

const refresh = vi.fn<() => void>();
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh }) }));

const setGlobalPaused = vi.fn(
  async (
    paused: boolean,
    _confirm?: { confirmText?: string; passphrase?: string }
  ) => ({
    ok: true,
    message: paused
      ? "已全域暫停：trader-core 停止所有跟單。"
      : "已恢復：trader-core 於下一筆領隊交易繼續跟單。",
    paused,
  })
);
vi.mock("@/app/settings/actions", () => ({
  setGlobalPaused: (...a: unknown[]) => setGlobalPaused(...a),
}));

const toastSuccess = vi.fn();
const toastError = vi.fn();
vi.mock("sonner", () => ({
  toast: {
    success: (...a: unknown[]) => toastSuccess(...a),
    error: (...a: unknown[]) => toastError(...a),
  },
}));

import { GlobalPauseToggle } from "@/components/global-pause-toggle";
import { t } from "@/lib/i18n";

const CONFIRM_PHRASE = "確認執行";
const OPEN_RESUME = t("inlineConfirm.openButton", { title: "恢復全部跟單" });
const RESUME_SUBMIT = t("inlineConfirm.submitButton");
const CONFIRM_LABEL = t("inlineConfirm.confirmPhraseLabel", {
  phrase: CONFIRM_PHRASE,
});
const PASS_LABEL = t("inlineConfirm.passphraseLabel");

beforeEach(() => {
  refresh.mockClear();
  setGlobalPaused.mockClear();
  toastSuccess.mockClear();
  toastError.mockClear();
});

describe("GlobalPauseToggle", () => {
  it("shows '暫停全部' when running and pauses on a single click (no confirm)", async () => {
    render(<GlobalPauseToggle paused={false} />);
    const btn = screen.getByRole("button", { name: "暫停全部跟單" });
    expect(btn).toHaveTextContent("暫停全部");
    await userEvent.click(btn);
    expect(setGlobalPaused).toHaveBeenCalledWith(true);
    expect(toastSuccess).toHaveBeenCalled();
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("renders an InlineConfirm trigger when paused (no direct resume on click)", async () => {
    render(<GlobalPauseToggle paused={true} />);
    expect(
      screen.getByRole("button", { name: new RegExp("恢復全部跟單") })
    ).toBeInTheDocument();
    expect(setGlobalPaused).not.toHaveBeenCalled();
  });

  it("requires CONFIRM_PHRASE + passphrase before calling setGlobalPaused(false)", async () => {
    render(<GlobalPauseToggle paused={true} />);
    await userEvent.click(
      screen.getByRole("button", { name: new RegExp("恢復全部跟單") })
    );
    await userEvent.type(screen.getByLabelText(CONFIRM_LABEL), CONFIRM_PHRASE);
    await userEvent.type(screen.getByLabelText(PASS_LABEL), "operator-secret");
    await userEvent.click(screen.getByRole("button", { name: RESUME_SUBMIT }));
    await waitFor(() =>
      expect(setGlobalPaused).toHaveBeenCalledWith(false, {
        confirmText: CONFIRM_PHRASE,
        passphrase: "operator-secret",
      })
    );
    expect(toastSuccess).toHaveBeenCalled();
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("does not call setGlobalPaused when confirm phrase is wrong", async () => {
    render(<GlobalPauseToggle paused={true} />);
    await userEvent.click(
      screen.getByRole("button", { name: new RegExp("恢復全部跟單") })
    );
    await userEvent.type(screen.getByLabelText(CONFIRM_LABEL), "wrong");
    await userEvent.type(screen.getByLabelText(PASS_LABEL), "secret");
    const submit = screen.getByRole("button", { name: RESUME_SUBMIT });
    // The submit stays disabled until BOTH gates are visually valid.
    expect(submit).toBeDisabled();
    expect(setGlobalPaused).not.toHaveBeenCalled();
  });

  it("toasts an error when the pause action throws", async () => {
    setGlobalPaused.mockRejectedValueOnce(new Error("db down"));
    render(<GlobalPauseToggle paused={false} />);
    await userEvent.click(
      screen.getByRole("button", { name: "暫停全部跟單" })
    );
    expect(toastError).toHaveBeenCalled();
    expect(refresh).not.toHaveBeenCalled();
  });
});
