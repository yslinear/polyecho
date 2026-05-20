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
 * preserved, and the prominent "新增回測" CTA wired to /backtest/new.
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

describe("/backtest list page (Server Component)", () => {
  beforeEach(() => {
    listBacktestJobs.mockReset();
  });

  it("renders the calm empty state when there are no rows", async () => {
    listBacktestJobs.mockResolvedValue([]);
    render(await BacktestListPage());
    expect(screen.getByText(EMPTY_TITLE)).toBeInTheDocument();
    expect(screen.getByText(EMPTY_HINT)).toBeInTheDocument();
    // Header CTA still rendered even when the list is empty (the
    // empty-state hint points at it).
    const cta = screen.getByRole("link", { name: NEW_BUTTON });
    expect(cta).toHaveAttribute("href", "/backtest/new");
  });

  it("renders multiple rows with the correct status badge variant", async () => {
    listBacktestJobs.mockResolvedValue([
      {
        id: "job-completed-1",
        leader_addresses: ["0xaaaa1111aaaa2222aaaa3333aaaa4444aaaa5555"],
        start_date: "2025-01-01",
        end_date: "2025-02-01",
        initial_capital_usd: "1000",
        status: "completed",
        progress: "100",
        result_keys: 3,
        created_at: new Date("2026-05-20T10:00:00Z"),
      },
      {
        id: "job-running-2",
        leader_addresses: [
          "0xbbbb1111bbbb2222bbbb3333bbbb4444bbbb5555",
          "0xcccc1111cccc2222cccc3333cccc4444cccc5555",
          "0xdddd1111dddd2222dddd3333dddd4444dddd5555",
        ],
        start_date: "2025-03-01",
        end_date: "2025-04-01",
        initial_capital_usd: "500",
        status: "running",
        progress: "45.5",
        result_keys: 0,
        created_at: new Date("2026-05-20T08:00:00Z"),
      },
      {
        id: "job-pending-3",
        leader_addresses: ["0xeeee1111eeee2222eeee3333eeee4444eeee5555"],
        start_date: "2025-01-01",
        end_date: "2025-02-01",
        initial_capital_usd: "250",
        status: "pending",
        progress: "0",
        result_keys: 0,
        created_at: new Date("2026-05-20T06:00:00Z"),
      },
      {
        id: "job-failed-4",
        leader_addresses: ["0xffff1111ffff2222ffff3333ffff4444ffff5555"],
        start_date: "2025-01-01",
        end_date: "2025-02-01",
        initial_capital_usd: "100",
        status: "failed",
        progress: "12",
        result_keys: 1,
        created_at: new Date("2026-05-19T00:00:00Z"),
      },
    ]);
    render(await BacktestListPage());

    // One badge per status, each tagged with data-status so we can assert
    // the underlying enum value (not just the i18n surface).
    expect(screen.getByText(STATUS_COMPLETED).closest("[data-status]"))
      .toHaveAttribute("data-status", "completed");
    expect(screen.getByText(STATUS_RUNNING).closest("[data-status]"))
      .toHaveAttribute("data-status", "running");
    expect(screen.getByText(STATUS_PENDING).closest("[data-status]"))
      .toHaveAttribute("data-status", "pending");
    expect(screen.getByText(STATUS_FAILED).closest("[data-status]"))
      .toHaveAttribute("data-status", "failed");

    // "+N more" leader summary suffix appears when >2 leaders.
    expect(
      screen.getByText(t("backtest.list.leadersMore", { count: 1 }))
    ).toBeInTheDocument();

    // Each row has a 「檢視」 deep-link to its own detail page.
    const viewLinks = screen.getAllByRole("link", { name: VIEW_LABEL });
    expect(viewLinks).toHaveLength(4);
    expect(viewLinks[0]).toHaveAttribute("href", "/backtest/job-completed-1");

    // And a 「重新執行」 link that deep-links into /backtest/new with the
    // source job id (the form pre-fill is a future enhancement, NOT a
    // write at the list level).
    const rerunLinks = screen.getAllByRole("link", { name: RERUN_LABEL });
    expect(rerunLinks).toHaveLength(4);
    expect(rerunLinks[0]).toHaveAttribute(
      "href",
      "/backtest/new?from=job-completed-1"
    );
  });

  it("renders rows in the order returned by listBacktestJobs (created_at desc upstream)", async () => {
    listBacktestJobs.mockResolvedValue([
      {
        id: "newest",
        leader_addresses: ["0x11"],
        start_date: "2025-01-01",
        end_date: "2025-02-01",
        initial_capital_usd: "111",
        status: "completed",
        progress: "100",
        result_keys: 1,
        created_at: new Date("2026-05-20T10:00:00Z"),
      },
      {
        id: "middle",
        leader_addresses: ["0x22"],
        start_date: "2025-01-01",
        end_date: "2025-02-01",
        initial_capital_usd: "222",
        status: "completed",
        progress: "100",
        result_keys: 1,
        created_at: new Date("2026-05-19T10:00:00Z"),
      },
      {
        id: "oldest",
        leader_addresses: ["0x33"],
        start_date: "2025-01-01",
        end_date: "2025-02-01",
        initial_capital_usd: "333",
        status: "completed",
        progress: "100",
        result_keys: 1,
        created_at: new Date("2026-05-18T10:00:00Z"),
      },
    ]);
    render(await BacktestListPage());

    const rows = screen.getAllByTestId("backtest-row");
    expect(rows).toHaveLength(3);
    // The detail-link href reveals the rendered order; the SSR mapper
    // walks `rows` in order, so the upstream `created_at desc` ordering
    // is preserved here verbatim.
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

  it("requests the default 50-row page from listBacktestJobs", async () => {
    listBacktestJobs.mockResolvedValue([]);
    render(await BacktestListPage());
    expect(listBacktestJobs).toHaveBeenCalledWith({ limit: 50 });
  });
});
