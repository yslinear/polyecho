/**
 * /backtest/[id] — Backtest result viewer (Phase 2)
 *
 * Server Component: reads backtest_jobs row via Drizzle.
 * Shows status/progress; when result is present renders PnL chart,
 * stats table, 3-scenario side-by-side comparison.
 *
 * Refresh strategy: Next.js `revalidate = 10` while status is pending/running.
 */

import type { ReactNode } from "react";
import { notFound } from "next/navigation";
import { eq } from "drizzle-orm";
import Link from "next/link";

import { db } from "@/lib/db";
import { cn } from "@/lib/utils";
import { formatUsd } from "@/lib/format";
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
// Status badge — text + variant (never colour-only; DESIGN.md A11y).
// `pending` is the value the worker polls on (db default), so it is a
// first-class state here, not an unknown fallback.
// ---------------------------------------------------------------------------
const STATUS_VARIANT: Record<
  string,
  "default" | "secondary" | "outline" | "destructive"
> = {
  pending: "secondary",
  queued: "secondary",
  running: "outline",
  completed: "default",
  failed: "destructive",
};

const STATUS_LABEL: Record<string, string> = {
  pending: "Queued",
  queued: "Queued",
  running: "Running",
  completed: "Completed",
  failed: "Failed",
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

/**
 * Signed percentage with the same legibility contract as <Pnl/>: the sign
 * and an arrow carry the meaning, the gain/loss tint is reinforcement only
 * (DESIGN.md). Used for return %s, where <Pnl/> (USD-only) does not apply.
 */
function ReturnPct({
  value,
  className,
}: {
  value: number;
  className?: string;
}) {
  const dir = value > 0 ? "up" : value < 0 ? "down" : "flat";
  const tone =
    dir === "up"
      ? "text-gain"
      : dir === "down"
        ? "text-loss"
        : "text-muted-foreground";
  const arrow = dir === "up" ? "↑" : dir === "down" ? "↓" : "→";
  return (
    <span className={cn("tabular-nums", tone, className)} data-pnl={dir}>
      <span aria-hidden="true" className="mr-0.5">
        {arrow}
      </span>
      {pct(value)}
    </span>
  );
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
  const isPending = job.status === "pending" || job.status === "queued";
  const isRunning = job.status === "running";
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
          className="px-3 py-1 text-sm"
        >
          {STATUS_LABEL[job.status] ?? job.status}
        </Badge>
      </div>

      {/* Job metadata */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle>Job Parameters</CardTitle>
        </CardHeader>
        <CardContent>
          <dl className="grid grid-cols-2 gap-x-6 gap-y-3 text-sm sm:grid-cols-4">
            <div>
              <dt className="text-xs text-muted-foreground">Period</dt>
              <dd className="mt-1 font-mono text-xs tabular-nums">
                {job.start_date} → {job.end_date}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">Capital</dt>
              <dd className="mt-1 tabular-nums">
                {formatUsd(job.initial_capital_usd)}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">Slippage</dt>
              <dd className="mt-1 tabular-nums">{job.slippage_bps} bps</dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">Delay</dt>
              <dd className="mt-1 tabular-nums">
                {job.execution_delay_seconds} s
              </dd>
            </div>
          </dl>
        </CardContent>
      </Card>

      {/* Queued state — the worker has not picked this job up yet. */}
      {isPending && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">Queued</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <p className="text-sm text-muted-foreground">
              Queued. A backtest worker will process this. This page
              refreshes every 10 seconds; you can also reload manually.
            </p>
            <div
              className="space-y-3"
              aria-hidden="true"
            >
              <Skeleton className="h-4 w-2/5" />
              <Skeleton className="h-4 w-1/4" />
              <Skeleton className="h-[220px] w-full" />
            </div>
          </CardContent>
        </Card>
      )}

      {/* Running state — worker is processing; show real progress. */}
      {isRunning && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center justify-between text-base">
              <span>Running</span>
              <span className="text-sm font-normal tabular-nums text-muted-foreground">
                {progress.toFixed(1)}%
              </span>
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div
              className="h-2 w-full overflow-hidden rounded-full bg-muted"
              role="progressbar"
              aria-valuenow={Math.round(progress)}
              aria-valuemin={0}
              aria-valuemax={100}
            >
              <div
                className="h-2 rounded-full bg-primary transition-all"
                style={{ width: `${progress}%` }}
              />
            </div>
            <p className="text-xs text-muted-foreground">
              This page refreshes every 10 seconds. You can also reload
              manually.
            </p>
            <div className="space-y-3" aria-hidden="true">
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
          <CardContent className="space-y-3">
            <p className="text-sm text-muted-foreground">
              The backtest engine returned an error. Check the Python
              worker logs with{" "}
              <code className="rounded bg-muted px-1 text-xs">
                polyecho logs backtest
              </code>
              , then queue the job again.
            </p>
            {result && (
              <pre className="overflow-x-auto rounded bg-muted p-3 text-xs">
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
          <div className="space-y-4">
            <div className="space-y-1">
              <h2 className="text-sm font-semibold text-foreground">
                Scenario comparison
              </h2>
              <p className="text-xs text-muted-foreground">
                Best, base, and worst case under different slippage and
                delay assumptions.
              </p>
            </div>
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

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center justify-between text-base capitalize">
          {label}
          <span className="text-xs font-normal tabular-nums text-muted-foreground">
            {slippage_bps} bps / {delay_seconds} s
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="mb-3 text-2xl font-semibold">
          <ReturnPct value={stats.total_return_pct} />
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
  // Return / drawdown carry gain/loss meaning -> render with the
  // sign+arrow+tint contract. Win Rate is a magnitude (not a PnL), so it
  // stays a plain tabular figure (color would be a false signal).
  const rows: { label: string; value: ReactNode }[] = [
    {
      label: "Total Return",
      value: <ReturnPct value={stats.total_return_pct} />,
    },
    {
      label: "Annualized",
      value: <ReturnPct value={stats.annualized_return_pct} />,
    },
    { label: "Sharpe", value: num2(stats.sharpe) },
    {
      label: "Max Drawdown",
      value: <ReturnPct value={stats.max_drawdown_pct} />,
    },
    { label: "Win Rate", value: `${num2(stats.win_rate_pct)}%` },
    { label: "Avg Hold", value: `${num2(stats.avg_hold_hours)} h` },
    { label: "Trades", value: String(stats.trades_total) },
    { label: "Skipped", value: String(stats.trades_skipped) },
  ];

  if (compact) {
    return (
      <dl className="grid grid-cols-2 gap-x-2 gap-y-1 text-xs">
        {rows.map(({ label, value }) => (
          <div key={label} className="contents">
            <dt className="text-muted-foreground">{label}</dt>
            <dd className="text-right tabular-nums">{value}</dd>
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
        {rows.map(({ label, value }) => (
          <TableRow key={label}>
            <TableCell className="text-sm">{label}</TableCell>
            <TableCell className="text-right text-sm tabular-nums">
              {value}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
