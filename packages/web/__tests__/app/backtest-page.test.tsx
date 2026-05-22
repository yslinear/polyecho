import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

import { t } from "@/lib/i18n";

/**
 * /backtest (list page) — Server Component render coverage.
 *
 * The page is async and ends in `await listBacktestJobs(...)`. We mock
 * `listBacktestJobs` directly (the Drizzle chain is exercised by
 * backtest-actions.test.ts) so this test focuses on what the LIST page
 * itself renders: empty state, populated rows, status badges, ordering
 * preserved, the prominent "新增回測" CTA, and the B1 denormalised summary
 * columns + sort-header deep links.
 */

const listBacktestJobs = vi.fn<() => Promise<unknown[]>>(async () => []);

vi.mock("@/app/backtest/actions", async () => {
  return {
    listBacktestJobs: (...a: unknown[]) => listBacktestJobs(...(a as [])),
  };
});

import BacktestListPage from "@/app/backtest/page";

const EMPTY_TITLE = t("backtest.list.empty.title");
const EMPTY_HINT = t("backtest.list.empty.hint");
const NEW_BUTTON = t("backtest.list.newButton");
const STATUS_COMPLETED = t("backtest.list.status.completed");
const STATUS_RUNNING = t("backtest.list.status.running");
const STATUS_PENDING = t("backtest.list.status.pending");
const STATUS_FAILED = t("backtest.list.status.failed");
const VIEW_LABEL = t("backtest.list.actions.view");
const RERUN_LABEL = t("backtest.list.actions.rerun");

// B1: a job row with the denormalised neutral-scenario summary fields. The
// `over` arg lets each test tweak just what it cares about.
function row(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: "job-x",
    leader_addresses: ["0xaaaa1111aaaa2222aaaa3333aaaa4444aaaa5555"],
    start_date: "2025-01-01",
    end_date: "2025-02-01",
    initial_capital_usd: "1000",
    status: "completed",
    progress: "100",
    result_keys: 3,
    total_return_pct: "15.0000",
    win_rate: "0.6000",
    max_drawdown_pct: "12.5000",
    total_trades: 8,
    sharpe_ratio: "1.8000",
    total_fees_usd: "4.0000",
    created_at: new Date("2026-05-20T10:00:00Z"),
    ...over,
  };
}

// The page now reads `searchParams` (Next 15: a Promise). A bare empty
// object exercises the default created_at-desc sort.
const noParams = Promise.resolve({});

