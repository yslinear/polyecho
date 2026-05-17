/**
 * /backtest/[id] — Backtest result viewer (Phase 2)
 *
 * Server Component: reads backtest_jobs row via Drizzle.
 * Shows status/progress; when result is present renders PnL chart,
 * stats table, 3-scenario side-by-side comparison.
 *
 * Refresh strategy: Next.js `revalidate = 10` while status is pending/running.
 */

import { notFound } from "next/navigation";
import { eq } from "drizzle-orm";
import Link from "next/link";

import { db } from "@/lib/db";
import { backtestJobs } from "@/lib/db/schema";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Separator } from "@/components/ui/separator";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { BacktestPnlChart } from "./backtest-pnl-chart";

export const revalidate = 10; // re-fetch every 10s (SSR cache)

type BacktestJob = typeof backtestJobs.$inferSelect;

// ---------------------------------------------------------------------------
// Status badge
// ---------------------------------------------------------------------------
const STATUS_VARIANT: Record<
  string,
  "default" | "secondary" | "outline" | "destructive"
> = {
  queued: "secondary",
  running: "outline",
  completed: "default",
  failed: "destructive",
};

// ---------------------------------------------------------------------------
// Types for result JSONB (PRD §5.4 outputs)
// ---------------------------------------------------------------------------
interface ScenarioStats {
  total_return_pct: number;
  annualized_return_pct: number;
  sharpe: number;
  max_drawdown_pct: number;
  win_rate_pct: number;
  avg_hold_hours: number;
  trades_total: number;
  trades_skipped: number;
}

interface DayPoint {
  date: string;
  pnl_usd: number;
}

interface ScenarioResult {
  label: "optimistic" | "neutral" | "pessimistic";
  slippage_bps: number;
  delay_seconds: number;
  stats: ScenarioStats;
  pnl_curve: DayPoint[];
}

