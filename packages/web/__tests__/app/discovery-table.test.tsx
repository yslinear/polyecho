import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { t } from "@/lib/i18n";

/**
 * DiscoveryTable — merged candidates-funnel client island (Phase-1 merge).
 *
 * Asserts the operator-approved contract:
 *
 *  - one row per candidate; the chip strip writes ?state= (link href);
 *  - each chip swaps its column set + applies its default sort;
 *  - switching chip via the URL `chip` prop also RESETS pagination to 1
 *    (page-size 50, RESEARCH_PAGE_SIZE pattern);
 *  - the 可升 live chip count badge appears only when count > 0;
 *  - the promote-to-live control is rendered ONLY on a trial_ready row
 *    inside the trial_ready chip view, NOT on other rows / chips;
 *  - user_status is NOT rendered anywhere (no column, no filter, no action).
 *
 * The Server Action + db are mocked so the suite is DB-free.
 */

// discovery-table imports a *type* from ./page, but page.tsx also imports
// "@/lib/db" at module load. Stub db + env so importing stays DB-free.
vi.mock("@/lib/db", () => ({ db: {} }));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));

const push = vi.fn<(href: string) => void>();
const refresh = vi.fn<() => void>();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, refresh }),
}));

const quickBacktest = vi.fn(async () => ({ id: "job-123" }));
vi.mock("@/app/discovery/actions", () => ({
  quickBacktest: (...a: unknown[]) => quickBacktest(...a),
}));

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

import {
  DiscoveryTable,
  type ChipState,
  type DiscoveryRow,
} from "@/app/discovery/discovery-table";

const CONFIRM_PHRASE = "確認執行";

function daysAgo(n: number): Date {
  return new Date(Date.now() - n * 86_400_000);
}

function makeRow(p: Partial<DiscoveryRow>): DiscoveryRow {
  return {
    wallet_address: "0x0000000000000000000000000000000000000000",
    display_name: null,
    strategy_type: null,
    profit_score: null,
    replicability_score: null,
    total_score: null,
    risk_flags: null,
    last_seen_ts: null,
    last_active_at: null,
    lifecycle_state: "discovered",
    locked: false,
    cooldown_count: 0,
    cooldown_until: null,
    first_discovered_at: null,
    updated_at: new Date(),
    paper_state: null,
    paper_started_at: null,
    paper_trade_count: null,
    paper_copyable_pnl: null,
    paper_max_drawdown: null,
    paper_score: null,
    ...p,
  };
}

const ALPHA = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const BETA = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const GAMMA = "0xcccccccccccccccccccccccccccccccccccccccc";
const DELTA = "0xdddddddddddddddddddddddddddddddddddddddd";
const EPSILON = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee";

const leaderboardRows: DiscoveryRow[] = [
  makeRow({
    wallet_address: ALPHA,
    display_name: "Alpha",
    strategy_type: "Scalper",
    total_score: "80",
    profit_score: "75",
    replicability_score: "70",
    last_seen_ts: daysAgo(1),
    lifecycle_state: "discovered",
    risk_flags: ["insider_suspect"],
  }),
  makeRow({
    wallet_address: BETA,
    display_name: "Beta",
    strategy_type: "Swing",
    total_score: "40",
    profit_score: "30",
    replicability_score: "20",
    last_seen_ts: daysAgo(20),
    lifecycle_state: "candidate",
  }),
  makeRow({
    wallet_address: GAMMA,
    display_name: "Gamma",
    strategy_type: "Scalper",
    total_score: "60",
    profit_score: "55",
    replicability_score: "50",
    last_seen_ts: daysAgo(5),
    lifecycle_state: "discovered",
  }),
];

const activeRows: DiscoveryRow[] = [
  makeRow({
    wallet_address: BETA,
    display_name: "Beta",
    lifecycle_state: "paper",
    paper_state: "open",
    paper_started_at: daysAgo(4),
    paper_trade_count: 7,
    paper_copyable_pnl: "-12.5",
    paper_max_drawdown: "-0.05",
    paper_score: "55",
  }),
  makeRow({
    wallet_address: DELTA,
    display_name: "Delta",
    lifecycle_state: "candidate",
    paper_state: null,
    paper_started_at: null,
    paper_trade_count: null,
    paper_copyable_pnl: null,
    paper_score: "20",
  }),
];

