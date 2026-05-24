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

import { eq } from "drizzle-orm";
import { revalidatePath } from "next/cache";

import { db } from "@/lib/db";
import { backtestJobs, candidateLeaders } from "@/lib/db/schema";
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

// ---------------------------------------------------------------------------
// addCandidateLeader — manual operator add of a smart-wallet address.
// ---------------------------------------------------------------------------
/**
 * Result of a manual add. `already_tracked` is a non-error outcome: the
 * client renders a friendly hint with the existing `lifecycle_state` rather
 * than firing the generic error toast. `invalid_address` is the input-guard
 * tier (matches `quickBacktest`'s shape rejection).
 */
export type AddCandidateLeaderResult =
  | { ok: true }
  | { ok: false; reason: "invalid_address" }
  | { ok: false; reason: "already_tracked"; lifecycle_state: string }
  | { ok: false; reason: "error" };

/**
 * addCandidateLeader — operator manually enters a smart-wallet address to
 * track. NOT a money-path operation: it only inserts an OBSERVATION row at
 * `lifecycle_state='discovered'` (the default). The discovery worker will
 * re-score it on the next cycle, and the C1 paper-trading ladder's
 * bootstrap clause admits every discovery-found candidate automatically.
 *
 * Schema mapping (the table has no `source_seed`/`source_attribution`/
 * `source_last_seen` column — we map to the existing columns instead):
 *   - sources             ← ['manual']  (the source tag)
 *   - user_notes          ← note        (free-text attribution; nullable)
 *   - first_discovered_at ← now()
 *   - last_active_at      ← now()       (fresh-source marker for the paper worker)
 *   - last_seen_ts        ← now()
 *   - lifecycle_state     ← 'discovered' (schema default; explicit for clarity)
 *
 * Duplicate handling: wallet_address is the PRIMARY KEY. We SELECT first so
 * we can return the EXISTING `lifecycle_state` to the client (a friendly UX
 * hint), then INSERT under that same uniqueness so a concurrent insert race
 * cannot create a dup row (PG raises 23505 → mapped to `already_tracked`).
 */
export async function addCandidateLeader(input: {
  address: string;
  note?: string;
}): Promise<AddCandidateLeaderResult> {
  const raw = (input?.address ?? "").trim();
  if (raw.length === 0 || raw.length > 64 || !EVM_ADDRESS_RE.test(raw)) {
    return { ok: false, reason: "invalid_address" };
  }
  // Polymarket display uses checksummed mixed-case but the existing worker
  // / models normalise to lowercase — keep the DB key shape consistent.
  const wallet = raw.toLowerCase();

  // Trim the note to a sensible cap and treat empty as NULL.
  const rawNote = (input?.note ?? "").trim();
  const note = rawNote.length === 0 ? null : rawNote.slice(0, 500);

  // 1) Duplicate check — return the existing lifecycle_state for the UX hint.
  const existing = await db
    .select({ lifecycle_state: candidateLeaders.lifecycle_state })
    .from(candidateLeaders)
    .where(eq(candidateLeaders.wallet_address, wallet))
    .limit(1);
  if (existing.length > 0) {
    return {
      ok: false,
      reason: "already_tracked",
      lifecycle_state: existing[0].lifecycle_state ?? "discovered",
    };
  }

  // 2) INSERT the bare observation row. Stats columns stay NULL — the
  //    discovery worker fills them in on the next re-score.
  const now = new Date();
  try {
    await db.insert(candidateLeaders).values({
      wallet_address: wallet,
      sources: ["manual"],
      user_notes: note,
      first_discovered_at: now,
      last_active_at: now,
      last_seen_ts: now,
      lifecycle_state: "discovered",
      updated_at: now,
    });
  } catch (e) {
    // Concurrent-insert race: PG 23505 → another writer landed first.
    // Re-query the lifecycle_state for the same UX hint as the eager path.
    const code = (e as { code?: string } | null)?.code;
    if (code === "23505") {
      const race = await db
        .select({ lifecycle_state: candidateLeaders.lifecycle_state })
        .from(candidateLeaders)
        .where(eq(candidateLeaders.wallet_address, wallet))
        .limit(1);
      return {
        ok: false,
        reason: "already_tracked",
        lifecycle_state: race[0]?.lifecycle_state ?? "discovered",
      };
    }
    return { ok: false, reason: "error" };
  }

  // Refresh the server component so the new row appears in the list.
  revalidatePath("/discovery");
  return { ok: true };
}
