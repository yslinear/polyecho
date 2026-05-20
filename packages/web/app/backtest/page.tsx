/**
 * /backtest — Backtest job list (Phase 4 UI fill-in, R-list 2026-05-21).
 *
 * Server Component. Reads recent backtest_jobs rows via the
 * `listBacktestJobs` Server Action — same query path as the future
 * client-side refresh island (no duplicated SQL between SSR and client).
 *
 * Visual contract follows existing tables (leaders / trades / discovery):
 *   header with title + subtitle + prominent "新增回測" button → /backtest/new
 *   either a calm empty state (with the next action named) OR a
 *   shadcn <Table> ordered by created_at desc, limit 50.
 *
 * Write tier: NONE (read-only PRD §5.7 唯讀 tier). The 「重新執行」 action
 * deep-links into `/backtest/new` with the source job id in the query
 * string; the form pre-fill is a future enhancement, NOT a write here.
 */

import Link from "next/link";

import { t } from "@/lib/i18n";
import { formatUsd } from "@/lib/format";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  listBacktestJobs,
  type BacktestJobSummary,
} from "./actions";

// Reads the DB on every request — never prerender at build time
// (no DB available during `next build` / CI). Mirrors /discovery, /leaders.
export const dynamic = "force-dynamic";

// ---------------------------------------------------------------------------
// Status badge — text + variant (never colour-only; DESIGN.md A11y).
// Mirrors the contract used by /backtest/[id]/page.tsx.
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

function statusLabel(status: string): string {
  // Map any of {pending, queued} -> the same label as the detail page.
  if (status === "queued") return t("backtest.list.status.pending");
  if (status === "pending") return t("backtest.list.status.pending");
  if (status === "running") return t("backtest.list.status.running");
  if (status === "completed") return t("backtest.list.status.completed");
  if (status === "failed") return t("backtest.list.status.failed");
  return status;
}

// ---------------------------------------------------------------------------
// Cell helpers
// ---------------------------------------------------------------------------
/**
 * Render the leader-address summary cell: first 2 addresses as a
 * monospace truncated form, with a "+N more" suffix when there are more
 * than 2. Empty array → em-dash placeholder.
 */
function LeadersSummary({ addresses }: { addresses: string[] }) {
  if (addresses.length === 0) {
    return <span className="text-muted-foreground">—</span>;
  }
  const head = addresses.slice(0, 2);
  const moreCount = Math.max(0, addresses.length - head.length);
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {head.map((a) => (
        <span key={a} className="font-mono text-xs tabular-nums">
          {a.slice(0, 8)}…{a.slice(-4)}
        </span>
      ))}
      {moreCount > 0 && (
        <span className="text-xs text-muted-foreground tabular-nums">
          {t("backtest.list.leadersMore", { count: moreCount })}
        </span>
      )}
    </div>
  );
}

/** Format created_at as YYYY-MM-DD HH:mm (tabular, UTC-locale-independent). */
function formatCreated(d: Date): string {
  // toISOString gives a stable, locale-independent shape.  Slice to
  // "YYYY-MM-DD HH:mm" without trailing seconds/timezone (the UI value
  // here is "when, roughly"; the detail page shows the full timestamp).
  return d.toISOString().replace("T", " ").slice(0, 16);
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------
export default async function BacktestListPage() {
  const rows: BacktestJobSummary[] = await listBacktestJobs({ limit: 50 });

  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
        <div className="space-y-1">
          <h1 className="text-2xl font-bold tracking-tight text-foreground">
            {t("backtest.list.title")}
          </h1>
          <p className="max-w-[60ch] text-sm text-muted-foreground">
            {t("backtest.list.subtitle")}
          </p>
        </div>
        {/* Prominent CTA — same anchor style as /discovery → /backtest/new
            so visual consistency is preserved across the data pages. */}
        <Link
          href="/backtest/new"
          className="inline-flex h-9 items-center rounded-md bg-primary px-4 text-sm font-medium text-primary-foreground transition-colors duration-150 ease-out hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
        >
          {t("backtest.list.newButton")}
        </Link>
      </div>

      {rows.length === 0 ? (
        <div className="rounded-lg border border-dashed py-16 text-center">
          <p className="text-sm font-medium text-foreground">
            {t("backtest.list.empty.title")}
          </p>
          <p className="mt-1 text-sm text-muted-foreground">
            {t("backtest.list.empty.hint")}
          </p>
        </div>
      ) : (
        <div className="rounded-lg border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t("backtest.list.columns.date")}</TableHead>
                <TableHead>{t("backtest.list.columns.leaders")}</TableHead>
                <TableHead>{t("backtest.list.columns.status")}</TableHead>
                <TableHead className="text-right">
                  {t("backtest.list.columns.progress")}
                </TableHead>
                <TableHead>{t("backtest.list.columns.window")}</TableHead>
                <TableHead className="text-right">
                  {t("backtest.list.columns.capital")}
                </TableHead>
                <TableHead className="text-right">
                  {t("backtest.list.columns.actions")}
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((r) => {
                const progress = Number(r.progress ?? 0);
                return (
                  <TableRow key={r.id} data-testid="backtest-row">
                    <TableCell className="font-mono text-xs tabular-nums">
                      {formatCreated(r.created_at)}
                    </TableCell>
                    <TableCell>
                      <LeadersSummary addresses={r.leader_addresses} />
                    </TableCell>
                    <TableCell>
                      <Badge
                        variant={STATUS_VARIANT[r.status] ?? "secondary"}
                        data-status={r.status}
                      >
                        {statusLabel(r.status)}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {progress.toFixed(0)}%
                    </TableCell>
                    <TableCell className="font-mono text-xs tabular-nums">
                      {r.start_date} → {r.end_date}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {formatUsd(r.initial_capital_usd)}
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex justify-end gap-3">
                        <Link
                          href={`/backtest/${r.id}`}
                          className="text-sm font-medium text-primary underline-offset-2 hover:underline"
                        >
                          {t("backtest.list.actions.view")}
                        </Link>
                        {/* Re-run is a deep-link into the create form; the
                            form pre-fill from a source id is a future
                            enhancement (NOT a write at the list level). */}
                        <Link
                          href={`/backtest/new?from=${r.id}`}
                          className="text-sm font-medium text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
                        >
                          {t("backtest.list.actions.rerun")}
                        </Link>
                      </div>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}
