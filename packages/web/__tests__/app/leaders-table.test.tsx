import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { t } from "@/lib/i18n";

/**
 * LeadersTable client island: renders relationship rows + the per-row
 * Pause/Resume SAFE write (PRD §5.7). The Server Action is mocked so the
 * test stays DB-free; we assert it is called with (id, nextStatus), then a
 * toast fires and router.refresh() re-reads the Server Component.
 *
 * Locators/expected toast text are derived from the i18n dictionary (the
 * SAME keys the island uses), so this asserts STRUCTURE/behaviour and
 * stays correct if a translation string changes.
 */

vi.mock("@/lib/db", () => ({ db: {} }));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));

const refresh = vi.fn<() => void>();
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh }) }));

const setFollowStatus = vi.fn(async (id: string, status: string) => ({
  id,
  status,
}));
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
    // status value is language-neutral data (rendered raw).
    expect(screen.getByText("active")).toBeInTheDocument();
    expect(screen.getByText("paused")).toBeInTheDocument();
    expect(screen.getByText("Main")).toBeInTheDocument();
    expect(screen.getByText("Balanced")).toBeInTheDocument();
  });

  it("uses the not-set fallback (no em dash) for null account/template/last-seen", () => {
    render(<LeadersTable rows={rows} />);
    // paused row has null account AND template -> 2 not-set cells.
    expect(
      screen.getAllByText(t("tables.common.notSet")).length
    ).toBeGreaterThanOrEqual(2);
    expect(
      screen.getByText(t("tables.common.notYet"))
    ).toBeInTheDocument();
  });

  it("active row offers Pause; calls setFollowStatus(id,'paused') + toast + refresh", async () => {
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

  it("paused row offers Resume; calls setFollowStatus(id,'active') + toast + refresh", async () => {
    render(<LeadersTable rows={rows} />);
    const resume = screen.getByRole("button", {
      name: t("tables.leaders.resumeAria", { address: PAUSED }),
    });
    await userEvent.click(resume);
    expect(setFollowStatus).toHaveBeenCalledWith("rel-paused", "active");
    expect(toastSuccess).toHaveBeenCalledWith(
      t("tables.leaders.resumedToast")
    );
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("toasts an error and does not refresh when the action throws", async () => {
    setFollowStatus.mockRejectedValueOnce(new Error("boom"));
    render(<LeadersTable rows={rows} />);
    await userEvent.click(
      screen.getByRole("button", {
        name: t("tables.leaders.pauseAria", { address: ACTIVE }),
      })
    );
    expect(toastError).toHaveBeenCalledWith(
      t("tables.leaders.updateError")
    );
    expect(refresh).not.toHaveBeenCalled();
  });

  it("has NO live-trading enable/disable control anywhere in the table", () => {
    render(<LeadersTable rows={rows} />);
    // Structural guard: the only actionable controls are the per-row
    // Pause/Resume toggles (their aria-labels are the pause/resume keys).
    // Nothing must expose a live-trading enable/disable affordance —
    // that is high-danger + CLI-only (PRD §5.7).
    const pauseLabel = t("tables.leaders.pauseAria", { address: ACTIVE });
    const resumeLabel = t("tables.leaders.resumeAria", { address: PAUSED });
    const allowed = new Set([pauseLabel, resumeLabel]);
    for (const btn of screen.getAllByRole("button")) {
      const label = btn.getAttribute("aria-label") ?? btn.textContent ?? "";
      // Every button is one of the two known SAFE toggles…
      expect(
        allowed.has(label) ||
          label === t("tables.leaders.pause") ||
          label === t("tables.leaders.resume")
      ).toBe(true);
      // …and none mentions the live-trading vocabulary (any locale).
      expect(label.toLowerCase()).not.toMatch(
        /live trading|enable live|disable live/
      );
      expect(label).not.toContain(t("common.liveTrading.locked"));
      expect(label).not.toContain(t("common.liveTrading.unlocked"));
    }
  });
});
