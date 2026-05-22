import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { t } from "@/lib/i18n";

/**
 * ResearchTable client island — C1-P5 paper-trading ladder.
 *
 * Asserts:
 *  - each ladder state renders its Badge + the row's paper stats;
 *  - the gate-progress hint ("7/10 trades", "day 4 of 7");
 *  - the promote-to-live control is offered ONLY on a trial_ready row, and
 *    is the danger-passphrase InlineConfirm (phrase + passphrase) — i.e. it
 *    is danger-gated;
 *  - a non-trial_ready / locked row shows a disabled hint, no control.
 *
 * The Server Action is mocked so the test stays DB-free.
 */

vi.mock("@/lib/db", () => ({ db: {} }));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));

const refresh = vi.fn<() => void>();
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh }) }));

const promoteToLiveFollow = vi.fn(async () => ({
  ok: true,
  message: "已升級",
  followRelationId: "fr-1",
}));
vi.mock("@/app/research/actions", () => ({
  promoteToLiveFollow: (...a: unknown[]) => promoteToLiveFollow(...a),
}));

const toastSuccess = vi.fn();
const toastError = vi.fn();
vi.mock("sonner", () => ({
  toast: {
    success: (...a: unknown[]) => toastSuccess(...a),
    error: (...a: unknown[]) => toastError(...a),
  },
}));

import { ResearchTable, type ResearchRow } from "@/app/research/research-table";

const CONFIRM_PHRASE = "確認執行";

const READY = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const PAPER = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const COOLDOWN = "0xcccccccccccccccccccccccccccccccccccccccc";
const LOCKED = "0xdddddddddddddddddddddddddddddddddddddddd";

/** A paper_session started N whole days ago. */
function daysAgo(n: number): Date {
  return new Date(Date.now() - n * 86_400_000);
}

const rows: ResearchRow[] = [
  {
    leader_address: READY,
    display_name: "Ready One",
    lifecycle_state: "trial_ready",
    locked: false,
    cooldown_count: 0,
    paper_state: "open",
    paper_started_at: daysAgo(9),
    paper_trade_count: 14,
    paper_copyable_pnl: "123.45",
    paper_max_drawdown: "-0.08",
    paper_score: "72",
  },
  {
    leader_address: PAPER,
    display_name: null,
    lifecycle_state: "paper",
    locked: false,
    cooldown_count: 0,
    paper_state: "open",
    paper_started_at: daysAgo(4),
    paper_trade_count: 7,
    paper_copyable_pnl: "-12.5",
    paper_max_drawdown: "-0.05",
    paper_score: "55",
  },
  {
    leader_address: COOLDOWN,
    display_name: null,
    lifecycle_state: "cooldown",
    locked: false,
    cooldown_count: 1,
    paper_state: null,
    paper_started_at: null,
    paper_trade_count: null,
    paper_copyable_pnl: null,
    paper_max_drawdown: null,
    paper_score: null,
  },
  {
    leader_address: LOCKED,
    display_name: null,
    lifecycle_state: "trial_ready",
    locked: true,
    cooldown_count: 0,
    paper_state: "open",
    paper_started_at: daysAgo(10),
    paper_trade_count: 20,
    paper_copyable_pnl: "9.0",
    paper_max_drawdown: "-0.02",
    paper_score: "80",
  },
];

beforeEach(() => {
  refresh.mockClear();
  promoteToLiveFollow.mockClear();
  toastSuccess.mockClear();
  toastError.mockClear();
});

