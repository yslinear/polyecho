/**
 * /discovery — merged candidate funnel page (Phase-1 merge, Option C).
 *
 * Server Component. Reads candidate_leaders LEFT JOIN paper_sessions through
 * Drizzle, applies the `?state=…` chip filter on the SERVER, and hands the
 * filtered rows + the active state to a client island (DiscoveryTable).
 *
 * The chip strip writes ?state=… via plain `<Link>`s in the island so the
 * URL stays shareable / refresh-stable; the SERVER re-runs the right query
 * on every navigation (force-dynamic). Five chips:
 *
 *   排行榜    `?state=discovered` (or absent — the default)
 *     → the full pool, ranked by total_score DESC. The legacy /discovery
 *       view, unchanged. NO paper-join filter is applied — every candidate
 *       (incl. ones that never entered the ladder) is shown.
 *
 *   驗證中    `?state=active`
 *     → lifecycle_state IN (candidate, paper, cooldown). The closest
 *       analogue of the legacy /research view minus trial_ready / retired.
 *
 *   可升 live  `?state=trial_ready`
 *     → lifecycle_state = trial_ready. The promote-to-live ladder bucket.
 *
 *   已退休    `?state=retired`
 *     → lifecycle_state = retired. Cooldown count + entry timestamp.
 *
 *   全部     `?state=all`
 *     → the union — every candidate the discovery scan has surfaced.
 *
 * The promote-to-live Server Action stays in `app/research/actions.ts` (the
 * operator scoped it so) and is imported by the client island.
 *
 * Phase-1 explicitly does NOT touch the DB schema, the CLI, or
 * candidate_leaders.user_status. The merged UI just stops rendering /
 * filtering on it.
 */

import Link from "next/link";
import { and, desc, eq, inArray, sql } from "drizzle-orm";

import { db } from "@/lib/db";
import { candidateLeaders, paperSessions } from "@/lib/db/schema";
import { t } from "@/lib/i18n";
import { DiscoveryTable, type DiscoveryRow, type ChipState } from "./discovery-table";

// Reads the database on every request — never prerender at build time
// (no DB is available during `next build` / CI).
export const dynamic = "force-dynamic";

// Drizzle-inferred row type (re-exported for the row helper in tests).
export type CandidateLeaderRow = typeof candidateLeaders.$inferSelect;

const VALID_CHIPS = new Set<ChipState>([
  "discovered",
  "active",
  "trial_ready",
  "retired",
  "all",
]);

/**
 * Coerce the raw `searchParams.state` to a valid ChipState. Absent / unknown
 * → "discovered" (the legacy /discovery default; the leaderboard view).
 */
function parseChip(raw: string | string[] | undefined): ChipState {
  const v = Array.isArray(raw) ? raw[0] : raw;
  if (v && VALID_CHIPS.has(v as ChipState)) return v as ChipState;
  return "discovered";
}

/** Active lifecycle-state set per chip (server-side filter). */
const STATE_FILTER: Record<ChipState, readonly string[] | null> = {
  // Leaderboard view = the full pool. No lifecycle filter.
  discovered: null,
  // Currently being evaluated by the ladder.
  active: ["candidate", "paper", "cooldown"],
  // Promotable now.
  trial_ready: ["trial_ready"],
  // Terminal bucket.
  retired: ["retired"],
  // Everything the scan has surfaced.
  all: null,
};

interface DiscoveryPageProps {
  searchParams: Promise<{ state?: string | string[] }>;
}

