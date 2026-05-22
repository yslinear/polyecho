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
import { Eye, RotateCw } from "lucide-react";

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
  type BacktestSortKey,
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
 * than 2. Empty array -> a calm muted "無" placeholder (no em dash:
 * DESIGN.md bans em dashes in UI copy).
 */
function LeadersSummary({ addresses }: { addresses: string[] }) {
  if (addresses.length === 0) {
    return (
      <span className="text-muted-foreground">
        {t("backtest.list.leadersEmpty")}
      </span>
    );
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
// B1: denormalised summary cell helpers + sortable column header
// ---------------------------------------------------------------------------
/**
 * Render a numeric summary value (the B1 denormalised columns arrive as
 * `numeric` strings, or `null` for a not-yet-completed job). `null` becomes
 * a calm muted placeholder (no em dash). `pct` appends a "%" suffix.
 */
function SummaryCell({
  value,
  digits = 2,
  pct = false,
}: {
  value: string | number | null;
  digits?: number;
  pct?: boolean;
}) {
  if (value === null || value === undefined) {
    return (
      <span className="text-muted-foreground">
        {t("backtest.list.summaryEmpty")}
      </span>
    );
  }
  const n = typeof value === "string" ? Number(value) : value;
  if (!Number.isFinite(n)) {
    return (
      <span className="text-muted-foreground">
        {t("backtest.list.summaryEmpty")}
      </span>
    );
  }
  return (
    <span className="tabular-nums">
      {n.toFixed(digits)}
      {pct ? "%" : ""}
    </span>
  );
}

/** The known sort keys (mirrors `BacktestSortKey` in actions.ts). */
const SORT_KEYS: ReadonlySet<string> = new Set<BacktestSortKey>([
  "created_at",
  "total_return_pct",
  "win_rate",
  "max_drawdown_pct",
  "sharpe_ratio",
  "total_trades",
]);

/**
 * A column header that doubles as a sort toggle. Clicking it deep-links the
 * page to `?sort=<key>&dir=<asc|desc>` — when the column is already the
 * active sort, the direction flips; otherwise it defaults to descending.
 * The active column shows a ▲ / ▼ marker (text, never colour-only — A11y).
 */
function SortHead({
  label,
  sortKey,
  activeKey,
  activeDir,
  align = "left",
}: {
  label: string;
  sortKey: BacktestSortKey;
  activeKey: BacktestSortKey;
  activeDir: "asc" | "desc";
  align?: "left" | "right";
}) {
  const isActive = activeKey === sortKey;
  const nextDir: "asc" | "desc" =
    isActive && activeDir === "desc" ? "asc" : "desc";
  const marker = isActive ? (activeDir === "desc" ? " ▼" : " ▲") : "";
  return (
    <Link
      href={`/backtest?sort=${sortKey}&dir=${nextDir}`}
      className={
        "inline-flex items-center font-medium text-foreground underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" +
        (align === "right" ? " justify-end" : "")
      }
      title={t("backtest.list.sortHint")}
      data-sort-key={sortKey}
      data-sort-active={isActive ? "true" : "false"}
    >
      {label}
      {marker}
    </Link>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------
export default async function BacktestListPage({
  searchParams,
}: {
  // Next.js 15: searchParams is a Promise in Server Components.
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  // B1: resolve the sort column/direction from the URL (?sort=&dir=).
  // Anything outside the known allowlist falls back to created_at desc so a
  // hand-edited URL can never break the page or inject an arbitrary column.
  const sp = await searchParams;
  const rawSort = typeof sp.sort === "string" ? sp.sort : "";
  const sortBy: BacktestSortKey = SORT_KEYS.has(rawSort)
    ? (rawSort as BacktestSortKey)
    : "created_at";
  const sortDir: "asc" | "desc" = sp.dir === "asc" ? "asc" : "desc";

  const rows: BacktestJobSummary[] = await listBacktestJobs({
    limit: 50,
    sortBy,
    sortDir,
  });

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
            {/* Sticky header (DESIGN.md Components: "Tables: sticky header").
                Mirrors the /leaders, /accounts, /discovery table treatment. */}
            <TableHeader className="sticky top-0 z-10 bg-muted">
              <TableRow>
                <TableHead>
                  <SortHead
                    label={t("backtest.list.columns.date")}
                    sortKey="created_at"
                    activeKey={sortBy}
                    activeDir={sortDir}
                  />
                </TableHead>
                <TableHead>{t("backtest.list.columns.leaders")}</TableHead>
                <TableHead>{t("backtest.list.columns.status")}</TableHead>
                <TableHead className="text-right">
                  {t("backtest.list.columns.progress")}
                </TableHead>
                {/* B1: denormalised neutral-scenario summary columns,
                    sortable so an operator can rank jobs by return / risk
                    without opening each one. */}
                <TableHead className="text-right">
                  <SortHead
                    label={t("backtest.list.columns.totalReturn")}
                    sortKey="total_return_pct"
                    activeKey={sortBy}
                    activeDir={sortDir}
                    align="right"
                  />
                </TableHead>
                <TableHead className="text-right">
                  <SortHead
                    label={t("backtest.list.columns.winRate")}
                    sortKey="win_rate"
                    activeKey={sortBy}
                    activeDir={sortDir}
                    align="right"
                  />
                </TableHead>
                <TableHead className="text-right">
                  <SortHead
                    label={t("backtest.list.columns.maxDrawdown")}
                    sortKey="max_drawdown_pct"
                    activeKey={sortBy}
                    activeDir={sortDir}
                    align="right"
                  />
                </TableHead>
                <TableHead className="text-right">
                  <SortHead
                    label={t("backtest.list.columns.sharpe")}
                    sortKey="sharpe_ratio"
                    activeKey={sortBy}
                    activeDir={sortDir}
                    align="right"
                  />
                </TableHead>
                <TableHead className="text-right">
                  <SortHead
                    label={t("backtest.list.columns.trades")}
                    sortKey="total_trades"
                    activeKey={sortBy}
                    activeDir={sortDir}
                    align="right"
                  />
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
                    {/* Progress is only meaningful while a job is RUNNING:
                        completed rows are always 100% and pending rows are
                        always 0% (pure noise). Non-running rows show a calm
                        muted placeholder instead. */}
                    <TableCell className="text-right tabular-nums">
                      {r.status === "running" ? (
                        `${progress.toFixed(0)}%`
                      ) : (
                        <span className="text-muted-foreground">
                          {t("backtest.list.progressEmpty")}
                        </span>
                      )}
                    </TableCell>
                    {/* B1: denormalised neutral-scenario summary cells. */}
                    <TableCell className="text-right">
                      <SummaryCell value={r.total_return_pct} pct />
                    </TableCell>
                    <TableCell className="text-right">
                      <SummaryCell value={r.win_rate} digits={3} />
                    </TableCell>
                    <TableCell className="text-right">
                      <SummaryCell value={r.max_drawdown_pct} pct />
                    </TableCell>
                    <TableCell className="text-right">
                      <SummaryCell value={r.sharpe_ratio} />
                    </TableCell>
                    <TableCell className="text-right">
                      <SummaryCell value={r.total_trades} digits={0} />
                    </TableCell>
                    <TableCell className="font-mono text-xs tabular-nums">
                      {r.start_date} → {r.end_date}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {formatUsd(r.initial_capital_usd)}
                    </TableCell>
                    <TableCell className="text-right">
                      {/* Icon-only actions: a label is too wide for this
                          column (重新執行 wrapped to 3 lines). aria-label +
                          title keep the icon buttons accessible + hoverable. */}
                      <div className="flex justify-end gap-1">
                        <Link
                          href={`/backtest/${r.id}`}
                          aria-label={t("backtest.list.actions.view")}
                          title={t("backtest.list.actions.view")}
                          className="inline-flex h-8 w-8 items-center justify-center rounded-md text-primary transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        >
                          <Eye className="h-4 w-4" aria-hidden="true" />
                        </Link>
                        {/* Re-run is a deep-link into the create form; the
                            form pre-fill from a source id is a future
                            enhancement (NOT a write at the list level). */}
                        <Link
                          href={`/backtest/new?from=${r.id}`}
                          aria-label={t("backtest.list.actions.rerun")}
                          title={t("backtest.list.actions.rerun")}
                          className="inline-flex h-8 w-8 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        >
                          <RotateCw className="h-4 w-4" aria-hidden="true" />
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
