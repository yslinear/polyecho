import { describe, it, expect } from "vitest";
import {
  adaptScenarioKeyedLeader,
  isScenarioKeyedLeader,
} from "@/app/backtest/[id]/adapt-result";

/**
 * P13 regression-lock for the backtest result adapter.
 *
 * The worker writes a per-leader, scenario-keyed JSONB dict whose field
 * names and units differ from what the /backtest/[id] UI components consume
 * (win_rate is a 0..1 ratio; total_return is USD not %; daily_pnl_curve is
 * [date, value] tuples). The adapter bridges that gap. "A misread number is
 * a bug" (PRODUCT.md) — every conversion below is asserted on concrete
 * values lifted from the real prod job 557eef18.
 */

// Real `neutral` scenario shape from prod job 557eef18 (verified DB read).
const NEUTRAL_SCENARIO = {
  stats: {
    win_rate: 1.0, // 0..1 ratio
    sharpe_ratio: 1.664,
    avg_hold_hours: 2.873,
    total_fees_usd: 0.0,
    max_drawdown_pct: 0.0,
    total_return_usd: 6.1927670836458875,
    annualized_return_pct: 2.48,
  },
  params: {
    slippage_bps: 150,
    execution_delay_seconds: 30,
    initial_capital_usd: 1000.0,
  },
  skip_analysis: {
    total: 33,
    below_min_size: 1,
    risk_triggered: 32,
    category_filtered: 0,
    insufficient_liquidity: 0,
  },
  daily_pnl_curve: [
    ["2026-02-19", 0.0],
    ["2026-02-20", 3.1],
    ["2026-02-21", 6.19],
  ],
};

const OPTIMISTIC_SCENARIO = {
  ...NEUTRAL_SCENARIO,
  params: { ...NEUTRAL_SCENARIO.params, slippage_bps: 50, execution_delay_seconds: 10 },
};
const PESSIMISTIC_SCENARIO = {
  ...NEUTRAL_SCENARIO,
  params: { ...NEUTRAL_SCENARIO.params, slippage_bps: 300, execution_delay_seconds: 60 },
};

const SCENARIO_KEYED_LEADER = {
  optimistic: OPTIMISTIC_SCENARIO,
  neutral: NEUTRAL_SCENARIO,
  pessimistic: PESSIMISTIC_SCENARIO,
};

describe("isScenarioKeyedLeader — detection heuristic", () => {
  it("accepts a worker scenario-keyed leader dict", () => {
    expect(isScenarioKeyedLeader(SCENARIO_KEYED_LEADER)).toBe(true);
  });

  it("accepts a subset of scenario keys", () => {
    expect(isScenarioKeyedLeader({ neutral: NEUTRAL_SCENARIO })).toBe(true);
  });

  it("rejects a legacy per-leader { scenarios } envelope", () => {
    // Legacy: keys are NOT scenario names, so the subset test fails and the
    // legacy render path in page.tsx keeps owning it.
    expect(isScenarioKeyedLeader({ scenarios: [] })).toBe(false);
  });

  it("rejects a legacy per-leader { stats } envelope", () => {
    expect(isScenarioKeyedLeader({ stats: {} })).toBe(false);
  });

  it("rejects a per-leader { error } envelope (Alert path untouched)", () => {
    expect(
      isScenarioKeyedLeader({ error: "RuntimeError: boom" }),
    ).toBe(false);
  });

  it("rejects a scenario-named key whose value lacks a stats object", () => {
    expect(isScenarioKeyedLeader({ neutral: { params: {} } })).toBe(false);
  });

  it("rejects empty / non-object inputs", () => {
    expect(isScenarioKeyedLeader({})).toBe(false);
    expect(isScenarioKeyedLeader(null)).toBe(false);
    expect(isScenarioKeyedLeader(undefined)).toBe(false);
    expect(isScenarioKeyedLeader([])).toBe(false);
  });
});

describe("adaptScenarioKeyedLeader — field mapping (job 557eef18)", () => {
  const scenarios = adaptScenarioKeyedLeader(SCENARIO_KEYED_LEADER);

  it("emits the three scenarios in fixed best/base/worst order", () => {
    expect(scenarios.map((s) => s.label)).toEqual([
      "optimistic",
      "neutral",
      "pessimistic",
    ]);
  });

  it("maps the neutral scenario stats with correct units", () => {
    const neutral = scenarios.find((s) => s.label === "neutral")!;
    // win_rate 1.0 (ratio) -> win_rate_pct 100
    expect(neutral.stats.win_rate_pct).toBe(100);
    // total_return_usd 6.1927670836458875 / initial 1000 * 100 ≈ 0.6193 %
    expect(neutral.stats.total_return_pct).toBeCloseTo(0.6193, 4);
    // sharpe_ratio -> sharpe (passthrough)
    expect(neutral.stats.sharpe).toBe(1.664);
    // annualized / drawdown / hold hours pass through unchanged
    expect(neutral.stats.annualized_return_pct).toBe(2.48);
    expect(neutral.stats.max_drawdown_pct).toBe(0.0);
    expect(neutral.stats.avg_hold_hours).toBe(2.873);
    // skip_analysis.total -> trades_total
    expect(neutral.stats.trades_total).toBe(33);
    // trades_skipped = 1 + 32 + 0 + 0 = 33
    expect(neutral.stats.trades_skipped).toBe(33);
  });

  it("maps scenario params (slippage / delay)", () => {
    const neutral = scenarios.find((s) => s.label === "neutral")!;
    expect(neutral.slippage_bps).toBe(150);
    expect(neutral.delay_seconds).toBe(30);

    const optimistic = scenarios.find((s) => s.label === "optimistic")!;
    expect(optimistic.slippage_bps).toBe(50);
    expect(optimistic.delay_seconds).toBe(10);

    const pessimistic = scenarios.find((s) => s.label === "pessimistic")!;
    expect(pessimistic.slippage_bps).toBe(300);
    expect(pessimistic.delay_seconds).toBe(60);
  });

  it("converts daily_pnl_curve tuples to { date, pnl_usd } points", () => {
    const neutral = scenarios.find((s) => s.label === "neutral")!;
    expect(neutral.pnl_curve).toEqual([
      { date: "2026-02-19", pnl_usd: 0.0 },
      { date: "2026-02-20", pnl_usd: 3.1 },
      { date: "2026-02-21", pnl_usd: 6.19 },
    ]);
  });

  it("guards a zero initial_capital_usd (no divide-by-zero)", () => {
    const adapted = adaptScenarioKeyedLeader({
      neutral: {
        ...NEUTRAL_SCENARIO,
        params: { ...NEUTRAL_SCENARIO.params, initial_capital_usd: 0 },
      },
    });
    expect(adapted[0].stats.total_return_pct).toBe(0);
  });

  it("tolerates a missing daily_pnl_curve (empty array)", () => {
    const adapted = adaptScenarioKeyedLeader({
      neutral: {
        stats: NEUTRAL_SCENARIO.stats,
        params: NEUTRAL_SCENARIO.params,
        skip_analysis: NEUTRAL_SCENARIO.skip_analysis,
      },
    });
    expect(adapted[0].pnl_curve).toEqual([]);
  });

  it("only emits the scenarios that are present", () => {
    const adapted = adaptScenarioKeyedLeader({
      optimistic: OPTIMISTIC_SCENARIO,
      pessimistic: PESSIMISTIC_SCENARIO,
    });
    expect(adapted.map((s) => s.label)).toEqual([
      "optimistic",
      "pessimistic",
    ]);
  });
});
