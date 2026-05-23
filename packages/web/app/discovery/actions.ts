"use server";

/**
 * Discovery Server Actions — PRD §5.7 "安全寫入" (sanctioned write) tier.
 *
 * - quickBacktest: inserts a backtest_jobs row for a single leader using a
 *   90-day window and the Balanced template preset (PRD §5.6), returning the
 *   new job id so the client can toast + navigate to /backtest/<id>.
 *
 * The legacy `updateLeaderStatus` / `LeaderStatus` Server Action was REMOVED
 * along with the `candidate_leaders.user_status` column it wrote to. The C1
 * paper-trading ladder's bootstrap clause now admits every discovery-found
 * candidate into paper automatically; promotion to a real follow remains the
 * existing danger-passphrase-gated Server Action in `app/research/actions.ts`.
 *
 * Mirrors the insert pattern in app/backtest/actions.ts (Drizzle client +
 * schema). No redirect here — the client island handles navigation/refresh
 * so it can also fire a sonner toast.
 */

import { db } from "@/lib/db";
import { backtestJobs } from "@/lib/db/schema";
import { EVM_ADDRESS_RE } from "@/lib/format";

// ---------------------------------------------------------------------------
// Balanced template preset — PRD §5.6
// ---------------------------------------------------------------------------
const BALANCED_TEMPLATE = {
  copy_ratio: 0.001,
  max_position_usd: 50,
  min_trade_size_usd: 5,
  daily_loss_limit_usd: 100,
  slippage_bps: 150,
  execution_delay_seconds: 30,
} as const;

const QUICK_BACKTEST_WINDOW_DAYS = 90;
const QUICK_BACKTEST_CAPITAL_USD = 1000;

/** YYYY-MM-DD for a Date (UTC, matches Postgres DATE column). */
function isoDate(d: Date): string {
  return d.toISOString().slice(0, 10);
}

/**
 * quickBacktest — one-click 90-day Balanced backtest for a single leader.
 * Returns the new job id; the caller toasts + navigates to /backtest/<id>.
 */
export async function quickBacktest(
  wallet: string
): Promise<{ id: string }> {
  // R16 HIGH: reject non-string / empty / over-long / non-address shapes
  // with a clear zh-TW error before any DB write.
  const w = (wallet ?? "").trim();
  if (w.length === 0) {
    throw new Error("缺少領單者錢包位址");
  }
  if (w.length > 64 || !EVM_ADDRESS_RE.test(w)) {
    throw new Error(`領單者錢包位址格式錯誤：${w.slice(0, 64)}`);
  }

  const end = new Date();
  const start = new Date(end);
  start.setUTCDate(start.getUTCDate() - QUICK_BACKTEST_WINDOW_DAYS);

  const templateParams = {
    copy_ratio: BALANCED_TEMPLATE.copy_ratio,
    fixed_size_usd: null,
    max_position_usd: BALANCED_TEMPLATE.max_position_usd,
    min_trade_size_usd: BALANCED_TEMPLATE.min_trade_size_usd,
    daily_loss_limit_usd: BALANCED_TEMPLATE.daily_loss_limit_usd,
    category_whitelist: [],
    preset: "Balanced",
  };

  const [inserted] = await db
    .insert(backtestJobs)
    .values({
      leader_addresses: [w],
      template_params: templateParams,
      start_date: isoDate(start),
      end_date: isoDate(end),
      initial_capital_usd: String(QUICK_BACKTEST_CAPITAL_USD),
      slippage_bps: BALANCED_TEMPLATE.slippage_bps,
      execution_delay_seconds: BALANCED_TEMPLATE.execution_delay_seconds,
      status: "pending",
      progress: "0",
    })
    .returning({ id: backtestJobs.id });

  return { id: inserted.id };
}
