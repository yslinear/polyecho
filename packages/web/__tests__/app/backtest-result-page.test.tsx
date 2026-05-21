import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { t } from "@/lib/i18n";

/**
 * State coverage for /backtest/[id] (async Server Component).
 *
 * The page ends in an awaited Drizzle `.limit(1)`; we mock `db` with a
 * chainable builder that resolves to a single row we control. `notFound`
 * is mocked to throw so the not-found branch is observable. Recharts'
 * ResponsiveContainer is stubbed (0x0 in jsdom) so the completed-state
 * chart island mounts. No DB, no network.
 *
 * These assert the DESIGN.md honesty contract via the i18n dictionary
 * (not hardcoded prose, so they survive translation edits): a queued job
 * says so plainly, failed jobs give the real next action, and completed
 * jobs render PnL with sign+arrow+tabular semantics (color is
 * reinforcement only — language-neutral, asserted literally).
 */

let row: Record<string, unknown> | undefined;

function makeBuilder() {
  const builder: Record<string, unknown> = {};
  const chain = () => builder;
  for (const m of ["select", "from", "where", "limit"]) {
    builder[m] = vi.fn(chain);
  }
  builder.then = (resolve: (v: unknown[]) => unknown) =>
    resolve(row ? [row] : []);
  return builder;
}

vi.mock("@/lib/db", () => ({ db: makeBuilder() }));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));

const notFound = vi.fn(() => {
  throw new Error("NEXT_NOT_FOUND");
});
vi.mock("next/navigation", () => ({ notFound: () => notFound() }));

// ResponsiveContainer measures its parent (0x0 under jsdom). Replace with
// a fixed-size passthrough so the completed-state chart mounts.
vi.mock("recharts", async () => {
  const actual = await vi.importActual<typeof import("recharts")>("recharts");
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
      <div style={{ width: 600, height: 300 }}>{children}</div>
    ),
  };
});

import BacktestResultPage from "@/app/backtest/[id]/page";

const baseJob = {
  id: "job-1",
  leader_addresses: ["0xabcdef0123456789"],
  start_date: "2025-01-01",
  end_date: "2025-04-01",
  initial_capital_usd: "1000",
  slippage_bps: 150,
  execution_delay_seconds: 30,
  progress: "0",
  status: "pending",
  result: null as unknown,
};

function renderPage(id = "job-1") {
  return BacktestResultPage({ params: Promise.resolve({ id }) });
}

