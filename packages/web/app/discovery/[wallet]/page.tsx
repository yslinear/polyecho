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
import { Pnl } from "@/components/pnl";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Separator } from "@/components/ui/separator";
import { formatDateTime } from "@/lib/format";
import { t } from "@/lib/i18n";
import { LeaderScoreChart } from "./leader-score-chart";

// Reads the database on every request — never prerender at build time
// (no DB is available during `next build` / CI).
export const dynamic = "force-dynamic";

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

// Canonical UTC-stable formatter (lib/format.ts) — identical output on the
// server and the client, so no React hydration drift on timestamps.
function fmtDate(d: Date | null | undefined): string {
  if (!d) return t("discovery.detail.notRecorded");
  return formatDateTime(d);
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

// A stat key whose value should read with gain/loss semantics.
function isPnlKey(key: string): boolean {
  return /pnl|profit|return|roi|net/i.test(key);
}

function renderStatValue(key: string, v: unknown) {
  if (v === null || v === undefined || v === "") {
    return (
      <span className="text-muted-foreground">
        {t("discovery.detail.notSet")}
      </span>
    );
  }
  if (Array.isArray(v)) {
    return (
      <span className="tabular-nums">{(v as unknown[]).join(", ")}</span>
    );
  }
  const num = typeof v === "number" ? v : Number(v);
  const numeric = typeof v !== "boolean" && Number.isFinite(num);
  if (numeric && isPnlKey(key)) {
    return <Pnl value={num} className="text-sm" />;
  }
  if (numeric) {
    return <span className="tabular-nums">{String(v)}</span>;
  }
  return <span>{String(v)}</span>;
}

// ---------------------------------------------------------------------------
// Stats block — a single labelled section (NOT a nested card; DESIGN bans
// identical card grids and nested cards). Rendered inside one outer Card.
// ---------------------------------------------------------------------------
function StatsBlock({ title, data }: { title: string; data: unknown }) {
  const entries =
    data === null || data === undefined
      ? []
      : Object.entries(data as Record<string, unknown>);

  return (
    <section className="space-y-2">
      <h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        {title}
      </h3>
      {entries.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          {t("discovery.detail.noDataAvailable")}
        </p>
      ) : (
        <dl className="grid grid-cols-2 gap-x-6 gap-y-1.5 text-xs">
          {entries.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="truncate text-muted-foreground">{k}</dt>
              <dd className="truncate text-right font-medium tabular-nums">
                {renderStatValue(k, v)}
              </dd>
            </div>
          ))}
        </dl>
      )}
    </section>
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
    <div className="space-y-8">
      {/* Back */}
      <Link
        href="/discovery"
        className="inline-flex items-center gap-1 rounded-sm text-sm text-muted-foreground underline-offset-4 transition-colors duration-150 ease-out hover:text-foreground hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <span aria-hidden="true">&larr;</span> {t("discovery.detail.back")}
      </Link>

      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
        <div className="space-y-1">
          <h1 className="text-2xl font-bold tracking-tight text-foreground break-all">
            {leader.display_name ?? shortWallet(leader.wallet_address)}
          </h1>
          <p className="font-mono text-sm text-muted-foreground break-all">
            {leader.wallet_address}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Badge
            variant={STATUS_VARIANT[leader.user_status] ?? "secondary"}
            className="px-3 py-1 text-sm capitalize"
          >
            {leader.user_status}
          </Badge>
          <Link
            href={`/backtest/new?leader=${encodeURIComponent(leader.wallet_address)}`}
            className="inline-flex h-9 items-center rounded-md border border-input px-4 text-sm font-medium transition-colors duration-150 ease-out hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            {t("discovery.detail.runBacktest")}
          </Link>
        </div>
      </div>

      {/* Score pills */}
      <div className="flex flex-wrap gap-3">
        <ScoreBadge
          label={t("discovery.detail.score.profit")}
          value={leader.profit_score}
        />
        <ScoreBadge
          label={t("discovery.detail.score.replicability")}
          value={leader.replicability_score}
        />
        <ScoreBadge
          label={t("discovery.detail.score.total")}
          value={leader.total_score}
        />
      </div>

      <Separator />

      {/* Tabs */}
      <Tabs defaultValue="overview">
        <TabsList>
          <TabsTrigger value="overview">
            {t("discovery.detail.tabs.overview")}
          </TabsTrigger>
          <TabsTrigger value="stats">
            {t("discovery.detail.tabs.stats")}
          </TabsTrigger>
          <TabsTrigger value="history">
            {t("discovery.detail.tabs.history")}{" "}
            <span className="ml-1 text-xs tabular-nums text-muted-foreground">
              ({chartData.length})
            </span>
          </TabsTrigger>
        </TabsList>

        {/* ---- Overview tab ---- */}
        <TabsContent value="overview" className="mt-5 space-y-5">
          {/* Profile */}
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base font-semibold">
                {t("discovery.detail.profile.title")}
              </CardTitle>
            </CardHeader>
            <CardContent>
              <dl className="grid grid-cols-1 gap-x-6 gap-y-4 text-sm sm:grid-cols-2">
                <div>
                  <dt className="text-xs text-muted-foreground">
                    {t("discovery.detail.profile.sources")}
                  </dt>
                  <dd className="mt-1 flex flex-wrap gap-1">
                    {(leader.sources ?? []).length > 0 ? (
                      (leader.sources ?? []).map((s) => (
                        <Badge key={s} variant="outline" className="text-xs">
                          {s}
                        </Badge>
                      ))
                    ) : (
                      <span className="text-muted-foreground">
                        {t("discovery.detail.notSet")}
                      </span>
                    )}
                  </dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">
                    {t("discovery.detail.profile.strategy")}
                  </dt>
                  <dd className="mt-1">
                    {leader.strategy_type ? (
                      leader.strategy_type
                    ) : (
                      <span className="text-muted-foreground">
                        {t("discovery.detail.notSet")}
                      </span>
                    )}
                  </dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">
                    {t("discovery.detail.profile.firstDiscovered")}
                  </dt>
                  <dd className="mt-1 tabular-nums">
                    {fmtDate(leader.first_discovered_at)}
                  </dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">
                    {t("discovery.detail.profile.lastActive")}
                  </dt>
                  <dd className="mt-1 tabular-nums">
                    {fmtDate(leader.last_active_at)}
                  </dd>
                </div>
                {leader.user_notes && (
                  <div className="sm:col-span-2">
                    <dt className="text-xs text-muted-foreground">
                      {t("discovery.detail.profile.notes")}
                    </dt>
                    <dd className="mt-1 whitespace-pre-wrap text-sm">
                      {leader.user_notes}
                    </dd>
                  </div>
                )}
              </dl>
            </CardContent>
          </Card>

          {/* Specialization + Risk — deliberately different shape, not a
              mirrored card grid (DESIGN: no identical card grids). */}
          <Card>
            <CardContent className="grid grid-cols-1 gap-x-8 gap-y-5 pt-6 sm:grid-cols-[2fr_1fr]">
              <div className="space-y-2">
                <h3 className="text-sm font-semibold">
                  {t("discovery.detail.specialization.title")}
                </h3>
                <div className="flex flex-wrap gap-1.5">
                  {(leader.specialization ?? []).length > 0 ? (
                    (leader.specialization ?? []).map((s) => (
                      <Badge key={s} variant="secondary" className="text-xs">
                        {s}
                      </Badge>
                    ))
                  ) : (
                    <span className="text-xs text-muted-foreground">
                      {t("discovery.detail.specialization.none")}
                    </span>
                  )}
                </div>
              </div>
              <div className="space-y-2 sm:border-l sm:border-border sm:pl-8">
                <h3 className="text-sm font-semibold">
                  {t("discovery.detail.riskFlags.title")}
                </h3>
                <div className="flex flex-wrap gap-1.5">
                  {(leader.risk_flags ?? []).length > 0 ? (
                    (leader.risk_flags ?? []).map((f) => (
                      <Badge
                        key={f}
                        variant="destructive"
                        className="text-xs"
                      >
                        {f}
                      </Badge>
                    ))
                  ) : (
                    <span className="text-xs text-muted-foreground">
                      {t("discovery.detail.riskFlags.none")}
                    </span>
                  )}
                </div>
              </div>
            </CardContent>
          </Card>

          {/* Score formula */}
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-base font-semibold">
                {t("discovery.detail.breakdown.title")}
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <p className="rounded-md bg-muted px-3 py-2 font-mono text-xs text-muted-foreground">
                {/* Formula over DB column names (proper nouns); not
                    translatable copy. */}
                {"total_score = profit_score × (replicability_score / 100)"}
              </p>
              <div className="grid grid-cols-1 gap-x-8 gap-y-4 text-xs sm:grid-cols-2">
                <div>
                  <p className="mb-1.5 font-semibold">
                    {t("discovery.detail.breakdown.profitHeading")}
                  </p>
                  <ul className="space-y-1 text-muted-foreground">
                    <li>{t("discovery.detail.breakdown.profitFactors.pnl")}</li>
                    <li>
                      {t("discovery.detail.breakdown.profitFactors.winRate")}
                    </li>
                    <li>
                      {t("discovery.detail.breakdown.profitFactors.sharpe")}
                    </li>
                    <li>
                      {t(
                        "discovery.detail.breakdown.profitFactors.maxDrawdown",
                      )}
                    </li>
                    <li>
                      {t(
                        "discovery.detail.breakdown.profitFactors.consistency",
                      )}
                    </li>
                    <li>
                      {t(
                        "discovery.detail.breakdown.profitFactors.specialization",
                      )}
                    </li>
                  </ul>
                </div>
                <div>
                  <p className="mb-1.5 font-semibold">
                    {t("discovery.detail.breakdown.replicabilityHeading")}
                  </p>
                  <ul className="space-y-1 text-muted-foreground">
                    <li>
                      {t(
                        "discovery.detail.breakdown.replicabilityFactors.frequencyMatch",
                      )}
                    </li>
                    <li>
                      {t(
                        "discovery.detail.breakdown.replicabilityFactors.capitalMatch",
                      )}
                    </li>
                    <li>
                      {t(
                        "discovery.detail.breakdown.replicabilityFactors.burstiness",
                      )}
                    </li>
                    <li>
                      {t(
                        "discovery.detail.breakdown.replicabilityFactors.activeHoursOverlap",
                      )}
                    </li>
                  </ul>
                </div>
              </div>
            </CardContent>
          </Card>
        </TabsContent>

        {/* ---- Stats tab — one Card, segmented sections (no card grid) ---- */}
        <TabsContent value="stats" className="mt-5">
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base font-semibold">
                {t("discovery.detail.stats.title")}
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-6">
              <div className="grid grid-cols-1 gap-x-10 gap-y-6 md:grid-cols-3">
                <StatsBlock
                  title={t("discovery.detail.stats.window7d")}
                  data={leader.stats_7d}
                />
                <StatsBlock
                  title={t("discovery.detail.stats.window30d")}
                  data={leader.stats_30d}
                />
                <StatsBlock
                  title={t("discovery.detail.stats.window90d")}
                  data={leader.stats_90d}
                />
              </div>
              <Separator />
              <StatsBlock
                title={t("discovery.detail.stats.frequencyStrategy")}
                data={leader.frequency_stats}
              />
            </CardContent>
          </Card>
        </TabsContent>

        {/* ---- Score History tab ---- */}
        <TabsContent value="history" className="mt-5">
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base font-semibold">
                {t("discovery.detail.history.title")}
              </CardTitle>
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
