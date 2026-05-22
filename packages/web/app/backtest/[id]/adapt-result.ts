/**
 * adapt-result — bridges the backtest worker's result JSONB shape to the
 * `ScenarioResult[]` shape the /backtest/[id] UI components consume.
 *
 * Why this exists (P13 Bug):
 * The worker (`packages/backtest`) writes a per-leader, scenario-keyed dict:
 *
 *   result.leaders["0x…"] = {
 *     optimistic:  <SCENARIO>,
 *     neutral:     <SCENARIO>,
 *     pessimistic: <SCENARIO>,
 *   }
 *
 * where each <SCENARIO> nests `stats` / `params` / `skip_analysis` /
 * `daily_pnl_curve` with worker-native field names (win_rate as a 0..1
 * ratio, total_return_usd in USD, daily_pnl_curve as [date, value] tuples).
 *
 * The UI's `ScenarioResult` expects a flat `{ label, slippage_bps,
 * delay_seconds, stats, pnl_curve }` with UI-native names (win_rate_pct as a
 * 0..100 %, total_return_pct as a %, pnl_curve as `{date, pnl_usd}[]`).
 *
 * Option A (per task brief): adapt in the UI, never touch the worker.
 */

// ---------------------------------------------------------------------------
// UI-facing shapes (kept in sync with page.tsx / backtest-pnl-chart.tsx)
// ---------------------------------------------------------------------------
export interface ScenarioStats {
  total_return_pct: number;
  annualized_return_pct: number;
  sharpe: number;
  max_drawdown_pct: number;
  win_rate_pct: number;
  avg_hold_hours: number;
  trades_total: number;
  trades_skipped: number;
}

export interface DayPoint {
  date: string;
  pnl_usd: number;
}

export interface ScenarioResult {
  label: "optimistic" | "neutral" | "pessimistic";
  slippage_bps: number;
  delay_seconds: number;
  stats: ScenarioStats;
  pnl_curve: DayPoint[];
}

// ---------------------------------------------------------------------------
// Worker-native shapes (verified against prod DB job 557eef18)
// ---------------------------------------------------------------------------
interface WorkerScenarioStats {
  win_rate?: number; // 0..1 RATIO
  sharpe_ratio?: number;
  avg_hold_hours?: number;
  total_fees_usd?: number;
  max_drawdown_pct?: number; // already a %
  total_return_usd?: number; // USD, NOT a %
  annualized_return_pct?: number; // already a %
}

interface WorkerScenarioParams {
  slippage_bps?: number;
  execution_delay_seconds?: number;
  initial_capital_usd?: number;
}

interface WorkerSkipAnalysis {
  total?: number;
  below_min_size?: number;
  risk_triggered?: number;
  category_filtered?: number;
  insufficient_liquidity?: number;
}

interface WorkerScenario {
  stats?: WorkerScenarioStats;
  params?: WorkerScenarioParams;
  skip_analysis?: WorkerSkipAnalysis;
  daily_pnl_curve?: Array<[string, number]>;
}

/** The three scenario keys, in the canonical best/base/worst order. */
const SCENARIO_KEYS = ["optimistic", "neutral", "pessimistic"] as const;
type ScenarioKey = (typeof SCENARIO_KEYS)[number];

// ---------------------------------------------------------------------------
// Detection
// ---------------------------------------------------------------------------
/**
 * True when `value` is a worker-native scenario-keyed leader dict, i.e. its
 * keys are a non-empty subset of {optimistic, neutral, pessimistic} and every
 * present scenario value carries a `stats` object.
 *
 * This deliberately rejects the legacy per-leader envelope (`{ scenarios }` /
 * `{ stats }` / `{ error }`): those keys are NOT scenario names, so the
 * subset test fails and the legacy render paths in page.tsx keep handling
 * them. A leader value with an `error` envelope also fails here (no
 * scenario-named keys), so the destructive-Alert path is untouched.
 */
export function isScenarioKeyedLeader(
  value: unknown,
): value is Record<ScenarioKey, WorkerScenario> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const keys = Object.keys(value as Record<string, unknown>);
  if (keys.length === 0) {
    return false;
  }
  // Every key must be a known scenario name …
  if (!keys.every((k) => (SCENARIO_KEYS as readonly string[]).includes(k))) {
    return false;
  }
  // … and every present scenario must carry a `stats` object.
  const dict = value as Record<string, unknown>;
  return keys.every((k) => {
    const scenario = dict[k];
    return (
      !!scenario &&
      typeof scenario === "object" &&
      !Array.isArray(scenario) &&
      typeof (scenario as WorkerScenario).stats === "object" &&
      (scenario as WorkerScenario).stats !== null
    );
  });
}

// ---------------------------------------------------------------------------
// Conversion
// ---------------------------------------------------------------------------
function num(v: unknown, fallback = 0): number {
  return typeof v === "number" && Number.isFinite(v) ? v : fallback;
}

/** Convert one worker `<SCENARIO>` into the UI `ScenarioResult`. */
function adaptScenario(
  label: ScenarioKey,
  scenario: WorkerScenario,
): ScenarioResult {
  const stats = scenario.stats ?? {};
  const params = scenario.params ?? {};
  const skip = scenario.skip_analysis ?? {};

  const initialCapital = num(params.initial_capital_usd);
  const totalReturnUsd = num(stats.total_return_usd);
  const totalReturnPct =
    initialCapital > 0 ? (totalReturnUsd / initialCapital) * 100 : 0;

  const tradesSkipped =
    num(skip.below_min_size) +
    num(skip.risk_triggered) +
    num(skip.category_filtered) +
    num(skip.insufficient_liquidity);

  const pnlCurve: DayPoint[] = Array.isArray(scenario.daily_pnl_curve)
    ? scenario.daily_pnl_curve
        .filter((p) => Array.isArray(p) && p.length >= 2)
        .map(([date, value]) => ({ date: String(date), pnl_usd: num(value) }))
    : [];

  return {
    label,
    slippage_bps: num(params.slippage_bps),
    delay_seconds: num(params.execution_delay_seconds),
    pnl_curve: pnlCurve,
    stats: {
      total_return_pct: totalReturnPct,
      annualized_return_pct: num(stats.annualized_return_pct),
      sharpe: num(stats.sharpe_ratio),
      max_drawdown_pct: num(stats.max_drawdown_pct),
      win_rate_pct: num(stats.win_rate) * 100, // worker stores a 0..1 ratio
      avg_hold_hours: num(stats.avg_hold_hours),
      trades_total: num(skip.total),
      trades_skipped: tradesSkipped,
    },
  };
}

/**
 * Convert a worker scenario-keyed leader dict into `ScenarioResult[]` in the
 * fixed [optimistic, neutral, pessimistic] order. Missing scenarios are
 * skipped (a leader could legitimately carry only a subset).
 */
export function adaptScenarioKeyedLeader(
  leader: Record<ScenarioKey, WorkerScenario>,
): ScenarioResult[] {
  return SCENARIO_KEYS.filter((k) => leader[k] != null).map((k) =>
    adaptScenario(k, leader[k]),
  );
}
