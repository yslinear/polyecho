import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

/**
 * LeadersTable client island: renders relationship rows + the per-row
 * Pause/Resume SAFE write (PRD §5.7). The Server Action is mocked so the
 * test stays DB-free; we assert it is called with (id, nextStatus), then a
 * toast fires and router.refresh() re-reads the Server Component.
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
    expect(screen.getByText("active")).toBeInTheDocument();
    expect(screen.getByText("paused")).toBeInTheDocument();
    expect(screen.getByText("Main")).toBeInTheDocument();
    expect(screen.getByText("Balanced")).toBeInTheDocument();
  });

  it("uses 'Not set' (no em dash) for null account/template/last-seen", () => {
    render(<LeadersTable rows={rows} />);
    expect(screen.getAllByText("Not set").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("Not yet")).toBeInTheDocument();
  });

  it("active row offers Pause; calls setFollowStatus(id,'paused') + toast + refresh", async () => {
    render(<LeadersTable rows={rows} />);
    const pause = screen.getByRole("button", {
      name: `Pause copying ${ACTIVE}`,
    });
    await userEvent.click(pause);
    expect(setFollowStatus).toHaveBeenCalledWith("rel-active", "paused");
    expect(toastSuccess).toHaveBeenCalledWith(
      expect.stringMatching(/paused/i)
    );
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("paused row offers Resume; calls setFollowStatus(id,'active') + toast + refresh", async () => {
    render(<LeadersTable rows={rows} />);
    const resume = screen.getByRole("button", {
      name: `Resume copying ${PAUSED}`,
    });
    await userEvent.click(resume);
    expect(setFollowStatus).toHaveBeenCalledWith("rel-paused", "active");
    expect(toastSuccess).toHaveBeenCalledWith(
      expect.stringMatching(/resumed/i)
    );
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("toasts an error and does not refresh when the action throws", async () => {
    setFollowStatus.mockRejectedValueOnce(new Error("boom"));
    render(<LeadersTable rows={rows} />);
    await userEvent.click(
      screen.getByRole("button", { name: `Pause copying ${ACTIVE}` })
    );
    expect(toastError).toHaveBeenCalledWith(
      "Failed to update follow status"
    );
    expect(refresh).not.toHaveBeenCalled();
  });

  it("has NO live-trading enable/disable control anywhere in the table", () => {
    render(<LeadersTable rows={rows} />);
    for (const btn of screen.getAllByRole("button")) {
      const label = (btn.getAttribute("aria-label") ?? btn.textContent ?? "")
        .toLowerCase();
      expect(label).not.toMatch(/live trading|enable live|disable live/);
    }
  });
});
