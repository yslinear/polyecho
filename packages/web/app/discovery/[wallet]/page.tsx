/**
 * /discovery/[wallet] — Leader detail page (Phase 1)
 *
 * Server Component: fetches candidate_leaders + candidate_score_history.
 * DataChart is a client island — receives pre-fetched data as props.
 */

import { notFound } from "next/navigation";
import { eq, asc } from "drizzle-orm";

import { db } from "@/lib/db";
import { candidateLeaders, candidateScoreHistory } from "@/lib/db/schema";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { ScoreBadge } from "@/components/score-badge";
import { DataChart, type ScorePoint } from "@/components/data-chart";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------
type CandidateLeader = typeof candidateLeaders.$inferSelect;
type ScoreHistory = typeof candidateScoreHistory.$inferSelect;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function shortWallet(addr: string): string {
  return `${addr.slice(0, 6)}…${addr.slice(-4)}`;
}

function fmt(v: string | null | undefined): string {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  if (!isNaN(n)) return n.toFixed(2);
  return v;
}

function fmtDate(d: Date | null | undefined): string {
  if (!d) return "—";
  return new Date(d).toLocaleString("en-CA", { timeZoneName: "short" });
}

// ---------------------------------------------------------------------------
// Stats card: renders a JSONB object as a readable key→value table
// ---------------------------------------------------------------------------
function JsonStatsCard({
  title,
  data,
}: {
  title: string;
  data: unknown;
}) {
  if (data === null || data === undefined) {
    return (
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium text-muted-foreground">
            {title}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-xs text-muted-foreground">No data available.</p>
        </CardContent>
      </Card>
    );
  }

  const entries = Object.entries(data as Record<string, unknown>);

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium">{title}</CardTitle>
      </CardHeader>
      <CardContent>
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
          {entries.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="text-muted-foreground truncate">{k}</dt>
              <dd className="font-mono text-right truncate">
                {Array.isArray(v)
                  ? (v as unknown[]).join(", ")
                  : v === null || v === undefined
                    ? "—"
                    : String(v)}
              </dd>
            </div>
          ))}
        </dl>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------
interface PageProps {
  params: Promise<{ wallet: string }>;
}

