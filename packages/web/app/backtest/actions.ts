"use server";

/**
 * Backtest Server Actions — PRD §5.7 "安全寫入" tier.
 * Inserts a backtest_jobs row (status='pending') and redirects to /backtest/[id].
 */

import { redirect } from "next/navigation";
import { desc, eq } from "drizzle-orm";
import { db } from "@/lib/db";
import { backtestJobs } from "@/lib/db/schema";

// ---------------------------------------------------------------------------
// List Server Action (R-list 2026-05-21)
// ---------------------------------------------------------------------------
/**
 * Backtest job status taxonomy. The DB column is a free-form `text` (see
 * schema.ts), but only these five values are produced by the form/worker.
 * Centralised here so the list UI + tests can `import { BacktestJobStatus }`.
 */
export type BacktestJobStatus =
  | "pending"
  | "queued"
  | "running"
  | "completed"
  | "failed";

/**
 * Row shape returned by `listBacktestJobs`. Intentionally a STRICT SUBSET
 * of `backtestJobs.$inferSelect` — the `result` jsonb (which can be
 * megabytes for a 3-scenario run) is collapsed to a key-count so the
 * over-the-wire payload stays small. The list UI never needs the full
 * `result` body; that is only loaded by `/backtest/[id]`.
 */
export interface BacktestJobSummary {
  id: string;
  leader_addresses: string[];
  start_date: string;
  end_date: string;
  initial_capital_usd: string;
  status: string;
  progress: string;
  /** Number of top-level keys in `result` jsonb (0 if NULL). */
  result_keys: number;
  created_at: Date;
}

/**
 * listBacktestJobs — read-only list query for the `/backtest` page.
 *
 * Surfaced as a Server Action so a future client-side refresh /
 * pagination island can call it without going through the SSR cache.
 * Today the SSR page also calls it directly (so the list query lives in
 * exactly one place and stays consistent between SSR and client refresh).
 *
 * IMPORTANT: this is a READ action (PRD §5.7 "唯讀" tier). No mutation,
 * no danger-action gating. It still filters `result` down to a key-count
 * to keep payload small even when the result jsonb is megabytes.
 */
export async function listBacktestJobs(opts?: {
  limit?: number;
  statusFilter?: BacktestJobStatus;
}): Promise<BacktestJobSummary[]> {
  const limit = Math.max(1, Math.min(opts?.limit ?? 50, 200));

  const base = db
    .select({
      id: backtestJobs.id,
      leader_addresses: backtestJobs.leader_addresses,
      start_date: backtestJobs.start_date,
      end_date: backtestJobs.end_date,
      initial_capital_usd: backtestJobs.initial_capital_usd,
      status: backtestJobs.status,
      progress: backtestJobs.progress,
      result: backtestJobs.result,
      created_at: backtestJobs.created_at,
    })
    .from(backtestJobs);

  // Drizzle's chain typing keeps `where` optional; the SQL builder accepts
  // calling .where(eq(...)) only when a status filter is requested.
  const rows = await (opts?.statusFilter
    ? base.where(eq(backtestJobs.status, opts.statusFilter))
    : base
  )
    .orderBy(desc(backtestJobs.created_at))
    .limit(limit);

  return rows.map((r) => {
    const result = r.result;
    const result_keys =
      result && typeof result === "object" && !Array.isArray(result)
        ? Object.keys(result as Record<string, unknown>).length
        : 0;
    return {
      id: r.id,
      leader_addresses: r.leader_addresses,
      start_date: r.start_date,
      end_date: r.end_date,
      initial_capital_usd: r.initial_capital_usd,
      status: r.status,
      progress: r.progress,
      result_keys,
      created_at: r.created_at,
    };
  });
}

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

/**
 * rev-web R16 HIGH: server-side scenario allowlist. The client form's zod is
 * NOT a server-side guard (a Server Action is a public RPC), and the
 * `scenario` value drives the slippage/delay overrides + is persisted into
 * `template_params`. Reject anything outside the known set BEFORE the insert.
 */
const KNOWN_SCENARIOS: ReadonlySet<string> = new Set<
  BacktestFormValues["scenario"]