const trialReadyRows: DiscoveryRow[] = [
  makeRow({
    wallet_address: ALPHA,
    display_name: "Alpha",
    lifecycle_state: "trial_ready",
    locked: false,
    paper_state: "open",
    paper_started_at: daysAgo(9),
    paper_trade_count: 14,
    paper_copyable_pnl: "123.45",
    paper_score: "72",
  }),
  makeRow({
    wallet_address: EPSILON,
    display_name: "Epsilon",
    lifecycle_state: "trial_ready",
    locked: true,
    paper_state: "open",
    paper_started_at: daysAgo(10),
    paper_trade_count: 20,
    paper_copyable_pnl: "9.0",
    paper_score: "80",
  }),
];

const retiredRows: DiscoveryRow[] = [
  makeRow({
    wallet_address: GAMMA,
    display_name: "Gamma",
    lifecycle_state: "retired",
    cooldown_count: 3,
    first_discovered_at: daysAgo(30),
  }),
];

beforeEach(() => {
  push.mockClear();
  refresh.mockClear();
  quickBacktest.mockClear();
  promoteToLiveFollow.mockClear();
  toastSuccess.mockClear();
  toastError.mockClear();
});

function bodyRows() {
  // Real data rows carry an aria-label "View leader 0x…".
  return screen
    .queryAllByRole("row")
    .filter((r) => r.getAttribute("aria-label")?.startsWith("View leader"));
}

describe("DiscoveryTable — chip strip", () => {
  it("renders all five chips as links that write ?state=…", () => {
    render(
      <DiscoveryTable
        data={leaderboardRows}
        chip="discovered"
        trialReadyCount={0}
      />,
    );

    const expected: { label: string; state: ChipState }[] = [
      { label: t("discovery.list.chips.discovered"), state: "discovered" },
      { label: t("discovery.list.chips.active"), state: "active" },
      { label: t("discovery.list.chips.trialReady"), state: "trial_ready" },
      { label: t("discovery.list.chips.retired"), state: "retired" },
      { label: t("discovery.list.chips.all"), state: "all" },
    ];
    for (const { label, state } of expected) {
      const link = screen.getByRole("link", { name: new RegExp(label) });
      expect(link).toHaveAttribute("href", `/discovery?state=${state}`);
    }
  });

  it("marks the active chip with aria-current='page'", () => {
    render(
      <DiscoveryTable
        data={leaderboardRows}
        chip="active"
        trialReadyCount={0}
      />,
    );
    const activeChip = screen.getByRole("link", {
      name: new RegExp(t("discovery.list.chips.active")),
    });
    expect(activeChip).toHaveAttribute("aria-current", "page");

    const inactiveChip = screen.getByRole("link", {
      name: new RegExp(t("discovery.list.chips.discovered")),
    });
    expect(inactiveChip).not.toHaveAttribute("aria-current");
  });

  it("renders the trial_ready count badge ONLY when count > 0", () => {
    const { rerender } = render(
      <DiscoveryTable
        data={leaderboardRows}
        chip="discovered"
        trialReadyCount={0}
      />,
    );
    // count=0 → no badge.
    const chip = screen.getByRole("link", {
      name: new RegExp(t("discovery.list.chips.trialReady")),
    });
    expect(within(chip).queryByText("0")).not.toBeInTheDocument();

    // count=3 → badge "3" appears inside the chip.
    rerender(
      <DiscoveryTable
        data={leaderboardRows}
        chip="discovered"
        trialReadyCount={3}
      />,
    );
    const chip2 = screen.getByRole("link", {
      name: new RegExp(t("discovery.list.chips.trialReady")),
    });
    expect(within(chip2).getByText("3")).toBeInTheDocument();
  });
});

