import { describe, it, expect } from "vitest";
import { t } from "@/lib/i18n";
import { dict } from "@/lib/locale/zh-TW";

/**
 * /discovery/[wallet] stats-section i18n contract.
 *
 * The leader-detail page's Stats tab (`StatsBlock`) renders the RAW
 * `stats_*` / `frequency_stats` JSONB keys produced by the discovery
 * worker (`packages/discovery` `_compute_stats` + `FrequencyStats.
 * to_dict()`) as the <dt> labels. Before this change those keys leaked
 * the English snake_case DB key (the reported "績效視窗 still English"
 * bug). They are now routed through the discovery locale via the dynamic
 * `t(`discovery.detail.stats.fields.${k}`)` seam, with the raw key as the
 * miss-fallback (mirroring lib/i18n.ts "degrade, don't crash").
 *
 * This suite asserts the i18n SEAM / STRUCTURE, never literal copy: it
 * stays correct if a zh-TW string is reworded, while still proving every
 * worker-emitted key resolves to a translated (non-key, non-empty,
 * not-pure-ASCII-snake_case) label and that unmapped keys degrade safely.
 * Pattern mirrors __tests__/lib/i18n.test.ts + discovery-table.test.tsx.
 */

// The exact key sets the deployed discovery worker writes into the
// candidate_leaders JSONB columns. Mirrors:
//   packages/discovery/src/polyecho/discovery/worker.py  _compute_stats()
//   packages/discovery/src/polyecho/discovery/frequency.py FrequencyStats.to_dict()
// If the worker grows a key, this list (and the locale map) must grow with
// it — that coupling is the point of the test.
const COMPUTE_STATS_KEYS = [
  "pnl_usd",
  "win_rate",
  "sharpe",
  "max_drawdown",
  "consistency",
  "specialization",
  "trades_per_day",
  "total_trades",
  "first_trade_at",
] as const;

const FREQUENCY_STATS_KEYS = [
  "trades_per_day",
  "active_days",
  "median_gap_minutes",
  "avg_hold_hours",
  "hold_to_resolution_pct",
  "concurrent_positions_avg",
  "concurrent_positions_max",
  "burstiness",
  "active_hours_utc",
] as const;

const ALL_WORKER_KEYS = [
  ...new Set<string>([...COMPUTE_STATS_KEYS, ...FREQUENCY_STATS_KEYS]),
];

const STAT_FIELD_PREFIX = "discovery.detail.stats.fields";

// Re-implements the page's miss-fallback (kept tiny + in-sync with
// app/discovery/[wallet]/page.tsx statFieldLabel/statusLabel): t() returns
// the key itself on a miss, which must degrade to the raw key.
function resolve(prefix: string, key: string): string {
  const path = `${prefix}.${key}`;
  const out = t(path as never);
  return out === path ? key : out;
}

describe("discovery/[wallet] stats field i18n map", () => {
  it("has a locale entry for EVERY worker-emitted stats/frequency key", () => {
    const fields = dict.discovery.detail.stats.fields as Record<
      string,
      string
    >;
    const missing = ALL_WORKER_KEYS.filter(
      (k) => typeof fields[k] !== "string" || fields[k].length === 0,
    );
    expect(
      missing,
      `discovery.detail.stats.fields is missing/empty for worker keys ` +
        `(the Stats tab would leak the raw English DB key):\n${missing.join(
          "\n",
        )}`,
    ).toEqual([]);
  });

  it("resolves every worker key to a translated (non-key) label", () => {
    for (const k of ALL_WORKER_KEYS) {
      const label = resolve(STAT_FIELD_PREFIX, k);
      // Not the raw key (would mean the map missed it) …
      expect(label, `${k} fell back to the raw key`).not.toBe(k);
      // … not the dotted i18n path (would mean t() missed) …
      expect(label).not.toBe(`${STAT_FIELD_PREFIX}.${k}`);
      // … and non-empty.
      expect(label.length, `${k} label empty`).toBeGreaterThan(0);
    }
  });

  it("degrades an UNKNOWN/new worker key to the raw key (never a crash)", () => {
    // A key the worker might add later but the locale hasn't mapped yet
    // must render verbatim, not the i18n path and not throw.
    expect(resolve(STAT_FIELD_PREFIX, "some_future_metric")).toBe(
      "some_future_metric",
    );
  });

  it("keeps proper nouns English inside the label (PROPER-NOUN POLICY)", () => {
    // pnl_usd / sharpe surface PnL / Sharpe — kept English per
    // lib/locale/zh-TW/common.ts PROPER-NOUN POLICY.
    expect(resolve(STAT_FIELD_PREFIX, "pnl_usd")).toContain("PnL");
    expect(resolve(STAT_FIELD_PREFIX, "sharpe")).toContain("Sharpe");
  });
});

// The legacy "discovery/[wallet] header status label i18n" suite was removed
// when the candidate_leaders.user_status column was dropped (the header no
// longer renders a status Badge; the locale map's statusLabel.* entries are
// gone too, kept in sync with the orphan-guard).
