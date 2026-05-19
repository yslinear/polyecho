import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { t } from "@/lib/i18n";

// Recharts' ResponsiveContainer measures its parent (0x0 in jsdom) and would
// render nothing. Replace it with a fixed-size passthrough so the SVG mounts.
vi.mock("recharts", async () => {
  const actual = await vi.importActual<typeof import("recharts")>("recharts");
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
      <div style={{ width: 600, height: 300 }}>{children}</div>
    ),
  };
});

// Both chart islands import a *type* from their sibling page.tsx which pulls
// in @/lib/db. Stub db + env so the import graph is DB-free.
vi.mock("@/lib/db", () => ({ db: {} }));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));

import { LeaderScoreChart } from "@/app/discovery/[wallet]/leader-score-chart";
import { BacktestPnlChart } from "@/app/backtest/[id]/backtest-pnl-chart";
import type { ScorePoint } from "@/app/discovery/[wallet]/page";

describe("LeaderScoreChart", () => {
  it("renders an empty-state message when there is no history", () => {
    render(<LeaderScoreChart data={[]} />);
    expect(
      screen.getByText(t("discovery.chart.noHistory"))
    ).toBeInTheDocument();
  });

  it("renders the chart container when given score points", () => {
    // Recharts emits no <svg> under jsdom (no layout). Assert the shadcn
    // ChartContainer (<div data-chart>) mounted — proving the non-empty
    // branch + config build ran without throwing.
    const data: ScorePoint[] = [
      { date: "2025-01-01", total_score: 40 },
      { date: "2025-01-02", total_score: 55 },
      { date: "2025-01-03", total_score: 72 },
    ] as ScorePoint[];
    const { container } = render(<LeaderScoreChart data={data} />);
    expect(container.querySelector("[data-chart]")).toBeInTheDocument();
    expect(
      screen.queryByText(t("discovery.chart.noHistory"))
    ).not.toBeInTheDocument();
  });

  it("renders a reserved-space skeleton while loading (no layout shift)", () => {
    const { container } = render(<LeaderScoreChart data={[]} loading />);
    const sk = container.querySelector(".h-\\[260px\\]");
    expect(sk).toBeInTheDocument();
    // Loading must not fall through to the empty-state copy.
    expect(
      screen.queryByText(t("discovery.chart.noHistory"))
    ).not.toBeInTheDocument();
  });

  it("tints the line via --gain for a rising score history", () => {
    const data: ScorePoint[] = [
      { date: "2025-01-01", total_score: 30 },
      { date: "2025-01-02", total_score: 70 },
    ] as ScorePoint[];
    const { container } = render(<LeaderScoreChart data={data} />);
    // shadcn ChartContainer injects the config color as a CSS var on a
    // <style> block scoped to the chart id; assert the gain token is used.
    expect(container.innerHTML).toContain("hsl(var(--gain))");
    // Accessible trend caption is present for screen readers (i18n).
    expect(
      screen.getByText(
        t("discovery.chart.caption", { trend: t("discovery.chart.trendUp") })
      )
    ).toBeInTheDocument();
  });

  it("tints the line via --loss for a falling score history", () => {
    const data: ScorePoint[] = [
      { date: "2025-01-01", total_score: 80 },
      { date: "2025-01-02", total_score: 40 },
    ] as ScorePoint[];
    const { container } = render(<LeaderScoreChart data={data} />);
    expect(container.innerHTML).toContain("hsl(var(--loss))");
    expect(
      screen.getByText(
        t("discovery.chart.caption", { trend: t("discovery.chart.trendDown") })
      )
    ).toBeInTheDocument();
  });
});

describe("BacktestPnlChart", () => {
  it("renders an empty-state message when there are no scenarios", () => {
    render(<BacktestPnlChart scenarios={[]} />);
    expect(
      screen.getByText(t("backtest.result.pnlEmpty"))
    ).toBeInTheDocument();
  });

  it("renders a multi-scenario chart and merges dates without throwing", () => {
    const { container } = render(
      <BacktestPnlChart
        scenarios={[
          {
            label: "optimistic",
            slippage_bps: 50,
            delay_seconds: 10,
            pnl_curve: [
              { date: "2025-01-01", pnl_usd: 10 },
              { date: "2025-01-02", pnl_usd: 25 },
            ],
          },
          {
            label: "pessimistic",
            slippage_bps: 300,
            delay_seconds: 60,
            pnl_curve: [
              { date: "2025-01-02", pnl_usd: -5 },
              { date: "2025-01-03", pnl_usd: -12 },
            ],
          },
        ]}
      />
    );
    expect(container.querySelector("[data-chart]")).toBeInTheDocument();
    expect(
      screen.queryByText("No PnL data available.")
    ).not.toBeInTheDocument();
  });

  it("renders with a single scenario", () => {
    const { container } = render(
      <BacktestPnlChart
        scenarios={[
          {
            label: "neutral",
            slippage_bps: 150,
            delay_seconds: 30,
            pnl_curve: [{ date: "2025-02-01", pnl_usd: 0 }],
          },
        ]}
      />
    );
    expect(container.querySelector("[data-chart]")).toBeInTheDocument();
  });
});