describe("DiscoveryTable — chip-aware columns + default sort", () => {
  it("排行榜 chip shows the leaderboard columns + sorts by total_score DESC", () => {
    render(
      <DiscoveryTable
        data={leaderboardRows}
        chip="discovered"
        trialReadyCount={0}
      />,
    );
    // total column header is present and active (descending).
    const totalBtn = screen.getByRole("button", {
      name: t("discovery.table.columns.total"),
    });
    const totalTh = totalBtn.closest("th")!;
    expect(totalTh).toHaveAttribute("aria-sort", "descending");

    // ladder-only columns must NOT be present on this chip.
    expect(
      screen.queryByText(t("discovery.table.columns.copyability")),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText(t("discovery.table.columns.paperPnl")),
    ).not.toBeInTheDocument();

    // freshness column present.
    expect(
      screen.getByRole("button", { name: t("discovery.table.columns.lastSeen") }),
    ).toBeInTheDocument();

    // rows are sorted total_score DESC: Alpha(80), Gamma(60), Beta(40).
    const walletCells = bodyRows().map(
      (r) => within(r).getAllByRole("cell")[0].textContent ?? "",
    );
    expect(walletCells[0]).toContain("0xaaaa");
    expect(walletCells[1]).toContain("0xcccc");
    expect(walletCells[2]).toContain("0xbbbb");
  });

  it("驗證中 chip shows ladder columns + sorts by copyability DESC", () => {
    render(
      <DiscoveryTable data={activeRows} chip="active" trialReadyCount={0} />,
    );
    const copyBtn = screen.getByRole("button", {
      name: t("discovery.table.columns.copyability"),
    });
    const copyTh = copyBtn.closest("th")!;
    expect(copyTh).toHaveAttribute("aria-sort", "descending");

    // Beta(score=55) > Delta(score=20) ; Beta first.
    const wallets = bodyRows().map(
      (r) => within(r).getAllByRole("cell")[0].textContent ?? "",
    );
    expect(wallets[0]).toContain("0xbbbb");
    expect(wallets[1]).toContain("0xdddd");

    // leaderboard-only columns (total, freshness) hidden on this chip.
    expect(
      screen.queryByRole("button", { name: t("discovery.table.columns.total") }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: t("discovery.table.columns.lastSeen") }),
    ).not.toBeInTheDocument();

    // gate-progress hint is present for the paper row.
    expect(
      screen.getByText(t("discovery.table.gate.trades", { count: 7, need: 10 })),
    ).toBeInTheDocument();
  });

  it("可升 live chip adds the promote-to-live ACTION column and sorts by copyability DESC", () => {
    render(
      <DiscoveryTable
        data={trialReadyRows}
        chip="trial_ready"
        trialReadyCount={2}
      />,
    );
    // copyability column is the default sort.
    const copyBtn = screen.getByRole("button", {
      name: t("discovery.table.columns.copyability"),
    });
    expect(copyBtn.closest("th")).toHaveAttribute("aria-sort", "descending");

    // gate hint present on the gate-cleared trial_ready rows (both rows
    // here clear 10 trades + 7 days).
    expect(
      screen.getAllByText(t("discovery.table.gate.ready")).length,
    ).toBeGreaterThanOrEqual(1);
  });

  it("已退休 chip shows cooldown + created_at + sorts by createdAt DESC", () => {
    render(
      <DiscoveryTable
        data={retiredRows}
        chip="retired"
        trialReadyCount={0}
      />,
    );
    const cooldownBtn = screen.getByRole("button", {
      name: t("discovery.table.columns.cooldownCount"),
    });
    expect(cooldownBtn).toBeInTheDocument();
    const createdBtn = screen.getByRole("button", {
      name: t("discovery.table.columns.createdAt"),
    });
    expect(createdBtn.closest("th")).toHaveAttribute("aria-sort", "descending");
  });
});

