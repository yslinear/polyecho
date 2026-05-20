import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// discovery-table imports a *type* from ./page, but page.tsx also imports
// "@/lib/db" at module load (which throws without POLYECHO_DB_URL_DRIZZLE).
// Stub db + env so importing the island stays DB-free.
vi.mock("@/lib/db", () => ({ db: {} }));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));

// next/navigation is server-bound; mock useRouter so row navigation works.
const push = vi.fn<(href: string) => void>();
const refresh = vi.fn<() => void>();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, refresh }),
}));

// Server Actions are DB-bound; mock the module so the island stays DB-free.
const quickBacktest = vi.fn(async () => ({ id: "job-123" }));
const updateLeaderStatus = vi.fn(
  async (wallet: string, status: string) => ({ wallet, status })
);
vi.mock("@/app/discovery/actions", () => ({
  quickBacktest: (...a: unknown[]) => quickBacktest(...a),
  updateLeaderStatus: (...a: unknown[]) => updateLeaderStatus(...a),
}));

// sonner toast — assert messages without a real Toaster.
const toastSuccess = vi.fn();
const toastError = vi.fn();
vi.mock("sonner", () => ({
  toast: {
    success: (...a: unknown[]) => toastSuccess(...a),
    error: (...a: unknown[]) => toastError(...a),
  },
}));

import { DiscoveryTable } from "@/app/discovery/discovery-table";
import type { CandidateLeaderRow } from "@/app/discovery/page";
import { t } from "@/lib/i18n";

// Locators are derived from the i18n dictionary, never hardcoded copy: the
// suite asserts table STRUCTURE / behaviour (sort, filter, paginate, row
// actions) and stays correct if a zh-TW string changes — while still proving
// the i18n seam renders an accessible name for every control. Pattern mirrors
// __tests__/components/app-sidebar.test.tsx.
const TX = {
  cardTitle: t("discovery.table.cardTitle"),
  search: t("discovery.table.searchPlaceholder"),
  notSet: t("discovery.table.notSet"),
  noRiskFlags: t("discovery.table.noRiskFlags"),
  reset: t("discovery.table.reset"),
  columnsBtn: t("discovery.table.columnsButton"),
  filterStrategy: t("discovery.table.filters.strategy"),
  filterStatus: t("discovery.table.filters.status"),
  filterRiskFlag: t("discovery.table.filters.riskFlag"),
  hasRiskFlag: t("discovery.table.filters.hasRiskFlag"),
  colProfit: t("discovery.table.columns.profit"),
  colReplicability: t("discovery.table.columns.replicability"),
  colTotal: t("discovery.table.columns.total"),
  rangeProfit: t("discovery.table.range.profit"),
  rangeReplicability: t("discovery.table.range.replicability"),
  rangeTotal: t("discovery.table.range.total"),
  rangeClear: t("discovery.table.range.clear"),
  rangeSliderAria: (title: string) =>
    t("discovery.table.range.sliderAria", { title }),
  // Localised strategy_type / risk_flags DISPLAY labels — the filter/sort
  // VALUE stays the raw enum; only the rendered text is zh-TW. Mirrors
  // packages/discovery frequency.py StrategyType / riskflags.py codes.
  stratScalper: t("discovery.table.strategy.Scalper"),
  stratSwing: t("discovery.table.strategy.Swing"),
  riskInsider: t("discovery.table.riskFlag.insider_suspect"),
  colWallet: t("discovery.table.columns.wallet"),
  statusFollowing: t("discovery.table.statusOptions.following"),
  actWatch: t("discovery.table.actions.watch"),
  actFollow: t("discovery.table.actions.follow"),
  actReject: t("discovery.table.actions.reject"),
  actQuickBacktest: t("discovery.table.actions.quickBacktest"),
  actCustomizeBacktest: t("discovery.table.actions.customizeBacktest"),
  toastQueued: t("discovery.table.toast.backtestQueued"),
  toastMarkedFollow: t("discovery.table.toast.leaderMarked", {
    label: t("discovery.table.actions.follow"),
  }),
  emptyFilteredTitle: t("discovery.table.empty.filteredTitle"),
  emptyClearFilters: t("discovery.table.empty.clearFilters"),
  emptyNoDataTitle: t("discovery.table.empty.noDataTitle"),
  emptyNoDataHint: t("discovery.table.empty.noDataHint"),
  pageOf: (page: number, total: number) =>
    t("discovery.table.pagination.pageOf", { page, total }),
  range: (first: number, last: number, total: number) =>
    t("discovery.table.pagination.range", { first, last, total }),
  rowsPerPageAria: t("discovery.table.pagination.rowsPerPageAria"),
  nextPage: t("discovery.table.pagination.nextPage"),
  rowActionsAria: (wallet: string) =>
    t("discovery.table.rowActionsAria", { wallet }),
} as const;