interface BacktestResult {
  scenarios?: ScenarioResult[];
  /** Single-scenario run */
  stats?: ScenarioStats;
  pnl_curve?: DayPoint[];
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function pct(v: number) {
  return `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`;
}
function num2(v: number) {
  return v.toFixed(2);
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------
interface PageProps {
  params: Promise<{ id: string }>;
}

export default async function BacktestResultPage({ params }: PageProps) {
  const { id } = await params;

  const [job]: BacktestJob[] = await db
    .select()
    .from(backtestJobs)
    .where(eq(backtestJobs.id, id))
    .limit(1);

  if (!job) {
    notFound();
  }

  const result = job.result as BacktestResult | null;
  const isTerminal = job.status === "completed" || job.status === "failed";
  const progress = Number(job.progress ?? 0);

  return (
    <div className="space-y-6">
      {/* Back */}
      <Link
        href="/backtest/new"
        className="text-sm text-muted-foreground hover:text-foreground underline underline-offset-2"
      >
        ← New Backtest
      </Link>

      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">Backtest Result</h1>
          <p className="font-mono text-sm text-muted-foreground mt-0.5 break-all">
            Job: {id}
          </p>
          {job.leader_addresses.length > 0 && (
            <p className="text-sm text-muted-foreground mt-0.5">
              Leader:{" "}
              <Link
                href={`/discovery/${job.leader_addresses[0]}`}
                className="font-mono underline underline-offset-2 hover:text-primary"
              >
                {job.leader_addresses[0].slice(0, 8)}…
              </Link>
            </p>
          )}
        </div>
        <Badge
          variant={STATUS_VARIANT[job.status] ?? "secondary"}
          className="capitalize text-sm px-3 py-1"
        >
          {job.status}
        </Badge>
      </div>

      {/* Job metadata */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle>Job Parameters</CardTitle>
        </CardHeader>
        <CardContent>
          <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-4">
            <div>
              <dt className="text-xs text-muted-foreground">Period</dt>
              <dd className="font-mono text-xs mt-0.5">
                {job.start_date} → {job.end_date}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">Capital</dt>
              <dd className="mt-0.5">
                ${Number(job.initial_capital_usd ?? 0).toLocaleString()}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">Slippage</dt>
              <dd className="mt-0.5">{job.slippage_bps} bps</dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">Delay</dt>
              <dd className="mt-0.5">{job.execution_delay_seconds} s</dd>
            </div>
          </dl>
        </CardContent>
      </Card>

      {/* In-progress state */}
      {!isTerminal && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2">
              Running…
              <span className="text-sm font-normal text-muted-foreground">
                {progress.toFixed(1)}%
              </span>
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="h-2 w-full rounded-full bg-muted overflow-hidden">
              <div
                className="h-2 rounded-full bg-primary transition-all"
                style={{ width: `${progress}%` }}
              />
            </div>
            <p className="text-xs text-muted-foreground">
              Page auto-refreshes every 10 s. You can also reload manually.
            </p>
            <div className="space-y-2">
              <Skeleton className="h-4 w-3/4" />
              <Skeleton className="h-4 w-1/2" />
              <Skeleton className="h-[200px] w-full" />
            </div>
          </CardContent>
        </Card>
      )}

      {/* Failed state */}
      {job.status === "failed" && (
        <Card className="border-destructive">
          <CardHeader className="pb-3">
            <CardTitle className="text-destructive">Backtest Failed</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground">
              The backtest engine returned an error. Check the Python worker
              logs:{" "}
              <code className="text-xs bg-muted px-1 rounded">
                polyecho logs backtest
              </code>
              .
            </p>
            {result && (
              <pre className="mt-3 rounded bg-muted p-3 text-xs overflow-x-auto">
                {JSON.stringify(result, null, 2)}
              </pre>
            )}
          </CardContent>
        </Card>
      )}

      {/* Completed: triple scenario */}
      {job.status === "completed" && result?.scenarios && (
        <>
          <Separator />
          {/* 3-scenario side-by-side */}
          <div>
            <h2 className="text-lg font-semibold mb-3">
              3-Scenario Comparison
            </h2>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
              {result.scenarios.map((s) => (
                <ScenarioCard key={s.label} scenario={s} />
              ))}
            </div>
          </div>

          {/* PnL curve — all 3 scenarios */}
          <Card>
            <CardHeader className="pb-3">
              <CardTitle>PnL Curves</CardTitle>
            </CardHeader>
            <CardContent>
              <BacktestPnlChart scenarios={result.scenarios} />
            </CardContent>
          </Card>
        </>
      )}

      {/* Completed: single scenario */}
      {job.status === "completed" && result?.stats && !result.scenarios && (
        <>
          <Separator />
          <Card>
            <CardHeader className="pb-3">
              <CardTitle>Stats</CardTitle>
            </CardHeader>
            <CardContent>
              <StatsTable stats={result.stats} />
            </CardContent>
          </Card>

          {result.pnl_curve && result.pnl_curve.length > 0 && (
            <Card>
              <CardHeader className="pb-3">
                <CardTitle>PnL Curve</CardTitle>
              </CardHeader>
              <CardContent>
                <BacktestPnlChart
                  scenarios={[
                    {
                      label: "neutral",
                      slippage_bps: job.slippage_bps ?? 150,
                      delay_seconds: job.execution_delay_seconds ?? 30,
                      stats: result.stats,
                      pnl_curve: result.pnl_curve,
                    },
                  ]}
                />
              </CardContent>
            </Card>
          )}
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------
function ScenarioCard({ scenario }: { scenario: ScenarioResult }) {
  const { stats, label, slippage_bps, delay_seconds } = scenario;
  const returnPositive = stats.total_return_pct >= 0;

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center justify-between text-base capitalize">
          {label}
          <span className="text-xs font-normal text-muted-foreground">
            {slippage_bps} bps / {delay_seconds} s
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div
          className={`text-2xl font-bold mb-3 ${returnPositive ? "text-green-600 dark:text-green-400" : "text-red-600 dark:text-red-400"}`}
        >
          {pct(stats.total_return_pct)}
        </div>
        <StatsTable stats={stats} compact />
      </CardContent>
    </Card>
  );
}

function StatsTable({
  stats,
  compact = false,
}: {
  stats: ScenarioStats;
  compact?: boolean;
}) {
  const rows: [string, string][] = [
    ["Total Return", pct(stats.total_return_pct)],
    ["Annualized", pct(stats.annualized_return_pct)],
    ["Sharpe", num2(stats.sharpe)],
    ["Max Drawdown", pct(stats.max_drawdown_pct)],
    ["Win Rate", pct(stats.win_rate_pct)],
    ["Avg Hold", `${num2(stats.avg_hold_hours)} h`],
    ["Trades", String(stats.trades_total)],
    ["Skipped", String(stats.trades_skipped)],
  ];

  if (compact) {
    return (
      <dl className="grid grid-cols-2 gap-x-2 gap-y-1 text-xs">
        {rows.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-muted-foreground">{k}</dt>
            <dd className="font-mono text-right">{v}</dd>
          </div>
        ))}
      </dl>
    );
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Metric</TableHead>
          <TableHead className="text-right">Value</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map(([k, v]) => (
          <TableRow key={k}>
            <TableCell className="text-sm">{k}</TableCell>
            <TableCell className="font-mono text-sm text-right">{v}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