>(["optimistic", "neutral", "pessimistic", "triple"]);

/** A finite, strictly-positive number (rejects NaN/Infinity/0/negatives). */
function isPositiveFinite(n: unknown): n is number {
  return typeof n === "number" && Number.isFinite(n) && n > 0;
}

/** A finite, non-negative number (allows 0, rejects NaN/Infinity/negatives). */
function isNonNegativeFinite(n: unknown): n is number {
  return typeof n === "number" && Number.isFinite(n) && n >= 0;
}

export async function createBacktestJob(
  values: BacktestFormValues
): Promise<{ id: string }> {
  // rev-web I-3 / R16 HIGH: the module comment claims a "lightweight manual
  // parse" but the original had only a few checks — the form's zod is
  // client-side only and a Server Action is a public RPC. Validate ALL
  // load-bearing fields server-side and reject (clear zh-TW error) BEFORE
  // the insert. Behaviour for valid inputs is unchanged; only the rejection
  // paths are new.
  if (!ISO_DATE_RE.test(values.start_date)) {
    throw new Error(`起始日期格式錯誤（須為 YYYY-MM-DD）：${values.start_date}`);
  }
  if (!ISO_DATE_RE.test(values.end_date)) {
    throw new Error(`結束日期格式錯誤（須為 YYYY-MM-DD）：${values.end_date}`);
  }
  // R16 HIGH: start_date must be strictly before end_date. Lexicographic
  // comparison is correct for the YYYY-MM-DD shape already validated above.
  if (!(values.start_date < values.end_date)) {
    throw new Error("起始日期必須早於結束日期");
  }
  // R16 HIGH: scenario ∈ the known allowlist (drives slippage/delay + is
  // persisted into template_params).
  if (!KNOWN_SCENARIOS.has(values.scenario)) {
    throw new Error(`未知的情境（scenario）：${String(values.scenario)}`);
  }
  if (!isPositiveFinite(values.initial_capital_usd)) {
    throw new Error("初始資金必須大於 0");
  }
  // R16 HIGH: at least ONE of copy_ratio / fixed_size_usd must be a positive
  // finite number, and if either IS provided it must be positive finite
  // (reject negative/zero/NaN/Infinity).
  const hasCopyRatio = values.copy_ratio != null;
  const hasFixedSize = values.fixed_size_usd != null;
  if (!hasCopyRatio && !hasFixedSize) {
    throw new Error("請至少提供 copy_ratio 或 fixed_size_usd 其中一項");
  }
  if (hasCopyRatio && !isPositiveFinite(values.copy_ratio)) {
    throw new Error("copy_ratio 必須為大於 0 的有效數值");
  }
  if (hasFixedSize && !isPositiveFinite(values.fixed_size_usd)) {
    throw new Error("fixed_size_usd 必須為大於 0 的有效數值");
  }
  // R16 HIGH: the risk-limit fields must be positive finite (a zero/negative
  // position cap / loss limit would silently disable the guard rails).
  if (!isPositiveFinite(values.max_position_usd)) {
    throw new Error("max_position_usd 必須為大於 0 的有效數值");
  }
  if (!isPositiveFinite(values.min_trade_size_usd)) {
    throw new Error("min_trade_size_usd 必須為大於 0 的有效數值");
  }
  if (!isPositiveFinite(values.daily_loss_limit_usd)) {
    throw new Error("daily_loss_limit_usd 必須為大於 0 的有效數值");
  }
  if (!isNonNegativeFinite(values.slippage_bps)) {
    throw new Error("滑價（slippage_bps）不可為負數");
  }
  if (!isNonNegativeFinite(values.execution_delay_seconds)) {
    throw new Error("執行延遲（execution_delay_seconds）不可為負數");
  }
  // R16 HIGH: category_whitelist must be an array of reasonable non-empty
  // bounded strings (it is persisted verbatim into template_params jsonb).
  if (!Array.isArray(values.category_whitelist)) {
    throw new Error("category_whitelist 必須為字串陣列");
  }
  for (const c of values.category_whitelist) {
    if (typeof c !== "string" || c.trim().length === 0 || c.length > 64) {
      throw new Error("category_whitelist 含有無效的分類字串");
    }
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
