"use server";

/**
 * Backtest Server Actions — PRD §5.7 "安全寫入" tier.
 * Inserts a backtest_jobs row (status='pending') and redirects to /backtest/[id].
 */

import { redirect } from "next/navigation";
import { db } from "@/lib/db";
import { backtestJobs } from "@/lib/db/schema";

// ---------------------------------------------------------------------------
// Schema validation types (replicate zod shape for server-side coercion)
// Zod is used client-side in the form; here we do a lightweight manual parse.
// ---------------------------------------------------------------------------
export interface BacktestFormValues {
  leader_address: string;
  /** ISO date string YYYY-MM-DD */
  start_date: string;
  /** ISO date string YYYY-MM-DD */
  end_date: string;
  initial_capital_usd: number;
  /** Either copy_ratio or fixed_size_usd must be provided */
  copy_ratio?: number;
  fixed_size_usd?: number;
  max_position_usd: number;
  min_trade_size_usd: number;
  daily_loss_limit_usd: number;
  slippage_bps: number;
  execution_delay_seconds: number;
  category_whitelist: string[];
  scenario: "optimistic" | "neutral" | "pessimistic" | "triple";
}

/**
 * createBacktestJob — called from the /backtest/new form's onSubmit handler.
 * Returns the new job ID so the client can toast + navigate.
 */
/** Postgres DATE shape — the SQL the column accepts. */
const ISO_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

export async function createBacktestJob(
  values: BacktestFormValues
): Promise<{ id: string }> {
  // rev-web I-3: the module comment claims a "lightweight manual parse" but
  // none existed — the form's zod is client-side only and a Server Action is
  // a public RPC. Validate the load-bearing fields server-side and reject
  // (clear zh-TW error) BEFORE the insert. Behaviour for valid inputs is
  // unchanged; only the rejection paths are new.
  if (!ISO_DATE_RE.test(values.start_date)) {
    throw new Error(`起始日期格式錯誤（須為 YYYY-MM-DD）：${values.start_date}`);
  }
  if (!ISO_DATE_RE.test(values.end_date)) {
    throw new Error(`結束日期格式錯誤（須為 YYYY-MM-DD）：${values.end_date}`);
  }
  if (!(values.initial_capital_usd > 0)) {
    throw new Error("初始資金必須大於 0");
  }
  if (!(values.slippage_bps >= 0)) {
    throw new Error("滑價（slippage_bps）不可為負數");
  }
  if (values.execution_delay_seconds < 0) {
    throw new Error("執行延遲（execution_delay_seconds）不可為負數");
  }

  // Resolve scenario into concrete slippage/delay overrides
  let slippage = values.slippage_bps;
  let delay = values.execution_delay_seconds;

  if (values.scenario === "optimistic") {
    slippage = 50;
    delay = 10;
  } else if (values.scenario === "neutral") {
    slippage = 150;
    delay = 30;
  } else if (values.scenario === "pessimistic") {
    slippage = 300;
    delay = 60;
  }
  // "triple" keeps the user-provided values as the neutral baseline;
  // the backtest engine handles triple-run itself.

  const templateParams = {
    copy_ratio: values.copy_ratio ?? null,
    fixed_size_usd: values.fixed_size_usd ?? null,
    max_position_usd: values.max_position_usd,
    min_trade_size_usd: values.min_trade_size_usd,
    daily_loss_limit_usd: values.daily_loss_limit_usd,
    category_whitelist: values.category_whitelist,
    scenario: values.scenario,
  };

  const [inserted] = await db
    .insert(backtestJobs)
    .values({
      leader_addresses: [values.leader_address],
      template_params: templateParams,
      start_date: values.start_date,
      end_date: values.end_date,
      initial_capital_usd: String(values.initial_capital_usd),
      slippage_bps: slippage,
      execution_delay_seconds: delay,
      status: "pending",
      progress: "0",
    })
    .returning({ id: backtestJobs.id });

  redirect(`/backtest/${inserted.id}`);
}