export default async function DiscoveryPage({ searchParams }: DiscoveryPageProps) {
  // Next 15 dynamic API: searchParams is a Promise — must be awaited.
  const params = await searchParams;
  const chip = parseChip(params.state);
  const states = STATE_FILTER[chip];

  // The leaderboard chip (排行榜) wants leader pool fields only — fast path.
  // Every other chip needs the paper_sessions join for copyability / paper
  // PnL / paper days. We unify them into one Drizzle query that LEFT-JOINs
  // paper_sessions either way; the join is cheap (idx_paper_sessions_leader)
  // and lets the client island use one row shape across chips.
  const joined = await db
    .select({
      wallet_address: candidateLeaders.wallet_address,
      display_name: candidateLeaders.display_name,
      strategy_type: candidateLeaders.strategy_type,
      profit_score: candidateLeaders.profit_score,
      replicability_score: candidateLeaders.replicability_score,
      total_score: candidateLeaders.total_score,
      risk_flags: candidateLeaders.risk_flags,
      last_seen_ts: candidateLeaders.last_seen_ts,
      last_active_at: candidateLeaders.last_active_at,
      lifecycle_state: candidateLeaders.lifecycle_state,
      locked: candidateLeaders.locked,
      cooldown_count: candidateLeaders.cooldown_count,
      cooldown_until: candidateLeaders.cooldown_until,
      // Use the candidate row's first_discovered_at as "進入時間" for the
      // retired chip — paper_sessions.started_at is per-session and would
      // miss never-paper candidates.
      first_discovered_at: candidateLeaders.first_discovered_at,
      updated_at: candidateLeaders.updated_at,
      paper_state: paperSessions.state,
      paper_started_at: paperSessions.started_at,
      paper_trade_count: paperSessions.trade_count,
      paper_copyable_pnl: paperSessions.copyable_pnl,
      paper_max_drawdown: paperSessions.max_drawdown,
      paper_score: paperSessions.score,
    })
    .from(candidateLeaders)
    .leftJoin(
      paperSessions,
      eq(paperSessions.leader_address, candidateLeaders.wallet_address),
    )
    .where(
      // states === null → no filter (排行榜 + 全部). Otherwise restrict to
      // the chip's lifecycle_state set. `and()` with a single arg degrades
      // to that arg; with `undefined` it degrades to a true predicate.
      and(
        states ? inArray(candidateLeaders.lifecycle_state, [...states]) : sql`true`,
      ),
    )
    // Server-side default order = total_score DESC. The client island then
    // applies the chip's own default sort on top (chip-aware idiom — see
    // discovery-table.tsx), so this only stabilises ties.
    .orderBy(desc(candidateLeaders.total_score));

  const rows: DiscoveryRow[] = joined.map((r) => ({
    wallet_address: r.wallet_address,
    display_name: r.display_name,
    strategy_type: r.strategy_type,
    profit_score: r.profit_score,
    replicability_score: r.replicability_score,
    total_score: r.total_score,
    risk_flags: r.risk_flags,
    last_seen_ts: r.last_seen_ts,
    last_active_at: r.last_active_at,
    lifecycle_state: r.lifecycle_state,
    locked: r.locked,
    cooldown_count: r.cooldown_count,
    cooldown_until: r.cooldown_until,
    first_discovered_at: r.first_discovered_at,
    updated_at: r.updated_at,
    paper_state: r.paper_state,
    paper_started_at: r.paper_started_at,
    paper_trade_count: r.paper_trade_count,
    paper_copyable_pnl: r.paper_copyable_pnl,
    paper_max_drawdown: r.paper_max_drawdown,
    paper_score: r.paper_score,
  }));

  // Trial-ready count is the badge on the "可升 live" chip — computed from
  // the candidate_leaders.lifecycle_state column, NOT the current row set.
  // This means the badge is correct on EVERY chip (the count does not flip
  // when the operator is staring at e.g. the retired bucket).
  const [{ count: trialReadyCount } = { count: 0 }] = await db
    .select({ count: sql<number>`cast(count(*) as int)` })
    .from(candidateLeaders)
    .where(eq(candidateLeaders.lifecycle_state, "trial_ready"));

  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
        <div className="space-y-1">
          <h1 className="text-2xl font-bold tracking-tight text-foreground">
            {t("discovery.list.title")}
          </h1>
          <p className="max-w-[60ch] text-sm text-muted-foreground">
            {t("discovery.list.subtitle")}
          </p>
        </div>
        <Link
          href="/backtest/new"
          className="inline-flex h-9 items-center rounded-md bg-primary px-4 text-sm font-medium text-primary-foreground transition-colors duration-150 ease-out hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
        >
          {t("discovery.list.newBacktest")}
        </Link>
      </div>

      <DiscoveryTable
        data={rows}
        chip={chip}
        trialReadyCount={trialReadyCount}
      />
    </div>
  );
}
