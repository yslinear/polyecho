import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { t } from "@/lib/i18n";

/**
 * LeadersTable client island: per-row Pause / Resume SAFE write (PRD §5.7).
 *
 * P7d / R24 asymmetry:
 *   - active row → Pause is a single click (safe-direction).
 *   - paused row → Resume renders the InlineConfirm primitive that forwards
 *     `{ confirmText }` to setFollowStatus(id, 'active', …).
 *
 * The Server Action is mocked so the test stays DB-free.
 */

vi.mock("@/lib/db", () => ({ db: {} }));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));

const refresh = vi.fn<() => void>();
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh }) }));

const setFollowStatus = vi.fn(
  async (id: string, status: string, _confirm?: { confirmText?: string }) => ({
    id,
    status,
  })
);
vi.mock("@/app/leaders/actions", () => ({
  setFollowStatus: (...a: unknown[]) => setFollowStatus(...a),
}));

const toastSuccess = vi.fn();
const toastError = vi.fn();
vi.mock("sonner", () => ({
  toast: {
    success: (...a: unknown[]) => toastSuccess(...a),
    error: (...a: unknown[]) => toastError(...a),
  },
}));

import { LeadersTable, type LeaderRow } from "@/app/leaders/leaders-table";

const CONFIRM_PHRASE = "確認執行";
const ACTIVE = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const PAUSED = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

const rows: LeaderRow[] = [
  {
    id: "rel-active",
    leader_address: ACTIVE,
    account_name: "Main",
    template_name: "Balanced",
    status: "active",
    created_at: new Date("2026-01-01T00:00:00Z"),
    last_seen_ts: new Date("2026-05-01T12:30:00Z"),
  },
  {
    id: "rel-paused",
    leader_address: PAUSED,
    account_name: null,
    template_name: null,
    status: "paused",
    created_at: new Date("2026-02-02T00:00:00Z"),
    last_seen_ts: null,
  },
];

beforeEach(() => {
  refresh.mockClear();
  setFollowStatus.mockClear();
  toastSuccess.mockClear();
  toastError.mockClear();
});

describe("LeadersTable", () => {
  it("renders one row per relationship with shortened wallet + status badge", () => {
    render(<LeadersTable rows={rows} />);
    expect(screen.getByText("0xaaaa…aaaa")).toBeInTheDocument();
    expect(screen.getByText("0xbbbb…bbbb")).toBeInTheDocument();
    expect(screen.getByText("active")).toBeInTheDocument();
    expect(screen.getByText("paused")).toBeInTheDocument();
    expect(screen.getByText("Main")).toBeInTheDocument();
    expect(screen.getByText("Balanced")).toBeInTheDocument();
  });

  it("uses the not-set fallback for null account/template/last-seen", () => {
    render(<LeadersTable rows={rows} />);
    expect(
      screen.getAllByText(t("tables.common.notSet")).length
    ).toBeGreaterThanOrEqual(2);
    expect(screen.getByText(t("tables.common.notYet"))).toBeInTheDocument();
  });

  it("active row Pause is single-click; calls setFollowStatus(id,'paused')", async () => {
    render(<LeadersTable rows={rows} />);
    const pause = screen.getByRole("button", {
      name: t("tables.leaders.pauseAria", { address: ACTIVE }),
    });
    await userEvent.click(pause);
    expect(setFollowStatus).toHaveBeenCalledWith("rel-active", "paused");
    expect(toastSuccess).toHaveBeenCalledWith(
      t("tables.leaders.pausedToast")
    );
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("paused row Resume requires the inline-confirm phrase", async () => {
    render(<LeadersTable rows={rows} />);
    const trigger = screen.getByRole("button", {
      name: new RegExp(t("tables.leaders.resume")),
    });
    await userEvent.click(trigger);
    await userEvent.type(
      screen.getByLabelText(
        t("inlineConfirm.confirmPhraseLabel", { phrase: CONFIRM_PHRASE })
      ),
      CONFIRM_PHRASE
    );
    await userEvent.click(
      screen.getByRole("button", { name: t("inlineConfirm.submitButton") })
    );
    await waitFor(() =>
      expect(setFollowStatus).toHaveBeenCalledWith(
        "rel-paused",
        "active",
        { confirmText: CONFIRM_PHRASE }
      )
    );
    expect(toastSuccess).toHaveBeenCalledWith(
      t("tables.leaders.resumedToast")
    );
  });

  it("toasts an error and does not refresh when the pause action throws", async () => {
    setFollowStatus.mockRejectedValueOnce(new Error("boom"));
    render(<LeadersTable rows={rows} />);
    await userEvent.click(
      screen.getByRole("button", {
        name: t("tables.leaders.pauseAria", { address: ACTIVE }),
      })
    );
    expect(toastError).toHaveBeenCalledWith(t("tables.leaders.updateError"));
    expect(refresh).not.toHaveBeenCalled();
  });

  it("has NO live-trading enable/disable control anywhere in the table", () => {
    render(<LeadersTable rows={rows} />);
    // Structural guard: no button advertises live-trading vocabulary.
    for (const btn of screen.getAllByRole("button")) {
      const label = btn.getAttribute("aria-label") ?? btn.textContent ?? "";
      expect(label.toLowerCase()).not.toMatch(
        /live trading|enable live|disable live/
      );
      expect(label).not.toContain(t("common.liveTrading.locked"));
      expect(label).not.toContain(t("common.liveTrading.unlocked"));
    }
  });
});