describe("/backtest/[id] result page", () => {
  beforeEach(() => {
    row = { ...baseJob };
    notFound.mockClear();
  });

  it("calls notFound() when the job row is missing", async () => {
    row = undefined;
    await expect(renderPage("nope")).rejects.toThrow("NEXT_NOT_FOUND");
    expect(notFound).toHaveBeenCalledTimes(1);
  });

  it("pending: shows the plain queued sentence + humanised badge", async () => {
    row = { ...baseJob, status: "pending" };
    render(await renderPage());
    // The honest "worker will process this" sentence (i18n body copy).
    expect(
      screen.getByText(t("backtest.result.pending.body"))
    ).toBeInTheDocument();
    // Status label is humanised, never the raw enum.
    expect(
      screen.getAllByText(t("backtest.result.status.pending")).length
    ).toBeGreaterThan(0);
    expect(screen.queryByText("pending")).not.toBeInTheDocument();
  });

  it("running: shows a labelled progressbar with the percentage", async () => {
    row = { ...baseJob, status: "running", progress: "42" };
    render(await renderPage());
    const bar = screen.getByRole("progressbar");
    expect(bar).toHaveAttribute("aria-valuenow", "42");
    expect(screen.getByText("42.0%")).toBeInTheDocument();
  });

  it("failed: states the error and the real next action", async () => {
    row = {
      ...baseJob,
      status: "failed",
      result: { error: "engine boom" },
    };
    render(await renderPage());
    expect(
      screen.getByText(t("backtest.result.failed.title"))
    ).toBeInTheDocument();
    // The CLI command stays English by proper-noun policy (rendered in a
    // <code> block) — the real next action a failed run must surface.
    expect(
      screen.getByText("polyecho logs backtest")
    ).toBeInTheDocument();
    // The actionable suffix prose ("…then queue the job again") is i18n.
    // It sits in the same <p> as an inline <code>, so match the INNERMOST
    // element containing it (ancestors are excluded because a child also
    // matches) — otherwise getByText sees the <p>, CardContent, Card, … and
    // throws "multiple elements".
    const suffix = t("backtest.result.failed.bodySuffix")
      .replace(/\s+/g, " ")
      .trim();
    expect(
      screen.getByText((_content, el) => {
        const norm = (n: Element | null | undefined) =>
          (n?.textContent ?? "").replace(/\s+/g, " ");
        return (
          norm(el).includes(suffix) &&
          Array.from(el?.children ?? []).every(
            (c) => !norm(c).includes(suffix)
          )
        );
      })
    ).toBeInTheDocument();
  });

  it("renders per-leader error envelopes in a destructive Alert (P11 Bug 3)", async () => {
    // P11 Bug 3 regression-lock: result.leaders[wallet].error MUST be
    // surfaced, never silently skipped.  Job 557eef18 was 'completed' but
    // every leader carried the
    //   RuntimeError: LiveDataSource must be used as an async context manager
    // envelope — pre-P11 the detail page rendered the "completed" badge with
    // no error block.  We render here a job that mirrors that wire shape
    // (status='failed' now per Bug 2; we still assert the Alert renders).
    const leaderError =
      "RuntimeError: LiveDataSource must be used as an async context manager " +
      "(`async with LiveDataSource() as ds: ...`)";
    row = {
      ...baseJob,
      status: "failed",
      result: {
        leaders: {
          "0x1f1d29e8aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": {
            error: leaderError,
          },
        },
      },
    };
    const { container } = render(await renderPage());
    // The destructive Alert region must be present (data-testid lets the
    // test bind to the surface independently of i18n copy).
    const alert = container.querySelector(
      '[data-testid="backtest-leader-errors"]',
    );
    expect(alert).not.toBeNull();
    // The localised title is rendered.
    expect(
      screen.getByText(t("backtest.result.leaderError.title")),
    ).toBeInTheDocument();
    // The raw worker error string is rendered verbatim (operator-actionable;
    // truncating would defeat the entire point of showing it).
    expect(screen.getByText(leaderError)).toBeInTheDocument();
    // The wallet is tagged so an operator can copy the full address from the
    // DOM even though the visible label is truncated.
    const item = container.querySelector(
      '[data-leader="0x1f1d29e8aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]',
    );
    expect(item).not.toBeNull();
  });

  it("does NOT render the leader-error Alert when every leader is clean", async () => {
    // Regression-lock for Bug 3: the Alert must appear ONLY when at least
    // one leader carries an `error` envelope.  A clean completed run must
    // not display an empty/spurious error surface.
    row = {
      ...baseJob,
      status: "completed",
      result: {
        leaders: {
          "0xabc": { scenarios: [] },
        },
        scenarios: [],
      },
    };
    const { container } = render(await renderPage());
    expect(
      container.querySelector('[data-testid="backtest-leader-errors"]'),
    ).toBeNull();
  });

  it("completed (triple): renders signed returns with arrows + the chart", async () => {
    row = {
      ...baseJob,
      status: "completed",
      result: {
        scenarios: [
          {
            label: "optimistic",
            slippage_bps: 50,
            delay_seconds: 10,
            stats: {
              total_return_pct: 12.5,
              annualized_return_pct: 40,
              sharpe: 1.2,
              max_drawdown_pct: -8,
              win_rate_pct: 55,
              avg_hold_hours: 6,
              trades_total: 30,
              trades_skipped: 2,
            },
            pnl_curve: [
              { date: "2025-01-01", pnl_usd: 10 },
              { date: "2025-01-02", pnl_usd: 25 },
            ],
          },
          {
            label: "pessimistic",
            slippage_bps: 300,
            delay_seconds: 60,
            stats: {
              total_return_pct: -4.25,
              annualized_return_pct: -12,
              sharpe: -0.3,
              max_drawdown_pct: -22,
              win_rate_pct: 41,
              avg_hold_hours: 5,
              trades_total: 28,
              trades_skipped: 6,
            },
            pnl_curve: [
              { date: "2025-01-01", pnl_usd: -3 },
              { date: "2025-01-02", pnl_usd: -9 },
            ],
          },
        ],
      },
    };
    const { container } = render(await renderPage());

    // Gain return: arrow + signed % (sign carries meaning, not colour).
    // The figure appears twice (big ScenarioCard + StatsTable row).
    expect(screen.getAllByText("↑").length).toBeGreaterThan(0);
    expect(screen.getAllByText("+12.50%").length).toBeGreaterThan(0);
    // Loss return: down arrow + negative %.
    expect(screen.getAllByText("-4.25%").length).toBeGreaterThan(0);
    // tone class is reinforcement on the data-pnl wrapper span.
    const gainSpans = container.querySelectorAll('[data-pnl="up"]');
    expect(gainSpans.length).toBeGreaterThan(0);
    expect(gainSpans[0].className).toContain("text-gain");
    const lossSpans = container.querySelectorAll('[data-pnl="down"]');
    expect(lossSpans.length).toBeGreaterThan(0);
    expect(lossSpans[0].className).toContain("text-loss");
    // Chart island mounted (shadcn ChartContainer div).
    expect(container.querySelector("[data-chart]")).toBeInTheDocument();
    // No raw Tailwind palette colours leaked into PnL rendering.
    expect(container.innerHTML).not.toContain("text-green-600");
    expect(container.innerHTML).not.toContain("text-red-600");
  });
});