describe("/backtest list page (Server Component)", () => {
  beforeEach(() => {
    listBacktestJobs.mockReset();
  });

  it("renders the calm empty state when there are no rows", async () => {
    listBacktestJobs.mockResolvedValue([]);
    render(await BacktestListPage({ searchParams: noParams }));
    expect(screen.getByText(EMPTY_TITLE)).toBeInTheDocument();
    expect(screen.getByText(EMPTY_HINT)).toBeInTheDocument();
    const cta = screen.getByRole("link", { name: NEW_BUTTON });
    expect(cta).toHaveAttribute("href", "/backtest/new");
  });

  it("renders multiple rows with the correct status badge variant", async () => {
    listBacktestJobs.mockResolvedValue([
      row({ id: "job-completed-1", status: "completed", progress: "100" }),
      row({
        id: "job-running-2",
        leader_addresses: [
          "0xbbbb1111bbbb2222bbbb3333bbbb4444bbbb5555",
          "0xcccc1111cccc2222cccc3333cccc4444cccc5555",
          "0xdddd1111dddd2222dddd3333dddd4444dddd5555",
        ],
        status: "running",
        progress: "45.5",
        total_return_pct: null,
        win_rate: null,
        max_drawdown_pct: null,
        total_trades: null,
        sharpe_ratio: null,
        total_fees_usd: null,
      }),
      row({ id: "job-pending-3", status: "pending", progress: "0" }),
      row({ id: "job-failed-4", status: "failed", progress: "12" }),
    ]);
    render(await BacktestListPage({ searchParams: noParams }));

    expect(
      screen.getByText(STATUS_COMPLETED).closest("[data-status]"),
    ).toHaveAttribute("data-status", "completed");
    expect(
      screen.getByText(STATUS_RUNNING).closest("[data-status]"),
    ).toHaveAttribute("data-status", "running");
    expect(
      screen.getByText(STATUS_PENDING).closest("[data-status]"),
    ).toHaveAttribute("data-status", "pending");
    expect(
      screen.getByText(STATUS_FAILED).closest("[data-status]"),
    ).toHaveAttribute("data-status", "failed");

    // "+N more" leader summary suffix appears when >2 leaders.
    expect(
      screen.getByText(t("backtest.list.leadersMore", { count: 1 })),
    ).toBeInTheDocument();

    const viewLinks = screen.getAllByRole("link", { name: VIEW_LABEL });
    expect(viewLinks).toHaveLength(4);
    expect(viewLinks[0]).toHaveAttribute("href", "/backtest/job-completed-1");

    const rerunLinks = screen.getAllByRole("link", { name: RERUN_LABEL });
    expect(rerunLinks).toHaveLength(4);
    expect(rerunLinks[0]).toHaveAttribute(
      "href",
      "/backtest/new?from=job-completed-1",
    );
  });

  it("renders rows in the order returned by listBacktestJobs", async () => {
    listBacktestJobs.mockResolvedValue([
      row({ id: "newest", created_at: new Date("2026-05-20T10:00:00Z") }),
      row({ id: "middle", created_at: new Date("2026-05-19T10:00:00Z") }),
      row({ id: "oldest", created_at: new Date("2026-05-18T10:00:00Z") }),
    ]);
    render(await BacktestListPage({ searchParams: noParams }));

    const rows = screen.getAllByTestId("backtest-row");
    expect(rows).toHaveLength(3);
    const hrefs = rows.map((tr) => {
      const a = tr.querySelector("a[href^='/backtest/']");
      return a?.getAttribute("href");
    });
    expect(hrefs).toEqual([
      "/backtest/newest",
      "/backtest/middle",
      "/backtest/oldest",
    ]);
  });

  // -------------------------------------------------------------------------
  // B1: denormalised summary columns + sortable headers
  // -------------------------------------------------------------------------
  it("renders the B1 denormalised summary cells for a completed job", async () => {
    listBacktestJobs.mockResolvedValue([
      row({
        id: "job-summary",
        total_return_pct: "15.0000",
        win_rate: "0.6000",
        max_drawdown_pct: "12.5000",
        total_trades: 8,
        sharpe_ratio: "1.8000",
      }),
    ]);
    render(await BacktestListPage({ searchParams: noParams }));

    // total_return_pct rendered with a "%" suffix at 2 decimals.
    expect(screen.getByText("15.00%")).toBeInTheDocument();
    // win_rate at 3 decimals; max_drawdown with "%"; sharpe at 2 decimals;
    // total_trades at 0 decimals.
    expect(screen.getByText("0.600")).toBeInTheDocument();
    expect(screen.getByText("12.50%")).toBeInTheDocument();
    expect(screen.getByText("1.80")).toBeInTheDocument();
    expect(screen.getByText("8")).toBeInTheDocument();
  });

  it("shows a muted placeholder for summary cells of a not-yet-completed job", async () => {
    listBacktestJobs.mockResolvedValue([
      row({
        id: "job-pending",
        status: "pending",
        total_return_pct: null,
        win_rate: null,
        max_drawdown_pct: null,
        total_trades: null,
        sharpe_ratio: null,
        total_fees_usd: null,
      }),
    ]);
    render(await BacktestListPage({ searchParams: noParams }));
    // The summary placeholder reuses the i18n summaryEmpty string.
    expect(
      screen.getAllByText(t("backtest.list.summaryEmpty")).length,
    ).toBeGreaterThan(0);
  });

  it("renders sortable column headers as deep links to ?sort=&dir=", async () => {
    listBacktestJobs.mockResolvedValue([row()]);
    render(await BacktestListPage({ searchParams: noParams }));

    // The total-return header is a Link; with no active sort it defaults to
    // a descending deep link for that column.
    const returnHead = screen.getByText(
      t("backtest.list.columns.totalReturn"),
    );
    expect(returnHead.closest("a")).toHaveAttribute(
      "href",
      "/backtest?sort=total_return_pct&dir=desc",
    );
  });

  it("flips the active sort column's direction to asc on the next click", async () => {
    listBacktestJobs.mockResolvedValue([row()]);
    // Already sorting by total_return_pct desc → the header now offers asc.
    render(
      await BacktestListPage({
        searchParams: Promise.resolve({
          sort: "total_return_pct",
          dir: "desc",
        }),
      }),
    );
    const returnHead = screen.getByText(/總報酬率/);
    expect(returnHead.closest("a")).toHaveAttribute(
      "href",
      "/backtest?sort=total_return_pct&dir=asc",
    );
  });

  it("passes the resolved sort to listBacktestJobs (allowlisted)", async () => {
    listBacktestJobs.mockResolvedValue([]);
    await BacktestListPage({
      searchParams: Promise.resolve({ sort: "win_rate", dir: "asc" }),
    });
    expect(listBacktestJobs).toHaveBeenCalledWith({
      limit: 50,
      sortBy: "win_rate",
      sortDir: "asc",
    });
  });

  it("falls back to created_at desc for an unknown ?sort= value", async () => {
    listBacktestJobs.mockResolvedValue([]);
    await BacktestListPage({
      searchParams: Promise.resolve({ sort: "DROP TABLE", dir: "weird" }),
    });
    expect(listBacktestJobs).toHaveBeenCalledWith({
      limit: 50,
      sortBy: "created_at",
      sortDir: "desc",
    });
  });
});