describe("DiscoveryTable — pagination", () => {
  it("resets to page 1 on chip change", async () => {
    // Build 60 rows so we get 2 pages at PAGE_SIZE=50.
    const many = Array.from({ length: 60 }, (_, i) =>
      makeRow({
        wallet_address:
          "0x" + String(i).padStart(40, "0"),
        total_score: String(60 - i),
        lifecycle_state: "discovered",
      }),
    );
    const { rerender } = render(
      <DiscoveryTable data={many} chip="discovered" trialReadyCount={0} />,
    );
    // Initial page == 1 of 2.
    expect(
      screen.getByText(t("discovery.table.pagination.pageOf", { page: 1, total: 2 })),
    ).toBeInTheDocument();

    // Advance to page 2.
    await userEvent.click(
      screen.getByRole("button", {
        name: t("discovery.table.pagination.nextPage"),
      }),
    );
    expect(
      screen.getByText(t("discovery.table.pagination.pageOf", { page: 2, total: 2 })),
    ).toBeInTheDocument();

    // Now switch chips — pagination must snap back to page 1.
    rerender(
      <DiscoveryTable data={many} chip="all" trialReadyCount={0} />,
    );
    expect(
      screen.getByText(t("discovery.table.pagination.pageOf", { page: 1, total: 2 })),
    ).toBeInTheDocument();
  });
});

describe("DiscoveryTable — promote-to-live gate", () => {
  it("offers the promote trigger ONLY on a trial_ready (unlocked) row inside the trial_ready chip", () => {
    render(
      <DiscoveryTable
        data={trialReadyRows}
        chip="trial_ready"
        trialReadyCount={2}
      />,
    );
    const triggers = screen.getAllByRole("button", {
      name: new RegExp(t("discovery.table.promote.title")),
    });
    expect(triggers.length).toBe(1); // alpha only (epsilon is locked)
    expect(
      screen.getByText(t("discovery.table.promote.locked")),
    ).toBeInTheDocument();
  });

  it("does NOT render the promote control on a non-trial_ready chip", () => {
    render(
      <DiscoveryTable
        data={activeRows}
        chip="active"
        trialReadyCount={2}
      />,
    );
    expect(
      screen.queryByRole("button", {
        name: new RegExp(t("discovery.table.promote.title")),
      }),
    ).not.toBeInTheDocument();
  });

  it("promote is danger-gated: requires the confirm phrase AND the passphrase", async () => {
    const ready = trialReadyRows[0];
    render(
      <DiscoveryTable
        data={[ready]}
        chip="trial_ready"
        trialReadyCount={1}
      />,
    );

    await userEvent.click(
      screen.getByRole("button", {
        name: new RegExp(t("discovery.table.promote.title")),
      }),
    );

    const passField = screen.getByLabelText(t("inlineConfirm.passphraseLabel"));
    expect(passField).toBeInTheDocument();
    await userEvent.type(
      screen.getByLabelText(
        t("inlineConfirm.confirmPhraseLabel", { phrase: CONFIRM_PHRASE }),
      ),
      CONFIRM_PHRASE,
    );
    await userEvent.type(passField, "operator-pw");
    await userEvent.click(
      screen.getByRole("button", { name: t("inlineConfirm.submitButton") }),
    );

    await waitFor(() =>
      expect(promoteToLiveFollow).toHaveBeenCalledWith({
        leaderAddress: ready.wallet_address,
        confirmText: CONFIRM_PHRASE,
        passphrase: "operator-pw",
      }),
    );
    expect(toastSuccess).toHaveBeenCalledWith(
      t("discovery.table.promote.successToast"),
    );
  });
});

describe("DiscoveryTable — no user_status UI", () => {
  it("never renders the legacy status column, status filter or follow action", () => {
    render(
      <DiscoveryTable
        data={leaderboardRows}
        chip="discovered"
        trialReadyCount={0}
      />,
    );
    // The legacy "狀態" facet/column lived under discovery.table.columns.status
    // (which is removed). The new layout uses lifecycle_state instead.
    expect(screen.queryByText("狀態")).not.toBeInTheDocument();
    // Legacy follow/watch/reject actions are gone from the row menu.
    expect(screen.queryByText("跟單中")).not.toBeInTheDocument();
  });
});
