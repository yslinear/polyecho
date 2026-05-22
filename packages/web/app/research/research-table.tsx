"use client";

/**
 * ResearchTable — client island for the C1 paper-trading ladder.
 *
 * Read-mostly: each row is a candidate leader showing its `lifecycle_state`
 * (a Badge), its open paper_session stats (PnL / days / trade count) and a
 * gate-progress hint. The ONLY write is the operator promote-to-live action,
 * offered solely on a `trial_ready` row and gated by the shared
 * `InlineConfirm` primitive with `requirePassphrase` — the danger-passphrase
 * money-safety lock (PRD §5.7 危险 tier).
 *
 * Sorting mirrors the /leaders `leaders-table.tsx` idiom (ghost-button <th>
 * + aria-sort). lifecycle_state / paper-stats / paper-days are sortable;
 * leader address + the action cell are not.
 */

import * as React from "react";
import {
  ArrowDown,
  ArrowUp,
  ChevronLeft,
  ChevronRight,
  ChevronsUpDown,
  Lock,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { InlineConfirm } from "@/components/inline-confirm";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { shortAddress, formatSignedUsd, pnlColor, toNum } from "@/lib/format";
import { t } from "@/lib/i18n";
import { promoteToLiveFollow } from "./actions";

/** One /research row — a candidate leader + its (optional) open paper run. */
export interface ResearchRow {
  leader_address: string;
  display_name: string | null;
  /** discovered | candidate | paper | trial_ready | cooldown | retired */
  lifecycle_state: string;
  locked: boolean;
  cooldown_count: number;
  /** paper_session columns — null when the leader has no open session. */
  paper_state: string | null;
  paper_started_at: Date | string | null;
  paper_trade_count: number | null;
  paper_copyable_pnl: string | number | null;
  paper_max_drawdown: string | number | null;
  paper_score: string | number | null;
}

/** Gates the C1 ladder promotes a `paper` row on (roadmap C1-P3). */
const GATE_TRADES = 10;
const GATE_DAYS = 7;

/** Whole-day age of a paper_session, or null when there is no session. */
function paperDays(startedAt: Date | string | null): number | null {
  if (!startedAt) return null;
  const start = startedAt instanceof Date ? startedAt : new Date(startedAt);
  if (Number.isNaN(start.getTime())) return null;
  const ms = Date.now() - start.getTime();
  return Math.max(0, Math.floor(ms / 86_400_000));
}

/** Badge variant per ladder state — DESIGN.md Restrained palette: only the
 *  terminal/cooldown states carry an accent; the rest stay neutral. */
function stateBadgeVariant(
  state: string
): "default" | "secondary" | "destructive" | "outline" {
  switch (state) {
    case "trial_ready":
      return "default";
    case "cooldown":
      return "outline";
    case "retired":
      return "destructive";
    default:
      return "secondary";
  }
}

function stateLabel(state: string): string {
  switch (state) {
    case "discovered":
      return t("research.state.discovered");
    case "candidate":
      return t("research.state.candidate");
    case "paper":
      return t("research.state.paper");
    case "trial_ready":
      return t("research.state.trial_ready");
    case "cooldown":
      return t("research.state.cooldown");
    case "retired":
      return t("research.state.retired");
    default:
      return t("research.state.unknown");
  }
}

/**
 * GateProgress — the col.gate cell. For a `paper` row it shows whichever
 * gate is not yet met (trade-count, then session-age); a row clearing both
 * shows the ready hint. Non-paper rows have no open session to gate.
 */
function GateProgress({ row }: { row: ResearchRow }) {
  if (row.paper_state == null) {
    return (
      <span className="text-muted-foreground">{t("research.gate.none")}</span>
    );
  }
  const trades = row.paper_trade_count ?? 0;
  const days = paperDays(row.paper_started_at) ?? 0;
  const tradesMet = trades >= GATE_TRADES;
  const daysMet = days >= GATE_DAYS;

  if (tradesMet && daysMet) {
    return (
      <span className="text-muted-foreground">{t("research.gate.ready")}</span>
    );
  }
  return (
    <span className="text-muted-foreground">
      {!tradesMet
        ? t("research.gate.trades", { count: trades, need: GATE_TRADES })
        : t("research.gate.days", { day: days, need: GATE_DAYS })}
    </span>
  );
}

/**
 * PromoteAction — the operator promote-to-live control. Offered ONLY on a
 * `trial_ready`, non-`locked` row; the InlineConfirm primitive with
 * `requirePassphrase` is the danger-passphrase gate. The Server Action
 * re-checks the ladder state authoritatively (a public RPC).
 */
function PromoteAction({ row }: { row: ResearchRow }) {
  if (row.locked) {
    return (
      <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
        <Lock className="h-3 w-3" aria-hidden="true" />
        {t("research.promote.locked")}
      </span>
    );
  }
  if (row.lifecycle_state !== "trial_ready") {
    return (
      <span className="text-xs text-muted-foreground">
        {t("research.promote.notReady")}
      </span>
    );
  }
  return (
    <div className="flex justify-end">
      <InlineConfirm
        title={t("research.promote.title")}
        description={t("research.promote.description")}
        requirePassphrase
        triggerVariant="danger"
        triggerClassName="h-8 gap-1.5 text-xs"
        successToast={t("research.promote.successToast")}
        action={async ({ confirmText, passphrase }) => {
          try {
            return await promoteToLiveFollow({
              leaderAddress: row.leader_address,
              confirmText,
              passphrase,
            });
          } catch {
            return { ok: false, message: t("research.promote.genericError") };
          }
        }}
      />
    </div>
  );
}

type SortKey = "state" | "pnl" | "days" | "trades";
type SortDir = "asc" | "desc";

function sortValue(row: ResearchRow, key: SortKey): number | string {
  switch (key) {
    case "state":
      return row.lifecycle_state;
    case "pnl":
      return toNum(row.paper_copyable_pnl) ?? Number.NEGATIVE_INFINITY;
    case "days":
      return paperDays(row.paper_started_at) ?? -1;
    case "trades":
      return row.paper_trade_count ?? -1;
  }
}

function SortableHead({
  label,
  sortKey,
  active,
  dir,
  onSort,
  align = "left",
}: {
  label: string;
  sortKey: SortKey;
  active: boolean;
  dir: SortDir;
  onSort: (key: SortKey) => void;
  align?: "left" | "right";
}) {
  const Icon = !active ? ChevronsUpDown : dir === "asc" ? ArrowUp : ArrowDown;
  return (
    <TableHead
      aria-sort={active ? (dir === "asc" ? "ascending" : "descending") : "none"}
      className={align === "right" ? "text-right" : undefined}
    >
      <Button
        variant="ghost"
        size="sm"
        className="-ml-3 h-8 data-[active=true]:text-foreground"
        data-active={active}
        onClick={() => onSort(sortKey)}
      >
        {label}
        <Icon className="ml-1 h-3.5 w-3.5" aria-hidden="true" />
      </Button>
    </TableHead>
  );
}

/** Rows per page. The ladder pool can hold thousands of candidates, so
 *  the table paginates client-side (the rows are already in this island
 *  for the client-side sort). */
const RESEARCH_PAGE_SIZE = 50;

export function ResearchTable({ rows }: { rows: ResearchRow[] }) {
  const [sortKey, setSortKey] = React.useState<SortKey>("state");
  const [sortDir, setSortDir] = React.useState<SortDir>("asc");
  const [page, setPage] = React.useState(1);

  function onSort(key: SortKey) {
    setPage(1); // a re-sort restarts at the first page
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("asc");
    }
  }

  const sortedRows = React.useMemo(() => {
    const copy = [...rows];
    copy.sort((a, b) => {
      const av = sortValue(a, sortKey);
      const bv = sortValue(b, sortKey);
      const cmp =
        typeof av === "string" && typeof bv === "string"
          ? av.localeCompare(bv)
          : Number(av) - Number(bv);
      return sortDir === "asc" ? cmp : -cmp;
    });
    return copy;
  }, [rows, sortKey, sortDir]);

  const totalPages = Math.max(
    1,
    Math.ceil(sortedRows.length / RESEARCH_PAGE_SIZE),
  );
  // Clamp: if the row set shrank below the current page, snap back.
  const safePage = Math.min(page, totalPages);
  const pageRows = sortedRows.slice(
    (safePage - 1) * RESEARCH_PAGE_SIZE,
    safePage * RESEARCH_PAGE_SIZE,
  );

  return (
    <div className="overflow-x-auto rounded-lg border">
      <Table className="min-w-[48rem]">
        <TableHeader className="sticky top-0 z-10 bg-muted">
          <TableRow className="hover:bg-transparent">
            <TableHead>{t("research.col.leader")}</TableHead>
            <SortableHead
              label={t("research.col.state")}
              sortKey="state"
              active={sortKey === "state"}
              dir={sortDir}
              onSort={onSort}
            />
            <SortableHead
              label={t("research.col.paperPnl")}
              sortKey="pnl"
              active={sortKey === "pnl"}
              dir={sortDir}
              onSort={onSort}
              align="right"
            />
            <SortableHead
              label={t("research.col.paperDays")}
              sortKey="days"
              active={sortKey === "days"}
              dir={sortDir}
              onSort={onSort}
              align="right"
            />
            <SortableHead
              label={t("research.col.trades")}
              sortKey="trades"
              active={sortKey === "trades"}
              dir={sortDir}
              onSort={onSort}
              align="right"
            />
            <TableHead>{t("research.col.gate")}</TableHead>
            <TableHead className="text-right">
              {t("research.col.action")}
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {pageRows.map((r) => {
            const days = paperDays(r.paper_started_at);
            const hasSession = r.paper_state != null;
            return (
              <TableRow
                key={r.leader_address}
                className="hover:bg-muted/50"
              >
                <TableCell
                  className="font-mono text-xs"
                  title={r.leader_address}
                >
                  {shortAddress(r.leader_address)}
                </TableCell>
                <TableCell>
                  <Badge
                    variant={stateBadgeVariant(r.lifecycle_state)}
                    className="text-xs"
                  >
                    {stateLabel(r.lifecycle_state)}
                  </Badge>
                </TableCell>
                <TableCell
                  className={`whitespace-nowrap text-right text-xs tabular-nums ${
                    hasSession
                      ? pnlColor(r.paper_copyable_pnl)
                      : "text-muted-foreground"
                  }`}
                >
                  {hasSession
                    ? formatSignedUsd(r.paper_copyable_pnl)
                    : t("research.noSession")}
                </TableCell>
                <TableCell className="whitespace-nowrap text-right text-xs tabular-nums text-muted-foreground">
                  {days != null
                    ? t("research.daysValue", { days })
                    : t("research.noSession")}
                </TableCell>
                <TableCell className="whitespace-nowrap text-right text-xs tabular-nums text-muted-foreground">
                  {hasSession
                    ? (r.paper_trade_count ?? 0)
                    : t("research.noSession")}
                </TableCell>
                <TableCell className="text-xs">
                  <GateProgress row={r} />
                </TableCell>
                <TableCell className="text-right">
                  <PromoteAction row={r} />
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
      {totalPages > 1 && (
        <div className="flex items-center justify-between gap-3 border-t bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
          <span>
            {t("research.pagination.total", { count: sortedRows.length })}
          </span>
          <div className="flex items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-7 gap-1 text-xs"
              disabled={safePage <= 1}
              onClick={() => setPage(safePage - 1)}
            >
              <ChevronLeft className="h-3.5 w-3.5" aria-hidden="true" />
              {t("research.pagination.prev")}
            </Button>
            <span className="tabular-nums">
              {t("research.pagination.pageOf", {
                page: safePage,
                total: totalPages,
              })}
            </span>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-7 gap-1 text-xs"
              disabled={safePage >= totalPages}
              onClick={() => setPage(safePage + 1)}
            >
              {t("research.pagination.next")}
              <ChevronRight className="h-3.5 w-3.5" aria-hidden="true" />
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