describe("ResearchTable — ladder render", () => {
  it("renders one row per candidate with the lifecycle-state Badge", () => {
    render(<ResearchTable rows={rows} />);
    expect(screen.getByText("0xaaaa…aaaa")).toBeInTheDocument();
    expect(screen.getByText("0xbbbb…bbbb")).toBeInTheDocument();
    // trial_ready appears twice (the ready row + the locked row).
    expect(
      screen.getAllByText(t("research.state.trial_ready")).length
    ).toBe(2);
    expect(
      screen.getByText(t("research.state.paper"))
    ).toBeInTheDocument();
    expect(
      screen.getByText(t("research.state.cooldown"))
    ).toBeInTheDocument();
  });

  it("renders paper PnL / days / trade count for a row with a session", () => {
    render(<ResearchTable rows={rows} />);
    // signed USD for the ready row.
    expect(screen.getByText("+$123.45")).toBeInTheDocument();
    // the 4-day paper row.
    expect(
      screen.getByText(t("research.daysValue", { days: 4 }))
    ).toBeInTheDocument();
    expect(screen.getByText("7")).toBeInTheDocument();
  });

  it("shows the no-session placeholder for a non-paper ladder row", () => {
    render(<ResearchTable rows={rows} />);
    // the cooldown row has 3 no-session cells (pnl / days / trades).
    expect(
      screen.getAllByText(t("research.noSession")).length
    ).toBeGreaterThanOrEqual(3);
  });

  it("shows the gate-progress hint for an under-gate paper row", () => {
    render(<ResearchTable rows={rows} />);
    // the paper row has 7/10 trades (the unmet gate shown first).
    expect(
      screen.getByText(
        t("research.gate.trades", { count: 7, need: 10 })
      )
    ).toBeInTheDocument();
  });

  it("shows the ready gate hint when both gates are cleared", () => {
    render(<ResearchTable rows={rows} />);
    // ready row: 14 trades, 9 days — both gates met.
    expect(
      screen.getAllByText(t("research.gate.ready")).length
    ).toBeGreaterThanOrEqual(1);
  });
});

describe("ResearchTable — promote-to-live control", () => {
  it("offers the promote control ONLY on a trial_ready (unlocked) row", () => {
    render(<ResearchTable rows={rows} />);
    // exactly one promote trigger (the READY row; the LOCKED trial_ready row
    // shows a locked hint, the paper/cooldown rows show notReady).
    const triggers = screen.getAllByRole("button", {
      name: new RegExp(t("research.promote.title")),
    });
    expect(triggers.length).toBe(1);
  });

  it("shows the notReady hint on a non-trial_ready row", () => {
    render(<ResearchTable rows={rows} />);
    // paper + cooldown rows are not promotable.
    expect(
      screen.getAllByText(t("research.promote.notReady")).length
    ).toBe(2);
  });

  it("shows the locked hint on a locked trial_ready row (no control)", () => {
    render(<ResearchTable rows={rows} />);
    expect(
      screen.getByText(t("research.promote.locked"))
    ).toBeInTheDocument();
  });

  it("promote is danger-gated: requires the phrase AND passphrase", async () => {
    render(<ResearchTable rows={[rows[0]]} />);
    // expand the inline confirm.
    await userEvent.click(
      screen.getByRole("button", {
        name: new RegExp(t("research.promote.title")),
      })
    );
    // the danger-passphrase field exists (requirePassphrase=true).
    const passField = screen.getByLabelText(
      t("inlineConfirm.passphraseLabel")
    );
    expect(passField).toBeInTheDocument();

    await userEvent.type(
      screen.getByLabelText(
        t("inlineConfirm.confirmPhraseLabel", { phrase: CONFIRM_PHRASE })
      ),
      CONFIRM_PHRASE
    );
    await userEvent.type(passField, "operator-pw");
    await userEvent.click(
      screen.getByRole("button", { name: t("inlineConfirm.submitButton") })
    );

    await waitFor(() =>
      expect(promoteToLiveFollow).toHaveBeenCalledWith({
        leaderAddress: READY,
        confirmText: CONFIRM_PHRASE,
        passphrase: "operator-pw",
      })
    );
    expect(toastSuccess).toHaveBeenCalledWith(
      t("research.promote.successToast")
    );
  });

  it("does not call the action when the confirm phrase is missing", async () => {
    render(<ResearchTable rows={[rows[0]]} />);
    await userEvent.click(
      screen.getByRole("button", {
        name: new RegExp(t("research.promote.title")),
      })
    );
    // submit is disabled until both inputs are valid.
    const submit = screen.getByRole("button", {
      name: t("inlineConfirm.submitButton"),
    });
    expect(submit).toBeDisabled();
    expect(promoteToLiveFollow).not.toHaveBeenCalled();
  });
});