export default async function LeaderDetailPage({ params }: PageProps) {
  const { wallet } = await params;

  // Fetch leader — TODO(verify): Drizzle 0.39 eq() with primaryKey text column
  const [leader]: CandidateLeader[] = await db
    .select()
    .from(candidateLeaders)
    .where(eq(candidateLeaders.wallet_address, wallet))
    .limit(1);

  if (!leader) {
    notFound();
  }

  // Fetch score history ordered by date asc
  const history: ScoreHistory[] = await db
    .select()
    .from(candidateScoreHistory)
    .where(eq(candidateScoreHistory.wallet_address, wallet))
    .orderBy(asc(candidateScoreHistory.date));

  // Map to chart-friendly format
  const chartData: ScorePoint[] = history.map((h) => ({
    date: h.date,
    total_score: h.total_score !== null ? Number(h.total_score) : null,
  }));

  const statusVariant: Record<
    string,
    "default" | "secondary" | "outline" | "destructive"
  > = {
    new: "secondary",
    watching: "outline",
    following: "default",
    rejected: "destructive",
  };

  return (
    <div className="space-y-6">
      {/* Back link */}
      <div>
        <a
          href="/discovery"
          className="text-sm text-muted-foreground hover:text-foreground underline underline-offset-2"
        >
          ← Back to Discovery
        </a>
      </div>

      {/* ------------------------------------------------------------------ */}
      {/* Header                                                               */}
      {/* ------------------------------------------------------------------ */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold break-all">
            {leader.display_name ?? shortWallet(leader.wallet_address)}
          </h1>
          <p className="font-mono text-sm text-muted-foreground mt-0.5 break-all">
            {leader.wallet_address}
          </p>
        </div>
        <Badge
          variant={statusVariant[leader.user_status] ?? "secondary"}
          className="capitalize text-sm px-3 py-1"
        >
          {leader.user_status}
        </Badge>
      </div>

      {/* ------------------------------------------------------------------ */}
      {/* Profile card                                                         */}
      {/* ------------------------------------------------------------------ */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle>Profile</CardTitle>
        </CardHeader>
        <CardContent>
          <dl className="grid grid-cols-1 gap-3 sm:grid-cols-2 text-sm">
            <div>
              <dt className="text-xs text-muted-foreground">Sources</dt>
              <dd className="mt-0.5 flex flex-wrap gap-1">
                {(leader.sources ?? []).length > 0 ? (
                  (leader.sources ?? []).map((s) => (
                    <Badge key={s} variant="outline" className="text-xs">
                      {s}
                    </Badge>
                  ))
                ) : (
                  <span className="text-muted-foreground">—</span>
                )}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">Strategy Type</dt>
              <dd className="mt-0.5">{leader.strategy_type ?? "—"}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">First Discovered</dt>
              <dd className="mt-0.5">{fmtDate(leader.first_discovered_at)}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">Last Active</dt>
              <dd className="mt-0.5">{fmtDate(leader.last_active_at)}</dd>
            </div>
            {leader.user_notes && (
              <div className="sm:col-span-2">
                <dt className="text-xs text-muted-foreground">Notes</dt>
                <dd className="mt-0.5 whitespace-pre-wrap text-sm">
                  {leader.user_notes}
                </dd>
              </div>
            )}
          </dl>
        </CardContent>
      </Card>

      {/* ------------------------------------------------------------------ */}
      {/* Scores panel (PRD §5.2 breakdown labels)                             */}
      {/* ------------------------------------------------------------------ */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle>Scores</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {/* Score pills row */}
          <div className="flex flex-wrap gap-3">
            <ScoreBadge label="Profit" value={leader.profit_score} />
            <ScoreBadge
              label="Replicability"
              value={leader.replicability_score}
            />
            <ScoreBadge label="Total" value={leader.total_score} />
          </div>

          {/* Formula reminder (PRD §5.2) */}
          <div className="rounded-md bg-muted/50 px-3 py-2 text-xs text-muted-foreground font-mono">
            total_score = profit_score × (replicability_score / 100)
          </div>

          {/* Breakdown labels */}
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 text-xs">
            <div>
              <p className="font-semibold mb-1">profit_score (0–100)</p>
              <ul className="space-y-0.5 text-muted-foreground">
                <li>PnL &mdash; 25%</li>
                <li>Win rate &mdash; 20%</li>
                <li>Sharpe ratio &mdash; 20%</li>
                <li>Max drawdown &mdash; 15%</li>
                <li>Consistency &mdash; 10%</li>
                <li>Specialization &mdash; 10%</li>
              </ul>
            </div>
            <div>
              <p className="font-semibold mb-1">replicability_score (0–100)</p>
              <ul className="space-y-0.5 text-muted-foreground">
                <li>Frequency match</li>
                <li>Capital match</li>
                <li>Burstiness (low = better)</li>
                <li>Active-hours overlap</li>
              </ul>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* ------------------------------------------------------------------ */}
      {/* Specialization + Risk flags                                          */}
      {/* ------------------------------------------------------------------ */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">Specialization</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex flex-wrap gap-1.5">
              {(leader.specialization ?? []).length > 0 ? (
                (leader.specialization ?? []).map((s) => (
                  <Badge key={s} variant="secondary" className="text-xs">
                    {s}
                  </Badge>
                ))
              ) : (
                <span className="text-xs text-muted-foreground">
                  No specialization recorded.
                </span>
              )}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">Risk Flags</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex flex-wrap gap-1.5">
              {(leader.risk_flags ?? []).length > 0 ? (
                (leader.risk_flags ?? []).map((f) => (
                  <Badge key={f} variant="destructive" className="text-xs">
                    {f}
                  </Badge>
                ))
              ) : (
                <span className="text-xs text-muted-foreground">
                  No risk flags.
                </span>
              )}
            </div>
          </CardContent>
        </Card>
      </div>

      {/* ------------------------------------------------------------------ */}
      {/* Stats panels (7d / 30d / 90d)                                        */}
      {/* ------------------------------------------------------------------ */}
      <div>
        <h2 className="text-lg font-semibold mb-3">Performance Stats</h2>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <JsonStatsCard title="7-day Stats" data={leader.stats_7d} />
          <JsonStatsCard title="30-day Stats" data={leader.stats_30d} />
          <JsonStatsCard title="90-day Stats" data={leader.stats_90d} />
        </div>
      </div>

      {/* ------------------------------------------------------------------ */}
      {/* Frequency stats (PRD §5.3 keys)                                      */}
      {/* ------------------------------------------------------------------ */}
      <JsonStatsCard
        title="Frequency & Strategy Stats (§5.3)"
        data={leader.frequency_stats}
      />

      {/* ------------------------------------------------------------------ */}
      {/* Score history chart — client island                                   */}
      {/* ------------------------------------------------------------------ */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle>
            Score History
            <span className="ml-2 text-sm font-normal text-muted-foreground">
              ({chartData.length} data points)
            </span>
          </CardTitle>
        </CardHeader>
        <CardContent>
          <DataChart data={chartData} />
        </CardContent>
      </Card>
    </div>
  );
}
