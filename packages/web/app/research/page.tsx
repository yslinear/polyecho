/**
 * /research — the C1 forward paper-trading ladder.
 *
 * Server Component. Reads via Drizzle: `candidate_leaders` (its automated
 * `lifecycle_state` + `cooldown_until` / `cooldown_count` / `locked` ladder
 * columns) LEFT-joined with `paper_sessions` (state, started_at, trade_count,
 * copyable_pnl, max_drawdown, score). A candidate at lifecycle_state='paper'
 * has exactly one open paper_session; a left join keeps non-paper candidates
 * (discovered / candidate / cooldown / retired) visible too.
 *
 * Writes: NONE here. The only write is the operator promote-to-live action
 * in the ResearchTable client island via the `promoteToLiveFollow` Server
 * Action — gated by the danger passphrase, and creating a PAUSED
 * follow_relations row (roadmap C1 "Money-safety boundary"). The ladder
 * state machine (packages/paper) NEVER inserts a follow; `trial_ready` is
 * advisory only.
 *
 * Scope filter: a candidate that has never entered the ladder
 * (lifecycle_state='discovered' AND no paper_session) carries no research
 * signal, so it is excluded — the page is the *evaluated* pool. Anything at
 * candidate/paper/trial_ready/cooldown/retired, OR anything with a
 * paper_session, is shown.
 */

import { desc, eq, ne } from "drizzle-orm";

import { db } from "@/lib/db";
import { candidateLeaders, paperSessions } from "@/lib/db/schema";
import { t } from "@/lib/i18n";
import { ResearchTable, type ResearchRow } from "./research-table";

export const dynamic = "force-dynamic";

export default async function ResearchPage() {
  // candidate_leaders LEFT JOIN paper_sessions on leader_address. A leader
  // at lifecycle_state='paper' has one open session; the left join keeps
  // ladder rows that have no session (candidate / cooldown / retired).
  const joined = await db
    .select({
      leader_address: candidateLeaders.wallet_address,
      display_name: candidateLeaders.display_name,
      lifecycle_state: candidateLeaders.lifecycle_state,
      locked: candidateLeaders.locked,
      cooldown_count: candidateLeaders.cooldown_count,
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
      eq(paperSessions.leader_address, candidateLeaders.wallet_address)
    )
    // The page is the *evaluated* pool — drop never-evaluated 'discovered'
    // leaders. A discovered leader that DID open a paper_session is kept
    // (the worker can re-set the state); the left-joined session columns
    // surface it. Ordering: newest cooldown / activity first.
    .where(ne(candidateLeaders.lifecycle_state, "discovered"))
    .orderBy(desc(candidateLeaders.updated_at));

  const rows: ResearchRow[] = joined.map((r) => ({
    leader_address: r.leader_address,
    display_name: r.display_name,
    lifecycle_state: r.lifecycle_state,
    locked: r.locked,
    cooldown_count: r.cooldown_count,
    paper_state: r.paper_state,
    paper_started_at: r.paper_started_at,
    paper_trade_count: r.paper_trade_count,
    paper_copyable_pnl: r.paper_copyable_pnl,
    paper_max_drawdown: r.paper_max_drawdown,
    paper_score: r.paper_score,
  }));

  const readyCount = rows.filter(
    (r) => r.lifecycle_state === "trial_ready"
  ).length;

  return (
    <div className="space-y-8">
      <header className="space-y-1.5">
        <h1 className="text-2xl font-bold tracking-tight">
          {t("research.title")}
        </h1>
        <p className="text-sm text-muted-foreground">
          {rows.length === 0
            ? t("research.subtitleEmpty")
            : t("research.subtitleCount", {
                count: rows.length,
                ready: readyCount,
              })}
        </p>
      </header>

      {/* The money-safety boundary, stated up front — a calm dashed-border
          advisory, NOT a hero metric (DESIGN.md). */}
      <p className="rounded-md border border-dashed bg-muted/40 p-3 text-xs text-muted-foreground">
        {t("research.boundary")}
      </p>

      {rows.length === 0 ? (
        <div className="rounded-lg border border-dashed py-16 text-center">
          <p className="text-sm font-medium text-foreground">
            {t("research.empty.title")}
          </p>
          <p className="mt-1.5 text-sm text-muted-foreground">
            {t("research.empty.hint")}
          </p>
        </div>
      ) : (
        <ResearchTable rows={rows} />
      )}
    </div>
  );
}
