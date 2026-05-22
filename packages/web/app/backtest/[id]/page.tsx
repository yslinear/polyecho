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

import { t } from "@/lib/i18n";
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
import {
  adaptScenarioKeyedLeader,
  isScenarioKeyedLeader,
} from "./adapt-result";

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
  pending: t("backtest.result.status.pending"),
  queued: t("backtest.result.status.queued"),
  running: t("backtest.result.status.running"),
  completed: t("backtest.result.status.completed"),
  failed: t("backtest.result.status.failed"),
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

/**
 * Per-leader envelope written by `packages/backtest/src/polyecho/backtest/
 * jobs.py:_process_job`. On a clean run, value is the run_triple dict
 * (`scenarios` / `stats` / `pnl_curve`). On a per-leader failure the worker
 * stores `{ error: "ExceptionName: message" }` instead (e.g. the
 * `LiveDataSource must be used as an async context manager` RuntimeError
 * that bit job 557eef18 — P11 Bug 3).
 */
interface LeaderResult {
  scenarios?: ScenarioResult[];
  stats?: ScenarioStats;
  pnl_curve?: DayPoint[];
  error?: string;
}

interface BacktestResult {
  scenarios?: ScenarioResult[];
  /** Single-scenario run */
  stats?: ScenarioStats;
  pnl_curve?: DayPoint[];
  /**
   * Per-leader envelopes keyed by wallet address. Schema written by
   * `_process_job`; preserved (no extra wrapper layer) per P11 contract.
   */
  leaders?: Record<string, LeaderResult>;
  /** Whole-job error envelope (when `_process_job` itself raises). */
  error?: string;
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

  // P11 Bug 3: surface per-leader error envelopes even if the row's terminal
  // status is 'completed' (legacy rows that pre-date P11 Bug 2's
  // failed-on-any-error rule) — silently skipping over them is exactly how
  // job 557eef18 displayed "已完成" with every leader carrying the
  // ``LiveDataSource must be used as an async context manager`` RuntimeError.
  // We collect [wallet, error] pairs and render them in a destructive Alert
  // above the rest of the result body. The result schema is NOT extended —
  // we just READ ``result.leaders[wallet].error`` and ``result.error``.
  const leaderErrors: Array<{ wallet: string; error: string }> = result?.leaders
    ? Object.entries(result.leaders)
        .filter(
          ([, leaderResult]) =>
            leaderResult &&
            typeof leaderResult === "object" &&
            typeof (leaderResult as LeaderResult).error === "string",
        )
        .map(([wallet, leaderResult]) => ({
          wallet,
          error: (leaderResult as LeaderResult).error as string,
        }))
    : [];

  // P13: resolve the scenario triple the UI renders.
  //
  // The page predates the worker's actual JSONB shape. The worker nests a
  // per-leader, scenario-keyed dict (`result.leaders[wallet] = { optimistic,
  // neutral, pessimistic }`) where each scenario carries worker-native field
  // names. The historic UI expected a flat `result.scenarios` / `result.stats`.
  //
  // Resolution order (legacy shapes win — never override an explicit one):
  //   1. `result.scenarios`  → use as-is (legacy / future top-level triple).
  //   2. `result.stats` only → leave to the single-scenario branch below.
  //   3. else, scan `result.leaders` for the FIRST scenario-keyed leader
  //      (the job's `leader_addresses[0]`) and adapt it to ScenarioResult[].
  // A per-leader `{ error }` envelope is NOT scenario-keyed, so step 3 skips
  // it and the destructive Alert above remains the only surface for it.
  const adaptedScenarios: ScenarioResult[] | undefined = (() => {
    if (result?.scenarios || result?.stats) return undefined;
    if (!result?.leaders) return undefined;
    for (const leaderResult of Object.values(result.leaders)) {
      if (isScenarioKeyedLeader(leaderResult)) {
        return adaptScenarioKeyedLeader(leaderResult);
      }
    }
    return undefined;
  })();
  // The scenario triple the completed-state body renders: an explicit
  // top-level `result.scenarios` if present, else the adapter's output.
  const scenarios = result?.scenarios ?? adaptedScenarios;

