import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

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
      screen.getByText("No score history recorded yet.")
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
      screen.queryByText("No score history recorded yet.")
    ).not.toBeInTheDocument();
  });
});

describe("BacktestPnlChart", () => {
  it("renders an empty-state message when there are no scenarios", () => {
    render(<BacktestPnlChart scenarios={[]} />);
    expect(screen.getByText("No PnL data available.")).toBeInTheDocument();
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
