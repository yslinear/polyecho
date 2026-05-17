/**
 * /discovery/[wallet] — Leader detail page (Phase 2)
 *
 * Server Component: fetches candidate_leaders + score_history.
 * Uses shadcn Tabs (Overview / Stats / Score History), shadcn Chart for scores.
 */

import { notFound } from "next/navigation";
import { eq, asc } from "drizzle-orm";
import Link from "next/link";

import { db } from "@/lib/db";
import { candidateLeaders, candidateScoreHistory } from "@/lib/db/schema";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { ScoreBadge } from "@/components/score-badge";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Separator } from "@/components/ui/separator";
import { LeaderScoreChart } from "./leader-score-chart";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------
type CandidateLeader = typeof candidateLeaders.$inferSelect;
type ScoreHistory = typeof candidateScoreHistory.$inferSelect;

export interface ScorePoint {
  date: string;
  total_score: number | null;
}

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

const STATUS_VARIANT: Record<
  string,
  "default" | "secondary" | "outline" | "destructive"
> = {
  new: "secondary",
  watching: "outline",
  following: "default",
  rejected: "destructive",
};

// ---------------------------------------------------------------------------
// Stats card
// ---------------------------------------------------------------------------
function JsonStatsCard({ title, data }: { title: string; data: unknown }) {
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

  const [leader]: CandidateLeader[] = await db
    .select()
    .from(candidateLeaders)
    .where(eq(candidateLeaders.wallet_address, wallet))
    .limit(1);

  if (!leader) {
    notFound();
  }

  const history: ScoreHistory[] = await db
    .select()
    .from(candidateScoreHistory)
    .where(eq(candidateScoreHistory.wallet_address, wallet))
    .orderBy(asc(candidateScoreHistory.date));

  const chartData: ScorePoint[] = history.map((h) => ({
    date: h.date,
    total_score: h.total_score !== null ? Number(h.total_score) : null,
  }));

  return (
    <div className="space-y-6">
      {/* Back */}
      <Link
        href="/discovery"
        className="text-sm text-muted-foreground hover:text-foreground underline underline-offset-2"
      >
        ← Back to Discovery
      </Link>

      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold break-all">
            {leader.display_name ?? shortWallet(leader.wallet_address)}
          </h1>
          <p className="font-mono text-sm text-muted-foreground mt-0.5 break-all">
            {leader.wallet_address}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Badge
            variant={STATUS_VARIANT[leader.user_status] ?? "secondary"}
            className="capitalize text-sm px-3 py-1"
          >
            {leader.user_status}
          </Badge>
          <Link
            href={`/backtest/new?leader=${encodeURIComponent(leader.wallet_address)}`}
            className="rounded-md border border-input px-3 py-1.5 text-xs font-medium hover:bg-accent transition-colors"
          >
            Run Backtest
          </Link>
        </div>
      </div>

      {/* Score pills */}
      <div className="flex flex-wrap gap-3">
        <ScoreBadge label="Profit" value={leader.profit_score} />
        <ScoreBadge label="Replicability" value={leader.replicability_score} />
        <ScoreBadge label="Total" value={leader.total_score} />
      </div>

      <Separator />

      {/* Tabs */}
      <Tabs defaultValue="overview">
        <TabsList>
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="stats">Stats</TabsTrigger>
          <TabsTrigger value="history">
            Score History{" "}
            <span className="ml-1 text-muted-foreground text-xs">
              ({chartData.length})
            </span>
          </TabsTrigger>
        </TabsList>

        {/* ---- Overview tab ---- */}
        <TabsContent value="overview" className="space-y-4 mt-4">
          {/* Profile */}
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
                  <dt className="text-xs text-muted-foreground">Strategy</dt>
                  <dd className="mt-0.5">{leader.strategy_type ?? "—"}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">
                    First Discovered
                  </dt>
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

          {/* Specialization + Risk */}
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

          {/* Score formula */}
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-base">Score Breakdown</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              <div className="rounded-md bg-muted/50 px-3 py-2 text-xs text-muted-foreground font-mono">
                total_score = profit_score × (replicability_score / 100)
              </div>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 text-xs">
                <div>
                  <p className="font-semibold mb-1">profit_score (0–100)</p>
                  <ul className="space-y-0.5 text-muted-foreground">
                    <li>PnL — 25%</li>
                    <li>Win rate — 20%</li>
                    <li>Sharpe ratio — 20%</li>
                    <li>Max drawdown — 15%</li>
                    <li>Consistency — 10%</li>
                    <li>Specialization — 10%</li>
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
        </TabsContent>

        {/* ---- Stats tab ---- */}
        <TabsContent value="stats" className="space-y-4 mt-4">
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
            <JsonStatsCard title="7-day Stats" data={leader.stats_7d} />
            <JsonStatsCard title="30-day Stats" data={leader.stats_30d} />
            <JsonStatsCard title="90-day Stats" data={leader.stats_90d} />
          </div>
          <JsonStatsCard
            title="Frequency & Strategy Stats (§5.3)"
            data={leader.frequency_stats}
          />
        </TabsContent>

        {/* ---- Score History tab ---- */}
        <TabsContent value="history" className="mt-4">
          <Card>
            <CardHeader className="pb-3">
              <CardTitle>Score History</CardTitle>
            </CardHeader>
            <CardContent>
              <LeaderScoreChart data={chartData} />
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}