// Regex-escape an i18n string so it is safe inside an accessible-name RegExp
// (toast/menu copy may contain (), ·, …). Used where substring match is wanted.
function rx(s: string): RegExp {
  return new RegExp(s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
}

function makeRow(p: Partial<CandidateLeaderRow>): CandidateLeaderRow {
  return {
    wallet_address: "0x0000000000000000000000000000000000000000",
    display_name: null,
    sources: null,
    first_discovered_at: null,
    last_active_at: null,
    stats_7d: null,
    stats_30d: null,
    stats_90d: null,
    frequency_stats: null,
    strategy_type: null,
    backtest_default: null,
    profit_score: null,
    replicability_score: null,
    total_score: null,
    risk_flags: null,
    specialization: null,
    user_status: "new",
    user_notes: null,
    updated_at: new Date(),
    ...p,
  } as CandidateLeaderRow;
}

const rows: CandidateLeaderRow[] = [
  makeRow({
    wallet_address: "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    display_name: "Alpha",
    strategy_type: "Scalper",
    total_score: "80",
    profit_score: "75",
    user_status: "following",
    risk_flags: ["insider_suspect"],
  }),
  makeRow({
    wallet_address: "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    display_name: "Beta",
    strategy_type: "Swing",
    total_score: "40",
    profit_score: "30",
    user_status: "new",
    risk_flags: [],
  }),
  makeRow({
    wallet_address: "0xcccccccccccccccccccccccccccccccccccccccc",
    display_name: "Gamma",
    strategy_type: "Scalper",
    total_score: "60",
    profit_score: "55",
    user_status: "new",
    risk_flags: null,
  }),
];

function bodyRows() {
  // Data rows carry an aria-label ("View leader 0x…"); the header row and the
  // empty-state row do not, so this counts real leader rows only.
  return screen
    .queryAllByRole("row")
    .filter((r) => r.getAttribute("aria-label")?.startsWith("View leader"));
}

function names() {
  return bodyRows().map(
    (r) => within(r).getAllByRole("cell")[1].textContent
  );
}

beforeEach(() => {
  push.mockClear();
  refresh.mockClear();
  quickBacktest.mockClear();
  updateLeaderStatus.mockClear();
  toastSuccess.mockClear();
  toastError.mockClear();
});

describe("DiscoveryTable (shadcn data-table)", () => {
  it("wraps the table in a Card titled with the i18n card title + a total-count badge", () => {
    render(<DiscoveryTable data={rows} />);
    expect(screen.getByText(TX.cardTitle)).toBeInTheDocument();
    // count badge shows total dataset size (3)
    const title = screen.getByText(TX.cardTitle);
    expect(within(title).getByText("3")).toBeInTheDocument();
  });

  it("renders one row per leader with shortened monospace wallet", () => {
    render(<DiscoveryTable data={rows} />);
    expect(bodyRows()).toHaveLength(3);
    expect(screen.getByText("0xaaaa…aaaa")).toBeInTheDocument();
  });

  it("defaults to sorting by Total score descending", () => {
    render(<DiscoveryTable data={rows} />);
    expect(names()).toEqual(["Alpha", "Gamma", "Beta"]);
  });

  it("toggles aria-sort on the Total header th when sorting", async () => {
    render(<DiscoveryTable data={rows} />);
    // EXACT name (not rx/substring): the column-header sort button's
    // accessible name is precisely the column title ("總分"). A substring
    // match would also catch the score range-filter trigger ("總分區間"),
    // a *different* control — assert only the header here.
    const totalBtn = screen.getByRole("button", { name: TX.colTotal });
    const th = totalBtn.closest("th")!;

    // default state = descending
    expect(th).toHaveAttribute("aria-sort", "descending");

    await userEvent.click(totalBtn); // -> ascending
    expect(th).toHaveAttribute("aria-sort", "ascending");
    expect(names()).toEqual(["Beta", "Gamma", "Alpha"]);

    await userEvent.click(totalBtn); // -> descending
    expect(th).toHaveAttribute("aria-sort", "descending");
    expect(names()).toEqual(["Alpha", "Gamma", "Beta"]);
  });

  it("marks non-sorted sortable headers aria-sort='none' and unsortable headers without it", () => {
    render(<DiscoveryTable data={rows} />);
    // EXACT name: target only the "獲利" header sort button, never the
    // "獲利區間" range-filter trigger (a substring match would hit both).
    const profitTh = screen
      .getByRole("button", { name: TX.colProfit })
      .closest("th")!;
    expect(profitTh).toHaveAttribute("aria-sort", "none");

    // Wallet is not sortable -> no aria-sort
    const walletTh = screen.getByText(TX.colWallet).closest("th")!;
    expect(walletTh).not.toHaveAttribute("aria-sort");
  });

  it("global search narrows by wallet or display name", async () => {
    render(<DiscoveryTable data={rows} />);
    const search = screen.getByPlaceholderText(TX.search);

    await userEvent.type(search, "beta");
    expect(bodyRows()).toHaveLength(1);
    expect(screen.getByText("Beta")).toBeInTheDocument();

    await userEvent.clear(search);
    await userEvent.type(search, "0xcccc");
    expect(bodyRows()).toHaveLength(1);
    expect(screen.getByText("Gamma")).toBeInTheDocument();
  });

  it("faceted Strategy filter narrows rows (option label localised, value = raw enum)", async () => {
    render(<DiscoveryTable data={rows} />);
    await userEvent.click(
      screen.getByRole("button", { name: rx(TX.filterStrategy) })
    );
    // The faceted option shows the localised zh-TW label, but still
    // filters on the underlying raw "Scalper" enum value.
    await userEvent.click(
      await screen.findByRole("menuitemcheckbox", { name: TX.stratScalper })
    );
    // Alpha + Gamma are Scalpers
    expect(bodyRows()).toHaveLength(2);
  });

  it("faceted Status filter narrows to a single status", async () => {
    render(<DiscoveryTable data={rows} />);
    await userEvent.click(
      screen.getByRole("button", { name: rx(TX.filterStatus) })
    );
    await userEvent.click(
      await screen.findByRole("menuitemcheckbox", {
        name: TX.statusFollowing,
      })
    );
    expect(bodyRows()).toHaveLength(1);
    expect(screen.getByText("Alpha")).toBeInTheDocument();
  });

  it("Risk Flag faceted filter splits has-flags vs no-flags", async () => {
    render(<DiscoveryTable data={rows} />);
    await userEvent.click(
      screen.getByRole("button", { name: rx(TX.filterRiskFlag) })
    );
    await userEvent.click(
      await screen.findByRole("menuitemcheckbox", { name: TX.hasRiskFlag })
    );
    expect(bodyRows()).toHaveLength(1); // only Alpha has flags
    expect(screen.getByText("Alpha")).toBeInTheDocument();
  });

  it("shows a Reset button only when filtered and clears all filters", async () => {
    render(<DiscoveryTable data={rows} />);
    expect(
      screen.queryByRole("button", { name: rx(TX.reset) })
    ).not.toBeInTheDocument();

    await userEvent.type(
      screen.getByPlaceholderText(TX.search),
      "beta"
    );
    expect(bodyRows()).toHaveLength(1);

    const reset = screen.getByRole("button", { name: rx(TX.reset) });
    await userEvent.click(reset);
    expect(bodyRows()).toHaveLength(3);
    expect(
      screen.queryByRole("button", { name: rx(TX.reset) })
    ).not.toBeInTheDocument();
  });

  it("column-visibility menu hides a column", async () => {
    render(<DiscoveryTable data={rows} />);
    // EXACT name: the "可複製性" header sort button only. The "可複製性區間"
    // range trigger stays mounted regardless of column visibility, so a
    // substring query would still match it and the post-hide
    // not-in-document assertion would wrongly fail.
    const replicabilityHeader = () =>
      screen.queryByRole("button", { name: TX.colReplicability });
    expect(replicabilityHeader()).toBeInTheDocument();

    await userEvent.click(
      screen.getByRole("button", { name: rx(TX.columnsBtn) })
    );
    await userEvent.click(
      await screen.findByRole("menuitemcheckbox", {
        name: TX.colReplicability,
      })
    );
    // sortable header button for Replicability is gone
    expect(replicabilityHeader()).not.toBeInTheDocument();
  });

  it("renders strategy + risk + status badges with LOCALISED labels (insider_suspect = destructive)", () => {
    render(<DiscoveryTable data={rows} />);
    // strategy_type + risk_flags + user_status DISPLAY are all the zh-TW
    // label; the underlying filter/sort VALUE stays the raw enum. user_status
    // was previously rendering the raw English enum verbatim — i18n-sweep
    // routes it through the same `statusOptions` dictionary the facet uses.
    expect(
      screen.getAllByText(TX.stratScalper).length
    ).toBeGreaterThanOrEqual(2);
    expect(screen.getByText(TX.stratSwing)).toBeInTheDocument();
    expect(screen.getByText(TX.statusFollowing)).toBeInTheDocument();
    const riskBadge = screen.getByText(TX.riskInsider);
    expect(riskBadge.className).toMatch(/destructive/);
    // the raw enum is preserved as the badge title (support / filter parity)
    expect(riskBadge).toHaveAttribute("title", "insider_suspect");
  });

  it("falls back to the raw value for an UNMAPPED strategy/risk code (never blank)", () => {
    render(
      <DiscoveryTable
        data={[
          makeRow({
            wallet_address: "0x9",
            display_name: "Edge",
            strategy_type: "BrandNewStrat",
            risk_flags: ["totally_new_flag"],
          }),
        ]}
      />
    );
    // Unmapped enum/code degrade to the raw string verbatim.
    expect(screen.getByText("BrandNewStrat")).toBeInTheDocument();
    expect(screen.getByText("totally_new_flag")).toBeInTheDocument();
  });

  it("shows a calm i18n 'no risk flags' label for rows with no risk flags", () => {
    render(<DiscoveryTable data={rows} />);
    // Beta + Gamma have no flags -> at least two such cells. (The only
    // em dash left on the page comes from the frozen shared ScoreBadge for
    // null scores — not from this owned table's own copy.)
    expect(
      screen.getAllByText(TX.noRiskFlags).length
    ).toBeGreaterThanOrEqual(2);
  });

  it("uses the i18n 'not set' label (not an em dash) for missing name/strategy", () => {
    render(<DiscoveryTable data={[makeRow({ wallet_address: "0x1" })]} />);
    // display_name + strategy_type are null on this row.
    expect(screen.getAllByText(TX.notSet).length).toBeGreaterThanOrEqual(2);
  });

  it("paginates: page size Select changes page and Next advances", async () => {
    const many: CandidateLeaderRow[] = Array.from({ length: 14 }, (_, i) =>
      makeRow({
        wallet_address: `0x${String(i).padStart(40, "0")}`,
        display_name: `User${i}`,
        total_score: String(i),
      })
    );
    render(<DiscoveryTable data={many} />);

    // open the rows-per-page Select and pick 10
    await userEvent.click(
      screen.getByRole("combobox", { name: rx(TX.rowsPerPageAria) })
    );
    await userEvent.click(await screen.findByRole("option", { name: "10" }));

    expect(bodyRows()).toHaveLength(10);
    expect(screen.getByText(TX.pageOf(1, 2))).toBeInTheDocument();
    expect(screen.getByText(TX.range(1, 10, 14))).toBeInTheDocument();

    await userEvent.click(
      screen.getByRole("button", { name: TX.nextPage })
    );
    expect(screen.getByText(TX.pageOf(2, 2))).toBeInTheDocument();
    expect(bodyRows()).toHaveLength(4);
    expect(screen.getByText(TX.range(11, 14, 14))).toBeInTheDocument();
  });

  it("navigates to the leader detail route on row click", async () => {
    render(<DiscoveryTable data={rows} />);
    // first body row (default sort desc -> Alpha)
    await userEvent.click(bodyRows()[0]);
    expect(push).toHaveBeenCalledWith(
      "/discovery/0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    );
  });

  it("navigates on keyboard Enter for a focused row", async () => {
    render(<DiscoveryTable data={rows} />);
    const row = bodyRows()[0];
    row.focus();
    await userEvent.keyboard("{Enter}");
    expect(push).toHaveBeenCalledWith(
      "/discovery/0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    );
  });

  it("shows a calm filtered empty state with a real Clear-filters action", async () => {
    render(<DiscoveryTable data={rows} />);
    await userEvent.type(
      screen.getByPlaceholderText(TX.search),
      "zzzznomatch"
    );
    expect(bodyRows()).toHaveLength(0);
    expect(screen.getByText(TX.emptyFilteredTitle)).toBeInTheDocument();
    // The empty state offers the real next action, and clicking it recovers.
    const clear = screen.getByRole("button", {
      name: rx(TX.emptyClearFilters),
    });
    await userEvent.click(clear);
    expect(bodyRows()).toHaveLength(3);
  });

  it("shows the calm i18n 'run discovery' empty state with no data", () => {
    render(<DiscoveryTable data={[]} />);
    // DESIGN-mandated single calm sentence + the real next action.
    expect(screen.getByText(TX.emptyNoDataTitle)).toBeInTheDocument();
    expect(screen.getByText(TX.emptyNoDataHint)).toBeInTheDocument();
  });
});

describe("DiscoveryTable per-row quick actions", () => {
  const ALPHA = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
  const BETA = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

  function actionsTrigger(wallet: string) {
    return screen.getByRole("button", {
      name: rx(TX.rowActionsAria(wallet)),
    });
  }

  it("opens the per-row actions menu", async () => {
    render(<DiscoveryTable data={rows} />);
    await userEvent.click(actionsTrigger(ALPHA));
    expect(
      await screen.findByRole("menuitem", {
        name: rx(TX.actQuickBacktest),
      })
    ).toBeInTheDocument();
    expect(
      screen.getByRole("menuitem", { name: rx(TX.actCustomizeBacktest) })
    ).toBeInTheDocument();
  });

  it("quick backtest calls the action then toasts + navigates", async () => {
    render(<DiscoveryTable data={rows} />);
    await userEvent.click(actionsTrigger(BETA));
    await userEvent.click(
      await screen.findByRole("menuitem", {
        name: rx(TX.actQuickBacktest),
      })
    );

    expect(quickBacktest).toHaveBeenCalledTimes(1);
    expect(quickBacktest).toHaveBeenCalledWith(BETA);
    expect(toastSuccess).toHaveBeenCalledWith(TX.toastQueued);
    expect(push).toHaveBeenCalledWith("/backtest/job-123");
  });

  it("customize backtest navigates to /backtest/new with the leader query", async () => {
    render(<DiscoveryTable data={rows} />);
    await userEvent.click(actionsTrigger(BETA));
    await userEvent.click(
      await screen.findByRole("menuitem", { name: rx(TX.actCustomizeBacktest) })
    );
    expect(push).toHaveBeenCalledWith(`/backtest/new?leader=${BETA}`);
    expect(quickBacktest).not.toHaveBeenCalled();
  });

  it("watch / reject menu items call updateLeaderStatus directly (safe-direction)", async () => {
    render(<DiscoveryTable data={rows} />);
    await userEvent.click(actionsTrigger(BETA));
    await userEvent.click(
      await screen.findByRole("menuitem", { name: TX.actWatch })
    );
    expect(updateLeaderStatus).toHaveBeenCalledWith(BETA, "watching");
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(push).not.toHaveBeenCalled();
  });

  it("follow menu item reveals the inline-confirm primitive (P7d/R24)", async () => {
    render(<DiscoveryTable data={rows} />);
    await userEvent.click(actionsTrigger(BETA));
    await userEvent.click(
      await screen.findByRole("menuitem", { name: TX.actFollow })
    );
    // Selecting Follow does NOT directly fire the action — the inline
    // confirm primitive is now in the action cell instead.
    expect(updateLeaderStatus).not.toHaveBeenCalled();
    // The inline-confirm trigger expands inline; complete the gate.
    await userEvent.click(
      screen.getByRole("button", {
        name: new RegExp(TX.actFollow),
      })
    );
    await userEvent.type(
      screen.getByLabelText(
        t("inlineConfirm.confirmPhraseLabel", { phrase: "確認執行" })
      ),
      "確認執行"
    );
    await userEvent.click(
      screen.getByRole("button", { name: t("inlineConfirm.submitButton") })
    );
    expect(updateLeaderStatus).toHaveBeenCalledWith(BETA, "following", {
      confirmText: "確認執行",
    });
  });

  it("hides the menu item matching the leader's current status", async () => {
    render(<DiscoveryTable data={rows} />);
    // Alpha.user_status === "following" -> no Follow item, Watch/Reject present
    await userEvent.click(actionsTrigger(ALPHA));
    expect(
      await screen.findByRole("menuitem", { name: TX.actWatch })
    ).toBeInTheDocument();
    expect(
      screen.getByRole("menuitem", { name: TX.actReject })
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("menuitem", { name: TX.actFollow })
    ).not.toBeInTheDocument();
  });

  it("clicking the actions trigger does NOT navigate to the row detail", async () => {
    render(<DiscoveryTable data={rows} />);
    await userEvent.click(actionsTrigger(ALPHA));
    // menu opened, but row→detail push must not have fired
    expect(push).not.toHaveBeenCalled();
  });

  it("row click still navigates while action area swallows clicks", async () => {
    render(<DiscoveryTable data={rows} />);
    // click a non-action cell of the first row (default sort -> Alpha)
    const firstRow = bodyRows()[0];
    await userEvent.click(within(firstRow).getAllByRole("cell")[1]);
    expect(push).toHaveBeenCalledWith(`/discovery/${ALPHA}`);
    expect(quickBacktest).not.toHaveBeenCalled();
    expect(updateLeaderStatus).not.toHaveBeenCalled();
  });
});

describe("DiscoveryTable score range filter (dual-thumb 拉桿)", () => {
  // Synthetic non-zero fixture across all three 0–100 score columns. (Real
  // total_score / replicability_score are all 0 today due to a separate
  // scoring bug fixed in parallel — the filter is exercised against the
  // 0–100 schema, never the buggy data.) `Null` carries no scores so its
  // null cells must drop out the moment a real bound is set. Scores sit
  // near the domain edges so a few deterministic arrow-key thumb nudges
  // cross the relevant thresholds in jsdom.
  const scored: CandidateLeaderRow[] = [
    makeRow({
      wallet_address: "0x1111111111111111111111111111111111111111",
      display_name: "Low",
      profit_score: "1",
      replicability_score: "1",
      total_score: "1",
    }),
    makeRow({
      wallet_address: "0x2222222222222222222222222222222222222222",
      display_name: "Mid",
      profit_score: "3",
      replicability_score: "3",
      total_score: "3",
    }),
    makeRow({
      wallet_address: "0x3333333333333333333333333333333333333333",
      display_name: "High",
      profit_score: "98",
      replicability_score: "98",
      total_score: "98",
    }),
    makeRow({
      wallet_address: "0x4444444444444444444444444444444444444444",
      display_name: "Null",
      profit_score: null,
      replicability_score: null,
      total_score: null,
    }),
  ];

  async function openRange(title: string) {
    await userEvent.click(screen.getByRole("button", { name: rx(title) }));
  }

  // Radix Slider renders one role="slider" thumb per value: [minThumb,
  // maxThumb]. Each thumb is keyboard-driven; ArrowRight/ArrowLeft step
  // the FOCUSED thumb by ±step (=1) and that is what a real drag does at
  // the granular level. Driving via userEvent.keyboard wraps the change
  // in act() and the LIVE onValueChange → setFilterValue fires on every
  // keystroke, so the assertions below observe real-time filtering.
  function thumbs(title: string) {
    const [lo, hi] = screen.getAllByRole("slider", {
      name: TX.rangeSliderAria(title),
    });
    return { lo, hi };
  }

  // Step a focused thumb n times in one direction (live filter each tick).
  async function step(
    thumb: HTMLElement,
    dir: "left" | "right",
    n: number
  ) {
    thumb.focus();
    const key = dir === "right" ? "{ArrowRight}" : "{ArrowLeft}";
    await userEvent.keyboard(key.repeat(n));
  }

  it("renders a range trigger for each of the three score columns", () => {
    render(<DiscoveryTable data={scored} />);
    expect(
      screen.getByRole("button", { name: rx(TX.rangeProfit) })
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: rx(TX.rangeReplicability) })
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: rx(TX.rangeTotal) })
    ).toBeInTheDocument();
  });

  it("exposes a dual-thumb 0–100 slider with an accessible name", async () => {
    render(<DiscoveryTable data={scored} />);
    await openRange(TX.rangeProfit);
    const all = screen.getAllByRole("slider", {
      name: TX.rangeSliderAria(TX.rangeProfit),
    });
    expect(all).toHaveLength(2); // two thumbs = a range slider
    for (const thumb of all) {
      expect(thumb).toHaveAttribute("aria-valuemin", "0");
      expect(thumb).toHaveAttribute("aria-valuemax", "100");
    }
  });

  it("dragging the min thumb up filters out lower rows LIVE (null excluded)", async () => {
    render(<DiscoveryTable data={scored} />);
    await openRange(TX.rangeProfit);
    const { lo } = thumbs(TX.rangeProfit);
    // One step right: min 0 -> 1. Low(1) still ≥ 1, Mid/High pass, Null
    // (no score) drops the instant a real bound is set.
    await step(lo, "right", 1);
    expect(names().sort()).toEqual(["High", "Low", "Mid"]);
    // Two more steps: min -> 3. Low(1) now drops, Mid(3)/High(98) remain.
    await step(lo, "right", 2);
    expect(names().sort()).toEqual(["High", "Mid"]);
  });

  it("dragging the max thumb down filters out higher rows LIVE", async () => {
    render(<DiscoveryTable data={scored} />);
    await openRange(TX.rangeProfit);
    const { hi } = thumbs(TX.rangeProfit);
    // One step left: max 100 -> 99. High(98) still ≤ 99, so only the
    // null-score row drops (a bound is now set).
    await step(hi, "left", 1);
    expect(names().sort()).toEqual(["High", "Low", "Mid"]);
    // Three steps: max -> 97. High(98) now exceeds the upper bound.
    await step(hi, "left", 3);
    expect(names().sort()).toEqual(["Low", "Mid"]);
  });

  it("min+max define an inclusive window, updating on each thumb move", async () => {
    render(<DiscoveryTable data={scored} />);
    await openRange(TX.rangeTotal);
    const { lo, hi } = thumbs(TX.rangeTotal);
    // min 0 -> 1 (Low edge stays in)
    await step(lo, "right", 1);
    expect(names().sort()).toEqual(["High", "Low", "Mid"]);
    // max 100 -> 97 (High=98 now excluded), live each step.
    await step(hi, "left", 3);
    // Window [1,97]: Low=1 (incl), Mid=3 (incl), High=98 (out), Null out.
    expect(names().sort()).toEqual(["Low", "Mid"]);
  });

  it("the replicability slider filters independently of profit/total", async () => {
    render(<DiscoveryTable data={scored} />);
    await openRange(TX.rangeReplicability);
    const { hi } = thumbs(TX.rangeReplicability);
    // Lower replicability max to 97 -> High(98) drops; profit/total
    // sliders are untouched so they impose no bound.
    await step(hi, "left", 3);
    expect(names().sort()).toEqual(["Low", "Mid"]);
  });

  it("an untouched / full-range slider never filters (null rows remain)", async () => {
    render(<DiscoveryTable data={scored} />);
    await openRange(TX.rangeProfit);
    // open the popover but move nothing -> dataset untouched, no Reset shown
    expect(bodyRows()).toHaveLength(4);
    expect(
      screen.queryByRole("button", { name: rx(TX.reset) })
    ).not.toBeInTheDocument();
  });

  it("an active range can be emptied by combining both thumbs", async () => {
    render(<DiscoveryTable data={scored} />);
    await openRange(TX.rangeProfit);
    const { lo, hi } = thumbs(TX.rangeProfit);
    await step(lo, "right", 4); // min -> 4 (Low/Mid drop, High=98 remains)
    expect(names()).toEqual(["High"]);
    await step(hi, "left", 3); // max -> 97 (High=98 now excluded too)
    expect(bodyRows()).toHaveLength(0);
  });

  it("returning the thumb back to the 0–100 edge clears the filter again", async () => {
    render(<DiscoveryTable data={scored} />);
    await openRange(TX.rangeProfit);
    const { lo } = thumbs(TX.rangeProfit);
    await step(lo, "right", 3); // min -> 3, an active bound
    expect(names().sort()).toEqual(["High", "Mid"]);
    expect(
      screen.getByRole("button", { name: rx(TX.reset) })
    ).toBeInTheDocument();
    await step(lo, "left", 3); // min back to 0 == full 0–100 range
    // Full span == no filter: all rows incl. the null-score one return.
    expect(bodyRows()).toHaveLength(4);
    expect(
      screen.queryByRole("button", { name: rx(TX.reset) })
    ).not.toBeInTheDocument();
  });

  it("the per-filter Clear button resets just that range", async () => {
    render(<DiscoveryTable data={scored} />);
    await openRange(TX.rangeProfit);
    const { lo } = thumbs(TX.rangeProfit);
    await step(lo, "right", 4); // min -> 4 -> only High remains
    expect(names()).toEqual(["High"]);

    await userEvent.click(
      screen.getByRole("button", { name: rx(TX.rangeClear) })
    );
    expect(bodyRows()).toHaveLength(4);
  });

  it("a set range shows the global Reset, which clears it", async () => {
    render(<DiscoveryTable data={scored} />);
    expect(
      screen.queryByRole("button", { name: rx(TX.reset) })
    ).not.toBeInTheDocument();

    await openRange(TX.rangeTotal);
    const { lo } = thumbs(TX.rangeTotal);
    await step(lo, "right", 4); // min -> 4 -> only High remains
    expect(names()).toEqual(["High"]);

    // Reset lives in the toolbar; close the popover then click it.
    await userEvent.keyboard("{Escape}");
    await userEvent.click(screen.getByRole("button", { name: rx(TX.reset) }));
    expect(bodyRows()).toHaveLength(4);
    expect(
      screen.queryByRole("button", { name: rx(TX.reset) })
    ).not.toBeInTheDocument();
  });
});