  return (
    <div className="space-y-6">
      {/* Back → list page (R-list 2026-05-21 UX: detail returns to the
          list, not the create form). The label text remains the i18n key
          to keep prior copy in case operators have it memorised. */}
      <Link
        href="/backtest"
        className="text-sm text-muted-foreground hover:text-foreground underline underline-offset-2"
      >
        {t("backtest.result.back")}
      </Link>

      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">{t("backtest.result.title")}</h1>
          <p className="font-mono text-sm text-muted-foreground mt-0.5 break-all">
            {t("backtest.result.job", { id })}
          </p>
          {job.leader_addresses.length > 0 && (
            <p className="text-sm text-muted-foreground mt-0.5">
              {t("backtest.result.leader")}{" "}
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

      {/* P11 Bug 3: per-leader error envelopes.  Shown ABOVE the rest of
          the result body so an operator viewing a job whose every leader
          failed (e.g. the live ``LiveDataSource must be used as an async
          context manager`` regression) cannot miss it — pre-P11 the page
          rendered "已完成" with no error surface and the scenarios block
          simply didn't appear (silent skip).  The Alert lists the leader
          address (truncated for legibility, full string still in the DOM
          via the wallet attribute) and the raw worker error string. */}
      {leaderErrors.length > 0 && (
        <Card
          className="border-destructive"
          data-testid="backtest-leader-errors"
        >
          <CardHeader className="pb-3">
            <CardTitle className="text-destructive">
              {t("backtest.result.leaderError.title")}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            <p className="text-sm text-muted-foreground">
              {t("backtest.result.leaderError.body")}
            </p>
            <ul className="space-y-2">
              {leaderErrors.map(({ wallet, error }) => (
                <li
                  key={wallet}
                  className="rounded border border-destructive/40 bg-destructive/5 p-3"
                  data-leader={wallet}
                >
                  <p className="font-mono text-xs text-muted-foreground">
                    {wallet.slice(0, 10)}…{wallet.slice(-6)}
                  </p>
                  <p className="mt-1 break-all text-sm text-destructive">
                    {error}
                  </p>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}

      {/* Job metadata */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle>{t("backtest.result.params.title")}</CardTitle>
        </CardHeader>
        <CardContent>
          <dl className="grid grid-cols-2 gap-x-6 gap-y-3 text-sm sm:grid-cols-4">
            <div>
              <dt className="text-xs text-muted-foreground">
                {t("backtest.result.params.period")}
              </dt>
              <dd className="mt-1 font-mono text-xs tabular-nums">
                {job.start_date} → {job.end_date}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">
                {t("backtest.result.params.capital")}
              </dt>
              <dd className="mt-1 tabular-nums">
                {formatUsd(job.initial_capital_usd)}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">
                {t("backtest.result.params.slippage")}
              </dt>
              <dd className="mt-1 tabular-nums">
                {t("backtest.result.params.slippageUnit", {
                  value: job.slippage_bps ?? 0,
                })}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">
                {t("backtest.result.params.delay")}
              </dt>
              <dd className="mt-1 tabular-nums">
                {t("backtest.result.params.delayUnit", {
                  value: job.execution_delay_seconds ?? 0,
                })}
              </dd>
            </div>
          </dl>
        </CardContent>
      </Card>

      {/* Queued state — the worker has not picked this job up yet. */}
      {isPending && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">
              {t("backtest.result.pending.title")}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <p className="text-sm text-muted-foreground">
              {t("backtest.result.pending.body")}
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
              <span>{t("backtest.result.running.title")}</span>
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
              {t("backtest.result.running.body")}
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
            <CardTitle className="text-destructive">
              {t("backtest.result.failed.title")}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <p className="text-sm text-muted-foreground">
              {t("backtest.result.failed.bodyPrefix")}
              <code className="rounded bg-muted px-1 text-xs">
                {/* Literal CLI command — proper noun per common.ts
                    PROPER-NOUN POLICY, intentionally not translated. */}
                {"polyecho logs backtest"}
              </code>
              {t("backtest.result.failed.bodySuffix")}
            </p>
            {result && (
              <pre className="overflow-x-auto rounded bg-muted p-3 text-xs">
                {JSON.stringify(result, null, 2)}
              </pre>
            )}
          </CardContent>
        </Card>
      )}

      {/* Completed: triple scenario.
          `scenarios` is `result.scenarios` when the worker (or a legacy row)
          wrote a top-level triple, else the P13 adapter's output from the
          first scenario-keyed leader in `result.leaders`. */}
      {job.status === "completed" && scenarios && scenarios.length > 0 && (
        <>
          <Separator />
          {/* One comparison surface: scenarios as columns, metrics as rows.
              DESIGN.md bans identical card grids and the hero-metric
              template; a single matrix table is the right affordance for a
              best/base/worst comparison and reads left-to-right at a glance. */}
          <div className="space-y-4">
            <div className="space-y-1">
              <h2 className="text-sm font-semibold text-foreground">
                {t("backtest.result.scenario.title")}
              </h2>
              <p className="text-xs text-muted-foreground">
                {t("backtest.result.scenario.desc")}
              </p>
            </div>
            <ScenarioComparison scenarios={scenarios} />
          </div>

          {/* PnL curve — all 3 scenarios */}
          <Card>
            <CardHeader className="pb-3">
              <CardTitle>{t("backtest.result.pnlCurves")}</CardTitle>
            </CardHeader>
            <CardContent>
              <BacktestPnlChart scenarios={scenarios} />
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
              <CardTitle>{t("backtest.result.statsTitle")}</CardTitle>
            </CardHeader>
            <CardContent>
              <StatsTable stats={result.stats} />
            </CardContent>
          </Card>

          {result.pnl_curve && result.pnl_curve.length > 0 && (
            <Card>
              <CardHeader className="pb-3">
                <CardTitle>{t("backtest.result.pnlCurve")}</CardTitle>
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
/**
 * One comparison matrix: metrics down the rows, the three scenarios across
 * the columns. Replaces the former 3-up identical ScenarioCard grid (which
 * tripped DESIGN.md "no identical card grids" and the per-card big-number
 * "hero-metric template"). This is a single varied surface and directly
 * serves the best/base/worst reading intent.
 */
function ScenarioComparison({ scenarios }: { scenarios: ScenarioResult[] }) {
  // Return / drawdown carry gain/loss meaning -> sign+arrow+tint contract.
  // Win Rate is a magnitude (not a PnL), so it stays a plain tabular figure
  // (color would be a false signal). Mirrors StatsTable's contract.
  const metrics: {
    label: string;
    render: (s: ScenarioStats) => ReactNode;
  }[] = [
    {
      label: t("backtest.result.table.totalReturn"),
      render: (s) => <ReturnPct value={s.total_return_pct} />,
    },
    {
      label: t("backtest.result.table.annualized"),
      render: (s) => <ReturnPct value={s.annualized_return_pct} />,
    },
    { label: t("backtest.result.table.sharpe"), render: (s) => num2(s.sharpe) },
    {
      label: t("backtest.result.table.maxDrawdown"),
      render: (s) => <ReturnPct value={s.max_drawdown_pct} />,
    },
    {
      label: t("backtest.result.table.winRate"),
      render: (s) => `${num2(s.win_rate_pct)}%`,
    },
    {
      label: t("backtest.result.table.avgHold"),
      render: (s) =>
        t("backtest.result.table.avgHoldUnit", { value: num2(s.avg_hold_hours) }),
    },
    {
      label: t("backtest.result.table.trades"),
      render: (s) => String(s.trades_total),
    },
    {
      label: t("backtest.result.table.skipped"),
      render: (s) => String(s.trades_skipped),
    },
  ];

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>{t("backtest.result.table.metric")}</TableHead>
          {scenarios.map((s) => (
            <TableHead key={s.label} className="text-right">
              <span>{t(`backtest.result.scenario.labels.${s.label}`)}</span>
              <span className="block text-xs font-normal tabular-nums text-muted-foreground">
                {t("backtest.result.scenario.paramsCol", {
                  slippage: s.slippage_bps,
                  delay: s.delay_seconds,
                })}
              </span>
            </TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        {metrics.map(({ label, render }) => (
          <TableRow key={label}>
            <TableCell className="text-sm">{label}</TableCell>
            {scenarios.map((s) => (
              <TableCell
                key={s.label}
                className="text-right text-sm tabular-nums"
              >
                {render(s.stats)}
              </TableCell>
            ))}
          </TableRow>
        ))}
      </TableBody>
    </Table>
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
      label: t("backtest.result.table.totalReturn"),
      value: <ReturnPct value={stats.total_return_pct} />,
    },
    {
      label: t("backtest.result.table.annualized"),
      value: <ReturnPct value={stats.annualized_return_pct} />,
    },
    { label: t("backtest.result.table.sharpe"), value: num2(stats.sharpe) },
    {
      label: t("backtest.result.table.maxDrawdown"),
      value: <ReturnPct value={stats.max_drawdown_pct} />,
    },
    {
      label: t("backtest.result.table.winRate"),
      value: `${num2(stats.win_rate_pct)}%`,
    },
    {
      label: t("backtest.result.table.avgHold"),
      value: t("backtest.result.table.avgHoldUnit", {
        value: num2(stats.avg_hold_hours),
      }),
    },
    {
      label: t("backtest.result.table.trades"),
      value: String(stats.trades_total),
    },
    {
      label: t("backtest.result.table.skipped"),
      value: String(stats.trades_skipped),
    },
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
          <TableHead>{t("backtest.result.table.metric")}</TableHead>
          <TableHead className="text-right">
            {t("backtest.result.table.value")}
          </TableHead>
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
